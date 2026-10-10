"""Offline tests for core storage, CME acquisition, engine positions and the Forecast Lab models.

Every test builds its own small SQLite file or synthetic series; none touches the network, credentials or the shared
test installation's database. Run with the rest:  python -m unittest discover -s tests
"""
import sys, unittest
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent))
import support  # noqa: F401  (temporary CME_Data, no credentials; must come before importing config)

import json
import shutil
import sqlite3
import tempfile
import threading
import time
from datetime import date, datetime, timedelta, timezone
from unittest import mock

import numpy as np
import pandas as pd

from core import cme, collect, forecast as F, importer, lake, positions, refining as RF, render
from core.sources import SourceSession

OLD_VIEW = """CREATE VIEW v2_latest_snapshot AS
        SELECT s.* FROM v2_snapshots s JOIN v2_runs r ON r.run_id = s.run_id
        WHERE r.status IN ('committed', 'completed', 'completed_with_warnings')
        ORDER BY s.created_at DESC LIMIT 1"""


class TempDb(unittest.TestCase):
    """Base class: `self.new_db()` gives a migrated scratch database, removed after the test."""

    def new_db(self, migrate=True):
        tmp = Path(tempfile.mkdtemp(prefix="a2test_"))
        self.addCleanup(shutil.rmtree, tmp, True)
        path = tmp / "portfolio.db"
        conn = lake.connect(path)
        self.addCleanup(conn.close)
        if migrate:
            lake.migrate(conn)
        return conn, path

    @staticmethod
    def add_run(conn, run_id, mode, status, created_at, ctx=None):
        """A run with a snapshot whose created_at is set by hand (the view orders by it)."""
        lake.create_run(conn, run_id, mode=mode, trigger="manual", run_folder="Oct-06-26", code_version="t")
        lake.update_run(conn, run_id, status=status)
        body = lake.dumps(ctx if ctx is not None else {"run": {"run_id": run_id}})
        with conn:
            conn.execute("INSERT INTO v2_snapshots (run_id, created_at, schema_version, context_json, context_sha256) "
                         "VALUES (?,?,?,?,?)", (run_id, created_at, lake.SCHEMA_VERSION, body,
                                                lake.sha256_bytes(body.encode())))


# ================================================================== lake: view, trigger, read-only connection
class LakeSemantics(TempDb):
    def latest(self, conn):
        row = conn.execute("SELECT run_id FROM v2_latest_snapshot").fetchone()
        return row["run_id"] if row else None

    def test_latest_snapshot_prefers_newest_live_over_newer_offline(self):
        conn, _ = self.new_db()
        self.add_run(conn, "live-old", "live", "completed", "2026-10-05T14:00:00+00:00")
        self.add_run(conn, "live-new", "live", "completed_with_warnings", "2026-10-06T14:00:00+00:00")
        self.add_run(conn, "offline-newest", "offline", "completed", "2026-10-07T14:00:00+00:00")
        self.assertEqual(self.latest(conn), "live-new")
        self.assertEqual(lake.load_snapshot(conn)["run"]["run_id"], "live-new")     # what the shims and replay read

    def test_latest_snapshot_falls_back_to_offline_only_without_a_live_one(self):
        conn, _ = self.new_db()
        self.assertIsNone(self.latest(conn))
        self.add_run(conn, "offline-1", "offline", "completed", "2026-10-06T14:00:00+00:00")
        self.add_run(conn, "offline-2", "offline", "completed", "2026-10-07T14:00:00+00:00")
        self.add_run(conn, "live-failed", "live", "failed", "2026-10-08T14:00:00+00:00")   # not committed: ignored
        self.assertEqual(self.latest(conn), "offline-2")
        self.add_run(conn, "live-ok", "live", "committed", "2026-10-04T14:00:00+00:00")     # older, but live
        self.assertEqual(self.latest(conn), "live-ok")

    def test_migrate_replaces_the_old_view_once_and_keeps_the_data(self):
        conn, _ = self.new_db(migrate=False)
        for stmt in lake.MIGRATIONS:
            conn.execute(stmt)
        conn.execute("DROP VIEW IF EXISTS v2_latest_snapshot")
        conn.execute(OLD_VIEW)
        self.add_run(conn, "r1", "offline", "completed", "2026-10-06T14:00:00+00:00")
        lake.migrate(conn)
        sql = conn.execute("SELECT sql FROM sqlite_master WHERE name = 'v2_latest_snapshot'").fetchone()[0]
        self.assertIn("r.mode = 'live'", sql)
        lake.migrate(conn)                                          # second call: same definition, nothing to do
        self.assertEqual(conn.execute("SELECT sql FROM sqlite_master WHERE name = 'v2_latest_snapshot'").fetchone()[0], sql)
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM v2_snapshots").fetchone()[0], 1)
        self.assertEqual(self.latest(conn), "r1")

    def test_snapshot_rows_cannot_be_updated_or_deleted(self):
        conn, _ = self.new_db()
        self.add_run(conn, "r1", "live", "completed", "2026-10-06T14:00:00+00:00")
        with self.assertRaises(sqlite3.IntegrityError):
            conn.execute("UPDATE v2_snapshots SET context_json = '{}' WHERE run_id = 'r1'")
        with self.assertRaises(sqlite3.IntegrityError) as cm:
            conn.execute("DELETE FROM v2_snapshots WHERE run_id = 'r1'")
        self.assertIn("cannot be deleted", str(cm.exception))
        with self.assertRaises(sqlite3.IntegrityError):
            conn.execute("DELETE FROM v2_snapshots")
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM v2_snapshots").fetchone()[0], 1)

    def test_connect_readonly_reads_but_cannot_write_or_create(self):
        conn, path = self.new_db()
        self.add_run(conn, "r1", "live", "completed", "2026-10-06T14:00:00+00:00")
        ro = lake.connect_readonly(path)
        self.addCleanup(ro.close)
        self.assertEqual(ro.execute("SELECT run_id FROM v2_latest_snapshot").fetchone()["run_id"], "r1")   # Row factory
        with self.assertRaises(sqlite3.OperationalError):
            ro.execute("CREATE TABLE nope (a)")
        with self.assertRaises(sqlite3.OperationalError):
            lake.connect_readonly(path.parent / "missing.db")
        self.assertFalse((path.parent / "missing.db").exists())


