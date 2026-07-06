"""AUDITOR GATE (PL13-BUG) — a FAILED placement must stay retryable; a SUCCESSFUL one dedupes.

2026-06-17: the first live --execute failed env-transient, but execute_plan recorded the idemp keys
anyway → the next run skipped all orders ("already done") and placed nothing. Fix: only record the
idemp key when the order actually reached the broker (success). These gates lock that in.
"""
from __future__ import annotations
import sys, tempfile, unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.executor import MirrorExecutor
from src.mcp.robinhood_client import Quote, Position, OrderResult
from src.core.storage import GraveyardDB, PersistentLog
from src.core.schemas import OrderIntent, Side


class CountingClient:
    def __init__(self, succeed: bool):
        self.succeed = succeed
        self.place_calls = 0

    def get_quote(self, ticker):
        return Quote(ticker=ticker, bid=9.99, ask=10.01, last=10.0, volume=1e7, avg_daily_volume=1e7)

    def get_positions(self):
        return []

    def get_buying_power(self):
        return 1000.0

    def place_market_order(self, ticker, side, shares, is_fractional=True):
        self.place_calls += 1
        if self.succeed:
            return OrderResult(True, "OID", filled_shares=abs(shares), avg_fill_price=10.0, reason="filled")
        return OrderResult(False, None, 0.0, 0.0, "broker_error:RuntimeError:transient")


class IdempRetryGate(unittest.TestCase):
    def setUp(self):
        self.td = Path(tempfile.mkdtemp())
        self.dd = self.td / "data"; self.dd.mkdir()
        self.ld = self.td / "logs"; self.ld.mkdir()

    def _order(self):
        return [OrderIntent("SNDK", Side.LONG, 1.0, "source_change", 0.2, 0.0)]

    def test_failed_place_is_retryable(self):
        """A broker failure must NOT record the idemp key → the next run re-attempts the place."""
        client = CountingClient(succeed=False)
        ex = MirrorExecutor(client=client, graveyard=GraveyardDB(self.dd), plog=PersistentLog(self.ld), data_dir=self.dd)
        ex.execute_plan(self._order(), [], trigger_id="T")
        ex.execute_plan(self._order(), [], trigger_id="T")  # same trigger → must retry, not skip
        self.assertEqual(client.place_calls, 2, "failed order must be retried on the next run (not idemp-skipped)")

    def test_successful_place_is_deduped(self):
        """A successful order records the key → an identical re-run is skipped (no double-buy)."""
        client = CountingClient(succeed=True)
        ex = MirrorExecutor(client=client, graveyard=GraveyardDB(self.dd), plog=PersistentLog(self.ld), data_dir=self.dd)
        ex.execute_plan(self._order(), [], trigger_id="T")
        ex.execute_plan(self._order(), [], trigger_id="T")
        self.assertEqual(client.place_calls, 1, "successful order must dedupe on identical re-run")


if __name__ == "__main__":
    unittest.main()
