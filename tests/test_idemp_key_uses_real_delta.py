"""AUDITOR GATE (2026-07-06) — idemp key must be built from the REAL translated delta, not the
reconciler's 0.0 signed_qty placeholder.

Found live: every weight-based order (drift_rebalance, source_change — the ONLY kind the
reconciler ever emits) carries signed_qty=0.0; the real share delta is computed later inside
execute_plan via translate_weight_to_shares. The idemp key was built from intent.signed_qty
BEFORE that translation, so it was always "...:TICKER:side:0.0" regardless of how much drift
existed. Once a ticker's order succeeded once under a given trigger_id (e.g. its very first
live buy), every later rebalance of that ticker with a *different* real size was still keyed
identically and silently skipped as a "duplicate" for the rest of that trigger's lifetime
(here, trigger_id is filing-accession-scoped — ~45 days). This explains "Executor results: []"
on every live cron/attended run since the 2026-06-17 go-live.

This gate proves: two cycles under the SAME trigger_id, with genuinely different real deltas
(driven by price drift moving the position away from target between cycles), must both place —
the second must NOT be skipped as a duplicate merely because the placeholder-era key collided.
"""
from __future__ import annotations
import sys, tempfile, unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.executor import MirrorExecutor
from src.mcp.robinhood_client import Quote, OrderResult
from src.core.storage import GraveyardDB, PersistentLog
from src.core.schemas import OrderIntent, Side


class DriftingPriceClient:
    """First quote makes SNDK under target weight (buy needed); second quote (after cycle 1's
    fill moves current_shares) still leaves it under target by a DIFFERENT amount, so cycle 2's
    real delta differs from cycle 1's — but both orders carry signed_qty=0.0 from the reconciler."""

    def __init__(self):
        self.place_calls = []

    def get_quote(self, ticker):
        return Quote(ticker=ticker, bid=9.99, ask=10.01, last=10.0, volume=1e7, avg_daily_volume=1e7)

    def get_positions(self):
        return []

    def get_buying_power(self):
        return 1000.0

    def place_market_order(self, ticker, side, shares, is_fractional=True):
        self.place_calls.append((ticker, side, shares))
        return OrderResult(True, f"OID{len(self.place_calls)}", filled_shares=abs(shares), avg_fill_price=10.0, reason="filled")


class IdempRealDeltaGate(unittest.TestCase):
    def setUp(self):
        self.td = Path(tempfile.mkdtemp())
        self.dd = self.td / "data"; self.dd.mkdir()
        self.ld = self.td / "logs"; self.ld.mkdir()

    def _weight_order(self, target_weight: float):
        # signed_qty=0.0 placeholder — exactly what reconciler.py actually emits for
        # drift_rebalance/source_change orders (never a nonzero literal).
        return [OrderIntent("SNDK", Side.LONG, 0.0, "drift_rebalance", target_weight, 0.0)]

    def test_second_cycle_different_real_delta_is_not_deduped_as_placeholder_collision(self):
        client = DriftingPriceClient()
        ex = MirrorExecutor(client=client, graveyard=GraveyardDB(self.dd), plog=PersistentLog(self.ld),
                             data_dir=self.dd, sleeve_usd=1000.0)

        # Cycle 1: target 20% of $1000 sleeve @ $10/share -> ~20 shares (minus sizing buffer), current 0.
        ex.execute_plan(self._weight_order(0.20), current_positions=[], trigger_id="SAME_FILING")
        self.assertEqual(len(client.place_calls), 1, "cycle 1 order must place")
        cycle1_qty = client.place_calls[0][2]
        self.assertGreater(cycle1_qty, 0)

        # Cycle 2: SAME trigger_id (no new 13F filing), but target weight has drifted to 35% ->
        # a real delta DIFFERENT from cycle 1's. Pre-fix, both keys collapse to
        # "...:SNDK:buy:0.0" -> cycle 2 is wrongly idemp-skipped even though it's a
        # legitimately different, larger rebalance.
        from src.mcp.robinhood_client import Position as BrokerPosition
        held = [BrokerPosition(ticker="SNDK", shares=cycle1_qty, avg_cost=10.0, market_value=cycle1_qty * 10.0)]
        ex.execute_plan(self._weight_order(0.35), current_positions=held, trigger_id="SAME_FILING")

        self.assertEqual(len(client.place_calls), 2,
                          "a genuinely different-sized rebalance under the same trigger must still place, "
                          "not be silently dropped as a false 'duplicate'")
        self.assertNotAlmostEqual(client.place_calls[1][2], cycle1_qty, places=3)


if __name__ == "__main__":
    unittest.main()
