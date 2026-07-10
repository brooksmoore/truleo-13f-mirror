"""MIRROR AGENT ORCHESTRATOR (event-driven, not cron).

Wires:
- health poller (light)
- sources (trump trigger+verify, leopold 13F)
- tagger (catalyst filter on trump delta)
- reconciler (pure math)
- executor (fractional, safety, logs, graveyard, kill, budget)
- attribution (shadow + reports)

For v1: demo loop with mocks/fixtures; "new filing" simulated by force=True on first, then cache prevents re-run.
Real: lightweight http poll on aggregator/EDGAR RSS or last-modified.

Separate state from hood_agent_1 completely.
"""

from __future__ import annotations

import os
import sys
import time
from pathlib import Path
from typing import Any, Optional

# robust path for running as module or script
ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from config import CFG
from src.core.storage import GraveyardDB, PersistentLog
from src.sources.leopold import LeopoldSource
from src.sources.trump import TrumpSource
from src.tagger import CatalystTagger, make_haiku_client
from src.reconciler import reconcile
from src.executor import MirrorExecutor
# Position import removed (unused; broker positions come from client via snapshot raw; schemas.Position for targets only)


def build_paper_executor(data_dir: Path, graveyard: Optional["GraveyardDB"] = None, plog: Optional["PersistentLog"] = None, sleeve_usd: Optional[float] = None, cfg: "Config" = None) -> MirrorExecutor:
    """Build executor for paper; accepts shared graveyard/plog for PL-9 single instance per run.
    Phase 2 surgical addition (only client selection): when cfg.use_live_broker=True use RealRobinhoodClient (MCP); else None (triggers prior Mock behavior).
    Flag-off path is byte-identical to Phase 1 (no other changes here).
    cfg defaults to global CFG; pass leopold_only_config() for the post-2026-06-16 run profile.
    """
    cfg = cfg or CFG
    g = graveyard or GraveyardDB(data_dir)
    pl = plog or PersistentLog(Path(cfg.logs_dir))
    if getattr(cfg, "use_live_broker", False):
        # real MCP-backed (injected transport in tests via RealRobinhoodClient(call_backend=...))
        from src.mcp.robinhood_client import RealRobinhoodClient
        client = RealRobinhoodClient()
    else:
        client = None  # Phase1 behavior: MirrorExecutor will create MockRobinhoodClient
    ex = MirrorExecutor(
        client=client,
        graveyard=g,
        plog=pl,
        data_dir=data_dir,
        sleeve_usd=sleeve_usd or cfg.robinhood_paper_starting_cash,
    )
    return ex


