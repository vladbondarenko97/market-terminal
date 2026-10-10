"""News lake and calendar dates: the RSS fetcher, headline clustering, the snapshot's news section, the stored
/api/macro_news route, the report block and the release-day rows of Forecast Lab card 7. All offline."""
import sys, unittest
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent))
import support  # noqa: F401  (temporary CME_Data, no credentials; must come before importing config)

import json
import shutil
import tempfile
import xml.etree.ElementTree as ET
from datetime import date, datetime, timedelta, timezone
from unittest import mock

import numpy as np
import pandas as pd

import config
from core import collect, forecast as F, lake, news, render
from core import sources as src
from core.sources import SourceSession, SourceUnavailable

sys.path.insert(0, str(support.ROOT / "options_whale"))
import api_router  # noqa: E402

RSS = support.FIX / "rss"
NOW = datetime(2026, 10, 10, 15, 0, tzinfo=timezone.utc)


def iso(hours_ago):
    return (NOW - timedelta(hours=hours_ago)).isoformat()


def item(title, url, source="wire", category="markets", hours_ago=1.0):
    return {"title": title, "url": url, "source": source, "category": category,
            "published_at": None if hours_ago is None else iso(hours_ago)}


class Db(unittest.TestCase):
    def setUp(self):
        tmp = Path(tempfile.mkdtemp(prefix="news_test_"))
        self.addCleanup(shutil.rmtree, tmp, True)
        self.path = tmp / "portfolio.db"
        self.conn = lake.connect(self.path)
        self.addCleanup(self.conn.close)
        lake.migrate(self.conn)
        self.tmp = tmp


class RssFetcher(Db):
    def get(self, fixture, status=200):
        return mock.Mock(status_code=status, content=(RSS / fixture).read_bytes() if fixture else b"")

    def fetch(self, fixture, status=200):
        s = SourceSession(self.conn, "t")
        with mock.patch("requests.get", return_value=self.get(fixture, status)) as g:
            out = src.rss_feed(s, "wire", "https://example.test/feed")
        self.assertIn("User-Agent", g.call_args.kwargs["headers"])
        self.assertEqual(g.call_args.kwargs["timeout"], 15)
        return out

    def test_rss_two_point_zero_keeps_titles_links_dates_and_never_invents_a_date(self):
        items, pid = self.fetch("sample_rss.xml")
        self.assertIsNotNone(pid)
        self.assertEqual([i["url"] for i in items], ["https://example.test/a1", "https://example.test/a2", "https://example.test/a3"])
        self.assertEqual(items[0]["published_at"], "2026-10-10T14:00:00+00:00")
        self.assertIsNone(items[2]["published_at"])                       # no date in the feed: None, not now
        self.assertEqual(set(items[0]), {"title", "url", "published_at"})  # no summary or body is kept

    def test_atom_is_parsed(self):
        items, _ = self.fetch("sample_atom.xml")
        self.assertEqual(items, [{"title": "Treasury yields edge higher", "url": "https://example.test/b1",
                                  "published_at": "2026-10-10T15:00:00+00:00"}])

    def test_a_malformed_feed_and_a_404_raise(self):
        with self.assertRaises(SourceUnavailable) as e:
            self.fetch("malformed.xml")
        self.assertEqual(e.exception.outcome, "invalid")
        with self.assertRaises(SourceUnavailable) as e:
            self.fetch(None, status=404)
        self.assertEqual(e.exception.outcome, "http_404")


class Clustering(unittest.TestCase):
    def test_dedup_merges_near_identical_titles_and_keeps_every_url(self):
        out = news.dedup([item("Fed holds rates steady, signals patience", "https://a/1", "bloomberg_markets", hours_ago=3),
                          item("Fed holds rates steady and signals patience on cuts", "https://b/1", "wsj_markets", hours_ago=2),
                          item("Oil falls on supply news", "https://c/1")])
        self.assertEqual(len(out), 2)
        fed = next(c for c in out if "Fed" in c["title"])
        self.assertEqual({s["url"] for s in fed["sources"]}, {"https://a/1", "https://b/1"})
        self.assertEqual(fed["title"], "Fed holds rates steady and signals patience on cuts")   # longest
        self.assertEqual(fed["published_at"], iso(2))                                           # latest
        self.assertEqual(len(fed["cluster_key"]), 12)
        self.assertIsInstance(fed["sentiment"], float)

    def test_an_official_item_makes_the_cluster_official(self):
        out = news.dedup([item("Fed announces new facility terms", "https://a/1", category="markets"),
                          item("Fed announces new facility terms today", "https://f/1", "fed_press", "official")])
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0]["category"], "official")

    def test_window_excludes_and_counts_undated_items(self):
        kept, unknown = news.window([item("a one", "u1", hours_ago=1), item("b two", "u2", hours_ago=40),
                                     item("c three", "u3", hours_ago=None)], NOW, 36)
        self.assertEqual([i["url"] for i in kept], ["u1"])
        self.assertEqual(unknown, 1)
        self.assertEqual(len(news.window([item("b two", "u2", hours_ago=40)], NOW, 168)[0]), 1)

    def test_bucket_is_deterministic_and_never_truncates_the_priority_category(self):
        rows = [dict(news.dedup([item(f"Official notice number {i} alpha{i}", f"https://o/{i}", "fed_press", "official", i / 10)])[0])
                for i in range(100)]
        rows += [news.dedup([item(f"Oil market headline {i} beta{i}", f"https://m/{i}", hours_ago=i / 10)])[0] for i in range(90)]
        rows += [news.dedup([item(f"Local bakery opens branch {i} gamma{i}", f"https://x/{i}", hours_ago=i / 10)])[0] for i in range(50)]
        a, b = news.bucket(rows), news.bucket(list(reversed(rows)))
        self.assertEqual([c["cluster_key"] for c in a], [c["cluster_key"] for c in b])
        self.assertEqual(sum(c["category"] == "official" for c in a), 100)
        self.assertEqual(len(a), 100 + news.RELEVANT_CAP + news.OTHER_CAP)


