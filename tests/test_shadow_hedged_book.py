"""Shadow hedged book — hermetic marks, no fabrication."""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from shadow_hedged_book import (
    HedgeNotional,
    LongPosition,
    emit_three_series_snapshot,
    mark_longs,
)


def test_mark_longs_uses_independent_prices_only() -> None:
    prices = {"AAPL": 200.0, "MSFT": 400.0}
    positions = [
        LongPosition("AAPL", 10, 180.0, "2026-01-01"),
        LongPosition("MSFT", 5, 350.0, "2026-01-01"),
        LongPosition("GONE", 1, 10.0, "2026-01-01"),
    ]
    nav, flags = mark_longs(positions, lambda t: prices.get(t))
    assert nav == 10 * 200 + 5 * 400
    assert any("GONE" in f for f in flags)


def test_emit_three_series_writes_assumption(tmp_path: Path) -> None:
    path = tmp_path / "shadow.jsonl"
    prices = {"AAPL": 100.0, "SPY": 500.0}

    def px(t: str):
        return prices.get(t)

    row = emit_three_series_snapshot(
        longs=[LongPosition("AAPL", 10, 90.0, "2026-01-01")],
        hedges=[HedgeNotional("AAPL", 200.0)],
        price_fn=px,
        spy_price=500.0,
        spy_prior=490.0,
        path=path,
    )
    assert path.exists()
    assert "put" in row["assumption"].lower() or "13F" in row["assumption"]
    assert row["series"]["longs_only_nav"] == 1000.0
    assert row["series"]["hedged_shadow_nav"] < row["series"]["longs_only_nav"]
    assert row["live_book_note"]
