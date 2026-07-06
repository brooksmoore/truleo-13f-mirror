"""Mirror-basket PersistentLog + GraveyardDB (bot-local copy of pattern from hood to avoid state sharing).

Per spec: use the logging *format* and GraveyardDB *pattern* for consistency, but schema is mirror-native
(no EVThesis). Records rebalances, catalyst tags, vetoes, source changes, attribution snapshots, etc.

Zero external deps beyond stdlib + sqlite3.
"""

from __future__ import annotations

import json
import sqlite3
import threading
from dataclasses import dataclass, asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional, Literal


DB_FILENAME = "graveyard.db"
LOG_FILENAME = "decision_log.jsonl"


@dataclass
class MirrorLogEntry:
    """Structured log for mirror decisions (rebalance, tag, veto, health, attribution)."""
    timestamp: str
    ticker: Optional[str]
    action: str  # "rebalance", "catalyst_tag", "veto", "source_update", "attribution", "health"
    sleeve: Optional[Literal["trump", "leopold", "combined"]] = None
    source_weight: Optional[float] = None
    target_weight: Optional[float] = None
    signed_qty: Optional[float] = None
    outcome: Optional[str] = None  # "filled", "rejected", "skipped", "verified", ...
    reject_reason: Optional[str] = None
    catalyst: Optional[bool] = None
    catalyst_type: Optional[str] = None
    meta: dict[str, Any] = None  # extra: filing_id, hash, prices, etc.


class PersistentLog:
    """Append-only JSONL. Same pattern as main agent."""

    def __init__(self, log_dir: Path):
        self.log_dir = log_dir
        self.log_dir.mkdir(parents=True, exist_ok=True)
        self.path = self.log_dir / LOG_FILENAME
        self._lock = threading.Lock()

    def append(self, entry: MirrorLogEntry) -> None:
        with self._lock:
            with self.path.open("a", encoding="utf-8") as f:
                f.write(json.dumps(asdict(entry), default=str) + "\n")

    def tail(self, n: int = 50) -> list[MirrorLogEntry]:
        if not self.path.exists():
            return []
        lines = self.path.read_text(encoding="utf-8").strip().splitlines()[-n:]
        out: list[MirrorLogEntry] = []
        for ln in lines:
            if ln.strip():
                d = json.loads(ln)
                out.append(MirrorLogEntry(**{k: v for k, v in d.items() if k in MirrorLogEntry.__dataclass_fields__}))
        return out


