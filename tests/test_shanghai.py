"""Date-paired Shanghai spread, premium statistics, SGE row rule and /api/shanghai_spread. Offline, synthetic data."""
import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import support  # noqa: F401,E402  (temporary CME_Data; must come before importing config)

import pandas as pd  # noqa: E402

from core import lake, metrics, sources  # noqa: E402
from test_api import ApiCase, api_router  # noqa: E402


class DatedSpread(unittest.TestCase):
    def test_only_dates_with_all_three_inputs_make_a_row(self):
        fixes = [{"date": "2026-10-01", "cny_per_kg": 14000.0, "session": "evening"},
                 {"date": "2026-10-02", "cny_per_kg": 14000.0, "session": "evening"},     # no COMEX close
                 {"date": "2026-10-03", "cny_per_kg": 14000.0, "session": "morning"},     # no FX close
                 {"date": "2026-10-04", "cny_per_kg": 0.0, "session": "morning"}]         # no positive fix
        comex = {"2026-10-01": 60.0, "2026-10-03": 60.0, "2026-10-04": 60.0}
        fx = {"2026-10-01": 7.0, "2026-10-02": 7.0, "2026-10-04": 7.0}
        out = metrics.shanghai_dated_spread(fixes, comex, fx)
        self.assertEqual(out["series"]["dates"], ["2026-10-01"])
        self.assertEqual(out["stats"]["history"]["n"], 1)

    def test_numbers_for_one_row(self):
        sh = 14000.0 / 7.0 / metrics.OZ_PER_KG
        out = metrics.shanghai_dated_spread([{"date": "2026-10-01", "cny_per_kg": 14000.0, "session": "evening"}],
                                            {"2026-10-01": 60.0}, {"2026-10-01": 7.0})
        self.assertEqual(out["status"], "fresh")
        self.assertEqual(out["latest"]["shanghai"], round(sh, 2))
        self.assertEqual(out["latest"]["raw_pct"], round((sh / 60 - 1) * 100, 2))
        self.assertEqual(out["latest"]["adj_pct"], round((sh / 1.13 / 60 - 1) * 100, 2))
        self.assertEqual(out["latest"]["adj_dollar"], round(sh / 1.13 - 60, 2))
        json.dumps(out, allow_nan=False)

    def test_series_is_capped_but_history_counts_every_row(self):
        days = [f"2026-09-{d:02d}" for d in range(1, 11)]
        fixes = [{"date": d, "cny_per_kg": 14000.0, "session": "evening"} for d in days]
        out = metrics.shanghai_dated_spread(fixes, {d: 60.0 for d in days}, {d: 7.0 for d in days}, keep=4)
        self.assertEqual(out["series"]["dates"], days[-4:])
        self.assertEqual(out["stats"]["history"]["n"], 10)

    def test_missing_inputs(self):
        fix = [{"date": "2026-10-01", "cny_per_kg": 1.0, "session": "evening"}]
        for args, text in (((fix, {}, {"a": 1}), "COMEX"), ((fix, {"a": 1}, {}), "USD/CNY"), (([], {"a": 1}, {"a": 1}), "SGE")):
            out = metrics.shanghai_dated_spread(*args)
            self.assertEqual(out["status"], "missing")
            self.assertIn(text, out["reason"])
        out = metrics.shanghai_dated_spread(fix, {"2026-10-02": 1.0}, {"2026-10-03": 1.0})
        self.assertEqual((out["status"], out["version"]), ("missing", metrics.SHANGHAI_VERSION))
        self.assertIn("no date", out["reason"])


class PremiumStats(unittest.TestCase):
    def rows(self, pcts, start="2026-10-01"):
        return [{"ts": pd.Timestamp(start) + pd.Timedelta(days=i), "label": f"d{i}", "pct": p, "a": 100 + p}
                for i, p in enumerate(pcts)]

    def test_small_case(self):
        s = metrics.premium_stats(self.rows([10.0, 20.0, 30.0]), {"a_pct": "a"})
        self.assertEqual((s["n"], s["mean"], s["std"], s["median"], s["min"], s["max"]), (3, 20.0, 10.0, 20.0, 10.0, 30.0))
        self.assertEqual((s["zscore"], s["pct_rank"]), (None, None))         # under 20 days
        self.assertEqual(s["changes"]["1d"], {"ref_label": "d1", "pts": 10.0, "a_pct": round((130 / 120 - 1) * 100, 2)})
        self.assertIsNone(s["changes"]["7d"])
        self.assertEqual(s["history"], {"n": 3, "days": 3, "first_label": "d0", "min": 10.0, "max": 30.0})

    def test_zscore_and_rank_with_twenty_days(self):
        s = metrics.premium_stats(self.rows([float(i) for i in range(1, 21)]), {})
        self.assertEqual((s["mean"], s["std"], s["zscore"], s["pct_rank"]), (10.5, 5.92, 1.61, 95.0))


class SgeRowRule(unittest.TestCase):
    def test_late_session_then_early_session_then_skip(self):
        df = pd.DataFrame({"交易时间": ["2026-10-01", "2026-10-02", "2026-10-03", "2026-10-04"],
                           "晚盘价": [100.0, 0.0, None, None], "早盘价": [90.0, 95.0, 96.0, None]})
        self.assertEqual(sources.sge_fixes(df), [
            {"date": "2026-10-01", "cny_per_kg": 100.0, "session": "evening"},
            {"date": "2026-10-02", "cny_per_kg": 95.0, "session": "morning"},
            {"date": "2026-10-03", "cny_per_kg": 96.0, "session": "morning"}])

    def test_an_unreadable_row_is_skipped_and_the_rest_kept(self):
        df = pd.DataFrame({"交易时间": ["2026-10-01", "not a date", "2026-10-03"],
                           "晚盘价": [100.0, 101.0, "-"], "早盘价": [90.0, 91.0, 96.0]})
        self.assertEqual(sources.sge_fixes(df), [{"date": "2026-10-01", "cny_per_kg": 100.0, "session": "evening"}])


class ShanghaiRoute(ApiCase):
    def test_no_database(self):
        r = self.client.get("/api/shanghai_spread")
        self.assertEqual(r.status_code, 503)

    def test_snapshot_without_dated_key(self):
        conn = lake.connect(api_router._V2_DB_PATH)
        lake.migrate(conn)
        lake.create_run(conn, "r1", mode="live", trigger="test", run_folder="x", code_version="t")
        lake.update_run(conn, "r1", status="committed")
        lake.commit_snapshot(conn, "r1", {"run": {"run_id": "r1", "generated_at": "2026-10-09T19:45:01+00:00"},
                                          "shanghai": {"status": "fresh", "sge_date": "2026-10-07", "cny_per_kg": 14312.0}})
        conn.close()
        d = self.get_json("/api/shanghai_spread")["data"]
        self.assertIsNone(d["dated"])
        self.assertEqual((d["legacy"]["sge_date"], d["legacy"]["cny_per_kg"], d["legacy"]["raw_spread"]),
                         ("2026-10-07", 14312.0, None))
        self.assertEqual(d["run_at"], api_router._utc_to_local_label("2026-10-09T19:45:01+00:00"))


if __name__ == "__main__":
    unittest.main()
