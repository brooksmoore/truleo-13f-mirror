#!/usr/bin/env python3
"""Standalone LIVE runner — Leopold-only mirror, decoupled broker transport.

Runs the full daily cycle as a plain process (no agent runtime): real EDGAR 13F + real Robinhood
via the OAuth MCP bridge. Default is a DRY run (reads live account, computes + prints the plan,
places NOTHING). Pass --execute to actually place orders on the agentic account (••••8050).

    cd /Users/brooksmoore/Desktop/truleo_agent
    live_broker/venv/bin/python -m live_broker.run_live              # DRY (safe)
    live_broker/venv/bin/python -m live_broker.run_live --execute    # LIVE (places real orders)

Cron (daily, after you've verified a dry + one attended live run):
    0 14 * * 1-5  cd /…/truleo_agent && live_broker/venv/bin/python -m live_broker.run_live --execute >> live_broker/cron.log 2>&1
"""
from __future__ import annotations
import argparse, sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from config import leopold_only_config
from src.mirror_agent import run_cycle
from src.sources.leopold import LeopoldSource
from src.sources.trump import TrumpSource
from src.tagger import CatalystTagger
from src.executor import MirrorExecutor
from src.core.storage import GraveyardDB, PersistentLog
from src.mcp.robinhood_client import RealRobinhoodClient
from live_broker.rh_transport import RobinhoodMCPBridge


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--execute", action="store_true", help="Place real orders (default: dry run, no orders).")
    ap.add_argument("--cycles", type=int, default=1)
    args = ap.parse_args()

    cfg = leopold_only_config(use_live_edgar=True, use_live_broker=True, skip_attribution=True)
    data_dir = Path(cfg.data_dir); data_dir.mkdir(exist_ok=True)
    logs_dir = Path(cfg.logs_dir); logs_dir.mkdir(exist_ok=True)
    kill = data_dir / "KILL_SWITCH"

    mode = "EXECUTE (real orders)" if args.execute else "DRY (no orders)"
    print(f"=== truleo_agent LIVE runner — Leopold-only — {mode} ===")
    if args.execute and kill.exists():
        print(f"KILL_SWITCH present at {kill} — refusing to execute. Remove it to trade."); sys.exit(2)

    g = GraveyardDB(data_dir); pl = PersistentLog(logs_dir)
    with RobinhoodMCPBridge() as bridge:
        client = RealRobinhoodClient(live_call_fn=bridge.call)
        # DRY: force the executor's kill check True → computes + prints the plan, places nothing.
        is_killed = (lambda: True) if not args.execute else None
        ex = MirrorExecutor(client=client, graveyard=g, plog=pl, data_dir=data_dir, is_killed=is_killed,
                            place_spacing_sec=4.0)  # space orders to stay under RH's order-rate throttle

        # OWNERSHIP LEDGER SEED (one-time migration):
        # truleo is the sole tenant today, so every current position is genuinely truleo's.
        # After this seed, any new position that appears without a ledger record will be left untouched
        # on the sell path. Seed is idempotent — safe to call every startup.
        if not ex.ledger.is_seeded():
            snap = ex.get_portfolio_snapshot()
            ex.ledger.seed(snap["raw_positions"], snap["total_equity"])
            ex._positions_cache = None  # force fresh read for the actual cycle
            print(f"[ledger] SEEDED: {len(ex.ledger.get_all_owned())} positions, "
                  f"seed_budget=${ex.ledger.budget_usd():.2f} (reference only — sizing compounds off "
                  f"live own_nav from here; verify this number against your real account value before "
                  f"trusting it: a flaky-quote at seed silently under-seeds, never over-seeds)")
        else:
            owned = ex.ledger.get_all_owned()
            own_nav = ex._compute_own_nav()
            print(f"[ledger] loaded: {len(owned)} owned tickers, seed_budget=${ex.ledger.budget_usd():.2f}, "
                  f"current own_nav=${own_nav:.2f} (this is what sizing uses today)")

        leop = LeopoldSource(cache_dir=data_dir, graveyard=g, live=True)
        trump = TrumpSource(cache_dir=data_dir, graveyard=g, live=cfg.use_live_trump)
        tagger = CatalystTagger(graveyard=g, llm_client=None, live=cfg.use_live_tagger,
                                can_spend=getattr(ex, "can_spend", None), record_spend=getattr(ex, "record_spend", None))
        for c in range(args.cycles):
            print(f"\n--- cycle {c+1} ({mode}) ---")
            run_cycle(leop, trump, tagger, ex, data_dir, force=(c == 0), c=c, cfg=cfg)
        _emit_umbrella_snapshot(ex, g, cfg, data_dir, kill)
    print("\nDone." + ("" if args.execute else "  (DRY — nothing was placed. Re-run with --execute to trade.)"))


def _emit_umbrella_snapshot(ex, graveyard, cfg, data_dir: Path, kill: Path) -> None:
    """Read-only side effect: write canonical data/state.json for the umbrella dashboard."""
    try:
        from snapshot_emit import (
            emit_truleo_snapshot,
            price_fn_from_client,
            quote_degraded_for_ledger,
        )
    except ImportError:
        print("[snapshot] umbrella_core not installed — pip install -e ../umbrella")
        return
    try:
        snap = ex.get_portfolio_snapshot()
        lookup = price_fn_from_client(ex.client)
        ok = emit_truleo_snapshot(
            data_dir / "state.json",
            ledger=ex.ledger,
            price_fn=lookup,
            account_total=float(snap["total_equity"]),
            cfg=cfg,
            data_dir=data_dir,
            killed=kill.exists(),
            graveyard=graveyard,
            quote_degraded=quote_degraded_for_ledger(ex.ledger, lookup),
        )
        if ok:
            own_nav = ex.ledger.own_nav(lookup)
            print(f"[snapshot] wrote {data_dir / 'state.json'} own_nav=${own_nav:.2f}")
    except Exception as exc:
        print(f"[snapshot] emit failed: {exc}")


if __name__ == "__main__":
    main()