# ================================================================== SourceSession concurrency
class SourceSessionLimits(TempDb):
    def test_unknown_provider_requests_are_serialised_and_share_one_semaphore(self):
        conn, _ = self.new_db()
        s = SourceSession(conn, "t")
        self.assertNotIn("ishares", s._sem)
        sem = s._semaphore("ishares")
        self.assertIs(s._semaphore("ishares"), sem)                  # the same object on every call
        self.assertIsNot(s._semaphore("federalreserve"), sem)       # but one per provider
        running, peak, lock = [0], [0], threading.Lock()

        def slow():
            with lock:
                running[0] += 1
                peak[0] = max(peak[0], running[0])
            time.sleep(0.05)
            with lock:
                running[0] -= 1
            return "x"
        threads = [threading.Thread(target=s.fetch, args=("ishares", {"n": i}, slow),
                                    kwargs=dict(kind="t", fmt="text", capture=lambda r: (b"x", None, None)))
                   for i in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(peak[0], 1, "an unlisted provider ran more than one request at a time")
        self.assertEqual(s.calls["ishares"], 4)

    def test_listed_provider_keeps_its_own_limit(self):
        conn, _ = self.new_db()
        s = SourceSession(conn, "t")
        self.assertEqual(s._semaphore("yahoo")._value, 2)


# ================================================================== CME acquisition
DENIED = cme.AcquisitionResult("access_denied", http_status=403, detail="denied", url="u", attempts=1)


class CmeAcquisition(TempDb):
    def _data_dir(self):
        data_dir = Path(tempfile.mkdtemp(prefix="a2cme_"))
        self.addCleanup(shutil.rmtree, data_dir, True)
        return data_dir

    def test_no_cme_browser_keeps_chromium_closed(self):
        conn, _ = self.new_db()
        boom = AssertionError("browser opened with --no-cme-browser")
        with mock.patch.object(cme, "browser_fetch_volume_listing", side_effect=boom) as listing, \
                mock.patch.object(cme, "browser_fetch", side_effect=boom) as fetch, \
                mock.patch.object(cme, "fetch_bounded", return_value=DENIED):
            out = collect.acquire_cme(conn, SourceSession(conn, "t"), "t", self._data_dir(), offline=False,
                                      use_browser=False, max_volume_files=10)
        listing.assert_not_called()
        fetch.assert_not_called()
        self.assertEqual(out["volume"], {"outcome": "skipped", "detail": "browser disabled (--no-cme-browser)"})
        self.assertEqual(out["inventory"]["outcome"], "access_denied")      # plain HTTP only, no browser retry
        self.assertEqual(out["inventory"]["via"], "requests")

    def test_browser_is_used_when_allowed(self):
        conn, _ = self.new_db()
        with mock.patch.object(cme, "browser_fetch_volume_listing",
                               return_value=([], {"outcome": "ok", "requested": [], "listing_latest": "20261006"})) as listing, \
                mock.patch.object(cme, "browser_fetch", return_value=DENIED) as fetch, \
                mock.patch.object(cme, "fetch_bounded", return_value=DENIED):
            out = collect.acquire_cme(conn, SourceSession(conn, "t"), "t", self._data_dir(), offline=False,
                                      use_browser=True, max_volume_files=10)
        listing.assert_called_once()
        fetch.assert_called_once()
        self.assertEqual(out["volume"]["outcome"], "up_to_date")

    def test_pick_volume_files_fills_gaps_older_than_the_newest_held_date(self):
        listed = {d: f"https://x/daily_volume_{d}.xlsx" for d in
                  ("20260901", "20260902", "20260903", "20260904", "20260908", "20260909", "20260910")}
        have = {"20260908", "20260909", "20260910", "20260902"}        # newest dates held, an old gap at 09-01/03/04
        self.assertEqual(cme.pick_volume_files(listed, have, 10), ["20260904", "20260903", "20260901"])
        self.assertEqual(cme.pick_volume_files(listed, have, 2), ["20260904", "20260903"])      # still capped, newest first
        self.assertEqual(cme.pick_volume_files(listed, have, 10, earliest="20260903"), ["20260904", "20260903"])
        self.assertEqual(cme.pick_volume_files(listed, set(listed), 10), [])

    def test_acquire_passes_no_lower_bound_to_the_listing(self):
        conn, _ = self.new_db()
        recs = [lake.obs("cme.ES_F.volume", d, entity="ES_F", value=1000.0, source="t") for d in ("2026-09-10", "2026-09-11")]
        lake.record_observations(conn, recs)
        with mock.patch.object(cme, "browser_fetch_volume_listing",
                               return_value=([], {"outcome": "ok", "requested": [], "listing_latest": "20260914"})) as listing, \
                mock.patch.object(cme, "fetch_bounded", return_value=DENIED), \
                mock.patch.object(cme, "browser_fetch", return_value=DENIED):
            collect.acquire_cme(conn, SourceSession(conn, "t"), "t", self._data_dir(), offline=False, use_browser=True)
        args, kwargs = listing.call_args
        self.assertIsNone(kwargs.get("earliest"))
        have = args[1]
        self.assertIn("20260911", have)        # trade date 09-10 is published under file date 09-11
        self.assertIn("20260914", have)        # trade date 09-11 (a Friday) under Monday 09-14
        self.assertNotIn("20260909", have)

    def test_files_downloaded_before_are_never_requested_again(self):
        """A workbook stored under a different CME file date than the next weekday (a holiday, a weekend post) is
        remembered by its file name, so a duplicate download does not repeat on every run."""
        conn, _ = self.new_db()
        self.assertEqual(collect._volume_files_held(conn, {"20260904"}), {"20260907"})      # next weekday only
        lake.log_fetch(conn, run_id="t", source="cme_volume", request={"url": "https://x/daily_volume_20260908.xlsx"},
                       started_at="x", elapsed_ms=1, outcome="ok")
        lake.log_fetch(conn, run_id="t", source="cme_volume", request={"url": "https://x/daily_volume_20260909.xlsx"},
                       started_at="x", elapsed_ms=1, outcome="rejected")        # a failed download is retried
        lake.store_payload(conn, source="cme_volume", kind="volume_workbook", content=b"abc", fmt="xlsx",
                           request={"url": "https://x/daily_volume_20260910.xlsx"})
        held = collect._volume_files_held(conn, {"20260904"})
        self.assertEqual(held, {"20260907", "20260908", "20260910"})


# ================================================================== importer
class ImporterRules(TempDb):
    def test_rendered_daily_report_is_skipped_but_xml_outputs_are_imported(self):
        data = Path(tempfile.mkdtemp(prefix="a2imp_"))
        self.addCleanup(shutil.rmtree, data, True)
        day = data / "Oct-06-26"
        (day / "offline_abc").mkdir(parents=True)
        (day / "daily_market_report.txt").write_text("plain text report")
        (day / "tactical_ruling.txt").write_text("<tactical><x>1</x></tactical>")
        (day / "email.eml").write_text("From: a\n\nbody")
        (day / "offline_abc" / "tactical_ruling.txt").write_text("<tactical/>")
        db = data / "portfolio.db"
        report = importer.import_history(data_dir=data, db_path=db, verbose=False)
        conn = lake.connect(db)
        self.addCleanup(conn.close)
        kinds = {r["kind"] for r in conn.execute("SELECT kind FROM v2_payloads WHERE source = 'legacy_daily_output'")}
        self.assertEqual(kinds, {"tactical_ruling.txt"})                  # the .txt report is skipped, the XML is not
        self.assertEqual(report["files"]["daily_output"], 1)
        self.assertEqual(report["not_imported"], [])


# ================================================================== render: macro ledger
class MacroLedger(unittest.TestCase):
    def _ctx(self, gex):
        return {"prices": {}, "run": {"generated_local": "2026-10-06 08:31 AM CDT", "generated_at": "2026-10-06T13:31:00+00:00"},
                "flow": {"SPY": {}, "SLV": {}}, "options": {"SPY": {"gex": gex, "spot": 500.0}, "SLV": {}},
                "technicals": {}, "weather": {}, "breadth": {}, "vmri": {}, "shanghai": {},
                "macro": {"oas": {}, "rrp": {}, "walcl": {}}, "paper_physical": {}, "ebay": {}, "ratios": {}}

    def test_gex_is_spy_net_gex_and_dix_stays_empty(self):
        rows = render.ledger_rows(self._ctx({"status": "fresh", "net_gex": 2.5e9, "call_wall": 510.0}))
        macro = rows["macro_master_ledger"][0]
        self.assertEqual(macro["GEX"], 2.5e9)
        self.assertIsNone(macro["DIX"])
        self.assertEqual(rows["institutional_ledger"][0]["Net_Gamma"], 2.5e9)       # same figure, same unit

    def test_gex_is_empty_when_the_model_had_no_inputs(self):
        for gex in ({"status": "missing", "reason": "no option chains"}, {}, None):
            macro = render.ledger_rows(self._ctx(gex))["macro_master_ledger"][0]
            self.assertIsNone(macro["GEX"])


# ================================================================== positions
def _closes(start="2026-10-05", periods=12, base=500.0, step=1.0):
    idx = pd.bdate_range(start, periods=periods)
    return pd.Series([base + step * i for i in range(periods)], index=idx)


def _call(**kw):
    """A ticket row as stored in v2_trade_signals (entered Monday 2026-10-05, 09:31 ET)."""
    row = {"run_id": "run-1", "created_at": "2026-10-05T13:31:00+00:00", "underlying": "SPY", "position_type": "CALL",
           "contract": "SPY261120C00500000", "strike": 500.0, "expiration": "2026-11-20", "entry_mid": 12.0,
           "entry_iv": 0.2, "underlying_price": 500.0, "score": 4, "bias": "BULLISH"}
    row.update(kw)
    return row


class Positions(TempDb):
    def _mark(self, conn, contract, at, mid):
        lake.record_observations(conn, [lake.obs("position.mark", at, entity=contract, value={"contract": contract, "mid": mid},
                                                 source="t")])

    def test_horizon_mark_is_the_last_one_recorded_that_day(self):
        conn, _ = self.new_db()
        c = "SPY261120C00500000"
        self._mark(conn, c, "2026-10-06T13:31:00+00:00", 11.0)      # opening run
        self._mark(conn, c, "2026-10-06T19:45:00+00:00", 14.5)      # closing run: this one counts
        self._mark(conn, c, "2026-10-07T13:31:00+00:00", 99.0)      # another day
        self.assertEqual(positions._market_mark(conn, c, "2026-10-06")["mid"], 14.5)
        self.assertIsNone(positions._market_mark(conn, c, "2026-10-08"))
        h = positions.horizon_values(conn, _call(), _closes())
        self.assertEqual(h["1d"]["basis"], "market")
        self.assertEqual(h["1d"]["value"], 14.5)
        self.assertAlmostEqual(h["1d"]["change_pct"], (14.5 / 12.0 - 1) * 100)
        self.assertEqual(h["1d"]["date"], "2026-10-06")
        self.assertEqual(h["_version"], positions.HORIZON_VERSION)

    def test_horizons_model_pending_cash_and_expiry(self):
        conn, _ = self.new_db()
        closes = _closes()                                          # 2026-10-05 .. 2026-10-20, +1 per day
        h = positions.horizon_values(conn, _call(), closes)
        self.assertEqual([h[k]["basis"] for k in ("1d", "1w", "2w")], ["model"] * 3)    # no recorded marks
        self.assertGreater(h["_model_iv"], 0.0)
        self.assertEqual(h["1w"]["date"], "2026-10-12")
        self.assertEqual(h["1w"]["spy_close"], 505.0)
        self.assertGreater(h["1w"]["value"], h["1d"]["value"])      # call gains as SPY climbs $1 a day
        short = positions.horizon_values(conn, _call(), closes.iloc[:3])                # data ends 10-07
        self.assertEqual(short["1d"]["basis"], "model")
        self.assertEqual(short["1w"], {"basis": "pending", "target": "2026-10-12"})
        self.assertEqual(short["2w"], {"basis": "pending", "target": "2026-10-19"})
        cash = positions.horizon_values(conn, _call(position_type="CASH", contract=None, strike=None, expiration=None,
                                                     entry_mid=None), closes)
        self.assertEqual(cash["1d"]["basis"], "spy_move")
        self.assertAlmostEqual(cash["1d"]["change_pct"], (501.0 / 500.0 - 1) * 100)
        expiring = positions.horizon_values(conn, _call(expiration="2026-10-09", strike=502.0), closes)
        self.assertEqual(expiring["1w"]["basis"], "expiry_intrinsic")       # 1 week is past the expiry
        self.assertEqual(expiring["1w"]["value"], 2.0)                      # SPY 504 on 10-09, strike 502
        self.assertEqual(expiring["1w"]["date"], "2026-10-09")
        no_inputs = positions.horizon_values(conn, _call(entry_mid=None, entry_iv=None), closes)
        self.assertEqual(no_inputs["1d"]["basis"], "unavailable")           # nothing to price with: no invented value

    def _ctx(self, run_id, score=4, with_contract=True):
        ticket = {"contract": "SPY261120C00500000", "type": "CALL", "strike": 500.0, "expiration": "2026-11-20",
                  "bid": 11.9, "ask": 12.1, "last": 12.0, "iv": 0.2, "volume": 10, "open_interest": 100,
                  "allocation": "5%"} if with_contract else "CASH POSITION - No trade ticket generated."
        return {"run": {"run_id": run_id, "generated_at": "2026-10-05T13:31:00+00:00"},
                "technicals": {"current_price": 500.0}, "options": {"SPY": {"spot": 500.0}},
                "execution": {"total_score": score, "directional_bias": "BULLISH", "ticket": ticket,
                              "strike_rationale": "wall", "expiration_rationale": "friday"}}

    def test_backfill_records_only_committed_live_runs_without_a_ticket_and_is_idempotent(self):
        conn, _ = self.new_db()
        self.add_run(conn, "live-missing", "live", "completed", "2026-10-05T14:00:00+00:00", self._ctx("live-missing"))
        self.add_run(conn, "live-has-ticket", "live", "completed", "2026-10-05T15:00:00+00:00", self._ctx("live-has-ticket"))
        self.add_run(conn, "live-committed", "live", "committed", "2026-10-05T16:00:00+00:00", self._ctx("live-committed"))
        self.add_run(conn, "live-cash", "live", "completed_with_warnings", "2026-10-05T17:00:00+00:00",
                     self._ctx("live-cash", score=1, with_contract=False))
        self.add_run(conn, "live-no-verdict", "live", "completed", "2026-10-05T18:00:00+00:00", self._ctx("live-no-verdict", score=None))
        self.add_run(conn, "live-failed", "live", "failed", "2026-10-05T19:00:00+00:00", self._ctx("live-failed"))
        self.add_run(conn, "offline-1", "offline", "completed", "2026-10-05T20:00:00+00:00", self._ctx("offline-1"))
        positions.record_signal(conn, self._ctx("live-has-ticket"))                 # recorded by its own run
        before = conn.execute("SELECT * FROM v2_trade_signals WHERE run_id = 'live-has-ticket'").fetchone()
        self.assertEqual(positions.backfill(conn), 3)                               # missing, committed, cash
        recorded = {r["run_id"]: r for r in conn.execute("SELECT * FROM v2_trade_signals")}
        self.assertEqual(set(recorded), {"live-missing", "live-has-ticket", "live-committed", "live-cash"})
        self.assertEqual(recorded["live-cash"]["position_type"], "CASH")
        self.assertEqual(recorded["live-missing"]["contract"], "SPY261120C00500000")
        self.assertEqual(recorded["live-missing"]["source"], "engine_run")
        self.assertEqual(tuple(recorded["live-has-ticket"]), tuple(before))         # untouched
        self.assertEqual(positions.backfill(conn), 0)                               # second call changes nothing
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM v2_trade_signals").fetchone()[0], 4)

    def test_list_live_runs_no_ddl_and_works_on_a_read_only_connection(self):
        conn, path = self.new_db()
        positions.record_signal(conn, self._ctx("run-1"))
        ro = lake.connect_readonly(path)
        self.addCleanup(ro.close)
        rows = positions.list_live(ro, lambda s: 505.0, lambda s, e: None, lambda s: _closes())
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["underlying_now"], 505.0)
        self.assertAlmostEqual(rows[0]["underlying_change_pct"], 1.0)
        self.assertIn("1d", rows[0]["horizons"])
        self.assertIsNone(rows[0].get("now_mid"))                                   # no chain: unknown, not zero

    def test_list_live_on_an_unmigrated_database_raises_and_creates_nothing(self):
        conn, path = self.new_db(migrate=False)
        with self.assertRaises(sqlite3.OperationalError):
            positions.list_live(conn, lambda s: 1.0, lambda s, e: None)
        self.assertEqual(lake.table_columns(conn, "v2_trade_signals"), [])          # still no table: no DDL ran
        ro = lake.connect_readonly(path)
        self.addCleanup(ro.close)
        with self.assertRaises(sqlite3.OperationalError):
            positions.list_live(ro, lambda s: 1.0, lambda s, e: None)


