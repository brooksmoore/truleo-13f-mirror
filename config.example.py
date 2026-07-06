"""ALL tunables centralized for the mirror-basket agent. No magic numbers elsewhere.

Per spec §13: logic modules import from here.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal


@dataclass(frozen=True)
class Config:
    # Capital & sleeves (50/50)
    sleeve_trump: float = 0.50
    sleeve_leopold: float = 0.50

    # Concentration (top-N before weighting for economic coherence at small cap)
    top_n_per_sleeve: int = 10
    min_position_floor_pct: float = 0.015  # 1.5%
    min_position_floor_usd: float = 1.0

    # Caps (ruin awareness)
    per_name_cap: float = 0.15  # 15% of mirror sleeve
    # per-sleeve 50% is implicit in split; allow ±5% drift before force? (handled in orchestrator)

    # Rebalance discipline
    drift_band_pct: float = 0.01  # 1.0% of sleeve
    drift_band_usd: float = 10.0  # spec §11 "> $position_min"; 10 chosen > floor_usd=1 for practicality (known minor divergence, documented)

    # Shorts (deferred)
    enable_shorts: bool = False

    # Budget (mostly decorative given pennies cost; still wire the breaker)
    max_budget_usd_per_day: float = 1.0
    degrade_at_frac: float = 0.80

    # Data sources
    edgar_cik_leopold: str = "0002045724"
    # Trump: prefer aggregator for trigger, always verify vs official OGE filing
    trump_aggregator_url: str = "https://api.speculator.io/trump/holdings"  # placeholder
    oge_disclosure_search: str = "https://extapps2.oge.gov/201/Presiden.nsf/PAS+Index"

    # LLM (only Haiku, temp=0, cached)
    haiku_model: str = "claude-3-haiku-20240307"
    tagger_temperature: float = 0.0
    tagger_max_tokens: int = 400

    # Execution
    fractional_min_notional: float = 1.0
    robinhood_paper_starting_cash: float = 10000.0  # fallback for mock / first-run when no real equity snapshot

    # State roots (PL-7: single configured root; kill, budget, logs, data all derive; no CWD literals)
    data_dir: str = "data"
    logs_dir: str = "logs"

    # Live data flags (P4: only Leopold EDGAR for now; default off for deterministic tests/demo)
    use_live_edgar: bool = False
    # For attribution P2, can share or separate; use same for simplicity
    use_live_prices: bool = False
    # Part A: real Trump aggregator + official verify
    use_live_trump: bool = False
    # Part B: real Haiku tagger
    use_live_tagger: bool = False
    # Phase 2: real broker (MUST stay False until sandbox smoke + overseer signoff; paper path byte-identical)
    use_live_broker: bool = False

    # 2026-06-16: Trump sleeve dropped (signal is a diffuse small-cap/SMA sweep, not the concentrated
    # growth thesis; no free clean-ticker source). When True, run_cycle skips the Trump branch ENTIRELY
    # (no fetch, no fixture, no carry, no orders) — this is the real disable, distinct from use_live_trump.
    # SAFETY: with use_live_trump=False the Trump source serves a FIXTURE as "verified"; without this flag a
    # live-broker run would place real orders for fixture tickers. Default False so the retained Trump test
    # corpus stays byte-identical; the canonical live config is leopold_only_config() below.
    disable_trump_sleeve: bool = False

    # Skip post-trade attribution analytics in the cycle (live runner sets this — attribution would
    # re-quote dozens of historical tickers via the broker each cycle, adding latency/flakiness for no
    # trading value). Default False so paper/demo + tests keep computing it.
    skip_attribution: bool = False

    # Phase 2 — agentic Robinhood account (agentic_allowed=true; NEVER use the default margin account)
    robinhood_agentic_account_number: str = "YOUR_AGENTIC_ACCOUNT_NUMBER"

    # Phase 2 (PL-3) order handling + sizing safety (all in config, no magic)
    order_poll_max_attempts: int = 10
    order_poll_interval_sec: float = 3.0
    sizing_cash_buffer_pct: float = 0.02  # buffer when sizing target_dollars by last (to cover ask)

    # Health / polling
    trump_poll_interval_sec: int = 3600  # 1h lightweight check
    leopold_poll_interval_sec: int = 86400  # daily ok (13F quarterly)
    quiet_threshold_days_trump: int = 90  # alert if no filing in ~quarter
    quiet_threshold_days_13f: int = 60

    # Attribution
    attribution_report_days: int = 30

    # Corporate actions
    ca_tolerance_days: int = 5

    # Order reason threshold (was magic 0.05 in reconciler)
    reason_drift_threshold: float = 0.05


CFG = Config()


def get_config() -> Config:
    return CFG


# For easy override in tests
def override(**kw) -> Config:
    return Config(**{**CFG.__dict__, **kw})


def leopold_only_config(**kw) -> Config:
    """Canonical post-2026-06-16 run profile: Trump sleeve silenced, 100% Leopold.

    Use this (not the frozen CFG defaults) for any real/paper run now that the Trump
    sleeve is dropped. Extra kwargs (e.g. use_live_broker=True, use_live_edgar=True)
    layer on top. Keeping it as an override rather than changing CFG defaults preserves
    the retained Trump test corpus.
    """
    base = dict(
        disable_trump_sleeve=True,
        sleeve_trump=0.0,
        sleeve_leopold=1.0,
        per_name_cap=1.0,  # 2026-06-16: caps abandoned — mimic Leopold's top-10 weights directly (owner decision)
    )
    base.update(kw)
    return override(**base)
