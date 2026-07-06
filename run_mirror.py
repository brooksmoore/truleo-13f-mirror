#!/usr/bin/env python3
"""Top-level entry for mirror-basket agent.

Usage:
  PYTHONPATH=. python3 run_mirror.py --paper --cycles 3
  PYTHONPATH=. python3 run_mirror.py --leopold-only --cycles 3   # Trump sleeve silenced, 100% Leopold

Going live (owner decision; live gate is OFF by default): add live flags on top of the
leopold-only profile in a LOCAL runner, never by committing config defaults, e.g.
  from config import leopold_only_config
  cfg = leopold_only_config(use_live_edgar=True, use_live_broker=True)
  run_demo(cfg=cfg)
"""

import argparse
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).parent))

from src.mirror_agent import run_demo
from config import leopold_only_config

if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--paper", action="store_true", default=True)
    ap.add_argument("--cycles", type=int, default=2)
    ap.add_argument("--leopold-only", action="store_true",
                    help="Silence the (dropped) Trump sleeve and allocate 100%% to Leopold (canonical post-2026-06-16 profile).")
    args = ap.parse_args()
    cfg = leopold_only_config() if args.leopold_only else None
    run_demo(paper=args.paper, cycles=args.cycles, cfg=cfg)