class GraveyardDB:
    """Queryable store for rejections, source disagreements, skipped fractional, catalyst rejections, etc.
    Designed so attribution + health can query "what was rejected and why".
    """

    def __init__(self, data_dir: Path):
        self.data_dir = data_dir
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.db_path = self.data_dir / DB_FILENAME
        self._conn: Optional[sqlite3.Connection] = None
        self._lock = threading.Lock()
        self._init_db()

    def _get_conn(self) -> sqlite3.Connection:
        if self._conn is None:
            self._conn = sqlite3.connect(
                str(self.db_path), check_same_thread=False, isolation_level=None
            )
            self._conn.row_factory = sqlite3.Row
        return self._conn

    def _init_db(self) -> None:
        conn = self._get_conn()
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS mirror_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp TEXT NOT NULL,
                ticker TEXT,
                action TEXT NOT NULL,
                sleeve TEXT,
                source_weight REAL,
                target_weight REAL,
                signed_qty REAL,
                outcome TEXT,
                reject_reason TEXT,
                catalyst INTEGER,
                catalyst_type TEXT,
                meta TEXT,  -- JSON
                created_at TEXT DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
        conn.execute("CREATE INDEX IF NOT EXISTS idx_mirror_ticker ON mirror_events(ticker)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_mirror_action ON mirror_events(action)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_mirror_outcome ON mirror_events(outcome)")
        # PL-5: composite for fast targeted approval lookup (action + ticker)
        conn.execute("CREATE INDEX IF NOT EXISTS idx_mirror_action_ticker ON mirror_events(action, ticker)")
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS catalyst_cache (
                ticker TEXT PRIMARY KEY,
                catalyst INTEGER NOT NULL,
                type TEXT,
                reason TEXT,
                source_url TEXT,
                confidence REAL,
                ts TEXT,
                context_hash TEXT,
                raw_json TEXT
            )
            """
        )
        conn.commit()

    def record_event(
        self,
        action: str,
        ticker: Optional[str] = None,
        sleeve: Optional[str] = None,
        source_weight: Optional[float] = None,
        target_weight: Optional[float] = None,
        signed_qty: Optional[float] = None,
        outcome: Optional[str] = None,
        reject_reason: Optional[str] = None,
        catalyst: Optional[bool] = None,
        catalyst_type: Optional[str] = None,
        meta: Optional[dict[str, Any]] = None,
    ) -> int:
        ts = datetime.now(timezone.utc).isoformat()
        conn = self._get_conn()
        with self._lock:
            cur = conn.execute(
                """
                INSERT INTO mirror_events
                (timestamp, ticker, action, sleeve, source_weight, target_weight, signed_qty,
                 outcome, reject_reason, catalyst, catalyst_type, meta)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    ts,
                    ticker,
                    action,
                    sleeve,
                    source_weight,
                    target_weight,
                    signed_qty,
                    outcome,
                    reject_reason,
                    1 if catalyst else 0 if catalyst is not None else None,
                    catalyst_type,
                    json.dumps(meta or {}, default=str),
                ),
            )
            conn.commit()
            return cur.lastrowid

    def record_catalyst_tag(self, tag: dict[str, Any]) -> None:
        """Idempotent upsert cache."""
        conn = self._get_conn()
        with self._lock:
            conn.execute(
                """
                INSERT OR REPLACE INTO catalyst_cache
                (ticker, catalyst, type, reason, source_url, confidence, ts, context_hash, raw_json)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    tag["ticker"],
                    1 if tag.get("catalyst") else 0,
                    tag.get("type"),
                    tag.get("reason"),
                    tag.get("source_url"),
                    tag.get("confidence"),
                    tag.get("ts"),
                    tag.get("context_hash"),
                    json.dumps(tag, default=str),
                ),
            )
            conn.commit()

    def get_catalyst(self, ticker: str) -> Optional[dict]:
        conn = self._get_conn()
        row = conn.execute(
            "SELECT raw_json FROM catalyst_cache WHERE ticker = ?", (ticker,)
        ).fetchone()
        if row:
            return json.loads(row["raw_json"])
        return None

    def get_rejected_by_action(self, action: str = "catalyst_reject", limit: Optional[int] = 100) -> list[dict]:
        """PL-6: support limit=None for unbounded query (no silent 100-row truncate in attribution)."""
        conn = self._get_conn()
        if limit is None or limit <= 0:
            rows = conn.execute(
                "SELECT * FROM mirror_events WHERE action = ? ORDER BY id DESC",
                (action,),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM mirror_events WHERE action = ? ORDER BY id DESC LIMIT ?",
                (action, limit),
            ).fetchall()
        return [dict(r) for r in rows]

    def get_events(self, ticker: Optional[str] = None, limit: Optional[int] = 200) -> list[dict]:
        """PL-6/5: support limit=None (unbounded) for approval queries and full-history attribution counts."""
        conn = self._get_conn()
        if limit is None or limit <= 0:
            if ticker:
                rows = conn.execute(
                    "SELECT * FROM mirror_events WHERE ticker = ? ORDER BY id DESC",
                    (ticker,),
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM mirror_events ORDER BY id DESC"
                ).fetchall()
        else:
            if ticker:
                rows = conn.execute(
                    "SELECT * FROM mirror_events WHERE ticker = ? ORDER BY id DESC LIMIT ?",
                    (ticker, limit),
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM mirror_events ORDER BY id DESC LIMIT ?", (limit,)
                ).fetchall()
        return [dict(r) for r in rows]

    def is_ticker_approved(self, ticker: str) -> bool:
        """PL-5: targeted, unbounded, indexed query for catalyst_approved (no aging out after 200 events)."""
        if not ticker:
            return False
        conn = self._get_conn()
        row = conn.execute(
            "SELECT 1 FROM mirror_events WHERE action = 'catalyst_approved' AND ticker = ? LIMIT 1",
            (ticker,),
        ).fetchone()
        return bool(row)


def make_log_entry(action: str, ticker: Optional[str] = None, **meta) -> MirrorLogEntry:
    return MirrorLogEntry(
        timestamp=datetime.now(timezone.utc).isoformat(),
        ticker=ticker,
        action=action,
        meta=meta or {},
    )


if __name__ == "__main__":
    import tempfile
    from pathlib import Path as P
    d = P(tempfile.mkdtemp())
    g = GraveyardDB(d)
    g.record_event("test_veto", ticker="FAKE", outcome="rejected", reject_reason="spread_too_wide")
    print("Graveyard OK, events:", len(g.get_events()))
    pl = PersistentLog(d)
    pl.append(make_log_entry("rebalance", "NVDA", sleeve="trump"))
    print("Log tail:", len(pl.tail()))
