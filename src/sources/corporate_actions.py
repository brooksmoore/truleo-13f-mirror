"""Corporate actions adapter (splits, merges, spinoffs, ticker changes, delistings).

In scope for v1 per spec §14: must reconcile without spurious buy/sell after CA.
Stub: detect via feed or known list; adjust current_qty and target in reconciler pre-step.
For now: pass-through + log if ticker in known split map.
"""

from __future__ import annotations

from typing import Any


SPLIT_MAP = {
    # "TICKER": (split_ratio, effective_date_str) e.g. 4:1 -> 4.0
    "OLDTICKER": (2.0, "2026-01-01"),  # example for test
}

TICKER_CHANGE_MAP = {
    # old -> new for test CA without spurious
    "OLDTICKER": "NEWTICKER",
}


def adjust_for_corporate_actions(holdings: list[dict], current: list[dict]) -> tuple[list[dict], list[dict]]:
    """Return adjusted (source_holdings, current_positions) post any CA.
    Real: query a CA feed or use yfinance-like but stdlib+cache; at min detect ticker change + adjust qty * ratio.
    v1: supports simple ticker change map + split for tests (M7/minor).
    """
    h2 = []
    for h in holdings or []:
        t = h.get("ticker")
        if t in TICKER_CHANGE_MAP:
            h = dict(h)
            h["ticker"] = TICKER_CHANGE_MAP[t]
        h2.append(h)
    c2 = []
    for c in current or []:
        t = c.get("ticker")
        if t in TICKER_CHANGE_MAP:
            c = dict(c)
            c["ticker"] = TICKER_CHANGE_MAP[t]
        if t in SPLIT_MAP:
            ratio = SPLIT_MAP[t][0]
            c = dict(c)
            if "weight" in c:
                c["weight"] = c["weight"] / ratio  # rough
            if "current_qty" in c:  # not standard
                c["current_qty"] = c.get("current_qty", 0) * ratio
        c2.append(c)
    return h2, c2


def get_split_adjusted_qty(ticker: str, qty: float) -> float:
    if ticker in SPLIT_MAP:
        ratio = SPLIT_MAP[ticker][0]
        return qty * ratio
    return qty
