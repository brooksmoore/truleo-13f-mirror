"""Read-only price provider for attribution (daily close, fail-safe to None, cached).

Used only for read-only benchmark/attribution; never for trading decisions.
"""

from __future__ import annotations

import json
import time
import urllib.request
from pathlib import Path
from typing import Optional


class YahooPriceProvider:
    """Simple Yahoo Finance daily close fetcher. Returns None on any error/unknown (never fabricates).
    Caches to disk with TTL.
    """

    def __init__(
        self,
        ua: str = "truleo_agent/1.0 (attribution prices; local@example.test)",
        cache_dir: Optional[Path] = None,
        cache_ttl_sec: int = 3600 * 6,  # 6h is fine for daily
    ):
        self.ua = ua
        self.cache_dir = cache_dir or Path(CFG.data_dir)
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self.cache_path = self.cache_dir / "price_cache.json"
        self.cache_ttl = cache_ttl_sec
        self._mem_cache: dict[str, dict] = {}
        self._load_cache()

    def _load_cache(self) -> None:
        if self.cache_path.exists():
            try:
                data = json.loads(self.cache_path.read_text())
                # only keep fresh
                now = time.time()
                self._mem_cache = {
                    k: v for k, v in data.items()
                    if now - v.get("ts", 0) < self.cache_ttl
                }
            except Exception:
                self._mem_cache = {}

    def _save_cache(self) -> None:
        try:
            self.cache_path.write_text(json.dumps(self._mem_cache, indent=2, default=str))
        except Exception:
            pass

    def get_close(self, ticker: str) -> Optional[float]:
        if not ticker:
            return None
        key = ticker.upper().strip()
        now = time.time()
        if key in self._mem_cache:
            ent = self._mem_cache[key]
            if now - ent.get("ts", 0) < self.cache_ttl:
                p = ent.get("price")
                return float(p) if p is not None else None
        p = self._fetch_close(key)
        self._mem_cache[key] = {"price": p, "ts": now}
        self._save_cache()
        return p

    def _fetch_close(self, ticker: str) -> Optional[float]:
        try:
            url = f"https://query1.finance.yahoo.com/v8/finance/chart/{ticker}?interval=1d&range=5d"
            req = urllib.request.Request(url, headers={"User-Agent": self.ua})
            resp = urllib.request.urlopen(req, timeout=15)
            data = json.loads(resp.read())
            result = data.get("chart", {}).get("result") or []
            if not result:
                return None
            q = result[0].get("indicators", {}).get("quote", [{}])[0]
            closes = q.get("close") or []
            for c in reversed(closes):
                if c is not None:
                    return float(c)
            # fallback to meta regularMarketPrice if available
            meta = result[0].get("meta", {})
            rp = meta.get("regularMarketPrice")
            if rp is not None:
                return float(rp)
            return None
        except Exception:
            # any error -> None (fail-safe, never guess)
            return None
