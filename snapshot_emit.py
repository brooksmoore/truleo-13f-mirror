"""Emit the umbrella canonical snapshot from truleo's ownership ledger + broker read."""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime
from pathlib import Path
from typing import Callable, Optional

from umbrella_core.emit import (
    AccountInfo,
    CapitalInfo,
    ComputeInfo,
    HealthInfo,
    IdentityInfo,
    LifecycleInfo,
    PositionInfo,
    ReconciliationInfo,
    Snapshot,
    TimingInfo,
    snapshot_to_dict,
    write_snapshot_atomic,
)
from umbrella_core.snapshot import validate_snapshot

from config import Config
from src.core.storage import GraveyardDB
from src.ownership_ledger import OwnershipLedger

log = logging.getLogger(__name__)

DRIFT_TOLERANCE_USD = 1.0
LIVENESS_INTERVAL_SEC = 345600  # 4 days — weekday cron survives long weekends


def _load_filing_id(data_dir: Path) -> str | None:
    cache_path = data_dir / "leopold_13f_cache.json"
    if not cache_path.exists():
        return None
    try:
        payload = json.loads(cache_path.read_text(encoding="utf-8"))
        accession = payload.get("last_accession")
        return str(accession) if accession else None
    except (OSError, json.JSONDecodeError, TypeError):
        return None


def _last_fill_at(graveyard: GraveyardDB | None) -> str | None:
    if graveyard is None:
        return None
    try:
        events = graveyard.get_events(limit=500)
    except Exception:
        return None
    for event in events:
        if event.get("action") == "fill" and event.get("outcome") == "filled":
            ts = event.get("timestamp")
            return str(ts) if ts else None
    return None


def build_truleo_snapshot(
    ledger: OwnershipLedger,
    *,
    price_fn: Callable[[str], Optional[float]],
    account_total: float,
    cfg: Config,
    data_dir: Path,
    killed: bool,
    graveyard: GraveyardDB | None = None,
    cycle_at: datetime | None = None,
    warnings: list[str] | None = None,
    quote_degraded: bool = False,
    filing_id: str | None = None,
) -> Snapshot:
    """Map truleo's ledger + broker account read to the umbrella canonical snapshot."""
    now = cycle_at or datetime.now(UTC)
    own_nav = ledger.own_nav(price_fn)
    cash = ledger.cash_usd()
    invested = max(0.0, own_nav - cash)

    positions: list[PositionInfo] = []
    for symbol, qty in sorted(ledger.get_all_owned().items()):
        if qty <= 0:
            continue
        mark = price_fn(symbol)
        market_value = qty * mark if mark and mark > 0 else None
        weight = (market_value / own_nav) if market_value is not None and own_nav > 0 else None
        positions.append(
            PositionInfo(
                symbol=symbol,
                qty=qty,
                avg_cost=None,
                mark=mark,
                market_value=market_value,
                unrealized_pnl=None,
                weight=weight,
            )
        )

    unassigned = round(account_total - own_nav, 2)
    # Degraded quotes undercount own_nav (the ledger omits unquotable positions), so
    # account_total - own_nav LOOKS like foreign capital. Don't evaluate drift on a
    # tenant_sum we can't trust — otherwise truleo's flaky MCP bridge cries "foreign
    # capital" on every quote miss and the operator learns to ignore the one signal that
    # catches a real second bot on the shared account. (Auditor fix 2026-07-01.)
    drift_flag = (abs(unassigned) > DRIFT_TOLERANCE_USD) and not quote_degraded

    run_warnings = list(warnings or [])
    if quote_degraded:
        run_warnings.append(
            "one or more owned positions missing live quotes; reconciliation not evaluated this cycle"
        )
    if drift_flag:
        run_warnings.append(
            f"reconciliation drift: unassigned_house=${unassigned:.2f} (foreign/orphan capital)"
        )

    if killed:
        overall = "down"
    elif quote_degraded or drift_flag:
        overall = "degraded"
    else:
        overall = "ok"

    spend_cap = float(cfg.max_budget_usd_per_day)
    resolved_filing = filing_id if filing_id is not None else _load_filing_id(data_dir)

    return Snapshot(
        schema_version="1.0",
        identity=IdentityInfo(
            bot_id="truleo",
            display_name="Leopold 13F Mirror",
            membrane="broker_tenancy",
            account=AccountInfo(broker="robinhood", number=cfg.robinhood_agentic_account_number),
            asset_classes=["equity"],
            strategy="Quarterly 13F mirror (Situational Awareness LP)",
        ),
        lifecycle=LifecycleInfo(
            stage="live",
            mode="live",
            live_gate="armed",
            killed=killed,
            cadence="cron",
            expected_update_interval_sec=LIVENESS_INTERVAL_SEC,
        ),
        timing=TimingInfo(
            generated_at=now.isoformat(),
            last_cycle_at=now.isoformat(),
            last_fill_at=_last_fill_at(graveyard),
        ),
        capital=CapitalInfo(
            base_currency="USD",
            own_nav=round(own_nav, 2),
            cash=round(cash, 2),
            invested=round(invested, 2),
            budget_allocation=ledger.budget_usd(),
            day_pnl=None,
            total_pnl=None,
        ),
        positions=positions,
        compute=ComputeInfo(
            llm_spend_today_usd=0.0,
            llm_budget_usd=spend_cap,
            budget_remaining_usd=spend_cap,
            calls_today=0,
            breaker_tripped=False,
        ),
        health=HealthInfo(
            overall=overall,  # type: ignore[arg-type]
            sources={
                "broker": "ok",
                "edgar": "ok",
                "ledger": "ok" if ledger.is_seeded() else "degraded",
            },  # type: ignore[arg-type]
            warnings=run_warnings,
        ),
        reconciliation=ReconciliationInfo(
            account_total=round(account_total, 2),
            tenant_sum=round(own_nav, 2),
            unassigned_house=unassigned,
            drift_flag=drift_flag,
        ),
        extra={
            "filing_id": resolved_filing,
            "cadence_note": "quarterly mirror; idem-skips intra-quarter by design",
        },
    )


