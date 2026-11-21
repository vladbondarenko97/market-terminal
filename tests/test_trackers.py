"""T · Trackers (core/trackers.py, the tracker-alerts job and the /api/trackers routes): the expression parser, validation, evaluation,
edge-triggered firing with cooldown and priorities, the regime-change alert, storage and its fallback, and the write guards. Offline; fixed dates."""
import sys, unittest
from pathlib import Path

import support  # noqa: F401  (temporary CME_Data, no credentials; must come before importing config)

import json
import tempfile
from datetime import datetime, timedelta, timezone
from unittest import mock

import config
from core import trackers as T
from test_api import ApiCase

T0 = datetime(2026, 11, 20, 15, 0, tzinfo=timezone.utc)


def ctx(**over):
    c = {"run": {"mode": "live"}, "chaos": {"score": 72.0, "components": {"vvix": {"value": 138.0}}},
         "forecast": {"macro_regime": {"hy_oas": 3.52, "sahm": 0.0}}, "prices": {"vix": {"value": 35.9}, "vix3m": {"value": 30.9}},
         "regime": {"key": "VOL_SHOCK", "regime": "VOLATILITY SHOCK, NO CREDIT/RECESSION CONFIRMATION", "read": {"status": "fresh", "text": "A shock without credit stress."}},
         "bitcoin": {"verdict": {"call": "HOLD"}, "onchain": {"realized_price": 56000.0}}}
    c.update(over)
    return c


def env(quotes=None, c=None, why=None):
    q = dict(quotes or {})
    return T.Env(ctx() if c is None and why is None else c, why, lambda s: q.get(s), run_id="r1")


def tracker(expr, op, value=None, **kw):
    cond = {"expr": expr, "op": op, **({} if value is None else {"value": value})}
    return T.clean({"name": kw.pop("name", "t"), "conditions": [cond], **kw}, kw.pop("id", "t.test"))


class Sender:
    def __init__(self, ok=True):
        self.sent, self.ok = [], ok

    def __call__(self, title, text, priority, tags):
        self.sent.append((title, text, priority, tags))
        return self.ok


class Expressions(unittest.TestCase):
    def test_operands_and_arithmetic(self):
        e = env({"^VIX": 20.0, "^VIX3M": 25.0, "BTC-USD": 50000.0})
        self.assertAlmostEqual(T.value_of("^VIX / ^VIX3M", e)[0], 0.8)
        self.assertEqual(T.value_of("hy_oas", e), (3.52, None))
        self.assertAlmostEqual(T.value_of("BTC-USD / btc_realized_price", e)[0], 50000 / 56000)
        self.assertAlmostEqual(T.value_of("(vix - vix3m) * 2", e)[0], 10.0)                      # a binary minus has spaces
        self.assertEqual(T.value_of("regime", e), ("VOL_SHOCK", None))                            # a single text metric stays text
        self.assertEqual(T.value_of("macro.vmri.score", env(c=ctx(vmri={"score": 500.0}))), (500.0, None))   # a catalog id

    def test_bad_expressions_are_refused_with_a_reason(self):
        for bad, why in (("", "1 to 200"), ("hy_oas +", "not valid"), ("nonsense_metric", "unknown metric"), ("__import__('os')", "cannot read"),
                         ("SLV ** 2", "not valid"), ("1 + 2", "at least one symbol"), ("a" * 201, "1 to 200")):
            with self.assertRaises(ValueError, msg=bad) as cm:
                T.parse(bad)
            self.assertIn(why, str(cm.exception))

    def test_missing_readings_are_errors_never_zero(self):
        v, err = T.value_of("^VIX", env({}))
        self.assertIsNone(v)
        self.assertIn("no live quote", err)
        v, err = T.value_of("hy_oas", env(c={"run": {"mode": "live"}}))
        self.assertIn("not in the latest run", err)
        v, err = T.value_of("hy_oas", env(c=None, why="the latest run is offline, not a live run"))
        self.assertIn("offline", err)
        self.assertIn("division", T.value_of("SLV / hy_oas", env({"SLV": 1.0}, c=ctx(forecast={"macro_regime": {"hy_oas": 0.0}})))[1])
        self.assertIn("text metric", T.value_of("regime + 1", env())[1])

    def test_distance_and_progress(self):
        c = T.check_condition({"expr": "^VIX / ^VIX3M", "op": "<", "value": 1.0}, env({"^VIX": 36.0, "^VIX3M": 31.0}))
        self.assertFalse(c["met"])
        self.assertAlmostEqual(c["gap_pct"], 16.13, places=1)
        self.assertAlmostEqual(c["progress"], round(1 - 16.129 / 25, 3), places=2)
        hit = T.check_condition({"expr": "SLV", "op": "<=", "value": 36.71}, env({"SLV": 36.5}))
        self.assertEqual((hit["met"], hit["progress"]), (True, 1.0))
        self.assertEqual(T.check_condition({"expr": "hy_oas", "op": "changes"}, env())["met"], None)


