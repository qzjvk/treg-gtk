"""treg console — a native Linux desktop app (GTK3) that uses treg (https://treg.to) as its backend.

Native look: no custom CSS. The app is a plain GTK3 application, so it
follows the desktop's GTK theme, system font and icon theme, and its
GApplication id ("treg.console") associates the window with the .desktop
entry so the desktop environment treats it like any other installed
application.

Tabs:
  Browse   — search the web, click a result, read the page (the simple path)
  Summarize — the current search results, summarized via an OpenAI-compatible
             /v1/completions endpoint (localhost:5000 by default), with an
             'Ask further' box for follow-up questions about the summary
  Catalog  — search the 4,000+ endpoint catalog, inspect params/price/reliability
  Providers — browse the platform catalog by category, drill into a
             platform's capabilities and endpoints
  Call     — build and fire a call (query params or JSON body), see cost + result
  History  — the team's call audit log, with stored results
  X        — an X (Twitter) frontend: search posts, browse an account's
             timeline, read a post with its media and replies, profiles,
             and trending topics

Credentials come from the treg CLI config (~/.treg/config.json) or TREG_TOKEN.
"""

from __future__ import annotations

import json
import os
import re
import sys
import threading
import time
from pathlib import Path
from urllib.parse import urljoin

import gi

gi.require_version("Gtk", "3.0")
gi.require_version("Gdk", "3.0")
gi.require_version("GdkPixbuf", "2.0")
from gi.repository import Gio, GLib, Gdk, GdkPixbuf, Gtk, Pango

import httpx

from .client import TregClient, TregError, load_credentials
from .desktop import install_desktop_entry, remove_desktop_entry

MICRO = 1_000_000

# Summarizer for the Browse tab: an OpenAI-compatible /v1/completions
# endpoint. Override with SUMMARY_URL / SUMMARY_API_KEY / TREG_SUMMARY_MODEL
# if you run it elsewhere.
SUMMARY_URL = os.environ.get("SUMMARY_URL", "http://localhost:5000/v1")
SUMMARY_API_KEY = os.environ.get("SUMMARY_API_KEY", "7dce0de9cc98b5af9b0f59e7a22d4ab8")
SUMMARY_MODEL = os.environ.get("TREG_SUMMARY_MODEL", "qwen3.8-27B-3.5bpw")


def usd(micro: int | float | None) -> str:
    if micro is None:
        return "—"
    v = micro / MICRO
    return f"${v:,.4f}".rstrip("0").rstrip(".") if v else "$0"


def usd_amount(value: int | float | None) -> str:
    """Format a plain USD amount (the catalog's cost.usd / price_from fields).
    Six decimals so per-row prices like $0.000036 survive."""
    if value is None:
        return "—"
    return f"${value:,.6f}".rstrip("0").rstrip(".") if value else "$0"


def _x_norm_post(p: dict) -> dict:
    """Normalize one provider-native X post row to the tab's fixed shape.
    Providers use different key styles (likes vs likeCount, authorHandle vs
    authorUsername), so take the first key that is present."""
    def first(*keys):
        for k in keys:
            v = p.get(k)
            if v is not None:
                return v
        return None
    pid = str(first("id", "tweet_id", "status_id") or "")
    url = first("url") or (f"https://x.com/i/web/status/{pid}" if pid else "")
    media = [m.get("url") for m in (first("media") or [])
             if isinstance(m, dict) and m.get("url")]
    return {
        "id": pid,
        "text": first("text", "full_text") or "",
        "url": url,
        "author_name": first("authorName", "name") or "",
        "author_handle": first("authorHandle", "authorUsername", "screen_name") or "",
        "created": first("createdUtc", "created_at", "created"),
        "likes": first("likes", "likeCount") or 0,
        "retweets": first("retweets", "retweetCount") or 0,
        "replies": first("replies", "replyCount") or 0,
        "views": first("views", "viewCount") or 0,
        "media": media,
        "is_reply": bool(first("isReply", "is_reply")),
    }


def _x_when(created) -> str:
    if created is None:
        return ""
    if isinstance(created, (int, float)) and not isinstance(created, bool):
        return time.strftime("%Y-%m-%d %H:%M", time.gmtime(created))
    return str(created)


def _count(v) -> str:
    """1234 -> 1.2k, 1234567 -> 1.2m"""
    v = int(v or 0)
    if v >= 1_000_000:
        return f"{v / 1_000_000:.1f}m".replace(".0", "")
    if v >= 1000:
        return f"{v / 1000:.1f}k".replace(".0", "")
    return str(v)


def _x_fetch_image(url: str) -> bytes | None:
    """Fetch one media image off the GUI thread (None on any failure)."""
    try:
        r = httpx.get(url, timeout=20.0, follow_redirects=True)
        r.raise_for_status()
        return r.content
    except httpx.HTTPError:
        return None


def pretty_json(obj: object) -> str:
    try:
        return json.dumps(obj, indent=2, ensure_ascii=False)
    except (TypeError, ValueError):
        return str(obj)


def _markup_escape(s: str) -> str:
    """Escape &, < and > for Pango markup (pango_markup_escape_text is not
    exposed in the PyGObject binding)."""
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _esc(s: object) -> str:
    """Escape a value for Pango markup."""
    return _markup_escape(str(s))


# ---------------------------------------------------------------------------
# Markdown → Pango markup (practical subset, dependency-free)
#
# Used by the Browse page view, the Summarize pane and the 'Ask further'
# chat: headings, bold/italic, inline code, fenced code blocks, links,
# lists, blockquotes and horizontal rules.
# ---------------------------------------------------------------------------

def _inline_md_segments(text: str) -> list[tuple[str, str, str, str | None]]:
    """Inline Markdown → [(text, open, close, href)] segments. `text` is
    Pango-escaped; open/close are markup wrappers; href is set for links."""
    segs: list[tuple[str, str, str, str | None]] = []
    pattern = re.compile(
        r"(`[^`\n]+`)|(\*\*.+?\*\*)"
        r"|((?<!\*)\*[^*\n]+\*(?!\*))"
        r"|(\[[^\]]+\]\([^)\s]+\))")
    pos = 0
    for m in pattern.finditer(text):
        if m.start() > pos:
            segs.append((_markup_escape(text[pos:m.start()]), "", "", None))
        tok = m.group(0)
        if tok.startswith("`"):
            segs.append((_markup_escape(tok[1:-1]), "<tt>", "</tt>", None))
        elif tok.startswith("**"):
            segs.append((_markup_escape(tok[2:-2]), "<b>", "</b>", None))
        elif tok.startswith("["):
            mm = re.match(r"\[([^\]]+)\]\(([^)\s]+)\)", tok)
            segs.append((_markup_escape(mm.group(1)), "", "", mm.group(2)))
        else:
            segs.append((_markup_escape(tok[1:-1]), "<i>", "</i>", None))
        pos = m.end()
    if pos < len(text):
        segs.append((_markup_escape(text[pos:]), "", "", None))
    return segs


def markdown_segments(md: str) -> list[tuple[str, str, str, str | None]]:
    """Markdown → [(text, open, close, href)] segments for a Gtk.TextBuffer.
    Pango markup has no link tag, so links come back as plain segments with
    an href; the renderer turns those into clickable link tags."""
    lines = md.replace("\r\n", "\n").split("\n")
    out: list[tuple[str, str, str, str | None]] = []
    in_code = False
    for i, line in enumerate(lines):
        if i > 0:
            out.append(("\n", "", "", None))
        stripped = line.strip()
        if stripped.startswith("```"):
            in_code = not in_code
            continue
        if in_code:
            out.append((_markup_escape(line), "<tt>", "</tt>", None))
            continue
        m = re.match(r"^(#{1,6})\s+(.*)$", stripped)
        if m:
            size = {1: "xx-large", 2: "x-large", 3: "large"}.get(len(m.group(1)), "medium")
            for text, o, c, href in _inline_md_segments(m.group(2)):
                out.append((text, f'<span size="{size}"><b>', "</b></span>", None))
            continue
        if re.match(r"^(-{3,}|\*{3,}|_{3,})$", stripped):
            continue
        m = re.match(r"^>\s?(.*)$", stripped)
        if m:
            out.append(("  ", "", "", None))
            for text, o, c, href in _inline_md_segments(m.group(1)):
                out.append((text, "<i>", "</i>", None))
            continue
        m = re.match(r"^(\s*)([-*+]|\d+\.)\s+(.*)$", line)
        if m:
            marker = m.group(2)
            bullet = "•" if not marker[0].isdigit() else marker
            out.append((f"{m.group(1)}  {bullet} ", "", "", None))
            for text, o, c, href in _inline_md_segments(m.group(3)):
                out.append((text, o, c, None))
            continue
        if not stripped:
            continue
        out.extend(_inline_md_segments(line))
    return out


def _set_buffer_markup(buffer: Gtk.TextBuffer, markup: str):
    """Set a TextView buffer's content from Pango markup. (TextBuffer has no
    set_markup in the PyGObject binding; insert_markup parses it for us.)"""
    buffer.set_text("")
    buffer.insert_markup(buffer.get_start_iter(), markup, -1)


# ---------------------------------------------------------------------------
# browse-tab response parsing
# ---------------------------------------------------------------------------

