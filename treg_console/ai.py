"""AI interaction for treg console.

One OpenAI-compatible chat endpoint (the same local server the old Summarize
tab used) becomes an agent with tools: the treg catalog (every endpoint,
`treg_call` for anything), web search/extract, X, Reddit, News (RSS + treg
search), Hacker News (free official API) and Markets (yfinance). Native
OpenAI tool calling is used when the server supports it; otherwise the agent
falls back to a fenced-JSON tool protocol in plain chat.

All functions here are synchronous and blocking — call them from a worker
thread, never from the GUI thread.

Configuration (environment):
  AI_URL          base URL of the OpenAI-compatible server (default
                  http://localhost:5000/v1)
  AI_API_KEY      bearer token for that server
  TREG_AI_MODEL   model id (default qwen3.8-27B-3.5bpw)
  AI_MAX_TOKENS   completion budget (default 4096)

The old SUMMARY_URL / SUMMARY_API_KEY / TREG_SUMMARY_MODEL names are still
honoured as fallbacks.
"""

from __future__ import annotations

import json
import os
import re
import time
from dataclasses import dataclass
from typing import Any, Callable

import httpx

from .client import TregClient, TregError, poll_async_task
from .hackernews import (comment_tree as hn_comment_tree, feed as hn_feed,
                         item_view as hn_item_view, search as hn_search)
from .markets import (MAJOR_COINS, yf_coins, yf_crypto_quote, yf_fundamentals,
                      yf_history, yf_quote)
from .news import fetch_topic, search_news
from .parsing import (page_markdown, reddit_call, reddit_norm_post,
                      reddit_posts_of, search_results, x_norm_post)

AI_URL = (os.environ.get("AI_URL") or os.environ.get("SUMMARY_URL")
          or "http://localhost:5000/v1")
AI_API_KEY = (os.environ.get("AI_API_KEY")
              or os.environ.get("SUMMARY_API_KEY")
              or "7dce0de9cc98b5af9b0f59e7a22d4ab8")
AI_MODEL = (os.environ.get("TREG_AI_MODEL")
            or os.environ.get("TREG_SUMMARY_MODEL")
            or "qwen3.8-27B-3.5bpw")
AI_MAX_TOKENS = int(os.environ.get("AI_MAX_TOKENS", "4096"))
AI_TIMEOUT = float(os.environ.get("AI_TIMEOUT", "300"))
AI_TOOL_RESULT_LIMIT = int(os.environ.get("AI_TOOL_RESULT_LIMIT", "24000"))

MAX_STEPS = 8


# ---------------------------------------------------------------------------
# tools
# ---------------------------------------------------------------------------

@dataclass
class Tool:
    name: str
    description: str
    parameters: dict
    fn: Callable[[dict], Any]

    def spec(self) -> dict:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.parameters,
            },
        }


class ToolRegistry:
    """A small named set of tools with JSON schemas and safe execution."""

    def __init__(self):
        self._tools: dict[str, Tool] = {}

    def add(self, name: str, description: str, properties: dict,
            required: list[str] | tuple[str, ...], fn: Callable[[dict], Any]):
        self._tools[name] = Tool(
            name=name,
            description=description,
            parameters={"type": "object", "properties": properties,
                        "required": list(required)},
            fn=fn,
        )

    def __contains__(self, name: object) -> bool:
        return name in self._tools

    def __len__(self) -> int:
        return len(self._tools)

    def names(self) -> list[str]:
        return sorted(self._tools)

    def specs(self) -> list[dict]:
        return [t.spec() for t in self._tools.values()]

    def call(self, name: str, arguments: dict | None = None) -> str:
        """Run one tool and return a JSON string (errors included, so the
        model can recover instead of the turn failing)."""
        tool = self._tools.get(name)
        if tool is None:
            return _dump({"error": f"unknown tool {name!r}",
                          "available": self.names()})
        try:
            out = tool.fn(arguments or {})
        except TregError as e:
            return _dump({"error": str(e)})
        except Exception as e:  # a tool bug must not kill the conversation
            return _dump({"error": f"{type(e).__name__}: {e}"})
        return _dump(out)

    def prompt(self) -> str:
        """A plain-text tool list for servers without native tool calling."""
        lines = ["You can call tools. Reply with ONE fenced JSON block and "
                 "nothing else:", '```json',
                 '{"tool": "tool_name", "arguments": {"arg": "value"}}',
                 "```", "After the tool runs you will get a TOOL RESULT "
                 "message; then either call another tool or write the final "
                 "answer in Markdown.", "", "Tools:"]
        for t in self._tools.values():
            req = t.parameters.get("required") or []
            args = ", ".join(f"{k}={v.get('type', 'any')}" for k, v in
                             t.parameters.get("properties", {}).items())
            lines.append(f"- {t.name}({args}) — {t.description}"
                         + (f" Required: {', '.join(req)}." if req else ""))
        return "\n".join(lines)


