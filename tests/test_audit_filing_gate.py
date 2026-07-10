"""AUDITOR GATE — quiet-cycle no-op (trade only on genuine new 13F filings).

Root-cause finding (2026-07-09 session): live_broker/run_live.py called
    run_cycle(..., force=(c == 0), c=c, cfg=cfg)
Every cron/launchd invocation is a FRESH process — args.cycles defaults to 1, so c is always 0,
so force=(c==0) was True on EVERY daily run, regardless of whether Leopold actually filed a new
13F. That defeated the "no new filings -> no action" quiet-cycle gate that already exists in
mirror_agent.run_cycle (leop_new = leop.is_new_filing() or force), and caused the bot to fully
reconcile+trade every day off pure price drift against a target vector that is up to 45+ days
stale — an owner-identified architecture problem (see conversation), not the intended design.

Fix: run_live.py now passes force=False unconditionally. A genuinely first-ever run still
populates correctly because is_new_filing() compares the live accession against an empty cache
("" != real accession -> True) without needing the force flag at all.

These gates prove, at the orchestrator layer (real run_cycle, not reimplemented logic):
  1. FAIL-BEFORE: reproduce the historical bug — force=True on two consecutive same-filing days
     (what run_live.py used to effectively do) trades BOTH days on pure drift.
  2. FAIL-AFTER: with the fix's calling convention (force=False, relying on the on-disk cache),
     a same-filing day produces ZERO orders / no execute_plan call — the "hold between filings"
     architecture the owner explicitly chose.
  3. A genuine NEW filing (even with force=False) still triggers a full reconcile+trade — this
     is not "never trade again", only "don't trade on stale-target price noise".
  4. A regression guard on run_live.py's source: the old `force=(c == 0)` pattern must not
     reappear, and `force=False` must be present in the cycle loop.
"""
from __future__ import annotations

import unittest
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from config import leopold_only_config
from src.sources.trump import TrumpSource
from src.sources.leopold import LeopoldSource
from src.tagger import CatalystTagger
from src.mirror_agent import run_cycle
from src.core.storage import GraveyardDB, PersistentLog
from src.mcp.robinhood_client import MockRobinhoodClient
from src.executor import MirrorExecutor

SNDK = {"ticker": "SNDK", "source_weight": 22_000_000, "side": "long", "filing_accession": "acc-1"}
BE = {"ticker": "BE", "source_weight": 19_000_000, "side": "long", "filing_accession": "acc-1"}
SAME_FILING_BASKET = [SNDK, BE]

# A genuinely different filing (new accession, different weights) to prove trading still works
# when Leopold actually discloses something new.
NEW_FILING_BASKET = [
    {"ticker": "SNDK", "source_weight": 10_000_000, "side": "long", "filing_accession": "acc-2"},
    {"ticker": "CRWV", "source_weight": 25_000_000, "side": "long", "filing_accession": "acc-2"},
]


class _StubTrumpNoOp(TrumpSource):
    """Trump sleeve is disabled in leopold_only_config, but the top-level
    `leop_new or trump_new` gate still evaluates trump.is_new_filing() — stub it False
    so the test isolates Leopold's filing-gate behavior."""
    def __init__(self, cache_dir):
        super().__init__(cache_dir=cache_dir, live=False)

    def is_new_filing(self):
        return False


class _StubLeop(LeopoldSource):
    def __init__(self, cache_dir, *, is_new: bool, basket: list[dict]):
        super().__init__(cache_dir=cache_dir, live=False)
        self._is_new = is_new
        self._basket = basket

    def is_new_filing(self):
        return self._is_new

    def get_current_basket(self, force: bool = False):
        return list(self._basket)


