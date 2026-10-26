"""Backfill card 15's history: NY Fed repo and reverse repo operations (from 2014), the NY Fed reference rates SOFR, TGCR,
BGCR and EFFR (from 2018-04-02, when SOFR starts), every Treasury buyback operation since the program restarted in 2024, and
Treasury auction results (from 2021),
plus the announced auction calendar.

The pipeline refetches only the last weeks on each run (core/fedops.py, topup), so the years before come from here. It asks
for one calendar year per request (about 15 requests in all, a few MB each from TreasuryDirect). A past year that an earlier
backfill stored is skipped, so an interrupted backfill resumes where it stopped and a second run only refreshes the current year.
Every row goes into v2_observations through INSERT OR IGNORE: running it twice stores nothing twice.

Takes the run lock, like a pipeline run (exit 75 when a run or another job holds it). The operator reads and runs it; the pipeline never does.
Set PORTFOLIO_DATA_DIR to a scratch folder to try it without touching the real database.
"""
import argparse
import os
import sys
from datetime import date, datetime, timedelta

ROOT_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if ROOT_DIR not in sys.path:
    sys.path.insert(0, ROOT_DIR)

from core import backfill, fedops, lake, market_calendar as MC
from core import sources as src

JOBS = {   # metric: (fetch(session, start, end) -> (rows, payload_id), rows -> observation records)
    fedops.REPO: (src.nyfed_repo_ops, fedops.repo_records),
    fedops.RATE: (src.nyfed_ref_rates, fedops.rate_records),
    fedops.AUCTION: (src.treasury_auction_search, fedops.auction_records),
}


def today():
    """Today in New York (a function, so the tests can fix the date)."""
    return datetime.now(MC.NEW_YORK).date()


def years(start, today):
    """(first day, last day) of each calendar year from `start` to today, oldest first; the last one ends today."""
    out, y = [], date.fromisoformat(start)
    while y <= today:
        out.append((y, min(date(y.year, 12, 31), today)))
        y = date(y.year + 1, 1, 1)
    return out


def _stored(conn, metric, first, last):
    """True when an earlier backfill stored this year. Rows from the pipeline's top-up (the last weeks only) do not count."""
    with lake._WRITE_LOCK:
        return conn.execute("SELECT 1 FROM v2_observations WHERE metric_id = ? AND observed_at >= ? AND observed_at <= ? "
                            "AND run_id LIKE 'backfill-fed_ops-%' LIMIT 1",
                            (metric, first.isoformat(), f"{last.isoformat()}T99")).fetchone() is not None


def work_for(day):
    def work(conn, session, run_id):
        counts = {"requests": 0, "skipped": 0, "stored": 0, "failed": 0}
        for metric, (fetch, to_records) in JOBS.items():
            for first, last in years(fedops.EARLIEST[metric], day):
                if last < day - timedelta(days=7) and _stored(conn, metric, first, last):
                    counts["skipped"] += 1           # a finished past year a backfill stored: its rows went in in one transaction
                    continue
                counts["requests"] += 1
                try:
                    rows, pid = fetch(session, first.isoformat(), last.isoformat())
                except src.SourceUnavailable as e:
                    counts["failed"] += 1
                    print(f"{metric} {first.year}: {e}")
                    continue
                n = lake.record_observations(conn, to_records(rows, pid), run_id=run_id)
                counts["stored"] += n
                print(f"{metric} {first.year}: {len(rows)} rows fetched, {n} new")
        counts["requests"] += 1
        try:
            rows, pid = src.treasury_buybacks(session, fedops.EARLIEST[fedops.BUYBACK])
            n = lake.record_observations(conn, fedops.buyback_records(rows, pid), run_id=run_id)
            counts["stored"] += n
            print(f"{fedops.BUYBACK}: {len(rows)} operations fetched, {n} new")
        except src.SourceUnavailable as e:
            counts["failed"] += 1
            print(f"{fedops.BUYBACK}: {e}")
        counts["requests"] += 1
        try:
            rows, pid = src.treasury_upcoming(session)
            counts["stored"] += lake.record_observations(conn, fedops.upcoming_records(rows, pid), run_id=run_id)
            print(f"{fedops.UPCOMING}: {len(rows)} announced auctions")
        except src.SourceUnavailable as e:
            counts["failed"] += 1
            print(f"{fedops.UPCOMING}: {e}")
        for metric in (fedops.REPO, fedops.RATE, fedops.BUYBACK, fedops.AUCTION):
            print(f"{metric}: earliest stored {fedops.earliest(conn, metric)}")
        return counts
    return work


def main(argv=None):
    argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter).parse_args(argv)
    return backfill.run("fed_ops", work_for(today()))


if __name__ == "__main__":
    sys.exit(main())