def _dump(value: Any) -> str:
    """Serialize a tool result, truncated so one huge page cannot eat the
    whole context window."""
    try:
        text = json.dumps(value, ensure_ascii=False, default=str)
    except (TypeError, ValueError):
        text = str(value)
    if len(text) > AI_TOOL_RESULT_LIMIT:
        text = text[:AI_TOOL_RESULT_LIMIT] + "… (truncated)"
    return text


_STR = {"type": "string"}
_INT = {"type": "integer"}
_OBJ = {"type": "object"}


def _need(client: TregClient | None) -> TregClient:
    if client is None:
        raise TregError("this tool needs a treg token — run `treg login` "
                        "or set TREG_TOKEN")
    return client


def _response_payload(r: httpx.Response) -> object:
    try:
        return r.json()
    except ValueError:
        return r.text


def _call_body(client: TregClient, target: str, method: str = "GET",
               params: dict | None = None, body: object = None,
               on_event=None) -> dict:
    """One treg /call, with async task polling, as a JSON-able result."""
    plist = [(str(k), str(v)) for k, v in (params or {}).items()
             if v is not None]
    raw_body = body
    if isinstance(body, (dict, list)):
        raw_body = json.dumps(body)
    elif body is not None and not isinstance(body, str):
        raw_body = str(body)
    r = client.call(target, method or "GET", plist or None,
                    raw_body or None)
    out: dict = {"status": r.status_code,
                 "data": _response_payload(r)}
    for header, key in (("X-Treg-Cost-Micro", "cost_micro"),
                        ("X-Treg-Call-Id", "call_id"),
                        ("X-Treg-Served-By", "served_by")):
        if r.headers.get(header) is not None:
            out[key] = r.headers[header]
    descriptor = r.headers.get("X-Treg-Async")
    if descriptor:
        try:
            desc = json.loads(descriptor)
        except ValueError:
            desc = None
        if desc:
            if on_event:
                on_event("status", "polling the async treg task…")
            polled = poll_async_task(
                client, desc, r,
                on_progress=lambda m: on_event("status", m) if on_event else None,
                timeout=300.0)
            out["async"] = {k: v for k, v in polled.items() if k != "response"}
            if polled.get("response") is not None:
                out["data"] = _response_payload(polled["response"])
    return out


def _x_posts(body: object) -> list[dict]:
    if not isinstance(body, dict):
        return []
    out = body.get("output")
    if not isinstance(out, dict):
        out = body.get("data") if isinstance(body.get("data"), dict) else {}
    raw = out.get("posts") if isinstance(out.get("posts"), list) else []
    return [x_norm_post(p) for p in raw if isinstance(p, dict)]


def _x_trends(body: object) -> list[dict]:
    if not isinstance(body, dict):
        return []
    data = body.get("data") if isinstance(body.get("data"), dict) else {}
    return [t for t in (data.get("trends") or [])
            if isinstance(t, dict) and t.get("name")]


def _reddit_rows(r: httpx.Response, kind: str = "posts") -> list[dict]:
    try:
        body = r.json()
    except ValueError:
        return []
    if kind == "posts":
        return [reddit_norm_post(p) for p in reddit_posts_of(body)]
    return []


def _slim_post(p: dict) -> dict:
    return {k: p.get(k) for k in
            ("id", "title", "text", "body", "author", "author_handle",
             "subreddit", "created", "score", "comments", "likes", "retweets",
             "replies", "views", "url", "flair") if p.get(k) not in (None, "")}


