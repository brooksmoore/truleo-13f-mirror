"""Health poller (separate from rebalance trigger).

Alerts (logs + graveyard) if Trump aggregator or EDGAR 13F feed goes quiet beyond expected cadence.
Prevents silent outage masquerading as 'no new filings'.
"""

from __future__ import annotations

from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Optional

import sys
_p = Path(__file__).resolve().parents[2]
if str(_p) not in sys.path:
    sys.path.insert(0, str(_p))
from config import CFG
from .core.storage import GraveyardDB


class HealthMonitor:
    def __init__(self, graveyard: Optional[GraveyardDB] = None, data_dir: Optional[Path] = None):
        # PL-7: fallback to configured data root
        dd = data_dir or Path(CFG.data_dir)
        self.g = graveyard or GraveyardDB(dd)

    def check_trump_liveness(self, last_filing_ts: Optional[datetime]) -> bool:
        if not last_filing_ts:
            self.g.record_event(action="trump_quiet", reject_reason="no_last_filing_seen")
            return False
        age = datetime.now(timezone.utc) - last_filing_ts
        if age > timedelta(days=CFG.quiet_threshold_days_trump):
            self.g.record_event(action="trump_quiet", meta={"age_days": age.days})
            return False
        return True

    def check_13f_liveness(self, last_filing_ts: Optional[datetime]) -> bool:
        if not last_filing_ts:
            return True  # 13F quarterly, ok to be absent for weeks
        age = datetime.now(timezone.utc) - last_filing_ts
        if age > timedelta(days=CFG.quiet_threshold_days_13f):
            self.g.record_event(action="leopold_quiet", meta={"age_days": age.days})
            return False
        return True


if __name__ == "__main__":
    h = HealthMonitor()
    print("health stubs ready")
