"""The analyst note (Briefing): the brief, the validator, both drivers (mocked), the pipeline stage, replay, the console
shapers and the read-only `mtq` command. All offline: no network, no model, no `claude` process."""
import sys, unittest
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent))
import support  # noqa: F401  (temporary CME_Data, no credentials; must come before importing config)

import contextlib
import copy
import io
import json
import os
import subprocess
from unittest import mock

import config
from core import lake, narrative as N, render

ROOT = support.ROOT
sys.path.insert(0, str(ROOT / "options_whale"))
import main_pipeline as mp  # noqa: E402


def clusters():
    def c(i, title, hours, official=False):
        return {"id": f"S{i}", "title": title, "published_at": f"2026-10-09T{hours}:00+00:00", "official": official, "sentiment": -0.3,
                "sources": [{"name": "fed_press" if official else "wire", "url": f"https://example.test/{i}"}], "category": "markets", "new": True}
    return [c(1, "Wire headline about oil", "15:30"), c(2, "Fed press release on rates", "13:59", True), c(3, "Another wire item", "16:00")]


def make_ctx(**over):
    ctx = {"run": {"run_id": "20261010T182206Z-abc123", "generated_local": "2026-10-10 01:22 PM CDT", "mode": "live", "trigger": "manual"},
           "prices": {k: {"value": v, "change": ch, "prev_close": v - ch, "observed_at": "2026-10-09T00:00:00-04:00", "status": "fresh"}
                      for k, v, ch in (("spy", 778.57, 4.64), ("slv", 54.78, 1.33), ("gold", 4216.3, 59.3), ("silver_futures", 60.67, 0.5),
                                       ("vix", 14.84, -0.57), ("dxy", 102.21, 0.07), ("tnx", 5.244, 0.013))},
           "vmri": {"score": 194.5, "tier": "MODERATE RISK (Standard)", "inputs": {"dxy": 102.21, "tnx": 5.24, "oas": 3.15, "vix": 14.84}},
           "chaos": {"status": "fresh", "score": -13.9, "regime": "neutral", "as_of": "2026-10-09", "components_used": 10, "components_total": 12,
                     "prev": {"date": "2026-10-08", "regime": "neutral", "score": -10.5},
                     "drivers": [{"key": "vvix", "label": "VVIX", "value": 84.88, "z": -0.87}],
                     "components_missing": {"skew": "short history"}, "election": {"date": "2026-11-03", "trading_days_to": 17},
                     "record": {"days": 60, "high": {"date": "2026-07-29", "score": 7.4}, "low": {"date": "2026-08-14", "score": -44.4}},
                     "levers": {"rows": [{"status": "fresh", "label": "Bank reserves", "level": 3029.659, "unit": "USD bn", "as_of": "2026-10-07", "change_20d": 38.349}]},
                     "event_study": {"status": "fresh", "rows": [{"series": "VIX", "window": "0..+5", "mean_pct": 1.5, "base_mean_pct": 0.2, "hit_rate": 0.84, "n": 19, "grade": "thin"}]}},
           "execution": {"total_score": 3, "directional_bias": "BULLISH (+3) [CALL]"},
           "calendar": {"status": "fresh", "today_tier1": [], "upcoming": [], "upcoming_releases": [{"date_ct": "2026-10-14", "title": "CPI release"}]},
           "news": {"status": "fresh", "clusters": clusters(), "coverage": {"answered": 15, "enabled": 15}}}
    ctx.update(over)
    return ctx


NOTE = {"tagline": "SPY rose 4.64; VIX fell 0.57 [D2]", "focus": ["SPY rose 4.64 [D2]", "VIX fell 0.57 [D5]", "Chaos score is -13.9 [D8]"],
        "issues": [{"what_moved": "SPY rose 0.6% [D2]", "reaction": "Gold rose 59.3 [D4]", "contradiction": "none", "would_change": "VIX above 15 [D6]", "refs": ["D2", "S2"]},
                   {"what_moved": "Chaos score moved from -10.5 to -13.9 [D8]", "reaction": "Fed press release [S2]", "contradiction": "none",
                    "would_change": "Chaos score above -10 [D8]", "refs": ["D8", "S2"]},
                   {"what_moved": "Oil headline [S1]", "reaction": "none", "contradiction": "none", "would_change": "none", "refs": ["S1"]}]}


