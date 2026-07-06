"""AUDITOR GATE (PL3-BUG-2) — live snapshot valuation / no day-2 liquidation.

The live get_equity_positions endpoint returns NO market_value (robinhood_client sets it 0.0).
If get_portfolio_snapshot trusted that, cycle 2 would see total_equity ≈ leftover cash → every target
sizes to ~0 shares → executor computes delta = 0 − real_shares < 0 → SELLs the whole book (liquidation).

These gates prove get_portfolio_snapshot re-values unpriced positions from a fresh quote, so equity +
weights reflect reality and a held book at its target stays put (no spurious sells).
"""
from __future__ import annotations
import sys, tempfile, unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.executor import MirrorExecutor
from src.mcp.robinhood_client import Position, Quote, OrderResult
from src.core.storage import GraveyardDB, PersistentLog

PRICES = {"SNDK": 70.0, "BE": 50.0}


class LiveLikeClient:
    """Mimics the REAL client: positions carry market_value=0.0 (endpoint omits it); quotes are live."""
    def __init__(self, holdings: dict[str, float], cash: float):
        self._h = holdings
        self._cash = cash
        self.placed: list[tuple[str, str, float]] = []

    def get_positions(self):
        return [Position(ticker=t, shares=s, avg_cost=0.0, market_value=0.0) for t, s in self._h.items()]

    def get_quote(self, ticker):
        px = PRICES.get(ticker, 10.0)
        return Quote(ticker=ticker, bid=px - 0.01, ask=px + 0.01, last=px, volume=1e7, avg_daily_volume=1e7)

    def get_buying_power(self):
        return self._cash

    def place_market_order(self, ticker, side, shares, is_fractional=True):
        self.placed.append((ticker, side, shares))
        return OrderResult(success=True, order_id="X", filled_shares=abs(shares), avg_fill_price=PRICES.get(ticker, 10.0))


class SnapshotValuationGate(unittest.TestCase):
    def setUp(self):
        self.td = Path(tempfile.mkdtemp())
        self.dd = self.td / "data"; self.dd.mkdir()
        self.ld = self.td / "logs"; self.ld.mkdir()

    def _ex(self, client):
        return MirrorExecutor(client=client, graveyard=GraveyardDB(self.dd), plog=PersistentLog(self.ld), data_dir=self.dd)

    def test_snapshot_values_unpriced_positions_from_quote(self):
        """total_equity + weights must come from shares×quote, not the 0.0 market_value."""
        client = LiveLikeClient({"SNDK": 0.2, "BE": 0.3}, cash=0.5)  # held book, ~$0 leftover cash
        snap = self._ex(client).get_portfolio_snapshot()
        # 0.2*70 + 0.3*50 + 0.5 = 14 + 15 + 0.5 = 29.5  (NOT ~0.5)
        self.assertAlmostEqual(snap["total_equity"], 29.5, places=3)
        self.assertGreater(snap["weights"]["SNDK"], 0.4)  # would be 0.0 pre-fix
        self.assertGreater(snap["weights"]["BE"], 0.4)

    def test_held_book_at_target_does_not_liquidate_on_cycle_2(self):
        """Cycle 2 (held book already == target): execute_plan must place NO sell orders.
        Pre-fix, equity collapsed to ~cash → full-position sells. Post-fix, equity is real → at most a
        sub-$1 cash-buffer trim, which the fractional_min_notional floor skips → zero sells placed."""
        from src.core.schemas import OrderIntent, Side
        client = LiveLikeClient({"SNDK": 0.2, "BE": 0.3}, cash=0.5)
        ex = self._ex(client)
        snap = ex.get_portfolio_snapshot()
        ex.sleeve_usd = snap["total_equity"]  # mirror_agent.py:171 does this
        # day-2 plan: each held name at its (current==target) weight, executor sizes (signed_qty=0)
        plan = [OrderIntent(t, Side.LONG, 0.0, "drift_rebalance", snap["weights"][t], snap["weights"][t])
                for t in client._h]
        ex.execute_plan(plan, snap["raw_positions"], trigger_id="cycle2")
        sells = [o for o in client.placed if o[1] == "sell"]
        self.assertEqual(sells, [], f"cycle 2 must not SELL the held book; got {sells}")


if __name__ == "__main__":
    unittest.main()