class FilingGateTests(unittest.TestCase):
    def setUp(self):
        self.td = Path(tempfile.mkdtemp())
        self.data_dir = self.td / "data"; self.data_dir.mkdir()
        self.logs_dir = self.td / "logs"; self.logs_dir.mkdir()
        self.cfg = leopold_only_config()

    def _fresh_ex(self):
        client = MockRobinhoodClient(starting_cash=100.0)
        g = GraveyardDB(self.data_dir)
        pl = PersistentLog(self.logs_dir)
        ex = MirrorExecutor(client=client, graveyard=g, plog=pl, data_dir=self.data_dir, sleeve_usd=100.0)
        return ex, g, client

    def test_fail_before_force_true_every_day_trades_on_pure_drift(self):
        """Reproduces the historical bug: force=True (what run_live.py effectively did every day)
        trades on day 1 AND day 2 even though the filing never changed between them."""
        ex, g, client = self._fresh_ex()
        tagger = CatalystTagger(graveyard=g)

        trump1 = _StubTrumpNoOp(cache_dir=self.data_dir)
        leop1 = _StubLeop(cache_dir=self.data_dir, is_new=True, basket=SAME_FILING_BASKET)
        plan1 = run_cycle(leop1, trump1, tagger, ex, self.data_dir, force=True, c=0, cfg=self.cfg)
        self.assertIsNotNone(plan1)
        self.assertTrue(len(plan1.orders) > 0, "day 1 should trade (populating the book)")

        # Day 2: SAME filing (is_new=False, per a real accession-unchanged check), but the bug
        # forced force=True unconditionally every day -> reconcile+trade fires anyway on drift.
        trump2 = _StubTrumpNoOp(cache_dir=self.data_dir)
        leop2 = _StubLeop(cache_dir=self.data_dir, is_new=False, basket=SAME_FILING_BASKET)
        plan2 = run_cycle(leop2, trump2, tagger, ex, self.data_dir, force=True, c=0, cfg=self.cfg)
        self.assertIsNotNone(plan2, "BUG REPRODUCED: force=True bypasses the quiet-cycle gate "
                                     "even on an unchanged filing")

    def test_fail_after_force_false_same_filing_is_a_noop(self):
        """The fix: force=False (what run_live.py now passes) on a same-filing day returns early
        — no reconcile, no orders, no execute_plan call, no new graveyard fill events."""
        ex, g, client = self._fresh_ex()
        tagger = CatalystTagger(graveyard=g)

        # Day 1: genuinely new filing (accession changes from "" -> acc-1); populates the book.
        trump1 = _StubTrumpNoOp(cache_dir=self.data_dir)
        leop1 = _StubLeop(cache_dir=self.data_dir, is_new=True, basket=SAME_FILING_BASKET)
        plan1 = run_cycle(leop1, trump1, tagger, ex, self.data_dir, force=False, c=0, cfg=self.cfg)
        self.assertIsNotNone(plan1)
        self.assertTrue(len(plan1.orders) > 0)
        events_after_day1 = len(g.get_events(limit=None))

        # Day 2: SAME filing, is_new=False, force=False (the fixed calling convention) — must no-op.
        trump2 = _StubTrumpNoOp(cache_dir=self.data_dir)
        leop2 = _StubLeop(cache_dir=self.data_dir, is_new=False, basket=SAME_FILING_BASKET)
        plan2 = run_cycle(leop2, trump2, tagger, ex, self.data_dir, force=False, c=1, cfg=self.cfg)
        self.assertIsNone(plan2, "quiet cycle (no new filing, force=False) must return early with no plan")

        # No new graveyard events from a second reconcile/execute pass (proves execute_plan was never called).
        events_after_day2 = len(g.get_events(limit=None))
        self.assertEqual(events_after_day1, events_after_day2,
            "a quiet cycle must not touch the graveyard at all (no reconcile, no execute_plan)")

    def test_genuine_new_filing_still_trades_even_with_force_false(self):
        """Not 'never trade again': a real new 13F filing (accession changes) still triggers a
        full reconcile+trade, proving the fix only suppresses NOISE trading on a stale target,
        not legitimate rebalancing on genuine new information."""
        ex, g, client = self._fresh_ex()
        tagger = CatalystTagger(graveyard=g)

        trump1 = _StubTrumpNoOp(cache_dir=self.data_dir)
        leop1 = _StubLeop(cache_dir=self.data_dir, is_new=True, basket=SAME_FILING_BASKET)
        run_cycle(leop1, trump1, tagger, ex, self.data_dir, force=False, c=0, cfg=self.cfg)

        # Day 2: genuinely new filing (different accession, different weights), force=False.
        trump2 = _StubTrumpNoOp(cache_dir=self.data_dir)
        leop2 = _StubLeop(cache_dir=self.data_dir, is_new=True, basket=NEW_FILING_BASKET)
        plan2 = run_cycle(leop2, trump2, tagger, ex, self.data_dir, force=False, c=1, cfg=self.cfg)
        self.assertIsNotNone(plan2, "a genuine new filing must still trigger a reconcile even with force=False")
        self.assertTrue(len(plan2.orders) > 0 or len(plan2.targets) > 0,
            "a genuine new filing must produce targets/orders reflecting the new basket")


class RunLiveSourceRegressionGuard(unittest.TestCase):
    """Static guard: the old force=(c==0) pattern must never reappear in run_live.py."""

    def test_run_live_does_not_force_every_cycle(self):
        run_live_path = Path(__file__).resolve().parents[1] / "live_broker" / "run_live.py"
        src = run_live_path.read_text()
        self.assertNotIn("force=(c == 0)", src,
            "run_live.py must not reintroduce force=(c==0) — every cron invocation is a fresh "
            "process with c always 0, which forces a full reconcile+trade EVERY day regardless "
            "of whether Leopold actually filed a new 13F (see test_fail_before_... above)")
        self.assertIn("force=False", src,
            "run_live.py must pass force=False and rely on the on-disk accession cache "
            "(is_new_filing()) to detect genuinely new filings")


if __name__ == "__main__":
    unittest.main()
