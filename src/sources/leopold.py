"""Leopold / Situational Awareness LP source (13F-HR).

- Poll EDGAR for new 13F by CIK (0002045724).
- Parse holdings (longs exact $ value; shorts side-tagged from future).
- Cache by accession.
- Emit list of {"ticker": , "source_weight": exact_usd, "side": "long"|"short", "filing_accession": }

Per spec: longs now; shorts deferred but data model ready (side tag from day 1).
Filing amendments (13F-HR/A) must trigger re-eval.
"""

from __future__ import annotations

import json
import time
import urllib.request
import urllib.error
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional
import xml.etree.ElementTree as ET
import re

import sys
from pathlib import Path
_p = Path(__file__).resolve().parents[2]
if str(_p) not in sys.path:
    sys.path.insert(0, str(_p))
from config import CFG

DEFAULT_UA = "truleo_agent/1.0 (mirror-basket; contact: local@example.test)"

CIK = CFG.edgar_cik_leopold
BASE = "https://www.sec.gov"


@dataclass
class LeopoldHolding:
    ticker: str
    value_usd: float  # exact from 13F
    shares: Optional[int] = None
    side: str = "long"  # "long" | "short" (puts etc)
    filing_accession: str = ""


class LeopoldSource:
    """Light poller + parser for 13F. Use for trigger on new filing + holdings."""

    def __init__(self, ua: str = DEFAULT_UA, cache_dir: Optional[Path] = None, graveyard: Optional["GraveyardDB"] = None, live: Optional[bool] = None):
        self.ua = ua
        self.cache_dir = cache_dir or Path(CFG.data_dir)
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self.cache_path = self.cache_dir / "leopold_13f_cache.json"
        self.graveyard = graveyard
        self.live = live if live is not None else getattr(CFG, "use_live_edgar", False)
        self._load_cache()

    def _load_cache(self):
        if self.cache_path.exists():
            try:
                self._cache = json.loads(self.cache_path.read_text())
            except Exception:
                self._cache = {"last_accession": "", "holdings": []}
        else:
            self._cache = {"last_accession": "", "holdings": []}

    def _save_cache(self):
        self.cache_path.write_text(json.dumps(self._cache, indent=2, default=str))

    def _fetch(self, url: str) -> str:
        req = urllib.request.Request(url, headers={"User-Agent": self.ua, "Accept": "text/html,application/xml"})
        with urllib.request.urlopen(req, timeout=30) as resp:
            return resp.read().decode("utf-8", errors="replace")

    def get_latest_13f(self) -> Optional[dict]:
        """Return latest 13F info or None.
        If live=True: use real fetch result if available (None on failure -- do NOT serve fixture).
        Fixture only for live=False (deterministic tests/demo). Live failure is fail-safe: no data.
        """
        if self.live:
            live = self._fetch_latest_13f_live()
            if live is not None:
                return live
            # live failure (P5-1): do not serve demo fixture (fail-safe).
            # Serve last-known-good *real* cached basket if we have prior real data (acc will match cache, no spurious is_new).
            # Otherwise None/empty -> no rebalance.
            last_acc = self._cache.get("last_accession")
            if last_acc and self._cache.get("holdings"):
                return {
                    "accession": last_acc,
                    "holdings": list(self._cache.get("holdings", [])),
                    "filing_date": self._cache.get("last_filing_date", ""),
                    "period_end": "",
                }
            if self.graveyard:
                self.graveyard.record_event(
                    action="leopold_feed_unavailable",
                    meta={"reason": "live_fetch_failed_no_prior_real_cache"}
                )
            return None
        # non-live: fixture only
        return {
            "accession": "000204572426000008",  # example
            "filing_date": "2026-05-18",
            "period_end": "2026-03-31",
            "holdings": self._get_fixture_holdings(),
        }

    def _get_fixture_holdings(self) -> list[dict]:
        # From public 13F summaries (longs heavy in semis/AI infra; puts noted as short).
        # Exact values from reports; using approx big ones for v1. Real parse will get precise.
        return [
            {"ticker": "SMH", "value_usd": 2040000000, "side": "long"},
            {"ticker": "NVDA", "value_usd": 1570000000, "side": "long"},
            {"ticker": "ORCL", "value_usd": 1070000000, "side": "long"},
            # ... more in real; also AVGO, AMD etc. Shorts as puts on semis.
            {"ticker": "SMH", "value_usd": 12345678, "side": "short"},  # example put exposure
        ]

    def parse_13f_xml(self, xml_text: str) -> list[dict]:
        """Parse <informationTable> XML (real or fixture) to list of raw holdings.
        Extracts name, cusip, value, sshPrnamt, putCall -> side.
        Does NOT resolve ticker here (done in caller with bounded map + skip).
        """
        holdings = []
        if not xml_text or not xml_text.strip():
            return holdings
        try:
            root = ET.fromstring(xml_text)
            # handle default ns or no ns
            for info in root.findall('.//{*}infoTable') or root.findall('.//infoTable'):
                def txt(el, tag):
                    v = el.findtext(f'.//{{*}}{tag}') or el.findtext(f'.//{tag}')
                    return (v or '').strip()
                name = txt(info, 'nameOfIssuer')
                cusip = txt(info, 'cusip').upper()
                val_s = txt(info, 'value').replace(',', '') or '0'
                try:
                    value_usd = float(val_s)
                except Exception:
                    value_usd = 0.0
                sh_s = txt(info, 'sshPrnamt').replace(',', '') or '0'
                try:
                    shares = int(sh_s)
                except Exception:
                    shares = 0
                put = txt(info, 'putCall').upper()
                side = 'short' if put == 'PUT' else 'long'
                holdings.append({
                    'name': name,
                    'cusip': cusip,
                    'value_usd': value_usd,
                    'shares': shares,
                    'side': side,
                    'putCall': put,
                })
            return holdings
        except Exception as e:
            # caller will skip/log
            return []

    def _resolve_ticker(self, cusip: str, name: str = "") -> Optional[str]:
        """Bounded CUSIP->ticker (plus name fallback only for exact known). Unmapped -> None (skip + log in caller)."""
        cusip = (cusip or "").upper().strip()
        if not cusip:
            return None
        # Bounded static map (no universal resolver; never guess). CUSIPs verified against the real
        # Situational Awareness LP 13F-HR (acc 0002045724-26-000008, filed 2026-05-18, period 2026-03-31).
        # Unmapped CUSIPs are skipped + logged by the caller (fail-safe; they simply don't trade).
        CUSIP_MAP = {
            # --- semis / big-cap (mostly held as PUTS=short in this filing; dropped when enable_shorts=False) ---
            "67066G104": "NVDA",   # NVIDIA
            "68389X105": "ORCL",   # Oracle
            "11135F101": "AVGO",   # Broadcom
            "007903107": "AMD",    # Advanced Micro Devices
            "595112103": "MU",     # Micron
            "874039100": "TSM",    # Taiwan Semiconductor (ADR)
            "N07059210": "ASML",   # ASML Holding NV (NY registry)
            "458140100": "INTC",   # Intel
            "219350105": "GLW",    # Corning
            "456788108": "INFY",   # Infosys (ADR)
            "92189F676": "SMH",    # VanEck Semiconductor ETF
            # --- AI-power / datacenter / crypto-miner LONG book (the tradeable sleeve) ---
            "093712107": "BE",     # Bloom Energy
            "80004C200": "SNDK",   # SanDisk
            "21873S108": "CRWV",   # CoreWeave
            "Q4982L109": "IREN",   # IREN Limited
            "21874A106": "CORZ",   # Core Scientific
            "038169207": "APLD",   # Applied Digital
            "767292105": "RIOT",   # Riot Platforms
            "18452B209": "CLSK",   # CleanSpark
            "09173B107": "BITF",   # Bitfarms
            "G11448100": "BTDR",   # Bitdeer Technologies
            "73933G202": "PSIX",   # Power Solutions International
            "05614L209": "BW",     # Babcock & Wilcox
            "74347M108": "PUMP",   # ProPetro Holding
            "433921103": "HIVE",   # HIVE Digital Technologies
            # Intentionally UNMAPPED (lower confidence → safe skip): 83418M103 Solaris Energy Infras,
            # 35834F104 T1 Energy, G96115103 WhiteFiber, 778920306 Sharon AI.
        }
        if cusip in CUSIP_MAP:
            return CUSIP_MAP[cusip]
        # no name guess per rules; only exact if we add
        return None

    def _resolve_and_filter(self, raw_rows: list[dict], acc: str) -> list[dict]:
        """Network-free: take raw parsed rows from parse_13f_xml (cusip/name/value_usd/side/...),
        resolve tickers via bounded map, drop unmapped (logging cusip_unmapped to graveyard if present),
        return list of {"ticker", "value_usd", "side"} for mapped ones.
        This makes the real skip+log path unit-testable (P5-2).
        """
        if not raw_rows:
            return []
        # Aggregate by (ticker, side): a 13F lists multiple lots per issuer (e.g. shares + options,
        # split blocks). Summing avoids one ticker taking multiple top-N slots / double-counting in weights.
        agg: dict = {}
        for h in raw_rows:
            tkr = self._resolve_ticker(h.get("cusip", ""), h.get("name", ""))
            if not tkr:
                if self.graveyard:
                    self.graveyard.record_event(
                        action="cusip_unmapped",
                        meta={
                            "cusip": h.get("cusip"),
                            "name": h.get("name"),
                            "acc": acc,
                            "value_usd": h.get("value_usd"),
                        },
                    )
                continue
            side = h.get("side", "long")
            key = (tkr, side)
            agg[key] = agg.get(key, 0.0) + float(h.get("value_usd", 0) or 0)
        return [{"ticker": t, "value_usd": v, "side": s} for (t, s), v in agg.items()]

    def _select_latest_13f_candidate(self, forms: list, accs: list, fdates: list) -> Optional[tuple]:
        """Network-free helper to select (date, acc, form) for the latest 13F-HR or 13F-HR/A.
        Extracted so amendment supersession logic is testable without network (P5-2).
        """
        if not forms or not accs or not fdates:
            return None
        cands = []
        for f, a, d in zip(forms, accs, fdates):
            if f in ("13F-HR", "13F-HR/A"):
                cands.append((d, a, f))
        if not cands:
            return None
        cands.sort(reverse=True)
        return cands[0]

    def _fetch_latest_13f_live(self) -> Optional[dict]:
        """Real SEC submissions + XML extract + parse. On error returns None; get_latest treats as unavailable for live=True (no fixture fallback, P5-1)."""
        try:
            # submissions for latest 13F-HR incl amendments
            sub_url = f"https://data.sec.gov/submissions/CIK{CIK}.json"
            sub = json.loads(self._fetch(sub_url))
            recent = sub.get("filings", {}).get("recent", {})
            forms = recent.get("form", [])
            accs = recent.get("accessionNumber", [])
            fdates = recent.get("filingDate", [])
            cand = self._select_latest_13f_candidate(forms, accs, fdates)
            if not cand:
                return None
            _, acc, form = cand
            acc_clean = acc.replace("-", "")
            base = f"https://www.sec.gov/Archives/edgar/data/{CIK.lstrip('0')}/{acc_clean}/"
            # Modern 13F filings keep the <informationTable> in a SEPARATE xml doc (not the combined .txt).
            # Resolve it via the filing index, then fall back to the embedded-in-.txt regex for older filings.
            xml = self._fetch_info_table_xml(base, acc)
            if not xml:
                return None
            raw = self.parse_13f_xml(xml)
            holdings = self._resolve_and_filter(raw, acc)
            return {
                "accession": acc,
                "filing_date": cand[0],
                "period_end": cand[0],
                "form": form,
                "holdings": holdings,
            }
        except Exception as e:
            # fail safe, do not crash; caller/get_latest will treat as unavailable (no fixture for live=True)
            return None

    def _fetch_info_table_xml(self, base: str, acc: str) -> Optional[str]:
        """Locate + fetch the 13F <informationTable> XML for a filing.
        Primary: read the filing index.json, pick the .xml that is NOT primary_doc.xml and contains an infoTable.
        Fallback: older filings embed the table in the combined {acc}.txt — extract via regex.
        Returns the XML string (a full info-table doc, or the extracted fragment), or None on failure.
        """
        # Primary: index.json -> separate info-table xml
        try:
            idx = json.loads(self._fetch(base + "index.json"))
            names = [it.get("name", "") for it in idx.get("directory", {}).get("item", [])]
            xml_files = [n for n in names if n.lower().endswith(".xml") and "primary_doc" not in n.lower()]
            for n in xml_files:
                try:
                    body = self._fetch(base + n)
                except Exception:
                    continue
                if re.search(r"infoTable", body, re.IGNORECASE):
                    return body
        except Exception:
            pass
        # Fallback: embedded in combined .txt (older filings)
        try:
            txt = self._fetch(base + f"{acc}.txt")
            m = re.search(r"(<informationTable[^>]*>.*?</informationTable>)", txt, re.DOTALL | re.IGNORECASE)
            if m:
                return m.group(1)
        except Exception:
            pass
        return None

    def get_current_basket(self, force: bool = False) -> list[dict]:
        """Return list ready for reconciler: top longs (shorts separate if enabled)."""
        latest = self.get_latest_13f()
        if not latest:
            return []
        acc = latest["accession"]
        if not force and acc == self._cache.get("last_accession"):
            return self._cache.get("holdings", [])
        holdings = []
        for h in latest.get("holdings", []):
            # Always include with side for conflict detection (M6/§9); ENABLE_SHORTS gates execution/sizing only.
            # Tolerate BOTH shapes: fresh parse ("value_usd") and the last-known-good cache fallback
            # (already-processed "source_weight") served by get_latest_13f on a transient live-fetch failure.
            sw = h.get("value_usd", h.get("source_weight"))
            if sw is None:
                continue  # malformed row — skip rather than crash (fail-safe)
            holdings.append(
                {
                    "ticker": h["ticker"],
                    "source_weight": float(sw),
                    "side": h.get("side", "long"),
                    "filing_accession": h.get("filing_accession", acc),
                }
            )
        # Sort desc for top-N later in reconciler
        holdings.sort(key=lambda x: x["source_weight"], reverse=True)
        self._cache["last_accession"] = acc
        self._cache["holdings"] = holdings
        self._save_cache()
        return holdings

    def is_new_filing(self) -> bool:
        latest = self.get_latest_13f()
        return bool(latest and latest["accession"] != self._cache.get("last_accession"))


if __name__ == "__main__":
    src = LeopoldSource()
    b = src.get_current_basket(force=True)
    print("Leopold sample basket (longs):", [ (h["ticker"], round(h["source_weight"]/1e6,1), "M") for h in b if h["side"]=="long" ][:5])
    print("New filing?", src.is_new_filing())
