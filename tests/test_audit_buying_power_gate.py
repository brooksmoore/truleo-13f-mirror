"""AUDITOR GATE — settlement-aware buying power (sells-before-buys, real cash gates buys).

FAIL-BEFORE (this already happened live, no toggle needed to reproduce — see graveyard.db /
logs/decision_log.jsonl for 2026-07-09): a single cycle sold SNDK and TSM, then in the same pass
tried to buy CRWV/IREN/APLD sized off the reconciler's target weights. CRWV filled. IREN and APLD
were sent to the broker and REJECTED:
    broker_error:RuntimeError:MCP tool error: [...'detail':'Not enough buying power.'...]
Root cause: order sizing used `ledger.own_nav()` / `ledger.cash_usd()`, which credits sell proceeds
the INSTANT a fill is confirmed — but equities settle T+1/T+2, so that cash isn't actually
spendable yet. The account's real buying power that day was ~$0.60 (confirmed against the owner's
broker screen); the ledger thought it had ~$3.60 after the SNDK+TSM sells.

FIX: execute_plan now runs in two placement passes — all sells first, then buys — and re-queries
REAL broker buying power (client.get_buying_power()) after the sells, before sizing/placing any
buy. A buy whose notional exceeds what's actually available is SKIPPED (clean, logged), never
attempted against the broker. A get_buying_power() failure fails safe (skip all buys, never
overspend).

These gates prove the fix at the executor level with a stub broker reproducing 2026-07-09's exact
shape (2 small sells, 3 buys, only enough real cash for the first, smallest buy).
"""
from __future__ import annotations
import sys, tempfile, unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.executor import MirrorExecutor
from src.ownership_ledger import OwnershipLedger
from src.mcp.robinhood_client import Position, Quote, OrderResult
from src.core.storage import GraveyardDB, PersistentLog
from src.core.schemas import OrderIntent, Side

PRICES = {"SNDK": 1912.01, "TSM": 439.51, "CRWV": 90.40, "IREN": 42.81, "APLD": 32.87}


class _SettlementBroker:
    """Real buying power is independent of the ledger's synthetic cash tracking — this stub lets
    tests set it directly, simulating unsettled sell proceeds not yet being spendable."""
    def __init__(self, buying_power: float, fail_buying_power: bool = False):
        self._bp = buying_power
        self._fail_bp = fail_buying_power
        self.placed: list[tuple[str, str, float]] = []
        self.buying_power_queries = 0

    def get_quote(self, ticker: str) -> Quote:
        px = PRICES.get(ticker, 100.0)
        return Quote(ticker=ticker, bid=px - 0.01, ask=px + 0.01, last=px,
                     volume=1_000_000, avg_daily_volume=1_000_000)

    def get_buying_power(self) -> float:
        self.buying_power_queries += 1
        if self._fail_bp:
            raise RuntimeError("MCP get_portfolio failed (simulated bridge flake)")
        return self._bp

    def get_positions(self):
        return []

    def place_market_order(self, ticker, side, shares, is_fractional=True):
        qty = abs(float(shares))
        self.placed.append((ticker, side, qty))
        return OrderResult(success=True, order_id=f"o-{ticker}", filled_shares=qty, avg_fill_price=PRICES.get(ticker, 100.0))


def _seeded_ledger(tmp: Path, owned: dict[str, float]) -> OwnershipLedger:
    dd = tmp / "data"; dd.mkdir(exist_ok=True)
    ledger = OwnershipLedger(dd)
    positions = [Position(t, shares=s, avg_cost=100.0, market_value=s * PRICES.get(t, 100.0))
                 for t, s in owned.items()]
    ledger.seed(positions, budget_usd=100.0)
    return ledger


def _make_ex(tmp: Path, client, ledger: OwnershipLedger) -> MirrorExecutor:
    dd = tmp / "data"; dd.mkdir(exist_ok=True)
    ld = tmp / "logs"; ld.mkdir(exist_ok=True)
    return MirrorExecutor(client=client, graveyard=GraveyardDB(dd), plog=PersistentLog(ld),
                          data_dir=dd, ledger=ledger)


def _sell(ticker: str, qty: float) -> OrderIntent:
    return OrderIntent(ticker, Side.LONG, signed_qty=-qty, reason="drift_rebalance",
                       target_weight=0.1, current_weight=0.15)


def _buy(ticker: str, qty: float) -> OrderIntent:
    return OrderIntent(ticker, Side.LONG, signed_qty=qty, reason="drift_rebalance",
                       target_weight=0.15, current_weight=0.1)