def run_cycle(
    leop: "LeopoldSource",
    trump: "TrumpSource",
    tagger: "CatalystTagger",
    ex: "MirrorExecutor",
    data_dir: Path,
    force: bool = False,
    c: int = 0,
    cfg: "Config" = None,
) -> None:
    """Process one cycle's logic (fetch, tag, reconcile with carry-forward for Trump on quiet/hold, execute, attrib).
    Extracted for R2 integration tests that drive the *real* orchestrator path with stubbed sources/executor.
    Behavior and prints identical to previous inline in run_demo (header printed by caller).
    cfg defaults to global CFG; pass leopold_only_config() to silence the Trump sleeve + 100% Leopold.
    """
    cfg = cfg or CFG
    # Check for new filings (force on first to populate)
    leop_new = leop.is_new_filing() or force
    trump_new = trump.is_new_filing() or force

    if not (leop_new or trump_new):
        print("No new filings — no action (event-driven).")
        time.sleep(0.2)
        return

    # Fetch (verified for trump)
    leop_basket = leop.get_current_basket(force=force)
    trump_raw: list[dict] = []
    trump_status = "no_update"
    filter_input = None
    if getattr(cfg, "disable_trump_sleeve", False):
        # Trump sleeve dropped (2026-06-16): skip the branch ENTIRELY — no fetch, no fixture,
        # no carry, no tag, no orders. trump_raw stays []; reconcile (sleeve_trump=0.0) puts 100% on Leopold.
        # This is the genuine disable that prevents the use_live_trump=False fixture from ever reaching a live order.
        trump_status = "disabled"
        print("Trump sleeve DISABLED (disable_trump_sleeve=True) — skipped; 100% Leopold this cycle.")
    elif trump_new or force:
        raw, status = trump.get_raw_disclosed(force=force)
        if status == "verified":
            filter_input = raw  # full disclosed; filter below, then use only acc for basket
            trump_status = "verified"
        else:
            # HOLD: carry last *verified* (accepted-only) basket so reconcile keeps Trump names (no spurious Trump exits);
            # leop exits will still be emitted naturally. Log hold below.
            trump_raw = trump.get_last_verified_raw()
            trump_status = "hold"
            print("Trump: aggregator triggered but verify failed or no change — HOLD (no rebalance).")
    else:
        # No new Trump filing per spec: carry last verified (accepted-only) Trump basket into reconcile.
        # This makes Trump targets present (carry), while leop changes produce their exits.
        trump_raw = trump.get_last_verified_raw()
        trump_status = "no_update"
    # (The old post-filter for source_exit is removed; carry-forward replaces it.)

    # Tag trump only on verified (new filing). For carry/hold/no_update we use the already-accepted carried set verbatim (no re-filter).
    if trump_status == "verified" and filter_input:
        trump_acc, trump_rej, trump_pend = tagger.filter_trump_holdings(filter_input)
    else:
        trump_acc, trump_rej, trump_pend = [], [], []
    print(f"Trump accepted (catalyst): {[h['ticker'] for h in trump_acc]}")
    print(f"Trump rejected (shadow): {[h['ticker'] for h in trump_rej]}")
    print(f"Trump pending (approval queue): {[h['ticker'] for h in trump_pend]}")

    # C3: capture ref prices at decision time (executor quote source), persist to Graveyard using consistent actions.
    # Reconciler stays pure; prices + persist here in orchestrator layer. Even rejected shadow get entry price for later mtm vs bench.
    # Pending go to distinct catalyst_pending (not reject) so they don't pollute acc-vs-rej attribution; still passed combined in rejected_raw
    # to preserve the "held out of live basket" effect for reconcile/plan (unchanged gating).
    if trump_status == "verified" and (trump_acc or trump_rej or trump_pend):
        def get_price(tkr: str) -> float:
            try:
                q = ex.client.get_quote(tkr)
                last = getattr(q, "last", 0.0)
                bid = getattr(q, "bid", 0.0)
                ask = getattr(q, "ask", 0.0)
                return last if last > 0.01 else ((bid + ask) / 2.0 if bid > 0 and ask > 0 else 0.0)
            except Exception:
                return 0.0
        # Capture decision-time benchmark refs (SMH/SPY) from the same price source.
        # Fail-safe: only stamp if valid (>0); if neither available, omit bench_entry entirely.
        smh_p = get_price("SMH")
        spy_p = get_price("SPY")
        bench_entry = {}
        if smh_p > 0.01:
            bench_entry["SMH"] = smh_p
        if spy_p > 0.01:
            bench_entry["SPY"] = spy_p
        if not bench_entry:
            bench_entry = None
        tagger.persist_decision(trump_acc, trump_rej, get_price, bench_entry=bench_entry, pending=trump_pend)

    # Prepare the Trump input for CA/reconcile: MUST be catalyst-accepted only (the filter now gates the basket).
    # For verified: use acc (pre-CA). For hold/no_update: the carried trump_raw is already the accepted basket (set verbatim, no re-filter).
    if trump_status == "verified":
        trump_raw = [{k: v for k, v in h.items() if k != "catalyst_tag"} for h in (trump_acc or [])]
    # else: trump_raw already set to carried accepted-only above

    # Current book (dicts for reconcile which expects list[dict]; real mcp positions for executor)
    # PL-14: ONE snapshot per cycle, used for BOTH reconcile (weights) and (via raw) execute (positions) for consistency.
    # PL-1: real total_equity (pos MV + cash) from snapshot is the capital base; thread to reconcile + executor sizing.
    snap = ex.get_portfolio_snapshot()
    curr_w = snap["weights"]
    curr_for_recon = [{"ticker": p["ticker"], "weight": curr_w.get(p["ticker"], 0.0)} for p in snap.get("positions", [])]
    equity = snap.get("total_equity") or cfg.robinhood_paper_starting_cash
    if equity <= 0:
        equity = cfg.robinhood_paper_starting_cash
    # thread to executor's sizing for this cycle (share sizing + any translate inside)
    ex.sleeve_usd = equity

    # R4: perform CA adjustment in orchestrator (pre-reconcile) so reconciler stays pure.
    # (the hook+test remain; full impl deferred as before)
    from .sources import corporate_actions
    trump_raw, _ = corporate_actions.adjust_for_corporate_actions(trump_raw or [], curr_for_recon or [])
    leopold_raw, adj_curr = corporate_actions.adjust_for_corporate_actions(leop_basket or [], curr_for_recon or [])
    curr_for_recon = adj_curr or curr_for_recon

    # CRITICAL: on verified cycle, now that CA has run on the *gated* (accepted-only) trump_raw,
    # update Trump's carry cache so future hold/no_update carry the accepted basket (not full raw).
    if trump_status == "verified":
        trump.update_last_verified_accepted(trump_raw or [])

    # Reconcile (top-N inside). trump_raw is now the carried last-verified when on hold/no_update.
    # sleeve_total_usd now real equity (or fallback), so floor $ and drift $ gates + downstream share sizing scale with account.
    plan = reconcile(
        trump_raw=trump_raw,
        leopold_raw=leopold_raw,
        current_positions=curr_for_recon,
        cfg=cfg,
        # pending names held out of basket same as before by including in rejected_raw for the plan
        # (genuine catalyst=False + pending catalyst=True-unapproved); acc list itself not passed here
        trump_rejected_raw=(list(trump_rej or []) + list(trump_pend or [])) if trump_status == "verified" else [],
        sleeve_total_usd=equity,
        current_weights=curr_w,
    )

    # Note: the C1 membership filter for source_exit was removed (R1); carry-forward of Trump raw
    # ensures Trump names stay in targets (no unwanted exits), while genuine Leopold source_exits fire.

    # Still log hold event when applicable (R1 requirement)
    # PL-9: reuse ex.graveyard (shared instance), never recreate
    if trump_status == "hold":
        ex.graveyard.record_event(
            action="trump_hold",
            outcome="hold",
            reject_reason="verify_failed_or_disagree",
            meta={"trump_new": trump_new, "raw_len": len(trump_raw)},
        )

    print(f"Reconciler targets: {[(p.ticker, round(p.target_weight,3)) for p in plan.targets]}")
    print(f"Orders (minimal): {[(o.ticker, o.reason, round(o.target_weight,3)) for o in plan.orders]}")

    # R5: narrow the suppression predicate.
    # - no_update: NO suppression (rely on carry-forward + drift band; overlaps must follow Leopold)
    # - hold: suppress ONLY pure-Trump (in trump_tkrs AND not in current leopold basket)
    # This fixes overlap rebalancing on quiet cycles while preserving fail-safe for pure-Trump on hold.
    if trump_status == "hold":
        leop_tkrs = {h.get("ticker") for h in (leopold_raw or [])}
        trump_tkrs = {h.get("ticker") for h in (trump_raw or [])}
        plan.orders = [o for o in plan.orders if not (o.ticker in trump_tkrs and o.ticker not in leop_tkrs)]

    if plan.orders:
        # M5: trigger_id from filing ids (not loop counter) so replay of same filing dedupes even across "cycles"
        t_fid = (trump_raw[0].get("filing_id") if trump_raw else "none") if trump_status == "verified" else "none"
        l_acc = (leopold_raw[0].get("filing_accession") if leopold_raw else "none")
        trigger_id = f"trump:{t_fid}:leop:{l_acc}"
        # Umbrella decisions contract (pre-execution; fail-safe — never blocks trading)
        try:
            from decision_emit import emit_plan_intents

            def _px(tkr: str) -> float:
                try:
                    q = ex.client.get_quote(tkr)
                    last = getattr(q, "last", 0.0) or 0.0
                    return float(last)
                except Exception:
                    return 0.0

            mode = "live" if getattr(cfg, "use_live_broker", False) else "paper"
            _benches = None
            try:
                _benches = bench_entry  # set on verified Trump path; may be unset
            except NameError:
                _benches = None
            n_dec = emit_plan_intents(
                plan.orders,
                mode=mode,
                path=data_dir / "decisions.ndjson",
                get_price=_px,
                filing_id=str(l_acc),
                benchmarks=_benches,
            )
            if n_dec:
                print(f"[decisions] emitted {n_dec} pre-exec records")
        except Exception as _dec_exc:
            print(f"[decisions] emit skipped: {_dec_exc}")
        # PL-14: use the raw_positions captured in the single snapshot (no second client.get_positions)
        mcp_positions = snap.get("raw_positions") or (ex.client.get_positions() if hasattr(ex, "client") else [])
        results = ex.execute_plan(plan.orders, mcp_positions, trigger_id=trigger_id)
        print(f"Executor results: {[(r.success, r.order_id) for r in results]}")

    # Attribution is post-trade ANALYTICS — it must never block or slow the trading path. On the live
    # broker it would re-quote every historically-recorded ticker (dozens) through the MCP each cycle,
    # adding latency and flakiness for zero trading value. Skip it when cfg.skip_attribution is set
    # (the live runner does); orders + fills are already logged to the graveyard above.
    if getattr(cfg, "skip_attribution", False):
        time.sleep(0.05)
        return plan
    # C3: real attribution (not stub). Query uses the records just persisted with prices.
    # PL-4: in production/live cycle path, use real YahooPriceProvider (when CFG.use_live_prices) or a non-fabricating
    # client-quote-or-None fn. Never pass a fn that returns fabricated 100+hash on failure. Attribution's internal
    # fail-safe (None -> exclude + count missing) applies. demo_only_price_fn kept for flag-off demo visibility via client quotes.
    # PL-9: use ex.graveyard (shared)
    from src.attribution import Attribution
    attr = Attribution(ex.graveyard, data_dir)
    if getattr(cfg, "use_live_prices", False):
        from src.prices import YahooPriceProvider
        _prov = YahooPriceProvider(cache_dir=data_dir)
        def price_fn(tkr: str):
            try:
                p = _prov.get_close(tkr)
                return float(p) if p is not None else None
            except Exception:
                return None
    else:
        # clearly-named demo-only: client quote (for mock paper mtm) or None (no fabricate; lets attr _default or exclude)
        def demo_only_price_fn(tkr: str):
            try:
                q = ex.client.get_quote(tkr)
                last = getattr(q, "last", 0.0)
                bid = getattr(q, "bid", 0.0)
                ask = getattr(q, "ask", 0.0)
                p = last if last > 0.01 else ((bid + ask) / 2.0 if bid > 0 and ask > 0 else 0.0)
                return p if p > 0 else None
            except Exception:
                return None  # never fabricate; missing-excluded is the safe outcome (PL-4)
        price_fn = demo_only_price_fn
    report = attr.monthly_report(price_fn=price_fn)
    print(f"Attribution: acc={report.get('accepted_count')} rej={report.get('rejected_count')} mtm_acc={report.get('accepted_mtm')[:3]} bench_delta_computed={report.get('has_bench_delta')} bench={report.get('benchmark_prices')}")

    time.sleep(0.1)
    return plan  # returned for R2 integration tests to inspect real orders etc; demo ignores return value


