#!/usr/bin/env python3
"""Wire Leopold 13F cache → shadow hedged book (READ-ONLY, no RH orders).

Uses:
  - data/leopold_13f_cache.json (top longs + short/put notionals)
  - Independent Yahoo chart closes for marks (fail-closed)
  - shadow_hedged_book.emit_three_series_snapshot

Does NOT touch the live broker.
"""

from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from shadow_hedged_book import (  # noqa: E402
    HedgeNotional,
    LongPosition,
    emit_four_series_snapshot,
)

CACHE = ROOT / "data" / "leopold_13f_cache.json"
OUT = ROOT / "data" / "shadow_hedged_book.jsonl"
STATE = ROOT / "data" / "shadow_hedge_state.json"
TOP_N_LONGS = 10


def _ssl_context():
    """CA-verified SSL context. The launchd interpreter (/usr/local/bin/python3.11)
    ships with NO system CA bundle (cafile=None), so plain urlopen() fails EVERY https
    fetch with CERTIFICATE_VERIFY_FAILED — which the broad except below swallowed to
    None, silently emitting all-zero NAVs since deploy (found 2026-07-15). Use certifi's
    bundle explicitly; fall back to the default context only if certifi is unavailable."""
    import ssl
    try:
        import certifi
        return ssl.create_default_context(cafile=certifi.where())
    except Exception:
        return ssl.create_default_context()


_SSL_CTX = _ssl_context()


# Per-run memo so the sizing pass and the mark pass don't each hit Yahoo for the
# same ticker (was 2 network fetches per ticker per run). Cleared naturally — the
# script is a one-shot launchd job, so "per-run" == process lifetime.
_CLOSE_MEMO: dict[str, Optional[float]] = {}


def _yahoo_close(ticker: str) -> Optional[float]:
    """Memoized per-run wrapper around the network fetch."""
    if ticker not in _CLOSE_MEMO:
        _CLOSE_MEMO[ticker] = _yahoo_close_fetch(ticker)
    return _CLOSE_MEMO[ticker]


def _yahoo_close_fetch(ticker: str) -> Optional[float]:
    """Independent daily close via Yahoo (same discipline as umbrella resolver)."""
    import json as _json
    import time
    import urllib.error
    import urllib.request

    url = f"https://query1.finance.yahoo.com/v8/finance/chart/{ticker}?interval=1d&range=5d"
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": "Mozilla/5.0 (compatible; pma-shadow/1.0)",
            "Accept": "application/json",
        },
    )
    for attempt in range(3):
        try:
            with urllib.request.urlopen(req, timeout=20, context=_SSL_CTX) as resp:
                data = _json.loads(resp.read().decode())
            result = (data.get("chart") or {}).get("result") or [{}]
            closes = (
                ((result[0].get("indicators") or {}).get("quote") or [{}])[0].get("close")
                or []
            )
            for c in reversed(closes):
                if c is not None:
                    return float(c)
            return None
        except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError, OSError, json.JSONDecodeError):
            time.sleep(0.4 * (attempt + 1))
    return None


def load_leopold_top10(
    cache_path: Path = CACHE,
) -> tuple[list[LongPosition], list[HedgeNotional], list[HedgeNotional]]:
    """Return (longs top-10, hedges_name_matched, hedges_full_basket).

    hedges_full_basket = ALL disclosed puts (sized proportional to disclosed weight);
    hedges_name_matched = the strict subset whose ticker overlaps a top-10 long. Both
    are handed to the four-series emitter so we can measure how much the modeling choice
    (name-matched vs full disclosed basket) actually moves the hedged-vs-longs comparison.
    """
    raw = json.loads(cache_path.read_text(encoding="utf-8"))
    holdings = list(raw.get("holdings") or [])
    longs = [h for h in holdings if h.get("side", "long") == "long"]
    shorts = [h for h in holdings if h.get("side") == "short"]
    longs.sort(key=lambda h: float(h.get("source_weight") or 0), reverse=True)
    top = longs[:TOP_N_LONGS]

    # Scale shadow sleeve to ~$100 notionals proportional to source weights
    total_w = sum(float(h.get("source_weight") or 0) for h in top) or 1.0
    sleeve = 100.0
    long_pos: list[LongPosition] = []
    for h in top:
        tkr = str(h["ticker"]).upper()
        w = float(h.get("source_weight") or 0) / total_w
        dollars = sleeve * w
        px = _yahoo_close(tkr)
        if not px or px <= 0:
            continue
        shares = dollars / px
        long_pos.append(
            LongPosition(
                ticker=tkr,
                shares=shares,
                entry_price=px,  # as-of mark = "entry" for this snapshot (no look-ahead)
                entry_ts=datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
            )
        )

    # Puts: scale short notionals to the same sleeve proportional to disclosed put $.
    short_total = sum(float(h.get("source_weight") or 0) for h in shorts) or 1.0
    hedges_full: list[HedgeNotional] = []
    for h in shorts:
        tkr = str(h["ticker"]).upper()
        notional = sleeve * (float(h.get("source_weight") or 0) / short_total)
        hedges_full.append(HedgeNotional(ticker=tkr, put_notional_usd=notional))

    # Name-matched = strict overlap with top-10 longs (may be empty; that's honest).
    top_set = {p.ticker for p in long_pos}
    hedges_matched = [h for h in hedges_full if h.ticker in top_set]

    return long_pos, hedges_matched, hedges_full


