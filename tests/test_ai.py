"""Unit tests for the AI agent: tool registry, fallback action parsing and
the tool-calling loop.

Run with:  python3 -m unittest discover -s tests -v
"""

from __future__ import annotations

import json
import unittest

from treg_console import ai


class RegistryTest(unittest.TestCase):
    def test_unknown_tool_is_an_error_not_an_exception(self):
        reg = ai.ToolRegistry()
        out = json.loads(reg.call("nope", {}))
        self.assertIn("error", out)
        self.assertIn("available", out)

    def test_tool_error_is_reported_to_the_model(self):
        reg = ai.ToolRegistry()
        reg.add("boom", "always fails", {}, [], lambda a: 1 / 0)
        out = json.loads(reg.call("boom", {}))
        self.assertIn("error", out)
        self.assertIn("ZeroDivisionError", out["error"])

    def test_spec_is_openai_shaped(self):
        reg = ai.ToolRegistry()
        reg.add("echo", "echo", {"x": {"type": "string"}}, ["x"],
                lambda a: {"x": a.get("x")})
        spec = reg.specs()[0]
        self.assertEqual(spec["type"], "function")
        self.assertEqual(spec["function"]["name"], "echo")
        self.assertEqual(spec["function"]["parameters"]["required"], ["x"])

    def test_built_registry_covers_news_and_markets(self):
        reg = ai.build_registry(None)
        names = set(reg.names())
        for expected in ("news_top", "news_tech", "news_business", "news_search",
                         "market_quote", "market_history", "market_fundamentals",
                         "crypto_list", "crypto_quote", "web_search", "read_page",
                         "treg_call", "x_search_posts", "reddit_search_posts"):
            self.assertIn(expected, names)

    def test_built_registry_covers_hacker_news(self):
        reg = ai.build_registry(None)
        names = set(reg.names())
        for expected in ("hn_stories", "hn_search", "hn_item", "hn_comments"):
            self.assertIn(expected, names)

    def test_built_registry_covers_github(self):
        reg = ai.build_registry(None)
        names = set(reg.names())
        for expected in ("github_search_repos", "github_trending",
                         "github_repo", "github_issues", "github_releases"):
            self.assertIn(expected, names)

    def test_tool_summary_lists_hacker_news_and_github(self):
        summary = ai.tool_summary()
        self.assertIn("Hacker News", summary)
        self.assertIn("GitHub", summary)

    def test_built_registry_covers_olx(self):
        reg = ai.build_registry(None)
        names = set(reg.names())
        for expected in ("olx_search", "olx_listing", "olx_categories"):
            self.assertIn(expected, names)

    def test_tool_summary_lists_olx(self):
        self.assertIn("OLX.pt", ai.tool_summary())


class ParseActionTest(unittest.TestCase):
    def test_fenced_json_action(self):
        text = 'Sure!\n```json\n{"tool": "news_top", "arguments": {"limit": 5}}\n```'
        self.assertEqual(ai.parse_action(text), ("news_top", {"limit": 5}))

    def test_bare_json_action(self):
        self.assertEqual(ai.parse_action('{"name": "crypto_list", "args": {}}'),
                         ("crypto_list", {}))

    def test_plain_prose_is_not_an_action(self):
        self.assertIsNone(ai.parse_action("The market is up today."))


class AgentLoopTest(unittest.TestCase):
    def setUp(self):
        self._orig = ai.build_registry

        def fake_registry(client, on_event=None):
            reg = ai.ToolRegistry()
            reg.add("peek", "peek at something",
                    {"what": {"type": "string"}}, ["what"],
                    lambda a: {"what": a.get("what"), "value": 42})
            return reg

        ai.build_registry = fake_registry

    def tearDown(self):
        ai.build_registry = self._orig

    def test_native_tool_call_then_answer(self):
        calls = []

        def chat(messages, tools=None, model=ai.AI_MODEL):
            calls.append({"tools": bool(tools), "messages": len(messages)})
            if len(calls) == 1:
                return {"role": "assistant", "content": "",
                        "tool_calls": [{"id": "c1", "type": "function",
                                        "function": {"name": "peek",
                                                     "arguments": '{"what":"x"}'}}]}
            return {"role": "assistant", "content": "The value is 42."}

        events = []
        out = ai.run_agent(None, [{"role": "user", "content": "peek x"}],
                           on_event=lambda k, d: events.append((k, d)),
                           chat_fn=chat)
        self.assertEqual(out["text"], "The value is 42.")
        self.assertEqual(out["messages"][-1]["role"], "assistant")
        self.assertTrue(calls[0]["tools"])
        kinds = [k for k, _ in events]
        self.assertIn("tool", kinds)
        self.assertIn("tool_result", kinds)
        tool_events = [d for k, d in events if k == "tool"]
        self.assertEqual(tool_events[0]["name"], "peek")

    def test_fallback_json_protocol_when_tools_unsupported(self):
        state = {"round": 0}

        def chat(messages, tools=None, model=ai.AI_MODEL):
            if tools:
                raise ai.ToolsUnsupported("no tools here")
            state["round"] += 1
            if state["round"] == 1:
                return {"role": "assistant",
                        "content": '```json\n{"tool": "peek", '
                                   '"arguments": {"what": "y"}}\n```'}
            return {"role": "assistant", "content": "Peeked y = 42."}

        out = ai.run_agent(None, [{"role": "user", "content": "peek y"}],
                           chat_fn=chat)
        self.assertEqual(out["text"], "Peeked y = 42.")
        self.assertEqual(state["round"], 2)

    def test_step_limit_is_reported(self):
        def chat(messages, tools=None, model=ai.AI_MODEL):
            return {"role": "assistant", "content": "",
                    "tool_calls": [{"id": "c", "type": "function",
                                    "function": {"name": "peek",
                                                 "arguments": '{"what":"z"}'}}]}

        out = ai.run_agent(None, [{"role": "user", "content": "loop"}],
                           max_steps=3, chat_fn=chat)
        self.assertIn("stopped after 3 tool steps", out["text"])


if __name__ == "__main__":
    unittest.main()
