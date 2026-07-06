"""TIER-1 RECONCILER — pure deterministic functions (NO I/O, NO LLM, NO network).

Implements Section 11 EXACTLY:
- Step 0: top-N per sleeve by source weight (Trump midpoint, Leopold exact), min floor after.
- Step 1-2: raw within-sleeve, normalize to sleeve_alloc (0.5).
- Step 3: merge (sum for overlaps).
- Step 4: 15% per-name cap with iterative pro-rata redistribution to fixed point (order-independent).
- Step 5: cross-sleeve trim attribution.
- Step 6: drift band gate (1.0% sleeve OR $min) before emitting order.

Also: internal overlap handling (sum under cap), produce minimal OrderIntent list.
Trump rejected names emitted for attribution shadow basket.

All functions pure: (source_positions, current_positions, cfg) -> RebalancePlan
Unit-testable for convergence, order independence, cap math.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Optional
import hashlib
import json

from .core.schemas import (
    Position,
    OrderIntent,
    RebalancePlan,
    Side,
    CatalystTag,
)
import sys
from pathlib import Path
_CFG_PATH = Path(__file__).resolve().parents[1]
if str(_CFG_PATH) not in sys.path:
    sys.path.insert(0, str(_CFG_PATH))
from config import CFG, Config  # central tunables


def _midpoint(range_str: str) -> float:
    """Trump disclosed ranges -> midpoint. Open top bracket -> floor (e.g. 'over $50M' -> 50M).
    Single implementation (M7); called by sources too. Robust to $ , M/K, case.
    """
    import re
    if not range_str:
        return 0.0
    s = range_str.lower().replace("$", "").replace(",", "").strip()
    # open top
    if "over " in s or s.startswith(">") or ">" in s:
        m = re.search(r"([\d.]+)\s*([mk]?)", s)
        if m:
            val = float(m.group(1))
            unit = m.group(2)
            if unit == "m":
                val *= 1_000_000
            elif unit == "k":
                val *= 1_000
            return val
        return 50_000_000.0
    # range low-high
    m = re.search(r"([\d.]+)\s*([mk]?)\s*[-–]\s*([\d.]+)\s*([mk]?)", s)
    if m:
        lo = float(m.group(1))
        lu = m.group(2)
        hi = float(m.group(3))
        hu = m.group(4) or lu
        # scale each by its unit independently (handles mixed K/M)
        if lu == "m":
            lo *= 1e6
        elif lu == "k":
            lo *= 1000
        if hu == "m":
            hi *= 1e6
        elif hu == "k":
            hi *= 1000
        return (lo + hi) / 2
    # single
    m = re.search(r"([\d.]+)\s*([mk]?)", s)
    if m:
        val = float(m.group(1))
        if m.group(2) == "m":
            val *= 1e6
        elif m.group(2) == "k":
            val *= 1000
        return val
    return 0.0


def select_top_n(
    holdings: list[dict[str, Any]],
    sleeve: str,
    n: int = None,
    min_floor_pct: float = None,
) -> list[Position]:
    n = n or CFG.top_n_per_sleeve
    min_floor_pct = min_floor_pct or CFG.min_position_floor_pct
    """Step 0: rank by source_weight, keep top N. Return Position list (target_weight=0 for now)."""
    # holdings: [{"ticker": , "source_weight": raw$, "side": "long" ...}]
    ranked = sorted(holdings, key=lambda h: h.get("source_weight", 0), reverse=True)[:n]
    poss: list[Position] = []
    for h in ranked:
        p = Position(
            ticker=h["ticker"].upper(),
            side=Side(h.get("side", "long")),
            source=sleeve,  # type: ignore
            source_weight=float(h.get("source_weight", 0.0)),
            target_weight=0.0,
            current_qty=float(h.get("current_qty", 0.0)),
        )
        poss.append(p)
    return poss


def normalize_sleeve(
    positions: list[Position], sleeve_alloc: float
) -> list[Position]:
    """Step 1-2: within-sleeve normalize raw source_weight to sleeve_alloc total."""
    total_raw = sum(p.source_weight for p in positions)
    if total_raw <= 0:
        return positions
    for p in positions:
        p.target_weight = sleeve_alloc * (p.source_weight / total_raw)
    return positions


def merge_sleeves(trump_pos: list[Position], leopold_pos: list[Position]) -> tuple[list[Position], list[dict]]:
    """Step 3: for names in both, SUM the target_weights. Return unified list.
    Detects long/short conflict (M6/§9) and flags without silently netting.
    """
    by_ticker: dict[str, Position] = {}
    conflicts: list[dict] = []
    for p in trump_pos:
        by_ticker[p.ticker] = Position(
            ticker=p.ticker,
            side=p.side,
            source="trump",
            source_weight=p.source_weight,
            target_weight=p.target_weight,
        )
    for p in leopold_pos:
        if p.ticker in by_ticker:
            existing = by_ticker[p.ticker]
            if existing.side != p.side:
                conflicts.append({"ticker": p.ticker, "trump_side": existing.side.value, "leop_side": p.side.value})
                # do not net silently; keep trump side's target, flag for human
                continue
            # overlap same side: sum
            existing.target_weight += p.target_weight
            existing.source_weight += p.source_weight
        else:
            by_ticker[p.ticker] = Position(
                ticker=p.ticker,
                side=p.side,
                source="leopold",
                source_weight=p.source_weight,
                target_weight=p.target_weight,
            )
    return list(by_ticker.values()), conflicts


def _cross_side_conflicts(trump_raw: list[dict], leopold_raw: list[dict]) -> list[dict]:
    """M6/§9: detect same-ticker long-in-one-sleeve / short-in-other across the FULL raw picture,
    even when shorts are filtered out of the executable basket (enable_shorts=False). Surfaces the
    conflict for a human without ever netting or trading the short leg."""
    tside = {h["ticker"].upper(): h.get("side", "long") for h in (trump_raw or [])}
    out: list[dict] = []
    for h in (leopold_raw or []):
        t = h["ticker"].upper()
        ls = h.get("side", "long")
        if t in tside and tside[t] != ls:
            out.append({"ticker": t, "trump_side": tside[t], "leop_side": ls})
    return out


def _apply_cap_iterative(positions: list[Position], cap: float) -> tuple[list[Position], dict]:
    """Step 4: iterative pro-rata cap redistribution until fixed point. Order independent.
    Implements spec §11 literally:
      repeat:
        capped = names w > cap
        if none: break
        set each capped w = cap
        excess = sum(pre_w - cap for capped)
        redistribute full excess pro-rata to currently-uncapped (by their current w)
        # any pushed over will be re-capped in next iteration
      until no exceeds or no receivers left (residual excess = intended cash)
    No mid-pass clamp that leaks weight.
    """
    ws = {p.ticker: float(p.target_weight) for p in positions}
    iteration = 0
    max_iter = 200
    while iteration < max_iter:
        iteration += 1
        pre = dict(ws)
        capped_names = [t for t, w in ws.items() if w > cap + 1e-12]
        if not capped_names:
            break
        excess = sum(max(0.0, pre[t] - cap) for t in capped_names)
        for t in capped_names:
            ws[t] = cap
        if excess <= 1e-12:
            break
        receivers = {t: w for t, w in ws.items() if w < cap - 1e-12}
        r_sum = sum(receivers.values())
        if r_sum <= 0:
            # no uncapped receivers left → remaining excess is residual-to-cash (intended)
            break
        for t, w in list(receivers.items()):
            ws[t] = w + (excess * (w / r_sum) if r_sum > 0 else 0.0)
        # loop: next iter will detect and cap any that now exceed
    # final safety clamp (should be no-op)
    for t in ws:
        if ws[t] > cap + 1e-9:
            ws[t] = cap
    for p in positions:
        p.target_weight = round(ws.get(p.ticker, 0.0), 8)
    trim_attr = {}
    for p in positions:
        if p.target_weight + 1e-9 >= cap:
            trim_attr[p.ticker] = {"capped_at": cap}
    return positions, trim_attr


def apply_min_floor(
    positions: list[Position], floor_pct: float, floor_usd: float, sleeve_total_usd: float, sleeve_alloc: float = 0.5
) -> list[Position]:
    """Drop names below min floor after weighting, redistribute their weight pro-rata.
    floor_pct is 1.5% of the *sleeve* (per spec); compare target (of total) against floor_pct * sleeve_alloc.
    """
    survivors = []
    dropped_weight = 0.0
    eff_floor = floor_pct * sleeve_alloc
    for p in positions:
        target_usd = p.target_weight * sleeve_total_usd
        if p.target_weight >= eff_floor and target_usd >= floor_usd:
            survivors.append(p)
        else:
            dropped_weight += p.target_weight
            p.target_weight = 0.0
    if dropped_weight > 0 and survivors:
        s = sum(p.target_weight for p in survivors)
        if s > 0:
            for p in survivors:
                p.target_weight += dropped_weight * (p.target_weight / s)
    return [p for p in survivors if p.target_weight > 0]


def compute_drift_orders(
    targets: list[Position],
    current_map: dict[str, float],  # ticker -> current weight (of whole mirror sleeve)
    drift_pct: float,
    drift_usd: float,
    sleeve_total_usd: float,
    reason_drift_threshold: float = 0.05,
) -> list[OrderIntent]:
    """Step 6: only emit order if |target_w - current_w| > drift_pct OR dollar delta > drift_usd."""
    orders: list[OrderIntent] = []
    for p in targets:
        curr_w = current_map.get(p.ticker, 0.0)
        delta_w = p.target_weight - curr_w
        delta_usd = delta_w * sleeve_total_usd
        if abs(delta_w) > drift_pct or abs(delta_usd) > drift_usd:
            # qty will be filled by executor using price
            orders.append(
                OrderIntent(
                    ticker=p.ticker,
                    side=p.side,
                    signed_qty=0.0,  # placeholder; executor translates w->shares
                    reason="drift_rebalance" if abs(delta_w) <= reason_drift_threshold else "source_change",
                    target_weight=p.target_weight,
                    current_weight=curr_w,
                )
            )
    # Also emit sells for names no longer in target (source exit)
    for tkr, cw in current_map.items():
        if cw > 0 and tkr not in {pp.ticker for pp in targets}:
            orders.append(
                OrderIntent(
                    ticker=tkr,
                    side=Side.LONG,
                    signed_qty=0.0,
                    reason="source_exit",
                    target_weight=0.0,
                    current_weight=cw,
                )
            )
    return orders


def reconcile(
    trump_raw: list[dict[str, Any]],
    leopold_raw: list[dict[str, Any]],
    current_positions: list[dict[str, Any]],  # [{"ticker":, "shares":, "weight": of sleeve?}]
    cfg: Config = None,
    trump_rejected_raw: Optional[list[dict]] = None,
    sleeve_total_usd: float = 10000.0,  # for floor calc; executor will use real equity
    current_weights: Optional[dict[str, float]] = None,  # ticker -> current mirror weight (0-1)
) -> RebalancePlan:
    cfg = cfg or CFG
    """Main pure entry: returns RebalancePlan with targets + minimal orders."""
    # CRITICAL (§9): we NEVER buy a short position. A 13F lists puts as holdings (side=short); ranking
    # them by $ value would let the fund's largest SHORT bets crowd the top-N and get BOUGHT — a full
    # signal inversion (e.g. buying NVDA the fund is short via puts). Drop shorts from the EXECUTABLE
    # basket unless enable_shorts; conflict detection below still sees the full picture (M6).
    include_shorts = getattr(cfg, "enable_shorts", False)
    sel_trump = trump_raw if include_shorts else [h for h in (trump_raw or []) if h.get("side", "long") == "long"]
    sel_leop = leopold_raw if include_shorts else [h for h in (leopold_raw or []) if h.get("side", "long") == "long"]

    # 0. select top N
    trump_pos = select_top_n(sel_trump, "trump", cfg.top_n_per_sleeve, cfg.min_position_floor_pct)
    leop_pos = select_top_n(sel_leop, "leopold", cfg.top_n_per_sleeve, cfg.min_position_floor_pct)

    # 1-2 normalize each to sleeve alloc
    trump_pos = normalize_sleeve(trump_pos, cfg.sleeve_trump)
    leop_pos = normalize_sleeve(leop_pos, cfg.sleeve_leopold)

    # 3 merge (sum overlaps)
    unified, conflicts = merge_sleeves(trump_pos, leop_pos)
    # M6/§9: when shorts are filtered out of the basket, still surface cross-sleeve long/short conflicts.
    if not include_shorts:
        for cf in _cross_side_conflicts(trump_raw, leopold_raw):
            if cf not in conflicts:
                conflicts.append(cf)

    # apply min floor + redistribute (before cap? spec says after weighting)
    # floor is % of sleeve; pass sleeve_alloc for correct relative (M3)
    sleeve_alloc = cfg.sleeve_trump + cfg.sleeve_leopold
    unified = apply_min_floor(
        unified, cfg.min_position_floor_pct, cfg.min_position_floor_usd, sleeve_total_usd, sleeve_alloc=sleeve_alloc
    )

    # 4 cap + iterative redistribution (fixed point)
    unified, trim_attr = _apply_cap_iterative(unified, cfg.per_name_cap)

    # 5 cross trim attribution already in trim_attr (for logs)
    # residual cash is intentional when caps bind; do NOT renormalize (per spec)

    # 6 drift
    curr_w_map = current_weights or {p["ticker"]: p.get("weight", 0.0) for p in current_positions}
    orders = compute_drift_orders(unified, curr_w_map, cfg.drift_band_pct, cfg.drift_band_usd, sleeve_total_usd, cfg.reason_drift_threshold)

    # rejected for shadow (pass through; tagger will have marked)
    rejected = trump_rejected_raw or []

    plan = RebalancePlan(
        targets=unified,
        orders=orders,
        trump_rejected=rejected,
        notes=[f"trim_attr={trim_attr}"],
        asof=datetime.now(timezone.utc).isoformat(),
        conflicts=conflicts,
    )
    return plan


# --- small helpers for tests ---
def _hash_holdings(holdings: list[dict]) -> str:
    s = json.dumps(sorted(holdings, key=lambda x: x["ticker"]), sort_keys=True)
    return hashlib.sha256(s.encode()).hexdigest()[:12]


if __name__ == "__main__":
    import sys
    from pathlib import Path
    _p = Path(__file__).resolve().parents[1]
    if str(_p) not in sys.path: sys.path.insert(0, str(_p))
    from config import CFG
    trump_h = [
        {"ticker": "NVDA", "source_weight": 50_000_000},
        {"ticker": "ORCL", "source_weight": 30_000_000},
        {"ticker": "AVGO", "source_weight": 10_000_000},
    ]
    leop_h = [
        {"ticker": "NVDA", "source_weight": 1_570_000_000},  # from sample 13F
        {"ticker": "SMH", "source_weight": 2_040_000_000},
    ]
    plan = reconcile(trump_h, leop_h, [], sleeve_total_usd=100_000)
    print("Targets:", [(p.ticker, round(p.target_weight,4)) for p in plan.targets])
    print("Orders:", len(plan.orders))
    print("Reconciler pure math OK (smoke)")