class Fake:
    """chat(system, user, max_tokens, timeout) replaying scripted replies; `clock` makes each call take `cost` seconds."""
    def __init__(self, *replies, clock=None, cost=0):
        self.replies, self.calls, self.clock, self.cost = list(replies), [], clock, cost

    def __call__(self, system, user, max_tokens, timeout):
        self.calls.append((system, user, max_tokens, timeout))
        if self.clock:
            self.clock[0] += self.cost
        r = self.replies.pop(0)
        if isinstance(r, Exception):
            raise r
        return r


def good_chat():
    return Fake("REGIME: calm [D8]", json.dumps(NOTE))


class Brief(unittest.TestCase):
    def test_inputs_fits_resolves_ids_and_orders_official_first(self):
        i = N.inputs(make_ctx(), 8000)
        self.assertLessEqual(len(i["text"]), 8000)
        self.assertEqual(i["refs"]["S2"]["url"], "https://example.test/2")
        self.assertTrue(i["refs"]["S2"]["official"])
        self.assertTrue(any(v.get("label") == "VMRI" for v in i["refs"].values()))
        self.assertLess(i["text"].index("[S2]"), i["text"].index("[S3]"))          # official first, then newest
        self.assertLess(i["text"].index("[S3]"), i["text"].index("[S1]"))
        self.assertIn(778.57, i["numbers"])
        self.assertIn(17.0, i["numbers"])

    def test_inputs_trims_to_a_small_budget(self):
        i = N.inputs(make_ctx(), 1500)
        self.assertLessEqual(len(i["text"]), 1500)
        self.assertIn("RUN", i["text"])

    def test_missing_and_old_snapshots(self):
        for ctx in (make_ctx(chaos={"status": "missing", "reason": "no data"}, news={"status": "missing", "reason": "feeds down"}),
                    {k: v for k, v in make_ctx().items() if k not in ("chaos", "news", "calendar", "execution", "vmri")}):
            i = N.inputs(ctx, 4000)
            self.assertIn("RUN", i["text"])
            self.assertTrue("missing" in i["text"] or "not in this snapshot" in i["text"])