class Validation(unittest.TestCase):
    def test_a_good_tracker_is_normalised(self):
        t = T.clean({"name": "  x  ", "conditions": [{"expr": "hy_oas  >", "op": ">", "value": "4.5"}][:0] or [{"expr": "hy_oas", "op": ">", "value": "4.5"}], "priority": "URGENT"})
        self.assertEqual((t["name"], t["priority"], t["cooldown_min"], t["hours"], t["enabled"]), ("x", "urgent", 60, "24/7", True))
        self.assertEqual(t["conditions"], [{"expr": "hy_oas", "op": ">", "value": 4.5}])
        self.assertTrue(t["id"].startswith("t."))

    def test_refusals(self):
        c = [{"expr": "hy_oas", "op": ">", "value": 4}]
        for raw, why in (({"name": "", "conditions": c}, "name"), ({"name": "x", "conditions": []}, "conditions"), ({"name": "x", "conditions": c * 5}, "conditions"),
                         ({"name": "x", "conditions": [{"expr": "hy_oas", "op": "~", "value": 1}]}, "comparator"),
                         ({"name": "x", "conditions": [{"expr": "hy_oas", "op": ">", "value": "abc"}]}, "number"),
                         ({"name": "x", "conditions": [{"expr": "hy_oas", "op": ">", "value": float("inf")}]}, "finite"),
                         ({"name": "x", "conditions": c, "priority": "loud"}, "priority"), ({"name": "x", "conditions": c, "cooldown_min": -1}, "cooldown"),
                         ({"name": "x", "conditions": c, "hours": "nights"}, "hours"), ({"name": "x", "conditions": c, "id": "../etc"}, "id"),
                         ({"name": "x", "conditions": c + [{"expr": "regime", "op": "changes"}]}, "only condition"), ("nope", "object")):
            with self.assertRaises(ValueError, msg=str(raw)) as cm:
                T.clean(raw)
            self.assertIn(why, str(cm.exception))

    def test_text_metrics_compare_with_double_equals(self):
        t = T.clean({"name": "x", "conditions": [{"expr": "btc_verdict", "op": "==", "value": "BUY (start)"}]})
        self.assertEqual(t["conditions"][0]["value"], "BUY (start)")
        self.assertTrue(T.check_condition(t["conditions"][0], env(c=ctx(bitcoin={"verdict": {"call": "BUY (start)"}})))["met"])

    def test_priority_maps_to_ntfy_one_to_five(self):
        self.assertEqual(T.PRIORITIES, {"min": 1, "low": 2, "default": 3, "high": 4, "urgent": 5})
        for name, n in T.PRIORITIES.items():
            s = Sender()
            T.fire_test(tracker("hy_oas", ">", 1, priority=name), env(), s, T0)
            self.assertEqual(s.sent[0][2], n)
            self.assertTrue(s.sent[0][3])

    def test_the_seeds_are_all_valid_and_named_as_asked(self):
        names = [s["name"] for s in T.SEEDS]
        for s in T.SEEDS:
            self.assertEqual(T.clean(s, s["id"])["conditions"], [{**c, "expr": T.parse(c["expr"])[2]} for c in s["conditions"]])
        self.assertEqual(names, ["1st tranche: VIX curve back under 1.0", "2nd tranche: Chaos < 50 and HY OAS < 4%", "STOP: HY OAS > 4.5%", "Regime change",
                                 "BTC verdict changes", "BTC below realized price", "Silver (SLV) at or below $36.71 (tested buy signal)"])
        self.assertEqual({s["name"]: s["priority"] for s in T.SEEDS}["STOP: HY OAS > 4.5%"], "urgent")


