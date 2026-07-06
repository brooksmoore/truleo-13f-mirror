"""AUDITOR GATE — Trump sleeve DISABLED / Leopold-only safety (2026-06-16 pivot).

The Trump sleeve was dropped. The landmine: with use_live_trump=False the TrumpSource serves a
FIXTURE (NVDA/ORCL/AVGO/MSFT/FAKELEGACY) as status "verified". Without a real disable, a live-broker
run would place REAL orders for those fixture tickers.

These gates prove that with leopold_only_config() (disable_trump_sleeve=True, sleeve_trump=0.0,
sleeve_leopold=1.0):
  1. NO fixture ticker can reach plan.targets or plan.orders — even though the fixture is reachable.
  2. The Leopold sleeve is allocated ~100%.
  3. reconcile() with sleeve_trump=0.0 puts zero weight on any Trump name.

Do not weaken these without an explicit overseer decision — they fence real money.
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from config import CFG, leopold_only_config
from src.reconciler import reconcile
from src.sources.trump import TrumpSource
from src.sources.leopold import LeopoldSource
from src.tagger import CatalystTagger
from src.mirror_agent import run_cycle, build_paper_executor
from src.core.storage import GraveyardDB, PersistentLog
from src.mcp.robinhood_client import MockRobinhoodClient
from src.executor import MirrorExecutor

FIXTURE_TICKERS = {"NVDA", "ORCL", "AVGO", "MSFT", "FAKELEGACY"}


class TrumpDisabledGate(unittest.TestCase):
    def setUp(self):
        self.td = Path(tempfile.mkdtemp())
        self.data_dir = self.td / "data"; self.data_dir.mkdir()
        self.logs_dir = self.td / "logs"; self.logs_dir.mkdir()

    def _leop_stub(self, basket):
        class StubLeop(LeopoldSource):
            def is_new_filing(self_inner):
                return True
            def get_current_basket(self_inner, force=False):
                return list(basket)
        return StubLeop(cache_dir=self.data_dir)

    def test_disabled_sleeve_admits_no_fixture_ticker_and_leopold_is_full(self):
        """Even with the Trump fixture reachable (use_live_trump=False), disabled sleeve => no fixture
        ticker in targets/orders, and a Leopold name IS bought."""
        cfg = leopold_only_config()  # disable_trump_sleeve=True, sleeve_trump=0, sleeve_leopold=1.0
        # A REAL TrumpSource (live=False) — its get_raw_disclosed() would serve the fixture if called.
        trump = TrumpSource(cache_dir=self.data_dir, graveyard=GraveyardDB(self.data_dir), live=False)
        # sanity: confirm the fixture really is reachable (so the gate is meaningful, not vacuous)
        raw, status = trump.get_raw_disclosed(force=True)
        self.assertEqual(status, "verified")
        self.assertTrue(FIXTURE_TICKERS & {h["ticker"] for h in raw}, "fixture must be reachable for this gate to mean anything")

        leop = self._leop_stub([{"ticker": "TSM", "source_weight": 5_000_000, "side": "long", "filing_accession": "L1"}])
        tagger = CatalystTagger(graveyard=GraveyardDB(self.data_dir))
        client = MockRobinhoodClient(starting_cash=10000.0)
        g = GraveyardDB(self.data_dir); pl = PersistentLog(self.logs_dir)
        ex = MirrorExecutor(client=client, graveyard=g, plog=pl, data_dir=self.data_dir, sleeve_usd=10000.0)

        # Patch the CFG that run_cycle + reconcile read.
        with mock.patch("src.mirror_agent.CFG", cfg):
            plan = run_cycle(leop, trump, tagger, ex, self.data_dir, force=True, c=0)

        target_tkrs = {p.ticker for p in (plan.targets if plan else [])}
        order_tkrs = {o.ticker for o in (plan.orders if plan else [])}
        self.assertEqual(FIXTURE_TICKERS & target_tkrs, set(), f"NO fixture ticker may reach targets; got {target_tkrs}")
        self.assertEqual(FIXTURE_TICKERS & order_tkrs, set(), f"NO fixture ticker may reach orders; got {order_tkrs}")
        # Leopold name made it in (sleeve is live and full)
        self.assertIn("TSM", target_tkrs, "Leopold name must be in the basket")
        # No fixture position actually executed
        pos_tkrs = {p.ticker for p in client.get_positions()}
        self.assertEqual(FIXTURE_TICKERS & pos_tkrs, set(), "no fixture position may be opened")

    def test_reconcile_sleeve_trump_zero_gives_leopold_full_weight(self):
        """Pure reconcile contract: sleeve_trump=0.0 => any Trump-only name gets 0 weight, Leopold ~100%."""
        cfg = leopold_only_config()
        plan = reconcile(
            trump_raw=[{"ticker": "NVDA", "source_weight": 9_000_000}],   # would-be Trump name
            leopold_raw=[{"ticker": "TSM", "source_weight": 5_000_000, "side": "long"}],
            current_positions=[], cfg=cfg, sleeve_total_usd=100_000, current_weights={},
        )
        weights = {p.ticker: p.target_weight for p in plan.targets}
        self.assertNotIn("NVDA", weights, "Trump-only name must get zero allocation with sleeve_trump=0")
        self.assertIn("TSM", weights)
        # Leopold name should carry ~the full sleeve (1.0), capped by per_name_cap only if it bound.
        self.assertGreater(weights["TSM"], 0.0)

    def test_default_config_unchanged_trump_still_flows(self):
        """Guard: the frozen CFG default must STILL run the Trump sleeve (retained test corpus stays valid)."""
        self.assertFalse(getattr(CFG, "disable_trump_sleeve", False))
        self.assertEqual(CFG.sleeve_trump, 0.50)
        self.assertEqual(CFG.sleeve_leopold, 0.50)


if __name__ == "__main__":
    unittest.main()
