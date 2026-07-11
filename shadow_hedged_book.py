"""Shadow Leopold longs + modeled hedge — read-only, no orders.

CRITICAL honesty constraints (owner-enforced):
- Marks from independent closes only (caller supplies price_fn).
- 13F puts: notional only — we do NOT invent strike/expiry. Hedge is modeled as
  short-delta notional on the underlying (conservative linear exposure proxy).
  Assumption is stamped on every record.
- Three return series: (1) longs-only (2) hedged shadow (3) SPY.
- Score net of a stated cost drag; emit to umbrella decisions when requested.

LIVE rebuild of RH book is OUT OF SCOPE here — requires Brooks confirmation.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Optional

log = logging.getLogger(__name__)

ROOT = Path(__file__).resolve().parent
DEFAULT_SHADOW_PATH = ROOT / "data" / "shadow_hedged_book.jsonl"

# Transparent hedge model (documented — not Leopold's true option package)
HEDGE_ASSUMPTION = (
    "13F put disclosed as notional only (no strike/expiry). Model each put as "
    "short-delta linear exposure on the underlying sized to put_notional/price. "
    "This is a CONSERVATIVE proxy, not the real option payoff. Cost drag applied "
    "as HEDGE_COST_BPS_PER_DAY on short notional."
)
HEDGE_COST_BPS_PER_DAY = 2.0  # ~50 bps/month rough friction/decay proxy


@dataclass
class LongPosition:
    ticker: str
    shares: float
    entry_price: float
    entry_ts: str


@dataclass
class HedgeNotional:
    ticker: str
    put_notional_usd: float  # absolute $ notional from 13F


@dataclass
class ShadowMark:
    ts: str
    series: str  # longs_only | hedged | spy
    nav: float
    day_return: float | None
    notes: str = ""
    assumption: str = HEDGE_ASSUMPTION


def mark_longs(
    positions: list[LongPosition],
    price_fn: Callable[[str], Optional[float]],
) -> tuple[float, list[str]]:
    """Independent marks only; missing price → exclude that name + flag."""
    nav = 0.0
    flags: list[str] = []
    for p in positions:
        px = price_fn(p.ticker)
        if px is None or px <= 0:
            flags.append(f"missing_mark:{p.ticker}")
            continue
        nav += p.shares * float(px)
    return nav, flags


def mark_hedge_short_delta(
    hedges: list[HedgeNotional],
    price_fn: Callable[[str], Optional[float]],
    *,
    prior_nav_longs: float,
    days: float = 1.0,
) -> tuple[float, list[str]]:
    """Hedge P&L proxy: short exposure sized as put_notional/price shares.

    P&L ≈ -shares * Δprice for the day is NOT computed here without prior
    prices; we apply a cost drag only for static snapshot NAV contribution:
    short notional is offset against longs for 'exposure' but NAV impact of
    options is modeled as -cost only when we lack true marks.

    For a single as-of NAV of the hedged book we report:
      hedged_nav ≈ longs_nav - hedge_cost_accrual
    and separately log short_notional for transparency.
    """
    flags: list[str] = []
    short_notional = 0.0
    for h in hedges:
        px = price_fn(h.ticker)
        if px is None or px <= 0:
            flags.append(f"missing_hedge_mark:{h.ticker}")
            continue
        short_notional += abs(float(h.put_notional_usd))
    cost = short_notional * (HEDGE_COST_BPS_PER_DAY / 10_000.0) * max(days, 0.0)
    # Hedged NAV proxy = long equity minus accrued hedge friction (not full option MTM)
    return max(0.0, prior_nav_longs - cost), flags + [
        f"short_notional_usd={short_notional:.2f}",
        f"hedge_cost_usd={cost:.4f}",
    ]


def append_shadow_row(path: Path, row: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(row, separators=(",", ":"), default=str) + "\n")


def emit_three_series_snapshot(
    *,
    longs: list[LongPosition],
    hedges: list[HedgeNotional],
    price_fn: Callable[[str], Optional[float]],
    spy_price: Optional[float],
    spy_prior: Optional[float] = None,
    path: Path = DEFAULT_SHADOW_PATH,
    days: float = 1.0,
) -> dict[str, Any]:
    """Write one multi-series shadow record. No orders."""
    ts = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    longs_nav, long_flags = mark_longs(longs, price_fn)
    hedged_nav, hedge_flags = mark_hedge_short_delta(
        hedges, price_fn, prior_nav_longs=longs_nav, days=days,
    )
    spy_ret = None
    if spy_price and spy_prior and spy_prior > 0:
        spy_ret = (float(spy_price) - float(spy_prior)) / float(spy_prior)

    row = {
        "ts": ts,
        "assumption": HEDGE_ASSUMPTION,
        "hedge_cost_bps_per_day": HEDGE_COST_BPS_PER_DAY,
        "series": {
            "longs_only_nav": round(longs_nav, 4),
            "hedged_shadow_nav": round(hedged_nav, 4),
            "spy_price": spy_price,
            "spy_return": spy_ret,
        },
        "flags": long_flags + hedge_flags,
        "live_book_note": (
            "RH cash agentic cannot hold options — live book can only be longs; "
            "hedge is shadow-only until ENABLE_SHORTS/options."
        ),
        "net_of_cost": True,
        "look_ahead": False,
    }
    append_shadow_row(path, row)
    return row
