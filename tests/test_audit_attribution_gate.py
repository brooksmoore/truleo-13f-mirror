"""AUDITOR-OWNED gate tests (independent) for Pass #6 — attribution on real prices.

Drive the REAL Attribution.monthly_report to assert:
  P6-2: the benchmark comparison is a genuine RETURN DELTA (accepted vs rejected vs SMH/SPY),
        computed from entry-ref vs current prices — not current price *levels*; and
        `has_bench_delta` is data-driven (False when benchmark entry refs are absent).
  P6-1/P6-3: a price failure / missing price is NOT fabricated into the mtm aggregates;
        missing prices are excluded and surfaced via `missing_price_count`.

EXPECTED TO FAIL against pre-Pass-6 code (no real delta keys; has_bench_delta hardcoded True;
unknown tickers default to 100.0). That failure is the proof the findings are real.

Do not let Grok edit this file. Fixes belong in src/attribution.py.
"""

import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.core.storage import GraveyardDB
from src.attribution import Attribution


class AttributionGate(unittest.TestCase):
    def setUp(self):
        self.td = Path(tempfile.mkdtemp())
        self.dd = self.td / "data"
        self.dd.mkdir()
        self.g = GraveyardDB(self.dd)
        self.attr = Attribution(self.g, self.dd)

    def test_benchmark_is_real_return_delta_not_levels(self):
        """Seed accept/reject records WITH benchmark entry refs; inject current prices so
        returns are known. The report must expose real deltas (accepted-vs-rejected and
        accepted-vs-SMH/SPY), consistent with the seeded numbers — not raw price levels."""
        # Accepted name: entry 100 -> now 120 = +20%. Rejected: entry 100 -> now 105 = +5%.
        # Benchmark entry refs stamped at decision: SMH 200, SPY 500.
        self.g.record_event(action="catalyst_accept", ticker="NVDA",
                            meta={"entry_ref_price": 100.0, "catalyst_type": "policy_tailwind",
                                  "bench_entry": {"SMH": 200.0, "SPY": 500.0}})
        self.g.record_event(action="catalyst_reject", ticker="LEGACY",
                            meta={"entry_ref_price": 100.0, "catalyst_type": "legacy",
                                  "bench_entry": {"SMH": 200.0, "SPY": 500.0}})

        # current prices: NVDA +20%, LEGACY +5%, SMH +10% (220), SPY +0% (500)
        prices = {"NVDA": 120.0, "LEGACY": 105.0, "SMH": 220.0, "SPY": 500.0}
        report = self.attr.monthly_report(price_fn=lambda t: prices.get(t))

        self.assertTrue(report.get("has_bench_delta"),
                        "has_bench_delta must be True when bench entry refs are present")
        # Real delta keys must exist and be numeric return comparisons.
        d = report.get("deltas") or {}
        for k in ("accepted_vs_rejected", "accepted_vs_SMH", "accepted_vs_SPY"):
            self.assertIn(k, d, f"report must expose a real return delta '{k}', not price levels")
        # accepted +20% - rejected +5% = +15pp ; accepted +20% - SMH +10% = +10pp ; vs SPY +0% = +20pp
        self.assertAlmostEqual(d["accepted_vs_rejected"], 0.15, places=3)
        self.assertAlmostEqual(d["accepted_vs_SMH"], 0.10, places=3)
        self.assertAlmostEqual(d["accepted_vs_SPY"], 0.20, places=3)

    def test_has_bench_delta_false_without_bench_entry_refs(self):
        """When records carry no benchmark entry refs, a real delta is not computable;
        has_bench_delta must be False (not hardcoded True)."""
        self.g.record_event(action="catalyst_accept", ticker="NVDA",
                            meta={"entry_ref_price": 100.0})  # no bench_entry
        report = self.attr.monthly_report(price_fn=lambda t: 120.0)
        self.assertFalse(report.get("has_bench_delta"),
                         "has_bench_delta must be data-driven (False without bench entry refs)")

    def test_missing_price_not_fabricated_into_mtm(self):
        """A name whose current price is missing (None) must be excluded from the mtm
        aggregate and counted in missing_price_count — never folded in as a fabricated value."""
        self.g.record_event(action="catalyst_accept", ticker="GOODP",
                            meta={"entry_ref_price": 100.0})
        self.g.record_event(action="catalyst_accept", ticker="NOPRICE",
                            meta={"entry_ref_price": 100.0})

        def price_fn(t):
            return 130.0 if t == "GOODP" else None  # NOPRICE missing

        report = self.attr.monthly_report(price_fn=price_fn)
        self.assertGreaterEqual(report.get("missing_price_count", 0), 1,
                                "missing prices must be surfaced, not silently defaulted")
        # The one good name's return is +30%; the missing one must NOT dilute it toward a
        # fabricated default (e.g. 100.0 -> 0% or 0.0 -> -100%).
        acc = report.get("accepted_mtm", [])
        self.assertTrue(all(abs(x - 0.30) < 1e-6 for x in acc),
                        "missing-price name must be excluded from mtm, not fabricated")


if __name__ == "__main__":
    unittest.main()
