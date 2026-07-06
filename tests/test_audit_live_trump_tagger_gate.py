"""AUDITOR-OWNED gate tests (independent) for the live Trump source (Pass #7) and live
Haiku tagger (Pass #8). Drive the REAL classes with injected seams — no network, no API.

Safety properties locked in:
  A1: live aggregator failure -> ([], "hold"), no fixture served, outage logged.
  A2: aggregator vs official mismatch -> "hold", no holdings (flag-and-hold, §8).
  A3: clean injected match -> "verified" + parsed holdings.
  B1: live LLM failure -> catalyst=False (fail-safe), NOT the mock answer; error logged.
  B2: live new catalyst name is held in the approval queue (not in basket) until approve().

Do not let these be edited to pass. Fixes belong in src/sources/trump.py / src/tagger.py.
"""

import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.sources.trump import TrumpSource
from src.tagger import CatalystTagger
from src.core.storage import GraveyardDB

FIXTURE_TICKERS = {"NVDA", "ORCL", "AVGO", "MSFT", "FAKELEGACY"}


class LiveTrumpGate(unittest.TestCase):
    def setUp(self):
        self.dd = Path(tempfile.mkdtemp()) / "data"
        self.dd.mkdir(parents=True)
        self.g = GraveyardDB(self.dd)

    def test_a1_live_aggregator_failure_holds_no_fixture(self):
        def boom():
            raise RuntimeError("aggregator down (403)")
        src = TrumpSource(cache_dir=self.dd, graveyard=self.g, live=True, aggregator_fetcher=boom)
        raw, status = src.get_raw_disclosed(force=True)
        self.assertEqual(status, "hold")
        self.assertEqual(raw, [])
        served = {h.get("ticker") for h in raw}
        self.assertFalse(served & FIXTURE_TICKERS, "live failure must NOT serve fixture holdings")
        self.assertTrue(any(e.get("action") == "trump_feed_unavailable" for e in self.g.get_events(limit=20)),
                        "live outage must be logged")

    def test_a2_aggregator_filing_mismatch_holds(self):
        agg = lambda: {"filing_id": "F1", "date": "2026-05-01",
                       "claimed_holdings": [{"ticker": "AAA", "range": "$1M-$5M"}]}
        official = lambda fid: {"filing_id": fid, "holdings": [{"ticker": "BBB", "range": "$1M-$5M"}]}
        src = TrumpSource(cache_dir=self.dd, graveyard=self.g, live=True,
                          aggregator_fetcher=agg, official_fetcher=official)
        raw, status = src.get_raw_disclosed(force=True)
        self.assertEqual(status, "hold")
        self.assertEqual(raw, [])

    def test_a3_clean_match_verified(self):
        holdings = [{"ticker": "NVDA", "range": "$1M-$5M"}]
        agg = lambda: {"filing_id": "F2", "date": "2026-05-01", "claimed_holdings": holdings}
        official = lambda fid: {"filing_id": fid, "holdings": holdings}
        src = TrumpSource(cache_dir=self.dd, graveyard=self.g, live=True,
                          aggregator_fetcher=agg, official_fetcher=official)
        raw, status = src.get_raw_disclosed(force=True)
        self.assertEqual(status, "verified")
        self.assertEqual([h["ticker"] for h in raw], ["NVDA"])
        self.assertGreater(raw[0]["source_weight"], 0)


class LiveTaggerGate(unittest.TestCase):
    def setUp(self):
        self.dd = Path(tempfile.mkdtemp()) / "data"
        self.dd.mkdir(parents=True)
        self.g = GraveyardDB(self.dd)

    def test_b1_live_llm_failure_failsafe_not_mock(self):
        """NVDA would be accepted by the mock. On a live LLM failure the tagger must NOT
        fall back to the mock answer — it must fail safe (catalyst=False) and log."""
        def boom(prompt):
            raise RuntimeError("LLM 500")
        tagger = CatalystTagger(graveyard=self.g, live=True, llm_client=boom)
        tag = tagger.tag("NVDA")
        self.assertFalse(tag.catalyst, "live LLM failure must fail safe, not use mock accept")
        self.assertTrue(any(e.get("action") == "tagger_live_error" for e in self.g.get_events(limit=20)))

    def test_b2_new_catalyst_held_in_approval_queue_until_approved(self):
        """A live catalyst=True name must NOT enter the accepted basket until approved."""
        good_json = '{"catalyst": true, "type": "touted", "reason": "x", "source_url": "u", "confidence": 0.9}'
        tagger = CatalystTagger(graveyard=self.g, live=True, llm_client=lambda p: good_json)
        raw = [{"ticker": "NEWNAME"}]
        res = tagger.filter_trump_holdings(raw)
        if len(res) == 3:
            acc, rej, pend = res
        else:
            acc, rej = res
            pend = []
        self.assertNotIn("NEWNAME", [a["ticker"] for a in acc],
                         "unapproved catalyst name must be held out of the basket")
        self.assertTrue(any(e.get("action") == "catalyst_pending_approval" for e in self.g.get_events(limit=20)))
        # After approval it flows through.
        tagger.approve("NEWNAME")
        res2 = tagger.filter_trump_holdings(raw)
        if len(res2) == 3:
            acc2, rej2, pend2 = res2
        else:
            acc2, rej2 = res2
            pend2 = []
        self.assertIn("NEWNAME", [a["ticker"] for a in acc2], "approved name must enter the basket")


if __name__ == "__main__":
    unittest.main()
