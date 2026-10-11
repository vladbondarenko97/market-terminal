"""Chaos / Peace history: the backdated daily score, episodes, the regime study, both kinds of election, the collector's
wiring and the stored /api/chaos/history route. Synthetic data only."""
import sys, unittest
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent))
import support  # noqa: F401  (temporary CME_Data, no credentials; must come before importing config)

import json
import tempfile
from datetime import date
from unittest import mock

import numpy as np
import pandas as pd

from core import chaos as CH, collect, lake, sources as src
from core.sources import SourceSession
from test_api import ApiCase
from test_chaos import TODAY, fake_get, series


def frame(scores, used=4, hot=0, inverted=None, end=TODAY):
    n = len(scores)
    return pd.DataFrame({"score": np.asarray(scores, float), "used": used, "hot": hot, "inverted": [inverted] * n},
                        index=pd.bdate_range(end=end, periods=n))


def inputs(**over):
    s = {"VIX": series(1, last_z=1.0), "VIX3M": series(2) * 0.9, "SKEW": series(3, mean=130, sd=5), "SPY": series(4, mean=400, sd=3)}
    s.update(over)
    return s


class LongHistory(unittest.TestCase):
    def test_a_component_counts_only_three_years_after_its_series_begins(self):
        s = inputs(VIX3M=series(2, n=3000) * 0.9, VIX=series(1, n=3000), SPY=series(4, n=3000, mean=400, sd=3))
        late = s["VIX3M"].iloc[1200:]
        with_ = CH.long_history({**s, "VIX3M": late})
        without = CH.long_history({k: v for k, v in s.items() if k != "VIX3M"})
        diff = (with_["used"] - without["used"]).dropna()
        cut = late.index[0] + pd.Timedelta(days=CH.SPAN_DAYS)
        self.assertEqual(diff[diff.index < cut].max(), 0)
        self.assertEqual(diff[diff.index >= cut].min(), 1)

    def test_a_stale_component_drops_out(self):
        s = inputs()
        full = CH.long_history(s)
        old = CH.long_history({**s, "SKEW": s["SKEW"].iloc[:-20]})
        self.assertEqual(full["used"].iloc[-1] - old["used"].iloc[-1], 1)
        self.assertEqual(full["used"].iloc[-15] - old["used"].iloc[-15], 0)      # 5 days old: still inside the 10-day tolerance

    def test_hot_and_inverted(self):
        s = inputs()
        h = CH.long_history(s)
        self.assertEqual(h["inverted"].iloc[-1], True)                          # VIX3M is 0.9 x a similar series
        self.assertGreaterEqual(h["hot"].iloc[-1], 1)                           # VIX pinned at z = 1
        h2 = CH.long_history({**s, "VIX3M": s["VIX3M"] * 1.5})
        self.assertEqual(h2["inverted"].iloc[-1], False)
        self.assertIsNone(CH.long_history({k: v for k, v in s.items() if k != "VIX3M"})["inverted"].iloc[-1])
        self.assertTrue(CH.long_history({}).empty)

    def test_last_row_equals_build(self):
        s = inputs()
        h = CH.long_history(s)
        b = CH.build({"today": TODAY, "series": s})
        self.assertEqual(round(float(h["score"].iloc[-1]), 1), b["score"])
        self.assertEqual(int(h["used"].iloc[-1]), b["components_used"])


class Episodes(unittest.TestCase):
    def test_start_end_and_peak(self):
        h = frame([0] * 5 + [40, 50, 45, 90, 10, 10, 10, 10, 40, 10, 10, 10, 10, 10] + [0] * 3)
        (e,) = CH.episodes(h)
        self.assertEqual(e["start"], h.index[7].date().isoformat())             # the 3rd day above 30
        self.assertEqual(e["end"], h.index[18].date().isoformat())              # the 5th day below 15 (the 40 broke the first run)
        self.assertEqual((e["peak"], e["peak_regime"], e["days"], e["used"]), (90.0, "chaos", 12, 4))
        self.assertEqual(e["peak_date"], h.index[8].date().isoformat())

    def test_ongoing_and_two_days_do_not_start(self):
        self.assertEqual(CH.episodes(frame([0, 40, 40, 0, 0, 0, 0, 0, 0]))[:], [])
        (e,) = CH.episodes(frame([0, 40, 40, 40, 20, 20]))
        self.assertIsNone(e["end"])
        self.assertEqual(e["days"], 3)


