"""P1 fixture-based tests for real-ish Leopold 13F (no network in unittest).

Uses vendored sample XML. Tests parser, amendment supersede, unmapped skip+log.
Flag off regression covered by existing tests + this.
"""

import unittest
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.sources.leopold import LeopoldSource
from src.core.storage import GraveyardDB


class LeopoldEdgarFixtureTests(unittest.TestCase):
    def setUp(self):
        self.td = Path(tempfile.mkdtemp())
        self.data_dir = self.td / "data"
        self.data_dir.mkdir()
        self.g = GraveyardDB(self.data_dir)
        # force non-live
        self.src = LeopoldSource(cache_dir=self.data_dir, graveyard=self.g, live=False)

    def test_p1_parser_sample_xml(self):
        """Parser test: vendored real-format sample 13F-HR XML parses to expected rows with ticker (via map), value, side."""
        xml_path = Path(__file__).parent / "fixtures" / "sample_leopold_13f.xml"
        xml = xml_path.read_text()
        raw = self.src.parse_13f_xml(xml)
        self.assertGreaterEqual(len(raw), 3)
        # parse returns pre-resolve raw rows; check cusip/name/side from real-format fixture
        cusips = [h.get("cusip") for h in raw]
        self.assertIn("67066G104", cusips)  # NVDA
        self.assertIn("68389X105", cusips)
        # side for put
        sides = [h.get("side") for h in raw]
        self.assertIn("short", sides)

    def test_p1_amendment_supersedes(self):
        """Amendment test: drive the REAL _select_latest_13f_candidate (network-free).
        Given 13F-HR and a later 13F-HR/A for same period, /A must be chosen (supersedes, no double).
        """
        forms = ["13F-HR", "13F-HR/A"]
        accs = ["000204572426000007", "000204572426000008A"]
        fdates = ["2026-01-01", "2026-01-15"]
        cand = self.src._select_latest_13f_candidate(forms, accs, fdates)
        self.assertIsNotNone(cand)
        self.assertEqual(cand[1], "000204572426000008A")  # /A chosen
        self.assertEqual(cand[2], "13F-HR/A")

    def test_p1_unmapped_cusip_skipped_and_logged(self):
        """Unmapped: drive the REAL _resolve_and_filter (P5-2) with mixed rows.
        Must drop unmapped, keep mapped; the method itself must log exactly the cusip_unmapped (test does not write it).
        """
        # raw rows as parse_13f_xml would return (pre-ticker-resolve)
        raw_rows = [
            {"name": "NVIDIA CORP", "cusip": "67066G104", "value_usd": 1_000_000, "shares": 100, "side": "long"},
            {"name": "UNKNOWN CO", "cusip": "ZZZZZZZZZ", "value_usd": 50_000, "shares": 10, "side": "long"},
        ]
        before = len([e for e in self.g.get_events(limit=50) if e.get("action") == "cusip_unmapped"])
        out = self.src._resolve_and_filter(raw_rows, acc="test-acc-001")

        out_tickers = {h.get("ticker") for h in out}
        self.assertIn("NVDA", out_tickers, "mapped must be kept")
        self.assertNotIn("ZZZZZZZZZ", out_tickers)
        self.assertEqual(len(out), 1, "unmapped skipped")

        after = len([e for e in self.g.get_events(limit=50) if e.get("action") == "cusip_unmapped"])
        self.assertEqual(after, before + 1, "REAL method logged the event (test did not write it)")


if __name__ == "__main__":
    unittest.main()


class LeopoldFetchFailFallbackTests(unittest.TestCase):
    """Regression: on a transient live-fetch failure, get_current_basket must serve the last-known-good
    cached basket (stored in the PROCESSED 'source_weight' shape) without crashing — even with force=True."""

    def setUp(self):
        self.td = Path(tempfile.mkdtemp())
        self.data_dir = self.td / "data"; self.data_dir.mkdir()
        self.g = GraveyardDB(self.data_dir)

    def test_live_fetch_none_falls_back_to_cached_basket(self):
        src = LeopoldSource(cache_dir=self.data_dir, graveyard=self.g, live=True)
        # seed last-known-good cache in the PROCESSED shape (what gets persisted)
        src._cache["last_accession"] = "acc-1"
        src._cache["holdings"] = [
            {"ticker": "SNDK", "source_weight": 1.0e9, "side": "long", "filing_accession": "acc-1"},
            {"ticker": "SMH", "source_weight": 2.0e9, "side": "short", "filing_accession": "acc-1"},
        ]
        src._save_cache()
        # force a live-fetch failure
        src._fetch_latest_13f_live = lambda: None
        basket = src.get_current_basket(force=True)  # must NOT raise KeyError('value_usd')
        self.assertTrue(basket, "fallback must serve the cached basket, not empty")
        self.assertEqual({h["ticker"] for h in basket}, {"SNDK", "SMH"})
        self.assertTrue(all("source_weight" in h for h in basket))
