"""MIRROR EXECUTOR — own instance (spec §2, §14).

Fractional-aware weight -> shares translation.
Uses shared validate_execution_safety (imported).
Idempotency (per trigger or per-order key).
Re-checks caps (defense in depth, though reconciler already did).
Routes to PersistentLog + Graveyard (local).
Kill switch + lightweight budget.
Sells auto on source exit (via plan).
Fractional: market orders, regular hours assumed (no limit/ext for frac per RH realities).
Thin names: skip if not frac-eligible or < min notional (log to graveyard "fractional_ineligible_subshare").

Minimal diff already produced by reconciler; executor just executes the intents + safety.
"""

from __future__ import annotations

import json
import time
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Optional

from .core.schemas import (
    validate_execution_safety,
    OrderIntent,
    Side,
    ExecutionSafetyResult,
)
from .core.storage import GraveyardDB, PersistentLog, make_log_entry
from .mcp.robinhood_client import MockRobinhoodClient, Quote, OrderResult, Position as BrokerPosition
from .ownership_ledger import OwnershipLedger
import sys
from pathlib import Path
_p = Path(__file__).resolve().parents[1]
if str(_p) not in sys.path:
    sys.path.insert(0, str(_p))
from config import CFG


class MirrorExecutor:
    def __init__(
        self,
        client: Optional[MockRobinhoodClient] = None,
        graveyard: Optional[GraveyardDB] = None,
        plog: Optional[PersistentLog] = None,
        data_dir: Optional[Path] = None,
        is_killed: Optional[Callable[[], bool]] = None,
        sleeve_usd: Optional[float] = None,
        place_spacing_sec: float = 0.0,
        ledger: Optional[OwnershipLedger] = None,
    ):
        self.place_spacing_sec = place_spacing_sec
        self.client = client or MockRobinhoodClient(starting_cash=CFG.robinhood_paper_starting_cash)
        self.data_dir = data_dir or Path(CFG.data_dir)
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.graveyard = graveyard or GraveyardDB(self.data_dir)
        self.plog = plog or PersistentLog(Path(CFG.logs_dir))
        # PL-7/2: kill path derives from configured data root (unambiguous, not CWD); fail-safe default below
        kill_path = self.data_dir / "KILL_SWITCH"
        self.is_killed = is_killed or (lambda: kill_path.exists())
        self.sleeve_usd = sleeve_usd or CFG.robinhood_paper_starting_cash
        self.ledger = ledger or OwnershipLedger(self.data_dir)
        self._idemp_path = self.data_dir / "idempotency.json"
        self._idemp: set[str] = set()
        self._load_idemp()
        self._clean_cycles_path = self.data_dir / "clean_cycles.json"
        self._clean_cycles: int = 0
        self._load_clean_cycles()

        # budget light (for the Haiku calls upstream) — from configured logs root (PL-7)
        self.budget_path = Path(CFG.logs_dir) / "budget.json"
        self._spent_today = 0.0
        self._load_budget()

        # Phase 2 (PL-3 surgical): cache post-execution broker positions so next get_portfolio_snapshot reflects real broker state (not assumed fills from this plan)
        self._positions_cache: Optional[list[BrokerPosition]] = None

        # Ownership: per-cycle cache of truleo's own mark-to-market NAV (compounds; not the frozen seed budget).
        # Recomputed at the top of each execute_plan() call; translate_weight_to_shares falls back to computing
        # it fresh if called standalone (e.g. tests, or before any execute_plan call this process).
        self._cycle_own_nav: Optional[float] = None

    def _load_idemp(self):
        if self._idemp_path.exists():
            try:
                self._idemp = set(json.loads(self._idemp_path.read_text()))
            except Exception:
                self._idemp = set()

    def _save_idemp(self):
        self._idemp_path.write_text(json.dumps(list(self._idemp)))

    def _load_clean_cycles(self) -> None:
        if self._clean_cycles_path.exists():
            try:
                payload = json.loads(self._clean_cycles_path.read_text())
                self._clean_cycles = int(payload.get("clean_cycles_since_failure", 0))
            except Exception:
                self._clean_cycles = 0

    def _persist_clean_cycles(self) -> None:
        self._clean_cycles_path.write_text(
            json.dumps({
                "clean_cycles_since_failure": self._clean_cycles,
                "updated": datetime.now(timezone.utc).isoformat(),
            })
        )

    def clean_cycles_since_failure(self) -> int:
        return self._clean_cycles

    def reset_clean_cycles_on_graveyard_event(self, action: str) -> None:
        if action in {"silent_failure", "confirm_fill_failed", "order_accepted_no_fill"}:
            self._clean_cycles = 0
            self._persist_clean_cycles()

    def _record_clean_cycle(self, *, intended_qty: float, confirmed_qty: float) -> None:
        if abs(intended_qty - confirmed_qty) > 1e-4:
            self.graveyard.record_event(
                "silent_failure",
                outcome="mismatch",
                reject_reason="intended!=filled",
                meta={"intended": intended_qty, "confirmed": confirmed_qty},
            )
            self._clean_cycles = 0
        else:
            self._clean_cycles += 1
        self._persist_clean_cycles()

    def _load_budget(self):
        if self.budget_path.exists():
            try:
                d = json.loads(self.budget_path.read_text())
                if d.get("date") == str(datetime.now(timezone.utc).date()):
                    self._spent_today = d.get("spent", 0.0)
            except Exception:
                pass

    def _persist_budget(self):
        self.budget_path.parent.mkdir(parents=True, exist_ok=True)
        data = {
            "date": str(datetime.now(timezone.utc).date()),
            "spent": round(self._spent_today, 6),
            "updated": datetime.now(timezone.utc).isoformat(),
        }
        self.budget_path.write_text(json.dumps(data))

    def can_spend(self, est_cost: float = 0.01) -> bool:
        if self._spent_today + est_cost > CFG.max_budget_usd_per_day:
            return False
        return True

    def record_spend(self, cost: float):
        self._spent_today += cost
        self._persist_budget()

    def _killed(self) -> bool:
        """PL-2: on any error evaluating the kill check, treat as KILLED (fail SAFE, never fail open)."""
        try:
            return bool(self.is_killed())
        except Exception:
            # fail safe: error in safety control => halt (stand-down). Log if possible.
            try:
                self.graveyard.record_event(
                    action="kill_eval_error",
                    outcome="stand_down",
                    reject_reason="kill_check_raised_treated_as_killed",
                )
            except Exception:
                pass  # never let logging break the halt
            return True

    def _idemp_key(self, intent: OrderIntent, trigger_id: str = "", qty: Optional[float] = None) -> str:
        q = intent.signed_qty if qty is None else qty
        return f"{trigger_id}:{intent.ticker}:{intent.side}:{round(q, 6)}"

    def _compute_own_nav(self) -> float:
        """Truleo's own mark-to-market NAV (cash + current value of owned positions), re-quoted live.
        This is the compounding sizing base — NOT the frozen seed-time budget. Fails safe: a quote
        failure for an owned ticker omits that position's value (undercounts, never fabricates)."""
        if not self.ledger.is_seeded():
            return self.sleeve_usd

        def price_fn(tkr: str) -> Optional[float]:
            try:
                q = self.client.get_quote(tkr)
                if getattr(q, "last", 0.0) and q.last > 0.01:
                    return q.last
                if getattr(q, "ask", 0.0) and getattr(q, "bid", 0.0):
                    return (q.ask + q.bid) / 2.0
            except Exception:
                pass
            return None

        return self.ledger.own_nav(price_fn)

    def translate_weight_to_shares(self, target_weight: float, price: float, current_shares: float = 0.0) -> float:
        """weight (of whole mirror sleeve) -> target shares (frac), then diff qty.
        PL-3 (Phase 2): apply CFG sizing_cash_buffer_pct so last-price sizing doesn't overspend at ask (surgical addition for live broker path).
        Ownership: when ledger is seeded, size off truleo's own mark-to-market NAV (own_nav — cash +
        current value of owned positions) so foreign cash/positions never inflate order sizes, and
        truleo's book compounds as its own positions move (not frozen at the seed-time dollar figure).
        """
        if price <= 0:
            return 0.0
        from config import CFG
        buffer = getattr(CFG, "sizing_cash_buffer_pct", 0.0)
        if self.ledger.is_seeded():
            # Use the per-cycle cache (set at the top of execute_plan) when available; otherwise
            # compute fresh (standalone calls, e.g. tests, before any execute_plan this process).
            sizing_base = self._cycle_own_nav if self._cycle_own_nav is not None else self._compute_own_nav()
        else:
            sizing_base = self.sleeve_usd
        target_dollars = target_weight * sizing_base * (1.0 - buffer)
        target_shares = target_dollars / price
        delta = target_shares - current_shares
        return delta

    def execute_plan(self, plan_orders: list[OrderIntent], current_positions: list[BrokerPosition], trigger_id: str = "manual") -> list[OrderResult]:
        """Execute the minimal orders from reconciler. Returns list of results.
        PL-13: idemp disk writes batched once per plan (cycle); keys scoped with trigger so historical replays dedupe without unbounded growth in practice.
        Ownership: sell gate enforced via ledger (fail-closed); confirm-after-fill before ledger update.
        """
        results = []
        if self._killed():
            self.graveyard.record_event("killed", outcome="stand_down", reject_reason="kill_switch", meta={"trigger": trigger_id})
            return results

        idemp_blocked = bool(plan_orders and ":none" in trigger_id)
        if idemp_blocked:
            import logging

            logging.getLogger(__name__).error(
                "REFUSING idempotency for trigger_id=%s — :none fallback with %d orders would poison cache",
                trigger_id,
                len(plan_orders),
            )
            self.graveyard.record_event(
                "none_trigger_guard",
                outcome="idemp_blocked",
                reject_reason="trigger_id_contains_none",
                meta={"trigger": trigger_id, "order_count": len(plan_orders)},
            )

        # Ownership: snapshot truleo's own mark-to-market NAV once for this cycle (compounding sizing
        # base). Cached so every order in this plan sizes against the same NAV reading, and so
        # translate_weight_to_shares doesn't re-quote every owned ticker per order.
        self._cycle_own_nav = self._compute_own_nav() if self.ledger.is_seeded() else None

        # build current qty map (PL-8: honest BrokerPosition; tolerate dicts from snapshot for transition/compat)
        curr_qty: dict[str, float] = {}
        for p in current_positions or []:
            if isinstance(p, dict):
                tkr = p.get("ticker") or p.get("ticker")
                curr_qty[tkr] = float(p.get("shares", p.get("current_qty", 0.0)) or 0.0)
            else:
                tkr = getattr(p, "ticker", "")
                sh = getattr(p, "shares", getattr(p, "current_qty", 0.0))
                curr_qty[tkr] = float(sh or 0.0)

        keys_added_this_plan: set[str] = set()
        # pending_fills: collect (ticker, side, actual_filled, avg_fill_price) for ledger update after broker confirm-after-fill
        pending_fills: list[tuple[str, str, float, float]] = []
        cycle_intended_qty = 0.0
        cycle_confirmed_qty = 0.0
        cycle_had_placement = False

        for intent in plan_orders:
            if self._killed():
                break

            # get fresh quote for safety + sizing
            q = self.client.get_quote(intent.ticker)
            price = q.last or (q.ask + q.bid) / 2
            if price <= 0.01:
                self.graveyard.record_event("veto", ticker=intent.ticker, reject_reason="bad_price", meta={"q": asdict(q)})
                continue

            # compute delta shares from weight (if not already signed in intent)
            delta = intent.signed_qty
            if delta == 0.0:
                curr_s = curr_qty.get(intent.ticker, 0.0)
                delta = self.translate_weight_to_shares(intent.target_weight, price, curr_s)
            if abs(delta) < 1e-6:
                continue

            side = "buy" if delta > 0 else "sell"
            qty = abs(delta)

            # OWNERSHIP SELL GATE (fail-closed):
            # truleo may only sell shares its ledger records it bought.
            # If ledger is not seeded → no ownership data → zero sells this cycle.
            # If ledger is seeded but has no record of this ticker → skip (foreign position — leave it alone).
            if side == "sell":
                if not self.ledger.is_seeded():
                    self.graveyard.record_event(
                        "ownership_sell_blocked", ticker=intent.ticker, outcome="skipped",
                        reject_reason="ledger_not_seeded",
                    )
                    continue
                owned_shares = self.ledger.get_owned_shares(intent.ticker)
                if owned_shares <= 0:
                    self.graveyard.record_event(
                        "ownership_sell_blocked", ticker=intent.ticker, outcome="skipped",
                        reject_reason="no_ledger_record",
                        meta={"reason": intent.reason},
                    )
                    continue
                # cap sell qty at what the ledger says truleo owns (never sell more than we bought)
                qty = min(qty, owned_shares)
                delta = -qty

            # AUDITOR FIX (2026-07-06): idemp key must be built from the REAL, final delta —
            # not intent.signed_qty, which the reconciler always sets to a 0.0 placeholder for
            # weight-based drift/rebalance orders (see reconciler.py "placeholder; executor
            # translates w->shares"). Building the key before delta was known meant every
            # drift order for a ticker collapsed to the same key ("...:TICKER:side:0.0")
            # regardless of size, so the FIRST order ever placed for a ticker under a given
            # 13F accession silently blocked every later rebalance of that ticker for the rest
            # of the ~45-day filing cycle. Confirmed live: every rebalance since the 2026-06-17
            # go-live had been dead-on-arrival ("Executor results: []"), misread as floor-skip.
            key = self._idemp_key(intent, trigger_id, qty=delta)
            if key in self._idemp:
                self.graveyard.record_event("idemp_skip", ticker=intent.ticker, outcome="duplicate", meta={"key": key})
                continue

            # fractional min
            notional = qty * price
            if notional < CFG.fractional_min_notional:
                self.graveyard.record_event(
                    "skip", ticker=intent.ticker, outcome="fractional_ineligible_subshare",
                    reject_reason=f"notional_{notional:.2f}<min", meta={"price": price}
                )
                self.plog.append(make_log_entry("skip_subshare", intent.ticker, notional=notional))
                keys_added_this_plan.add(key)
                continue

            # safety veto (hard, even though reconciler is clean)
            safety: ExecutionSafetyResult = validate_execution_safety(
                q.bid, q.ask, q.avg_daily_volume, qty, q.is_halted
            )
            if not safety.ok:
                self.graveyard.record_event("veto", ticker=intent.ticker, outcome="safety", reject_reason=safety.reason, meta=asdict(safety))
                self.plog.append(make_log_entry("safety_veto", intent.ticker, reason=safety.reason))
                keys_added_this_plan.add(key)
                continue

            # place (market, frac ok in mock)
            # Phase 2 surgical: record *real* res.filled_shares from broker (never fall back to intended delta).
            # If broker confirms 0 fills (poll timeout, not-yet-filled accepted) we record 0 + log; drift corrects next cycle.
            if self.place_spacing_sec > 0:
                import time as _t; _t.sleep(self.place_spacing_sec)  # pace placements to avoid broker 429 throttling
            res: OrderResult = self.client.place_market_order(intent.ticker, side, delta)
            # AUDITOR FIX (PL3-BUG-1): use res.filled_shares directly — no fallback to abs(delta).
            # The `else abs(delta)` fallback was fabricating fills when success=True but filled_shares=0 (poll timeout).
            actual_filled = res.filled_shares  # real qty from broker; 0 = no confirmed fill (partial, timeout, or pending)
            if res.success:
                cycle_had_placement = True
                cycle_intended_qty += abs(delta)
                cycle_confirmed_qty += actual_filled
            if res.success and actual_filled > 0:
                self.plog.append(make_log_entry("fill", intent.ticker, side=side, qty=actual_filled, px=res.avg_fill_price, order_id=res.order_id))
                self.graveyard.record_event("fill", ticker=intent.ticker, signed_qty=actual_filled, outcome="filled", meta={"px": res.avg_fill_price, "intended_delta": delta})
                # queue for ledger update; verify against broker re-read below (confirm-after-fill)
                pending_fills.append((intent.ticker, side, actual_filled, res.avg_fill_price))
            elif res.success and actual_filled == 0:
                # Accepted but no confirmed fill (poll timeout or truly 0 fill) — do NOT fabricate; log for audit trail
                self.graveyard.record_event("order_accepted_no_fill", ticker=intent.ticker, outcome="no_fill_confirmed", reject_reason=res.reason, meta={"intended_delta": delta, "order_id": res.order_id})
                self.reset_clean_cycles_on_graveyard_event("order_accepted_no_fill")
            else:
                self.graveyard.record_event("fail", ticker=intent.ticker, outcome="place_fail", reject_reason=res.reason)
            results.append(res)
            # IDEMP (PL13-BUG): only dedupe orders that actually reached the broker (filled or accepted).
            # A hard place FAILURE (broker error, no order created) must stay RETRYABLE — otherwise a single
            # transient failure permanently poisons the idemp cache and blocks the real order on every re-run
            # (observed 2026-06-17: first --execute failed env-transient, recorded keys, blocked all 9 next run).
            if res.success:
                keys_added_this_plan.add(key)

        # PL-13: batch save once per cycle/plan (not per order/skip)
        if keys_added_this_plan and not idemp_blocked:
            self._idemp |= keys_added_this_plan
            self._save_idemp()
            # cheap prune: if set grows large, retain recent-ish (keys contain trigger so scoped; list slice arbitrary but bounds)
            if len(self._idemp) > 5000:
                self._idemp = set(list(self._idemp)[-3000:])
                self._save_idemp()

        # Phase 2 (PL-3 surgical): re-read from broker (real client) and cache so *next* snapshot/reconcile uses actual positions, not any assumed fills from this plan's deltas.
        # (For mock path this is cheap no-op relative to previous behavior; live path now sees truth.)
        try:
            self._positions_cache = self.client.get_positions()
        except Exception:
            self._positions_cache = None  # fail-safe: next snapshot will re-fetch

        # CONFIRM-AFTER-FILL: update ownership ledger only for fills the broker actually confirms.
        # Buys: verify the position appears in the re-read broker state before crediting ledger.
        # Sells: trust the fill (actual_filled > 0 already confirmed by poll); deduct from ledger.
        if pending_fills and self.ledger.is_seeded():
            broker_pos_map: dict[str, float] = {}
            if self._positions_cache:
                for p in self._positions_cache:
                    tkr = str(getattr(p, "ticker", "") or "")
                    sh = float(getattr(p, "shares", 0.0) or 0.0)
                    if tkr and sh > 0:
                        broker_pos_map[tkr] = sh
            for (tkr, fill_side, filled_qty, fill_px) in pending_fills:
                if fill_side == "buy":
                    if broker_pos_map.get(tkr, 0.0) > 0:
                        self.ledger.record_buy(tkr, filled_qty, price=fill_px)
                    else:
                        self.graveyard.record_event(
                            "confirm_fill_failed", ticker=tkr, outcome="ledger_not_updated",
                            reject_reason="position_not_in_broker_after_fill",
                            meta={"filled_qty": filled_qty},
                        )
                else:  # sell — fill already confirmed; deduct from ledger, return cash
                    self.ledger.record_sell(tkr, filled_qty, price=fill_px)

        # Reset cycle cache so a stale NAV never leaks into a later, unrelated call this process.
        self._cycle_own_nav = None

        if cycle_had_placement:
            self._record_clean_cycle(
                intended_qty=cycle_intended_qty,
                confirmed_qty=cycle_confirmed_qty,
            )
        elif plan_orders:
            self._record_clean_cycle(intended_qty=0.0, confirmed_qty=0.0)

        return results

    def get_portfolio_snapshot(self) -> dict:
        """One snapshot per call (PL-14). Includes raw_positions (broker objs) so callers (orchestrator) can use the *same* fetch for reconcile weights + execute current book (no 2nd get_positions in cycle).
        Phase 2 (PL-3 surgical): if _positions_cache set (post-execute re-read), use it so snapshot reflects broker reality immediately; otherwise fetch+cache.
        """
        if self._positions_cache is not None:
            poss = self._positions_cache
        else:
            poss = self.client.get_positions()
            self._positions_cache = poss
        # CRITICAL (PL3-BUG-2): the live get_equity_positions endpoint does NOT return market_value
        # (robinhood_client sets it to 0.0). If we trusted that, total_equity would collapse to leftover
        # cash on cycle 2 → every target sizes to ~0 shares → executor would SELL the whole book (liquidation).
        # Value any unpriced position from a FRESH quote (shares × last). Mock path already sets market_value,
        # so this only re-quotes the live case. Fail-safe: a position we cannot price is treated as 0 (it will
        # not inflate equity; reconcile self-corrects next cycle) — never fabricate.
        for p in poss:
            shares = getattr(p, "shares", 0.0) or 0.0
            if (not p.market_value or p.market_value <= 0.0) and shares > 0:
                try:
                    q = self.client.get_quote(p.ticker)
                    px = q.last if getattr(q, "last", 0.0) and q.last > 0.01 else (
                        (q.ask + q.bid) / 2.0 if getattr(q, "ask", 0.0) and getattr(q, "bid", 0.0) else 0.0)
                    if px and px > 0:
                        p.market_value = shares * px
                except Exception:
                    pass  # leave at 0.0 (fail-safe; do not fabricate a value)
        invested = sum((p.market_value or 0.0) for p in poss)
        cash = self.client.get_buying_power()
        total_equity = invested + cash
        if total_equity <= 0:
            total_equity = 0.001
        weights = {p.ticker: ((p.market_value or 0.0) / total_equity) for p in poss}
        return {
            "positions": [asdict(p) for p in poss],
            "raw_positions": poss,  # broker Position list for execute_plan (PL-8/14)
            "weights": weights,
            "cash": cash,
            "total_equity": total_equity,
        }


if __name__ == "__main__":
    ex = MirrorExecutor()
    print("Executor init OK, cash:", ex.client.get_buying_power())
    # smoke a tiny order
    ois = [OrderIntent("NVDA", Side.LONG, 1.5, "test", 0.1, 0.0)]
    ress = ex.execute_plan(ois, [], "smoke1")
    print("Smoke order result:", ress[0].success if ress else None)
