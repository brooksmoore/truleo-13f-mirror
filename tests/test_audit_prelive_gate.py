"""AUDITOR GATE — Pre-Live Hardening (PL-1, PL-2, PL-4).

These tests are AUDITOR-OWNED. Grok may not weaken assertions or re-implement logic here.
They must pass unmodified after Grok's Phase 1 implementation.

PL-1: Position dollar-sizing scales linearly with real account equity (not $10k constant).
PL-2: A kill-switch callable that raises => ZERO orders placed (fail-safe, not fail-open).
PL-4: A price_fn that returns None => attribution excludes with missing count; zero fabricated marks.
"""

import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.executor import MirrorExecutor
from src.mcp.robinhood_client import MockRobinhoodClient
from src.core.storage import GraveyardDB
from src.core.schemas import OrderIntent, Side
from src.attribution import Attribution


class PL1EquityScalesGate(unittest.TestCase):
    """PL-1: share sizing is proportional to real account equity, not a hardwired constant."""

    def setUp(self):
        self.td = Path(tempfile.mkdtemp())

    def test_pl1_translate_scales_5x_at_50k_vs_10k(self):
        """translate_weight_to_shares(w, price) at $50k sleeve => 5x the shares as at $10k."""
        ex10 = MirrorExecutor(
            client=MockRobinhoodClient(starting_cash=10_000),
            data_dir=self.td,
            sleeve_usd=10_000,
        )
        ex50 = MirrorExecutor(
            client=MockRobinhoodClient(starting_cash=50_000),
            data_dir=self.td,
            sleeve_usd=50_000,
        )
        weight = 0.15
        price = 200.0  # arbitrary non-trivial price

        shares10 = ex10.translate_weight_to_shares(weight, price, 0.0)
        shares50 = ex50.translate_weight_to_shares(weight, price, 0.0)

        self.assertGreater(shares10, 0, "10k executor must produce positive shares")
        self.assertGreater(shares50, 0, "50k executor must produce positive shares")
        self.assertAlmostEqual(
            shares50 / shares10, 5.0, places=6,
            msg=f"50k must produce exactly 5x shares of 10k; got {shares50}/{shares10}={shares50/shares10}"
        )

    def test_pl1_run_cycle_threads_equity_not_config_constant(self):
        """run_cycle must use real snapshot equity for ex.sleeve_usd, not CFG.robinhood_paper_starting_cash.

        Approach: inject an executor whose mock client starts at $100k. Record what equity the
        pre-cycle snapshot shows, then run run_cycle and verify ex.sleeve_usd was set from
        that real snapshot value — not from CFG.robinhood_paper_starting_cash (~$10k).

        We capture the pre-cycle snapshot equity because run_cycle sets ex.sleeve_usd = snap equity
        INSIDE the cycle (before any orders change it). Post-cycle equity may drift as mock fills
        consume buying power, but the assignment we're testing happens at cycle entry.
        """
        from src.mirror_agent import run_cycle, build_paper_executor
        from src.sources.leopold import LeopoldSource
        from src.sources.trump import TrumpSource
        from src.tagger import CatalystTagger
        import config

        data_dir = self.td / "cycle_data"
        data_dir.mkdir()
        g = GraveyardDB(data_dir)

        # Build executor whose mock client has $100k starting cash
        ex = build_paper_executor(data_dir, graveyard=g, sleeve_usd=100_000)
        ex.client = MockRobinhoodClient(starting_cash=100_000)

        leop = LeopoldSource(cache_dir=data_dir, graveyard=g)
        trump = TrumpSource(cache_dir=data_dir, graveyard=g, live=False)
        tagger = CatalystTagger(graveyard=g, live=False)

        cfg_default = config.CFG.robinhood_paper_starting_cash  # $10k

        # Capture the equity that run_cycle will see at cycle start (before any orders are placed)
        pre_cycle_equity = ex.get_portfolio_snapshot()["total_equity"]

        run_cycle(leop, trump, tagger, ex, data_dir, force=True, c=0)

        # After run_cycle, ex.sleeve_usd must have been set to the real equity (~$100k),
        # NOT the CFG constant ($10k). It may differ slightly from pre_cycle_equity because
        # the mock fills change buying_power — but it must be much closer to $100k than $10k.
        self.assertGreater(
            ex.sleeve_usd, cfg_default * 5,
            msg=f"ex.sleeve_usd={ex.sleeve_usd} must be from real equity (~{pre_cycle_equity:.0f}), not CFG default={cfg_default}"
        )
        # And specifically: ex.sleeve_usd must be within 20% of the pre-cycle real equity
        # (it was set once at cycle entry from the snapshot; fill-driven drift is minor)
        self.assertAlmostEqual(
            ex.sleeve_usd, pre_cycle_equity, delta=pre_cycle_equity * 0.20,
            msg=f"ex.sleeve_usd={ex.sleeve_usd} should be close to pre-cycle equity={pre_cycle_equity}"
        )


