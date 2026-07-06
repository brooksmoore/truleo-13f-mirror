"""R1/R2 integration tests for Trump HOLD / no-update carry-forward (no spurious Trump sells, but Leopold exits still fire).

These drive the *real* code in run_cycle / mirror_agent (not reimplemented filter logic in test).
Uses run_cycle (extracted for testability) with stubbed sources that control is_new_filing, get_*, and cache state.
"""

from __future__ import annotations

import unittest
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from config import CFG
from src.sources.trump import TrumpSource
from src.sources.leopold import LeopoldSource
from src.tagger import CatalystTagger
from src.mirror_agent import run_cycle, build_paper_executor
from src.core.storage import GraveyardDB
from src.mcp.robinhood_client import MockRobinhoodClient
from src.executor import MirrorExecutor
from src.core.storage import PersistentLog
from src.core.schemas import OrderIntent


class TrumpHoldIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.td = Path(tempfile.mkdtemp())
        self.data_dir = self.td / "data"
        self.data_dir.mkdir()
        self.logs_dir = self.td / "logs"
        self.logs_dir.mkdir()

    def _make_patched_trump(self, *, is_new: bool = False, status: str = "verified", raw: list[dict] | None = None, last_verified: list[dict] | None = None, **extra):
        """Patched TrumpSource for controlled scenarios (hold, no_update, carry)."""
        class PatchedTrump(TrumpSource):
            def __init__(self, cache_dir, **kwargs):
                super().__init__(cache_dir=cache_dir, **kwargs)
                # seed last_verified if provided (simulates previous successful populate)
                if last_verified is not None:
                    self._cache["raw"] = list(last_verified)
                    self._cache["last_filing_id"] = "prev-fid"
                    self._save_cache()
            def is_new_filing(self):
                return is_new
            def get_raw_disclosed(self, force: bool = False):
                if status == "hold":
                    return [], "hold"
                r = raw if raw is not None else self._cache.get("raw", [])
                return list(r), "verified" if status == "verified" else status
            def get_last_verified_raw(self):
                return list(self._cache.get("raw", []))
        return PatchedTrump(cache_dir=self.data_dir, **extra)

    def _make_patched_leop(self, *, is_new: bool = True, basket: list[dict] | None = None):
        class PatchedLeop(LeopoldSource):
            def __init__(self, cache_dir):
                super().__init__(cache_dir=cache_dir)
            def is_new_filing(self):
                return is_new
            def get_current_basket(self, force: bool = False):
                return list(basket) if basket is not None else super().get_current_basket(force=force)
        return PatchedLeop(cache_dir=self.data_dir)

    def test_r1_hold_zero_trump_orders_trump_position_untouched(self):
        """R1 #1: Trump verify-fail (hold) with existing Trump pos (from prior) → zero Trump orders in plan; position untouched (still in ex after)."""
        # Seed a prior verified Trump to have cache + will exec to have pos
        initial_trump = [{"ticker": "NVDA", "source_weight": 1000000, "filing_id": "prev"}]
        trump = self._make_patched_trump(is_new=True, status="verified", raw=initial_trump)
        leop = self._make_patched_leop(is_new=False, basket=[])
        tagger = CatalystTagger(graveyard=GraveyardDB(self.data_dir))
        # fresh ex
        client = MockRobinhoodClient(starting_cash=10000.0)
        g = GraveyardDB(self.data_dir)
        pl = PersistentLog(self.logs_dir)
        ex = MirrorExecutor(client=client, graveyard=g, plog=pl, data_dir=self.data_dir, sleeve_usd=10000.0)

        # First cycle: populate the Trump position (verified buy)
        plan1 = run_cycle(leop, trump, tagger, ex, self.data_dir, force=True, c=0)
        self.assertTrue(any(o.ticker == "NVDA" for o in (plan1.orders if plan1 else [])))
        # after exec, position should exist
        poss = {p.ticker for p in client.get_positions()}
        self.assertIn("NVDA", poss)

        # Now simulate hold on next "new" trigger: is_new=True but get returns hold; use cached
        trump2 = self._make_patched_trump(is_new=True, status="hold", last_verified=initial_trump)
        leop2 = self._make_patched_leop(is_new=False, basket=[])  # no leop change
        plan2 = run_cycle(leop2, trump2, tagger, ex, self.data_dir, force=False, c=1)

        # No Trump orders (carry, no sell)
        trump_orders = [o for o in (plan2.orders if plan2 else []) if o.ticker == "NVDA"]
        self.assertEqual(len(trump_orders), 0, "HOLD must not produce Trump orders (no liquidation)")
        # Position still there (untouched)
        poss_after = {p.ticker for p in client.get_positions()}
        self.assertIn("NVDA", poss_after)

        # Hold was logged
        events = g.get_events(limit=10)
        self.assertTrue(any(e.get("action") == "trump_hold" for e in events))

    def test_r1_no_update_leopold_exit_still_fires(self):
        """R1 #2 (the regression catcher): Trump no_update + Leopold new filing drops a held Leop name → the Leopold source_exit STILL fires in the *real* plan/orders."""
        # Seed Trump carry + a Leop name that will be dropped
        initial_trump = [{"ticker": "AVGO", "source_weight": 500000, "filing_id": "t1"}]
        initial_leop = [{"ticker": "OLDLEOP", "source_weight": 2000000, "side": "long", "filing_accession": "l1"}]

        trump = self._make_patched_trump(is_new=False, status="no_update", last_verified=initial_trump)
        leop = self._make_patched_leop(is_new=True, basket=[])  # "new" filing drops OLDLEOP (empty for this test)
        tagger = CatalystTagger(graveyard=GraveyardDB(self.data_dir))
        client = MockRobinhoodClient(starting_cash=10000.0)
        g = GraveyardDB(self.data_dir)
        pl = PersistentLog(self.logs_dir)
        ex = MirrorExecutor(client=client, graveyard=g, plog=pl, data_dir=self.data_dir, sleeve_usd=10000.0)

        # Pre-populate a position for OLDLEOP (as if held)
        # (run a prior verified leop would, but for simplicity seed via direct order on a low price name; use "TESTLEOP" for mock default price)
        # To make realistic, use execute to buy the name first (simulating prior leop)
        ex.execute_plan([OrderIntent("OLDLEOP", "long", 5.0, "seed", 0.2, 0.0)], [], "seed-leop")
        self.assertIn("OLDLEOP", {p.ticker for p in client.get_positions()})

        # The critical cycle: trump no_update (carry), leop "drops" (basket without it)
        # Note: leop basket=[] means the dropped name triggers exit in recon
        plan = run_cycle(leop, trump, tagger, ex, self.data_dir, force=False, c=0)

        # Assert: leop exit for OLDLEOP *did* fire (was not suppressed by any Trump-hold logic)
        leop_exits = [o for o in (plan.orders if plan else []) if o.ticker == "OLDLEOP" and o.reason == "source_exit"]
        self.assertEqual(len(leop_exits), 1, "Leopold source_exit must fire on genuine leop drop even during Trump no_update/hold")

        # Trump carry name should not have unwanted exit
        trump_exits = [o for o in (plan.orders if plan else []) if o.ticker == "AVGO" and o.reason == "source_exit"]
        self.assertEqual(len(trump_exits), 0)

    def test_r1_genuine_verified_trump_drop_emits_sell(self):
        """R1 #3: Genuine verified Trump filing that drops a name → its source_exit fires (no suppression)."""
        initial = [{"ticker": "ORCL", "source_weight": 1000000, "filing_id": "old"}]
        dropped_raw = []  # verified now empty for Trump
        trump = self._make_patched_trump(is_new=True, status="verified", raw=dropped_raw)
        leop = self._make_patched_leop(is_new=False, basket=[])
        tagger = CatalystTagger(graveyard=GraveyardDB(self.data_dir))
        client = MockRobinhoodClient(starting_cash=10000.0)
        g = GraveyardDB(self.data_dir)
        pl = PersistentLog(self.logs_dir)
        ex = MirrorExecutor(client=client, graveyard=g, plog=pl, data_dir=self.data_dir, sleeve_usd=10000.0)

        # seed position for DROPPED
        ex.execute_plan([OrderIntent("ORCL", "long", 2.0, "seed", 0.1, 0.0)], [], "seed")
        self.assertIn("ORCL", {p.ticker for p in client.get_positions()})

        plan = run_cycle(leop, trump, tagger, ex, self.data_dir, force=False, c=0)

        exits = [o for o in (plan.orders if plan else []) if o.ticker == "ORCL" and o.reason == "source_exit"]
        self.assertEqual(len(exits), 1, "Genuine verified Trump drop must emit source_exit (not suppressed)")

    def test_r5_r6_overlap_rebalances_on_no_update_pure_trump_frozen_on_hold(self):
        """R6: drives real run_cycle.
        1. Trump no_update + Leopold changes an OVERLAP name → overlap rebalance order fires (position changes).
        2. On hold, pure-Trump-only frozen (no rebal), but an overlap name still tracks Leopold.
        (Complements the auditor gate without editing it.)
        """
        # --- overlap on no_update ---
        initial_trump = [{"ticker": "AMD", "source_weight": 1000000, "filing_id": "t1"}]
        trump = self._make_patched_trump(is_new=False, status="no_update", last_verified=initial_trump)
        leop = self._make_patched_leop(is_new=True, basket=[{"ticker": "AMD", "source_weight": 10000000, "side": "long", "filing_accession": "l2"}])  # leop wants much larger
        tagger = CatalystTagger(graveyard=GraveyardDB(self.data_dir))
        client = MockRobinhoodClient(starting_cash=10000.0)
        g = GraveyardDB(self.data_dir)
        pl = PersistentLog(self.logs_dir)
        ex = MirrorExecutor(client=client, graveyard=g, plog=pl, data_dir=self.data_dir, sleeve_usd=10000.0)

        # seed small position for overlap
        ex.execute_plan([OrderIntent("AMD", "long", 1.0, "seed", 0.1, 0.0)], [], "seed-overlap")
        before = next((p.shares for p in client.get_positions() if p.ticker == "AMD"), 0.0)

        plan = run_cycle(leop, trump, tagger, ex, self.data_dir, force=False, c=0)

        after = next((p.shares for p in client.get_positions() if p.ticker == "AMD"), 0.0)
        overlap_orders = [o for o in (plan.orders if plan else []) if o.ticker == "AMD"]
        self.assertTrue(len(overlap_orders) > 0 or abs(after - before) > 0.01,
                        "Overlap must rebalance on Trump no_update (Leopold change must not be suppressed)")

        # --- on hold: pure-trump frozen, but overlap would still track if leop changed (but for this subtest, check pure frozen) ---
        # For simplicity, reuse a hold scenario for pure
        trump_hold = self._make_patched_trump(is_new=True, status="hold", last_verified=[{"ticker": "KLAC", "source_weight": 500000, "filing_id": "th"}])
        leop_hold = self._make_patched_leop(is_new=False, basket=[])
        ex2 = MirrorExecutor(client=MockRobinhoodClient(starting_cash=10000.0), graveyard=GraveyardDB(self.data_dir), plog=PersistentLog(self.logs_dir), data_dir=self.data_dir, sleeve_usd=10000.0)
        ex2.client.place_market_order("KLAC", "buy", 0.5)
        before_pure = next((p.shares for p in ex2.client.get_positions() if p.ticker == "KLAC"), 0.0)
        plan_hold = run_cycle(leop_hold, trump_hold, tagger, ex2, self.data_dir, force=False, c=1)
        after_pure = next((p.shares for p in ex2.client.get_positions() if p.ticker == "KLAC"), 0.0)
        self.assertAlmostEqual(after_pure, before_pure, places=4, msg="Pure-Trump-only must be frozen on hold")

    def test_live_trump_aggregator_fail_hold_no_fixture_no_orders(self):
        """Live aggregator fail (Part A): when live=True and _aggregator_fetcher returns None, get_raw returns hold, no fixture served, carry last or empty, no Trump orders/sells, unavailable logged."""
        g = GraveyardDB(self.data_dir)
        # create real TrumpSource with live and fail fetcher (injected seam at ctor), seed cache manually for carry
        trump = TrumpSource(cache_dir=self.data_dir, graveyard=g, live=True, aggregator_fetcher=lambda: None)
        # seed last verified for carry
        trump._cache["raw"] = [{"ticker": "NVDA", "source_weight": 1000000, "filing_id": "prev"}]
        trump._cache["last_filing_id"] = "prev"
        trump._save_cache()
        leop = self._make_patched_leop(is_new=False, basket=[])
        tagger = CatalystTagger(graveyard=GraveyardDB(self.data_dir))
        client = MockRobinhoodClient(starting_cash=10000.0)
        pl = PersistentLog(self.logs_dir)
        ex = MirrorExecutor(client=client, graveyard=g, plog=pl, data_dir=self.data_dir, sleeve_usd=10000.0)
        ex.execute_plan([OrderIntent("NVDA", "long", 2.0, "seed", 0.1, 0.0)], [], "seed")
        self.assertIn("NVDA", {p.ticker for p in client.get_positions()})

        plan = run_cycle(leop, trump, tagger, ex, self.data_dir, force=True, c=0)
        events = GraveyardDB(self.data_dir).get_events(limit=10)
        self.assertTrue(any(e.get("action") in ("trump_feed_unavailable", "trump_hold") for e in events))
        trump_exits = [o for o in (plan.orders if plan else []) if o.ticker == "NVDA" and o.reason == "source_exit"]
        self.assertEqual(len(trump_exits), 0)
        self.assertIn("NVDA", {p.ticker for p in client.get_positions()})

    def test_live_aggregator_fetch_failure_returns_hold_logs_unavailable_no_fixture(self):
        """Part A: live aggregator fail via seam -> ([], "hold"), trump_feed_unavailable logged, fixture NOT served (no fail-open)."""
        g = GraveyardDB(self.data_dir)
        def bad_agg(): return None
        src = TrumpSource(cache_dir=self.data_dir, graveyard=g, live=True, aggregator_fetcher=bad_agg)
        raw, status = src.get_raw_disclosed(force=True)
        self.assertEqual(raw, [])
        self.assertEqual(status, "hold")
        events = g.get_events(limit=20)
        self.assertTrue(any(e.get("action") == "trump_feed_unavailable" for e in events))
        served = {h.get("ticker") for h in raw}
        self.assertNotIn("NVDA", served)
        self.assertNotIn("FAKELEGACY", served)

    def test_live_official_unverifiable_yields_hold(self):
        """Part A: live official filing unverifiable (e.g. fetch/extract fails via seam) -> hold, no trade."""
        g = GraveyardDB(self.data_dir)
        def good_agg(): return {"filing_id": "f-uv", "date": "d", "claimed_holdings": [{"ticker": "NVDA", "range": "$1M-$5M"}]}
        def bad_off(fid): return None
        src = TrumpSource(cache_dir=self.data_dir, graveyard=g, live=True, aggregator_fetcher=good_agg, official_fetcher=bad_off)
        raw, status = src.get_raw_disclosed(force=True)
        self.assertEqual(status, "hold")
        self.assertEqual(raw, [])
        evs = [e for e in g.get_events() if e.get("action") == "trump_feed_unavailable"]
        self.assertTrue(len(evs) > 0)

    def test_live_aggregator_official_mismatch_yields_hold(self):
        """Part A: aggregator vs official mismatch (seeded via seams) -> hold, no trade (verify_before_execute contract)."""
        g = GraveyardDB(self.data_dir)
        def agg(): return {"filing_id": "f-mis", "claimed_holdings": [{"ticker": "NVDA", "range": "$1M-$5M"}]}
        def off(fid): return {"filing_id": fid, "holdings": [{"ticker": "ORCL", "range": "$1M-$5M"}]}
        src = TrumpSource(cache_dir=self.data_dir, graveyard=g, live=True, aggregator_fetcher=agg, official_fetcher=off)
        raw, status = src.get_raw_disclosed(force=True)
        self.assertEqual(status, "hold")
        self.assertEqual(raw, [])

    def test_live_clean_match_via_injected_fetchers_yields_verified_with_midpoints(self):
        """Part A: clean aggregator==official match via injected seams -> "verified" + parsed with midpoints (real _range_to_mid path)."""
        g = GraveyardDB(self.data_dir)
        def agg(): return {"filing_id": "f-clean", "date": "2026-06", "claimed_holdings": [{"ticker": "NVDA", "range": "$1M-$5M"}, {"ticker": "ORCL", "range": "over $50M"}]}
        def off(fid): return {"filing_id": fid, "holdings": [{"ticker": "NVDA", "range": "$1M-$5M"}, {"ticker": "ORCL", "range": "over $50M"}]}
        src = TrumpSource(cache_dir=self.data_dir, graveyard=g, live=True, aggregator_fetcher=agg, official_fetcher=off)
        raw, status = src.get_raw_disclosed(force=True)
        self.assertEqual(status, "verified")
        self.assertEqual(len(raw), 2)
        tkrs = {h["ticker"] for h in raw}
        self.assertIn("NVDA", tkrs)
        self.assertIn("ORCL", tkrs)
        for h in raw:
            self.assertIn("source_weight", h)
            self.assertGreater(h["source_weight"], 0)
            self.assertIn("range", h)
            self.assertIn("filing_id", h)

    def test_live_verified_empty_is_verified_not_hold(self):
        """Part A: genuine verified-empty (reduced filing) must return "verified" + [] (so downstream source_exit can fire); not hold."""
        g = GraveyardDB(self.data_dir)
        def agg(): return {"filing_id": "f-empty", "claimed_holdings": []}
        def off(fid): return {"filing_id": fid, "holdings": []}
        src = TrumpSource(cache_dir=self.data_dir, graveyard=g, live=True, aggregator_fetcher=agg, official_fetcher=off)
        raw, status = src.get_raw_disclosed(force=True)
        self.assertEqual(status, "verified")
        self.assertEqual(raw, [])

    def test_flag_off_ignores_fetcher_serves_fixture_byte_identical(self):
        """Flag-off must serve the exact fixture (byte-identical behavior) and ignore any live seams/fetchers (keeps 46+ tests + gates green unmodified)."""
        g = GraveyardDB(self.data_dir)
        def bad_agg(): return None
        src = TrumpSource(cache_dir=self.data_dir, graveyard=g, live=False, aggregator_fetcher=bad_agg)
        raw, status = src.get_raw_disclosed(force=True)
        self.assertEqual(status, "verified")
        tkrs = {h["ticker"] for h in raw}
        self.assertIn("NVDA", tkrs)
        self.assertIn("FAKELEGACY", tkrs)  # fixture marker

    def test_catalyst_filter_excludes_rejected_on_verified_and_no_update_carry(self):
        """Drives real run_cycle: on verified, rejected disclosed name excluded from basket.
        On subsequent no_update (carried), the carried set is accepted-only (update on verified ensures),
        so rejected never appears on carried cycle either.
        """
        disclosed_mixed = [
            {"ticker": "NVDA", "source_weight": 3_000_000, "filing_id": "f1"},
            {"ticker": "JUNKREJ", "source_weight": 2_000_000, "filing_id": "f1"},
        ]
        class StubTrump(TrumpSource):
            def __init__(self_inner, cache_dir=None, **kw):
                super().__init__(cache_dir=cache_dir, **kw)
            def is_new_filing(self_inner):
                return True
            def get_raw_disclosed(self_inner, force=False):
                return list(disclosed_mixed), "verified"
            def get_last_verified_raw(self_inner):
                # after first, orchestrator updated cache to acc-only
                return list(self_inner._cache.get("raw", disclosed_mixed))
        class StubLeop(LeopoldSource):
            def is_new_filing(self_inner):
                return False
            def get_current_basket(self_inner, force=False):
                return []
        tagger = CatalystTagger(graveyard=GraveyardDB(self.data_dir))
        client = MockRobinhoodClient(starting_cash=10000.0)
        g = GraveyardDB(self.data_dir)
        pl = PersistentLog(self.logs_dir)
        ex = MirrorExecutor(client=client, graveyard=g, plog=pl, data_dir=self.data_dir, sleeve_usd=10000.0)
        # verified cycle with mixed disclosed
        plan1 = run_cycle(StubLeop(cache_dir=self.data_dir), StubTrump(cache_dir=self.data_dir),
                          tagger, ex, self.data_dir, force=True, c=0)
        poss1 = {p.ticker for p in client.get_positions()}
        self.assertIn("NVDA", poss1)
        self.assertNotIn("JUNKREJ", poss1, "rejected must be excluded from basket on verified cycle")
        # now no_update cycle (is_new=false, carry the last which was updated to acc-only)
        class StubTrumpNoUpdate(StubTrump):
            def is_new_filing(self_inner):
                return False
        plan2 = run_cycle(StubLeop(cache_dir=self.data_dir), StubTrumpNoUpdate(cache_dir=self.data_dir),
                          tagger, ex, self.data_dir, force=False, c=1)
        poss2 = {p.ticker for p in client.get_positions()}
        self.assertNotIn("JUNKREJ", poss2, "rejected must be excluded from basket on carried no_update cycle (carry is acc-only)")
        if plan2:
            targs = {p.ticker for p in (plan2.targets or [])}
            self.assertNotIn("JUNKREJ", targs)


if __name__ == "__main__":
    unittest.main()