class Study(unittest.TestCase):
    def build(self, n=700, spikes=(100, 300, 500), tail=0):
        sc = np.full(n, -40.0)
        for i in spikes:
            sc[i:i + 3] = 40
        if tail:
            sc[-3:] = 40
        return frame(sc)

    def test_base_rates_signal_onsets_and_active(self):
        st = CH.regime_study(self.build())
        self.assertEqual(st["period"]["days"], 700)
        r20 = st["base_rates"][0]
        self.assertEqual((r20["all"]["n"], r20["all"]["p"]), (662, round(60 / 662, 3)))      # 680 usable days less 18 episode days
        self.assertEqual((r20["peace"]["n"], r20["peace"]["p"]), (656, round(54 / 656, 3)))  # the 2 'stress' days before each start are not peace
        self.assertEqual((r20["neutral"]["n"], r20["neutral"]["p"]), (0, None))
        self.assertEqual(st["durations"], {"episodes": 3, "median_episode_days": 6.0, "median_calm_days": 195.0})
        mom = next(s for s in st["signals"] if s["key"] == "momentum")
        self.assertEqual(mom["h20"]["n"], 3)                                                # true on 7 days in a row, counted once per spike
        self.assertIs(mom["active"], False)
        self.assertIsNone(next(s for s in st["signals"] if s["key"] == "term_inversion")["active"])     # no VIX / VIX3M input
        self.assertFalse(st["state"]["in_episode"])
        self.assertEqual(st["state"]["to_stress"], 70.0)

    def test_inside_an_episode_nothing_is_active(self):
        st = CH.regime_study(self.build(tail=1))
        self.assertTrue(st["state"]["in_episode"])
        self.assertIsNone(st["state"]["to_stress"])
        self.assertEqual([s["active"] for s in st["signals"]], [None] * 4)

    def test_too_short_is_missing(self):
        self.assertEqual(CH.regime_study(frame([0.0] * 400)), {"status": "missing", "reason": "under 500 days with 4 or more components"})
        self.assertEqual(CH.regime_study(frame([0.0] * 900, used=3))["status"], "missing")


class Elections(unittest.TestCase):
    def test_next_election_by_tracking_window(self):
        e = CH._election(date(2026, 10, 10))
        self.assertEqual((e["date"], e["kind"]), ("2026-11-03", "midterm"))
        self.assertEqual(CH._election(date(2026, 12, 1))["date"], "2026-11-03")           # still inside the 60-day window
        e = CH._election(date(2027, 3, 1))
        self.assertEqual((e["date"], e["kind"]), ("2028-11-07", "presidential"))

    def test_markers(self):
        m = CH.election_markers(2019, date(2026, 10, 10))
        self.assertEqual([x["date"][:4] for x in m], ["2020", "2022", "2024", "2026"])
        self.assertEqual(m[0], {"date": "2020-11-03", "kind": "presidential"})
        self.assertEqual(CH.election_markers(2019, date(2026, 11, 4))[-1]["date"], "2028-11-07")

    def test_event_study_has_both_kinds_and_keeps_the_midterm_rows(self):
        def px(start, seed):
            idx = pd.bdate_range(start, "2024-12-31")
            return pd.Series(100 * np.exp(np.cumsum(np.random.default_rng(seed).normal(0.0003, 0.01, len(idx)))), index=idx)
        g, v = px("1950-01-02", 7), px("1990-01-02", 8)
        old = CH._event_study(g, v)
        new = CH._event_study(g, v, px("1993-01-04", 9) - 100)
        mid = [r for r in new["rows"] if r["kind"] == "midterm" and r["series"] != "Chaos / Peace score"]
        self.assertEqual([{k: x for k, x in r.items() if k not in ("kind", "unit")} for r in mid],
                         [{k: x for k, x in r.items() if k not in ("kind", "unit")} for r in old["rows"] if r["kind"] == "midterm"])
        self.assertEqual({r["kind"] for r in new["rows"]}, {"midterm", "presidential"})
        self.assertEqual({r["unit"] for r in new["rows"] if r["series"] == "Chaos / Peace score"}, {"pts"})
        self.assertEqual({r["unit"] for r in mid}, {"pct"})
        self.assertGreater(new["presidentials"], 5)
        self.assertGreater(new["midterms"], 5)


def lake_conn(test):
    tmp = tempfile.mkdtemp(prefix="chaos_")
    test.addCleanup(__import__("shutil").rmtree, tmp, True)
    conn = lake.connect(Path(tmp) / "portfolio.db")
    test.addCleanup(conn.close)
    lake.migrate(conn)
    lake.create_run(conn, "t", mode="offline", trigger="manual", run_folder="Oct-09-26", code_version="t")
    return conn