class Firing(unittest.TestCase):
    def run_once(self, items, e, state, now=T0, session=True, send=None, log=None):
        send = send or Sender()
        new, fired = T.run_due(items, e, state, now, send, session, log_path=log)
        return new, fired, send

    def setUp(self):
        self.log = Path(tempfile.mkdtemp(prefix="trk_")) / "fires.jsonl"

    def test_first_check_only_records_then_a_crossing_fires_once(self):
        t = tracker("SLV", "<=", 36.71, cooldown_min=0)
        st, fired, s = self.run_once([t], env({"SLV": 37.0}), {}, log=self.log)
        self.assertEqual((fired, s.sent), ([], []))
        self.assertIs(st["t.test"]["met"], False)
        st, fired, s = self.run_once([t], env({"SLV": 36.5}), st, T0 + timedelta(minutes=15), send=s, log=self.log)
        self.assertEqual(fired, ["t.test"])
        st, fired, s = self.run_once([t], env({"SLV": 36.4}), st, T0 + timedelta(minutes=30), send=s, log=self.log)
        self.assertEqual((fired, len(s.sent)), ([], 1))                                           # still true: no repeat
        self.assertEqual(json.loads(self.log.read_text().splitlines()[0])["name"], "t")

    def test_cooldown_swallows_a_quick_re_crossing(self):
        t = tracker("SLV", "<=", 36.71, cooldown_min=60)
        st, _, s = self.run_once([t], env({"SLV": 37.0}), {})
        st, fired, s = self.run_once([t], env({"SLV": 36.5}), st, T0, send=s, log=self.log)
        self.assertEqual(len(fired), 1)
        for minutes, px in ((15, 37.0), (30, 36.5)):                                              # rearmed and crossed again inside the hour
            st, fired, s = self.run_once([t], env({"SLV": px}), st, T0 + timedelta(minutes=minutes), send=s, log=self.log)
        self.assertEqual(len(s.sent), 1)
        self.assertEqual(T.status_of(t, {"met": True, "last_fired": st["t.test"]["last_fired"]}, T0 + timedelta(minutes=30)), "FIRED")
        self.assertEqual(T.status_of(t, {"met": False, "last_fired": st["t.test"]["last_fired"]}, T0 + timedelta(minutes=30)), "cooling down")
        self.assertEqual(T.status_of(t, {"met": False, "last_fired": st["t.test"]["last_fired"]}, T0 + timedelta(minutes=61)), "armed")
        self.assertEqual(T.status_of({**t, "enabled": False}, {}, T0), "disabled")
        for minutes, px in ((90, 37.0), (105, 36.5)):                                             # after the cooldown it fires again
            st, fired, s = self.run_once([t], env({"SLV": px}), st, T0 + timedelta(minutes=minutes), send=s, log=self.log)
        self.assertEqual(len(s.sent), 2)

    def test_all_conditions_must_hold(self):
        t = T.clean({"name": "2nd", "conditions": [{"expr": "chaos", "op": "<", "value": 50}, {"expr": "hy_oas", "op": "<", "value": 4}], "cooldown_min": 0}, "t.two")
        st, _, s = self.run_once([t], env(), {})
        self.assertIs(st["t.two"]["met"], False)
        st, fired, s = self.run_once([t], env(c=ctx(chaos={"score": 45.0})), st, send=s)
        self.assertEqual(fired, ["t.two"])
        self.assertIn("chaos = 45", s.sent[0][1])
        self.assertIn("hy_oas = 3.52 (< 4.0)", s.sent[0][1])

    def test_a_missing_reading_neither_fires_nor_rearms(self):
        t = tracker("SLV", "<=", 36.71, cooldown_min=0)
        st, _, s = self.run_once([t], env({"SLV": 36.5}), {})                                     # recorded as already true
        st, fired, s = self.run_once([t], env({}), st, send=s)                                    # quote gone
        self.assertEqual((fired, st["t.test"]["met"]), ([], True))
        self.assertIn("no live quote", st["t.test"]["error"])
        st, fired, s = self.run_once([t], env({"SLV": 36.4}), st, send=s)
        self.assertEqual(fired, [])                                                               # it never went false: no new crossing

    def test_an_undelivered_push_is_sent_again_next_time(self):
        t = tracker("SLV", "<=", 36.71, cooldown_min=0)
        st, _, _ = self.run_once([t], env({"SLV": 37.0}), {})
        bad = Sender(ok=False)
        st, fired, _ = self.run_once([t], env({"SLV": 36.5}), st, send=bad)
        self.assertEqual((fired, st["t.test"]["met"]), ([], False))
        good = Sender()
        st, fired, _ = self.run_once([t], env({"SLV": 36.5}), st, send=good)
        self.assertEqual(fired, ["t.test"])

    def test_session_only_trackers_wait_for_the_session_and_24_7_ones_do_not(self):
        sess = tracker("SLV", "<=", 36.71, hours="session", id="t.s")
        always = tracker("BTC-USD", "<", 50000, hours="24/7", id="t.a")
        st, _, s = self.run_once([sess, always], env({"SLV": 37.0, "BTC-USD": 51000.0}), {}, session=False)
        self.assertEqual(sorted(st), ["t.a"])                                                     # the session-only tracker was not even looked at
        st, fired, s = self.run_once([sess, always], env({"SLV": 36.0, "BTC-USD": 49000.0}), st, session=False, send=s)
        self.assertEqual(fired, ["t.a"])
        self.assertEqual(s.sent[0][2], 3)

    def test_disabled_trackers_are_skipped(self):
        t = tracker("SLV", "<=", 36.71, enabled=False)
        st, fired, _ = self.run_once([t], env({"SLV": 30.0}), {})
        self.assertEqual((st, fired), ({}, []))

    def test_changes_fires_on_a_different_value_and_the_regime_alert_carries_the_read(self):
        t = next(T.clean(s, s["id"]) for s in T.SEEDS if s["id"] == "seed.regime")
        st, fired, s = self.run_once([t], env(), {}, log=self.log)                                # first look: records VOL_SHOCK
        self.assertEqual((fired, st[t["id"]]["last"]), ([], "VOL_SHOCK"))
        st, fired, s = self.run_once([t], env(), st, send=s, log=self.log)
        self.assertEqual(fired, [])                                                               # unchanged
        new = ctx(regime={"key": "CREDIT_STRESS", "regime": "CREDIT STRESS", "previous": {"regime": "VOLATILITY SHOCK"}, "read": {"status": "fresh", "text": "Credit is breaking."}})
        st, fired, s = self.run_once([t], env(c=new), st, send=s, log=self.log)
        self.assertEqual(fired, [t["id"]])
        title, text, prio, _ = s.sent[0]
        self.assertEqual((title, prio), ("Regime change", 5))                                     # urgent
        self.assertIn("Regime now: CREDIT STRESS (was VOLATILITY SHOCK)", text)
        self.assertIn("Read: Credit is breaking.", text)
        st, fired, s = self.run_once([t], env(c=new), st, send=s, log=self.log)                   # once per change
        st, fired, s = self.run_once([t], env(c=new), st, send=s, log=self.log)
        self.assertEqual(len(s.sent), 1)

    def test_an_offline_snapshot_never_alerts(self):
        t = next(T.clean(s, s["id"]) for s in T.SEEDS if s["id"] == "seed.regime")
        st, _, s = self.run_once([t], env(), {})
        offline = T.Env(None, "the latest run is offline, not a live run", lambda s: None)
        st, fired, s = self.run_once([t], offline, st, send=s)
        self.assertEqual((fired, s.sent), ([], []))
        self.assertEqual(st[t["id"]]["last"], "VOL_SHOCK")                                       # and what it knew is kept

    def test_snapshot_env_ignores_an_offline_run(self):
        from core import lake
        conn = lake.connect(Path(tempfile.mkdtemp()) / "p.db")
        lake.migrate(conn)
        lake.create_run(conn, "o1", mode="offline", trigger="manual", run_folder="x", code_version="t")
        lake.commit_snapshot(conn, "o1", {"run": {"run_id": "o1", "mode": "offline"}, "forecast": {"macro_regime": {"hy_oas": 9.9}}})
        lake.update_run(conn, "o1", status="completed")
        e = T.snapshot_env(conn, lambda s: None)
        self.assertIsNone(e.ctx)
        self.assertIn("offline", e.ctx_why)
        self.assertIn("offline", T.value_of("hy_oas", e)[1])
        conn.close()


