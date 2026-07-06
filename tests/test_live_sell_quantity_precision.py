"""AUDITOR GATE (2026-07-06) — live SELL orders must format `quantity` at <=6 decimal
places, matching Robinhood's own place_equity_order contract ("Fractional shares: ...
up to 6 decimal places"). The prior code formatted at 8dp (f"{qty:.8f}"), 2 digits over
the documented cap, and every live rebalance SELL failed with the broker's real
"Order quantity cannot include fractional shares" 400 (confirmed live 2026-07-06, BE/TSM)
once the separate idemp-key bug (fixed same day) stopped masking it.
"""
from __future__ import annotations
import sys, unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.mcp.robinhood_client import RealRobinhoodClient


class LiveSellQuantityPrecisionGate(unittest.TestCase):
    def test_sell_quantity_formatted_at_six_decimal_places_max(self):
        captured = {}

        def fake_live_call(full_tool_name, **params):
            if full_tool_name == "mcp__robinhood-trading__place_equity_order":
                captured.update(params)
                return {"order": {"id": "OID", "state": "filled", "cumulative_quantity": params["quantity"]}}
            raise AssertionError(f"unexpected tool call: {full_tool_name}")

        client = RealRobinhoodClient(live_call_fn=fake_live_call)
        # A share count with more precision than 6dp (as translate_weight_to_shares
        # produces from a float division) — the exact shape that broke live.
        client.place_market_order("BE", "sell", 0.0638451234567, is_fractional=True)

        self.assertIn("quantity", captured)
        qty_str = captured["quantity"]
        decimals = qty_str.split(".")[1] if "." in qty_str else ""
        self.assertLessEqual(
            len(decimals), 6,
            f"sell quantity {qty_str!r} exceeds Robinhood's documented 6-decimal-place fractional limit",
        )


if __name__ == "__main__":
    unittest.main()