def emit_truleo_snapshot(
    out_path: Path,
    *,
    ledger: OwnershipLedger,
    price_fn: Callable[[str], Optional[float]],
    account_total: float,
    cfg: Config,
    data_dir: Path,
    killed: bool,
    graveyard: GraveyardDB | None = None,
    cycle_at: datetime | None = None,
    warnings: list[str] | None = None,
    quote_degraded: bool = False,
    filing_id: str | None = None,
) -> bool:
    """Validate and atomically write state.json. Keeps prior file on validation failure."""
    snapshot = build_truleo_snapshot(
        ledger,
        price_fn=price_fn,
        account_total=account_total,
        cfg=cfg,
        data_dir=data_dir,
        killed=killed,
        graveyard=graveyard,
        cycle_at=cycle_at,
        warnings=warnings,
        quote_degraded=quote_degraded,
        filing_id=filing_id,
    )
    payload = snapshot_to_dict(snapshot)
    errors = validate_snapshot(payload)
    if errors:
        log.error(
            "umbrella snapshot validation failed; keeping prior %s: %s",
            out_path,
            errors,
        )
        return False
    write_snapshot_atomic(out_path, payload)
    log.info("umbrella snapshot written to %s", out_path)
    return True


def price_fn_from_client(client: object) -> Callable[[str], Optional[float]]:
    """Build a quote lookup compatible with OwnershipLedger.own_nav()."""

    def _lookup(ticker: str) -> Optional[float]:
        try:
            quote = client.get_quote(ticker)  # type: ignore[attr-defined]
            last = getattr(quote, "last", 0.0) or 0.0
            if last > 0.01:
                return float(last)
            ask = getattr(quote, "ask", 0.0) or 0.0
            bid = getattr(quote, "bid", 0.0) or 0.0
            if ask and bid:
                return float((ask + bid) / 2.0)
        except Exception:
            return None
        return None

    return _lookup


def quote_degraded_for_ledger(
    ledger: OwnershipLedger,
    price_fn: Callable[[str], Optional[float]],
) -> bool:
    """True when any owned position lacks a usable live quote."""
    for ticker, shares in ledger.get_all_owned().items():
        if shares <= 0:
            continue
        px = price_fn(ticker)
        if not px or px <= 0:
            return True
    return False