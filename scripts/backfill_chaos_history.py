"""Backfill the two Chaos / Peace components that need 3 years of daily history: the CBOE equity put/call ratio and FINRA's SPY short-volume ratio.

The pipeline adds only the 5 trading days before each run, so these two components would need 3 years of runs to become usable.
This fetches every NYSE trading day of the last --years years (plus 45 days, so the 3-year span test passes) that is not stored yet,
newest first. Re-running it only fetches what is still missing; a day the provider has no file for (HTTP 403 or 404) is counted and
reported, and asked again only by a later run of this script.

Downloads about 750 small JSON files (CBOE) and 750 text files of about 0.5 MB each (FINRA; one line per file is kept) for 3 years,
one request at a time with --sleep seconds between requests: expect roughly 15 to 30 minutes. Try `--limit 6` first.

Takes the run lock, like a pipeline run (exit 75 when a run or another job holds it). The operator reads and runs it; the pipeline never does.
Set PORTFOLIO_DATA_DIR to a scratch folder to try it without touching the real database.
"""
import argparse
import os
import sys
from datetime import datetime, timedelta

ROOT_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if ROOT_DIR not in sys.path:
    sys.path.insert(0, ROOT_DIR)

from core import backfill, collect, market_calendar as MC


def today():
    """Today in New York (a function, so the tests can fix the date)."""
    return datetime.now(MC.NEW_YORK).date()


def trading_days(today, years):
    """NYSE trading days before `today` (today's files are not out yet), newest first, back `years` years plus 45 days."""
    first, d, out = today - timedelta(days=int(365 * years) + 45), today, []
    while d > first:
        d -= timedelta(days=1)
        if MC.is_trading_day(d):
            out.append(d)
    return out


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--years", type=float, default=3, help="years of history (default 3)")
    ap.add_argument("--limit", type=int, default=None, help="at most this many requests per series (default: no limit)")
    ap.add_argument("--sleep", type=float, default=0.25, help="seconds between requests (default 0.25)")
    a = ap.parse_args(argv)
    days = trading_days(today(), a.years)

    def work(conn, session, run_id):
        total = {"stored": 0, "skipped": 0, "no_file": 0, "failed": 0}
        for name, (metric, source, _) in collect.CHAOS_DAILY.items():
            left, res = a.limit, {"errors": {}, "stored": 0, "skipped": 0, "no_file": 0, "failed": 0}
            for i in range(0, len(days), 50):                  # chunks of 50 days, so a long run shows progress
                if left is not None and left <= 0:
                    break
                r = collect.fetch_daily_days(conn, session, run_id, name, days[i:i + 50], limit=left, sleep=a.sleep)
                for k in ("stored", "skipped", "no_file", "failed"):
                    res[k] += r[k]
                res["errors"].update(r["errors"])
                if left is not None:
                    left -= r["stored"] + r["no_file"] + r["failed"]
                print(f"  {metric}: {res['stored']} stored, {res['skipped']} already stored, {res['no_file']} without a file, "
                      f"{res['failed']} failed ({min(i + 50, len(days))}/{len(days)} days looked at)", flush=True)
            print(f"{metric}: done. {res['stored']} stored, {res['skipped']} already stored, {res['no_file']} days without a file, {res['failed']} failed")
            for k, v in list(res["errors"].items())[:10]:
                print(f"    {k}: {v}")
            for k in total:
                total[k] += res[k]
        return total
    return backfill.run("chaos", work)


if __name__ == "__main__":
    sys.exit(main())