class Storage(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp(prefix="trk_store_"))
        self.path = self.dir / "trackers.json"

    def test_unsaved_means_the_seeds_and_a_save_round_trips(self):
        self.assertEqual(len(T.load(self.path)), 7)
        t = T.upsert({"name": "mine", "conditions": [{"expr": "SLV", "op": "<", "value": 30}]}, self.path)
        items = T.load(self.path)
        self.assertEqual(len(items), 8)                                                          # the seeds are saved with it
        self.assertEqual(items[-1]["id"], t["id"])
        T.upsert({**t, "name": "renamed", "priority": "low"}, self.path)
        self.assertEqual([x["name"] for x in T.load(self.path) if x["id"] == t["id"]], ["renamed"])
        c = T.duplicate(t["id"], self.path)
        self.assertEqual((c["name"], c["enabled"]), ("renamed (copy)", False))
        T.delete(t["id"], self.path)
        self.assertEqual(len(T.load(self.path)), 8)
        with self.assertRaises(KeyError):
            T.delete("nope", self.path)

    def test_state_and_history_sit_beside_the_file(self):
        with mock.patch.object(config, "TRACKERS_FILE", str(self.path)):
            T.save_state({"a": {"met": True}})
            T.log_fire({"t": "2026-11-20T15:00:00+00:00", "name": "x", "text": "y"})
            self.assertTrue((self.dir / "trackers_state.json").exists())
            self.assertEqual(T.load_state(), {"a": {"met": True}})
            self.assertEqual(T.history()[0]["name"], "x")

    def test_a_folder_that_is_not_there_falls_back_to_the_data_folder_with_a_warning(self):
        gone = self.dir / "Volumes" / "1tb SSD" / "market-terminal" / "trackers.json"          # neither the folder nor its parent volume exists
        with mock.patch.object(config, "TRACKERS_FILE", str(gone)), self.assertLogs("core.trackers", "WARNING"):
            path, warn = T.store_path()
        self.assertEqual(path, config.DATA_DIR / "trackers.json")
        self.assertIn("not available", warn)
        self.assertFalse((self.dir / "Volumes").exists())                                         # nothing was created under the missing volume

    def test_a_missing_leaf_folder_on_a_present_volume_is_created(self):
        want = self.dir / "market-terminal" / "trackers.json"
        with mock.patch.object(config, "TRACKERS_FILE", str(want)):
            self.assertEqual(T.store_path(), (want, None))
        self.assertTrue(want.parent.is_dir())

    def test_blank_setting_uses_the_data_folder(self):
        self.assertEqual(T.store_path(), (config.DATA_DIR / "trackers.json", None))


