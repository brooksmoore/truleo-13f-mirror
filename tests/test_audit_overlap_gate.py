"""AUDITOR-OWNED gate test (independent of Grok's tests) for R5/R6.

Drives the REAL orchestrator (run_cycle) to assert §9 overlap handling survives a
Trump-quiet cycle. These encode the acceptance criteria for R5 and MUST pass after
the fix. They are EXPECTED TO FAIL against the pre-R5 code (the trump_tkrs suppression
freezes overlap names) — that failure is the proof the bug is real.

Do not let Grok edit this file to make it pass by weakening the assertions; the fix
belongs in src/mirror_agent.py (narrow the suppression predicate), not here.
"""

import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.mirror_agent import run_cycle
from src.sources.trump import TrumpSource
from src.sources.leopold import LeopoldSource
from src.tagger import CatalystTagger
from src.executor import MirrorExecutor
from src.core.storage import GraveyardDB


def _shares(ex, ticker):
    return next((p.shares for p in ex.client.get_positions() if p.ticker == ticker), 0.0)


class OverlapGateTests(unittest.TestCase):
    def setUp(self):
        self.td = Path(tempfile.mkdtemp())
        self.dd = self.td / "data"
        self.dd.mkdir()
        self.g = GraveyardDB(self.dd)
        self.ex = MirrorExecutor(graveyard=self.g, data_dir=self.dd, sleeve_usd=10000.0)
        self.tag = CatalystTagger(graveyard=self.g)

    def test_overlap_rebalances_to_leopold_on_trump_no_update(self):
        """R5 GATE: Trump no_update (carries overlap name NVDA); Leopold's new filing
        makes NVDA a large position. The Leopold-driven rebalance of the OVERLAP name
        MUST execute — it must not be suppressed just because NVDA is also a Trump name."""
        # Start NVDA tiny / underweight.
        self.ex.client.place_market_order("NVDA", "buy", 1.0)
        before = _shares(self.ex, "NVDA")

        class StubTrump(TrumpSource):
            def __init__(self, cache_dir=None, **kwargs):
                super().__init__(cache_dir=cache_dir, **kwargs)
            def is_new_filing(self_inner):
                return False  # -> trump_status == no_update, carries last verified

            def get_last_verified_raw(self_inner):
                return [{"ticker": "NVDA", "source_weight": 3_000_000, "filing_id": "tf1"}]

        class StubLeop(LeopoldSource):
            def is_new_filing(self_inner):
                return True

            def get_current_basket(self_inner, force=False):
                return [{"ticker": "NVDA", "source_weight": 5_000_000_000,
                         "side": "long", "filing_accession": "la2"}]

        run_cycle(StubLeop(cache_dir=self.dd), StubTrump(cache_dir=self.dd),
                  self.tag, self.ex, self.dd, force=False, c=1)

        after = _shares(self.ex, "NVDA")
        self.assertGreater(
            abs(after - before), 0.01,
            "Overlap name must rebalance to Leopold's change on a Trump-quiet cycle "
            "(was frozen by the trump_tkrs suppression — R5).",
        )

    def test_pure_trump_only_name_frozen_on_hold(self):
        """Companion: on a HOLD (verify failed), a PURE-Trump-only name (not in Leopold)
        should NOT rebalance — fail-safe freeze. This is the legitimate part of the
        suppression and must remain true after narrowing the predicate."""
        # Seed a Trump-only holding off-target.
        self.ex.client.place_market_order("TRUMPONLY", "buy", 0.5)
        before = _shares(self.ex, "TRUMPONLY")

        class StubTrump(TrumpSource):
            def __init__(self, cache_dir=None, **kwargs):
                super().__init__(cache_dir=cache_dir, **kwargs)
            def is_new_filing(self_inner):
                return True  # aggregator fired...

            def get_raw_disclosed(self_inner, force=False):
                return [], "hold"  # ...but verify failed -> HOLD

            def get_last_verified_raw(self_inner):
                return [{"ticker": "TRUMPONLY", "source_weight": 3_000_000, "filing_id": "tf1"}]

        class StubLeop(LeopoldSource):
            def is_new_filing(self_inner):
                return False

            def get_current_basket(self_inner, force=False):
                return []  # TRUMPONLY is NOT in Leopold

        run_cycle(StubLeop(cache_dir=self.dd), StubTrump(cache_dir=self.dd),
                  self.tag, self.ex, self.dd, force=False, c=1)

        after = _shares(self.ex, "TRUMPONLY")
        self.assertAlmostEqual(
            after, before, places=4,
            msg="Pure-Trump-only name must stay frozen on a HOLD (fail-safe).",
        )


if __name__ == "__main__":
    unittest.main()
