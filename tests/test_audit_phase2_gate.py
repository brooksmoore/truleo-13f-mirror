"""AUDITOR GATE — Phase 2 Robinhood wiring (PL-3).

These tests are AUDITOR-OWNED. Grok may not weaken assertions or re-implement logic here.

PL3-GATE-1: Fill fabrication — when broker returns success=True but filled_shares=0 (poll timeout),
            the graveyard must record 0 confirmed fill, NOT the intended delta.

PL3-GATE-2: Poll timeout through RealRobinhoodClient — drive the REAL polling loop (not FakeMCPTransport)
            via call_backend injection. Assert: loop actually polls, times out, returns
            success=False, filled_shares=0, reason='poll_timeout'.

PL3-GATE-3: No-fill event is auditable — on poll timeout, graveyard records 'order_accepted_no_fill'
            (not 'fill'), so attribution cannot see a phantom trade.
"""

from __future__ import annotations

import sys
import tempfile
import time
import unittest
from pathlib import Path
from typing import Any, Optional
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from config import CFG, override
from src.executor import MirrorExecutor
from src.mcp.robinhood_client import (
    RealRobinhoodClient,
    Quote,
    Position,
    OrderResult,
)
from src.core.storage import GraveyardDB
from src.core.schemas import OrderIntent, Side


# ---------------------------------------------------------------------------
# Minimal client shim that accepts success=True but filled_shares=0
# (simulates a live poll timeout from the broker — the bug scenario)
# ---------------------------------------------------------------------------
class TimeoutResponseClient:
    """Client whose place_market_order returns success=True, filled_shares=0.
    This is what RealRobinhoodClient produces after poll exhaustion (pre-fix it was BUG;
    post-fix it returns success=False / poll_timeout).
    This shim is used to test the EXECUTOR's handling of such a result."""

    def get_quote(self, ticker: str) -> Quote:
        return Quote(ticker, 139.0, 141.0, 140.0, 10_000_000, 20_000_000, False, "")

    def get_positions(self) -> list[Position]:
        return []

    def get_buying_power(self) -> float:
        return 10_000.0

    def place_market_order(self, ticker: str, side: str, shares: float, is_fractional: bool = True) -> OrderResult:
        # success=True (order was accepted by broker) but filled_shares=0 (poll timed out, fill unconfirmed)
        return OrderResult(success=True, order_id="o-timeout", filled_shares=0.0, avg_fill_price=0.0, reason="accepted")

    def get_open_orders(self, ticker: Optional[str] = None) -> list:
        return []


# ---------------------------------------------------------------------------
# call_backend for driving RealRobinhoodClient polling
# ---------------------------------------------------------------------------
def make_pending_backend(poll_attempts_tracker: list):
    """Backend that: place_market_order returns pending; get_open_orders always returns it open."""
    def backend(tool_name: str, params: dict) -> dict:
        if tool_name == "place_market_order":
            return {
                "order_id": "rh-async-001",
                "status": "pending",
                "filled_shares": 0.0,
                "success": True,
            }
        elif tool_name == "get_open_orders":
            poll_attempts_tracker.append(1)
            # never clears — simulates a hung order
            return {"orders": [{"id": "rh-async-001", "ticker": "NVDA", "side": "buy", "status": "open"}]}
        elif tool_name == "get_order":
            return {"filled_shares": 0.0}
        return {}
    return backend