# ================================================================== Forecast Lab: scorecard, missing inputs
class ForecastScorecard(TempDb):
    def test_scorecard_runs_no_ddl_and_reads_a_read_only_connection(self):
        conn, path = self.new_db()
        conn.execute("INSERT INTO v2_forecasts (run_id, created_at, symbol, model, horizon, target_date, spot, p_up, "
                     "lower68, upper68, direction) VALUES ('r', '2026-10-01', 'SPY', 'implied', '1d', '2026-10-02', 100, "
                     "0.6, 99, 101, 'UP')")
        conn.commit()
        closes = pd.Series([100.0, 100.5], index=pd.to_datetime(["2026-10-01", "2026-10-02"]))
        ro = lake.connect_readonly(path)
        self.addCleanup(ro.close)
        sc = F.scorecard(ro, lambda s: closes, "SPY")
        self.assertEqual((sc["logged"], sc["pending"]), (1, 0))
        self.assertEqual(sc["graded"][0]["hit_rate"], 1.0)

    def test_scorecard_before_the_first_run_raises_and_creates_no_table(self):
        conn, path = self.new_db(migrate=False)
        with self.assertRaises(sqlite3.OperationalError):
            F.scorecard(conn, lambda s: None, "SPY")
        self.assertEqual(lake.table_columns(conn, "v2_forecasts"), [])