class FakeRegistry(Db):
    FEEDS = [("fed_press", "official"), ("sec_press", "official"), ("wsj", "markets"), ("cnbc", "markets")]

    def setUp(self):
        super().setUp()
        reg = self.tmp / "feeds.json"
        reg.write_text(json.dumps([{"name": n, "category": c, "url": f"https://example.test/{n}", "enabled": True}
                                   for n, c in self.FEEDS] + [{"name": "off", "category": "crypto", "url": "https://x", "enabled": False}]))
        self.patch_registry(reg)
        self.run_row = {"run_id": "r1", "generated_at": NOW.isoformat()}

    def patch_registry(self, path):
        p = mock.patch.object(config, "NEWS_FEEDS_FILE", str(path))
        p.start()
        self.addCleanup(p.stop)

    def collect(self, down=(), run_id="r1", session=None):
        def rss(s, name, url):
            if name in down:
                raise SourceUnavailable("rss", "http_404", name)
            return [{"title": f"{name} headline about rates", "url": f"https://example.test/{name}/1", "published_at": iso(2)},
                    {"title": f"{name} undated note", "url": f"https://example.test/{name}/2", "published_at": None}], f"p-{name}"
        run = dict(self.run_row, run_id=run_id)
        with mock.patch.object(src, "rss_feed", rss):
            return collect.collect_news(self.conn, session or SourceSession(self.conn, run_id), run, NOW)

    def test_all_answer_is_fresh_with_counts(self):
        n = self.collect()
        self.assertEqual(n["status"], "fresh")
        self.assertEqual(n["coverage"], {"enabled": 4, "answered": 4, "priority_answered": 2})
        self.assertEqual(n["items"]["fetched"], 8)
        self.assertEqual(n["items"]["unknown_date"], 4)
        self.assertEqual(n["items"]["in_window"], 4)
        self.assertEqual(n["feeds"]["wsj"]["status"], "fresh")
        self.assertTrue(all(c["new"] for c in n["clusters"]))
        self.assertEqual([c["id"] for c in n["clusters"]], [f"S{i}" for i in range(1, len(n["clusters"]) + 1)])
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM v2_news_items").fetchone()[0], 8)   # undated ones are stored too
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM v2_news_items WHERE published_at IS NULL").fetchone()[0], 4)

    def test_one_404_feed_is_missing_and_the_rule_decides_the_section_status(self):
        n = self.collect(down={"cnbc"})
        self.assertEqual(n["feeds"]["cnbc"]["status"], "missing")
        self.assertIn("http_404", n["feeds"]["cnbc"]["reason"])
        self.assertEqual(n["status"], "fresh")                       # an official feed answered and 3 of 4 did
        n = self.collect(down={"fed_press", "sec_press", "wsj"}, run_id="r2")
        self.assertEqual(n["status"], "partial")                     # only a markets feed answered: no priority feed
        self.assertEqual(n["coverage"], {"enabled": 4, "answered": 1, "priority_answered": 0})
        n = self.collect(down={"wsj", "cnbc", "sec_press"}, run_id="r3")
        self.assertEqual(n["status"], "partial")                     # priority answered but under half of the feeds did
        self.assertEqual(self.collect(down={"fed_press", "sec_press", "wsj", "cnbc"}, run_id="r4")["status"], "missing")

    def test_offline_session_gives_missing_with_the_offline_reason(self):
        n = collect.collect_news(self.conn, SourceSession(self.conn, "r1", offline=True), self.run_row, NOW)
        self.assertEqual(n["status"], "missing")
        self.assertIn("offline", n["reason"])
        self.assertEqual(n["clusters"], [])
        self.assertEqual(n["coverage"]["answered"], 0)

    def test_a_second_run_adds_no_rows_and_marks_nothing_new(self):
        first, second = self.collect(run_id="r1"), self.collect(run_id="r2")
        self.assertTrue(first["items"]["new_this_run"] and not second["items"]["new_this_run"])
        self.assertFalse(any(c["new"] for c in second["clusters"]))
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM v2_news_items").fetchone()[0], 8)
        self.assertEqual(self.conn.execute("SELECT DISTINCT first_seen_run FROM v2_news_items").fetchone()[0], "r1")

    def test_missing_or_invalid_registry_is_a_missing_section_not_a_failure(self):
        for body in (None, "{not json"):
            bad = self.tmp / "bad.json"
            if body:
                bad.write_text(body)
            self.patch_registry(bad)
            n = self.collect()
            self.assertEqual(n["status"], "missing")
            self.assertIn("feed list unusable", n["reason"])