def run_demo(paper: bool = True, cycles: int = 2, cfg: "Config" = None):
    cfg = cfg or CFG
    leop_only = getattr(cfg, "disable_trump_sleeve", False)
    print(f"=== truleo_agent mirror-basket demo (paper, mocks){' — LEOPOLD-ONLY' if leop_only else ''} ===")
    # PL-7: derive from configured root (single source of truth; no CWD literals)
    data_dir = Path(cfg.data_dir)
    data_dir.mkdir(exist_ok=True)
    logs_dir = Path(cfg.logs_dir)
    logs_dir.mkdir(exist_ok=True)

    # PL-9: ONE GraveyardDB + PersistentLog per run; inject everywhere (orchestrator, sources, tagger, executor)
    g = GraveyardDB(data_dir)
    pl = PersistentLog(logs_dir)

    leop = LeopoldSource(cache_dir=data_dir, graveyard=g, live=getattr(cfg, "use_live_edgar", False))
    # Trump source still constructed (so the disabled-path code stays exercised) but run_cycle skips it when disabled.
    trump = TrumpSource(cache_dir=data_dir, graveyard=g, live=cfg.use_live_trump)
    ex = build_paper_executor(data_dir, graveyard=g, plog=pl, cfg=cfg)
    llm_client = None
    if cfg.use_live_tagger:
        try:
            llm_client = make_haiku_client()
        except Exception:
            llm_client = None
    tagger = CatalystTagger(
        graveyard=g,
        llm_client=llm_client,
        live=cfg.use_live_tagger,
        can_spend=getattr(ex, "can_spend", None),
        record_spend=getattr(ex, "record_spend", None),
    )

    for c in range(cycles):
        print(f"\n--- cycle {c+1} ---")
        force = (c == 0)
        _ = run_cycle(leop, trump, tagger, ex, data_dir, force=force, c=c, cfg=cfg)  # ignore return in demo

    print("\nDemo complete. Check data/graveyard.db and logs/ for records.")
    print("To go real: wire real quotes (MarketData), real MCP client, real Haiku via LLM seam, real EDGAR/OGE pollers.")