def _base_inputs(**over):
    inputs = {"today": date(2026, 10, 9), "frames": {}, "spot": {}, "chains": {}, "gex": {}, "fred": {}, "cot": {},
              "slv_trust": None, "slv_trust_history": [], "fomc": None, "cpi": None, "aum": {}}
    inputs.update(over)
    return inputs


def _ohlc(n=900, seed=1, end="2026-10-08", start_price=100.0, drift=0.0003, vol=0.01):
    """Synthetic daily bars from a seeded random walk: open = prior close, high/low bracket open and close."""
    rng = np.random.default_rng(seed)
    close = start_price * np.exp(np.cumsum(rng.normal(drift, vol, n)))
    prev = np.concatenate([[start_price], close[:-1]])
    hi = np.maximum(prev, close) * (1 + np.abs(rng.normal(0, vol / 3, n)))
    lo = np.minimum(prev, close) * (1 - np.abs(rng.normal(0, vol / 3, n)))
    idx = pd.bdate_range(end=end, periods=n)
    return pd.DataFrame({"Open": prev, "High": hi, "Low": lo, "Close": close, "Volume": 5e6}, index=idx)


class ForecastBuildOnMissingInputs(TempDb):
    CARDS = ("implied", "vol_forecast", "trend", "calendar", "flows")

    def _assert_all_missing(self, fc):
        for sym in ("SPY", "SLV"):
            for card in self.CARDS:
                block = fc[sym][card]
                self.assertEqual(block["status"], "missing", (sym, card))
                self.assertTrue(block["reason"], (sym, card))
        for key in ("positioning", "silver_fair_value", "macro_regime"):
            self.assertEqual(fc[key]["status"], "missing", key)
            self.assertTrue(fc[key]["reason"], key)
        self.assertEqual(fc["positioning"]["silver"]["status"], "missing")      # the card still prints each reason
        self.assertEqual(fc["positioning"]["sp500"]["status"], "missing")
        json.dumps(lake.clean_json(fc))                                         # storable in a snapshot

    def test_no_price_history_at_all_comes_out_missing_instead_of_raising(self):
        """The offline run on an empty lake used to store the whole forecast as an error (AttributeError on
        closes.index.date of an empty RangeIndex)."""
        fc = F.build(_base_inputs())
        self.assertEqual(fc["version"], F.VERSION)
        self._assert_all_missing(fc)
        self.assertEqual(set(fc["consensus"]), {"SPY", "SLV"})

    def test_empty_and_unusable_frames_are_missing_too(self):
        empty = pd.DataFrame({c: [] for c in ("Open", "High", "Low", "Close", "Volume")}, index=pd.DatetimeIndex([]))
        nan = _ohlc(60).assign(Close=np.nan)
        for frame in (empty, nan, _ohlc(40)):
            frames = {k: frame for k in ("SPY", "SLV", "SI_F", "GC_F", "DXY", "HG_F")}
            fc = F.build(_base_inputs(frames=frames, spot={"SPY": 110.0, "SLV": 30.0}))
            for sym in ("SPY", "SLV"):
                for card in ("vol_forecast", "trend", "calendar"):
                    self.assertEqual(fc[sym][card]["status"], "missing", (sym, card))
            self.assertEqual(fc["silver_fair_value"]["status"], "missing")

    def test_offline_collect_build_forecast_on_an_empty_lake_is_not_an_error(self):
        conn, _ = self.new_db()
        session = SourceSession(conn, "t", offline=True)
        now = datetime(2026, 10, 9, 13, 31, tzinfo=timezone.utc)
        fc = collect.build_forecast(conn, session, now, date(2026, 10, 9), {}, {}, {"spy": {}, "slv": {}}, {})
        self.assertEqual(fc["status"], "partial")                               # sources failed; the models did not
        self.assertTrue(fc["source_errors"])
        self._assert_all_missing(fc)

    def test_refining_degrades_the_same_way(self):
        empty = pd.Series([], index=pd.DatetimeIndex([]), dtype=float)
        five = pd.Series(np.arange(5.0), index=pd.date_range("2026-08-01", periods=5, freq="W-FRI"))
        for s in (None, empty, five):           # absent, empty, and too short to use
            self.assertEqual(RF.crack_spreads(s, s, s)["status"], "missing")
            self.assertEqual(RF.maintenance_outlook(s)["status"], "missing")
            self.assertEqual(RF.inventories({k: s for k in RF.EIA_SERIES})["status"], "missing")
            self.assertEqual(RF.inventory_history({k: s for k in RF.EIA_SERIES})["status"], "missing")
        for s in (None, empty):                 # used to raise IndexError when dist_stocks was empty but demand was not
            self.assertEqual(RF.eia_fundamentals({"util_us": s, "dist_stocks": s, "dist_supplied": five})["status"], "missing")
        partial = RF.eia_fundamentals({"util_us": five, "dist_stocks": five, "dist_supplied": five})
        self.assertEqual(partial["status"], "partial")                  # present but short: lists what is missing
        self.assertIn("util_p1", partial["reason"])
        self.assertEqual(RF.refinery_table(None), [])
        self.assertEqual(RF.outage_summary([], date(2026, 10, 9))["events_7d"], 0)

    def test_offline_collect_build_refining_on_an_empty_lake(self):
        conn, _ = self.new_db()
        session = SourceSession(conn, "t", offline=True)
        out = collect.build_refining(conn, session, "t", date(2026, 10, 9))
        self.assertEqual(out["status"], "partial")
        self.assertTrue(out["source_errors"])
        for key in ("margins", "fundamentals", "maintenance", "inventories"):
            self.assertEqual(out[key]["status"], "missing", key)
            self.assertTrue(out[key]["reason"], key)
        json.dumps(lake.clean_json(out))


