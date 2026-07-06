# truleo — 13F Mirror-Basket Trading Agent

A live trading bot that automatically mirrors the disclosed public stock holdings of a specific
hedge fund (a 13F "smart money" mirror strategy), running on a ring-fenced ~$100 Robinhood
account. Currently live since June 2026.

## What it does

Institutional investment managers with >$100M in US equities must publicly disclose their long
holdings every quarter via SEC Form 13F. This bot:

1. Pulls the target fund's latest 13F filing directly from EDGAR (SEC's public filings API).
2. Builds a proportional mirror of their top-10 disclosed long positions.
3. Rebalances the live account toward those target weights as prices drift, respecting a
   minimum position floor and fractional-share constraints.
4. Tracks its own capital and share ownership independently of the broker account (see
   **Ownership ledger** below) so it never tries to sell shares it doesn't actually own.

No LLM makes trading decisions in the core rebalancing path — it's a deterministic reconciler.
The only model call is a small cached classifier used to screen a secondary catalyst-detection
feature; it costs pennies total and touches zero dollars of position sizing.

## Why this is harder than it sounds

Mirroring a 13F is a simple idea with several nasty edge cases in practice, most of which this
project found the hard way against a real brokerage API:

- **Fractional order precision.** Robinhood's own API only accepts fractional share quantities
  up to 6 decimal places. A one-digit-too-many bug here caused every rebalancing sell to reject
  silently for weeks before being caught (see `AUDIT-LEDGER.md` for the full writeup).
- **Idempotency vs. real rebalancing.** A naive "don't place the same order twice" guard, if
  keyed on the wrong field, can permanently block legitimate re-trades of the same ticker for
  an entire filing cycle. Also found and fixed live.
- **Ownership tracking independent of the broker.** The account could theoretically hold
  foreign positions (manual trades, a shared account, etc). The bot maintains its own ledger of
  what it actually bought, and refuses to sell anything it doesn't have a record of — fail-closed,
  not fail-open.
- **Broker-integration reality**: rate limiting, idempotency-key poisoning on transient failures,
  fractional-vs-notional order semantics that differ between buys and sells, and an intermittent
  MCP bridge that needed reconnect-with-backoff handling.

## Architecture

```
src/
  mirror_agent.py       # orchestrator — event-driven on new 13F filings, not a fixed schedule
  reconciler.py          # pure deterministic weight math: top-N select, caps, minimal-diff orders
  executor.py             # order sizing, ownership sell-gate, safety checks, idempotency
  ownership_ledger.py     # bot-local ledger of owned shares, independent of broker state
  attribution.py          # tracks whether the mirrored basket outperforms/underperforms
  sources/leopold.py       # EDGAR 13F fetch + CUSIP->ticker resolution
  tagger.py                 # cached LLM catalyst classifier (secondary signal, not sizing)
  mcp/robinhood_client.py    # broker client (mock for tests, real MCP-backed for live)
live_broker/
  run_live.py             # standalone live runner — dry-run by default, --execute for real orders
tests/                   # 22 test files, real assertions against safety/execution/reconciler logic
```

## Run it

`config.py` (with account-specific defaults) is intentionally excluded from this repo — copy
`config.example.py` to `config.py` and fill in your own account details to run it live.

```bash
# Tests (no network, no real credentials needed)
PYTHONPATH=. python3 -m pytest tests/ -v

# Dry run against real EDGAR + real broker quotes (places zero orders)
cd live_broker && python3 -m live_broker.run_live

# Live (real orders) — requires Robinhood OAuth + a funded, ring-fenced account
cd live_broker && python3 -m live_broker.run_live --execute
```

## Status

Live and trading on a small (~$85-100) ring-fenced account. This is explicitly an instrumented
research position, not a claim of edge — the bet is concentrated and the point is to measure
whether "mirror what a specific fund discloses" beats a naive benchmark, with real execution
plumbing proven out along the way.

**Not financial advice.**
