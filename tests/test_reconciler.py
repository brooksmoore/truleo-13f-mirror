"""Unit tests for pure reconciler (order independence, fixed-point cap, overlap sum, drift, min floor)."""

import unittest
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.reconciler import reconcile, select_top_n, normalize_sleeve, merge_sleeves, _apply_cap_iterative, apply_min_floor
from config import CFG, override
from src.core.schemas import Position


class TestReconcilerMath(unittest.TestCase):
    def test_top_n(self):
        h = [{"ticker": f"T{i}", "source_weight": 100-i} for i in range(15)]
        top = select_top_n(h, "trump", n=5)
        self.assertEqual(len(top), 5)
        self.assertEqual(top[0].ticker, "T0")

    def test_normalize(self):
        poss = [Position("A", source_weight=100), Position("B", source_weight=300)]
        out = normalize_sleeve(poss, 0.5)
        self.assertAlmostEqual(out[0].target_weight, 0.125, places=3)
        self.assertAlmostEqual(out[1].target_weight, 0.375, places=3)
        self.assertAlmostEqual(sum(p.target_weight for p in out), 0.5, places=5)

    def test_merge_overlap_sum(self):
        t = [Position("NVDA", source_weight=50, target_weight=0.25)]
        l = [Position("NVDA", source_weight=100, target_weight=0.3), Position("X", source_weight=10, target_weight=0.2)]
        merged, conflicts = merge_sleeves(t, l)
        nv = next(p for p in merged if p.ticker == "NVDA")
        self.assertAlmostEqual(nv.target_weight, 0.55)
        self.assertEqual(len(merged), 2)
        self.assertEqual(conflicts, [])

    def test_cap_iterative_converges_and_caps(self):
        poss = [Position("A", target_weight=0.6), Position("B", target_weight=0.25), Position("C", target_weight=0.15)]
        capped, _ = _apply_cap_iterative(poss, 0.15)
        ws = [p.target_weight for p in capped]
        self.assertTrue(all(w <= 0.150001 for w in ws))
        self.assertLessEqual(sum(ws), 1.00001)

    def test_cap_order_independent(self):
        # shuffle order shouldn't matter; also assert matches known correct fixed point (C2)
        base = [Position("H", target_weight=0.4), Position("M", target_weight=0.35), Position("L", target_weight=0.25)]
        c1, _ = _apply_cap_iterative([p for p in base], 0.2)
        c2, _ = _apply_cap_iterative([p for p in reversed(base)], 0.2)
        s1 = sorted((p.ticker, round(p.target_weight,4)) for p in c1)
        s2 = sorted((p.ticker, round(p.target_weight,4)) for p in c2)
        self.assertEqual(s1, s2)
        # known fixed point for this input+cap: each capped at 0.2, sum=0.6, residual cash 0.4 (0.4+0.35+0.25=0.999~1)
        expected = sorted([("H", 0.2), ("M", 0.2), ("L", 0.2)])
        self.assertEqual(s1, expected)

    def test_c2_cap_fixed_point_070_025_005(self):
        """C2: [0.7, 0.25, 0.05] cap 0.30 must reach exactly 0.30/0.30/0.30 (sum 0.90) per spec fixed point; no wrongful leak to cash."""
        poss = [Position("A", target_weight=0.7), Position("B", target_weight=0.25), Position("C", target_weight=0.05)]
        input_sum = sum(p.target_weight for p in poss)
        capped, _ = _apply_cap_iterative(poss, 0.30)
        ws = [p.target_weight for p in capped]
        self.assertAlmostEqual(ws[0], 0.30, places=9)
        self.assertAlmostEqual(ws[1], 0.30, places=9)
        self.assertAlmostEqual(ws[2], 0.30, places=9)
        self.assertAlmostEqual(sum(ws), 0.90, places=9)
        # conservation: final sum + residual_cash == input_sum
        residual = input_sum - sum(ws)
        self.assertAlmostEqual(sum(ws) + residual, input_sum, places=9)
        self.assertGreater(residual, 0.09)  # ~0.10 residual cash

    def test_c2_cap_all_over_residual_to_cash(self):
        """C2: four names all over 0.15 cap → each exactly 0.15, sum=0.60, residual cash, no extra leak."""
        poss = [Position(f"N{i}", target_weight=0.3) for i in range(4)]
        input_sum = sum(p.target_weight for p in poss)
        capped, _ = _apply_cap_iterative(poss, 0.15)
        ws = [p.target_weight for p in capped]
        self.assertTrue(all(abs(w - 0.15) < 1e-9 for w in ws))
        self.assertAlmostEqual(sum(ws), 0.60, places=9)
        residual = input_sum - sum(ws)
        self.assertAlmostEqual(sum(ws) + residual, input_sum, places=9)
        self.assertGreater(residual, 0.5)

    def test_full_reconcile_caps_and_produces_minimal_orders(self):
        cfg = override(top_n_per_sleeve=3, per_name_cap=0.15, drift_band_pct=0.0, drift_band_usd=0.0)
        trump = [{"ticker": "NVDA", "source_weight": 50e6}, {"ticker": "ORCL", "source_weight": 30e6}, {"ticker": "AVGO", "source_weight": 10e6}]
        leop = [{"ticker": "NVDA", "source_weight": 1.57e9}, {"ticker": "SMH", "source_weight": 2.04e9}]
        plan = reconcile(trump, leop, [], cfg=cfg, sleeve_total_usd=100_000, current_weights={})
        ws = [p.target_weight for p in plan.targets]
        self.assertTrue(all(w <= 0.15001 for w in ws))
        self.assertGreater(len(plan.orders), 0)  # should want buys

    def test_min_floor_drops_and_redist(self):
        poss = [Position("BIG", target_weight=0.4, source_weight=100), Position("TINY", target_weight=0.01, source_weight=1)]
        # use floor_pct=0.03 so eff~0.015 total; TINY 0.01 < drops (test intent preserved)
        out = apply_min_floor(poss, 0.03, 10.0, sleeve_total_usd=10000)
        tickers = [p.ticker for p in out]
        self.assertNotIn("TINY", tickers)
        self.assertGreater(out[0].target_weight, 0.4)  # got some of the dropped

    def test_m3_min_floor_sleeve_relative_2pct_survives(self):
        """M3: a name at 2% of its sleeve (0.01 of total for 50% sleeve) must survive floor_pct=0.015 (eff 0.0075 of total); was wrongly dropped at 2x before."""
        poss = [Position("MID", target_weight=0.01, source_weight=10), Position("BIG", target_weight=0.4, source_weight=100)]
        out = apply_min_floor(poss, 0.015, 1.0, sleeve_total_usd=10000, sleeve_alloc=0.5)
        tickers = [p.ticker for p in out]
        self.assertIn("MID", tickers, "2% of sleeve must survive (sleeve-relative floor)")
        self.assertIn("BIG", tickers)

    def test_m6_long_short_conflict_flagged_not_netted(self):
        """M6/§9: when same ticker long in Trump + short in Leopold (config-forced), conflict flagged in plan, not silently netted (no zeroing)."""
        # enable shorts for test data (reconciler sees sides regardless)
        trump = [{"ticker": "SEMI", "source_weight": 100}]
        leop = [{"ticker": "SEMI", "source_weight": 200, "side": "short"}]
        plan = reconcile(trump, leop, [], cfg=CFG, sleeve_total_usd=1000, current_weights={})
        self.assertTrue(len(plan.conflicts) >= 1)
        self.assertEqual(plan.conflicts[0]["ticker"], "SEMI")
        self.assertIn("short", str(plan.conflicts[0]))
        # target not netted to zero; has some positive from trump
        semi_targets = [p for p in plan.targets if p.ticker == "SEMI"]
        self.assertTrue(len(semi_targets) > 0)
        self.assertGreater(semi_targets[0].target_weight, 0.0)

    def test_m7_corporate_actions_no_spurious_buy_sell(self):
        """CA (M7): ticker change (OLD->NEW) + adjust hook called pre-recon; no spurious exit/buy for the rename (current adjusted to match raw)."""
        from src.sources import corporate_actions
        raw = [{"ticker": "NEWTICKER", "source_weight": 100}]
        curr = [{"ticker": "OLDTICKER", "weight": 0.1}]
        h2, c2 = corporate_actions.adjust_for_corporate_actions(raw, curr)
        self.assertEqual(h2[0]["ticker"], "NEWTICKER")
        self.assertEqual(c2[0]["ticker"], "NEWTICKER")  # mapped, no mismatch
        # recon with adjusted would see match, no source_exit
        plan = reconcile(h2, [], c2, cfg=CFG, current_weights={"NEWTICKER": 0.1})
        exits = [o for o in plan.orders if o.reason == "source_exit"]
        self.assertEqual(len(exits), 0, "CA rename must not cause spurious exit")

    def test_m7_single_range_midpoint_parser(self):
        """M7: single _midpoint (in reconciler, called by trump) covers spec cases; no divergent parsers."""
        from src.reconciler import _midpoint
        self.assertAlmostEqual(_midpoint("$1M-$5M"), 3_000_000.0)
        self.assertAlmostEqual(_midpoint("$500K-$1M"), 750_000.0)
        self.assertAlmostEqual(_midpoint("over $50M"), 50_000_000.0)
        self.assertAlmostEqual(_midpoint(">$50M"), 50_000_000.0)
        self.assertAlmostEqual(_midpoint("$10M"), 10_000_000.0)

    def test_source_exit_emits_sell_order(self):
        cfg = override(drift_band_pct=0.0, drift_band_usd=0.0)
        plan = reconcile([], [], [{"ticker": "OLD", "weight": 0.05}], cfg=cfg, current_weights={"OLD": 0.05})
        self.assertTrue(any(o.reason == "source_exit" and o.ticker == "OLD" for o in plan.orders))

    def test_shorts_never_enter_basket_when_disabled_signal_inversion_guard(self):
        """§9 SIGNAL-INVERSION GUARD (found in 2026-06-16 paper validation): a 13F lists puts as
        side=short with LARGE $ values. With enable_shorts=False those must NEVER be bought nor
        consume top-N slots. Real-data shape: big short puts on NVDA/AVGO + smaller real longs."""
        leop = [
            {"ticker": "NVDA", "source_weight": 1_500_000_000, "side": "short"},
            {"ticker": "AVGO", "source_weight": 1_000_000_000, "side": "short"},
            {"ticker": "SNDK", "source_weight": 100_000_000, "side": "long"},
            {"ticker": "BE", "source_weight": 80_000_000, "side": "long"},
        ]
        cfg = override(enable_shorts=False, sleeve_trump=0.0, sleeve_leopold=1.0)
        plan = reconcile([], leop, [], cfg=cfg, sleeve_total_usd=100_000, current_weights={})
        tkrs = {p.ticker for p in plan.targets}
        self.assertNotIn("NVDA", tkrs, "must not BUY a short position")
        self.assertNotIn("AVGO", tkrs, "must not BUY a short position")
        self.assertEqual(tkrs, {"SNDK", "BE"}, "only the real longs may be in the basket")
        self.assertFalse(any(o.ticker in ("NVDA", "AVGO") for o in plan.orders), "no orders for shorts")

    def test_cross_side_conflict_still_flagged_with_shorts_disabled(self):
        """M6 preserved: a name long in one sleeve + short in the other is still surfaced as a
        conflict even though the short leg is excluded from the executable basket."""
        cfg = override(enable_shorts=False)
        plan = reconcile([{"ticker": "SEMI", "source_weight": 100, "side": "long"}],
                         [{"ticker": "SEMI", "source_weight": 200, "side": "short"}],
                         [], cfg=cfg, current_weights={})
        self.assertTrue(any(c["ticker"] == "SEMI" for c in plan.conflicts))
        self.assertIn("short", str(plan.conflicts))


if __name__ == "__main__":
    unittest.main()
