# Pre-registered drawdown stop — pma (LIVE money)

*Dated 2026-07-16, BEFORE further loss. pma is the only live-money bot; it went live 2026-06-17
with ~$97.58 deployed and had NO written downside rule until now. This closes that gap per the
money rule ("every gate needs a metric, threshold, dated before the window"). Numbers set by
Brooks. Not edited except to append what fires.*

## Metric (checkable by script)
- **NAV:** `data/state.json → capital.own_nav` (the ring-fenced ownership-ledger mark; verified
  self-consistent = cash + Σ position market_value).
- **Cost basis (deployed capital):** **$97.58** (`capital.budget_allocation`).
- **Peak (high-water mark):** max own_nav ever recorded, persisted in `data/nav_hwm.json`.
  Seeded at $97.58 (no higher NAV has occurred — pma has only declined since inception).

## Thresholds & actions
| Level | Trigger | $ value | Action (automated by `bin/drawdown_guard.py`) |
|-------|---------|--------:|-----------------------------------------------|
| **WARN** | own_nav ≤ 75% of peak (25% drawdown from high-water mark) | **$73.19** | macOS notification + log. No trading change. |
| **HALT** | own_nav ≤ 50% of cost basis (50% loss of deployed capital) | **$48.79** | **Write `data/KILL_SWITCH`** → the executor fail-safe-halts all pma trading. Loud notification + log. |

## Status at registration (2026-07-16)
- own_nav **$70.43** → **27.8% drawdown**. **WARN is ALREADY TRIGGERED** (past $73.19).
- HALT ($48.79) is **not** triggered — $21.64 / ~31% of further downside remains.

## What HALT does and does NOT do (important)
- **DOES:** pull `KILL_SWITCH` so the bot stops trading (fail-safe — it can only STOP activity,
  never place an order). pma only trades on a new Leopold 13F anyway, so this freezes the next
  rebalance.
- **Does NOT:** liquidate the basket. Selling the live positions is a **live trade** and requires
  Brooks's explicit in-session confirmation (money rule). At HALT the guard flags "manual
  liquidation decision required" — it does not auto-sell. The basket rides until Brooks decides.

## On WARN (now)
Informational — the concentrated AI-infra / BTC-miner basket (CRWV/SNDK/MU/TSM + CLSK/RIOT/IREN/
CORZ/APLD) is doing what a high-beta basket does in a crypto selloff. No action mandated; this is
the "pay attention" line. Brooks may (a) let it ride to the HALT rule, (b) manually de-risk now
(a live trade, his confirm), or (c) tighten the HALT. Default = ride, per the pre-registered rule.

## Revision rule
Thresholds are frozen once set. Peak ratchets UP only (never down) as NAV recovers. To change a
threshold, supersede this doc with a new dated one — never edit the numbers mid-window.