class Validator(unittest.TestCase):
    def setUp(self):
        self.inp = N.inputs(make_ctx(), 8000)

    def check(self, text):
        return N.check_bullet(text, self.inp)

    def test_numbers(self):
        self.assertIsNone(self.check("SPY is 778.57 [D2]"))
        self.assertIsNone(self.check("SPY rose 0.6% [D2]"))                 # a rounding of 0.60 as shown
        self.assertIsNone(self.check("Reserves fell 38 [D6]"))              # rounding of 38.349 to 0 decimals
        self.assertIsNone(self.check("Gold is down 14.84"))                 # absolute values: the brief has -0.57 and 14.84
        self.assertIsNone(self.check("Reserves 3,029.7 on 2026-10-09"))         # commas, dates
        self.assertIn("999.5", self.check("SPY is 999.5 [D2]"))

    def test_ids_and_brackets(self):
        self.assertIn("unknown id S99", self.check("A headline [S99]"))
        self.assertIn("invalid citation", self.check("A reading [NARRATIVES 1]"))
        self.assertIsNone(self.check("A reading [D2][S1]"))
        self.assertIsNone(self.check("Two readings [D2, S1]"))                    # several ids in one bracket (Claude writes this)
        self.assertIn("unknown id S99", self.check("A reading [D2, S99]"))

    def test_causal_and_advice_wording(self):
        for bad in ("SPY fell because VIX rose", "Gold rose due to the dollar", "VIX fell as the dollar edged up", "Driven by oil, SPY rose",
                    "Stocks rose amid calm", "Oil sparked a move", "You should hold", "Consider a buy", "Set a price target", "Go long SPY"):
            self.assertIsNotNone(self.check(bad), bad)
        for fine in ("short volume share rose", "Sell-side notes were mixed", "Treasury buybacks fell", "As of 2026-10-09 SPY is 778.57",
                     "SPY is as high as 778.57", "VIX fell; DXY rose"):
            self.assertIsNone(self.check(fine), fine)

    def test_facts_need_a_number_or_a_headline_and_direction_words_agree_with_the_sign(self):
        self.assertIn("no number", N.check_bullet("Yields retreated [D8]", self.inp, fact=True))      # seen live: yields had risen
        self.assertIsNone(N.check_bullet("Yields retreated [D8]", self.inp))                          # not a statement-of-fact field
        self.assertIsNone(N.check_bullet("Fed press release on rates [S2]", self.inp, fact=True))     # a headline the reader can open
        self.assertIsNone(N.check_bullet("none", self.inp, fact=True))
        self.assertIn("direction", self.check("VIX rose -3.70% [D5]"))
        self.assertIn("direction", self.check("SPY fell +0.60% [D2]; VIX fell -3.70% [D5]"))
        self.assertIsNone(self.check("SPY rose +0.60% [D2]; VIX fell -3.70% [D5]"))

    def test_json_of_the_wrong_shape_is_a_missing_note_not_an_exception(self):
        for bad in ({"tagline": "x", "focus": {"a": 1}, "issues": []}, {"issues": {"a": 1}}, {"issues": [{"what_moved": "SPY rose 4.64 [D2]", "refs": 5}]}):
            calls = iter(["REGIME: calm [D2]", json.dumps(bad)])
            r = N.write(make_ctx(), lambda *a: next(calls), engine="local", model="m")
            self.assertEqual(r["status"], "missing", bad)

    def test_event_study_rows_without_statistics_do_not_break_the_brief(self):
        block = copy.deepcopy(make_ctx()["chaos"])
        block["event_study"]["rows"] = [{"series": "VIX", "window": "0..+5", "n": 5, "mean_pct": None, "base_mean_pct": None, "hit_rate": None, "grade": "none"}]
        self.assertTrue(any("too few cases" in line and "n 5" in line for _label, line in N.chaos_rows(block)))

    def test_validate_keeps_good_issues_and_drops_bad_refs(self):
        note = copy.deepcopy(NOTE)
        note["issues"][0]["refs"] = ["D2", "S99"]
        note["issues"][1]["reaction"] = "Gold rose because yields fell"
        clean, dropped, counts = N.validate(note, self.inp)
        self.assertEqual(clean["issues"][0]["refs"], ["D2"])                # unknown id removed from refs
        self.assertEqual(clean["issues"][1]["reaction"], "")                # the causal statement is gone
        self.assertEqual(counts["bullets_dropped"], 1)
        self.assertIn("causal", dropped[0]["reason"])

    def test_issue_without_a_valid_ref_or_what_moved_is_dropped(self):
        note = copy.deepcopy(NOTE)
        note["issues"][0]["refs"] = ["S99"]
        note["issues"][1]["what_moved"] = "SPY is 12345 [D2]"
        clean, dropped, counts = N.validate(note, self.inp)
        self.assertEqual(counts["issues_kept"], 1)