def build_registry(client: TregClient | None,
                   on_event: Callable[[str, Any], None] | None = None) -> ToolRegistry:
    """Every data source in the app as a tool, for the AI agent."""
    reg = ToolRegistry()

    # -- treg: the catalog and every endpoint ------------------------------

    reg.add(
        "treg_catalog_search",
        "Search treg's catalog of thousands of API endpoints by job or "
        "description (e.g. 'backlinks for a domain', 'stock news'). Returns "
        "endpoint ids, providers, cost and a summary. Start here when you "
        "need a capability the other tools do not cover.",
        {"query": _STR, "limit": _INT, "engine": _STR},
        ["query"],
        lambda a: _need(client).search(
            str(a["query"]), int(a.get("limit") or 20),
            engine=str(a.get("engine") or "agent")),
    )
    reg.add(
        "treg_endpoint_detail",
        "Full details for one treg catalog endpoint: parameters, price, "
        "output fields, async behavior and sibling providers.",
        {"endpoint_id": _STR}, ["endpoint_id"],
        lambda a: _need(client).endpoint(str(a["endpoint_id"])),
    )
    reg.add(
        "treg_platforms",
        "List treg's platform catalog (platforms, categories, endpoint "
        "counts and price-from).",
        {}, [],
        lambda a: _need(client).platforms(),
    )
    reg.add(
        "treg_platform",
        "One treg platform: its capabilities and every endpoint under them.",
        {"platform_slug": _STR}, ["platform_slug"],
        lambda a: _need(client).platform(str(a["platform_slug"])),
    )
    reg.add(
        "treg_call",
        "Call ANY treg endpoint (catalog id like 'treg.web.search' or a team "
        "tool URL). Paid — each call is charged to the team; prefer free "
        "news/markets tools when they suffice. `params` is a flat object for "
        "query parameters, `body` a JSON object for POST. Async generation "
        "tasks are polled to completion.",
        {"target": _STR, "method": {"type": "string", "enum": ["GET", "POST"]},
         "params": _OBJ, "body": _OBJ}, ["target"],
        lambda a: _call_body(client if client else _need(client),
                             str(a["target"]), str(a.get("method") or "GET"),
                             a.get("params") or {},
                             a.get("body"), on_event),
    )
    reg.add(
        "treg_history",
        "Recent calls from the team's audit log (endpoint, status, cost, "
        "call id, whether it was cached or async).",
        {"limit": _INT}, [],
        lambda a: _need(client).calls(int(a.get("limit") or 25)),
    )
    reg.add(
        "treg_call_result",
        "The stored result of an earlier treg call, by call id.",
        {"call_id": _STR}, ["call_id"],
        lambda a: _need(client).call_result(str(a["call_id"])),
    )
    reg.add(
        "treg_balance",
        "The team's prepaid balance and recent ledger.",
        {"limit": _INT}, [],
        lambda a: _balance(client, int(a.get("limit") or 10)),
    )

    # -- web ---------------------------------------------------------------

    def web_search(a: dict) -> object:
        c = _need(client)
        r = c.call("treg.web.search", "POST", None, json.dumps(
            {"q": str(a["query"]), "limit": int(a.get("limit") or 8)}))
        if r.status_code >= 400:
            raise TregError(f"web search failed: HTTP {r.status_code}")
        try:
            return search_results(r.json())
        except ValueError:
            raise TregError("web search returned non-JSON")

    def read_page(a: dict) -> object:
        c = _need(client)
        r = c.call("treg.web.extract", "POST", None,
                   json.dumps({"url": str(a["url"])}))
        if r.status_code >= 400:
            raise TregError(f"could not read the page: HTTP {r.status_code}")
        try:
            md = page_markdown(r.json())
        except ValueError:
            raise TregError("the page response was not JSON")
        return {"url": str(a["url"]), "markdown": md}

    reg.add(
        "web_search",
        "Search the web through treg (paid, fractions of a cent). Returns "
        "title/url/snippet rows; use read_page for a full article.",
        {"query": _STR, "limit": _INT}, ["query"], web_search)
    reg.add(
        "read_page",
        "Fetch a web page as clean Markdown (treg web extract; paid). Use it "
        "to read the sources returned by web_search or news.",
        {"url": _STR}, ["url"], read_page)

    # -- X -----------------------------------------------------------------

    def x_search(a: dict) -> object:
        r = _need(client).call("treg.x.search.posts", "POST", None,
                               json.dumps({"q": str(a["query"])}))
        return [_slim_post(p) for p in _x_posts(_response_payload(r))]

    def x_user_posts(a: dict) -> object:
        handle = str(a["username"]).lstrip("@")
        r = _need(client).call("treg.x.user.posts", "POST", None,
                               json.dumps({"username": handle}))
        return [_slim_post(p) for p in _x_posts(_response_payload(r))]

    def x_trends(a: dict) -> object:
        country = str(a.get("country") or "US")
        r = _need(client).call("tikhub.x.twitter-web-fetch-trending", "GET",
                               [("country", country)], None)
        return _x_trends(_response_payload(r))

    def x_replies(a: dict) -> object:
        r = _need(client).call("treg.x.post.comments", "POST", None,
                               json.dumps({"tweet_id": str(a["tweet_id"])}))
        body = _response_payload(r)
        out = body.get("output") if isinstance(body, dict) else None
        rows = (out or {}).get("comments") if isinstance(out, dict) else []
        return [_slim_post(x_norm_post(p)) for p in (rows or [])
                if isinstance(p, dict)]

    def x_profile(a: dict) -> object:
        handle = str(a["username"]).lstrip("@")
        r = _need(client).call("treg.x.user.profile", "POST", None,
                               json.dumps({"username": handle}))
        body = _response_payload(r)
        return (body.get("output") if isinstance(body, dict) else body) or {}

    reg.add("x_search_posts", "Search X/Twitter posts by keyword, #hashtag or "
            "from:user (paid).", {"query": _STR}, ["query"], x_search)
    reg.add("x_user_posts", "An X account's recent posts by handle (paid).",
            {"username": _STR}, ["username"], x_user_posts)
    reg.add("x_trends", "Trending topics on X by country code (paid).",
            {"country": _STR}, [], x_trends)
    reg.add("x_post_replies", "Replies to one X post by tweet id (paid).",
            {"tweet_id": _STR}, ["tweet_id"], x_replies)
    reg.add("x_user_profile", "An X user's profile by handle (paid).",
            {"username": _STR}, ["username"], x_profile)

    # -- Reddit ------------------------------------------------------------

    def reddit_posts(a: dict, target: str, method: str = "GET",
                     params: list[tuple[str, str]] | None = None,
                     body: str | None = None) -> object:
        r = reddit_call(_need(client), target, method, params, body)
        return [_slim_post(p) for p in _reddit_rows(r)]

    def reddit_search(a: dict) -> object:
        return reddit_posts(a, "scrapecreators.reddit.search.posts", "GET",
                            [("query", str(a["query"]))])

    def reddit_subreddit(a: dict) -> object:
        name = str(a["subreddit"])
        name = name[2:] if name.startswith("r/") else name
        return reddit_posts(a, "scrapecreators.reddit.subreddit.posts", "GET",
                            [("subreddit", name)])

    def reddit_user(a: dict) -> object:
        handle = str(a["username"])
        handle = handle[2:] if handle.startswith("u/") else handle
        return reddit_posts(a, "tikhub.x.reddit-app-fetch-user-posts", "GET",
                            [("username", handle)])

    def reddit_detail(a: dict) -> object:
        r = reddit_call(_need(client), "treg.reddit.post.detail", "POST", None,
                        json.dumps({"post_id": str(a["post_id"])}))
        body = _response_payload(r)
        out = body.get("output") if isinstance(body, dict) else None
        raw = out.get("post") if isinstance(out, dict) else None
        return _slim_post(reddit_norm_post(raw)) if isinstance(raw, dict) else {}

    def reddit_comments(a: dict) -> object:
        r = reddit_call(_need(client),
                        "tikhub.x.reddit-app-fetch-post-comments", "GET",
                        [("post_id", str(a["post_id"]))], None)
        body = _response_payload(r)
        data = body.get("data") if isinstance(body, dict) else None
        forest = ((data or {}).get("postInfoById") or {}).get("commentForest") or {}
        out = []
        for t in forest.get("trees") or []:
            node = (t or {}).get("node") or {}
            if not isinstance(node, dict):
                continue
            out.append({
                "depth": int(t.get("depth") or 0),
                "author": ((node.get("authorInfo") or {}).get("name")) or "",
                "score": node.get("score") or 0,
                "body": ((node.get("content") or {}).get("markdown")) or "",
            })
        return out

    def reddit_profile(a: dict) -> object:
        handle = str(a["username"])
        handle = handle[2:] if handle.startswith("u/") else handle
        r = reddit_call(_need(client),
                        "tikhub.x.reddit-app-fetch-user-profile", "GET",
                        [("username", handle)], None)
        body = _response_payload(r)
        data = body.get("data") if isinstance(body, dict) else None
        return ((data or {}).get("redditorInfoByName") or {}) if isinstance(data, dict) else {}

    reg.add("reddit_search_posts", "Search Reddit posts by keyword (paid).",
            {"query": _STR}, ["query"], reddit_search)
    reg.add("reddit_subreddit_posts", "A subreddit's posts (paid).",
            {"subreddit": _STR}, ["subreddit"], reddit_subreddit)
    reg.add("reddit_user_posts", "A Reddit user's posts (paid).",
            {"username": _STR}, ["username"], reddit_user)
    reg.add("reddit_post_detail", "One Reddit post in full by fullname/id "
            "(e.g. t3_abc) (paid).", {"post_id": _STR}, ["post_id"], reddit_detail)
    reg.add("reddit_post_comments", "The comment tree of a Reddit post (paid).",
            {"post_id": _STR}, ["post_id"], reddit_comments)
    reg.add("reddit_user_profile", "A Reddit user's profile (paid).",
            {"username": _STR}, ["username"], reddit_profile)

    # -- News (free RSS + treg search) -------------------------------------

    def news_topic(a: dict) -> object:
        return fetch_topic(str(a.get("topic") or "top"),
                           int(a.get("limit") or 20))

    def news_search(a: dict) -> object:
        return search_news(_need(client), str(a["query"]),
                           int(a.get("limit") or 20))

    reg.add(
        "news_top",
        "The current news front page: free RSS from major outlets, deduped "
        "and ranked. No treg charge. Great for 'what's happening today'.",
        {"limit": _INT}, [], lambda a: news_topic({"topic": "top", **a}))
    reg.add(
        "news_tech", "Current technology news, deduped and ranked (free RSS, "
        "no treg charge).", {"limit": _INT}, [],
        lambda a: news_topic({"topic": "tech", **a}))
    reg.add(
        "news_business", "Current business/markets news, deduped and ranked "
        "(free RSS, no treg charge).", {"limit": _INT}, [],
        lambda a: news_topic({"topic": "business", **a}))
    reg.add(
        "news_search",
        "Search recent news for an arbitrary query through treg (paid, "
        "fractions of a cent).",
        {"query": _STR, "limit": _INT}, ["query"], news_search)

    # -- Hacker News (free, official API) ----------------------------------

    def hn_stories(a: dict) -> object:
        return hn_feed(str(a.get("kind") or "top"), int(a.get("limit") or 20))

    def hn_search_stories(a: dict) -> object:
        return hn_search(str(a["query"]), int(a.get("limit") or 20))

    def hn_item(a: dict) -> object:
        return hn_item_view(int(a["item_id"]))

    def hn_comments(a: dict) -> object:
        return hn_comment_tree(int(a["item_id"]), int(a.get("limit") or 40))

    reg.add(
        "hn_stories",
        "The Hacker News front page from the free official API: kind is one "
        "of top, new, best, ask, show, jobs. Rows carry title, url, site, "
        "points, author and comment count. No treg charge.",
        {"kind": {"type": "string",
                  "enum": ["top", "new", "best", "ask", "show", "jobs"]},
         "limit": _INT}, [], hn_stories)
    reg.add(
        "hn_search",
        "Search Hacker News stories by keyword through the free Algolia API. "
        "No treg charge.",
        {"query": _STR, "limit": _INT}, ["query"], hn_search_stories)
    reg.add(
        "hn_item",
        "One Hacker News item (story, Ask/Show post or comment) in full by id, "
        "with its plain-text body. Free.",
        {"item_id": _INT}, ["item_id"], hn_item)
    reg.add(
        "hn_comments",
        "The comment tree of a Hacker News story by id, flattened with a depth "
        "per comment (indented replies). Free.",
        {"item_id": _INT, "limit": _INT}, ["item_id"], hn_comments)

    # -- Markets (yfinance, free) ------------------------------------------

    def market_quote(a: dict) -> object:
        q = yf_quote(str(a["symbol"]).upper())
        if q is None:
            raise TregError(f"no quote for {a['symbol']} — check the ticker")
        return q

    def market_history(a: dict) -> object:
        bars = yf_history(str(a["symbol"]).upper(), int(a.get("days") or 30))
        if not bars:
            raise TregError(f"no daily bars for {a['symbol']} — check the ticker")
        return bars

    def market_fundamentals(a: dict) -> object:
        met = yf_fundamentals(str(a["symbol"]).upper())
        if met is None:
            raise TregError(f"no fundamentals for {a['symbol']} — check the ticker")
        return met

    def crypto_list(a: dict) -> object:
        rows = yf_coins()
        if not rows:
            raise TregError("no coin data — Yahoo returned nothing")
        return rows

    def crypto_quote(a: dict) -> object:
        sym = str(a["symbol"]).upper().strip()
        if sym.endswith("-USD"):
            sym = sym[:-4]
        q = yf_crypto_quote(sym)
        if q is None:
            for s, name in MAJOR_COINS:
                if sym.lower() in (s.lower(), name.lower()):
                    q = yf_crypto_quote(s)
                    sym = s
                    break
        if q is None:
            raise TregError(f"no data for coin {a['symbol']} — try BTC or ETH")
        return q

    reg.add("market_quote", "Live stock/ETF/index quote: price, change, day "
            "range, volume, market cap, 52-week range and averages (free).",
            {"symbol": _STR}, ["symbol"], market_quote)
    reg.add("market_history", "Daily OHLCV bars for a ticker (free).",
            {"symbol": _STR, "days": _INT}, ["symbol"], market_history)
    reg.add("market_fundamentals", "Company profile and metrics: sector, P/E, "
            "EPS, revenue, margins, ROE, debt, dividend, beta (free).",
            {"symbol": _STR}, ["symbol"], market_fundamentals)
    reg.add("crypto_list", "Latest prices, 24h/7d change and volume for the "
            "major coins (free, one batched call).", {}, [], crypto_list)
    reg.add("crypto_quote", "One coin's live quote by ticker or name (free).",
            {"symbol": _STR}, ["symbol"], crypto_quote)

    return reg


