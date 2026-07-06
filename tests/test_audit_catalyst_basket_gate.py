"""AUDITOR-OWNED gate test (independent) for CRIT-FILTER — the catalyst filter must
actually GATE basket construction, not just attribution.

Spec §1: "the catalyst filter is the heart of this [Trump] sleeve." A catalyst-REJECTED
disclosed name must NOT receive a target weight or be traded. Drives the REAL run_cycle.

REGRESSION (found Pass #8 audit): run_cycle feeds the full raw disclosure (trump_raw) to
reconcile instead of the accepted set (trump_acc), so rejected/legacy names enter the live
basket. EXPECTED TO FAIL until run_cycle feeds only catalyst-accepted (+approved) names.

Do not edit to pass. Fix belongs in src/mirror_agent.py (feed accepted, not raw) and the
Trump carry-forward (carry the accepted basket, not unfiltered raw).
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


class CatalystBasketGate(unittest.TestCase):
    def setUp(self):
        self.dd = Path(tempfile.mkdtemp()) / "data"
        self.dd.mkdir(parents=True)
        self.g = GraveyardDB(self.dd)
        self.ex = MirrorExecutor(graveyard=self.g, data_dir=self.dd, sleeve_usd=10000.0)
        self.tag = CatalystTagger(graveyard=self.g)  # mock tagger (flag off)

    def test_rejected_disclosed_name_not_in_basket(self):
        """NVDA (mock-accepted) vs JUNKLEGACY (mock-rejected, catalyst=False).
        Only NVDA may be traded; JUNKLEGACY must never enter the basket."""
        disclosed = [
            {"ticker": "NVDA", "source_weight": 3_000_000, "filing_id": "f1"},
            {"ticker": "JUNKLEGACY", "source_weight": 3_000_000, "filing_id": "f1"},
        ]

        class StubTrump(TrumpSource):
            def __init__(self_inner, cache_dir=None, **kw):
                super().__init__(cache_dir=cache_dir, **kw)
            def is_new_filing(self_inner):
                return True
            def get_raw_disclosed(self_inner, force=False):
                return list(disclosed), "verified"
            def get_last_verified_raw(self_inner):
                return list(disclosed)

        class StubLeop(LeopoldSource):
            def is_new_filing(self_inner):
                return False
            def get_current_basket(self_inner, force=False):
                return []

        run_cycle(StubLeop(cache_dir=self.dd), StubTrump(cache_dir=self.dd),
                  self.tag, self.ex, self.dd, force=True, c=0)

        held = {p.ticker for p in self.ex.client.get_positions()}
        self.assertIn("NVDA", held, "catalyst-accepted name should be in the basket")
        self.assertNotIn("JUNKLEGACY", held,
                         "catalyst-REJECTED name must NOT be traded — the filter must gate "
                         "the basket, not just attribution (spec §1).")


if __name__ == "__main__":
    unittest.main()