if __name__ == "__main__":
    run_demo()


# --- Phase 2 sandbox smoke (read-only; flag-gated; no orders placed) ---
def run_sandbox_smoke() -> None:
    """Read-only smoke for RealRobinhoodClient + MCP connectivity (paper/sandbox first).
    - Sets local client to Real (never mutates global CFG.use_live_broker for safety).
    - Only reads: positions, buying_power, a quote.
    - Prints results for overseer verification.
    - Run *before* any funded enablement. Requires connected Robinhood Trading MCP + prior search_tool discovery in the env.
    - Kill switch / budget etc still apply if you later extend it to a plan, but this smoke does not place.
    """
    print("=== Phase 2 SANDBOX SMOKE (read-only; use_live_broker path) ===")
    data_dir = Path(CFG.data_dir)
    data_dir.mkdir(exist_ok=True)

    # Force real client for this smoke only (surgical wiring already in build; we construct directly here)
    from src.mcp.robinhood_client import RealRobinhoodClient
    try:
        rh = RealRobinhoodClient()
    except Exception as e:
        print(f"Real client construction failed (expected until MCP connected): {e}")
        print("Sandbox smoke ABORTED — connect Robinhood Trading MCP and re-run (search_tool first).")
        return

    ex = MirrorExecutor(client=rh, data_dir=data_dir, sleeve_usd=CFG.robinhood_paper_starting_cash)

    try:
        poss = rh.get_positions()
        cash = rh.get_buying_power()
        q = rh.get_quote("NVDA")
        print(f"Sandbox smoke passed — positions: {len(poss)}, cash: ${cash:.2f}, NVDA quote: bid={q.bid} ask={q.ask} last={q.last}")
        print("Next step (overseer): review positions/cash/quote look sane for *your* sandbox account; then sign off before funded.")
    except Exception as e:
        print(f"Smoke read failed (MCP may not be fully ready): {e}")
        print("This is expected pre-connection. Connect MCP and retry.")


# To run the smoke manually (after MCP server connected in your env):
#   PYTHONPATH=. python3 -c 'from src.mirror_agent import run_sandbox_smoke; run_sandbox_smoke()'
