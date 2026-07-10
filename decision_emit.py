"""Umbrella decisions contract emitter for truleo (portfolio-mirror-agent).

Mirrors hood's decision_emit pattern: schema-valid records, fail-safe append.
Never raises into the trading path.
"""

from __future__ import annotations

import hashlib
import logging
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Optional

logger = logging.getLogger(__name__)

ROOT = Path(__file__).resolve().parent
DEFAULT_DECISIONS_PATH = ROOT / "data" / "decisions.ndjson"
BOT_ID = "truleo"


def _git_sha() -> str:
    try:
        return subprocess.check_output(
            ["git", "rev-parse", "--short", "HEAD"],
            stderr=subprocess.DEVNULL,
            text=True,
            cwd=ROOT,
        ).strip()
    except Exception:
        return "unknown"


def _config_hash() -> str:
    cfg = ROOT / "config.py"
    if cfg.exists():
        return hashlib.sha256(cfg.read_bytes()).hexdigest()[:12]
    return hashlib.sha256(b"truleo-default").hexdigest()[:12]


def ts_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def build_decision_record(
    *,
    kind: str,
    instrument: str,
    reason: str,
    mode: str,
    side: str = "buy",
    qty: float = 0.0,
    ref_price: float | None = None,
    target_weight: float | None = None,
    actual: dict[str, Any] | None = None,
    prediction: dict[str, Any] | None = None,
    benchmarks: dict[str, float] | None = None,
    regime: str = "mirror",
    lineage: dict[str, Any] | None = None,
    ts: str | None = None,
    experiment_id: str | None = None,
    llm_calls: int = 0,
    llm_cost_usd: float = 0.0,
) -> dict[str, Any]:
    ts_val = ts or ts_now()
    intended = None
    if ref_price is not None or target_weight is not None or qty:
        intended = {
            "side": side,
            "qty": float(qty),
        }
        if ref_price is not None:
            intended["ref_price"] = float(ref_price)
        if target_weight is not None:
            intended["target_weight"] = float(target_weight)

    pred = prediction or {"type": "none"}
    lin = dict(lineage or {})
    lin.setdefault("trigger", "13f_mirror")
    lin["llm_calls"] = llm_calls
    lin["llm_cost_usd"] = round(llm_cost_usd, 6)

    content_tag = hashlib.sha256(
        f"{ts_val}:{instrument}:{kind}:{qty}:{ref_price}".encode()
    ).hexdigest()[:8]
    decision_id = f"truleo:{ts_val}:{instrument}:{kind}:{content_tag}"

    return {
        "schema_version": "1.0",
        "decision_id": decision_id,
        "bot_id": BOT_ID,
        "ts": ts_val,
        "kind": kind,
        "instrument": instrument,
        "intended": intended,
        "actual": actual,
        "prediction": pred,
        "reason": (reason or "")[:280],
        "benchmarks_at_decision": benchmarks or {},
        "regime": regime,
        "lineage": lin,
        "provenance": {
            "git_sha": _git_sha(),
            "config_hash": _config_hash(),
            "prompt_hash": hashlib.sha256(b"truleo-mirror-v1").hexdigest()[:12],
        },
        "mode": mode,
        "experiment_id": experiment_id,
    }


def emit_decision_safe(
    path: str | Path,
    record: dict[str, Any],
    *,
    append_fn: Optional[Callable[[str | Path, dict[str, Any]], None]] = None,
) -> bool:
    try:
        if append_fn is None:
            umbrella_root = ROOT.parent / "umbrella"
            if str(umbrella_root) not in sys.path:
                sys.path.insert(0, str(umbrella_root))
            from umbrella_core.decisions import append_decision_atomic

            append_fn = append_decision_atomic
        append_fn(path, record)
        return True
    except Exception as exc:
        logger.warning("truleo decision emit failed (non-fatal): %s", exc)
        return False


def emit_plan_intents(
    plan_orders: list[Any],
    *,
    mode: str = "live",
    path: str | Path | None = None,
    get_price: Optional[Callable[[str], float]] = None,
    filing_id: str | None = None,
    benchmarks: dict[str, float] | None = None,
) -> int:
    """Emit one entry/rebalance/exit decision per reconcile plan order (pre-execution)."""
    out_path = Path(path) if path else DEFAULT_DECISIONS_PATH
    n = 0
    for order in plan_orders or []:
        try:
            ticker = getattr(order, "ticker", None) or (order.get("ticker") if isinstance(order, dict) else None)
            if not ticker:
                continue
            side = getattr(order, "side", None) or (order.get("side") if isinstance(order, dict) else "buy")
            side = str(side).lower()
            if side not in ("buy", "sell"):
                side = "buy"
            tw = getattr(order, "target_weight", None)
            if tw is None and isinstance(order, dict):
                tw = order.get("target_weight")
            qty = getattr(order, "qty", None)
            if qty is None and isinstance(order, dict):
                qty = order.get("qty") or order.get("shares") or 0
            qty = float(qty or 0)
            ref = None
            if get_price:
                try:
                    ref = float(get_price(str(ticker)) or 0) or None
                except Exception:
                    ref = None
            kind = "exit" if side == "sell" else ("rebalance" if tw is not None else "entry")
            rec = build_decision_record(
                kind=kind if kind in ("entry", "exit", "rebalance") else "entry",
                instrument=str(ticker),
                reason=f"13F/mirror plan {side} target_w={tw}",
                mode=mode,
                side=side,
                qty=qty,
                ref_price=ref,
                target_weight=float(tw) if tw is not None else None,
                benchmarks=benchmarks,
                lineage={"trigger": f"filing:{filing_id or 'unknown'}", "source": "reconcile_plan"},
            )
            if emit_decision_safe(out_path, rec):
                n += 1
        except Exception as exc:
            logger.warning("truleo plan emit item failed: %s", exc)
    return n