class Write(unittest.TestCase):
    ctx = make_ctx()

    def write(self, chat, **kw):
        return N.write(self.ctx, chat, engine="local", model="m1", max_chars=8000, **kw)

    def test_valid_note_is_fresh_and_renders_with_sources(self):
        chat = good_chat()
        r = self.write(chat)
        self.assertEqual(r["status"], "fresh", r["reason"])
        self.assertEqual(len(chat.calls), 2)
        text = N.render_text(r)
        self.assertIn("local model m1", text)
        self.assertIn("3 of 3 issues kept", text)
        self.assertIn("S2 | fed_press | Fed press release on rates | https://example.test/2", text)
        self.assertIn("D2 | SPY", text)

    def test_fewer_than_two_issues_is_missing(self):
        note = copy.deepcopy(NOTE)
        for i in note["issues"][:2]:
            i["what_moved"] = "SPY fell because of oil [D2]"
        r = self.write(Fake("memo", json.dumps(note)))
        self.assertEqual(r["status"], "missing")
        self.assertIn("kept 1 of 3", r["reason"])
        self.assertTrue(r["dropped"])
        self.assertEqual(N.render_text(r), f"Briefing unavailable: {r['reason']}")

    def test_invalid_json_twice_is_missing_and_once_gets_one_retry(self):
        chat = Fake("memo", "not json", "still not json")
        self.assertEqual(self.write(chat)["status"], "missing")
        self.assertEqual(len(chat.calls), 3)                                # memo, note, one repair
        chat = Fake("memo", "```json\n{broken", "Here: " + json.dumps(NOTE))
        r = self.write(chat)
        self.assertEqual(r["status"], "fresh")
        self.assertEqual(len(chat.calls), 3)

    def test_driver_error_and_budget_become_missing(self):
        r = self.write(Fake(N.DriverError("local model not reachable: ConnectionError")))
        self.assertEqual((r["status"], r["reason"]), ("missing", "local model not reachable: ConnectionError"))
        r = self.write(Fake(RuntimeError("weird")))
        self.assertEqual(r["status"], "missing")
        clock = [0.0]
        with mock.patch.object(N, "_clock", lambda: clock[0]):
            r = self.write(Fake("memo", json.dumps(NOTE), clock=clock, cost=100), budget_s=150)
        self.assertEqual(r["status"], "missing")
        self.assertIn("out of time", r["reason"])

    def test_numbers_the_analyst_looked_up_are_verifiable(self):
        note = copy.deepcopy(NOTE)
        note["issues"][0]["what_moved"] = "COMEX registered is 95494503 [D2]"
        chat = Fake("memo", json.dumps(note))
        chat.tool_results = ["registered 95494503"]
        r = self.write(chat)
        self.assertEqual(r["counts"]["issues_kept"], 3)
        self.assertEqual(r["prompt"]["tool_results"], ["registered 95494503"])