class StoredRoute(Db):
    def snapshot(self, news_block):
        lake.create_run(self.conn, "r1", mode="live", trigger="manual", run_folder="Oct-10-26", code_version="t")
        lake.commit_snapshot(self.conn, "r1", {"run": {"run_id": "r1", "generated_at": NOW.isoformat()}, "news": news_block})
        lake.update_run(self.conn, "r1", status="completed")

    def get(self):
        with mock.patch.object(api_router, "_V2_DB_PATH", str(self.path)), \
                mock.patch.object(api_router.requests, "get", side_effect=AssertionError("network")), \
                mock.patch("requests.get", side_effect=AssertionError("network")):
            return api_router.app.test_client().get("/api/macro_news")

    def test_answers_from_the_stored_snapshot_in_the_old_shape_plus_coverage(self):
        clusters = [{"id": f"S{i}", "title": f"Headline {i}", "published_at": iso(i), "category": "markets", "official": False,
                     "sentiment": 0.1, "new": False, "cluster_key": f"k{i}", "sources": [{"name": "wsj", "url": f"https://e.test/{i}"}]}
                    for i in range(1, 41)]
        self.snapshot({"status": "partial", "reason": "x", "as_of": NOW.isoformat(), "clusters": clusters,
                       "coverage": {"enabled": 15, "answered": 9, "priority_answered": 2}})
        r = self.get()
        self.assertEqual(r.status_code, 200)
        root = ET.fromstring(r.get_data())
        self.assertEqual(root.tag, "macro_news")
        self.assertEqual((root.get("source"), root.get("status"), root.get("feeds_answered"), root.get("feeds_enabled")),
                         ("stored", "partial", "9", "15"))
        self.assertEqual(root.get("as_of"), NOW.isoformat())
        arts = root.findall("article")
        self.assertEqual(len(arts), 30)
        self.assertEqual(set(arts[0].attrib), {"published", "title", "link", "sentiment", "source", "official"})
        self.assertEqual(arts[0].get("title"), "Headline 1")

    def test_missing_news_section_is_an_error_with_the_stored_reason(self):
        self.snapshot({"status": "missing", "reason": "rss: offline network disabled for this run", "clusters": []})
        r = self.get()
        self.assertEqual(r.status_code, 503)
        self.assertIn("offline network disabled", ET.fromstring(r.get_data()).text)

    def test_snapshot_without_a_news_key(self):
        self.snapshot(None)
        self.assertEqual(self.get().status_code, 503)


class ReportBlock(unittest.TestCase):
    def cluster(self, i, official=True, new=False):
        return {"id": f"S{i}", "title": f"Notice {i}", "published_at": iso(i), "category": "official" if official else "markets",
                "official": official, "new": new, "sentiment": 0, "cluster_key": f"k{i}",
                "sources": [{"name": "fed_press", "url": f"https://e.test/{i}"}]}

    def test_prints_only_official_clusters_new_first_and_at_most_fifteen(self):
        cl = [self.cluster(i, new=(i == 20)) for i in range(1, 21)] + [self.cluster(99, official=False)]
        out = render.headlines_text({"news": {"status": "fresh", "clusters": cl, "coverage": {"enabled": 5, "answered": 4, "priority_answered": 2}}})
        lines = [ln for ln in out.splitlines() if "| fed_press |" in ln]
        self.assertEqual(len(lines), 15)
        self.assertIn("Notice 20", lines[0])                       # first seen this run, listed first
        self.assertNotIn("Notice 99", out)
        self.assertIn("feeds answered 4 of 5", out)

    def test_one_line_fallback_without_official_clusters_or_without_a_news_key(self):
        missing = render.headlines_text({"news": {"status": "missing", "reason": "rss: offline network disabled for this run",
                                                  "clusters": [], "coverage": {"enabled": 15, "answered": 0, "priority_answered": 0}}})
        self.assertEqual(len(missing.splitlines()), 1)
        self.assertIn("offline", missing)
        self.assertIn("answered 0 of 15", missing)
        self.assertEqual(len(render.headlines_text({}).splitlines()), 1)                        # an old snapshot replays
        self.assertEqual(len(render.headlines_text({"news": {"status": "fresh", "clusters": [self.cluster(1, official=False)]}}).splitlines()), 1)


