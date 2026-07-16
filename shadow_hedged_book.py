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


# ---------------------------------------------------------------------------
# Real short-delta hedge P&L (replaces the cost-only proxy above for the 4-series
# emitter). A put's notional is modeled as a SHORT position sized on a FROZEN basis
# (the underlying's mark the first day the put appears), so when the underlying falls
# the hedge GAINS — the whole point the cost-only model could never express.
# ---------------------------------------------------------------------------
DEFAULT_STATE_PATH = ROOT / "data" / "shadow_hedge_state.json"

HEDGE_ASSUMPTION_PNL = (
    "13F put disclosed as notional only (no strike/expiry). Modeled as a SHORT-DELTA "
    "position on the underlying: shares_short = put_notional_usd / basis_price, basis "
    "FROZEN on the first day the put appears (re-based only on a new 13F). Daily hedge "
    "P&L = -shares_short * (px_today - px_prior), minus HEDGE_COST_BPS_PER_DAY drag on "
    "short notional; accrued on top of longs NAV. This is NOT the true option payoff "
    "(no convexity/strike/expiry) — a conservative linear proxy. Index-ETF puts (e.g. "
    "SMH) are modeled as direct short-delta on the ETF, not a beta model."
)


def load_hedge_state(path: Path = DEFAULT_STATE_PATH) -> dict[str, Any]:
    if Path(path).exists():
        try:
            return json.loads(Path(path).read_text(encoding="utf-8"))
        except Exception:
            log.warning("shadow_hedge_state unreadable; starting fresh")
    return {}


def save_hedge_state(state: dict[str, Any], path: Path = DEFAULT_STATE_PATH) -> None:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(state, indent=2, default=str), encoding="utf-8")
    tmp.replace(p)  # atomic


def mark_hedge_pnl(
    hedges: list[HedgeNotional],
    price_fn: Callable[[str], Optional[float]],
    *,
    state: dict[str, Any],  # THIS series' namespace {ticker: {...}} — mutated in place
    today: str,
    days: float = 1.0,
) -> tuple[float, float, list[str]]:
    """Cumulative short-delta hedge P&L with frozen basis + persisted per-ticker state.

    Returns (total_pnl_cum, short_notional, flags). `total_pnl_cum` is the sum of each
    put's cumulative P&L (from its frozen basis); the caller adds it on top of longs NAV.
    Missing mark → skip that name for the day + flag `missing_hedge_mark:<t>` (never guess);
    the name's last cumulative P&L is carried (frozen, not advanced).
    """
    flags: list[str] = []
    short_notional = 0.0
    total_pnl = 0.0
    cost_rate = HEDGE_COST_BPS_PER_DAY / 10_000.0
    for h in hedges:
        px = price_fn(h.ticker)
        st = state.get(h.ticker)
        if px is None or float(px) <= 0:
            flags.append(f"missing_hedge_mark:{h.ticker}")
            if st:  # carry last-known cumulative P&L, do not advance
                total_pnl += float(st.get("pnl_cum", 0.0))
            continue
        px = float(px)
        notional = abs(float(h.put_notional_usd))
        short_notional += notional
        cost = notional * cost_rate * max(days, 0.0)
        if st is None:
            # inception: freeze basis, no price P&L yet; apply day-1 holding cost only
            state[h.ticker] = {
                "basis_price": px,
                "shares_short": (notional / px),
                "prior_mark": px,
                "pnl_cum": -cost,
                "inception": today,
                "notional": notional,
            }
            total_pnl += -cost
            flags.append(f"hedge_inception:{h.ticker}@{px:.4f}")
        else:
            shares_short = float(st.get("shares_short") or (notional / float(st["basis_price"])))
            day_price_pnl = -shares_short * (px - float(st["prior_mark"]))
            st["pnl_cum"] = float(st.get("pnl_cum", 0.0)) + day_price_pnl - cost
            st["prior_mark"] = px
            st["notional"] = notional
            total_pnl += float(st["pnl_cum"])
    flags.append(f"short_notional_usd={short_notional:.2f}")
    return total_pnl, short_notional, flags


def emit_four_series_snapshot(
    *,
    longs: list[LongPosition],
    hedges_matched: list[HedgeNotional],
    hedges_full: list[HedgeNotional],
    price_fn: Callable[[str], Optional[float]],
    spy_price: Optional[float],
    spy_prior: Optional[float] = None,
    path: Path = DEFAULT_SHADOW_PATH,
    state_path: Path = DEFAULT_STATE_PATH,
    days: float = 1.0,
    today: Optional[str] = None,
) -> dict[str, Any]:
    """Write one shadow record with FOUR series and real short-delta hedge P&L:
    longs_only / hedged_name_matched / hedged_full_basket / SPY. Keeps the old
    `hedged_shadow_nav` key as an alias of hedged_name_matched_nav for one release.
    No orders; shadow/paper only.
    """
    ts = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    today = today or datetime.now(timezone.utc).date().isoformat()

    longs_nav, long_flags = mark_longs(longs, price_fn)

    state = load_hedge_state(state_path)
    ns_matched = state.setdefault("name_matched", {})
    ns_full = state.setdefault("full_basket", {})
    pnl_m, notional_m, flags_m = mark_hedge_pnl(
        hedges_matched, price_fn, state=ns_matched, today=today, days=days
    )
    pnl_f, notional_f, flags_f = mark_hedge_pnl(
        hedges_full, price_fn, state=ns_full, today=today, days=days
    )
    hedged_m_nav = longs_nav + pnl_m
    hedged_f_nav = longs_nav + pnl_f

    # Loud, honest failure if hedges were supplied but NOTHING could be marked.
    if hedges_full and notional_f == 0.0:
        log.error(
            "shadow hedge: ALL full-basket hedge marks missing — hedged_full == longs "
            "(no hedge P&L this run). Check price_fn / Yahoo."
        )

    spy_ret = None
    if spy_price and spy_prior and float(spy_prior) > 0:
        spy_ret = (float(spy_price) - float(spy_prior)) / float(spy_prior)

    # Inception date of the (younger) full-basket series = earliest inception on record.
    inceptions = [
        v.get("inception") for v in ns_full.values() if isinstance(v, dict) and v.get("inception")
    ]
    series_inception = min(inceptions) if inceptions else today

    row = {
        "ts": ts,
        "assumption": HEDGE_ASSUMPTION_PNL,
        "hedge_cost_bps_per_day": HEDGE_COST_BPS_PER_DAY,
        "series": {
            "longs_only_nav": round(longs_nav, 4),
            "hedged_name_matched_nav": round(hedged_m_nav, 4),
            "hedged_full_basket_nav": round(hedged_f_nav, 4),
            "hedged_shadow_nav": round(hedged_m_nav, 4),  # alias (one release)
            "spy_price": spy_price,
            "spy_return": spy_ret,
        },
        "schema_note": "hedged_shadow_nav aliases hedged_name_matched_nav",
        "short_notional": {
            "name_matched": round(notional_m, 2),
            "full_basket": round(notional_f, 2),
        },
        "flags": (
            long_flags
            + [f"name_matched:{x}" for x in flags_m]
            + [f"full_basket:{x}" for x in flags_f]
            + [f"series_inception={series_inception}"]
        ),
        "live_book_note": (
            "RH cash agentic cannot hold options — live book can only be longs; "
            "hedge is shadow-only until ENABLE_SHORTS/options."
        ),
        "net_of_cost": True,
        "look_ahead": False,
    }
    save_hedge_state(state, state_path)
    append_shadow_row(path, row)
    return row


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