def main() -> int:
    if not CACHE.exists():
        print(f"missing {CACHE}", file=sys.stderr)
        return 1
    longs, hedges_matched, hedges_full = load_leopold_top10()
    print(f"longs={len(longs)} hedges_matched={len(hedges_matched)} hedges_full={len(hedges_full)}")
    if not longs:
        # Loud, non-silent failure: an empty long book means EVERY mark fetch failed
        # (the SSL/CA bug that emitted zero-NAV rows undetected for days). Do not emit
        # a garbage snapshot to umbrella; surface it so the launchd log + liveness show it.
        print(
            "WARNING: 0 longs marked — mark fetch is failing (check SSL/CA + Yahoo). "
            "Skipping umbrella emit to avoid polluting the measurement layer.",
            file=sys.stderr,
        )
    for p in longs:
        print(f"  LONG {p.ticker} shares={p.shares:.4f} @ {p.entry_price}")
    matched_tkrs = {h.ticker for h in hedges_matched}
    for h in hedges_full:
        tag = "matched+full" if h.ticker in matched_tkrs else "full-only"
        print(f"  PUT_NOTIONAL {h.ticker} ${h.put_notional_usd:.2f} [{tag}]")

    cache: dict[str, Optional[float]] = {}

    def price_fn(t: str) -> Optional[float]:
        import time

        if t not in cache:
            cache[t] = _yahoo_close(t)
            time.sleep(0.25)  # be kind to free Yahoo endpoints
        return cache[t]

    spy = price_fn("SPY")
    # prior SPY: approximate via same series (second-to-last close) — fail closed
    spy_prior = None
    try:
        import json as _json
        import urllib.request
        url = "https://query1.finance.yahoo.com/v8/finance/chart/SPY?interval=1d&range=10d"
        req = urllib.request.Request(url, headers={"User-Agent": "pma-shadow/1.0"})
        with urllib.request.urlopen(req, timeout=15, context=_SSL_CTX) as resp:
            data = _json.loads(resp.read().decode())
        closes = (
            ((data.get("chart") or {}).get("result") or [{}])[0]
            .get("indicators", {})
            .get("quote", [{}])[0]
            .get("close")
            or []
        )
        cleaned = [float(c) for c in closes if c is not None]
        if len(cleaned) >= 2:
            spy_prior = cleaned[-2]
            spy = cleaned[-1]
    except Exception:
        pass

    row = emit_four_series_snapshot(
        longs=longs,
        hedges_matched=hedges_matched,
        hedges_full=hedges_full,
        price_fn=price_fn,
        spy_price=spy,
        spy_prior=spy_prior,
        path=OUT,
        state_path=STATE,
        days=1.0,
    )
    print(json.dumps(row, indent=2, default=str))
    print(f"wrote {OUT}")

    # --- Umbrella emit: surface all four series to the measurement layer ---
    # (read-only observation, not a trade). Skipped when marks failed (longs==0) so we
    # never emit zero-NAV noise. Non-fatal: emit_decision_safe swallows any schema/IO error.
    series = row.get("series", {})
    if longs and series.get("longs_only_nav"):
        try:
            from decision_emit import build_decision_record, emit_decision_safe

            longs_nav = float(series.get("longs_only_nav") or 0.0)
            hedged_m = float(series.get("hedged_name_matched_nav") or 0.0)
            hedged_f = float(series.get("hedged_full_basket_nav") or 0.0)
            spy_px = series.get("spy_price")
            spy_ret = series.get("spy_return")
            short_n = row.get("short_notional", {})
            bench = {"SPY": float(spy_px)} if spy_px else {}
            bench["shadow_longs_only_nav"] = round(longs_nav, 4)
            bench["shadow_hedged_name_matched_nav"] = round(hedged_m, 4)
            bench["shadow_hedged_full_basket_nav"] = round(hedged_f, 4)
            rec = build_decision_record(
                kind="hold",
                instrument="LEOPOLD_SHADOW",
                reason=(
                    f"shadow hedged book: longs_only={longs_nav:.2f} "
                    f"hedged_name_matched={hedged_m:.2f} hedged_full_basket={hedged_f:.2f} "
                    f"spy_ret={spy_ret} n_longs={len(longs)} "
                    f"n_hedges_matched={len(hedges_matched)} n_hedges_full={len(hedges_full)}"
                ),
                mode="paper",
                regime="mirror",
                benchmarks=bench,
                prediction={"type": "none"},
                lineage={
                    "trigger": "shadow_hedged_book",
                    "longs_only_nav": round(longs_nav, 4),
                    "hedged_name_matched_nav": round(hedged_m, 4),
                    "hedged_full_basket_nav": round(hedged_f, 4),
                    "short_notional_name_matched": short_n.get("name_matched"),
                    "short_notional_full_basket": short_n.get("full_basket"),
                    "spy_return": spy_ret,
                    "n_longs": len(longs),
                    "n_hedges_matched": len(hedges_matched),
                    "n_hedges_full": len(hedges_full),
                    "hedge_cost_bps_per_day": row.get("hedge_cost_bps_per_day"),
                },
            )
            ok = emit_decision_safe(str(ROOT / "data" / "decisions.ndjson"), rec)
            print(f"umbrella emit: {'ok' if ok else 'FAILED (non-fatal)'}")
        except Exception as exc:  # never let emit break the shadow run
            print(f"umbrella emit skipped (non-fatal): {exc}", file=sys.stderr)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
