"""The Claude engine of the console assistant, with the Anthropic client mocked (no network, no key)."""
import os
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace as NS
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
import support  # noqa: E402,F401  (sets up the environment)

sys.path.insert(0, str(support.ROOT / "options_whale"))
import assistant as A  # noqa: E402
A.NINEB_OFF = Path("/nonexistent/.imessage_9b_off")   # never the live dash switch (on Cloud the engine checks would see claude)
from flask import Flask, jsonify  # noqa: E402


def _app():
    app = Flask(__name__)
    app.add_url_rule("/api/gex", "gex", lambda: jsonify({"data": {"spot": 769.43, "callWall": 775.0}}))
    return app


def _msg(content, stop):
    return NS(content=content, stop_reason=stop, usage=NS(output_tokens=7))


class FakeStream:
    def __init__(self, events, final):
        self.events, self.final = events, final

    def __enter__(self): return self
    def __exit__(self, *a): return False
    def __iter__(self): return iter(self.events)
    def get_final_message(self): return self.final


def _delta(kind, **kw):
    return NS(type="content_block_delta", delta=NS(type=kind, **kw))


class ClaudeEngine(unittest.TestCase):
    def run_it(self, rounds):
        sent = []

        def stream(**kw):
            sent.append(kw)
            return FakeStream(*rounds[len(sent) - 1])
        client = NS(messages=NS(stream=stream))
        with mock.patch.dict(os.environ, {"ANTHROPIC_API_KEY": "sk-test"}), \
             mock.patch.object(A, "_claude_client", return_value=client), \
             mock.patch.object(A, "brief", lambda app: "BRIEF TEXT"):
            return list(A.run_claude(_app(), {"turns": []}, "where is the gex call wall right now", )), sent

    def test_tool_round_then_answer_has_the_local_event_shape(self):
        use = NS(type="tool_use", id="tu_1", name="fetch_source", input={"source": "gex", "query": "ticker=SPY"})
        think = NS(type="thinking", thinking="", signature="sig")
        rounds = [([_delta("thinking_delta", thinking="hm")], _msg([think, use], "tool_use")),
                  ([_delta("text_delta", text="The call wall is 775.")], _msg([NS(type="text", text="The call wall is 775.")], "end_turn"))]
        events, sent = self.run_it(rounds)
        types = [e["type"] for e in events]
        self.assertEqual(types, ["thinking", "tool", "delta", "done"])
        self.assertEqual(events[1]["name"], "gex")
        self.assertEqual(events[-1]["engine"], "claude")
        self.assertEqual(events[-1]["model"], "claude-sonnet-5-5")
        self.assertEqual((events[-1]["lookups"], events[-1]["tokens"], events[-1]["empty"]), (1, 14, False))
        first, second = sent
        self.assertEqual(first["output_config"], {"effort": "medium"})
        self.assertNotIn("temperature", first)
        self.assertNotIn("tool_choice", first)
        self.assertEqual({t["name"] for t in first["tools"]}, {"fetch_source", "calc"})
        self.assertIn("input_schema", first["tools"][0])
        # the whole assistant turn (thinking block included) goes back, with the tool result in one user message
        self.assertEqual(second["messages"][-2], {"role": "assistant", "content": [think, use]})
        self.assertEqual(second["messages"][-1]["content"][0]["tool_use_id"], "tu_1")
        self.assertIn("775", second["messages"][-1]["content"][0]["content"])

    def test_missing_key_is_an_error_event(self):
        with mock.patch.dict(os.environ, {"ANTHROPIC_API_KEY": ""}):
            events = list(A.run_claude(_app(), {"turns": []}, "hi"))
        self.assertEqual([e["type"] for e in events], ["error"])
        self.assertIn("No ANTHROPIC_API_KEY", events[0]["message"])

    def test_refusal_is_an_error_event(self):
        events, _ = self.run_it([([], _msg([], "refusal"))])
        self.assertEqual([e["type"] for e in events], ["error"])

    def test_engine_choice(self):
        with mock.patch.dict(os.environ, {"ASSISTANT_ENGINE": ""}):
            self.assertEqual(A.pick_engine(), "local")
            self.assertEqual(A.pick_engine("claude"), "claude")
            self.assertEqual(A.pick_engine("nonsense"), "local")
        with mock.patch.dict(os.environ, {"ASSISTANT_ENGINE": "claude"}):
            self.assertEqual(A.pick_engine(), "claude")
            self.assertEqual(A.pick_engine("local"), "local")
            self.assertEqual(A.pick_engine("nonsense"), "claude")
        with mock.patch.dict(os.environ, {"ASSISTANT_ENGINE": "gpt"}):
            self.assertEqual(A.pick_engine(), "local")
        with mock.patch.object(A, "NINEB_OFF", NS(exists=lambda: True)):   # the 9B switched off: Sonnet whatever was asked
            self.assertEqual(A.pick_engine("local"), "claude")
            self.assertEqual(A.pick_engine(), "claude")

    def test_tools_are_converted_once(self):
        self.assertEqual([t["name"] for t in A.CLAUDE_TOOLS], ["fetch_source", "calc"])
        self.assertEqual(A.CLAUDE_TOOLS[1]["input_schema"]["required"], ["expression"])


if __name__ == "__main__":
    unittest.main()