class Claude(unittest.TestCase):
    def test_argument_list_grants_only_mtq_and_blocks_writes(self):
        a = N.claude_args("SYS", "PROMPT", "haiku", 0.1)
        self.assertEqual(a[:3], ["claude", "-p", "PROMPT"])
        self.assertEqual(a[a.index("--allowedTools") + 1], "Bash(mtq *)")
        self.assertEqual(a[a.index("--tools") + 1], "Bash")
        self.assertEqual(a.count("Bash"), 1)
        dis = a[a.index("--disallowedTools") + 1:a.index("--setting-sources")]
        self.assertEqual(dis, ["Edit", "Write", "NotebookEdit", "WebFetch", "WebSearch"])
        self.assertEqual(a[a.index("--setting-sources") + 1], "project")          # the operator's settings and plugins stay out
        hook = json.loads(a[a.index("--settings") + 1])["hooks"]["PreToolUse"][0]
        self.assertEqual(hook["matcher"], "")                                    # every tool call goes through the guard
        self.assertTrue(hook["hooks"][0]["command"].endswith('mtq" --guard'))
        for flag, value in (("--permission-mode", "dontAsk"), ("--max-turns", "8"), ("--max-budget-usd", "0.1"), ("--effort", "low"),
                            ("--model", "haiku"), ("--system-prompt-snapshot", "off"), ("--output-format", "stream-json")):
            self.assertEqual(a[a.index(flag) + 1], value)
        for flag in ("--strict-mcp-config", "--disable-slash-commands", "--verbose"):
            self.assertIn(flag, a)
        self.assertTrue(a[a.index("--system-prompt") + 1].startswith("SYS"))
        self.assertIn("mtq", a[a.index("--system-prompt") + 1])

    def test_only_sonnet_and_haiku(self):
        for bad in ("opus", "sonnet; rm -rf", ""):
            with self.assertRaises(N.DriverError):
                N.claude_args("s", "p", bad)
            with self.assertRaises(N.DriverError):
                N.ClaudeChat(bad)

    def test_environment_is_scrubbed(self):
        base = {"PATH": "/usr/bin", "CLAUDECODE": "1", "CLAUDE_PID": "7", "CLAUDE_EFFORT": "x", "CLAUDE_CODE_SSE_PORT": "9", "KEEP": "y"}
        env = N.claude_env(base, "/data", "/py")
        self.assertEqual(sorted(k for k in env if k.startswith("CLAUDE")), [])
        self.assertEqual(env["KEEP"], "y")
        self.assertTrue(env["PATH"].startswith(str(N.BIN_DIR) + os.pathsep))
        self.assertEqual(env["PATH"].count(str(N.BIN_DIR)), 1)
        self.assertEqual(env["PORTFOLIO_DATA_DIR"], "/data")

    def test_stream_parser(self):
        lines = [{"type": "system", "subtype": "init"},
                 {"type": "assistant", "message": {"content": [{"type": "tool_use", "name": "Bash", "input": {"command": "mtq chaos"}}]}},
                 {"type": "user", "message": {"content": [{"type": "tool_result", "content": "score -13.9"}]}},
                 {"type": "user", "message": {"content": [{"type": "tool_result", "content": [{"type": "text", "text": "rows 3"}]}]}},
                 {"type": "result", "subtype": "success", "is_error": False, "result": "FINAL", "total_cost_usd": 0.05, "num_turns": 3}]
        r = N.parse_stream("\n".join(json.dumps(x) for x in lines) + "\nnot json")
        self.assertEqual((r["result"], r["cost"], r["turns"]), ("FINAL", 0.05, 3))
        self.assertEqual(r["tool_results"], ["score -13.9", "rows 3"])

    def test_driver_runs_a_mocked_process_and_a_missing_cli_is_clear(self):
        out = json.dumps({"type": "result", "result": "OK", "total_cost_usd": 0.01, "num_turns": 1})
        with mock.patch("subprocess.run", return_value=mock.Mock(stdout=out, stderr="", returncode=0)) as run:
            chat = N.ClaudeChat("sonnet")
            self.assertEqual(chat("s", "u", 100, 60), "OK")
        self.assertEqual(run.call_args.kwargs["cwd"].name, "analyst_sandbox")
        self.assertEqual(list(run.call_args.kwargs["cwd"].iterdir()), [])        # empty: no CLAUDE.md, no .mcp.json
        self.assertNotIn("CLAUDECODE", run.call_args.kwargs["env"])
        with mock.patch("subprocess.run", side_effect=FileNotFoundError):
            r = N.write(make_ctx(), N.ClaudeChat("sonnet"), engine="claude", model="sonnet")
        self.assertEqual(r["status"], "missing")
        self.assertIn("not installed", r["reason"])


class Mtq(unittest.TestCase):
    def run_mtq(self, *args):
        env = {**os.environ, "MTQ_PYTHON": sys.executable}
        return subprocess.run([str(N.BIN_DIR / "mtq"), *args], capture_output=True, text=True, env=env, timeout=120)

    def test_allowlist(self):
        for src in ("darkpool", "gex", "option_chain", "positions", "nope"):
            self.assertNotIn(src, N.MTQ_SOURCES)
            p = self.run_mtq(src)
            self.assertEqual(p.returncode, 2, src)
            self.assertIn("source must be one of", p.stderr)
        self.assertIn("chaos", N.MTQ_SOURCES)
        p = self.run_mtq("chaos")                                            # no snapshot in the test folder: an error text, exit 0
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertTrue(p.stdout.strip())


    def test_guard_rejects_bash_and_every_other_tool(self):
        """Claude Code runs read-only commands such as `ls` despite an allowlist, so the runner's own hook decides."""
        for cmd in ("ls -la", "cat ../.env", "mtq chaos; echo x > y", "mtq chaos | head", "mtq chaos && ls", "mtq $(ls)", "mtq `ls`",
                    "mtq chaos > out.txt", "mtq darkpool", "mtq", "python3 -c 1", "mtq 'chaos'", "mtq chaos\nls", "mtq a b c d e f"):
            self.assertFalse(N.bash_allowed(cmd), cmd)
        for cmd in ("mtq chaos", "mtq forecast ticker=SPY data.trend", "mtq eia_history path=series.0 last=5", " mtq run_status "):
            self.assertTrue(N.bash_allowed(cmd), cmd)

        def guard(call):
            return subprocess.run([str(N.BIN_DIR / "mtq"), "--guard"], input=json.dumps(call), capture_output=True, text=True,
                                  env={**os.environ, "MTQ_PYTHON": sys.executable}, timeout=60)
        self.assertEqual(guard({"tool_name": "Bash", "tool_input": {"command": "mtq chaos"}}).returncode, 0)
        for call in ({"tool_name": "Bash", "tool_input": {"command": "ls"}}, {"tool_name": "Write", "tool_input": {"file_path": "x"}},
                     {"tool_name": "Read", "tool_input": {"file_path": "/etc/hosts"}}, {}):
            p = guard(call)
            self.assertEqual(p.returncode, 2, call)
            self.assertIn("blocked", p.stderr)


