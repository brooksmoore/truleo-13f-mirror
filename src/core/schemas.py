"""Mirror-basket schemas (dataclasses, no EV/auditor). Per spec Sections 1,4,9,11.

Own types: Position (side-tagged), CatalystTag, OrderIntent, RebalancePlan, etc.
Imports ONLY the pure stateless validate_execution_safety from the main agent's hood_agent_1
to prevent logic drift on safety veto (spread/halt/liquidity). All else bot-local.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, asdict, field
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any, Literal, Optional

# --- Import shared stateless safety (DO NOT reimplement or drift) ---
import sys
import warnings
_HOOD_SRC = Path(__file__).resolve().parents[3] / "hood_agent_1" / "src"
# APPEND (not insert-0): borrowing hood_agent_1's `core.schemas` must NOT shadow top-level packages
# that live in site-packages (e.g. the official `mcp` SDK used by the live broker transport).
# Appending keeps the borrow resolvable while site-packages win for any name they also define.
if str(_HOOD_SRC) not in sys.path:
    sys.path.append(str(_HOOD_SRC))
try:
    from core.schemas import validate_execution_safety, ExecutionSafetyResult  # type: ignore
    _SAFETY_SOURCE = "shared"
except Exception as e:
    # Fallback: define locally if import fails (tests in isolation); MUST match exactly.
    # Visible warning so drift risk is not silent (per M2).
    warnings.warn(f"Using local fallback validate_execution_safety (shared from hood_agent_1 not found: {e})", stacklevel=2)
    _SAFETY_SOURCE = "fallback"
    @dataclass
    class ExecutionSafetyResult:
        ok: bool
        reason: str
        spread_pct: Optional[float] = None
        pct_of_adv: Optional[float] = None

    def validate_execution_safety(
        bid: float,
        ask: float,
        avg_daily_volume: float,
        order_size_shares: float,
        is_halted: bool,
        max_allowed_spread_pct: float = 0.02,
        max_pct_of_adv: float = 0.05,
    ) -> ExecutionSafetyResult:
        if is_halted:
            return ExecutionSafetyResult(ok=False, reason="halted")
        if bid <= 0 or ask <= 0:
            return ExecutionSafetyResult(ok=False, reason="no_two_sided_market")
        mid = (ask + bid) / 2.0
        spread_pct = (ask - bid) / mid
        if spread_pct > max_allowed_spread_pct:
            return ExecutionSafetyResult(
                ok=False, reason=f"spread_too_wide_{spread_pct:.3f}", spread_pct=spread_pct
            )
        if avg_daily_volume <= 0 or (order_size_shares / avg_daily_volume) > max_pct_of_adv:
            pct = (order_size_shares / avg_daily_volume) if avg_daily_volume > 0 else 1.0
            return ExecutionSafetyResult(
                ok=False, reason="order_too_large_vs_liquidity", pct_of_adv=pct
            )
        return ExecutionSafetyResult(ok=True, reason="ok", spread_pct=spread_pct, pct_of_adv=order_size_shares / avg_daily_volume if avg_daily_volume > 0 else 0)

# Exposed for tests (M2) to assert whether shared implementation was loaded vs silent fallback.
SAFETY_SOURCE = _SAFETY_SOURCE


class Side(str, Enum):
    LONG = "long"
    SHORT = "short"


class CatalystType(str, Enum):
    TOUTED = "touted"
    GOV_STAKE = "gov_stake"
    POLICY_TAILWIND = "policy_tailwind"
    LEGACY = "legacy"


@dataclass
class Position:
    """Source or current position. side from day one for future shorts."""
    ticker: str
    side: Side = Side.LONG
    source: Literal["trump", "leopold"] = "leopold"
    source_weight: float = 0.0  # raw (midpoint $ for Trump, exact $ for Leopold)
    target_weight: float = 0.0  # after normalize + cap + overlap in sleeve or combined
    current_qty: float = 0.0  # shares held (fractional ok)


@dataclass
class CatalystTag:
    """Output of the (only) LLM call. Cached forever per ticker + context hash."""
    ticker: str
    catalyst: bool
    type: Optional[CatalystType] = None
    reason: str = ""
    source_url: str = ""
    confidence: float = 0.0
    ts: str = ""  # ISO
    context_hash: Optional[str] = None  # to re-tag only on material change


@dataclass
class OrderIntent:
    """Minimal rebalance order from reconciler."""
    ticker: str
    side: Side
    signed_qty: float  # + buy, - sell (fractional)
    reason: str  # "new_from_trump", "source_exit_leopold", "drift_rebalance", ...
    target_weight: float = 0.0
    current_weight: float = 0.0


@dataclass
class SleeveTarget:
    """Per-sleeve target after weighting/capping."""
    sleeve: Literal["trump", "leopold"]
    total_alloc: float  # 0.5
    positions: list[Position]  # top-N, target_weight set


@dataclass
class RebalancePlan:
    """Output of Tier-1 reconciler (pure)."""
    targets: list[Position]  # unified, capped, overlap-resolved target weights (sum ~1.0)
    orders: list[OrderIntent]  # minimal diff past drift band
    trump_rejected: list[dict]  # for attribution shadow basket: the ~40 catalyst=False
    notes: list[str] = field(default_factory=list)
    asof: str = ""
    conflicts: list[dict] = field(default_factory=list)  # e.g. long/short same ticker across sleeves (M6)


def make_position(ticker: str, **kw) -> Position:
    return Position(ticker=ticker, **kw)


if __name__ == "__main__":
    print("Safety import OK:", validate_execution_safety(10.0, 10.2, 500000, 100, False))
    from config import CFG
    print("Config topN:", CFG.top_n_per_sleeve, "cap:", CFG.per_name_cap)
