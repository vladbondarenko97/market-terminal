"""Backfill SEC Form 4 insider filings for the Edge Lab's tracked symbols (or --symbols), for the insider cluster-buy edge.

For each symbol: look up its CIK (a symbol with none, such as a fund, is reported and skipped), list its Form 4 filings of the last
--years years from the company's EDGAR submissions file, skip the accessions already stored, fetch and parse the rest, and store one
`edgar.form4` observation per filing. The observation date is the FILING date, so the edge never looks ahead of what was public.
Re-run it any time to pick up new filings; the pipeline does not fetch Form 4 itself.

Needs SEC_USER_AGENT in .env ("Your Name you@example.com"): SEC asks automated clients to identify themselves. Nothing is requested without it.
Downloads one submissions file per symbol (plus older ones when --years reaches back past the last ~1,000 filings) and one small
XML file per filing; a company with many insiders has a few hundred filings in 5 years. One request at a time with --sleep seconds
between requests (SEC allows 10 a second; the default is far below). Try `--symbols INTC --limit 5` first.

Takes the run lock, like a pipeline run (exit 75 when a run or another job holds it). The operator reads and runs it; the pipeline never does.
Set PORTFOLIO_DATA_DIR to a scratch folder to try it without touching the real database.
"""
import argparse
import os
import sys
import time
from datetime import datetime, timedelta

ROOT_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if ROOT_DIR not in sys.path:
    sys.path.insert(0, ROOT_DIR)

import config
from core import backfill, lake, sources as src
from core.sources import SourceUnavailable

METRIC = "edgar.form4"


def today():
    """Today's date (a function, so the tests can fix the date)."""
    return datetime.now().date()


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--symbols", help="comma-separated tickers (default: the Edge Lab tracked list)")
    ap.add_argument("--years", type=float, default=5, help="years of filings (default 5)")
    ap.add_argument("--limit", type=int, default=None, help="at most this many new filings in total (default: no limit)")
    ap.add_argument("--sleep", type=float, default=0.3, help="seconds between requests (default 0.3)")
    a = ap.parse_args(argv)
    if not config.SEC_USER_AGENT.strip():
        print("SEC_USER_AGENT is not set: put your name and email in .env (see .env.example). Nothing was requested.")
        return 2
    if a.symbols:
        symbols = [x.strip().upper() for x in a.symbols.split(",") if x.strip()]
    else:
        from core import edges
        symbols = edges.tracked()
    since = (today() - timedelta(days=int(365 * a.years))).isoformat()

    def work(conn, session, run_id):
        total = {"stored": 0, "skipped": 0, "no_cik": 0, "failed": 0}
        left = a.limit
        try:
            tickers, _ = src.sec_company_tickers(session)
        except SourceUnavailable as e:
            print(f"cannot list SEC tickers: {e}")
            total["failed"] += 1
            return total
        with lake._WRITE_LOCK:
            have = {r[0] for r in conn.execute("SELECT entity FROM v2_observations WHERE metric_id = ?", (METRIC,))}
        for sym in symbols:
            if left is not None and left <= 0:
                break
            cik = tickers.get(sym)
            if not cik:
                print(f"{sym}: no SEC CIK for this ticker (a fund or unlisted), skipped")
                total["no_cik"] += 1
                continue
            time.sleep(a.sleep)
            try:
                rows, _ = src.sec_submissions(session, cik, since)
            except SourceUnavailable as e:
                print(f"{sym}: submissions failed: {e}")
                total["failed"] += 1
                continue
            new, stored, failed = [r for r in rows if r["accession"] not in have], 0, 0
            for r in new:
                if left is not None and left <= 0:
                    break
                time.sleep(a.sleep)
                try:
                    parsed, pid = src.sec_form4(session, cik, r["accession"], r["primary_document"])
                except SourceUnavailable as e:
                    failed += 1
                    print(f"    {r['accession']}: {e}")
                    continue
                lake.record_observations(conn, [lake.obs(METRIC, r["filing_date"], entity=r["accession"], dims={"symbol": sym}, value={**parsed, "company_cik": cik},
                                                         source="sec", payload_id=pid, time_quality="as_of_date")], run_id=run_id)
                have.add(r["accession"])
                stored += 1
                left = None if left is None else left - 1
            print(f"{sym}: {len(rows)} Form 4 filings since {since}: {stored} stored, {len(rows) - len(new)} already stored, "
                  f"{failed} failed, {len(new) - stored - failed} left for a later run")
            total["stored"] += stored
            total["skipped"] += len(rows) - len(new)
            total["failed"] += failed
        print(f"done. {total['stored']} stored, {total['skipped']} already stored, {total['no_cik']} symbols without a CIK, {total['failed']} failed")
        return total
    return backfill.run("form4", work)


if __name__ == "__main__":
    sys.exit(main())