class Console(unittest.TestCase):
    def test_chaos_and_news_shapers(self):
        import assistant as A
        self.assertIn("chaos", A.SOURCES)
        self.assertIn("Chaos", A.catalog_text())
        out = A._shape_chaos({"as_of": "2026-10-09", "data": make_ctx()["chaos"]}, {})
        self.assertTrue(any("score -13.9" in r for r in out["rows"]))
        self.assertTrue(any("Bank reserves" in r and "4-week change +38.3" in r for r in out["rows"]))
        self.assertTrue(any("evidence grade thin" in r and "n 19" in r for r in out["rows"]))
        xml = {"macro_news": {"as_of": "2026-10-10T18:00:00+00:00", "status": "fresh", "feeds_answered": "15", "feeds_enabled": "15",
                              "article": [{"published": "2026-10-09 15:30 UTC", "title": "T1", "link": "https://e/1", "sentiment": "0.1", "source": "wire", "official": "false"},
                                          {"published": "2026-10-09 16:00 UTC", "title": "T2", "link": "https://e/2", "sentiment": "0.0", "source": "fed_press", "official": "true"}]}}
        news = A._shape_news(xml, {})
        self.assertEqual(news["official_sources"], ["2026-10-09 11:00 fed_press (sentiment 0.0): T2"])      # Chicago time, its own list
        self.assertEqual(news["other_sources"], ["2026-10-09 10:30 wire (sentiment 0.1): T1"])
        self.assertEqual(news["coverage"], "feeds answered 15 of 15")


# ------------------------------------------------------------ pipeline stage, email, push, replay (child process)
TESTS_DIR = Path(__file__).resolve().parent