class PL3FillFabricationGate(unittest.TestCase):
    """PL3-GATE-1/3: executor must NOT fabricate a fill when broker returns 0 filled_shares."""

    def setUp(self):
        self.td = Path(tempfile.mkdtemp())
        self.g = GraveyardDB(self.td)

    def test_pl3_zero_filled_shares_not_recorded_as_fill(self):
        """When client returns success=True but filled_shares=0 (poll timeout / unconfirmed),
        executor must NOT record a 'fill' event with the intended delta in the graveyard.
        If this fails, attribution will see phantom trades that never occurred."""
        ex = MirrorExecutor(
            client=TimeoutResponseClient(),
            graveyard=self.g,
            data_dir=self.td,
            sleeve_usd=10_000,
        )
        intents = [OrderIntent("NVDA", Side.LONG, 1.0, "test", 0.01, 0.0)]
        res = ex.execute_plan(intents, [], trigger_id="gate-no-fabricate")

        self.assertEqual(len(res), 1)

        # The critical assertion: no 'fill' event with signed_qty != 0 in graveyard
        all_events = self.g.get_events(limit=20)
        fill_events = [e for e in all_events if e.get("action") == "fill"]

        for ev in fill_events:
            recorded_qty = float(ev.get("signed_qty") or 0.0)
            self.assertEqual(
                recorded_qty, 0.0,
                f"CRITICAL: graveyard recorded signed_qty={recorded_qty} as 'fill' "
                f"when broker returned filled_shares=0. This fabricates a trade that never occurred."
            )

    def test_pl3_accepted_no_fill_is_logged_for_audit(self):
        """When broker accepts but 0 fills confirmed, an auditable event must exist
        (order_accepted_no_fill) so the position can be tracked externally."""
        ex = MirrorExecutor(
            client=TimeoutResponseClient(),
            graveyard=self.g,
            data_dir=self.td,
            sleeve_usd=10_000,
        )
        intents = [OrderIntent("NVDA", Side.LONG, 1.0, "test", 0.01, 0.0)]
        ex.execute_plan(intents, [], trigger_id="gate-no-fill-log")

        all_events = self.g.get_events(limit=20)
        no_fill_events = [e for e in all_events if e.get("action") == "order_accepted_no_fill"]
        self.assertTrue(
            len(no_fill_events) >= 1,
            f"An 'order_accepted_no_fill' event must be logged when success=True but filled_shares=0; "
            f"events found: {[e.get('action') for e in all_events]}"
        )
        # Confirm it carries the order_id for external tracking
        ev = no_fill_events[0]
        meta_str = ev.get("meta") or "{}"
        import json
        meta = json.loads(meta_str) if isinstance(meta_str, str) else meta_str
        self.assertIn("intended_delta", meta, "intended_delta must be in meta for external tracking")

    def test_pl3_real_fill_still_recorded_correctly(self):
        """Sanity: when filled_shares > 0 (genuine fill), the fill IS recorded with real qty."""
        class RealFillClient:
            def get_quote(self, t): return Quote(t, 139.0, 141.0, 140.0, 10_000_000, 20_000_000, False, "")
            def get_positions(self): return []
            def get_buying_power(self): return 10_000.0
            def place_market_order(self, t, s, shares, is_fractional=True):
                return OrderResult(True, "o-real", 0.75, 140.1, "filled")  # partial fill
            def get_open_orders(self, ticker=None): return []

        ex = MirrorExecutor(
            client=RealFillClient(),
            graveyard=self.g,
            data_dir=self.td,
            sleeve_usd=10_000,
        )
        intents = [OrderIntent("NVDA", Side.LONG, 1.0, "test", 0.01, 0.0)]
        res = ex.execute_plan(intents, [], trigger_id="gate-real-fill")

        fill_events = [e for e in self.g.get_events(limit=20) if e.get("action") == "fill"]
        self.assertEqual(len(fill_events), 1, "a genuine fill must produce exactly one fill event")
        self.assertAlmostEqual(float(fill_events[0].get("signed_qty")), 0.75, places=4,
                               msg="fill event must record real filled_shares (0.75), not intended delta (1.0)")


class PL3RealClientPollGate(unittest.TestCase):
    """PL3-GATE-2: drive RealRobinhoodClient._poll_until_filled_or_timeout through call_backend.

    This is the test that Grok's test suite missed: test 3 in test_phase2_broker.py
    bypasses RealRobinhoodClient entirely (FakeMCPTransport used as direct client).
    This gate drives the REAL client with a call_backend that simulates a hung order.
    """

    def setUp(self):
        self.td = Path(tempfile.mkdtemp())

    def test_pl3_real_client_poll_timeout_returns_fail_safe(self):
        """RealRobinhoodClient with a backend that returns pending + never-clearing open order:
        After order_poll_max_attempts exhausted, must return:
        - success=False
        - filled_shares=0.0
        - reason='poll_timeout'
        Uses override(order_poll_max_attempts=2, order_poll_interval_sec=0) for speed.
        """
        poll_tracker = []
        backend = make_pending_backend(poll_tracker)

        client = RealRobinhoodClient(call_backend=backend)
        cfg_fast = override(order_poll_max_attempts=2, order_poll_interval_sec=0.0)

        # _poll_until_filled_or_timeout does `from config import CFG` locally → patch config module
        with mock.patch("config.CFG", cfg_fast):
            res = client.place_market_order("NVDA", "buy", 1.0)

        self.assertFalse(
            res.success,
            f"Poll timeout must return success=False (fail-safe); got success={res.success}"
        )
        self.assertEqual(
            res.filled_shares, 0.0,
            f"Poll timeout must return filled_shares=0.0; got {res.filled_shares}"
        )
        self.assertIn(
            "poll_timeout", (res.reason or "").lower(),
            f"Poll timeout must surface 'poll_timeout' in reason; got reason={res.reason!r}"
        )
        self.assertGreaterEqual(
            len(poll_tracker), 2,
            f"Must have actually polled get_open_orders at least {2} times; polled {len(poll_tracker)}"
        )

    def test_pl3_real_client_immediate_fill_not_treated_as_timeout(self):
        """Sanity: when place returns filled_shares immediately, no polling should fire."""
        poll_tracker = []

        def immediate_backend(tool_name, params):
            if tool_name == "place_market_order":
                return {"order_id": "o-imm", "status": "filled", "filled_shares": 1.2, "avg_fill_price": 140.5, "success": True}
            elif tool_name == "get_open_orders":
                poll_tracker.append(1)  # should NOT be called
                return {"orders": []}
            return {}

        client = RealRobinhoodClient(call_backend=immediate_backend)
        cfg_fast = override(order_poll_max_attempts=2, order_poll_interval_sec=0.0)

        with mock.patch("config.CFG", cfg_fast):
            res = client.place_market_order("NVDA", "buy", 1.2)

        self.assertTrue(res.success)
        self.assertAlmostEqual(res.filled_shares, 1.2, places=4)
        self.assertEqual(len(poll_tracker), 0, "No polling should happen when fill is immediate")


if __name__ == "__main__":
    unittest.main()
