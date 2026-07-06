"""Auditor-owned gate tests for umbrella canonical snapshot emission.

Grok creates this file; Claude owns assertions thereafter.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import patch

import pytest

from config import Config, leopold_only_config
from snapshot_emit import build_truleo_snapshot, emit_truleo_snapshot
from src.core.storage import GraveyardDB
from src.ownership_ledger import OwnershipLedger
from umbrella_core.emit import snapshot_to_dict
from umbrella_core.snapshot import validate_snapshot

_TS = datetime(2026, 7, 1, 14, 0, 0, tzinfo=UTC)
_PRICES = {"NVDA": 134.0, "SPY": 548.0}


def _price_fn(ticker: str) -> float | None:
    return _PRICES.get(ticker.upper())


def _seed_ledger(tmp_path: Path) -> OwnershipLedger:
    ledger = OwnershipLedger(tmp_path)
    ledger._data = {
        "seeded": True,
        "seed_ts": _TS.isoformat(),
        "budget_usd": 100.0,
        "cash_usd": 10.0,
        "positions": {"NVDA": 0.5, "SPY": 0.1},
    }
    ledger._save()
    return ledger


def _cfg() -> Config:
    return leopold_only_config(data_dir="data", logs_dir="logs")


def test_emitted_snapshot_validates(tmp_path: Path) -> None:
    ledger = _seed_ledger(tmp_path)
    own_nav = ledger.own_nav(_price_fn)
    snap = build_truleo_snapshot(
        ledger,
        price_fn=_price_fn,
        account_total=own_nav,
        cfg=_cfg(),
        data_dir=tmp_path,
        killed=False,
        cycle_at=_TS,
    )
    errors = validate_snapshot(snapshot_to_dict(snap))
    assert errors == [], "\n".join(errors)


def test_capital_matches_ledger(tmp_path: Path) -> None:
    ledger = _seed_ledger(tmp_path)
    own_nav = ledger.own_nav(_price_fn)
    snap = build_truleo_snapshot(
        ledger,
        price_fn=_price_fn,
        account_total=own_nav,
        cfg=_cfg(),
        data_dir=tmp_path,
        killed=False,
        cycle_at=_TS,
    )
    assert snap.capital.own_nav == pytest.approx(own_nav)
    assert snap.capital.cash == pytest.approx(ledger.cash_usd())
    assert snap.capital.invested == pytest.approx(own_nav - ledger.cash_usd())


def test_positions_match_ledger(tmp_path: Path) -> None:
    ledger = _seed_ledger(tmp_path)
    own_nav = ledger.own_nav(_price_fn)
    snap = build_truleo_snapshot(
        ledger,
        price_fn=_price_fn,
        account_total=own_nav,
        cfg=_cfg(),
        data_dir=tmp_path,
        killed=False,
        cycle_at=_TS,
    )
    assert {p.symbol for p in snap.positions} == set(ledger.get_all_owned().keys())
    for pos in snap.positions:
        assert pos.qty == pytest.approx(ledger.get_owned_shares(pos.symbol))


def test_reconciliation_no_drift_when_sole_tenant(tmp_path: Path) -> None:
    ledger = _seed_ledger(tmp_path)
    own_nav = ledger.own_nav(_price_fn)
    snap = build_truleo_snapshot(
        ledger,
        price_fn=_price_fn,
        account_total=own_nav,
        cfg=_cfg(),
        data_dir=tmp_path,
        killed=False,
        cycle_at=_TS,
    )
    assert snap.reconciliation is not None
    assert snap.reconciliation.drift_flag is False


def test_reconciliation_drift_when_foreign_position(tmp_path: Path) -> None:
    ledger = _seed_ledger(tmp_path)
    own_nav = ledger.own_nav(_price_fn)
    snap = build_truleo_snapshot(
        ledger,
        price_fn=_price_fn,
        account_total=own_nav + 50.0,
        cfg=_cfg(),
        data_dir=tmp_path,
        killed=False,
        cycle_at=_TS,
    )
    assert snap.reconciliation is not None
    assert snap.reconciliation.drift_flag is True
    assert snap.reconciliation.unassigned_house == pytest.approx(50.0)


def test_validation_failure_does_not_overwrite_good_file(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    ledger = _seed_ledger(tmp_path)
    own_nav = ledger.own_nav(_price_fn)
    out_path = tmp_path / "state.json"
    graveyard = GraveyardDB(tmp_path)

    assert emit_truleo_snapshot(
        out_path,
        ledger=ledger,
        price_fn=_price_fn,
        account_total=own_nav,
        graveyard=graveyard,
        cfg=_cfg(),
        data_dir=tmp_path,
        killed=False,
        cycle_at=_TS,
    )
    good_bytes = out_path.read_bytes()

    broken = build_truleo_snapshot(
        ledger,
        price_fn=_price_fn,
        account_total=own_nav,
        cfg=_cfg(),
        data_dir=tmp_path,
        killed=False,
        cycle_at=_TS,
    )
    broken.health.overall = "not-a-real-status"  # type: ignore[misc]

    with patch("snapshot_emit.build_truleo_snapshot", return_value=broken):
        assert not emit_truleo_snapshot(
            out_path,
            ledger=ledger,
            price_fn=_price_fn,
            account_total=own_nav,
            graveyard=graveyard,
            cfg=_cfg(),
            data_dir=tmp_path,
            killed=False,
            cycle_at=_TS,
        )

    assert out_path.read_bytes() == good_bytes
    assert any("validation failed" in r.message.lower() for r in caplog.records)

def test_reconciliation_drift_suppressed_when_quotes_degraded(tmp_path: Path) -> None:
    """AUDITOR (2026-07-01): a quote miss drops own_nav (the ledger omits unquotable
    positions), so account_total - own_nav LOOKS like foreign capital. With truleo's
    flaky MCP bridge this false-alarms the reconciliation checksum — the one signal whose
    job is to catch a REAL second bot on the shared account. When quotes are degraded we
    cannot trust tenant_sum, so drift MUST NOT be flagged (else you learn to ignore the
    real signal)."""
    ledger = _seed_ledger(tmp_path)
    own_nav = ledger.own_nav(_price_fn)
    snap = build_truleo_snapshot(
        ledger,
        price_fn=_price_fn,
        account_total=own_nav + 50.0,  # looks like +$50 foreign, but it's a quote miss
        cfg=_cfg(),
        data_dir=tmp_path,
        killed=False,
        cycle_at=_TS,
        quote_degraded=True,
    )
    assert snap.reconciliation is not None
    assert snap.reconciliation.drift_flag is False