def _balance(client: TregClient | None, limit: int) -> object:
    c = _need(client)
    orgs = c.orgs()
    if not orgs:
        raise TregError("this token has no org")
    return c.balance(int(orgs[0].get("id") or 0), limit=limit)


# ---------------------------------------------------------------------------
# the agent loop
# ---------------------------------------------------------------------------

class ToolsUnsupported(TregError):
    """The server rejected the native `tools` payload."""


def system_prompt() -> str:
    today = time.strftime("%A, %d %B %Y")
    return (
        "You are the AI inside treg console, a native Linux desktop app. "
        f"Today is {today}.\n\n"
        "You have tools for the treg catalog (thousands of API endpoints), "
        "web search and page reading, X/Twitter, Reddit, news (free RSS and "
        "treg news search), Hacker News (free) and markets (stocks, "
        "fundamentals, crypto). Use them instead of guessing whenever the "
        "question involves current facts, prices, posts or news. Chain tools "
        "when needed: e.g. search the catalog, inspect an endpoint, then call "
        "it.\n\n"
        "Costs: treg_*, web_*, x_* and reddit_* calls are paid (usually "
        "fractions of a cent) and charged to the team; news_top/news_tech/"
        "news_business, the hn_* tools and market_*/crypto_* are free. Prefer "
        "free tools when they answer the question, and do not repeat an "
        "expensive call.\n\n"
        "Style: answer in Markdown, concisely. Give numbers with units and "
        "cite the URLs you relied on. If a tool fails, say what failed and "
        "try a reasonable alternative; never invent data. If the question is "
        "unclear, ask one short clarifying question."
    )


