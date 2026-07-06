"""AUDITOR-OWNED gate test (independent) for P8-INTEG — attribution measurement integrity.

A live, unapproved catalyst=True (pending) name must NOT be counted as a filter-REJECT in
attribution. It is filter-ACCEPTED but held for human approval; lumping it into the rejected
shadow basket corrupts the accepted-vs-rejected instrument (the number that tests the thesis).

Drives the REAL filter_trump_holdings -> persist_decision -> monthly_report path. No network.

EXPECTED TO FAIL against pre-fix code (pending names go into `rejected` -> persisted as
catalyst_reject -> appear in rejected_mtm). Do not edit to pass; fix src/tagger.py + the
mirror_agent call site (give pending names a third bucket / `catalyst_pending` action).
"""

import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.tagger import CatalystTagger
from src.attribution import Attribution
from src.core.storage import GraveyardDB

GOOD_JSON = '{"catalyst": true, "type": "touted", "reason": "x", "source_url": "u", "confidence": 0.9}'
BAD_JSON = '{"catalyst": false, "type": "legacy", "reason": "no thesis", "source_url": "", "confidence": 0.5}'


class PendingIntegrityGate(unittest.TestCase):
    def setUp(self):
        self.dd = Path(tempfile.mkdtemp()) / "data"
        self.dd.mkdir(parents=True)
        self.g = GraveyardDB(self.dd)

    def _route(self, tagger, raw):
        """Call the real filter, then persist via the real persist_decision, tolerating
        either a 2-tuple (acc, rej) or a 3-tuple (acc, rej, pending) return."""
        res = tagger.filter_trump_holdings(raw)
        if len(res) == 3:
            acc, rej, pending = res
        else:
            acc, rej = res
            pending = []
        # Persist exactly as the orchestrator would. Pending (if surfaced) must NOT be
        # persisted as catalyst_reject.
        try:
            tagger.persist_decision(acc, rej, get_price=lambda t: 100.0,
                                    bench_entry={"SMH": 200.0, "SPY": 500.0})
        except TypeError:
            tagger.persist_decision(acc, rej)
        return acc, rej, pending

    def test_pending_name_not_counted_as_rejected(self):
        tagger = CatalystTagger(graveyard=self.g, live=True, llm_client=lambda p: GOOD_JSON)
        self._route(tagger, [{"ticker": "PENDINGNAME", "filing_id": "f1"}])

        # The pending name must NOT have been persisted as a catalyst_reject.
        rejects = self.g.get_rejected_by_action("catalyst_reject", 50)
        rej_tickers = {e.get("ticker") for e in rejects}
        self.assertNotIn("PENDINGNAME", rej_tickers,
                         "pending (filter-ACCEPTED) name must not be persisted as catalyst_reject")

        report = Attribution(self.g, self.dd).monthly_report(price_fn=lambda t: 130.0)
        self.assertEqual(report.get("rejected_count", 0), 0,
                         "pending name must not inflate rejected_count in attribution")

    def test_genuine_reject_still_counted(self):
        """A real filter-NO (catalyst=False) must still land in the rejected shadow basket."""
        tagger = CatalystTagger(graveyard=self.g, live=True, llm_client=lambda p: BAD_JSON)
        self._route(tagger, [{"ticker": "REALLEGACY", "filing_id": "f1"}])
        rejects = self.g.get_rejected_by_action("catalyst_reject", 50)
        self.assertIn("REALLEGACY", {e.get("ticker") for e in rejects},
                      "genuine catalyst=False name must still be recorded as catalyst_reject")


if __name__ == "__main__":
    unittest.main()
