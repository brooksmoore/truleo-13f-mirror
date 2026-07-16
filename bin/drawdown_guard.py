#!/usr/bin/env python3
"""pma live drawdown guard — enforces DRAWDOWN_STOP_2026-07-16.md.

Reads the ring-fenced NAV, ratchets a persisted high-water mark, and:
  - WARN at own_nav <= 75% of peak (25% drawdown)  -> notify + log
  - HALT at own_nav <= 50% of cost basis           -> write data/KILL_SWITCH (fail-safe
    stop; the executor already treats KILL_SWITCH presence as killed), notify LOUD, log.
    HALT never sells — liquidation is Brooks's live-trade call (money rule).

Zero-cost, no LLM, read-only except the HWM file and (on HALT) the KILL_SWITCH file.
Fail-safe: any error reading state does NOT create a KILL_SWITCH (never halt on a glitch),
but DOES log — a missing/garbled NAV is surfaced, not silently ignored.
"""
from __future__ import annotations

import datetime as dt
import json
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
STATE = ROOT / "data" / "state.json"
HWM = ROOT / "data" / "nav_hwm.json"
KILL = ROOT / "data" / "KILL_SWITCH"
LOG = ROOT / "logs" / "drawdown_guard.log"

COST_BASIS = 100.00         # starting bankroll (Brooks funded ~$100; the round bankroll is the risk baseline)
WARN_FRAC = 0.75            # 25% drawdown from peak
HALT_FRAC_OF_COST = 0.50    # 50% loss of deployed capital
HALT_LEVEL = HALT_FRAC_OF_COST * COST_BASIS  # $50.00


def _notify(title: str, msg: str) -> None:
    subprocess.run(
        ["osascript", "-e", f'display notification "{msg}" with title "{title}" sound name "Basso"'],
        capture_output=True,
    )


def _log(line: str) -> None:
    LOG.parent.mkdir(parents=True, exist_ok=True)
    with LOG.open("a") as f:
        f.write(f"{dt.datetime.now(dt.timezone.utc).isoformat()} {line}\n")


def _load_hwm(seed: float) -> float:
    try:
        return float(json.loads(HWM.read_text())["peak"])
    except Exception:
        return seed


def _save_hwm(peak: float) -> None:
    HWM.write_text(json.dumps({"peak": round(peak, 4), "updated": dt.datetime.now(dt.timezone.utc).isoformat()}))


def main() -> int:
    try:
        nav = float(json.loads(STATE.read_text())["capital"]["own_nav"])
    except Exception as e:
        _log(f"ERROR reading own_nav ({e}) — no action taken (fail-safe: never halt on a glitch)")
        _notify("⚠️ pma drawdown guard", "could not read NAV — check state.json")
        return 1

    # ratchet the high-water mark up only (seed at cost basis; pma has only declined)
    peak = max(_load_hwm(COST_BASIS), nav, COST_BASIS)
    _save_hwm(peak)

    warn_level = WARN_FRAC * peak
    dd = 100 * (1 - nav / peak)
    loss = 100 * (1 - nav / COST_BASIS)

    if nav <= HALT_LEVEL:
        first = not KILL.exists()
        KILL.write_text(
            f"HALT auto-tripped by drawdown_guard {dt.datetime.now(dt.timezone.utc).isoformat()}\n"
            f"own_nav=${nav:.2f} <= HALT ${HALT_LEVEL:.2f} (50% loss of ${COST_BASIS:.2f}). "
            f"pma trading fail-safe-halted. Manual liquidation is Brooks's live-trade call — this "
            f"file does NOT sell. Remove this file to resume trading.\n"
        )
        _log(f"HALT own_nav=${nav:.2f} <= ${HALT_LEVEL:.2f} loss={loss:.1f}% -> KILL_SWITCH written")
        if first:
            _notify("🛑 pma HALT", f"NAV ${nav:.2f} hit 50% loss. KILL_SWITCH set. Liquidation is YOUR call.")
        print(f"HALT: own_nav=${nav:.2f} <= ${HALT_LEVEL:.2f} — KILL_SWITCH written")
        return 0

    if nav <= warn_level:
        _log(f"WARN own_nav=${nav:.2f} <= ${warn_level:.2f} dd={dd:.1f}% (peak ${peak:.2f})")
        _notify("⚠️ pma drawdown WARN", f"NAV ${nav:.2f}, {dd:.0f}% off peak. Halt at ${HALT_LEVEL:.2f}.")
        print(f"WARN: own_nav=${nav:.2f} dd={dd:.1f}% (warn≤${warn_level:.2f}, halt≤${HALT_LEVEL:.2f})")
        return 0

    _log(f"OK own_nav=${nav:.2f} dd={dd:.1f}% (warn≤${warn_level:.2f})")
    print(f"OK: own_nav=${nav:.2f} dd={dd:.1f}% — above warn ${warn_level:.2f}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
