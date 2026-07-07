"""Gate: clean-cycle counter tracks intended vs confirmed fills."""

from __future__ import annotations

from pathlib import Path

from src.core.schemas import OrderIntent, Side
from src.core.storage import GraveyardDB
from src.executor import MirrorExecutor
from src.mcp.robinhood_client import MockRobinhoodClient


def test_clean_cycles_increment_on_matching_fill(tmp_path: Path) -> None:
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    ex = MirrorExecutor(client=MockRobinhoodClient(), graveyard=GraveyardDB(data_dir), data_dir=data_dir)
    orders = [OrderIntent("NVDA", Side.LONG, 0.0, "rebalance", 0.10, 0.0)]

    ex.execute_plan(orders, [], trigger_id="trump:f1:leop:a1")
    assert ex.clean_cycles_since_failure() == 1

    ex.execute_plan(orders, ex.client.get_positions(), trigger_id="trump:f1:leop:a1")
    assert ex.clean_cycles_since_failure() == 2


def test_clean_cycles_reset_on_silent_failure(tmp_path: Path) -> None:
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    g = GraveyardDB(data_dir)
    ex = MirrorExecutor(client=MockRobinhoodClient(), graveyard=g, data_dir=data_dir)
    ex._record_clean_cycle(intended_qty=1.0, confirmed_qty=1.0)
    ex._record_clean_cycle(intended_qty=1.0, confirmed_qty=1.0)
    assert ex.clean_cycles_since_failure() == 2

    g.record_event("silent_failure", outcome="mismatch", reject_reason="intended!=filled")
    ex.reset_clean_cycles_on_graveyard_event("silent_failure")
    assert ex.clean_cycles_since_failure() == 0