def _clean_tool_calls(raw: object) -> list[dict]:
    """OpenAI tool_calls → clean {id,type,function:{name,arguments}} dicts."""
    out: list[dict] = []
    if not isinstance(raw, list):
        return out
    for i, call in enumerate(raw):
        if not isinstance(call, dict):
            continue
        fn = call.get("function") or {}
        name = str(fn.get("name") or "").strip()
        if not name:
            continue
        args = fn.get("arguments")
        if not isinstance(args, str):
            args = json.dumps(args or {})
        out.append({
            "id": str(call.get("id") or f"call_{i}_{int(time.time() * 1000)}"),
            "type": "function",
            "function": {"name": name, "arguments": args},
        })
    return out


_ACTION_RE = re.compile(r"```(?:json)?\s*(\{.*?\})\s*```", re.S)


def parse_action(text: str) -> tuple[str, dict] | None:
    """Find a fallback tool action in plain model output.

    Accepts a fenced JSON object (or a bare one) shaped like
    {"tool": "name", "arguments": {...}} or {"name": ..., "arguments": ...}.
    Returns (name, arguments) or None.
    """
    candidates = [m.group(1) for m in _ACTION_RE.finditer(text or "")]
    stripped = (text or "").strip()
    if stripped.startswith("{"):
        candidates.append(stripped)
    for cand in candidates:
        try:
            doc = json.loads(cand)
        except ValueError:
            continue
        if not isinstance(doc, dict):
            continue
        name = doc.get("tool") or doc.get("name")
        if not isinstance(name, str) or not name.strip():
            continue
        args = doc.get("arguments") if "arguments" in doc else doc.get("args")
        if isinstance(args, str):
            try:
                args = json.loads(args)
            except ValueError:
                args = {}
        if not isinstance(args, dict):
            args = {}
        return name.strip(), args
    return None


