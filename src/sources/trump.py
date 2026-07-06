"""Trump personal disclosure source (STOCK Act / OGE filings).

Two-tier:
- Trigger: aggregator (speculator.io style) detects change.
- Verify: BEFORE any trade, reconcile aggregator claim vs official filing (PDF or structured).
  If disagree or cannot fetch/verify: FLAG + HOLD, log to Graveyard, NO orders.

Per spec §8: fail safe (no rebalance) if aggregator down or mismatch.
Gov-stake context only for tagger qualification.

Outputs same shape as leopold for reconciler: list of filtered catalyst-approved + raw rejected for shadow.
"""

from __future__ import annotations

import json
import time
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional, Callable

import sys
from pathlib import Path
_p = Path(__file__).resolve().parents[2]
if str(_p) not in sys.path:
    sys.path.insert(0, str(_p))
from config import CFG
from ..reconciler import _midpoint  # single tested impl (M7)
from ..core.storage import GraveyardDB  # for type and logging


@dataclass
class TrumpDisclosure:
    filing_id: str  # e.g. date or OGE id
    date: str
    holdings: list[dict]  # raw disclosed with ranges like "$1M-$5M"


class TrumpSource:
    """Aggregator trigger + filing verifier. For v1: fixtures + manual verify stub."""

    def __init__(self, cache_dir: Optional[Path] = None, graveyard: Optional["GraveyardDB"] = None, live: Optional[bool] = None,
                 aggregator_fetcher: Optional[Callable] = None, official_fetcher: Optional[Callable] = None):
        self.cache_dir = cache_dir or Path(CFG.data_dir)
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self.cache_path = self.cache_dir / "trump_disclosure_cache.json"
        self.graveyard = graveyard
        self.live = live if live is not None else getattr(CFG, "use_live_trump", False)
        self._aggregator_fetcher = aggregator_fetcher
        self._official_fetcher = official_fetcher
        self._load_cache()

    def _load_cache(self):
        if self.cache_path.exists():
            try:
                self._cache = json.loads(self.cache_path.read_text())
            except Exception:
                self._cache = {"last_filing_id": "", "raw": []}
        else:
            self._cache = {"last_filing_id": "", "raw": []}

    def _save_cache(self):
        self.cache_path.write_text(json.dumps(self._cache, indent=2, default=str))

    def _fetch(self, url: str) -> str:
        req = urllib.request.Request(url, headers={"User-Agent": "truleo_agent/1.0 (trump-source; local@example.test)", "Accept": "application/json,text/html,application/xml"})
        with urllib.request.urlopen(req, timeout=30) as resp:
            return resp.read().decode("utf-8", errors="replace")

    def _validate_aggregator_shape(self, d: Any) -> bool:
        """Small helper for shape check (cleaner live path)."""
        return isinstance(d, dict) and "filing_id" in d and isinstance(d.get("claimed_holdings"), list)

    def fetch_aggregator_latest(self) -> Optional[dict]:
        """In prod: hit aggregator RSS/JSON for latest disclosed changes.
        Returns {"filing_id": , "date": , "claimed_holdings": [{"ticker":, "range": "$1M-$5M", ...}] }
        """
        if self.live:
            if self._aggregator_fetcher:
                try:
                    res = self._aggregator_fetcher()
                    if res and self._validate_aggregator_shape(res):
                        return res
                    if self.graveyard:
                        self.graveyard.record_event(action="trump_feed_unavailable", meta={"reason": "aggregator_fetcher_bad_shape_or_none"})
                    return None
                except Exception:
                    if self.graveyard:
                        self.graveyard.record_event(action="trump_feed_unavailable", meta={"reason": "aggregator_fetcher_error"})
                    return None
            # fallback default live (may fail -> None, log). Never serve fixture on live.
            try:
                url = getattr(CFG, "trump_aggregator_url", "https://extapps2.oge.gov/201/Presiden.nsf/PAS+Index")
                try:
                    data = self._fetch(url)
                    if "speculator" in url or "oge" in url.lower():
                        if self.graveyard:
                            self.graveyard.record_event(action="trump_feed_unavailable", meta={"reason": "aggregator_live_not_configured_or_fetch_fail"})
                        return None
                    parsed = json.loads(data)
                    if self._validate_aggregator_shape(parsed):
                        return parsed
                    if self.graveyard:
                        self.graveyard.record_event(action="trump_feed_unavailable", meta={"reason": "aggregator_parse_bad_shape"})
                    return None
                except Exception:
                    if self.graveyard:
                        self.graveyard.record_event(action="trump_feed_unavailable", meta={"reason": "aggregator_fetch_error"})
                    return None
            except Exception:
                if self.graveyard:
                    self.graveyard.record_event(action="trump_feed_unavailable", meta={"reason": "aggregator_live_error"})
                return None
        # Fixture: recent example (made up ranges consistent with public reports of big tech/AI).
        return {
            "filing_id": "2026-05-08-278T2",
            "date": "2026-05-08",
            "claimed_holdings": [
                {"ticker": "NVDA", "range": "$1M-$5M"},
                {"ticker": "ORCL", "range": "over $50M"},
                {"ticker": "AVGO", "range": "$1M-$5M"},
                {"ticker": "MSFT", "range": "$500K-$1M"},
                # legacy names etc that filter will drop
                {"ticker": "FAKELEGACY", "range": "$1M-$5M"},
            ],
        }

    def fetch_official_filing(self, filing_id: str) -> Optional[dict]:
        """Fetch/parse the actual OGE PDF or data. For v1 fixture; real: download + pdf parse or text extract.
        Must return the authoritative list of holdings (ranges).
        Best-effort only: any fail/ambiguous/mismatch -> None (hold).
        """
        if self.live:
            try:
                # Real: OGE filings are often PDFs or pages. Use oge base + filing_id heuristic.
                # For v1, attempt text fetch from known OGE index or direct; on fail or no extractable holdings -> None.
                base = getattr(CFG, "oge_disclosure_search", "https://extapps2.oge.gov/201/Presiden.nsf/PAS+Index")
                # To support seam for tests/smoke (injected), prefer if provided.
                if self._official_fetcher:
                    try:
                        res = self._official_fetcher(filing_id)
                        if isinstance(res, dict):
                            return res
                        if self.graveyard:
                            self.graveyard.record_event(action="trump_feed_unavailable", meta={"reason": "official_fetcher_bad_shape", "filing_id": filing_id})
                        return None
                    except Exception:
                        if self.graveyard:
                            self.graveyard.record_event(action="trump_feed_unavailable", meta={"reason": "official_fetcher_error", "filing_id": filing_id})
                        return None
                # Default live attempt: fetch base or specific, parse text for tickers/ranges. Best effort only.
                try:
                    # For smoke, user injects. Default: try fetch and simple parse, else None.
                    text = self._fetch(base)
                    # Very best-effort: look for known patterns like "NVDA" + range in text.
                    # If we find a match for the id, parse; else None (conservative).
                    # Real impl would download PDF and extract text (no OCR here).
                    holdings = []
                    # simplistic scan (for demo only)
                    import re
                    for m in re.finditer(r'([A-Z]{1,5})\s*\$?([\d.]+[MK]?)\s*[-–]\s*\$?([\d.]+[MK]?)', text, re.I):
                        t = m.group(1).upper()
                        lo = m.group(2)
                        hi = m.group(3)
                        rng = f"${lo}-${hi}"
                        holdings.append({"ticker": t, "range": rng})
                    if holdings:
                        return {"filing_id": filing_id, "holdings": holdings}
                    if self.graveyard:
                        self.graveyard.record_event(action="trump_feed_unavailable", meta={"reason": "official_text_extract_failed", "filing_id": filing_id})
                    return None
                except Exception:
                    if self.graveyard:
                        self.graveyard.record_event(action="trump_feed_unavailable", meta={"reason": "official_fetch_error", "filing_id": filing_id})
                    return None
            except Exception:
                if self.graveyard:
                    self.graveyard.record_event(action="trump_feed_unavailable", meta={"reason": "official_live_error", "filing_id": filing_id})
                return None
        # Simulate: for the fixture id, return matching (or mismatch for test).
        if "278T2" in filing_id:
            return {
                "filing_id": filing_id,
                "holdings": [  # authoritative
                    {"ticker": "NVDA", "range": "$1M-$5M"},
                    {"ticker": "ORCL", "range": "over $50M"},
                    {"ticker": "AVGO", "range": "$1M-$5M"},
                    {"ticker": "MSFT", "range": "$500K-$1M"},
                    {"ticker": "FAKELEGACY", "range": "$1M-$5M"},
                ],
            }
        return None

    def verify_before_execute(self, aggregator_claim: dict) -> tuple[str, str, list[dict]]:
        """Core safety (tri-state per C1): compare claim vs official.
        Returns (status, reason, verified_list or []).
        status: 'verified' | 'hold'
        If cannot fetch or aggregator↔filing disagree → 'hold' (never collapse to empty for exit).
        Genuine empty after verified is 'verified' + [] .
        """
        fid = aggregator_claim.get("filing_id")
        official = self.fetch_official_filing(fid)
        if not official:
            return "hold", f"cannot_fetch_official_filing:{fid}", []
        claim_t = {h["ticker"]: h["range"] for h in aggregator_claim.get("claimed_holdings", [])}
        off_t = {h["ticker"]: h["range"] for h in official.get("holdings", [])}
        if claim_t != off_t:
            return "hold", f"aggregator_vs_filing_disagree tickers:{set(claim_t)^set(off_t)}", []
        # OK: return the verified (use official ranges)
        return "verified", "verified", official["holdings"]

    def get_raw_disclosed(self, force: bool = False) -> tuple[list[dict], str]:
        """For reconciler input (pre-filter). Returns (raw, status) where status in ('verified', 'hold').
        On 'hold' (unfetchable or disagree) returns ([], 'hold') — orchestrator must treat as HOLD not empty-for-exit.
        Genuine verified empty/reduced filing returns ([], 'verified') or reduced list + 'verified' (allows correct source_exit).
        """
        agg = self.fetch_aggregator_latest()
        if not agg:
            if self.live:
                if self.graveyard:
                    self.graveyard.record_event(action="trump_feed_unavailable", meta={"reason": "aggregator_live_returned_none_or_fail"})
            return [], "hold"
        fid = agg["filing_id"]
        if not force and fid == self._cache.get("last_filing_id"):
            return self._cache.get("raw", []), "verified"
        status, reason, verified = self.verify_before_execute(agg)
        if status != "verified":
            # caller (orchestrator) must treat as HOLD: flag, log to Graveyard, skip Trump recon, NO orders from Trump (incl. no source_exit sells)
            print(f"[TRUMP VERIFY FAIL] {reason} — holding, no trade on this trigger")
            if self.graveyard:
                self.graveyard.record_event(action="trump_feed_unavailable", meta={"reason": reason, "status": status})
            # do not advance last; allows retry/alert later
            return [], "hold"
        # Convert ranges to source_weight midpoint for reconciler
        raw = []
        for h in verified:
            mid = self._range_to_mid(h["range"])
            raw.append({"ticker": h["ticker"], "source_weight": mid, "range": h["range"], "filing_id": fid})
        self._cache["last_filing_id"] = fid
        self._cache["raw"] = raw
        self._save_cache()
        return raw, "verified"

    def _range_to_mid(self, rng: str) -> float:
        # delegate to single pure tested _midpoint in reconciler (M7)
        return _midpoint(rng)

    def is_new_filing(self) -> bool:
        agg = self.fetch_aggregator_latest()
        return bool(agg and agg["filing_id"] != self._cache.get("last_filing_id"))

    def get_last_verified_raw(self) -> list[dict]:
        """Return last successfully verified *catalyst-accepted* Trump holdings basket
        (post filter/approval, for carry-forward on hold/no_update per R1 + critical §1 gating).
        Never returns unverified, rejected, or pending data.
        """
        return list(self._cache.get("raw", []))

    def update_last_verified_accepted(self, accepted: list[dict]) -> None:
        """Store ONLY the post-catalyst-filter (and post-CA) accepted+approved basket as the
        carry-forward set. Called by orchestrator on verified cycles after filtering.
        This ensures get_last_verified_raw and no_update/hold carries are always filter-gated.
        """
        slim = []
        for h in accepted or []:
            entry = {
                "ticker": h.get("ticker"),
                "source_weight": float(h.get("source_weight", 0.0)),
                "filing_id": h.get("filing_id", ""),
            }
            if "range" in h:
                entry["range"] = h["range"]
            slim.append(entry)
        self._cache["raw"] = slim
        if slim:
            fid = slim[0].get("filing_id")
            if fid:
                self._cache["last_filing_id"] = fid
        self._save_cache()


if __name__ == "__main__":
    # Manual smoke for Part A (flag-on, not in CI/unittest): provide real fetchers or set use_live_trump=True + valid aggregator url that returns shape.
    # Example seam injection for real attempt (user can replace lambdas with http that hits aggregator + oge pdf/text extract):
    #   from config import CFG; CFG.use_live_trump = True
    #   src = TrumpSource(live=True)  # or pass aggregator_fetcher=real_fetch_fn, official_fetcher=...
    #   raw, status = src.get_raw_disclosed(force=True); print(status, raw)
    # On live error/unverifiable/mismatch: always "hold" + trump_feed_unavailable logged; NEVER fixture. Carry + get_last_verified_raw for no-sell.
    src = TrumpSource()
    raw, status = src.get_raw_disclosed(force=True)
    print("Trump verified raw (midpoints):", [(r["ticker"], round(r["source_weight"]/1e6,1)) for r in raw], "status=", status)
    print("New?", src.is_new_filing())