class SettlementGateTests(unittest.TestCase):
    def test_2026_07_09_scenario_sells_execute_affordable_buy_fills_rest_skip_cleanly(self):
        """Reproduces the exact live shape: SNDK/TSM sells, CRWV/IREN/APLD buys, only enough real
        buying power for the sells + the smallest buy (CRWV). IREN and APLD must be skipped
        BEFORE ever reaching the broker — not attempted-and-rejected."""
        tmp = Path(tempfile.mkdtemp())
        ledger = _seeded_ledger(tmp, owned={"SNDK": 1.0, "TSM": 1.0})
        # $2.00 real buying power: enough for CRWV ($1.49) but not IREN ($2.14) or APLD ($1.64) after.
        client = _SettlementBroker(buying_power=2.00)
        ex = _make_ex(tmp, client, ledger)

        plan = [
            _sell("SNDK", 0.001037),
            _sell("TSM", 0.002332),
            _buy("CRWV", 0.016483),
            _buy("IREN", 0.05),
            _buy("APLD", 0.05),
        ]
        results = ex.execute_plan(plan, [], trigger_id="test-2026-07-09")

        placed_tickers = [t for t, s, q in client.placed]
        self.assertIn("SNDK", placed_tickers, "sell must execute regardless of buy affordability")
        self.assertIn("TSM", placed_tickers, "sell must execute regardless of buy affordability")
        self.assertIn("CRWV", placed_tickers, "the one affordable buy must still fill")
        self.assertNotIn("IREN", placed_tickers,
            "IREN must be skipped BEFORE broker placement — not attempted and rejected")
        self.assertNotIn("APLD", placed_tickers,
            "APLD must be skipped BEFORE broker placement — not attempted and rejected")

        # sells must be placed before any buy (order matters: settlement can't happen retroactively)
        sell_indices = [i for i, (t, s, q) in enumerate(client.placed) if s == "sell"]
        buy_indices = [i for i, (t, s, q) in enumerate(client.placed) if s == "buy"]
        self.assertTrue(sell_indices and buy_indices)
        self.assertLess(max(sell_indices), min(buy_indices),
            "all sells must be placed before any buy")

        # real buying power must be queried exactly once, AFTER the sells (not the ledger's cash_usd)
        self.assertEqual(client.buying_power_queries, 1)

        # skipped buys must be logged as a clean insufficient-funds skip, not a broker failure
        events = ex.graveyard.get_events(limit=None)
        skip_events = {e["ticker"]: e for e in events if e.get("outcome") == "insufficient_buying_power"}
        self.assertIn("IREN", skip_events)
        self.assertIn("APLD", skip_events)

    def test_buying_power_query_failure_fails_safe_skips_all_buys(self):
        """If get_buying_power() itself raises (bridge flake), ALL buys must be skipped — never
        assume unlimited or ledger-derived cash. Sells are unaffected."""
        tmp = Path(tempfile.mkdtemp())
        ledger = _seeded_ledger(tmp, owned={"SNDK": 1.0})
        client = _SettlementBroker(buying_power=999.0, fail_buying_power=True)
        ex = _make_ex(tmp, client, ledger)

        plan = [_sell("SNDK", 0.001037), _buy("CRWV", 0.016483)]
        ex.execute_plan(plan, [], trigger_id="test-bp-fail")

        placed_tickers = [t for t, s, q in client.placed]
        self.assertIn("SNDK", placed_tickers, "sells must still execute even if buying-power check fails")
        self.assertNotIn("CRWV", placed_tickers,
            "buying-power query failure must fail safe: skip ALL buys, never assume cash is available")

        events = ex.graveyard.get_events(limit=None)
        self.assertTrue(any(e.get("action") == "buying_power_check_failed" for e in events))

    def test_no_buys_in_plan_never_queries_buying_power(self):
        """A sell-only cycle should not waste a real broker call checking buying power."""
        tmp = Path(tempfile.mkdtemp())
        ledger = _seeded_ledger(tmp, owned={"SNDK": 1.0})
        client = _SettlementBroker(buying_power=100.0)
        ex = _make_ex(tmp, client, ledger)
        ex.execute_plan([_sell("SNDK", 0.001037)], [], trigger_id="test-sell-only")
        self.assertEqual(client.buying_power_queries, 0)

    def test_buys_fit_within_available_cash_all_fill(self):
        """Sanity: when real buying power comfortably covers every buy, nothing gets skipped."""
        tmp = Path(tempfile.mkdtemp())
        ledger = _seeded_ledger(tmp, owned={})
        client = _SettlementBroker(buying_power=1000.0)
        ex = _make_ex(tmp, client, ledger)
        plan = [_buy("CRWV", 0.016483), _buy("IREN", 0.05), _buy("APLD", 0.05)]
        ex.execute_plan(plan, [], trigger_id="test-plenty")
        placed_tickers = {t for t, s, q in client.placed}
        self.assertEqual(placed_tickers, {"CRWV", "IREN", "APLD"})


if __name__ == "__main__":
    unittest.main()
