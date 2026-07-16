"""Shadow hedged book — full-disclosed-basket series + REAL short-delta P&L.

Fail-before gate tests (GROK_HANDOFF_SHADOW_FULL_BASKET.md). The first test is the
whole point: with the old cost-only model, hedged NAV can NEVER exceed longs-only, so
"did hedging help?" is unanswerable. These prove the new model can gain when a hedge
underlying falls, and that the full-basket series sizes off ALL disclosed puts.
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from shadow_hedged_book import (  # noqa: E402
    HedgeNotional,
    LongPosition,
    emit_four_series_snapshot,
    load_hedge_state,
)


def test_hedged_beats_longs_when_hedge_underlying_falls(tmp_path: Path) -> None:
    """When a hedge underlying drops, the short-delta hedge GAINS → hedged NAV can
    exceed longs-only. This provably fails against the old `longs − cost` model."""
    state = tmp_path / "state.json"
    out = tmp_path / "shadow.jsonl"
    prices = {"AAPL": 100.0, "SMH": 50.0, "SPY": 500.0}

    # Day 1 — inception: freeze SMH basis at 50; no price P&L yet, only day-1 cost drag.
    r1 = emit_four_series_snapshot(
        longs=[LongPosition("AAPL", 10, 100.0, "d1")],
        hedges_matched=[],
        hedges_full=[HedgeNotional("SMH", 500.0)],
        price_fn=lambda t: prices.get(t),
        spy_price=500.0, spy_prior=500.0,
        path=out, state_path=state, days=1.0, today="2026-01-01",
    )
    assert r1["series"]["hedged_full_basket_nav"] <= r1["series"]["longs_only_nav"]

    # Day 2 — SMH falls 10% (50→45), AAPL flat. shares_short = 500/50 = 10;
    # price P&L = -10*(45-50) = +50 → hedged must now BEAT longs.
    prices["SMH"] = 45.0
    r2 = emit_four_series_snapshot(
        longs=[LongPosition("AAPL", 10, 100.0, "d1")],
        hedges_matched=[],
        hedges_full=[HedgeNotional("SMH", 500.0)],
        price_fn=lambda t: prices.get(t),
        spy_price=505.0, spy_prior=500.0,
        path=out, state_path=state, days=1.0, today="2026-01-02",
    )
    assert r2["series"]["hedged_full_basket_nav"] > r2["series"]["longs_only_nav"], (
        r2["series"]["hedged_full_basket_nav"], r2["series"]["longs_only_nav"],
    )


def test_full_basket_sizes_off_all_puts_name_matched_only_matches(tmp_path: Path) -> None:
    """full_basket sizes short notional off ALL disclosed puts; name_matched off only
    those overlapping a top-10 long. Alias key + inception flag present."""
    state = tmp_path / "s.json"
    out = tmp_path / "o.jsonl"
    prices = {"MU": 100.0, "TSM": 100.0, "SMH": 100.0, "NVDA": 100.0, "SPY": 500.0}
    longs = [LongPosition("MU", 1, 100.0, "d"), LongPosition("TSM", 1, 100.0, "d")]
    matched = [HedgeNotional("MU", 100.0), HedgeNotional("TSM", 100.0)]
    full = matched + [HedgeNotional("SMH", 100.0), HedgeNotional("NVDA", 100.0)]

    r = emit_four_series_snapshot(
        longs=longs, hedges_matched=matched, hedges_full=full,
        price_fn=lambda t: prices.get(t),
        spy_price=500.0, spy_prior=500.0,
        path=out, state_path=state, days=1.0, today="2026-01-01",
    )
    assert r["short_notional"]["name_matched"] == 200.0
    assert r["short_notional"]["full_basket"] == 400.0
    # alias preserved for one release
    assert r["series"]["hedged_shadow_nav"] == r["series"]["hedged_name_matched_nav"]
    assert "hedged_full_basket_nav" in r["series"]
    assert any("series_inception" in f for f in r["flags"])
    # state persisted with per-ticker basis + prior mark, in both namespaces
    st = load_hedge_state(state)
    assert st["full_basket"]["SMH"]["basis_price"] == 100.0
    assert "prior_mark" in st["full_basket"]["SMH"]


def test_missing_hedge_mark_is_flagged_not_guessed(tmp_path: Path) -> None:
    """A missing hedge underlying mark is skipped + flagged, never invented."""
    state = tmp_path / "s.json"
    out = tmp_path / "o.jsonl"
    prices = {"AAPL": 100.0, "SPY": 500.0}  # SMH absent on purpose
    r = emit_four_series_snapshot(
        longs=[LongPosition("AAPL", 10, 100.0, "d")],
        hedges_matched=[],
        hedges_full=[HedgeNotional("SMH", 500.0)],
        price_fn=lambda t: prices.get(t),
        spy_price=500.0, spy_prior=500.0,
        path=out, state_path=state, days=1.0, today="2026-01-01",
    )
    assert any("missing_hedge_mark:SMH" in f for f in r["flags"])
