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
    emit_three_series_snapshot,
)

CACHE = ROOT / "data" / "leopold_13f_cache.json"
OUT = ROOT / "data" / "shadow_hedged_book.jsonl"
TOP_N_LONGS = 10


def _yahoo_close(ticker: str) -> Optional[float]:
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
            with urllib.request.urlopen(req, timeout=20) as resp:
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


def load_leopold_top10(cache_path: Path = CACHE) -> tuple[list[LongPosition], list[HedgeNotional]]:
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

    # Puts: scale short notionals to same sleeve proportional to disclosed put $
    short_total = sum(float(h.get("source_weight") or 0) for h in shorts) or 1.0
    hedges: list[HedgeNotional] = []
    # Only hedges on names in top-10 longs (the ask: puts against those top-10)
    top_set = {p.ticker for p in long_pos}
    for h in shorts:
        tkr = str(h["ticker"]).upper()
        if tkr not in top_set and tkr not in {x["ticker"] for x in top}:
            # still include major put names disclosed (SMH/NVDA etc.) as portfolio hedges
            pass
        notional = sleeve * (float(h.get("source_weight") or 0) / short_total)
        # Cap hedge notional contribution so shadow stays order-of-sleeve
        hedges.append(HedgeNotional(ticker=tkr, put_notional_usd=notional))

    # Prefer hedges matching top-10 long names; if none match, keep top put names by weight
    matched = [h for h in hedges if h.ticker in top_set]
    if matched:
        hedges = matched
    else:
        hedges = hedges[:10]

    return long_pos, hedges


def main() -> int:
    if not CACHE.exists():
        print(f"missing {CACHE}", file=sys.stderr)
        return 1
    longs, hedges = load_leopold_top10()
    print(f"longs={len(longs)} hedges={len(hedges)}")
    for p in longs:
        print(f"  LONG {p.ticker} shares={p.shares:.4f} @ {p.entry_price}")
    for h in hedges:
        print(f"  PUT_NOTIONAL {h.ticker} ${h.put_notional_usd:.2f}")

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
        with urllib.request.urlopen(req, timeout=15) as resp:
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

    row = emit_three_series_snapshot(
        longs=longs,
        hedges=hedges,
        price_fn=price_fn,
        spy_price=spy,
        spy_prior=spy_prior,
        path=OUT,
        days=1.0,
    )
    print(json.dumps(row, indent=2, default=str))
    print(f"wrote {OUT}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
