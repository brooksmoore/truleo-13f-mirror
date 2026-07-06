"""AUDITOR-OWNED gate tests (independent of Grok's tests) for P4-1 / P4-2.

Drive the REAL Leopold code — no network, no tautologies — to assert:
  P4-1: a live-fetch failure does NOT silently substitute the demo fixture (fail-safe, not fail-open).
  P4-2: the real CUSIP resolve→skip→log path skips an unmapped holding and logs `cusip_unmapped`,
        WITHOUT the test writing the event itself.

These encode the acceptance criteria for Pass #5 and MUST pass after the fix. They are EXPECTED TO
FAIL against the pre-fix code — that failure is the proof the findings are real.

Do not let Grok edit this file to make it pass. The fixes belong in src/sources/leopold.py:
  - guard the fixture behind `live=False` (no fixture failover when live=True), and
  - extract the parse→resolve→skip/log step into a network-free method `_resolve_and_filter(raw_rows, acc)`
    that returns the mapped holdings and logs `cusip_unmapped` for skips.
"""

import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.sources.leopold import LeopoldSource
from src.core.storage import GraveyardDB


class LeopoldFailSafeGate(unittest.TestCase):
    def setUp(self):
        self.td = Path(tempfile.mkdtemp())
        self.dd = self.td / "data"
        self.dd.mkdir()
        self.g = GraveyardDB(self.dd)

    # ---- P4-1: live error must NOT fall back to the demo fixture ----
    def test_live_fetch_failure_does_not_serve_fixture(self):
        """A live source whose real fetch fails must report 'no data' (None / empty),
        NEVER the hardcoded demo basket. Otherwise a transient EDGAR outage rebalances
        the sleeve toward stale fake holdings (fail-open, violates §8)."""
        src = LeopoldSource(cache_dir=self.dd, graveyard=self.g, live=True)
        # Force the real fetch to fail, as a live outage / 403 / format change would.
        src._fetch_latest_13f_live = lambda: None

        latest = src.get_latest_13f()
        basket = src.get_current_basket(force=True)

        # The demo fixture is SMH/NVDA/ORCL. On a live failure none of it may appear.
        fixture_tickers = {"SMH", "NVDA", "ORCL"}
        served = {h.get("ticker") for h in (basket or [])}
        self.assertFalse(
            served & fixture_tickers,
            f"live-fetch failure served demo fixture holdings {served & fixture_tickers} "
            "(fail-open). Must return no data / last-known-good instead.",
        )
        # Acceptable fail-safe outcomes: None latest, or an empty basket.
        self.assertTrue(latest is None or not basket,
                        "live failure must yield no tradeable basket (fail-safe)")

    # ---- P4-2: real resolve→skip→log path, no test-written event ----
    def test_unmapped_cusip_skipped_and_logged_by_real_path(self):
        """Drive the REAL resolve/skip/log method with parsed rows containing one unmapped
        CUSIP. The method must drop it AND log `cusip_unmapped` itself — the test must not
        write the event. Requires `_resolve_and_filter(raw_rows, acc)` to exist (Pass #5)."""
        src = LeopoldSource(cache_dir=self.dd, graveyard=self.g, live=True)
        self.assertTrue(
            hasattr(src, "_resolve_and_filter"),
            "expected network-free method _resolve_and_filter(raw_rows, acc) so the real "
            "skip+log path is testable (Pass #5 refactor).",
        )
        raw_rows = [
            {"name": "NVIDIA CORP", "cusip": "67066G104", "value_usd": 1_000_000, "shares": 100, "side": "long"},
            {"name": "UNKNOWN CO", "cusip": "ZZZZZZZZZ", "value_usd": 50_000, "shares": 10, "side": "long"},
        ]
        before = len([e for e in self.g.get_events(limit=50) if e.get("action") == "cusip_unmapped"])
        out = src._resolve_and_filter(raw_rows, acc="000111-22-333333")

        out_tickers = {h.get("ticker") for h in out}
        self.assertIn("NVDA", out_tickers, "mapped CUSIP must resolve and be kept")
        self.assertNotIn(None, out_tickers, "unmapped holding must be dropped, not kept with ticker=None")
        self.assertEqual(len(out), 1, "exactly the mapped holding survives; unmapped is skipped")

        after = len([e for e in self.g.get_events(limit=50) if e.get("action") == "cusip_unmapped"])
        self.assertEqual(after, before + 1,
                         "the REAL method must log exactly one cusip_unmapped event (not the test)")


if __name__ == "__main__":
    unittest.main()