# ================================================================== Forecast Lab: models on synthetic data
def _chain(spot=500.0, iv=0.18, dte=30):
    T = dte / 365
    rows = {"calls": [], "puts": []}
    for k in np.arange(spot * 0.7, spot * 1.3 + 1, spot * 0.01):
        for side, call in (("calls", True), ("puts", False)):
            p = F._bs(spot, k, T, iv, call)
            rows[side].append({"strike": float(k), "bid": p * 0.995, "ask": p * 1.005, "lastPrice": p, "impliedVolatility": iv})
    return {k: pd.DataFrame(v) for k, v in rows.items()}


def _cot_rows(n=160, fields=(), flip=False):
    """Weekly CFTC rows, oldest first. For each (long_field, short_field) pair the net position climbs every week
    (falls when flip=True), so the last report is the 3-year high (low)."""
    start = date(2023, 10, 3)
    rows = []
    for i in range(n):
        r = {"report_date_as_yyyy_mm_dd": (start + timedelta(weeks=i)).isoformat() + "T00:00:00.000",
             "open_interest_all": 100000 + i}
        for j, (lf, sf) in enumerate(fields):
            net = (n - i if flip else i) * (10 + j)
            r[lf], r[sf] = 50000 + net, 50000
        rows.append(r)
    return rows


