"""Phase 4: the shared daily-fetch helper and the chaos backfill script, the Fed speech days, and SEC Form 4 (fetchers, the Form 4 backfill
script and the insider cluster edge). Everything runs on saved or synthetic responses with requests.get mocked."""
import sys, unittest
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent))
import support  # noqa: F401  (temporary CME_Data, no credentials; must come before importing config)

import contextlib
import io
import json
import shutil
import tempfile
from datetime import date, timedelta
from types import SimpleNamespace
from unittest import mock

import numpy as np
import pandas as pd

import config
from core import backfill, chaos as CH, collect, edges, forecast, lake, sources as src
from core.runlock import EXIT_BUSY, RunLock
from core.sources import SourceSession, SourceUnavailable

sys.path.insert(0, str(support.ROOT / "scripts"))
import backfill_chaos_history, backfill_form4  # noqa: E402

FIX = support.FIX / "edgar"
TODAY = date(2026, 10, 9)


def fake_get(status=200, content=b""):
    return SimpleNamespace(status_code=status, content=content, text=content.decode("utf-8", "replace"), json=lambda: None)


class Lake(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="p4_"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.conn = lake.connect(self.tmp / "portfolio.db")
        self.addCleanup(self.conn.close)
        lake.migrate(self.conn)
        lake.create_run(self.conn, "t", mode="live", trigger="manual", run_folder="x", code_version="t")

    def session(self, offline=False, run_id="t"):
        return SourceSession(self.conn, run_id, offline=offline)

    def script(self, module, *args):
        """Run a script's main() on this test's database; returns (exit code, printed text)."""
        out = io.StringIO()
        with mock.patch.object(config, "DB_PATH", self.tmp / "portfolio.db"), contextlib.redirect_stdout(out):
            return module.main(list(args)), out.getvalue()


class ChaosBackfill(Lake):
    def fake_fetchers(self, no_file=()):
        calls = []

        def fetch(s, d):
            calls.append(d)
            if str(d) in no_file:
                raise SourceUnavailable("cboe", "http_403", str(d))
            return 0.5 + (d.toordinal() % 10) / 100, None
        return calls, mock.patch.dict(collect.CHAOS_DAILY, {"PUTCALL": ("chaos.equity_put_call", "cboe", fetch),
                                                           "SHORTVOL": ("chaos.short_volume_spy", "finra", fetch)})

    def test_helper_stores_one_observation_a_day_and_skips_stored_days(self):
        calls, patch = self.fake_fetchers(no_file={"2026-10-07"})
        days = [date(2026, 10, 8), date(2026, 10, 7), date(2026, 10, 6)]
        with patch:
            r = collect.fetch_daily_days(self.conn, self.session(), "t", "PUTCALL", days)
            self.assertEqual((r["stored"], r["skipped"], r["no_file"], r["failed"]), (2, 0, 1, 0))
            self.assertEqual(list(r["errors"]), ["cboe:2026-10-07"])
            r = collect.fetch_daily_days(self.conn, self.session(), "t", "PUTCALL", days)
        self.assertEqual((r["stored"], r["skipped"], r["no_file"]), (0, 2, 1))
        self.assertEqual(len(calls), 4)                                   # the 403 day is asked again, the stored ones are not
        rows = [x[0] for x in self.conn.execute("SELECT observed_at FROM v2_observations WHERE metric_id = 'chaos.equity_put_call' ORDER BY 1")]
        self.assertEqual(rows, ["2026-10-06", "2026-10-08"])

    def test_helper_limit_and_offline(self):
        calls, patch = self.fake_fetchers()
        with patch:
            r = collect.fetch_daily_days(self.conn, self.session(), "t", "PUTCALL", [date(2026, 10, 8), date(2026, 10, 7), date(2026, 10, 6)], limit=2)
        self.assertEqual((r["stored"], len(calls)), (2, 2))
        off = mock.patch.dict(collect.CHAOS_DAILY, {"PUTCALL": ("chaos.equity_put_call", "cboe", src.cboe_putcall)})
        with off, mock.patch("requests.get", side_effect=AssertionError("network")):
            r = collect.fetch_daily_days(self.conn, self.session(offline=True), "t", "SHORTVOL", [date(2026, 10, 5)])
        self.assertEqual(r["stored"], 0)

    def test_three_years_of_observations_make_both_components_fresh(self):
        days = backfill_chaos_history.trading_days(TODAY, 3)
        self.assertEqual(days[0], date(2026, 10, 8))
        self.assertGreaterEqual((days[0] - days[-1]).days, 3 * 365 - 30)
        _, patch = self.fake_fetchers()
        with patch:
            for name in ("PUTCALL", "SHORTVOL"):
                self.assertEqual(collect.fetch_daily_days(self.conn, self.session(), "t", name, days)["stored"], len(days))
        series = {}
        for name, (metric, _, _) in collect.CHAOS_DAILY.items():
            rows = self.conn.execute("SELECT observed_at, value_num FROM v2_observations WHERE metric_id = ?", (metric,)).fetchall()
            series[name] = pd.Series({pd.Timestamp(r[0][:10]): r[1] for r in rows}, dtype=float).sort_index()
        c = CH.build({"today": TODAY, "series": series})
        self.assertEqual(c["components"]["equity_put_call"]["status"], "fresh")
        self.assertEqual(c["components"]["short_volume_spy"]["status"], "fresh")

    def test_script_honours_limit_and_records_a_backfill_run(self):
        calls, patch = self.fake_fetchers(no_file={"2026-10-08"})
        with patch:
            code, out = self.script(backfill_chaos_history, "--limit", "3", "--sleep", "0")
            self.assertEqual(code, 0)
            self.assertEqual(len(calls), 6)                                # 3 per series
            self.assertIn("1 without a file", out)
            code, out = self.script(backfill_chaos_history, "--limit", "3", "--sleep", "0", "--years", "0.1")
        conn = lake.connect(self.tmp / "portfolio.db")
        runs = [dict(r) for r in conn.execute("SELECT mode, status FROM v2_runs WHERE mode = 'backfill'")]
        self.assertEqual(len(runs), 2)
        self.assertTrue(all(r["status"].startswith("completed") for r in runs))
        self.assertIsNone(conn.execute("SELECT * FROM v2_latest_snapshot").fetchone())     # a backfill run is never a latest snapshot
        conn.close()

    def test_script_exits_75_when_the_run_lock_is_held(self):
        lock = RunLock()
        self.assertTrue(lock.acquire())
        self.addCleanup(lock.release)
        with mock.patch("requests.get", side_effect=AssertionError("network")), contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(backfill_chaos_history.main(["--limit", "1"]), EXIT_BUSY)
            self.assertEqual(backfill.run("x", lambda *a: self.fail("work must not start")), EXIT_BUSY)


class FedSpeeches(Lake):
    BODY = ("﻿" + json.dumps([{"d": "10/8/2026 4:30:00 AM", "t": "Outlook", "s": "Governor Christopher J. Waller", "l": "/a.htm"},
                                   {"d": "9/30/2026 1:05:00 PM", "t": "Rates", "s": "President Some Person"},
                                   {"t": "undated entry"}, {"d": "1/4/2006", "t": "Old, date only as the real file has it", "s": "Chairman Ben S. Bernanke"}])).encode("utf-8")

    def test_fetcher_parses_a_bom_file_and_skips_undated_entries(self):
        with mock.patch("requests.get", return_value=fake_get(200, self.BODY)) as g:
            rows, pid = src.fed_speeches(self.session())
        self.assertIn("ne-speeches.json", g.call_args.args[0])
        self.assertTrue(pid)
        self.assertEqual([r["date"] for r in rows], ["2026-10-08", "2026-09-30", "2006-01-04"])
        self.assertEqual(rows[0], {"date": "2026-10-08", "speaker": "Governor Christopher J. Waller", "title": "Outlook"})

    def test_stored_capture_is_reused_for_seven_days_and_offline(self):
        with mock.patch("requests.get", return_value=fake_get(200, self.BODY)):
            src.fed_speeches(self.session())
        with mock.patch("requests.get", side_effect=AssertionError("network")):
            self.assertEqual(len(src.fed_speeches(self.session(run_id="t2"))[0]), 3)
            self.assertEqual(len(src.fed_speeches(self.session(offline=True, run_id="t3"))[0]), 3)
        old = (pd.Timestamp.now(tz="UTC") - pd.Timedelta(days=8)).isoformat()
        self.conn.execute("UPDATE v2_payloads SET fetched_at = ? WHERE kind = 'fed_speeches'", (old,))
        self.conn.commit()
        with mock.patch("requests.get", return_value=fake_get(200, self.BODY)) as g:
            src.fed_speeches(self.session(run_id="t4"))
        self.assertEqual(g.call_count, 1)                                  # older than 7 days: fetched again
        with mock.patch("requests.get", side_effect=AssertionError("network")):
            self.assertEqual(len(src.fed_speeches(self.session(offline=True, run_id="t5"))[0]), 3)
        empty = SourceSession(lake.connect(self.tmp / "e.db"), "x", offline=True)
        lake.migrate(empty.conn)
        with self.assertRaises(SourceUnavailable):
            src.fed_speeches(empty)

    def test_calendar_row_for_board_speech_days(self):
        rng = np.random.default_rng(5)
        ix = pd.bdate_range("2018-01-01", periods=900)
        df = pd.DataFrame({"Close": 100 * np.cumprod(1 + rng.normal(0, 0.01, len(ix)))}, index=ix)
        board = [str(d.date()) for d in ix[20::15]]
        speeches = [{"date": d, "speaker": "Governor Test Person", "title": "t"} for d in board] + \
                   [{"date": str(ix[30].date()), "speaker": "President Regional Bank", "title": "t"}]
        eff = forecast.calendar_effects(df, [], [], date(2021, 6, 1), None, speeches)["effects"]["fed_speaker_day"]
        self.assertEqual(eff["n"], len(board))
        self.assertIn("grade", eff)
        self.assertIn("Board member", eff["definition"])
        self.assertEqual(forecast.calendar_effects(df, [], [], date(2021, 6, 1))["effects"]["fed_speaker_day"]["status"], "missing")
        only_presidents = [{"date": board[0], "speaker": "President Regional Bank", "title": "t"}]
        self.assertEqual(forecast.calendar_effects(df, [], [], date(2021, 6, 1), None, only_presidents)["effects"]["fed_speaker_day"]["status"], "missing")


class Sec(Lake):
    def setUp(self):
        super().setUp()
        patch = mock.patch.object(config, "SEC_USER_AGENT", "Test Operator test@example.invalid")
        patch.start()
        self.addCleanup(patch.stop)

    def routes(self, **extra):
        """A requests.get stand-in answering by URL; records the headers."""
        table = {"company_tickers.json": (FIX / "company_tickers.json").read_bytes(),
                 "CIK0000123456.json": (FIX / "submissions.json").read_bytes(),
                 "CIK0000123456-submissions-001.json": (FIX / "submissions_old.json").read_bytes(),
                 "000012345626000001/doc4.xml": (FIX / "form4_officer_buy.xml").read_bytes(),
                 "000012345626000003/doc4.xml": (FIX / "form4_director_sale.xml").read_bytes()}
        table.update(extra)
        seen = []

        def get(url, **kw):
            seen.append((url, kw.get("headers")))
            for k, v in table.items():
                if url.endswith(k):
                    return fake_get(200, v)
            return fake_get(404)
        get.seen = seen
        return get

    def test_form4_parser(self):
        p = src.parse_form4((FIX / "form4_officer_buy.xml").read_bytes())
        self.assertEqual((p["issuer_cik"], p["owner"], p["is_officer"], p["is_director"], p["officer_title"]),
                         ("0000123456", "Doe Jane", True, False, "Chief Financial Officer"))
        self.assertEqual(len(p["transactions"]), 2)                       # the derivative row is ignored
        self.assertEqual(p["transactions"][0], {"date": "2026-03-02", "code": "P", "acquired": True, "shares": 1000.0, "price": 12.5})
        self.assertEqual((p["transactions"][1]["code"], p["transactions"][1]["acquired"], p["transactions"][1]["price"]), ("S", False, None))
        d = src.parse_form4((FIX / "form4_director_sale.xml").read_bytes())
        self.assertEqual((d["is_officer"], d["is_director"], d["officer_title"]), (False, True, None))
        with self.assertRaises(SourceUnavailable):
            src.parse_form4(b"<not xml")

    def test_form4_fetch_strips_the_rendered_view_folder_and_stores_the_xml(self):
        get = self.routes()
        s = self.session()
        with mock.patch("requests.get", get):
            parsed, pid = src.sec_form4(s, "0000123456", "0000123456-26-000001", "xslF345X05/doc4.xml")
        self.assertEqual(get.seen[0][0], "https://www.sec.gov/Archives/edgar/data/123456/000012345626000001/doc4.xml")
        self.assertEqual(get.seen[0][1]["User-Agent"], "Test Operator test@example.invalid")
        self.assertEqual(parsed["owner"], "Doe Jane")
        self.assertIn(b"<ownershipDocument>", lake.load_payload(self.conn, pid)[1])

    def test_submissions_parser_returns_form_4_only_and_reaches_back_only_when_needed(self):
        get = self.routes()
        with mock.patch("requests.get", get):
            rows, _ = src.sec_submissions(self.session(), "123456", "2026-01-01")
            self.assertEqual([r["accession"] for r in rows], ["0000123456-26-000001", "0000123456-26-000003"])    # not the 8-K, the 4/A or 2019
            self.assertEqual(len(get.seen), 1)                                                                     # the old file was not needed
            rows, _ = src.sec_submissions(self.session(run_id="t2"), "123456", "2010-01-01")
        self.assertEqual([r["filing_date"] for r in rows], ["2017-05-05", "2019-01-10", "2026-03-03", "2026-04-02"])
        self.assertEqual(rows[2]["primary_document"], "xslF345X05/doc4.xml")

    def test_tickers_map_to_ten_digit_ciks_and_are_reused_for_30_days(self):
        with mock.patch("requests.get", self.routes()):
            t, _ = src.sec_company_tickers(self.session())
        self.assertEqual(t, {"EXMP": "0000123456", "OTH": "0000000987"})
        with mock.patch("requests.get", side_effect=AssertionError("network")):
            self.assertEqual(src.sec_company_tickers(self.session(run_id="t2"))[0], t)

    def test_unset_user_agent_is_not_configured_and_makes_no_request(self):
        with mock.patch.object(config, "SEC_USER_AGENT", ""), mock.patch("requests.get", side_effect=AssertionError("network")):
            for call in (lambda s: src.sec_company_tickers(s), lambda s: src.sec_submissions(s, "1"), lambda s: src.sec_form4(s, "1", "0-0-1", "d.xml")):
                with self.assertRaises(SourceUnavailable) as cm:
                    call(self.session())
                self.assertEqual((cm.exception.source, cm.exception.outcome), ("sec", "not_configured"))
            code, out = self.script(backfill_form4, "--symbols", "EXMP")
        self.assertEqual(code, 2)
        self.assertIn("SEC_USER_AGENT", out)

    def test_script_stores_one_observation_per_filing_and_nothing_on_a_second_pass(self):
        get = self.routes()
        with mock.patch("requests.get", get):
            code, out = self.script(backfill_form4, "--symbols", "EXMP,SPY", "--years", "1", "--sleep", "0")
            self.assertEqual(code, 0, out)
            self.assertIn("SPY: no SEC CIK", out)
            first = len(get.seen)
            code, out = self.script(backfill_form4, "--symbols", "EXMP", "--years", "1", "--sleep", "0")
        self.assertEqual(len(get.seen) - first, 1)                          # only the submissions list is asked again
        self.assertIn("0 stored, 2 already stored", out)
        conn = lake.connect(self.tmp / "portfolio.db")
        rows = [dict(r) for r in conn.execute("SELECT entity, observed_at, dims_json, source, payload_id, value_json FROM v2_observations "
                                              "WHERE metric_id = 'edgar.form4' ORDER BY observed_at")]
        conn.close()
        self.assertEqual([(r["entity"], r["observed_at"]) for r in rows], [("0000123456-26-000001", "2026-03-03"), ("0000123456-26-000003", "2026-04-02")])
        self.assertEqual(json.loads(rows[0]["dims_json"]), {"symbol": "EXMP"})
        self.assertTrue(rows[0]["payload_id"] and rows[0]["source"] == "sec")
        self.assertEqual(json.loads(rows[0]["value_json"])["owner"], "Doe Jane")

    def test_script_limit(self):
        with mock.patch("requests.get", self.routes()):
            code, out = self.script(backfill_form4, "--symbols", "EXMP", "--years", "1", "--sleep", "0", "--limit", "1")
        self.assertIn("1 stored", out)
        self.assertIn("1 left for a later run", out)


def filing(day, owner, code="P", officer=True, acquired=True):
    return {"date": day, "owner": owner, "is_officer": officer, "transactions": [{"code": code, "acquired": acquired}]}


class InsiderEdge(unittest.TestCase):
    def test_cluster_needs_three_different_officers_buying_within_ten_days(self):
        clusters = lambda fs: edges.insider_clusters(fs)
        three = [filing("2026-03-02", "A"), filing("2026-03-05", "B"), filing("2026-03-09", "C")]
        self.assertEqual(clusters(three), ["2026-03-09"])
        self.assertEqual(clusters([filing("2026-03-02", "A"), filing("2026-03-05", "A"), filing("2026-03-09", "A")]), [])      # one officer
        self.assertEqual(clusters([filing("2026-03-02", "A"), filing("2026-03-05", "B"), filing("2026-03-12", "C")]), [])      # C is 10 days after A: outside the window
        self.assertEqual(clusters([filing("2026-03-02", "A"), filing("2026-03-05", "B"), filing("2026-03-11", "C")]), ["2026-03-11"])
        sales = [filing("2026-03-02", "A"), filing("2026-03-05", "B"), filing("2026-03-09", "C", code="S", acquired=False)]
        self.assertEqual(clusters(sales), [])
        self.assertEqual(clusters([filing("2026-03-02", "A"), filing("2026-03-05", "B"), {**filing("2026-03-09", "C"), "is_officer": False}]), [])
        # no re-trigger while the count stays at 3 or more, a new start after it fell below 3
        more = three + [filing("2026-03-10", "D"), filing("2026-03-12", "E")]
        self.assertEqual(clusters(more), ["2026-03-09"])
        again = three + [filing("2026-04-20", "A"), filing("2026-04-21", "B"), filing("2026-04-22", "C")]
        self.assertEqual(clusters(again), ["2026-03-09", "2026-04-22"])
        gap = three + [filing("2026-03-25", "A"), filing("2026-03-25", "B"), filing("2026-03-25", "D")]
        self.assertEqual(clusters(gap), ["2026-03-09", "2026-03-25"])      # the count fell to 0 in between, so this is a new cluster

    def planted(self, drift=0.006, n_clusters=16):
        rng = np.random.default_rng(11)
        ix = pd.bdate_range("2012-01-02", periods=3000)
        ret = rng.normal(0, 0.01, len(ix))
        fs, last = [], None
        for j in range(n_clusters):
            k = 100 + 180 * j
            for o, off in zip("ABC", (0, 1, 2)):
                fs.append(filing(str(ix[k + off].date()), "Officer " + o))
            ret[k + 4:k + 25] += drift
            last = k
        return pd.Series(100 * np.cumprod(1 + ret), index=ix), fs, last

    def test_edge_with_a_planted_forward_drift(self):
        c, fs, last = self.planted()
        e = edges._insider_cluster("XYZ", c, fs)
        self.assertEqual((e["key"], e["horizon"]), ("insider", "mid"))
        self.assertRegex(e["evidence"], r"n=\d+")
        self.assertRegex(e["evidence"], r"t \d")
        self.assertIn(e["edge"], ("tested", "thin"))
        self.assertIn("48 Form 4 filings stored through", e["now"])
        self.assertFalse(e["active"])                                      # the last cluster is long past
        late = fs + [filing(str(c.index[-3].date()), "X"), filing(str(c.index[-2].date()), "Y"), filing(str(c.index[-2].date()), "Z")]
        self.assertTrue(edges._insider_cluster("XYZ", c, late)["active"])

    def test_noise_gets_no_edge_and_no_data_is_unavailable_with_the_reason(self):
        c, fs, _ = self.planted(drift=0.0)
        self.assertEqual(edges._insider_cluster("XYZ", c, fs)["edge"], "none")
        for none in (None, []):
            e = edges._insider_cluster("XYZ", c, none)
            self.assertEqual((e["edge"], e["active"], e["now"]), ("n/a", False, "no Form 4 history stored"))
            self.assertEqual(e["evidence"], "no Form 4 history stored (run scripts/backfill_form4.py)")
        f = edges._insider_cluster("SPY", c, None, kind="ETF")
        self.assertEqual((f["edge"], f["now"]), ("n/a", "no officers: this is a fund"))

    def test_filings_are_read_from_the_lake_and_a_missing_database_is_none(self):
        tmp = Path(tempfile.mkdtemp(prefix="p4_"))
        self.addCleanup(shutil.rmtree, tmp, True)
        with mock.patch.object(edges, "DB_PATH", tmp / "none.db"):
            self.assertIsNone(edges._form4_filings("EXMP"))
        conn = sqlite3_connect(tmp / "portfolio.db")
        with mock.patch.object(edges, "DB_PATH", tmp / "portfolio.db"):
            self.assertIsNone(edges._form4_filings("EXMP"))                 # database without the table
            lake.migrate(conn)
            self.assertEqual(edges._form4_filings("EXMP"), [])
            lake.record_observations(conn, [lake.obs("edgar.form4", "2026-03-03", entity="a-1", dims={"symbol": "EXMP"}, value={"owner": "X", "is_officer": True, "transactions": []}, source="sec"),
                                            lake.obs("edgar.form4", "2026-03-04", entity="a-2", dims={"symbol": "OTHER"}, value={"owner": "Y"}, source="sec")])
            lake.record_observations(conn, [      # the company's own filing as the owner of ANOTHER issuer's shares, and its insiders' filings
                lake.obs("edgar.form4", "2026-03-05", entity="a-3", dims={"symbol": "EXMP"}, source="sec",
                         value={"owner": "Example Corp", "is_officer": True, "issuer_cik": "0000999999", "company_cik": "0000000123", "transactions": []}),
                lake.obs("edgar.form4", "2026-03-06", entity="a-4", dims={"symbol": "EXMP"}, source="sec",
                         value={"owner": "Z", "is_officer": True, "issuer_cik": "123", "company_cik": "0000000123", "transactions": []}),
                lake.obs("edgar.form4", "2026-03-07", entity="a-5", dims={"symbol": "OLD"}, source="sec", value={"owner": "A", "issuer_cik": "0000000555"}),
                lake.obs("edgar.form4", "2026-03-08", entity="a-6", dims={"symbol": "OLD"}, source="sec", value={"owner": "B", "issuer_cik": "0000000555"}),
                lake.obs("edgar.form4", "2026-03-09", entity="a-7", dims={"symbol": "OLD"}, source="sec", value={"owner": "Old Corp", "issuer_cik": "0000000777"})])
            self.assertEqual([f["accession"] for f in edges._form4_filings("EXMP")], ["a-1", "a-4"])       # a-3 is about another issuer
            self.assertEqual([f["accession"] for f in edges._form4_filings("OLD")], ["a-5", "a-6"])        # rows without company_cik: the usual issuer
            got = [f for f in edges._form4_filings("EXMP") if f["accession"] == "a-1"]
        conn.close()
        self.assertEqual([(f["accession"], f["date"], f["owner"]) for f in got], [("a-1", "2026-03-03", "X")])

    def test_verdict_and_card_accept_an_eighth_edge(self):
        c, fs, _ = self.planted()
        late = fs + [filing(str(c.index[-3].date()), "X"), filing(str(c.index[-2].date()), "Y"), filing(str(c.index[-2].date()), "Z")]
        e = edges._insider_cluster("XYZ", c, late)
        v = edges._verdict([e])
        self.assertEqual(set(v), {"short", "mid", "long"})
        if e["edge"] in ("tested", "thin"):
            self.assertEqual(v["mid"]["bias"], "bullish")


def sqlite3_connect(path):
    return lake.connect(path)


if __name__ == "__main__":
    unittest.main()