class Pipeline(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        env = {k: v for k, v in os.environ.items() if k != "PORTFOLIO_DATA_DIR"}
        proc = subprocess.run([sys.executable, "-c", "import test_narrative as t; t._child_main()"], cwd=TESTS_DIR, env=env,
                              capture_output=True, text=True, timeout=900)
        lines = [ln for ln in proc.stdout.splitlines() if ln.startswith("RESULT:")]
        if proc.returncode != 0 or not lines:
            raise AssertionError(f"child failed ({proc.returncode}):\n{proc.stdout[-2000:]}\n{proc.stderr[-3000:]}")
        cls.r = json.loads(lines[-1][len("RESULT:"):])

    def test_offline_and_unset_url_skip_the_stage(self):
        self.assertEqual(self.r["offline"]["stage"], {"status": "skipped", "reason": "offline run"})
        self.assertEqual(self.r["unset"]["stage"]["status"], "skipped")
        self.assertIn("ASSISTANT_LOCAL_URL", self.r["unset"]["stage"]["reason"])
        self.assertEqual(self.r["unset"]["body_is_report"], True)           # no heading, no placeholder
        self.assertEqual(self.r["unset"]["note_passed"], None)

    def test_a_raising_write_is_a_warning_and_the_run_still_renders_and_delivers(self):
        r = self.r["raising"]
        self.assertEqual(r["rc"], 3)
        self.assertTrue(any("narrative error: boom" in w for w in r["warnings"]), r["warnings"])
        self.assertTrue(r["rendered"])
        self.assertTrue(r["delivered"])
        self.assertTrue(r["body_is_report"])

    def test_missing_note_is_a_normal_outcome_with_one_line_in_the_email(self):
        r = self.r["unreachable"]
        self.assertEqual(r["rc"], 0, r)
        self.assertEqual(r["stage"]["status"], "missing")
        self.assertIn("not reachable", r["stage"]["reason"])
        self.assertTrue(r["body"].startswith("Briefing unavailable: local model not reachable"))

    def test_fresh_note_is_stored_emailed_pushed_and_kept_out_of_the_report(self):
        r = self.r["fresh"]
        self.assertEqual(r["rc"], 0, r)
        self.assertEqual(r["stage"]["status"], "fresh")
        self.assertEqual(sorted(r["payload_kinds"]), ["narrative_json", "narrative_prompt", "narrative_text"])
        self.assertTrue(all("payload_id" in r["files"][f"{k}.local"] and r["files"][f"{k}.local"]["engine"] == "local"
                            for k in ("narrative_json", "narrative_prompt", "narrative_text")))
        self.assertTrue(r["body"].startswith("BRIEFING (local model fake-model)"))
        self.assertIn("Stocks rose", r["html"])
        self.assertNotIn("BRIEFING", r["report_file"])                       # the uploaded file is the report only
        self.assertNotIn("Stocks rose", r["report_file"])
        self.assertTrue(r["body"].rstrip("\n").endswith(r["report_file"].rstrip("\n")))
        self.assertIn("Stocks rose", r["push_text"])
        self.assertLess(r["push_bytes"], 2000)
        self.assertEqual(r["note_status_passed"], "fresh")
        self.assertRegex(r["note_file"], r"^\d{4}-\d{2}-\d{2}_\d{4}_local_.+\.md$")

    def test_replay_reuses_the_stored_note_without_a_model_call(self):
        r = self.r["replay"]
        self.assertEqual(r["rc"], 0)
        self.assertEqual(r["body"], self.r["fresh"]["body"])
        self.assertIn("report identical to original: True", r["out"])
        self.assertIn("email body == report: True", r["out"])
        self.assertTrue(self.r["replay_plain"]["body_is_report"])           # a run without a note replays as before


def _child_main():
    """Runs in a subprocess. Prints one RESULT line of JSON."""
    import email as _email
    from core import collect
    from core.importer import import_history
    import send_email

    out = {}
    data = config.DATA_DIR

    def capture(fn, *a, **kw):
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = fn(*a, **kw)
        return rc, buf.getvalue()

    def last_run(**where):
        conn = lake.connect(config.DB_PATH)
        sql = "SELECT * FROM v2_runs" + (" WHERE " + " AND ".join(f"{k} = ?" for k in where) if where else "")
        row = dict(conn.execute(sql + " ORDER BY rowid DESC LIMIT 1", tuple(where.values())).fetchone())
        conn.close()
        return row

    import_history(verbose=False)
    conn = lake.connect(config.DB_PATH)
    lake.migrate(conn)
    capture(mp.run, offline=True, deliver=False)
    row = last_run(mode="offline")
    out["offline"] = {"stage": json.loads(row["stages_json"])["narrative"]}
    base_ctx = lake.load_snapshot(conn, row["run_id"])

    def fake_build(conn_, session, run_meta, **kw):
        ctx = copy.deepcopy(base_ctx)
        ctx["run"] = {**ctx["run"], **run_meta}
        ctx["news"] = {"status": "fresh", "clusters": clusters(), "coverage": {"answered": 1, "enabled": 1}}
        return ctx, {}

    passed = []

    def fake_deliver(conn_, ctx, run_id, out_dir, report, *, upload, note=None):
        passed.append(note)
        return []

    def body_of(run):
        d = Path(json.loads(run["artifacts_json"])["out_dir"])
        msg = send_email.load_eml(d / "email.eml")
        html = msg.get_body(preferencelist=("html",)).get_content()
        return d, send_email.plain_body(msg), html, (d / "daily_market_report.txt").read_text()

    def live(url, write=None, chat=None):
        passed.clear()
        settings = (url, "fake-model", "k")
        with mock.patch.object(collect, "build_context", fake_build), mock.patch.object(collect, "context_observations", lambda c, f: []), \
                mock.patch.object(mp, "_deliver", fake_deliver), mock.patch.object(N, "local_settings", lambda: settings), \
                mock.patch.object(N, "write", write or N.write), mock.patch.object(N, "chat_local", chat or N.chat_local):
            rc, text = capture(mp.run, offline=False, deliver=True, upload=False)
        run = last_run(mode="live")
        d, body, html, report = body_of(run)
        return {"rc": rc, "run": run, "stages": json.loads(run["stages_json"]), "dir": d, "body": body, "html": html, "report_file": report}

    u = live(None)
    out["unset"] = {"stage": u["stages"]["narrative"], "body_is_report": u["body"].rstrip("\n") == u["report_file"].rstrip("\n"),
                    "note_passed": passed[0] if passed else "not delivered"}

    def boom(*a, **k):
        raise RuntimeError("boom")
    b = live("http://x", write=boom)
    out["raising"] = {"rc": b["rc"], "warnings": b["stages"].get("warnings"), "rendered": "export" in b["stages"], "delivered": len(passed) == 1,
                      "body_is_report": b["body"].rstrip("\n") == b["report_file"].rstrip("\n")}

    def down(system, user, max_tokens, timeout):
        raise N.DriverError("local model not reachable: ConnectionError")
    m = live("http://x", chat=down)
    out["unreachable"] = {"rc": m["rc"], "stage": m["stages"]["narrative"], "body": m["body"]}

    def smart_chat(system, user, max_tokens, timeout):
        brief = user.split("\n\nTASK")[0]
        if "MEMO" not in user:
            return "REGIME: calm [D2]"
        note = {"tagline": "Stocks rose in 2026; volatility fell [D2]", "focus": ["SPY moved in 2026 [D2]", "VIX moved in 2026 [D3]", "Gold moved in 2026 [D4]"],
                "issues": [{"what_moved": "SPY moved in 2026 [D2]", "reaction": "VIX moved in 2026 [D3]", "contradiction": "none", "would_change": "VIX level [D3]", "refs": ["D2", "D3"]}
                           for _ in range(3)]}
        return json.dumps(note)
    f = live("http://x", chat=smart_chat)
    run = f["run"]
    conn.close()
    conn = lake.connect(config.DB_PATH)
    files = json.loads(run["artifacts_json"])["files"]
    kinds = [r["kind"] for r in conn.execute("SELECT kind FROM v2_payloads WHERE source = 'narrative' AND first_run_id = ?", (run["run_id"],))]
    title, push, prio, tags = render.ntfy_summary(lake.load_snapshot(conn, run["run_id"]), passed[0])
    out["fresh"] = {"rc": f["rc"], "stage": f["stages"]["narrative"], "payload_kinds": kinds, "files": files, "body": f["body"], "html": f["html"],
                    "report_file": f["report_file"], "push_text": push, "push_bytes": len(push.encode()),
                    "note_status_passed": passed[0]["status"],
                    "note_file": next((data / "analyst_notes").iterdir()).name if (data / "analyst_notes").exists() else None}

    def no_model(*a, **k):
        raise AssertionError("replay must not call a model")
    with mock.patch.object(N, "write", no_model), mock.patch.object(N, "chat_local", no_model):
        rc, text = capture(mp.replay, run["run_id"])
    rrun = last_run(mode="replay")
    d, body, html, report = body_of(rrun)
    out["replay"] = {"rc": rc, "out": text, "body": body}
    # a run that never had a note replays exactly as before
    rc, text = capture(mp.replay, row["run_id"])
    d, body, html, report = body_of(last_run(mode="replay"))
    out["replay_plain"] = {"body_is_report": body.rstrip("\n") == report.rstrip("\n"), "out": text}
    print("RESULT:" + json.dumps(out, default=str))


if __name__ == "__main__":
    unittest.main()