def _bars(n=900, end="2026-10-09", seed=3):
    rng = np.random.default_rng(seed)
    close = 100 * np.exp(np.cumsum(rng.normal(0.0003, 0.01, n)))
    return pd.DataFrame({"Close": close}, index=pd.bdate_range(end=end, periods=n))


class CalendarDates(unittest.TestCase):
    def test_release_day_rows_carry_n_t_and_a_grade(self):
        df = _bars()
        days = [d.date().isoformat() for d in df.index[60::20]]
        out = F.calendar_effects(df, [], [], date(2026, 10, 12), {"payrolls": days, "ppi": days[:3]})
        row = out["effects"]["payrolls_day"]
        self.assertGreater(row["n"], 20)
        self.assertIn(row["grade"], ("tested", "thin", "none"))
        self.assertIn("t_vs_baseline", row)
        self.assertEqual(out["effects"]["ppi_day"]["n"], 3)
        self.assertEqual(out["effects"]["ppi_day"]["grade"], "none")       # under 8 cases: no grade
        for k in ("gdp_day", "pce_day", "retail_sales_day", "fomc_minutes_day"):
            self.assertEqual(out["effects"][k]["status"], "missing", k)

    def test_minutes_rows_come_from_the_decision_dates_and_existing_rows_are_unchanged(self):
        df = _bars()
        decisions = [d.date().isoformat() for d in df.index[50:-30:25]]
        base = F.calendar_effects(df, decisions, [], date(2026, 10, 12))
        more = F.calendar_effects(df, decisions, [], date(2026, 10, 12), {"gdp": decisions})
        self.assertGreater(base["effects"]["fomc_minutes_day"]["n"], 5)
        self.assertEqual(more["effects"]["gdp_day"]["n"], len(decisions))
        for k in ("fomc_day", "day_before_fomc", "cpi_day", "opex_week", "turn_of_month"):
            self.assertEqual(base["effects"][k], more["effects"][k])
        self.assertEqual(base["upcoming_7d"], more["upcoming_7d"])

    def test_build_works_without_the_new_inputs(self):
        out = F.build({"today": date(2026, 10, 9), "frames": {}, "spot": {}, "chains": {}, "gex": {}, "fred": {}, "cot": {},
                       "slv_trust": None, "slv_trust_history": [], "fomc": None, "cpi": None, "aum": {}})
        self.assertEqual(out["version"], "forecast_v2")
        self.assertEqual(out["SPY"]["calendar"]["status"], "missing")


class ReleaseCalendar(Db):
    def test_far_dated_releases_go_to_a_sibling_key_and_minutes_are_marked_as_a_rule(self):
        today = date(2026, 10, 10)
        dates = {10: ["2026-10-14", "2026-11-12", "2026-12-20"], 50: ["2026-11-06"]}
        with mock.patch.object(src, "fred_release_dates", lambda s, rid, key, start: (dates.get(rid, []), "p")), \
                mock.patch.object(src, "fomc_calendar", lambda s: (["2026-10-28", "2026-12-09"], "p")):
            rows, meta = collect.collect_releases(SourceSession(self.conn, "t"), today, "2026-10-17")
        by = {(r["release"], r["date_iso"]): r for r in rows}
        self.assertNotIn(("cpi", "2026-10-14"), by)                   # inside the Forex Factory coverage
        self.assertNotIn(("cpi", "2026-12-20"), by)                   # beyond 45 days
        self.assertEqual(by[("cpi", "2026-11-12")]["source"], "fred")
        self.assertEqual(by[("payrolls", "2026-11-06")]["country"], "USD")
        minutes = by[("fomc_minutes", "2026-11-18")]
        self.assertEqual((minutes["source"], minutes["title"]), ("fed_rule", collect.MINUTES_TITLE))
        self.assertEqual(meta["status"], "fresh")

    def test_offline_or_without_a_key_the_release_part_is_missing_with_a_reason(self):
        rows, meta = collect.collect_releases(SourceSession(self.conn, "t", offline=True), date(2026, 10, 10), "2026-10-17")
        self.assertEqual((rows, meta["status"]), ([], "missing"))
        self.assertIn("offline", meta["reason"])


if __name__ == "__main__":
    unittest.main()