def _chat(messages: list[dict], tools: list[dict] | None = None,
          model: str = AI_MODEL) -> dict:
    """One blocking /chat/completions call; returns the assistant message."""
    payload: dict = {
        "model": model,
        "messages": messages,
        "temperature": 0.3,
        "max_tokens": AI_MAX_TOKENS,
    }
    if tools:
        payload["tools"] = tools
        payload["tool_choice"] = "auto"
    try:
        r = httpx.post(f"{AI_URL}/chat/completions",
                       headers={"Authorization": f"Bearer {AI_API_KEY}"},
                       json=payload, timeout=AI_TIMEOUT)
    except httpx.ConnectError as e:
        raise TregError(f"cannot reach the AI endpoint at {AI_URL}") from e
    except httpx.TimeoutException as e:
        raise TregError(f"the AI endpoint at {AI_URL} timed out") from e
    if r.status_code >= 400:
        if tools and r.status_code in (400, 404, 405, 415, 422):
            raise ToolsUnsupported(
                f"the AI endpoint does not accept tools ({r.status_code})")
        raise TregError(
            f"the AI endpoint returned HTTP {r.status_code}: {r.text[:300]}",
            status=r.status_code)
    try:
        data = r.json()
    except ValueError as e:
        raise TregError("the AI endpoint returned non-JSON") from e
    choices = data.get("choices") or []
    if not choices or not isinstance(choices[0], dict):
        raise TregError("the AI endpoint returned no choices")
    msg = choices[0].get("message")
    return msg if isinstance(msg, dict) else {"role": "assistant", "content": ""}


