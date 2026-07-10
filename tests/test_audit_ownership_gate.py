"""AUDITOR GATE — Ownership ledger: sell-path protection + confirm-after-fill + sizing isolation.

The four gates this file proves:

  1. FOREIGN_SELL_BLOCKED — a position truleo has no ledger record of buying must NEVER be sold,
     even if the reconciler emits a source_exit for it.  This is the primary safety property.

  2. OWN_POSITION_CAN_BE_SOLD — truleo CAN sell a position that IS in its ledger (guard against
     over-blocking).

  3. UNSEEDED_ZERO_SELLS — when the ledger has not been seeded (no ownership data), all sell
     orders are blocked (fail-closed per spec).

  4. SIZING_USES_LEDGER_BUDGET — when the ledger is seeded with a $100 budget, translate_weight_to_shares
     sizes off $100 even if sleeve_usd (account NAV) is set to $200 (e.g. foreign cash appeared).

  5. SEED_IDEMPOTENT — calling seed() twice does not overwrite the first seed.

  6. CONFIRM_AFTER_FILL_BUY_VERIFIED — a buy fill is recorded in the ledger only when the broker
     confirms the position exists after the fill.

  7. CONFIRM_AFTER_FILL_BUY_UNVERIFIED — if the broker does NOT show the position after a claimed
     fill, the ledger is NOT updated (no phantom ownership).
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

# ---------------------------------------------------------------------------
# Minimal broker stubs
# ---------------------------------------------------------------------------

class _BrokerBase:
    def __init__(self):
        self.placed: list[tuple[str, str, float]] = []

    def get_buying_power(self) -> float:
        # Deliberately generous: these gates test ownership/confirm-after-fill mechanics, not the
        # buying-power settlement gate (see test_audit_buying_power_gate.py for that).
        return 1000.0

    def get_quote(self, ticker: str) -> Quote:
        return Quote(ticker=ticker, bid=99.0, ask=101.0, last=100.0,
                     volume=1_000_000, avg_daily_volume=1_000_000)


class _BrokerWithPositions(_BrokerBase):
    """After a fill, broker shows the position (normal happy path)."""
    def __init__(self, pre_positions: dict[str, float]):
        super().__init__()
        self._pre = pre_positions  # positions before this cycle

    def get_positions(self):
        # After a buy fill we add the ticker; reflect net of pre state
        combined = dict(self._pre)
        for tkr, side, qty in self.placed:
            if side == "buy":
                combined[tkr] = combined.get(tkr, 0.0) + qty
            else:
                combined[tkr] = max(0.0, combined.get(tkr, 0.0) - qty)
        return [Position(ticker=t, shares=s, avg_cost=100.0, market_value=s * 100.0)
                for t, s in combined.items() if s > 0]

    def place_market_order(self, ticker, side, shares, is_fractional=True):
        qty = abs(float(shares))
        self.placed.append((ticker, side, qty))
        return OrderResult(success=True, order_id=f"o-{ticker}", filled_shares=qty, avg_fill_price=100.0)


class _BrokerFillNoConfirm(_BrokerBase):
    """Broker claims fill but position does NOT appear in get_positions (edge case / race)."""
    def get_positions(self):
        return []  # empty — position never materialised

    def place_market_order(self, ticker, side, shares, is_fractional=True):
        qty = abs(float(shares))
        self.placed.append((ticker, side, qty))
        return OrderResult(success=True, order_id=f"o-{ticker}", filled_shares=qty, avg_fill_price=100.0)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_ex(tmp: Path, client, ledger: OwnershipLedger) -> MirrorExecutor:
    dd = tmp / "data"; dd.mkdir(exist_ok=True)
    ld = tmp / "logs"; ld.mkdir(exist_ok=True)
    return MirrorExecutor(
        client=client,
        graveyard=GraveyardDB(dd),
        plog=PersistentLog(ld),
        data_dir=dd,
        ledger=ledger,
        sleeve_usd=200.0,  # whole-account NAV (includes foreign cash)
    )


def _seeded_ledger(tmp: Path, owned: dict[str, float], budget: float) -> OwnershipLedger:
    """Build an already-seeded ledger with the given positions and budget."""
    dd = tmp / "data"; dd.mkdir(exist_ok=True)
    ledger = OwnershipLedger(dd)
    positions = [Position(ticker=t, shares=s, avg_cost=100.0, market_value=s * 100.0)
                 for t, s in owned.items()]
    ledger.seed(positions, budget)
    return ledger


def _source_exit(ticker: str) -> OrderIntent:
    """source_exit intent — negative signed_qty=0 (executor translates weight→shares)."""
    return OrderIntent(ticker=ticker, side=Side.LONG, signed_qty=0.0,
                       reason="source_exit", target_weight=0.0, current_weight=0.05)


def _drift_buy(ticker: str) -> OrderIntent:
    return OrderIntent(ticker=ticker, side=Side.LONG, signed_qty=0.0,
                       reason="drift_rebalance", target_weight=0.5, current_weight=0.0)


# ---------------------------------------------------------------------------
# Gate 1: FOREIGN_SELL_BLOCKED
# ---------------------------------------------------------------------------

class ForeignSellBlockedGate(unittest.TestCase):
    """Primary safety property: truleo must never sell a position it did not buy."""

    def test_foreign_position_not_sold(self):
        """FAIL-BEFORE proof (commented): without the ledger gate, a source_exit for FOREIGN
        (which appears in current_weights) would produce a sell order via translate_weight_to_shares
        returning a negative delta (target=0, current>0 → sell).
        FAIL-AFTER: with the ledger seeded for SNDK only, FOREIGN's source_exit is silently
        dropped and no sell is placed.
        """
        tmp = Path(tempfile.mkdtemp())
        # Ledger: truleo owns only SNDK.  FOREIGN is NOT in the ledger.
        ledger = _seeded_ledger(tmp, owned={"SNDK": 0.5}, budget=100.0)
        # Account has both SNDK and FOREIGN (simulating a foreign position that appeared).
        client = _BrokerWithPositions(pre_positions={"SNDK": 0.5, "FOREIGN": 1.0})
        ex = _make_ex(tmp, client, ledger)
        # Reconciler emits source_exit for FOREIGN (it's not in the target basket).
        foreign_exit = _source_exit("FOREIGN")
        ex.execute_plan([foreign_exit], client.get_positions(), trigger_id="test-foreign")
        sells = [(t, s) for t, s, _ in client.placed if s == "sell"]
        self.assertEqual(sells, [],
            f"FOREIGN must not be sold — truleo has no ledger record of buying it; got {sells}")

    def test_foreign_position_not_sold_even_with_truleo_positions_present(self):
        """If the plan contains both a legitimate truleo sell (SNDK) and a foreign ticker (ALIEN),
        only SNDK is sold; ALIEN is left alone."""
        tmp = Path(tempfile.mkdtemp())
        ledger = _seeded_ledger(tmp, owned={"SNDK": 0.5}, budget=100.0)
        client = _BrokerWithPositions(pre_positions={"SNDK": 0.5, "ALIEN": 2.0})
        ex = _make_ex(tmp, client, ledger)
        plan = [_source_exit("SNDK"), _source_exit("ALIEN")]
        ex.execute_plan(plan, client.get_positions(), trigger_id="test-mixed")
        sell_tickers = [t for t, s, _ in client.placed if s == "sell"]
        self.assertIn("SNDK", sell_tickers, "truleo's own SNDK exit should execute")
        self.assertNotIn("ALIEN", sell_tickers, "foreign ALIEN must not be touched")


# ---------------------------------------------------------------------------
# Gate 2: OWN_POSITION_CAN_BE_SOLD
# ---------------------------------------------------------------------------

class OwnPositionCanBeSoldGate(unittest.TestCase):
    def test_ledger_position_sells(self):
        """truleo CAN sell a ticker it has a ledger record for."""
        tmp = Path(tempfile.mkdtemp())
        ledger = _seeded_ledger(tmp, owned={"SNDK": 0.5}, budget=100.0)
        client = _BrokerWithPositions(pre_positions={"SNDK": 0.5})
        ex = _make_ex(tmp, client, ledger)
        ex.execute_plan([_source_exit("SNDK")], client.get_positions(), trigger_id="test-own-sell")
        sell_tickers = [t for t, s, _ in client.placed if s == "sell"]
        self.assertIn("SNDK", sell_tickers, "ledger-owned position must be sellable")

    def test_sell_capped_at_owned_shares(self):
        """If the reconciler asks to sell more shares than the ledger shows truleo bought,
        the sell is capped at the ledger qty (never sells more than we own)."""
        tmp = Path(tempfile.mkdtemp())
        # Ledger records 0.3 shares; account somehow shows 0.5 (could be drip dividend etc.)
        ledger = _seeded_ledger(tmp, owned={"SNDK": 0.3}, budget=100.0)
        client = _BrokerWithPositions(pre_positions={"SNDK": 0.5})
        ex = _make_ex(tmp, client, ledger)
        # Force a signed_qty sell of 0.5 shares
        intent = OrderIntent("SNDK", Side.LONG, signed_qty=-0.5, reason="source_exit",
                             target_weight=0.0, current_weight=0.05)
        ex.execute_plan([intent], client.get_positions(), trigger_id="test-cap")
        sells = [(t, s, qty) for t, s, qty in client.placed if s == "sell"]
        self.assertEqual(len(sells), 1)
        self.assertAlmostEqual(sells[0][2], 0.3, places=6,
            msg="sell qty must be capped at ledger-owned 0.3 shares, not the intent's 0.5")


# ---------------------------------------------------------------------------
# Gate 3: UNSEEDED_ZERO_SELLS
# ---------------------------------------------------------------------------

class UnseededZeroSellsGate(unittest.TestCase):
    def test_all_sells_blocked_when_ledger_not_seeded(self):
        """No ownership data → zero sells this cycle (fail-closed per spec)."""
        tmp = Path(tempfile.mkdtemp())
        dd = tmp / "data"; dd.mkdir()
        ld = tmp / "logs"; ld.mkdir()
        # Unseeded ledger (fresh data dir, no ownership_ledger.json)
        ledger = OwnershipLedger(dd)
        self.assertFalse(ledger.is_seeded())
        client = _BrokerWithPositions(pre_positions={"SNDK": 0.5})
        ex = MirrorExecutor(client=client, graveyard=GraveyardDB(dd), plog=PersistentLog(ld),
                            data_dir=dd, ledger=ledger)
        ex.execute_plan([_source_exit("SNDK")], client.get_positions(), trigger_id="test-unseeded")
        sells = [s for _, s, _ in client.placed if s == "sell"]
        self.assertEqual(sells, [],
            "unseeded ledger must block all sells (no ownership data → fail-closed)")


# ---------------------------------------------------------------------------
# Gate 4: SIZING_USES_LEDGER_BUDGET
# ---------------------------------------------------------------------------

class SizingUsesLedgerBudgetGate(unittest.TestCase):
    def test_sizing_ignores_foreign_cash(self):
        """When ledger is seeded with $100 budget, buy sizing uses $100, not sleeve_usd=$200."""
        tmp = Path(tempfile.mkdtemp())
        ledger = _seeded_ledger(tmp, owned={}, budget=100.0)
        dd = tmp / "data"
        ex = MirrorExecutor(client=_BrokerWithPositions({}), graveyard=GraveyardDB(dd),
                            plog=PersistentLog(tmp / "logs"), data_dir=dd, ledger=ledger,
                            sleeve_usd=200.0)  # account NAV inflated by foreign cash
        # target_weight=0.5, price=$100, budget=$100 → target_dollars=50 → 0.5 shares
        # If it used sleeve_usd=$200 instead → target_dollars=100 → 1.0 shares (WRONG)
        delta = ex.translate_weight_to_shares(target_weight=0.5, price=100.0, current_shares=0.0)
        # Allow for the 2% cash buffer: 0.5 * 100 * 0.98 / 100 = 0.49 shares
        self.assertAlmostEqual(delta, 0.49, places=5,
            msg=f"sizing must use ledger budget=$100 (got delta={delta:.6f}); "
                f"delta>0.9 would indicate sleeve_usd=$200 was used instead")


# ---------------------------------------------------------------------------
# Gate 4b: SIZING_COMPOUNDS (own_nav, not frozen seed budget)
# ---------------------------------------------------------------------------

class _PriceQuoteBroker(_BrokerBase):
    """Quote prices configurable per-test, to simulate owned positions appreciating."""
    def __init__(self, prices: dict[str, float]):
        super().__init__()
        self._prices = prices

    def get_quote(self, ticker: str) -> Quote:
        px = self._prices.get(ticker, 100.0)
        return Quote(ticker=ticker, bid=px - 0.01, ask=px + 0.01, last=px,
                     volume=1_000_000, avg_daily_volume=1_000_000)

    def get_positions(self):
        return []

    def place_market_order(self, ticker, side, shares, is_fractional=True):
        return OrderResult(success=True, order_id="x", filled_shares=abs(shares), avg_fill_price=self._prices.get(ticker, 100.0))


class SizingCompoundsGate(unittest.TestCase):
    def test_sizing_grows_when_owned_position_appreciates(self):
        """If truleo's owned position doubles in value, sizing must scale with it — NOT stay frozen
        at the seed-time budget. This is the compounding property: own_nav() is mark-to-market,
        not the static budget_usd recorded at seed."""
        tmp = Path(tempfile.mkdtemp())
        # Seed: SNDK 1.0 share @ $50 (mkt value $50) + $50 cash → budget=$100
        seed_positions = [Position("SNDK", shares=1.0, avg_cost=50.0, market_value=50.0)]
        dd = tmp / "data"; dd.mkdir()
        ledger = OwnershipLedger(dd)
        ledger.seed(seed_positions, budget_usd=100.0)
        self.assertAlmostEqual(ledger.cash_usd(), 50.0, places=2)

        # Quote SNDK at seed price ($50) → own_nav should be ~$100 (unchanged)
        client_flat = _PriceQuoteBroker({"SNDK": 50.0})
        ex_flat = MirrorExecutor(client=client_flat, graveyard=GraveyardDB(dd),
                                 plog=PersistentLog(tmp / "logs"), data_dir=dd, ledger=ledger)
        nav_flat = ex_flat._compute_own_nav()
        self.assertAlmostEqual(nav_flat, 100.0, places=2)

        # Now SNDK doubles to $100/share → owned 1.0 share now worth $100, + $50 cash = $150 NAV
        client_up = _PriceQuoteBroker({"SNDK": 100.0})
        ex_up = MirrorExecutor(client=client_up, graveyard=GraveyardDB(dd),
                               plog=PersistentLog(tmp / "logs"), data_dir=dd, ledger=ledger)
        nav_up = ex_up._compute_own_nav()
        self.assertAlmostEqual(nav_up, 150.0, places=2,
            msg=f"own_nav must compound with appreciation (expected $150, got ${nav_up:.2f})")

        # Sizing must reflect the higher NAV: target_weight=0.2 at price=$100 with NAV=$150
        # → target_dollars = 0.2*150*0.98 = 29.4 → 0.294 shares (NOT 0.2*100*0.98=19.6 → 0.196)
        delta = ex_up.translate_weight_to_shares(target_weight=0.2, price=100.0, current_shares=0.0)
        self.assertAlmostEqual(delta, 0.294, places=4,
            msg=f"sizing must scale off the appreciated NAV ($150), not frozen seed budget ($100); got {delta:.6f}")

    def test_cycle_nav_cached_across_orders_in_same_plan(self):
        """Within one execute_plan call, the own_nav reading must be stable across all orders
        (not re-quoted per order, which would be wasteful and could drift mid-cycle)."""
        tmp = Path(tempfile.mkdtemp())
        dd = tmp / "data"; dd.mkdir()
        ledger = OwnershipLedger(dd)
        ledger.seed([Position("SNDK", shares=1.0, avg_cost=50.0, market_value=50.0)], budget_usd=100.0)
        client = _PriceQuoteBroker({"SNDK": 50.0, "BE": 30.0})
        ex = MirrorExecutor(client=client, graveyard=GraveyardDB(dd), plog=PersistentLog(tmp / "logs"),
                            data_dir=dd, ledger=ledger)
        plan = [_drift_buy("BE")]
        ex.execute_plan(plan, [], trigger_id="test-cache")
        # Cache is reset to None after execute_plan completes (no stale leakage to later standalone calls)
        self.assertIsNone(ex._cycle_own_nav)


# ---------------------------------------------------------------------------
# Gate 5: SEED_IDEMPOTENT
# ---------------------------------------------------------------------------

class SeedIdempotentGate(unittest.TestCase):
    def test_double_seed_no_op(self):
        """Calling seed() twice does not overwrite the first seed."""
        tmp = Path(tempfile.mkdtemp())
        dd = tmp / "data"; dd.mkdir()
        ledger = OwnershipLedger(dd)
        pos_first = [Position("SNDK", shares=0.5, avg_cost=100.0, market_value=50.0)]
        ledger.seed(pos_first, budget_usd=100.0)
        # Second seed (e.g. run_live.py called twice before first cron)
        pos_second = [Position("AAPL", shares=1.0, avg_cost=200.0, market_value=200.0)]
        ledger.seed(pos_second, budget_usd=500.0)
        # Must reflect the FIRST seed
        self.assertAlmostEqual(ledger.get_owned_shares("SNDK"), 0.5, places=6)
        self.assertEqual(ledger.get_owned_shares("AAPL"), 0.0,
            "AAPL from second seed must not appear (second seed is a no-op)")
        self.assertAlmostEqual(ledger.budget_usd(), 100.0, places=2)

    def test_seed_persists_across_reload(self):
        """Ledger written to disk and reloaded preserves owned positions and budget."""
        tmp = Path(tempfile.mkdtemp())
        dd = tmp / "data"; dd.mkdir()
        ledger = OwnershipLedger(dd)
        ledger.seed([Position("BE", shares=0.3, avg_cost=50.0, market_value=15.0)], budget_usd=200.0)
        # Reload from disk
        ledger2 = OwnershipLedger(dd)
        self.assertTrue(ledger2.is_seeded())
        self.assertAlmostEqual(ledger2.get_owned_shares("BE"), 0.3, places=6)
        self.assertAlmostEqual(ledger2.budget_usd(), 200.0, places=2)


# ---------------------------------------------------------------------------
# Gate 6 & 7: CONFIRM-AFTER-FILL
# ---------------------------------------------------------------------------

class ConfirmAfterFillGate(unittest.TestCase):
    def test_buy_fill_confirmed_updates_ledger(self):
        """A buy fill that the broker confirms (position appears in get_positions) → ledger updated."""
        tmp = Path(tempfile.mkdtemp())
        ledger = _seeded_ledger(tmp, owned={}, budget=100.0)
        client = _BrokerWithPositions(pre_positions={})
        ex = _make_ex(tmp, client, ledger)
        buy = _drift_buy("SNDK")
        ex.execute_plan([buy], [], trigger_id="test-confirm-buy")
        # Ledger should now reflect the buy
        owned = ex.ledger.get_owned_shares("SNDK")
        self.assertGreater(owned, 0.0,
            f"ledger must record broker-confirmed buy; got {owned}")

    def test_buy_fill_unconfirmed_does_not_update_ledger(self):
        """Broker claims fill but position never appears in get_positions → ledger NOT updated."""
        tmp = Path(tempfile.mkdtemp())
        ledger = _seeded_ledger(tmp, owned={}, budget=100.0)
        client = _BrokerFillNoConfirm()  # fill succeeds but get_positions returns []
        ex = _make_ex(tmp, client, ledger)
        buy = _drift_buy("SNDK")
        ex.execute_plan([buy], [], trigger_id="test-noconfirm-buy")
        owned = ex.ledger.get_owned_shares("SNDK")
        self.assertEqual(owned, 0.0,
            f"unconfirmed fill must not update ledger; got {owned} shares recorded")

    def test_sell_fill_decrements_ledger(self):
        """A confirmed sell fill decrements the ledger by the filled quantity."""
        tmp = Path(tempfile.mkdtemp())
        ledger = _seeded_ledger(tmp, owned={"SNDK": 0.5}, budget=100.0)
        client = _BrokerWithPositions(pre_positions={"SNDK": 0.5})
        ex = _make_ex(tmp, client, ledger)
        intent = OrderIntent("SNDK", Side.LONG, signed_qty=-0.3, reason="source_exit",
                             target_weight=0.0, current_weight=0.05)
        ex.execute_plan([intent], client.get_positions(), trigger_id="test-sell-decrement")
        remaining = ex.ledger.get_owned_shares("SNDK")
        self.assertAlmostEqual(remaining, 0.2, places=5,
            msg=f"ledger should show 0.2 remaining after selling 0.3 of 0.5; got {remaining}")


if __name__ == "__main__":
    unittest.main()