def _search_results(body: object) -> list[dict]:
    """Normalise a treg.web.search response to a list of {title,url,snippet}."""
    out = body.get("output", body) if isinstance(body, dict) else body
    if isinstance(out, dict):
        rows = out.get("results") or out.get("items") or out.get("organic") or []
    elif isinstance(out, list):
        rows = out
    else:
        rows = []
    norm = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        url = row.get("url") or row.get("link") or row.get("href") or ""
        title = row.get("title") or row.get("name") or url
        snippet = (row.get("snippet") or row.get("description")
                   or row.get("content") or "")
        norm.append({"title": str(title), "url": str(url), "snippet": str(snippet)})
    return norm


def _page_markdown(body: object) -> str:
    """Pull the page's Markdown text out of a treg.web.extract response."""
    out = body.get("output", body) if isinstance(body, dict) else body
    if isinstance(out, dict):
        pages = out.get("pages")
        if isinstance(pages, list) and pages and isinstance(pages[0], dict):
            p = pages[0]
            return str(p.get("markdown") or p.get("text")
                       or p.get("content") or "")
        for key in ("markdown", "text", "content", "html"):
            if out.get(key):
                return str(out[key])
    if isinstance(out, str):
        return out
    return pretty_json(body)


# ---------------------------------------------------------------------------
# summarizer (OpenAI-compatible /v1/completions endpoint)
# ---------------------------------------------------------------------------

def _results_block(query: str, rows: list[dict]) -> str:
    """The numbered title/url/snippet list of the search results."""
    lines = [f"Web search results for the query: “{query}”", ""]
    for i, r in enumerate(rows, 1):
        lines.append(f"{i}. {r['title']} — {r['url']}")
        if r["snippet"]:
            lines.append(f"   {r['snippet']}")
    return "\n".join(lines)


def _complete(prompt: str, model: str = SUMMARY_MODEL) -> str:
    """One blocking call to the OpenAI-compatible /v1/completions endpoint."""
    try:
        r = httpx.post(f"{SUMMARY_URL}/completions",
                       headers={"Authorization": f"Bearer {SUMMARY_API_KEY}"},
                       json={"model": model, "prompt": prompt},
                       timeout=180.0)
    except httpx.ConnectError:
        raise TregError(f"cannot reach the summarize endpoint at {SUMMARY_URL}")
    if r.status_code >= 400:
        raise TregError(
            f"the summarize endpoint returned HTTP {r.status_code}: {r.text[:300]}",
            status=r.status_code)
    try:
        data = r.json()
    except ValueError:
        raise TregError("the summarize endpoint returned non-JSON")
    choices = data.get("choices") or []
    if not choices or not isinstance(choices[0], dict):
        raise TregError("the summarize endpoint returned no choices")
    raw = str(choices[0].get("text", ""))
    # The proxy prefixes the answer with a blank line, and the model is a
    # thinking model: it emits its reasoning in a leading "think" block. In
    # that case the answer follows the closing tag. The tags are built from
    # parts so this source file stays free of the literal tag text.
    open_tag = chr(60) + "think" + chr(62)
    close_tag = chr(60) + "/think" + chr(62)
    if raw.lstrip().startswith(open_tag):
        parts = raw.rsplit(close_tag, 1)
        return parts[1].strip() if len(parts) > 1 else "(the model stopped inside its reasoning block)"
    return raw.strip()


def summarize(query: str, rows: list[dict],
              model: str = SUMMARY_MODEL) -> str:
    """Summarize the Browse-tab search results (blocking)."""
    prompt = (
        "Summarize the web search results below in 3-5 sentences. "
        "Write the answer in Markdown (use **bold** for emphasis and a short "
        "list where it helps); cover what the results say about the query; "
        "do not list URLs.\n\n"
        + _results_block(query, rows)
    )
    return _complete(prompt, model)


def ask_followup(query: str, rows: list[dict], summary: str, question: str,
                 model: str = SUMMARY_MODEL) -> str:
    """Answer a follow-up question about a previous summary (blocking)."""
    prompt = (
        "You are answering a follow-up question about a web search summary.\n\n"
        + _results_block(query, rows) + "\n\n"
        + f"Summary:\n{summary}\n\n"
        + f"Question: {question}\n"
        "Answer in 2-4 sentences, in Markdown (use **bold** for emphasis; "
        "no headings)."
    )
    return _complete(prompt, model)


# ---------------------------------------------------------------------------
# async task polling (mirrors the treg CLI's descriptor semantics)
# ---------------------------------------------------------------------------

def _json_path(doc: object, dotted: str) -> object:
    cur = doc
    for part in dotted.split("."):
        if isinstance(cur, dict):
            cur = cur.get(part)
        elif isinstance(cur, list) and part.isdigit() and int(part) < len(cur):
            cur = cur[int(part)]
        else:
            return None
    return cur


def _classify(descriptor: dict, doc: object) -> str:
    rule = descriptor.get("status") or {}
    v = _json_path(doc, str(rule.get("path") or ""))
    if v is None:
        return "progress"
    s = str(v)
    if s in {str(x) for x in rule.get("success", [])}:
        return "success"
    if s in {str(x) for x in rule.get("billed_failure", [])}:
        return "billed_failure"
    if s in {str(x) for x in rule.get("failure", [])}:
        return "failure"
    return "progress"


def poll_async_task(client: TregClient, descriptor: dict, submission: httpx.Response,
                    on_progress, timeout: float = 900.0) -> dict:
    """Follow one X-Treg-Async descriptor to its terminal state (blocking)."""
    try:
        submitted = submission.json()
    except ValueError:
        return {"error": "the async submission response is not JSON", "response": submission}

    task_id = _json_path(submitted, str(descriptor.get("id_from") or ""))
    if task_id in (None, ""):
        return {"error": "submission response does not contain the async task id",
                "response": submission}
    task_id = str(task_id)

    poll = descriptor.get("poll") or {}
    if poll.get("endpoint"):
        rule = poll["param"]
        target, params = str(rule["in"]), [(str(rule["name"]), task_id)]
    else:
        url = _json_path(submitted, str(poll.get("url_from") or ""))
        if not isinstance(url, str) or not url:
            return {"error": "submission response does not contain the async poll URL",
                    "response": submission}
        target, params = url, []

    interval = float(descriptor.get("interval") or 10)
    max_age = descriptor.get("max_age")
    if isinstance(max_age, (int, float)) and not isinstance(max_age, bool) and max_age > 0:
        timeout = min(timeout, float(max_age))

    status_rule = descriptor.get("status") or {}

    def status_of(doc):
        return str(_json_path(doc, str(status_rule.get("path") or "")))

    # The submission may already be the finished answer: no poll needed.
    if descriptor.get("terminal_on_submission") is True and _classify(descriptor, submitted) == "success":
        return {"response": submission, "status": status_of(submitted), "task_id": task_id}

    start = time.monotonic()
    while True:
        if time.monotonic() - start >= timeout:
            return {"error": f"timed out after {int(timeout)}s waiting for task {task_id}",
                    "task_id": task_id, "response": None}
        time.sleep(interval)
        try:
            r = client.call(target, "GET", params=params)
        except httpx.HTTPError as e:
            on_progress(f"poll error: {e} — retrying")
            continue
        if r.status_code >= 400:
            return {"error": f"polling returned HTTP {r.status_code}",
                    "task_id": task_id, "response": r}
        try:
            doc = r.json()
        except ValueError:
            return {"error": "a polling response was not JSON", "task_id": task_id, "response": r}

        outcome = _classify(descriptor, doc)
        status = status_of(doc)
        if outcome == "success":
            out: dict = {"response": r, "status": status, "task_id": task_id}
            rule = descriptor.get("result") or {}
            if rule.get("path"):
                value = _json_path(doc, str(rule["path"]))
                out["result"] = value
                cands = value if isinstance(value, list) else [value]
                out["result_url"] = next(
                    (c for c in cands if isinstance(c, str) and c.startswith(("http://", "https://"))),
                    None)
            else:
                fr = rule.get("fetch_param") or {}
                fv = _json_path(doc, str(fr.get("value_from") or ""))
                if rule.get("fetch") and fr.get("name") and fv not in (None, ""):
                    out["fetch"] = {"endpoint": str(rule["fetch"]),
                                    "name": str(fr["name"]), "value": str(fv)}
            if rule.get("ttl_note"):
                out["ttl_note"] = str(rule["ttl_note"])
            return out
        if outcome in ("failure", "billed_failure"):
            return {"error": f"task {task_id} finished with status {status!r} ({outcome})",
                    "task_id": task_id, "response": r, "status": status}
        on_progress(f"task {task_id}: {status} ({int(time.monotonic() - start)}s elapsed)")


# ---------------------------------------------------------------------------
# worker thread
# ---------------------------------------------------------------------------

class Worker(threading.Thread):
    """Run a blocking function off the GUI thread and deliver the result (or
    error) back on the main loop via GLib.idle_add. Never call the GUI from
    run() directly — only schedule it."""

    _MISSING = object()

    def __init__(self, fn, *args, on_ok=None, on_fail=None, **kwargs):
        super().__init__(daemon=True)
        self._fn, self._args, self._kwargs = fn, args, kwargs
        self._on_ok, self._on_fail = on_ok, on_fail

    def run(self):
        try:
            ok, fail = self._fn(*self._args, **self._kwargs), self._MISSING
        except TregError as e:
            ok, fail = self._MISSING, str(e)
        except Exception as e:  # network, JSON, anything
            ok, fail = self._MISSING, f"{type(e).__name__}: {e}"
        GLib.idle_add(self._dispatch, ok, fail)

    def _dispatch(self, ok, fail) -> bool:
        if fail is not self._MISSING and self._on_fail:
            self._on_fail(fail)
        elif ok is not self._MISSING and self._on_ok:
            self._on_ok(ok)
        return False