def run_agent(client: TregClient | None, history: list[dict],
              on_event: Callable[[str, Any], None] | None = None,
              model: str = AI_MODEL, max_steps: int = MAX_STEPS,
              chat_fn: Callable[..., dict] | None = None) -> dict:
    """Run one turn: let the model call tools until it answers.

    `history` is the conversation so far, ending with the user's message.
    Returns {"text": final answer, "messages": history + the answer}; the
    transient tool exchanges are not kept, so the next turn sends a clean
    transcript. `on_event(kind, data)` reports progress from this (worker)
    thread; kinds are "status", "tool" and "tool_result".
    """
    chat = chat_fn or _chat
    registry = build_registry(client, on_event)
    emit = on_event or (lambda *_: None)
    messages: list[dict] = [{"role": "system", "content": system_prompt()}]
    messages += [dict(m) for m in history]
    native = True
    answer = ""

    for step in range(max_steps):
        emit("status", f"thinking (step {step + 1}/{max_steps})…")
        if native:
            try:
                msg = chat(messages, registry.specs(), model=model)
            except ToolsUnsupported:
                native = False
                # Keep a single system message at the start: some servers
                # (and chat templates) reject a system message anywhere else.
                messages[0]["content"] = (str(messages[0].get("content") or "")
                                          + "\n\n" + registry.prompt())
                emit("status", "server has no native tool calling — using the "
                               "JSON tool protocol")
                msg = chat(messages, None, model=model)
        else:
            msg = chat(messages, None, model=model)

        content = str(msg.get("content") or "").strip()
        calls = _clean_tool_calls(msg.get("tool_calls"))
        if not calls:
            action = parse_action(content)
            if action and action[0] in registry:
                calls = [{
                    "id": f"call_{step}_{int(time.time() * 1000)}",
                    "type": "function",
                    "function": {"name": action[0],
                                 "arguments": json.dumps(action[1])},
                }]

        if not calls:
            answer = content
            if not answer:
                reasoning = str(msg.get("reasoning_content") or "").strip()
                answer = ("(the model stopped while reasoning; try a shorter "
                          "question or raise AI_MAX_TOKENS)")
                if reasoning:
                    answer += "\n\nReasoning so far:\n\n" + reasoning[-1200:]
            break

        assistant_msg: dict = {"role": "assistant", "content": content or ""}
        if native:
            assistant_msg["tool_calls"] = calls
        messages.append(assistant_msg)
        for call in calls:
            name = call["function"]["name"]
            try:
                args = json.loads(call["function"].get("arguments") or "{}")
            except ValueError:
                args = {}
            if not isinstance(args, dict):
                args = {}
            emit("tool", {"name": name, "arguments": args})
            result = registry.call(name, args)
            emit("tool_result", {"name": name, "result": result[:400]})
            if native:
                messages.append({"role": "tool", "tool_call_id": call["id"],
                                 "name": name, "content": result})
            else:
                messages.append({"role": "user",
                                 "content": f"TOOL RESULT ({name}):\n{result}"})
    else:
        answer = (f"(stopped after {max_steps} tool steps — the task may need "
                  "a narrower question)")

    out = [dict(m) for m in history]
    out.append({"role": "assistant", "content": answer})
    return {"text": answer, "messages": out}


# ---------------------------------------------------------------------------
# simple one-shot helper (no tools), kept for the older call sites
# ---------------------------------------------------------------------------

def complete(prompt: str, model: str = AI_MODEL) -> str:
    """One blocking chat completion without tools."""
    msg = _chat([{"role": "user", "content": prompt}], None, model=model)
    text = str(msg.get("content") or "").strip()
    if not text and msg.get("reasoning_content"):
        return "(the model stopped while reasoning; raise AI_MAX_TOKENS)"
    return text


def tool_summary() -> str:
    """Markdown list of the AI's tools (for the tab's hint text)."""
    reg = build_registry(None)
    groups = {
        "treg": "treg catalog + any endpoint",
        "web": "web search / page reader",
        "x": "X (Twitter)",
        "reddit": "Reddit",
        "news": "News (free RSS + search)",
        "hn": "Hacker News (free)",
        "market": "Markets (stocks)",
        "crypto": "Crypto",
    }
    bits = []
    for prefix, label in groups.items():
        n = sum(1 for name in reg.names() if name.startswith(prefix))
        if n:
            bits.append(f"**{label}** ({n})")
    return " · ".join(bits)