class Wiring(unittest.TestCase):
    def test_offline_empty_lake_has_a_missing_long_block_and_no_private_key(self):
        conn = lake_conn(self)
        c = collect.build_chaos(conn, SourceSession(conn, "t", offline=True), "t", TODAY, {})
        self.assertEqual(c["long"]["status"], "missing")
        self.assertNotIn("_long_frame", c)
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM v2_observations WHERE metric_id = 'chaos.score'").fetchone()[0], 0)
        json.dumps(c, allow_nan=False)

    def test_a_failure_in_the_long_block_leaves_the_rest(self):
        with mock.patch.object(CH, "long_history", side_effect=RuntimeError("boom")):
            c = CH.build({"today": TODAY, "series": inputs(), "long": {"series": inputs()}})
        self.assertEqual(c["status"], "fresh")
        self.assertEqual(c["long"], {"status": "missing", "reason": "RuntimeError: boom"})
        self.assertIsNone(c["_long_frame"])

    def test_scores_are_recorded_once_and_a_changed_day_is_a_second_row(self):
        conn = lake_conn(self)
        h = frame([10.0, 20.0, 30.0], end=date(2026, 10, 9))
        for _ in range(2):
            collect._record_chaos_scores(conn, "t", h)
        count = lambda: conn.execute("SELECT COUNT(*) FROM v2_observations WHERE metric_id = 'chaos.score'").fetchone()[0]
        self.assertEqual(count(), 3)
        h2 = h.copy()
        h2.iloc[-1, 0] = 35.0
        collect._record_chaos_scores(conn, "t", h2)
        self.assertEqual(count(), 4)
        collect._record_chaos_scores(conn, "t", h2)
        self.assertEqual(count(), 4)

    def test_fred_long_series_is_requested_at_full_size_and_reused(self):
        conn = lake_conn(self)
        payload = {"observations": [{"date": "2020-01-01", "value": "0"}, {"date": "2020-02-01", "value": "1"}, {"date": "2020-03-01", "value": "."}]}
        with mock.patch("requests.get", return_value=fake_get(payload=payload)) as g:
            a = src.fred_long_series(SourceSession(conn, "t"), "USREC", "k")
            b = src.fred_long_series(SourceSession(conn, "t2"), "USREC", "k")
        self.assertEqual(g.call_count, 1)
        self.assertEqual(g.call_args.kwargs["params"]["limit"], 100000)
        self.assertEqual(list(a.values), [0.0, 1.0])
        self.assertEqual(list(b.values), [0.0, 1.0])
        self.assertEqual(len(src.fred_long_series(SourceSession(conn, "t3", offline=True), "USREC", "")), 2)


class HistoryRoute(ApiCase):
    def rows(self, items):
        conn = lake.connect(self.tmp / "portfolio.db")
        lake.migrate(conn)
        lake.create_run(conn, "r1", mode="live", trigger="manual", run_folder="Oct-09-26", code_version="t")
        for i, (d, v, used, ver) in enumerate(items):      # fetched_at orders the rows of one date
            lake.record_observations(conn, [lake.obs("chaos.score", d, value=v, dims={"used": used}, source="derived", derivation=ver,
                                                     time_quality="as_of_date")], run_id="r1", fetched_at=f"2026-10-09T00:00:0{i}Z")
        conn.close()

    def test_503_404_and_success(self):
        self.assertEqual(self.client.get("/api/chaos/history").status_code, 503)
        self.rows([])
        self.assertEqual(self.client.get("/api/chaos/history").status_code, 404)
        self.rows([("2026-10-05", 10.0, 4, "chaos_v1"), ("2026-10-06", 20.0, 4, "chaos_v1"), ("2026-10-06", 22.5, 5, "chaos_v1"),
                   ("2026-10-07", 99.0, 4, "chaos_v0")])
        api = __import__("api_router")
        api.data_cache.cache.clear()
        d = self.get_json("/api/chaos/history")["data"]
        self.assertEqual((d["version"], d["n"], d["first"], d["last"]), ("chaos_v1", 2, "2026-10-05", "2026-10-06"))
        self.assertEqual(d["score"], [10.0, 22.5])                                         # the newest row of a date wins
        self.assertEqual(d["used"], [4, 5])
        self.assertEqual(d["t"], [(date(2026, 10, 5) - date(1970, 1, 1)).days, (date(2026, 10, 6) - date(1970, 1, 1)).days])
        json.dumps(d, allow_nan=False)
        self.assertEqual(self.get_json("/api/chaos/history?from=2026-10-06")["data"]["n"], 1)
        self.assertEqual(self.client.get("/api/chaos/history?from=nope").status_code, 400)