class Routes(ApiCase):
    H = {"X-Tracker-Request": "1"}
    BODY = {"name": "Mine", "conditions": [{"expr": "SLV", "op": "<=", "value": 36.71}], "priority": "high", "cooldown_min": 30, "hours": "session"}

    def setUp(self):
        super().setUp()
        mock.patch.object(config, "TRACKERS_FILE", str(self.tmp / "trk" / "trackers.json")).start()
        mock.patch.object(config, "NTFY_URL", "").start()
        mock.patch.object(__import__("api_router"), "_trackers_readonly", lambda: False).start()

    def test_list_shows_the_seeds_with_live_values_missing_not_zero(self):
        j = self.get_json("/api/trackers")
        self.assertEqual(len(j["trackers"]), 7)
        self.assertTrue(j["writable"])
        slv = next(t for t in j["trackers"] if t["id"] == "seed.silver3671")
        self.assertIsNone(slv["live"]["conditions"][0]["value"])
        self.assertIn("no live quote", slv["live"]["conditions"][0]["error"])
        self.assertEqual(slv["status"], "armed")

    def test_live_values_come_from_the_latest_live_snapshot_and_the_bar_shows_the_distance(self):
        from core import lake
        conn = lake.connect(self.tmp / "portfolio.db")
        lake.migrate(conn)
        lake.create_run(conn, "r1", mode="live", trigger="manual", run_folder="x", code_version="t")
        lake.commit_snapshot(conn, "r1", {"run": {"run_id": "r1", "mode": "live"}, "forecast": {"macro_regime": {"hy_oas": 3.52}}, "chaos": {"score": 71.8},
                                          "regime": {"key": "VOL_SHOCK"}})
        lake.update_run(conn, "r1", status="completed")
        conn.close()
        j = self.get_json("/api/trackers")
        stop = next(t for t in j["trackers"] if t["id"] == "seed.stop")["live"]["conditions"][0]
        self.assertEqual((stop["value"], stop["met"]), (3.52, False))
        self.assertAlmostEqual(stop["gap_pct"], -21.78, places=1)
        self.assertGreater(stop["progress"], 0)
        reg = next(t for t in j["trackers"] if t["id"] == "seed.regime")
        self.assertEqual(reg["live"]["conditions"][0]["value"], "VOL_SHOCK")
        self.assertEqual(j["run_id"], "r1")

    def test_writes_need_the_header_and_the_same_origin(self):
        r = self.client.post("/api/trackers", json=self.BODY)
        self.assertEqual(r.status_code, 403)
        self.assertIn("X-Tracker-Request", r.get_json()["message"])
        r = self.client.post("/api/trackers", json=self.BODY, headers={**self.H, "Origin": "https://evil.example"})
        self.assertEqual((r.status_code, r.get_json()["message"]), (403, "cross-site request refused"))
        self.assertEqual(self.client.delete("/api/trackers/seed.stop").status_code, 403)
        self.assertEqual(self.client.post("/api/trackers/seed.stop/duplicate").status_code, 403)
        self.assertEqual(self.client.post("/api/trackers/seed.stop/test").status_code, 403)
        self.assertFalse((self.tmp / "trk" / "trackers.json").exists())

    def test_a_read_only_copy_refuses_every_write(self):
        mock.patch.object(__import__("api_router"), "_trackers_readonly", lambda: True).start()
        for r in (self.client.post("/api/trackers", json=self.BODY, headers=self.H), self.client.delete("/api/trackers/seed.stop", headers=self.H),
                  self.client.post("/api/trackers/seed.stop/duplicate", headers=self.H), self.client.post("/api/trackers/seed.stop/test", headers=self.H)):
            self.assertEqual(r.status_code, 403)
            self.assertIn("read-only", r.get_json()["message"])
        self.assertFalse(self.get_json("/api/trackers")["writable"])

    def test_add_edit_duplicate_delete(self):
        r = self.client.post("/api/trackers", json=self.BODY, headers=self.H)
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        tid = r.get_json()["tracker"]["id"]
        self.assertTrue((self.tmp / "trk" / "trackers.json").exists())                            # the folder was created, the file is the store
        r = self.client.post("/api/trackers", json={**self.BODY, "id": tid, "name": "Edited"}, headers=self.H)
        self.assertEqual(r.get_json()["tracker"]["name"], "Edited")
        names = [t["name"] for t in self.get_json("/api/trackers")["trackers"]]
        self.assertEqual((names.count("Edited"), len(names)), (1, 8))
        d = self.client.post(f"/api/trackers/{tid}/duplicate", headers=self.H).get_json()["tracker"]
        self.assertEqual((d["name"], d["enabled"]), ("Edited (copy)", False))
        self.assertEqual(self.client.delete(f"/api/trackers/{tid}", headers=self.H).status_code, 200)
        self.assertEqual(self.client.delete(f"/api/trackers/{tid}", headers=self.H).status_code, 404)

    def test_a_bad_tracker_is_a_400_with_the_reason(self):
        r = self.client.post("/api/trackers", json={**self.BODY, "conditions": [{"expr": "bogus_metric", "op": "<", "value": 1}]}, headers=self.H)
        self.assertEqual(r.status_code, 400)
        self.assertIn("unknown metric", r.get_json()["message"])
        self.assertEqual(self.client.post("/api/trackers", data="x", headers=self.H).status_code, 400)

    def test_test_sends_a_push_titled_test_at_the_trackers_priority(self):
        self.assertEqual(self.client.post("/api/trackers/seed.stop/test", headers=self.H).status_code, 503)    # no NTFY_URL
        mock.patch.object(config, "NTFY_URL", "https://ntfy.example/topic").start()
        s = Sender()
        mock.patch.object(T, "send_ntfy", s).start()
        r = self.client.post("/api/trackers/seed.stop/test", headers=self.H)
        self.assertEqual((r.status_code, r.get_json()["title"]), (200, "TEST · STOP: HY OAS > 4.5%"))
        self.assertEqual(s.sent[0][2], 5)
        s.ok = False
        self.assertEqual(self.client.post("/api/trackers/seed.stop/test", headers=self.H).status_code, 502)
        self.assertEqual(self.client.post("/api/trackers/none/test", headers=self.H).status_code, 404)

    def test_preview_and_metrics(self):
        j = self.get_json("/api/trackers/preview?expr=hy_oas&op=%3C&value=4")
        self.assertIn("hy_oas", j["error"])                                                      # no database in this scratch folder: an error, not a zero
        self.assertIsNone(j["value"])
        r = self.client.get("/api/trackers/preview?expr=nope_metric")
        self.assertEqual(r.status_code, 400)
        ids = {m["id"] for m in self.get_json("/api/trackers/metrics")["metrics"]}
        self.assertTrue({"hy_oas", "chaos", "regime", "btc_verdict", "macro.vmri.score"} <= ids)

    def test_the_cards_script_and_modal_are_on_the_page(self):
        html = self.client.get("/").get_data(as_text=True)
        self.assertIn('id="fcT"', html)
        self.assertIn('id="trkModal"', html)

    def test_the_job_is_wired_into_the_cli_and_setup(self):
        import main_pipeline
        with mock.patch.object(sys, "argv", ["x", "tracker-alerts", "--help"]), self.assertRaises(SystemExit) as cm:
            main_pipeline.main()
        self.assertEqual(cm.exception.code, 0)
        setup = (Path(__file__).resolve().parents[1] / "setup.sh").read_text()
        self.assertIn("tracker-alerts", setup)
        self.assertIn("<key>StartInterval</key>", setup)


if __name__ == "__main__":
    unittest.main()