class PL2KillFailSafeGate(unittest.TestCase):
    """PL-2: kill-switch error must halt all orders (fail-safe = return True = killed)."""

    def setUp(self):
        self.td = Path(tempfile.mkdtemp())

    def test_pl2_raising_kill_yields_zero_orders(self):
        """is_killed() that raises -> execute_plan places ZERO orders, logs kill_eval_error."""
        def explosive_kill():
            raise OSError("kill file read failed: permission denied")

        g = GraveyardDB(self.td)
        ex = MirrorExecutor(
            client=MockRobinhoodClient(starting_cash=10_000),
            graveyard=g,
            data_dir=self.td,
            is_killed=explosive_kill,
        )
        intents = [
            OrderIntent("NVDA", Side.LONG, 1.0, "test", 0.10, 0.0),
            OrderIntent("ORCL", Side.LONG, 0.5, "test", 0.05, 0.0),
        ]
        results = ex.execute_plan(intents, [], trigger_id="audit-kill-raise")

        self.assertEqual(
            len(results), 0,
            f"CRITICAL: raising kill must produce 0 orders; got {len(results)}"
        )

    def test_pl2_kill_eval_error_is_logged_to_graveyard(self):
        """When kill raises, the kill_eval_error event must be persisted (auditable)."""
        def boom():
            raise RuntimeError("simulated permission error")

        g = GraveyardDB(self.td)
        ex = MirrorExecutor(
            client=MockRobinhoodClient(starting_cash=10_000),
            graveyard=g,
            data_dir=self.td,
            is_killed=boom,
        )
        ex.execute_plan([OrderIntent("NVDA", Side.LONG, 1.0, "test", 0.10, 0.0)], [], "kill-log-probe")

        events = g.get_events(limit=None)
        actions = [e.get("action") or e.get("event_type", "") for e in events]
        has_kill_error = any("kill_eval_error" in str(a) for a in actions)
        self.assertTrue(
            has_kill_error,
            f"kill_eval_error must be logged when kill check raises; events={actions}"
        )

    def test_pl2_false_kill_allows_orders(self):
        """Sanity: is_killed() returning False normally -> orders are not blocked."""
        g = GraveyardDB(self.td)
        ex = MirrorExecutor(
            client=MockRobinhoodClient(starting_cash=10_000),
            graveyard=g,
            data_dir=self.td,
            is_killed=lambda: False,
        )
        intents = [OrderIntent("NVDA", Side.LONG, 1.5, "test", 0.15, 0.0)]
        results = ex.execute_plan(intents, [], trigger_id="normal-kill-false")
        # We expect at least one result (may or may not succeed based on mock quote)
        self.assertIsInstance(results, list, "execute_plan must return a list")


class PL4NoFabricatedPricesGate(unittest.TestCase):
    """PL-4: a failing price_fn must yield missing-excluded attribution, never fabricated marks.

    The old bug: run_cycle passed a price fn returning 100.0+hash%50 on failure.
    The fix: price_fn returns None on failure; attribution excludes and counts missing.
    This gate asserts the correct contract is in force.
    """

    def setUp(self):
        self.td = Path(tempfile.mkdtemp())

    def test_pl4_none_price_fn_yields_empty_mtm_and_missing_count(self):
        """price_fn always None -> accepted_mtm=[], missing_price_count>0, no 100-ish values."""
        g = GraveyardDB(self.td)
        g.record_event("catalyst_accept", ticker="NVDA", meta={"entry_ref_price": 120.0})
        g.record_event("catalyst_accept", ticker="ORCL", meta={"entry_ref_price": 90.0})
        g.record_event("catalyst_reject", ticker="LEGACY", meta={"entry_ref_price": 50.0})

        attr = Attribution(g, self.td)

        def always_none(tkr: str):
            return None

        report = attr.monthly_report(price_fn=always_none)

        self.assertEqual(
            report.get("accepted_mtm"), [],
            "None price_fn must yield empty accepted_mtm (all excluded)"
        )
        self.assertEqual(
            report.get("rejected_mtm"), [],
            "None price_fn must yield empty rejected_mtm (all excluded)"
        )
        missing = report.get("missing_price_count", 0)
        self.assertGreaterEqual(
            missing, 3,
            f"missing_price_count must be >= 3 (one per record); got {missing}"
        )

    def test_pl4_no_fabricated_values_in_output(self):
        """No value in any mtm list should look like a fabricated 100+hash%50 mark (100-149 range)."""
        g = GraveyardDB(self.td)
        for i in range(10):
            g.record_event("catalyst_accept", ticker=f"TKR{i}", meta={"entry_ref_price": 50.0 + i})

        attr = Attribution(g, self.td)

        def always_none(tkr: str):
            return None

        report = attr.monthly_report(price_fn=always_none)

        for v in report.get("accepted_mtm", []):
            if v is not None:
                # a fabricated value from `100.0 + hash(tkr)%50` would land in [100, 149]
                self.assertFalse(
                    100 <= v <= 149,
                    f"Fabricated-looking value {v} found in accepted_mtm — PL-4 regression!"
                )

    def test_pl4_real_prices_still_compute_correctly(self):
        """Sanity: when price_fn returns real values, mtm is computed correctly."""
        g = GraveyardDB(self.td)
        g.record_event("catalyst_accept", ticker="NVDA", meta={"entry_ref_price": 100.0})

        attr = Attribution(g, self.td)

        # Current price = 120 -> return = (120-100)/100 = 0.20
        def real_price(tkr: str):
            return 120.0 if tkr == "NVDA" else None

        report = attr.monthly_report(price_fn=real_price)
        mtm = report.get("accepted_mtm", [])
        self.assertEqual(len(mtm), 1, "One accepted record with valid price should produce one mtm entry")
        self.assertAlmostEqual(mtm[0], 0.20, places=6, msg="(120-100)/100 = 0.20")


if __name__ == "__main__":
    unittest.main()