# ---------------------------------------------------------------------------
# main window
# ---------------------------------------------------------------------------

class MainWindow(Gtk.ApplicationWindow):
    ASK_HINT = "Ask a question about the summary — the answer appears here."

    def __init__(self, application):
        super().__init__(application=application)
        self.set_title("treg console")
        self.set_default_size(1280, 800)
        self._set_icon()

        self.base_url, self.token = load_credentials()
        self.client = TregClient(self.base_url, self.token) if self.token else None
        self.org_id: int | None = None
        self._workers: list[Worker] = []
        self._status_msg = ""
        self._selected_endpoint: dict | None = None
        self._platforms: list[dict] = []
        self._x_posts: list[dict] = []
        self._x_trends: list[dict] = []
        self._x_selected: dict | None = None
        self._md_links: dict[Gtk.TextView, list[tuple[int, int, str]]] = {}

        self._build_ui()
        self.connect("destroy", self._on_destroy)

        if self.client is None:
            self._status(
                "no treg token found — run `treg login` or set TREG_TOKEN, then restart")
        else:
            self._refresh_identity()

    def _set_icon(self):
        # The icon theme copy is what the .desktop entry installs; fall back
        # to the package's own icon.png when the theme doesn't have it.
        if Gtk.IconTheme.get_default().has_icon("treg-console"):
            self.set_icon_name("treg-console")
        else:
            self.set_icon_from_file(str(Path(__file__).parent / "icon.png"))

    # -- UI construction ------------------------------------------------------

    def _build_ui(self):
        root = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        root.set_border_width(8)
        self.add(root)

        # top bar
        top = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        title = Gtk.Label()
        title.set_markup("<b>treg console</b>")
        top.pack_start(title, False, False, 0)
        self.team_label = Gtk.Label(label="team: …")
        self.balance_label = Gtk.Label()
        self.balance_label.set_markup("<b>balance: …</b>")
        refresh = Gtk.Button(label="Refresh")
        refresh.connect("clicked", lambda b: self._refresh_identity())
        # pack_end adds right-to-left: refresh, balance, team
        top.pack_end(refresh, False, False, 0)
        top.pack_end(self.balance_label, False, False, 0)
        top.pack_end(self.team_label, False, False, 0)
        root.pack_start(top, False, False, 0)

        self.tabs = Gtk.Notebook()
        self.tabs.append_page(self._build_browse_tab(), Gtk.Label(label="Browse"))
        self.tabs.append_page(self._build_summarize_tab(), Gtk.Label(label="Summarize"))
        self.tabs.append_page(self._build_catalog_tab(), Gtk.Label(label="Catalog"))
        self.tabs.append_page(self._build_providers_tab(), Gtk.Label(label="Providers"))
        self.tabs.append_page(self._build_call_tab(), Gtk.Label(label="Call"))
        self.tabs.append_page(self._build_history_tab(), Gtk.Label(label="History"))
        self.tabs.append_page(self._build_x_tab(), Gtk.Label(label="X"))
        self.tabs.set_current_page(0)
        root.pack_start(self.tabs, True, True, 0)

        self.statusbar = Gtk.Statusbar()
        self._status_ctx = self.statusbar.get_context_id("main")
        root.pack_start(self.statusbar, False, False, 0)

    @staticmethod
    def _scrolled(widget, max_height: int | None = None) -> Gtk.ScrolledWindow:
        sw = Gtk.ScrolledWindow(hscrollbar_policy=Gtk.PolicyType.AUTOMATIC,
                                vscrollbar_policy=Gtk.PolicyType.AUTOMATIC)
        if max_height is not None:
            sw.set_max_content_height(max_height)
        sw.add(widget)
        return sw

    @staticmethod
    def _textview(readonly: bool = True) -> Gtk.TextView:
        v = Gtk.TextView()
        v.set_editable(not readonly)
        v.set_wrap_mode(Gtk.WrapMode.WORD_CHAR)
        # padding between the text and the frame border
        v.set_left_margin(8)
        v.set_right_margin(8)
        v.set_top_margin(6)
        v.set_bottom_margin(6)
        return v

    def _build_browse_tab(self) -> Gtk.Widget:
        """The simple path: search the web, click a result, read the page.

        Search uses treg.web.search; clicking a result fetches the page with
        treg.web.extract and shows its clean Markdown here.
        """
        w = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)

        row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        self.browse_query = Gtk.Entry()
        self.browse_query.set_placeholder_text("search the web — e.g. “meson build system”")
        self.browse_query.connect("activate", lambda e: self._do_browse_search())
        go = Gtk.Button(label="Search")
        go.connect("clicked", lambda b: self._do_browse_search())
        self.browse_summarize_btn = Gtk.Button(label="Summarize")
        self.browse_summarize_btn.set_tooltip_text(
            f"Summarize the current results with {SUMMARY_MODEL} via {SUMMARY_URL}")
        self.browse_summarize_btn.set_sensitive(False)
        self.browse_summarize_btn.connect("clicked", lambda b: self._do_browse_summarize())
        self.browse_limit = Gtk.SpinButton.new_with_range(3, 20, 1)
        self.browse_limit.set_value(8)
        self.browse_limit.set_size_request(64, -1)
        row.pack_start(self.browse_query, True, True, 0)
        row.pack_start(go, False, False, 0)
        row.pack_start(self.browse_summarize_btn, False, False, 0)
        row.pack_start(Gtk.Label(label="Results"), False, False, 0)
        row.pack_start(self.browse_limit, False, False, 0)
        w.pack_start(row, False, False, 0)

        paned = Gtk.Paned.new(Gtk.Orientation.HORIZONTAL)

        self.browse_store = Gtk.ListStore(str, str, str)
        self.browse_results = Gtk.TreeView(model=self.browse_store)
        for header, col_idx in (("Title", 0), ("URL", 1), ("Snippet", 2)):
            self.browse_results.append_column(
                Gtk.TreeViewColumn(header, Gtk.CellRendererText(), text=col_idx))
        self.browse_results.get_selection().connect(
            "changed", lambda s: self._on_browse_result_selected())
        paned.pack1(self._scrolled(self.browse_results), True, True)

        right = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        self.browse_meta = Gtk.Label(label="Search, then click a result to read the page.")
        self.browse_meta.set_selectable(True)
        self.browse_meta.set_line_wrap(True)
        self.browse_meta.set_xalign(0.0)
        right.pack_start(self.browse_meta, False, False, 0)


        self.browse_page = self._textview()
        self.browse_page.connect("button-press-event", self._md_link_click)
        right.pack_start(self._scrolled(self.browse_page), True, True, 0)

        paned.pack2(right, True, True)
        paned.set_position(500)
        w.pack_start(paned, True, True, 0)
        return w

    def _build_summarize_tab(self) -> Gtk.Widget:
        """The summary of the current Browse-tab search, plus an 'Ask
        further' box for follow-up questions about it."""
        w = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)

        # A draggable divider between the summary and the 'Ask further'
        # pane, so either side can be resized.
        paned = Gtk.Paned.new(Gtk.Orientation.VERTICAL)

        out_box = Gtk.Frame(label=f"Summary — {SUMMARY_MODEL}")
        self.summarize_view = self._textview()
        self.summarize_view.connect("button-press-event", self._md_link_click)
        out_box.add(self._scrolled(self.summarize_view))
        paned.pack1(out_box, True, True)

        chat_box = Gtk.Frame(label="Ask further")
        cv = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        chat_box.add(cv)
        self.ask_view = self._textview()
        self.ask_view.connect("button-press-event", self._md_link_click)
        self.ask_scroll = self._scrolled(self.ask_view)
        cv.pack_start(self.ask_scroll, True, True, 0)
        # placeholder until the first question is asked
        abuf = self.ask_view.get_buffer()
        abuf.set_text(self.ASK_HINT)
        atag = abuf.create_tag("chat-hint", style=Pango.Style.ITALIC)
        abuf.apply_tag(atag, abuf.get_start_iter(), abuf.get_end_iter())
        ask_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        self.ask_entry = Gtk.Entry()
        self.ask_entry.set_placeholder_text("Ask a follow-up question about the summary…")
        self.ask_entry.connect("activate", lambda e: self._do_ask())
        self.ask_btn = Gtk.Button(label="Ask")
        self.ask_btn.connect("clicked", lambda b: self._do_ask())
        ask_row.pack_start(self.ask_entry, True, True, 0)
        ask_row.pack_start(self.ask_btn, False, False, 0)
        cv.pack_start(ask_row, False, False, 0)
        paned.pack2(chat_box, True, True)
        paned.set_position(500)
        w.pack_start(paned, True, True, 0)
        return w

    def _build_catalog_tab(self) -> Gtk.Widget:
        w = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)

        row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        self.search_edit = Gtk.Entry()
        self.search_edit.set_placeholder_text(
            "search the catalog — describe the job, e.g. “backlinks for a domain”")
        self.search_edit.connect("activate", lambda e: self._do_search())
        go = Gtk.Button(label="Search")
        go.connect("clicked", lambda b: self._do_search())
        row.pack_start(self.search_edit, True, True, 0)
        row.pack_start(go, False, False, 0)
        w.pack_start(row, False, False, 0)

        paned = Gtk.Paned.new(Gtk.Orientation.HORIZONTAL)

        self.results_store = Gtk.ListStore(str, str, str, str, str)
        self.results_table = Gtk.TreeView(model=self.results_store)
        for header, col_idx in (("Endpoint", 0), ("Provider", 1), ("Cost", 2),
                                ("Kind", 3), ("Summary", 4)):
            self.results_table.append_column(
                Gtk.TreeViewColumn(header, Gtk.CellRendererText(), text=col_idx))
        self.results_table.get_selection().connect(
            "changed", lambda s: self._on_result_selected())
        paned.pack1(self._scrolled(self.results_table), True, True)

        self.detail_view = self._textview()
        paned.pack2(self._scrolled(self.detail_view), True, True)
        paned.set_position(560)
        w.pack_start(paned, True, True, 0)

        btn_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        self.call_this_btn = Gtk.Button(label="Call this endpoint…")
        self.call_this_btn.set_sensitive(False)
        self.call_this_btn.connect("clicked", lambda b: self._call_selected())
        btn_row.pack_end(self.call_this_btn, False, False, 0)
        w.pack_start(btn_row, False, False, 0)
        return w

    def _build_providers_tab(self) -> Gtk.Widget:
        w = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)

        row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        self.prov_filter = Gtk.Entry()
        self.prov_filter.set_placeholder_text("filter by platform or category…")
        self.prov_filter.connect("changed", lambda e: self._apply_prov_filter())
        load = Gtk.Button(label="Load")
        load.connect("clicked", lambda b: self._load_platforms())
        row.pack_start(self.prov_filter, True, True, 0)
        row.pack_start(load, False, False, 0)
        w.pack_start(row, False, False, 0)

        paned = Gtk.Paned.new(Gtk.Orientation.HORIZONTAL)

        self.prov_store = Gtk.ListStore(str, str, str, str, str, str)
        self.prov_table = Gtk.TreeView(model=self.prov_store)
        for header, col_idx in (("Platform", 1), ("Category", 2), ("Endpoints", 3),
                                ("Verified", 4), ("Price from", 5)):
            self.prov_table.append_column(
                Gtk.TreeViewColumn(header, Gtk.CellRendererText(), text=col_idx))
        self.prov_table.get_selection().connect(
            "changed", lambda s: self._on_platform_selected())
        paned.pack1(self._scrolled(self.prov_table), True, True)

        self.prov_detail = self._textview()
        paned.pack2(self._scrolled(self.prov_detail), True, True)
        paned.set_position(560)
        w.pack_start(paned, True, True, 0)
        return w

    def _build_call_tab(self) -> Gtk.Widget:
        w = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)

        # endpoint + method
        row1 = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        row1.pack_start(Gtk.Label(label="Endpoint"), False, False, 0)
        self.call_target = Gtk.Entry()
        self.call_target.set_placeholder_text(
            "catalog id (treg.web.search) or team tool URL (https://api.stripe.com/v1/…)")
        self.call_method = Gtk.ComboBoxText()
        self.call_method.append_text("GET")
        self.call_method.append_text("POST")
        self.call_method.set_active(0)
        self.call_method.set_size_request(90, -1)
        row1.pack_start(self.call_target, True, True, 0)
        row1.pack_start(self.call_method, False, False, 0)
        w.pack_start(row1, False, False, 0)

        # query params
        params_box = Gtk.Frame(label="Query parameters")
        pv = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        params_box.add(pv)
        self.params_store = Gtk.ListStore(str, str)
        self.params_table = Gtk.TreeView(model=self.params_store)
        for header, col_idx in (("Key", 0), ("Value", 1)):
            rend = Gtk.CellRendererText()
            rend.set_property("editable", True)
            self.params_table.append_column(
                Gtk.TreeViewColumn(header, rend, text=col_idx))
        pv.pack_start(self._scrolled(self.params_table, max_height=160), True, True, 0)
        prow = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        add_p = Gtk.Button(label="Add")
        add_p.connect("clicked", lambda b: self._add_param_row())
        rm_p = Gtk.Button(label="Remove")
        rm_p.connect("clicked", lambda b: self._remove_param_row())
        prow.pack_start(add_p, False, False, 0)
        prow.pack_start(rm_p, False, False, 0)
        pv.pack_start(prow, False, False, 0)
        w.pack_start(params_box, False, False, 0)

        # body
        body_box = Gtk.Frame(label="JSON body (POST)")
        self.body_edit = self._textview(readonly=False)
        self.body_edit.set_wrap_mode(Gtk.WrapMode.NONE)
        body_box.add(self._scrolled(self.body_edit, max_height=160))
        w.pack_start(body_box, False, False, 0)

        # options
        opt = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        opt.pack_start(Gtk.Label(label="Route max cost ($, optional)"), False, False, 0)
        self.route_max = Gtk.Entry()
        self.route_max.set_placeholder_text("e.g. 0.50")
        self.route_max.set_size_request(110, -1)
        opt.pack_start(self.route_max, False, False, 0)
        opt.pack_start(Gtk.Label(label="Idempotency key (optional)"), False, False, 0)
        self.idem_key = Gtk.Entry()
        self.idem_key.set_placeholder_text("retry-safe key")
        self.idem_key.set_size_request(160, -1)
        opt.pack_start(self.idem_key, False, False, 0)
        w.pack_start(opt, False, False, 0)

        # fire
        fire_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        self.call_btn = Gtk.Button(label="Call")
        self.call_btn.connect("clicked", lambda b: self._do_call())
        self.call_status = Gtk.Label(label="")
        self.call_status.set_xalign(0.0)
        fire_row.pack_start(self.call_btn, False, False, 0)
        fire_row.pack_start(self.call_status, True, True, 0)
        w.pack_start(fire_row, False, False, 0)

        # result
        res_box = Gtk.Frame(label="Result")
        rv = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        res_box.add(rv)
        self.result_meta = Gtk.Label(label="—")
        self.result_meta.set_selectable(True)
        self.result_meta.set_line_wrap(True)
        self.result_meta.set_xalign(0.0)
        rv.pack_start(self.result_meta, False, False, 0)
        self.result_view = self._textview()
        rv.pack_start(self._scrolled(self.result_view), True, True, 0)
        w.pack_start(res_box, True, True, 0)
        return w

    def _build_history_tab(self) -> Gtk.Widget:
        w = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)

        row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        refresh = Gtk.Button(label="Refresh")
        refresh.connect("clicked", lambda b: self._do_history())
        self.history_limit = Gtk.SpinButton.new_with_range(5, 100, 1)
        self.history_limit.set_value(25)
        row.pack_start(refresh, False, False, 0)
        row.pack_start(Gtk.Label(label="Limit"), False, False, 0)
        row.pack_start(self.history_limit, False, False, 0)
        w.pack_start(row, False, False, 0)

        paned = Gtk.Paned.new(Gtk.Orientation.VERTICAL)

        self.history_store = Gtk.ListStore(str, str, str, str, str)
        self.history_table = Gtk.TreeView(model=self.history_store)
        for header, col_idx in (("Time", 0), ("Endpoint", 1), ("Status", 2),
                                ("Cost", 3), ("Call ID", 4)):
            self.history_table.append_column(
                Gtk.TreeViewColumn(header, Gtk.CellRendererText(), text=col_idx))
        self.history_table.get_selection().connect(
            "changed", lambda s: self._on_history_selected())
        paned.pack1(self._scrolled(self.history_table), True, True)

        self.history_result = self._textview()
        paned.pack2(self._scrolled(self.history_result), True, True)
        paned.set_position(300)
        w.pack_start(paned, True, True, 0)
        return w

    def _build_x_tab(self) -> Gtk.Widget:
        w = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)

        row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        self.x_mode = Gtk.ComboBoxText()
        for label in ("Search", "User", "Trends"):
            self.x_mode.append_text(label)
        self.x_mode.set_active(0)
        self.x_mode.connect("changed", self._x_mode_changed)
        self.x_entry = Gtk.Entry()
        self.x_entry.set_placeholder_text("search X — keyword, #hashtag, from:user…")
        self.x_entry.connect("activate", lambda e: self._x_go())
        go = Gtk.Button(label="Go")
        go.connect("clicked", lambda b: self._x_go())
        row.pack_start(self.x_mode, False, False, 0)
        row.pack_start(self.x_entry, True, True, 0)
        row.pack_start(go, False, False, 0)
        w.pack_start(row, False, False, 0)

        paned = Gtk.Paned.new(Gtk.Orientation.HORIZONTAL)

        self.x_store = Gtk.ListStore(str, str, str, str, str, str)
        self.x_table = Gtk.TreeView(model=self.x_store)
        for header, col_idx in (("Author", 0), ("Post", 1), ("Time", 2),
                                ("Likes", 3), ("Reposts", 4), ("Replies", 5)):
            self.x_table.append_column(
                Gtk.TreeViewColumn(header, Gtk.CellRendererText(), text=col_idx))
        self.x_table.get_selection().connect("changed", lambda s: self._on_x_selected())
        paned.pack1(self._scrolled(self.x_table), True, True)

        right = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        self.x_image = Gtk.Image()
        right.pack_start(self.x_image, False, False, 0)
        self.x_detail = self._textview()
        self.x_detail.connect("button-press-event", self._md_link_click)
        right.pack_start(self._scrolled(self.x_detail), True, True, 0)
        paned.pack2(right, True, True)
        paned.set_position(560)
        w.pack_start(paned, True, True, 0)

        btn_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        self.x_replies_btn = Gtk.Button(label="Replies")
        self.x_replies_btn.set_sensitive(False)
        self.x_replies_btn.connect("clicked", lambda b: self._x_replies())
        self.x_profile_btn = Gtk.Button(label="Profile")
        self.x_profile_btn.set_sensitive(False)
        self.x_profile_btn.connect("clicked", lambda b: self._x_profile())
        self.x_open_btn = Gtk.Button(label="Open on X")
        self.x_open_btn.set_sensitive(False)
        self.x_open_btn.connect("clicked", lambda b: self._x_open())
        btn_row.pack_start(self.x_replies_btn, False, False, 0)
        btn_row.pack_start(self.x_profile_btn, False, False, 0)
        btn_row.pack_start(self.x_open_btn, False, False, 0)
        w.pack_start(btn_row, False, False, 0)
        return w

    # -- plumbing -------------------------------------------------------------

    def _link_style_tag(self, buffer: Gtk.TextBuffer) -> Gtk.TextTag:
        """Tag that makes Markdown link text look like a link (GTK3's
        TextTag has no link-href property, so click handling is ours)."""
        table = buffer.get_tag_table()
        tag = table.lookup("md-link-style")
        if tag is None:
            tag = buffer.create_tag("md-link-style")
            tag.set_property("foreground", "#1b6ca8")
            tag.set_property("underline", Pango.Underline.SINGLE)
        return tag

    def _insert_md(self, view: Gtk.TextView, it: Gtk.TextIter, md: str,
                   base_url: str | None = None) -> None:
        """Insert rendered Markdown at `it`; href segments become clickable links.
        `base_url` resolves relative hrefs (page links) to absolute URIs."""
        buffer = view.get_buffer()
        links = self._md_links.setdefault(view, [])
        for text, open_m, close_m, href in markdown_segments(md):
            if not text:
                continue
            off = it.get_offset()
            if href is not None:
                uri = urljoin(base_url, href) if base_url else href
                buffer.insert(it, text)
                start = buffer.get_iter_at_offset(off)
                end = buffer.get_iter_at_offset(off + len(text))
                buffer.apply_tag(self._link_style_tag(buffer), start, end)
                links.append((off, off + len(text), uri))
            else:
                buffer.insert_markup(it, f"{open_m}{text}{close_m}" if open_m else text, -1)
            it.forward_chars(len(text))

    def _render_md(self, view: Gtk.TextView, md: str,
                   base_url: str | None = None) -> None:
        """Replace a TextView's content with rendered Markdown."""
        buffer = view.get_buffer()
        buffer.set_text("")
        self._md_links[view] = []
        self._insert_md(view, buffer.get_start_iter(), md, base_url)

    def _md_link_click(self, view, event) -> bool:
        """Open a Markdown link under the pointer, if any."""
        # Event coordinates are relative to the widget window's on-screen
        # origin (viewport space); get_iter_at_location() wants coordinates
        # relative to the top of the buffer (content space), so add the
        # scroll offset of the enclosing scrolled window.
        sx, sy = self._view_scroll(view)
        ok, it = view.get_iter_at_location(int(event.x) + sx,
                                           int(event.y) + sy)
        if not ok or it is None:
            return False
        offset = it.get_offset()
        for start, end, uri in self._md_links.get(view, []):
            if start <= offset < end:
                try:
                    Gio.AppInfo.launch_default_for_uri(uri, None)
                except GLib.Error as e:
                    self._error(f"could not open {uri}: {e.message}")
                return True
        return False

    @staticmethod
    def _view_scroll(view) -> tuple[int, int]:
        """(hscroll, vscroll) of the scrolled window containing `view`."""
        p = view.get_parent()
        while p is not None:
            if isinstance(p, Gtk.ScrolledWindow):
                return (int(p.get_hadjustment().get_value()),
                        int(p.get_vadjustment().get_value()))
            p = p.get_parent()
        return (0, 0)


    @staticmethod
    def _text(view: Gtk.TextView) -> str:
        buf = view.get_buffer()
        start, end = buf.get_bounds()
        return buf.get_text(start, end, True)

    def _spawn(self, fn, *args, on_ok=None, on_fail=None, **kwargs) -> Worker:
        w = Worker(fn, *args, on_ok=on_ok, on_fail=on_fail, **kwargs)
        self._workers.append(w)
        w.start()
        return w

    def _status(self, msg: str):
        if self._status_msg:
            self.statusbar.pop(self._status_ctx)
        self._status_msg = msg
        self.statusbar.push(self._status_ctx, msg)

    def _busy(self, msg: str):
        self._status(msg)

    def _error(self, msg: str):
        self._status(f"error: {msg}")

    # -- identity / balance ----------------------------------------------------

    def _refresh_identity(self):
        if self.client is None:
            return
        self._busy("Loading team and balance…")
        self._spawn(self._load_identity,
                    on_ok=self._identity_loaded,
                    on_fail=lambda m: self._error(f"identity: {m}"))

    def _load_identity(self):
        me = self.client.me()
        org_id = me.get("org_id")
        bal = self.client.balance(org_id, limit=5) if org_id else None
        return {"me": me, "balance": bal}

    def _identity_loaded(self, data):
        me, bal = data["me"], data["balance"]
        self.org_id = me.get("org_id")
        team = me.get("org") or "—"
        self.team_label.set_text(f"team: {team} · {me.get('email', '')}")
        if bal:
            self.balance_label.set_markup(f"<b>balance: {usd(bal.get('balance_micro'))}</b>")
        self._busy(f"Connected to {self.base_url} as {me.get('email', '')}")

    # -- catalog ----------------------------------------------------------------

    def _do_search(self):
        if self.client is None:
            return
        q = self.search_edit.get_text().strip()
        if not q:
            return
        self._busy(f"Searching “{q}”…")
        self._spawn(self.client.search, q, 25,
                    on_ok=self._search_done,
                    on_fail=lambda m: self._error(f"search: {m}"))

    def _search_done(self, body: dict):
        rows = body.get("results") or []
        self.results_store.clear()
        for e in rows:
            cost = e.get("cost") or {}
            cost_txt = usd_amount(cost.get("usd")) if cost.get("usd") is not None else (
                "varies" if cost.get("varies") else "—")
            kind = e.get("kind") or ""
            self.results_store.append([
                e.get("id", ""), e.get("provider", ""), cost_txt, kind,
                e.get("summary") or e.get("name") or ""])
        verdict = body.get("verdict")
        if verdict == "none":
            self._error(f"the catalog has no tool for “{self.search_edit.get_text().strip()}”")
        elif not rows:
            self._error("no matches — try different task words")
        else:
            self._busy(f"{body.get('total', len(rows))} matches for “{self.search_edit.get_text().strip()}”")

    def _on_result_selected(self):
        model, treeiter = self.results_table.get_selection().get_selected()
        if treeiter is None or self.client is None:
            return
        ep_id = model[treeiter][0]
        if not ep_id:
            return
        self._busy(f"Loading {ep_id}…")
        self._spawn(self.client.endpoint, ep_id,
                    on_ok=lambda d: self._endpoint_loaded(ep_id, d),
                    on_fail=lambda m: self._error(f"endpoint: {m}"))

    def _endpoint_loaded(self, ep_id: str, data: dict):
        e = data.get("endpoint") or {}
        prov = data.get("provider") or {}
        self._selected_endpoint = e
        self.call_this_btn.set_sensitive(True)

        cost = e.get("cost") or {}
        cost_txt = usd_amount(cost.get("usd")) if cost.get("usd") is not None else "not priced"
        if cost.get("varies"):
            cost_txt += " (varies)"

        h = [f'<span size="xx-large">{_esc(e.get("id", ep_id))}</span>']
        if e.get("summary"):
            h.append(_esc(e.get("summary")))
        h.append("")
        h.append(f"<b>Provider:</b> {_esc(prov.get('display_name') or e.get('provider', ''))}")
        h.append(f"<b>Call:</b> <tt>{_esc(e.get('method', ''))} {_esc(e.get('path', ''))}</tt>")
        h.append(f"<b>Cost:</b> {cost_txt}")
        h.append(f"<b>Kind:</b> {_esc(e.get('kind', ''))}")
        h.append(f"<b>Capability:</b> {_esc(e.get('capability', ''))}")
        h.append(f"<b>Added:</b> {_esc(e.get('added') or '—')}")
        if cost.get("note"):
            h.append(f"<i>{_esc(cost['note'])}</i>")

        inp = e.get("input") or {}
        body_spec = inp.get("body") or {}
        if body_spec:
            h.append("")
            h.append('<span size="large"><b>Inputs</b></span>')
            for name, spec in body_spec.items():
                if not isinstance(spec, dict):
                    continue
                req = "required" if spec.get("required") else "optional"
                dflt = f" (default {json.dumps(spec['default'])})" if "default" in spec else ""
                note = f" — {spec['note']}" if spec.get("note") else ""
                h.append(f"  <tt>{_esc(name)}</tt> — {_esc(spec.get('type', ''))} · {req}{dflt}{note}")
        if inp.get("note"):
            h.append(f"<i>{_esc(inp['note'])}</i>")

        out = e.get("output") or {}
        fields = out.get("fields") if isinstance(out, dict) and "fields" in out else (
            list(out) if isinstance(out, dict) else None)
        if fields:
            h.append("")
            h.append('<span size="large"><b>Output</b></span>')
            h.append(_esc(", ".join(str(x) for x in fields)))

        siblings = data.get("siblings") or []
        if siblings:
            h.append("")
            h.append('<span size="large"><b>Other providers for this job</b></span>')
            for s in siblings[:12]:
                sc = s.get("cost") or {}
                sc_txt = usd_amount(sc.get("usd")) if sc.get("usd") is not None else "—"
                h.append(f"  <tt>{_esc(s.get('id', ''))}</tt> — {_esc(s.get('provider', ''))} — {sc_txt}")

        if e.get("async"):
            h.append("")
            h.append("<b>Async</b> — this is a generation task; the Call tab polls it for you.")
        if e.get("call_template"):
            h.append(f"<tt>{_esc(e.get('call_template'))}</tt>")

        _set_buffer_markup(self.detail_view.get_buffer(), "\n".join(h))
        self._busy(f"Loaded {ep_id}")

    def _call_selected(self):
        e = self._selected_endpoint
        if not e:
            return
        self.call_target.set_text(e.get("id", ""))
        self.call_method.set_active(1 if (e.get("method") or "GET").upper() == "POST" else 0)
        self.tabs.set_current_page(4)  # the Call tab

    # -- providers ------------------------------------------------------------------

    def _load_platforms(self):
        if self.client is None:
            return
        self._busy("Loading platforms…")
        self._spawn(self.client.platforms,
                    on_ok=self._platforms_loaded,
                    on_fail=lambda m: self._error(f"platforms: {m}"))

    def _platforms_loaded(self, body: dict):
        self._platforms = body.get("platforms") or []
        self._apply_prov_filter()
        self._busy(f"{len(self._platforms)} platforms")

    def _apply_prov_filter(self):
        q = self.prov_filter.get_text().strip().lower()
        self.prov_store.clear()
        for p in self._platforms:
            label = (p.get("label") or p.get("slug") or "").lower()
            cat = (p.get("category") or "").lower()
            if q and q not in label and q not in cat:
                continue
            pf = p.get("price_from") or {}
            val = pf.get("usd", pf.get("value"))
            self.prov_store.append([
                p.get("slug", ""), p.get("label") or p.get("slug") or "",
                p.get("category") or "", str(p.get("endpoints") or ""),
                str(p.get("verified") or ""),
                usd_amount(val) if val is not None else "—"])

    def _on_platform_selected(self):
        model, treeiter = self.prov_table.get_selection().get_selected()
        if treeiter is None or self.client is None:
            return
        slug = model[treeiter][0]
        if not slug:
            return
        self._busy(f"Loading {slug}…")
        self._spawn(self.client.platform, slug,
                    on_ok=lambda d: self._platform_loaded(slug, d),
                    on_fail=lambda m: self._error(f"platform: {m}"))

    def _platform_loaded(self, slug: str, data: dict):
        p = data.get("platform") or {}
        row = next((x for x in self._platforms if x.get("slug") == slug), {})
        h = [f'<span size="xx-large">{_esc(p.get("label") or slug)}</span>']
        if row.get("summary"):
            h.append(_esc(row["summary"]))
        h.append("")
        h.append(f"<b>Category:</b> {_esc(p.get('category', ''))}")
        if row.get("providers"):
            h.append(f"<b>Providers:</b> {_esc(', '.join(row['providers']))}")
        pf = row.get("price_from") or {}
        val = pf.get("usd", pf.get("value"))
        if val is not None:
            note = f" (checked {pf['checked']})" if pf.get("checked") else ""
            h.append(f"<b>Price from:</b> {usd_amount(val)}{note}")
        h.append(f"<b>Endpoints:</b> {row.get('endpoints', '—')} "
                 f"({row.get('verified', '—')} verified)")
        h.append("")
        h.append('<span size="large"><b>Capabilities</b></span>')
        for cap in data.get("capabilities") or []:
            eps = cap.get("endpoints") or []
            h.append(f"<b>{_esc(cap.get('id', ''))}</b> — "
                     f"{_esc(cap.get('description') or '')} — {len(eps)} endpoints")
            for e in eps:
                cost = e.get("cost") or {}
                cval = cost.get("usd", cost.get("value"))
                cost_txt = usd_amount(cval) if cval is not None else "—"
                h.append(f"  <tt>{_esc(e.get('id', ''))}</tt> — "
                         f"{_esc(e.get('name') or '')} — {cost_txt}")
        _set_buffer_markup(self.prov_detail.get_buffer(), "\n".join(h))
        self._busy(f"Loaded {slug}")

    # -- browse --------------------------------------------------------------------

    def _do_browse_search(self):
        if self.client is None:
            return
        q = self.browse_query.get_text().strip()
        if not q:
            return
        self._browse_last_query = q
        self._busy(f"Searching the web for “{q}”…")
        body = json.dumps({"q": q, "limit": int(self.browse_limit.get_value())})
        self._spawn(self.client.call, "treg.web.search", "POST", None, body,
                    on_ok=self._browse_search_done,
                    on_fail=lambda m: self._error(f"search: {m}"))

    def _browse_search_done(self, r: httpx.Response):
        try:
            body = r.json()
        except ValueError:
            body = None
        if r.status_code >= 400 or body is None:
            detail = body.get("detail", "") if isinstance(body, dict) else ""
            self._error(f"search: HTTP {r.status_code} {detail}".rstrip())
            return
        rows = _search_results(body)
        self._browse_rows = rows
        self.browse_summarize_btn.set_sensitive(bool(rows))
        self.browse_store.clear()
        for res in rows:
            self.browse_store.append(
                [res["title"] or res["url"] or "(untitled)", res["url"], res["snippet"]])
        bits = [f"{len(rows)} results"]
        if r.headers.get("X-Treg-Served-By"):
            bits.append(f"via {r.headers['X-Treg-Served-By']}")
        if r.headers.get("X-Treg-Cost-Micro") is not None:
            bits.append(f"charged {usd(int(r.headers['X-Treg-Cost-Micro']))}")
        bits.append("— click a result to read the page")
        self.browse_meta.set_text(" · ".join(bits))
        self._busy(" · ".join(bits))

    def _do_browse_summarize(self):
        rows = getattr(self, "_browse_rows", None)
        if not rows:
            self._error("run a search first, then press Summarize")
            return
        self.browse_summarize_btn.set_sensitive(False)
        self.tabs.set_current_page(1)  # the Summarize tab
        self._render_md(self.summarize_view, f"*Summarizing with {SUMMARY_MODEL} …*")
        self._busy(f"Summarizing “{self._browse_last_query}” with {SUMMARY_MODEL} …")
        self._spawn(summarize, self._browse_last_query, rows,
                    on_ok=self._browse_summary_done,
                    on_fail=lambda m: self._browse_summary_failed(m))

    def _browse_summary_done(self, text: str):
        self.browse_summarize_btn.set_sensitive(True)
        self.tabs.set_current_page(1)
        self._render_md(self.summarize_view,
                        text or "(the model returned an empty summary)")
        self._busy(f"Summary ready ({SUMMARY_MODEL})")

    def _browse_summary_failed(self, msg: str):
        self.browse_summarize_btn.set_sensitive(True)
        self.tabs.set_current_page(1)
        self._render_md(self.summarize_view, f"*(summarize failed) {msg}*")
        self._error(f"summarize: {msg}")

    def _do_ask(self):
        rows = getattr(self, "_browse_rows", None)
        question = self.ask_entry.get_text().strip()
        summary = self._text(self.summarize_view)
        if not question:
            return
        if not rows or not summary or summary.startswith("Summarizing"):
            self._error("run a search and press Summarize first, then ask a follow-up")
            return
        self.ask_entry.set_text("")
        self.ask_btn.set_sensitive(False)
        buf = self.ask_view.get_buffer()
        if buf.get_text(buf.get_start_iter(), buf.get_end_iter(), True) == self.ASK_HINT:
            buf.set_text("")
        self._append_chat_q(question)
        self._busy("Answering follow-up…")
        self._spawn(ask_followup, self._browse_last_query, rows, summary, question,
                    on_ok=self._ask_done,
                    on_fail=lambda m: self._ask_failed(m))

    def _chat_q_tag(self, buffer: Gtk.TextBuffer) -> Gtk.TextTag:
        table = buffer.get_tag_table()
        tag = table.lookup("chat-q")
        if tag is None:
            tag = buffer.create_tag("chat-q")
            tag.set_property("weight", Pango.Weight.BOLD)
        return tag

    def _append_chat_q(self, question: str) -> None:
        """Append a styled question line (bold 'You — …')."""
        buf = self.ask_view.get_buffer()
        text = f"You — {question}\n"
        it = buf.get_end_iter()
        off = it.get_offset()
        buf.insert(it, text)
        buf.apply_tag(self._chat_q_tag(buf),
                      buf.get_iter_at_offset(off), buf.get_iter_at_offset(off + len(text)))

    @staticmethod
    def _scroll_bottom(sw: Gtk.ScrolledWindow) -> None:
        adj = sw.get_vadjustment()
        if adj is not None:
            adj.set_value(adj.get_upper())

    def _ask_done(self, text: str):
        self.ask_btn.set_sensitive(True)
        self._insert_md(self.ask_view, self.ask_view.get_buffer().get_end_iter(),
                        text or "(the model returned an empty answer)")
        self._scroll_bottom(self.ask_scroll)
        self._busy("Follow-up answered")

    def _ask_failed(self, msg: str):
        self.ask_btn.set_sensitive(True)
        self._insert_md(self.ask_view, self.ask_view.get_buffer().get_end_iter(),
                        f"*(failed) {msg}*\n")
        self._scroll_bottom(self.ask_scroll)
        self._error(f"ask: {msg}")

    def _on_browse_result_selected(self):
        model, treeiter = self.browse_results.get_selection().get_selected()
        if treeiter is None or self.client is None:
            return
        url = model[treeiter][1]
        if not url:
            return
        self._busy(f"Reading {url}…")
        self.browse_meta.set_text(f"Reading {url} …")
        body = json.dumps({"url": url})
        self._spawn(self.client.call, "treg.web.extract", "POST", None, body,
                    on_ok=lambda r: self._browse_page_loaded(url, r),
                    on_fail=lambda m: self._error(f"read: {m}"))

    def _browse_page_loaded(self, url: str, r: httpx.Response):
        try:
            body = r.json()
        except ValueError:
            body = None
        if r.status_code >= 400 or body is None:
            detail = body.get("detail", "") if isinstance(body, dict) else ""
            self.browse_meta.set_text(f"Could not read {url} (HTTP {r.status_code})")
            self.browse_page.get_buffer().set_text(detail or r.text[:2000])
            self._error(f"read: HTTP {r.status_code}")
            return
        bits = [url]
        if r.headers.get("X-Treg-Served-By"):
            bits.append(f"via {r.headers['X-Treg-Served-By']}")
        if r.headers.get("X-Treg-Cost-Micro") is not None:
            bits.append(f"charged {usd(int(r.headers['X-Treg-Cost-Micro']))}")
        self.browse_meta.set_text(" · ".join(bits))
        self._render_md(self.browse_page, _page_markdown(body), base_url=url)
        self._busy(f"Read {url}")

    # -- call --------------------------------------------------------------------

    def _add_param_row(self):
        self.params_store.append(["", ""])

    def _remove_param_row(self):
        model, treeiter = self.params_table.get_selection().get_selected()
        if treeiter is not None:
            self.params_store.remove(treeiter)

    def _do_call(self):
        if self.client is None:
            return
        target = self.call_target.get_text().strip()
        if not target:
            self._error("Enter an endpoint id or tool URL")
            return
        method = self.call_method.get_active_text() or "GET"
        params = []
        for row in self.params_store:
            k = (row[0] or "").strip()
            v = (row[1] or "").strip()
            if k:
                params.append((k, v))
        body = self._text(self.body_edit).strip() or None
        if body:
            try:
                json.loads(body)
            except ValueError as e:
                self._error(f"Body is not valid JSON: {e}")
                return
        headers = {}
        if self.route_max.get_text().strip():
            headers["X-Treg-Route-Max-Cost"] = self.route_max.get_text().strip()
        if self.idem_key.get_text().strip():
            headers["Idempotency-Key"] = self.idem_key.get_text().strip()

        self.call_btn.set_sensitive(False)
        self.call_status.set_text("Calling…")
        self._busy(f"Calling {target}…")
        self._spawn(self.client.call, target, method, params, body, headers,
                    on_ok=self._call_done,
                    on_fail=lambda m: self._call_failed(m))

    def _call_done(self, r: httpx.Response):
        self.call_btn.set_sensitive(True)
        h = r.headers
        meta_bits = [f"HTTP {r.status_code}"]
        if h.get("X-Treg-Cost-Micro") is not None:
            meta_bits.append(f"charged {usd(int(h['X-Treg-Cost-Micro']))}")
        if h.get("X-Treg-Call-Id"):
            meta_bits.append(f"call id {h['X-Treg-Call-Id']}")
        if h.get("X-Treg-Served-By"):
            meta_bits.append(f"served by {h['X-Treg-Served-By']}")
        if h.get("X-Treg-Served-Via"):
            meta_bits.append(f"via {h['X-Treg-Served-Via']}")
        if h.get("X-Treg-Cache"):
            meta_bits.append(f"cache {h['X-Treg-Cache']}")
        self.result_meta.set_text(" · ".join(meta_bits))

        if h.get("X-Treg-Async"):
            try:
                descriptor = json.loads(h["X-Treg-Async"])
            except ValueError:
                descriptor = None
            if descriptor:
                self.call_status.set_text("Async task — polling…")
                self._busy("Async task submitted, polling…")
                self._spawn(poll_async_task, self.client, descriptor, r,
                            self._async_progress,
                            on_ok=self._async_done,
                            on_fail=lambda m: self._call_failed(m))
                return
        self._show_response(r)
        self.call_status.set_text(f"Done — HTTP {r.status_code}")

    def _async_progress(self, msg: str):
        self.call_status.set_text(msg)
        self._busy(msg)

    def _async_done(self, out: dict):
        self.call_btn.set_sensitive(True)
        if out.get("error"):
            self.call_status.set_text(f"Async: {out['error']}")
            if out.get("response") is not None:
                self._show_response(out["response"])
            self._error(f"async task: {out['error']}")
            return
        r = out["response"]
        h = r.headers
        meta_bits = [f"HTTP {r.status_code}", f"task {out.get('task_id', '')}"]
        if h.get("X-Treg-Cost-Micro") is not None:
            meta_bits.append(f"charged {usd(int(h['X-Treg-Cost-Micro']))}")
        if h.get("X-Treg-Call-Id"):
            meta_bits.append(f"call id {h['X-Treg-Call-Id']}")
        self.result_meta.set_text(" · ".join(meta_bits))
        self._show_response(r)
        extra = []
        if out.get("result_url"):
            extra.append(f"result: {out['result_url']}")
        if out.get("fetch"):
            f = out["fetch"]
            extra.append(f"retrieve: treg call {f['endpoint']} -p {f['name']}={f['value']}")
        if out.get("ttl_note"):
            extra.append(out["ttl_note"])
        self.call_status.set_text("Async done" + (f" — {'; '.join(extra)}" if extra else ""))
        self._busy("Async task complete")

    def _call_failed(self, msg: str):
        self.call_btn.set_sensitive(True)
        self.call_status.set_text("Failed")
        self._error(msg)

    def _show_response(self, r: httpx.Response):
        ctype = r.headers.get("content-type", "")
        if "json" in ctype:
            try:
                self.result_view.get_buffer().set_text(pretty_json(r.json()))
                return
            except ValueError:
                pass
        self.result_view.get_buffer().set_text(r.text)

    # -- history -------------------------------------------------------------------

    def _do_history(self):
        if self.client is None:
            return
        self._busy("Loading call history…")
        self._spawn(self.client.calls, int(self.history_limit.get_value()),
                    on_ok=self._history_done,
                    on_fail=lambda m: self._error(f"history: {m}"))

    def _history_done(self, calls: list):
        self.history_store.clear()
        for cr in calls:
            st = cr.get("status_code", "")
            cost = cr.get("cost_charged_micro", cr.get("cost_observed_micro"))
            endpoint = cr.get("endpoint_id") or cr.get("tool_name") or ""
            if cr.get("cached"):
                endpoint += " (cached)"
            if cr.get("async_task"):
                endpoint += " (async)"
            self.history_store.append([
                str(cr.get("created_at", ""))[:19].replace("T", " "),
                endpoint,
                str(st),
                usd(cost) if cost is not None else "—",
                cr.get("call_ref") or str(cr.get("id") or ""),
            ])
        self._busy(f"{len(calls)} calls")

    def _on_history_selected(self):
        model, treeiter = self.history_table.get_selection().get_selected()
        if treeiter is None or self.client is None:
            return
        call_id = model[treeiter][4]
        if not call_id:
            return
        self._busy(f"Fetching result of {call_id}…")
        self._spawn(self.client.call_result, call_id,
                    on_ok=self._history_result,
                    on_fail=lambda m: self._error(f"result: {m}"))

    def _history_result(self, data):
        if isinstance(data, dict) and data.get("stored") is False:
            self.history_result.get_buffer().set_text(
                f"no stored copy: {data.get('note', 'the provider or terms forbid keeping the body')}")
            return
        self.history_result.get_buffer().set_text(pretty_json(data))
        self._busy("Result loaded")

    # -- x ------------------------------------------------------------------------

    def _x_mode(self) -> str:
        return ("search", "user", "trends")[self.x_mode.get_active()]

    def _x_mode_changed(self, combo):
        ph = ("search X — keyword, #hashtag, from:user…",
              "@handle — the account's recent posts",
              "country code for trends (default US)")[combo.get_active()]
        self.x_entry.set_placeholder_text(ph)

    def _x_go(self):
        if self.client is None:
            return
        q = self.x_entry.get_text().strip()
        if self._x_mode() == "trends":
            country = q or "US"
            self._busy(f"Trending in {country}…")
            self._spawn(self.client.call, "tikhub.x.twitter-web-fetch-trending", "GET",
                        [("country", country)], None,
                        on_ok=self._x_trends_done,
                        on_fail=lambda m: self._error(f"trends: {m}"))
            return
        if not q:
            return
        if self._x_mode() == "user":
            handle = q.lstrip("@")
            self._busy(f"Posts of @{handle}…")
            self._spawn(self.client.call, "treg.x.user.posts", "POST", None,
                        json.dumps({"username": handle}),
                        on_ok=self._x_posts_done,
                        on_fail=lambda m: self._error(f"posts: {m}"))
        else:
            self._busy(f"Searching X for “{q}”…")
            self._spawn(self.client.call, "treg.x.search.posts", "POST", None,
                        json.dumps({"q": q}),
                        on_ok=self._x_posts_done,
                        on_fail=lambda m: self._error(f"search: {m}"))

    def _x_status(self, n: int, r: httpx.Response, noun: str = "posts"):
        bits = [f"{n} {noun}"]
        if r.headers.get("X-Treg-Cost-Micro") is not None:
            bits.append(f"charged {usd(int(r.headers['X-Treg-Cost-Micro']))}")
        self._busy(" · ".join(bits))

    def _x_posts_done(self, r: httpx.Response):
        try:
            body = r.json()
        except ValueError:
            self._error("X: response was not JSON")
            return
        posts = (body.get("output") or {}).get("posts") or []
        if not posts:
            tried = (body.get("_treg") or {}).get("tried") or []
            note = f" (every provider missed: {len(tried)} tried)" if tried else ""
            self._error(f"no posts found{note}")
            return
        self._x_posts = [_x_norm_post(p) for p in posts if isinstance(p, dict)]
        self._x_fill_posts()
        self._x_status(len(self._x_posts), r)

    def _x_fill_posts(self):
        self.x_store.clear()
        for p in self._x_posts:
            self.x_store.append([
                p["author_name"], p["text"][:120], _x_when(p["created"]),
                _count(p["likes"]), _count(p["retweets"]), _count(p["replies"])])

    def _x_trends_done(self, r: httpx.Response):
        try:
            body = r.json()
        except ValueError:
            self._error("trends: response was not JSON")
            return
        trends = (body.get("data") or {}).get("trends") or []
        trends = [t for t in trends if isinstance(t, dict) and t.get("name")]
        if not trends:
            self._error("no trends returned")
            return
        self._x_trends = trends
        self.x_store.clear()
        for t in trends:
            self.x_store.append([t.get("context") or "Trending", t.get("name"),
                                 "", "", "", ""])
        self._x_status(len(trends), r, noun="trends")

    def _on_x_selected(self):
        model, treeiter = self.x_table.get_selection().get_selected()
        if treeiter is None:
            return
        idx = model.get_path(treeiter).get_indices()[0]
        if self._x_mode() == "trends":
            if idx < len(self._x_trends):
                self.x_entry.set_text(self._x_trends[idx]["name"])
                self.x_mode.set_active(0)
                self._x_go()
            return
        if idx >= len(self._x_posts):
            return
        self._x_selected = self._x_posts[idx]
        self._x_render_detail()

    def _x_render_detail(self):
        p = self._x_selected
        if not p:
            return
        lines = [f"**{p['author_name']}** (@{p['author_handle']})  ·  "
                 f"{_x_when(p['created'])}"]
        lines += ["", p["text"], ""]
        stats = [f"{_count(p['likes'])} likes", f"{_count(p['retweets'])} reposts",
                 f"{_count(p['replies'])} replies"]
        if p["views"]:
            stats.append(f"{_count(p['views'])} views")
        lines.append(" · ".join(stats))
        if p["media"]:
            lines += ["", "**Media**"]
            lines += [f"[{m}]({m})" for m in p["media"]]
        lines += ["", f"[Open on X]({p['url']})"]
        self._render_md(self.x_detail, "\n".join(lines))
        self.x_replies_btn.set_sensitive(bool(p["id"]))
        self.x_profile_btn.set_sensitive(bool(p["author_handle"]))
        self.x_open_btn.set_sensitive(bool(p["url"]))
        self._x_show_image(p)

    def _x_show_image(self, p: dict):
        if not p.get("media"):
            self.x_image.set_from_pixbuf(None)
            return
        url = p["media"][0]
        self._spawn(_x_fetch_image, url,
                    on_ok=lambda data: self._x_image_loaded(data),
                    on_fail=lambda m: self.x_image.set_from_pixbuf(None))

    def _x_image_loaded(self, data):
        if not data:
            self.x_image.set_from_pixbuf(None)
            return
        stream = Gio.MemoryInputStream.new_from_bytes(GLib.Bytes.new(data))
        try:
            pixbuf = GdkPixbuf.Pixbuf.new_from_stream(stream, None)
        except GLib.Error:
            self.x_image.set_from_pixbuf(None)
            return
        w, h = pixbuf.get_width(), pixbuf.get_height()
        if w > 560:
            pixbuf = pixbuf.scale_simple(560, max(1, int(h * 560 / w)),
                                         GdkPixbuf.InterpType.BILINEAR)
        self.x_image.set_from_pixbuf(pixbuf)

    def _x_replies(self):
        p = self._x_selected
        if not p or not p["id"] or self.client is None:
            return
        self._busy(f"Replies to {p['id']}…")
        self._spawn(self.client.call, "treg.x.post.comments", "POST", None,
                    json.dumps({"tweet_id": p["id"]}),
                    on_ok=self._x_replies_done,
                    on_fail=lambda m: self._error(f"replies: {m}"))

    def _x_replies_done(self, r: httpx.Response):
        try:
            body = r.json()
        except ValueError:
            self._error("replies: response was not JSON")
            return
        out = body.get("output") or {}
        comments = out.get("comments")
        if not comments:
            if (body.get("_treg") or {}).get("outcome") == "miss":
                self._error("no replies found — every provider missed")
            else:
                self._error("no replies returned")
            return
        self._x_posts = [_x_norm_post(c) for c in comments if isinstance(c, dict)]
        self._x_fill_posts()
        self._x_status(len(self._x_posts), r, noun="replies")

    def _x_profile(self):
        p = self._x_selected
        if not p or not p["author_handle"] or self.client is None:
            return
        self._busy(f"Profile of @{p['author_handle']}…")
        self._spawn(self.client.call, "treg.x.user.profile", "POST", None,
                    json.dumps({"username": p["author_handle"]}),
                    on_ok=self._x_profile_done,
                    on_fail=lambda m: self._error(f"profile: {m}"))

    def _x_profile_done(self, r: httpx.Response):
        try:
            body = r.json()
        except ValueError:
            self._error("profile: response was not JSON")
            return
        out = body.get("output") or {}
        if not out:
            self._error("no profile returned")
            return
        raw_row = (((body.get("raw") or {}).get("rows")) or [{}])[0]
        name = out.get("name", "")
        handle = out.get("username", "")
        head = f"**{name}** (@{handle})"
        if out.get("is_verified"):
            head += "  ✓"
        lines = [head]
        if out.get("description"):
            lines.append(out["description"])
        lines += ["", f"followers {_count(out.get('followers'))} · "
                    f"following {_count(out.get('following'))} · "
                    f"posts {_count(out.get('posts_count'))}"]
        if raw_row.get("location"):
            lines.append(f"Location: {raw_row['location']}")
        if raw_row.get("created"):
            lines.append(f"Joined: {raw_row['created'][:10]}")
        if raw_row.get("website"):
            lines.append(f"[{raw_row['website']}]({raw_row['website']})")
        if raw_row.get("image"):
            lines += ["", f"[profile picture]({raw_row['image']})"]
        lines += ["", f"[Open @{handle} on X](https://x.com/{handle})"]
        self._render_md(self.x_detail, "\n".join(lines))
        self._x_status(1, r, noun="profile")

    def _x_open(self):
        p = self._x_selected
        if p and p.get("url"):
            try:
                Gio.AppInfo.launch_default_for_uri(p["url"], None)
            except GLib.Error as e:
                self._error(f"could not open {p['url']}: {e.message}")

    # -- shutdown --------------------------------------------------------------------

    def _on_destroy(self, window):
        for w in self._workers:
            w.join(timeout=2.0)
        if self.client is not None:
            self.client.close()


# ---------------------------------------------------------------------------
# entry point
# ---------------------------------------------------------------------------

def _activate(app: Gtk.Application, auto_desktop: bool):
    if app.get_windows():
        return
    if auto_desktop:
        install_desktop_entry()
    MainWindow(application=app).show_all()


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)

    # .desktop management flags (no GUI)
    if "--register-desktop" in argv:
        p = install_desktop_entry()
        print(f"installed: {p}" if p else "failed to install .desktop entry")
        return 0 if p else 1
    if "--unregister-desktop" in argv:
        remove_desktop_entry()
        print("removed .desktop entry")
        return 0

    auto_desktop = "--no-desktop" not in argv

    # The GApplication id associates the window with the .desktop entry
    # (D-Bus), so the desktop environment shows the right icon and groups
    # the window with the app. NON_UNIQUE keeps the old behaviour of
    # allowing several independent instances. prgname sets the WM_CLASS,
    # which panels and task managers use for the same grouping.
    GLib.set_prgname("treg-console")
    app = Gtk.Application(application_id="treg.console",
                          flags=Gio.ApplicationFlags.NON_UNIQUE)
    app.connect("activate", lambda a: _activate(a, auto_desktop))
    return app.run([])


if __name__ == "__main__":
    sys.exit(main())