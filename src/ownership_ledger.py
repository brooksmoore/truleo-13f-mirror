"""Ownership ledger — tracks which shares truleo itself placed.

Sell-path gate: truleo may only sell tickers it has ledger records for.
No ownership record → zero sells that cycle (fail-closed per spec).

Sizing base: own_nav() — truleo's own mark-to-market NAV (cash + current
value of owned positions), NOT account-total NAV. Foreign cash/positions
that appear in the account do not inflate truleo's position sizing.
Compounds over time: as owned positions move, sizing tracks them up or down.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Optional

LEDGER_FILENAME = "ownership_ledger.json"


class OwnershipLedger:
    """Persists to data/ownership_ledger.json.

    Schema:
        seeded:     bool
        seed_ts:    ISO timestamp
        budget_usd: float  — truleo's capital allocation AT SEED TIME (reference only,
                             not used for sizing post-seed; see own_nav())
        cash_usd:   float  — truleo's own uninvested cash, tracked via buy/sell cash flows
        positions:  {ticker: shares}
    """

    def __init__(self, data_dir: Path):
        self._path = Path(data_dir) / LEDGER_FILENAME
        self._data: dict[str, Any] = self._load()

    # ------------------------------------------------------------------
    # Persistence
    # ------------------------------------------------------------------

    def _load(self) -> dict[str, Any]:
        if self._path.exists():
            try:
                return json.loads(self._path.read_text())
            except Exception:
                pass
        return {"seeded": False, "positions": {}, "budget_usd": 0.0, "cash_usd": 0.0}

    def _save(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._path.write_text(json.dumps(self._data, indent=2, default=str))

    # ------------------------------------------------------------------
    # Seed (one-time migration)
    # ------------------------------------------------------------------

    def is_seeded(self) -> bool:
        return bool(self._data.get("seeded", False))

    def seed(self, positions: list, budget_usd: float) -> None:
        """Adopt current broker positions as truleo's own at migration time.

        Idempotent: no-op if already seeded (safe to call every startup).
        positions: list of BrokerPosition objects or dicts with ticker/shares/market_value.
        budget_usd: total capital truleo is managing (invested + cash at seed time).
                    Stored for reference; sizing thereafter uses own_nav() (mark-to-market),
                    not this frozen number, so truleo's book compounds.
        """
        if self.is_seeded():
            return
        owned: dict[str, float] = {}
        invested = 0.0
        for p in positions:
            if isinstance(p, dict):
                tkr = str(p.get("ticker", "") or "").upper()
                sh = float(p.get("shares", p.get("current_qty", 0.0)) or 0.0)
                mv = float(p.get("market_value", 0.0) or 0.0)
            else:
                tkr = str(getattr(p, "ticker", "") or "").upper()
                sh = float(getattr(p, "shares", 0.0) or 0.0)
                mv = float(getattr(p, "market_value", 0.0) or 0.0)
            if tkr and sh > 0:
                owned[tkr] = round(sh, 8)
                invested += mv
        budget = float(budget_usd)
        # Leftover cash not tied up in seeded positions (e.g. the $2 buffer in a $100 budget).
        # Clamped to >=0: a quote-valuation shortfall at seed must never manufacture negative cash.
        cash = max(0.0, round(budget - invested, 2))
        self._data = {
            "seeded": True,
            "seed_ts": datetime.now(timezone.utc).isoformat(),
            "budget_usd": round(budget, 2),
            "cash_usd": cash,
            "positions": owned,
        }
        self._save()

    # ------------------------------------------------------------------
    # Reads
    # ------------------------------------------------------------------

    def budget_usd(self) -> float:
        """Seed-time capital allocation. Reference only — NOT the sizing base (see own_nav())."""
        return float(self._data.get("budget_usd", 0.0))

    def cash_usd(self) -> float:
        return float(self._data.get("cash_usd", self._data.get("budget_usd", 0.0)))

    def get_owned_shares(self, ticker: str) -> float:
        return float(self._data.get("positions", {}).get(ticker.upper(), 0.0))

    def get_all_owned(self) -> dict[str, float]:
        return dict(self._data.get("positions", {}))

    def own_nav(self, price_fn: Callable[[str], Optional[float]]) -> float:
        """Truleo's own mark-to-market NAV: own cash + current value of owned positions.

        This is the sizing base — it compounds as owned positions move, unlike the frozen
        seed-time budget_usd. price_fn(ticker) -> price or None; a missing price fails safe
        (that position's contribution is omitted, NAV undercounts rather than fabricates).
        """
        total = self.cash_usd()
        for tkr, shares in self._data.get("positions", {}).items():
            if shares <= 0:
                continue
            try:
                px = price_fn(tkr)
            except Exception:
                px = None
            if px and px > 0:
                total += shares * px
        return total

    # ------------------------------------------------------------------
    # Writes (called after confirmed broker fills only)
    # ------------------------------------------------------------------

    def record_buy(self, ticker: str, shares: float, price: Optional[float] = None) -> None:
        positions = self._data.setdefault("positions", {})
        tkr = ticker.upper()
        positions[tkr] = round(positions.get(tkr, 0.0) + shares, 8)
        if price and price > 0:
            self._data["cash_usd"] = round(self.cash_usd() - shares * price, 2)
        self._save()

    def record_sell(self, ticker: str, shares: float, price: Optional[float] = None) -> None:
        positions = self._data.setdefault("positions", {})
        tkr = ticker.upper()
        current = positions.get(tkr, 0.0)
        positions[tkr] = max(0.0, round(current - shares, 8))
        if price and price > 0:
            self._data["cash_usd"] = round(self.cash_usd() + shares * price, 2)
        self._save()