class ForecastModels(unittest.TestCase):
    def test_vol_forecast_tracks_realized_volatility(self):
        df = _ohlc(900, seed=3, vol=0.01)
        out = F.vol_forecast(df, None, 500.0)
        self.assertEqual(out["status"], "fresh")
        self.assertEqual(set(out["horizons"]), {"1d", "1w", "1m", "1y"})
        for k, h in out["horizons"].items():
            self.assertTrue(0.05 < h["forecast_vol"] < 0.40, (k, h["forecast_vol"]))    # 1% a day is about 16% a year
            self.assertNotIn("forecast_var", h)
            self.assertGreater(h["expected_move_1sd"], 0)
        self.assertTrue(0.05 < out["long_run_vol"] < 0.40)
        self.assertEqual(set(out["backtest"]), {"1d", "1w", "1m"})

    def test_vol_forecast_compares_with_implied_vol(self):
        df = _ohlc(900, seed=3, vol=0.01)
        fv = F.vol_forecast(df, None, 500.0)["horizons"]["1m"]["forecast_vol"]
        rich = F.vol_forecast(df, {"horizons": {"1m": {"atm_iv": fv * 1.5}}}, 500.0)["horizons"]["1m"]
        cheap = F.vol_forecast(df, {"horizons": {"1m": {"atm_iv": fv * 0.5}}}, 500.0)["horizons"]["1m"]
        self.assertAlmostEqual(rich["implied_to_forecast"], 1.5)
        self.assertIn("rich", rich["read"])
        self.assertIn("cheap", cheap["read"])
        self.assertAlmostEqual(rich["vrp"], fv * 0.5)

    def test_vol_forecast_missing_inputs(self):
        self.assertEqual(F.vol_forecast(None)["status"], "missing")
        short = F.vol_forecast(_ohlc(120))
        self.assertEqual(short["status"], "missing")
        self.assertIn("300", short["reason"])

    def test_positioning_flags_a_crowded_long_and_computes_trust_changes(self):
        silver = _cot_rows(fields=[("m_money_positions_long_all", "m_money_positions_short_all"),
                                   ("prod_merc_positions_long", "prod_merc_positions_short")])
        sp = _cot_rows(fields=[("lev_money_positions_long", "lev_money_positions_short"),
                               ("asset_mgr_positions_long", "asset_mgr_positions_short")], flip=True)
        trust = {"ounces_in_trust": 500e6, "as_of": "2026-10-05"}
        hist = [("2026-09-04", 480e6), ("2026-10-02", 495e6), ("2026-10-05", 500e6)]
        out = F.positioning(silver, sp, trust, hist, pd.Series(dtype=float), pd.Series(dtype=float))
        self.assertEqual(out["status"], "fresh")
        mm = out["silver"]["managed_money"]
        self.assertEqual(mm["cot_index"], 100.0)                       # net is at its 3-year high on the last report
        self.assertEqual(mm["read"], "crowded long (contrarian bearish)")
        self.assertEqual(mm["weeks"], 160)
        lev = out["sp500"]["leveraged_funds"]
        self.assertEqual(lev["cot_index"], 0.0)                         # net is at its low
        self.assertEqual(lev["read"], "crowded short (contrarian bullish)")
        self.assertEqual(out["slv_trust"]["change_vs_prev_oz"], 5e6)    # 500M vs the 10-02 reading
        self.assertEqual(out["slv_trust"]["change_30d_oz"], 20e6)       # 500M vs 09-04 (on or before 09-05)
        self.assertNotIn("swap_dealers", out["silver"])                 # that leg was not supplied

    def test_positioning_with_nothing_is_missing_and_rows_without_numbers_are_ignored(self):
        none = pd.Series(dtype=float)
        out = F.positioning(None, None, None, None, none, none)
        self.assertEqual(out["status"], "missing")
        self.assertIn("CFTC silver rows unavailable", out["reason"])
        self.assertEqual(out["silver"]["status"], "missing")
        self.assertEqual(out["sp500"]["status"], "missing")
        partial = F.positioning(None, None, {"ounces_in_trust": 1e6, "as_of": "2026-10-05"}, [], none, none)
        self.assertEqual(partial["status"], "fresh")                    # the trust reading alone keeps the block alive
        holes = [{"report_date_as_yyyy_mm_dd": "2026-10-06", "open_interest_all": 1}]       # no position fields
        self.assertEqual(F.positioning(holes, holes, None, None, none, none)["status"], "missing")

    def test_silver_fair_value_recovers_a_known_relationship(self):
        n = 800
        g, d, h = (_ohlc(n, seed=s, start_price=p, vol=0.008, drift=0.0001) for s, p in ((11, 2000.0), (12, 100.0), (13, 4.0)))
        idx = g.index
        ry = pd.Series(1.5 + np.cumsum(np.random.default_rng(14).normal(0, 0.01, n)), index=idx)
        si = np.exp(0.3 + 0.9 * np.log(g["Close"]) + 0.05 * ry - 0.4 * np.log(d["Close"]) + 0.2 * np.log(h["Close"]))
        sdf = pd.DataFrame({"Open": si, "High": si, "Low": si, "Close": si, "Volume": 1.0}, index=idx)
        frames = {"SI_F": sdf, "GC_F": g, "DXY": d, "HG_F": h}
        dfii = [{"date": t.date().isoformat(), "value": float(v)} for t, v in ry.items()][::-1]
        out = F.silver_fair_value(frames, dfii, None)
        self.assertEqual(out["status"], "fresh")
        self.assertGreater(out["r2"], 0.99)
        self.assertLess(abs(out["gap_pct"]), 1.0)                       # silver sits on its own fitted relationship
        self.assertAlmostEqual(out["coefficients"]["log_gold"]["beta"], 0.9, delta=0.05)
        self.assertAlmostEqual(out["coefficients"]["real_yield_10y"]["beta"], 0.05, delta=0.02)
        self.assertEqual(set(out["path"]), {"1w", "1m", "1y"})
        # latest silver 10% above the relationship: the model must call it rich
        rich = sdf.copy()
        rich.iloc[-1, :4] = rich.iloc[-1, :4] * 1.10
        out2 = F.silver_fair_value({**frames, "SI_F": rich}, dfii, None)
        self.assertGreater(out2["gap_pct"], 5.0)

    def test_silver_fair_value_names_what_is_missing(self):
        frames = {k: _ohlc(100) for k in ("SI_F", "GC_F", "DXY", "HG_F")}
        out = F.silver_fair_value(frames, [{"date": "2026-10-01", "value": 1.8}], None)
        self.assertEqual(out["status"], "missing")
        self.assertIn("SI_F", out["reason"])
        long_frames = {k: _ohlc(400, seed=i) for i, k in enumerate(("SI_F", "GC_F", "DXY", "HG_F"))}
        out = F.silver_fair_value(long_frames, None, None)
        self.assertEqual(out["status"], "missing")
        self.assertIn("DFII10", out["reason"])
        self.assertEqual(F.silver_fair_value({}, None, None)["status"], "missing")

    def test_calendar_effects_flags_the_events_of_the_coming_week(self):
        df = _ohlc(700, seed=5, end="2026-10-09")                      # a Friday
        today = date(2026, 10, 12)
        out = F.calendar_effects(df, ["2025-12-10", "2026-03-18", "2026-10-14"], ["2026-01-13", "2026-10-13"], today)
        self.assertEqual(out["status"], "fresh")
        self.assertEqual(out["next"], {"fomc": "2026-10-14", "cpi": "2026-10-13", "opex": "2026-10-16"})
        events = {e["event"] for e in out["upcoming_7d"]}
        self.assertEqual(events, {"FOMC decision", "CPI release", "Monthly OPEX"})
        self.assertEqual(set(out["day_of_week"]), {"Mon", "Tue", "Wed", "Thu", "Fri"})
        self.assertEqual(out["effects"]["fomc_day"]["n"], 2)            # the 2026-10-14 date has no return yet
        self.assertGreater(out["effects"]["opex_week"]["n"], 20)
        self.assertEqual(F._third_friday(2026, 10), date(2026, 10, 16))
        self.assertEqual(F._third_friday(2026, 11), date(2026, 11, 20))

    def test_calendar_effects_without_events_and_with_short_history(self):
        out = F.calendar_effects(_ohlc(700, seed=5, end="2026-10-09"), [], [], date(2026, 10, 12))
        self.assertEqual([e["event"] for e in out["upcoming_7d"]], ["Monthly OPEX"])
        self.assertIsNone(out["next"]["fomc"])
        self.assertEqual(out["effects"]["fomc_day"]["n"], 0)            # no sample: no mean, hit rate or t-statistic
        self.assertNotIn("mean_pct", out["effects"]["fomc_day"])
        self.assertEqual(F.calendar_effects(_ohlc(100), [], [], date(2026, 10, 12))["status"], "missing")
        self.assertEqual(F.calendar_effects(None, [], [], date(2026, 10, 12))["status"], "missing")

    def test_mechanical_flows_leveraged_etf_dealer_gamma_and_vol_control(self):
        n = 150
        idx = pd.bdate_range(end="2026-10-08", periods=n)
        rets = np.where(np.arange(n) % 2 == 0, 0.01, -0.01)
        close = 500 * np.cumprod(1 + rets)
        df = pd.DataFrame({"Close": close, "Volume": 1e7}, index=idx)
        aum = {"UPRO": 1e9, "SPXL": 2e9, "SH": 5e8}
        out = F.mechanical_flows("SPY", df, aum, {"net_gex": 5e9, "zero_gamma": 480.0}, None, 500.0)
        self.assertEqual(out["status"], "fresh")
        total = 1e9 * 6 + 2e9 * 6 + 5e8 * 2                              # sum of AUM x L x (L-1)
        self.assertAlmostEqual(out["leveraged_etfs"]["flow_per_1pct_move"], total * 0.01)
        self.assertAlmostEqual(out["leveraged_etfs"]["funds"]["SH"]["flow_per_1pct"], 5e8 * 2 * 0.01)
        last = float(close[-1] / close[-2] - 1)
        self.assertAlmostEqual(out["leveraged_etfs"]["last_session_rebalance"], total * last)
        r = pd.Series(close).pct_change().dropna()
        rv = max(r.tail(21).std(), r.tail(63).std()) * np.sqrt(252)
        self.assertAlmostEqual(out["vol_control"]["exposure"], min(0.10 / rv, 1.5))
        self.assertEqual(set(out["vol_control"]["flow_if_tomorrow"]), {"-3%", "-2%", "-1%", "+1%", "+2%", "+3%"})
        self.assertAlmostEqual(out["dealer_gamma"]["hedge_flow_per_1pct"], -5e9 * 500.0 * 0.01)
        self.assertIn("long gamma", out["dealer_gamma"]["regime"])
        self.assertNotIn("cta", out)                                    # no trend result was passed
        trend = {"status": "fresh", "exposure": 0.4, "sensitivity": {"+2%": 0.5}}
        short = F.mechanical_flows("SPY", df, aum, {"net_gex": -1e9}, trend, 500.0)
        self.assertIn("short gamma", short["dealer_gamma"]["regime"])
        self.assertEqual(short["cta"]["exposure"], 0.4)

    def test_mechanical_flows_slv_has_no_vol_control_and_unknown_inputs_stay_out(self):
        df = pd.DataFrame({"Close": np.linspace(20, 30, 120), "Volume": 1e6}, index=pd.bdate_range(end="2026-10-08", periods=120))
        out = F.mechanical_flows("SLV", df, {"AGQ": 1e9}, {}, None, 30.0)
        self.assertNotIn("vol_control", out)
        self.assertNotIn("dealer_gamma", out)                           # no GEX figure: the block is absent, not zero
        self.assertAlmostEqual(out["leveraged_etfs"]["flow_per_1pct_move"], 1e9 * 2 * 1 * 0.01)
        self.assertEqual(F.mechanical_flows("SPY", df.head(30), {}, {}, None, 30.0)["status"], "missing")
        self.assertEqual(F.mechanical_flows("SPY", None, {}, {}, None, 30.0)["status"], "missing")

    def test_build_end_to_end_on_synthetic_inputs(self):
        today = date(2026, 10, 9)
        frames = {k: _ohlc(900, seed=i, start_price=p, vol=v)
                  for i, (k, p, v) in enumerate((("SPY", 500.0, 0.009), ("SLV", 30.0, 0.018), ("SI_F", 32.0, 0.018),
                                                 ("GC_F", 2400.0, 0.009), ("DXY", 100.0, 0.004), ("HG_F", 4.0, 0.012)), 20)}
        chains = {"SPY": {(today + timedelta(days=d)).isoformat(): _chain(500.0, 0.18, d) for d in (8, 32, 95)}}
        fred = {"T10Y3M": [{"date": (today - timedelta(days=i)).isoformat(), "value": 0.5 + 0.001 * i} for i in range(400)],
                "NFCI": [{"date": (today - timedelta(weeks=i)).isoformat(), "value": -0.4} for i in range(200)],
                "DFII10": [{"date": (today - timedelta(days=i)).isoformat(), "value": 1.8 + 0.001 * i} for i in range(1400)]}
        silver_cot = _cot_rows(fields=[("m_money_positions_long_all", "m_money_positions_short_all"),
                                       ("prod_merc_positions_long", "prod_merc_positions_short")])
        fc = F.build(_base_inputs(
            frames=frames, spot={"SPY": 500.0, "SLV": 30.0}, chains=chains,
            gex={"SPY": {"net_gex": 3e9, "call_wall": 520.0, "put_wall": 480.0, "zero_gamma": 470.0}}, fred=fred,
            cot={"silver": silver_cot, "sp": None}, aum={"UPRO": 1e9}, fomc=["2026-10-28"], cpi=["2026-10-14"]))
        spy = fc["SPY"]
        self.assertEqual(spy["implied"]["status"], "fresh")
        self.assertEqual(spy["implied"]["horizons"]["1m"]["basis"], "smile")
        self.assertAlmostEqual(spy["implied"]["horizons"]["1m"]["atm_iv"], 0.18, delta=0.02)
        for card in ("vol_forecast", "trend", "calendar", "flows"):
            self.assertEqual(spy[card]["status"], "fresh", card)
        self.assertEqual(fc["SLV"]["implied"]["status"], "missing")      # no SLV chain was supplied: reason, not a guess
        self.assertEqual(fc["SLV"]["trend"]["status"], "fresh")
        self.assertEqual(fc["positioning"]["silver"]["managed_money"]["cot_index"], 100.0)
        self.assertEqual(fc["positioning"]["sp500"]["status"], "missing")
        self.assertEqual(fc["macro_regime"]["status"], "fresh")
        self.assertEqual(fc["silver_fair_value"]["status"], "fresh")
        self.assertEqual(set(fc["consensus"]["SPY"]), {"1d", "1w", "1m", "1y"})
        self.assertIsNotNone(fc["consensus"]["SPY"]["1m"]["implied_p_up"])
        json.dumps(lake.clean_json(fc))
        rows = F.forecast_rows({"forecast": fc, "run": {"run_id": "r", "generated_at": "2026-10-09T13:31:00+00:00"}})
        self.assertTrue({"implied", "har_vol"} <= {r[3] for r in rows})


if __name__ == "__main__":
    unittest.main()
