#!/usr/bin/env python3
"""ONE-ORDER diagnostic place — surfaces the real place_equity_order result/error.

Places a single SMALL real order (default ~$2 of CLSK) through the decoupled bridge + RealRobinhoodClient,
and prints the FULL OrderResult (now incl. the real broker error message). Either confirms the live wire
works (you'll own ~$2 of CLSK) or shows exactly why place failed — without firing all ten.

    cd /Users/brooksmoore/Desktop/portfolio-mirror-agent
    live_broker/venv/bin/python -m live_broker.probe_place            # ~$2 CLSK
    live_broker/venv/bin/python -m live_broker.probe_place RIOT 2     # custom ticker / dollars
"""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from live_broker.rh_transport import RobinhoodMCPBridge
from src.mcp.robinhood_client import RealRobinhoodClient
from dataclasses import asdict

TICKER = sys.argv[1] if len(sys.argv) > 1 else "CLSK"
DOLLARS = float(sys.argv[2]) if len(sys.argv) > 2 else 2.0

with RobinhoodMCPBridge() as b:
    c = RealRobinhoodClient(live_call_fn=b.call)
    q = c.get_quote(TICKER)
    px = q.last or (q.ask + q.bid) / 2
    qty = round(DOLLARS / px, 6)
    print(f"Placing BUY {qty} {TICKER} (~${qty*px:.2f}) at last={px} ...")
    res = c.place_market_order(TICKER, "buy", qty)
    print("RESULT:", asdict(res))
