"""Gate: refuse idempotency keys when trigger_id contains :none with nonempty plan."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from src.core.schemas import OrderIntent, Side
from src.core.storage import GraveyardDB
from src.executor import MirrorExecutor
from src.mcp.robinhood_client import MockRobinhoodClient


def test_none_trigger_id_with_orders_refuses_idemp_and_flags_health(tmp_path: Path) -> None:
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    g = GraveyardDB(data_dir)
    ex = MirrorExecutor(client=MockRobinhoodClient(), graveyard=g, data_dir=data_dir)

    orders = [
        OrderIntent("AAPL", Side.LONG, 0.0, "rebalance", 0.15, 0.0),
    ]
    trigger_id = "trump:none:leop:none"

    results = ex.execute_plan(orders, [], trigger_id=trigger_id)

    assert len(results) >= 1
    idemp_path = data_dir / "idempotency.json"
    assert not idemp_path.exists() or json.loads(idemp_path.read_text()) == []

    events = g.get_events(limit=20)
    assert any(e.get("action") == "none_trigger_guard" for e in events)