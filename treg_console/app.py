"""treg console — a native Linux desktop app (GTK3) that uses treg (https://treg.to) as its backend.

Native look: no custom CSS. The app is a plain GTK3 application, so it
follows the desktop's GTK theme, system font and icon theme, and its
GApplication id ("treg.console") associates the window with the .desktop
entry so the desktop environment treats it like any other installed
application.

Tabs:
  Browse   — search the web, click a result, read the page (the simple path)
  AI       — AI interaction: chat with the local model, which can call every
             data source in the app as a tool (treg catalog + any endpoint,
             web, X, Reddit, News, Markets, Hacker News, GitHub, OLX.pt,
             eBay) and answer in Markdown
  Catalog  — search the 4,000+ endpoint catalog, inspect params/price/reliability
  Providers — browse the platform catalog by category, drill into a
             platform's capabilities and endpoints
  Call     — build and fire a call (query params or JSON body), see cost + result
  History  — the team's call audit log, with stored results
  X        — an X (Twitter) frontend: search posts, browse an account's
             timeline, read a post with its media and replies, profiles,
             and trending topics
  Reddit   — a Reddit frontend: search posts, browse a subreddit's or a
             user's posts, read a post with its media, and its comment tree
  Markets  — market data: live stock quotes, daily price history, company
             fundamentals, and crypto. Backed by yfinance (free, direct —
             no treg calls, no charges), with price and margin charts
  News     — a news front page: Top, Tech and Business aggregate free RSS
             feeds (deduped and ranked), Search goes through treg. Read the
             full article or hand the current stories to the AI
  Hacker News — a Hacker News frontend: Top/New/Best/Ask/Show/Jobs from the
             free official API, free Algolia search, free comment trees, and
             an Ask AI / AI digest hand-off
  GitHub   — a GitHub frontend: repository search, trending, repo detail with
             README, issues/PRs and releases via the free public API, with
             Ask AI / AI digest
  OLX      — an OLX.pt frontend: classifieds search with category, price and
             sort filters, or any listing by id or URL, via the free public
             JSON API, with Ask AI / AI digest
  eBay     — an eBay frontend: listing search with category and price order,
             or full listing detail by product id or URL, through treg's
             paid SerpApi eBay endpoints ($0.015 per call), with Ask AI /
             AI digest
  Ctrl+K   — a command palette that jumps to any tab, searches a source, or
             hands the query to the AI
  Alt+<n>  — switch tabs from anywhere: Alt+1…Alt+9, Alt+0 for the tenth,
             then Alt+Shift+1… for later tabs

Credentials come from the treg CLI config (~/.treg/config.json) or TREG_TOKEN.
The AI endpoint is OpenAI-compatible; configure it with AI_URL, AI_API_KEY
and TREG_AI_MODEL (SUMMARY_* still works as a fallback).
"""

from __future__ import annotations

import json
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

import cairo
import httpx

from .ai import (AI_MODEL, AI_URL, run_agent, tool_summary)
from .client import TregClient, TregError, load_credentials, poll_async_task
from .desktop import install_desktop_entry, remove_desktop_entry
from .ebay import (digest_prompt as ebay_digest_prompt,
                   listing as ebay_listing, search as ebay_search)
from .github import (digest_prompt as gh_digest_prompt,
                     issues as gh_issues, releases as gh_releases,
                     repo as gh_repo, repo_slug as gh_repo_slug,
                     search_repos as gh_search, trending as gh_trending)
from .hackernews import (FEED_LABELS as HN_FEED_LABELS,
                         comment_tree as hn_comment_tree,
                         digest_prompt as hn_digest_prompt,
                         feed as hn_feed, search as hn_search)
from .markets import (MAJOR_COINS, yf_coins, yf_crypto_quote, yf_fundamentals,
                      yf_history, yf_quote)
from .news import (TOPIC_LABELS as NEWS_TOPIC_LABELS, digest_prompt,
                   fetch_topic, relative_time as news_when, search_news)
from .olx import (CATEGORIES as OLX_CATEGORIES,
                  digest_prompt as olx_digest_prompt,
                  listing as olx_listing, search as olx_search)
from .parsing import (call_error as _call_error, page_markdown as _page_markdown,
                      pretty_json, reddit_call as _reddit_call,
                      reddit_norm_post as _reddit_norm_post,
                      reddit_posts_of as _reddit_posts_of,
                      reddit_when as _reddit_when,
                      search_results as _search_results,
                      x_norm_post as _x_norm_post, x_when as _x_when)

MICRO = 1_000_000


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


def _count(v) -> str:
    """1234 -> 1.2k, 1234567 -> 1.2m"""
    v = int(v or 0)
    if v >= 1_000_000:
        return f"{v / 1_000_000:.1f}m".replace(".0", "")
    if v >= 1000:
        return f"{v / 1000:.1f}k".replace(".0", "")
    return str(v)


def _fetch_image(url: str) -> bytes | None:
    """Fetch one media image off the GUI thread (None on any failure)."""
    try:
        r = httpx.get(url, timeout=20.0, follow_redirects=True)
        r.raise_for_status()
        return r.content
    except httpx.HTTPError:
        return None


# ---------------------------------------------------------------------------
# Markets tab helpers — yfinance is the backend (markets.py); the tab makes
# no treg calls, so it works without a treg token and never charges
# ---------------------------------------------------------------------------

_M_MARKET_MODES = ("stock", "history", "fundamentals", "crypto")

# News tab: topic views aggregate free RSS; "search" uses treg's routed
# news search. The order matches the combo box.
_NEWS_MODES = ("top", "tech", "business", "search")

# Hacker News tab: the official free API's story lists via hackernews.py;
# "search" uses the free Algolia API. The order matches the combo box.
_HN_MODES = ("top", "new", "best", "ask", "show", "jobs", "search")

# GitHub tab: repositories, trending and issues/releases via github.py.
# The order matches the combo box.
_GH_MODES = ("search", "trending", "repo", "issues", "releases")


def _m_price(v) -> str:
    if v is None:
        return "—"
    sign = "-" if v < 0 else ""
    a = abs(v)
    if a >= 1000:
        return f"{sign}${a:,.0f}"
    if a >= 1:
        return f"{sign}${a:,.2f}"
    return f"{sign}${a:,.4f}"


def _m_big(v) -> str:
    """$4.46t / $340b / $12.3m for big amounts (caps, volumes, revenue)."""
    if v is None:
        return "—"
    a = abs(v)
    if a >= 1e12:
        return f"${v / 1e12:,.1f}t"
    if a >= 1e9:
        return f"${v / 1e9:,.1f}b"
    if a >= 1e6:
        return f"${v / 1e6:,.1f}m"
    return f"${v:,.0f}"


def _m_pct(v) -> str:
    if v is None:
        return "—"
    return f"{v:+.1f}%"


def _m_frac(v) -> str:
    """A fraction (0.45) as a percent (45.0%) — margins, yields, growth."""
    if v is None:
        return "—"
    return f"{v * 100:.1f}%"


def _m_ratio(v) -> str:
    """A plain ratio (P/E, beta, debt/equity) with two decimals."""
    if v is None:
        return "—"
    return f"{v:.2f}"


def _m_quote_worker(symbol: str) -> dict:
    """Live quote + the last 30 daily bars (for the chart)."""
    q = yf_quote(symbol)
    if q is None:
        raise TregError(f"no quote for {symbol} — check the ticker")
    return {"quote": q, "bars": yf_history(symbol, 30)}


def _m_history_worker(symbol: str, days: int) -> list[dict]:
    bars = yf_history(symbol, days)
    if not bars:
        raise TregError(f"no daily bars for {symbol} — check the ticker")
    return bars


def _m_fundamentals_worker(symbol: str) -> dict | None:
    return yf_fundamentals(symbol)


def _m_coins_worker() -> list[dict]:
    rows = yf_coins()
    if not rows:
        raise TregError("no coin data — Yahoo returned nothing")
    return rows


def _m_coin_worker(query: str) -> dict:
    """One coin by ticker (BTC, ETH, …) or by name (bitcoin), plus its last
    90 daily bars for the chart."""
    sym = query.upper().strip()
    if sym.endswith("-USD"):
        sym = sym[:-4]
    q = yf_crypto_quote(sym)
    if q is None:  # the query might be a coin name — match the majors
        for s, name in MAJOR_COINS:
            if query.lower() in (s.lower(), name.lower()):
                q = yf_crypto_quote(s)
                sym = s
                break
    if q is None:
        raise TregError(f"no data for coin {query} — try a ticker like BTC or ETH")
    return {"quote": q, "name": sym, "bars": yf_history(f"{sym}-USD", 90)}


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
# Used by the Browse page view, the AI tab and the detail panes:
# headings, bold/italic, inline code, fenced code blocks, links,
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
# prompts for the Browse tab's AI button
# ---------------------------------------------------------------------------

def _results_block(query: str, rows: list[dict]) -> str:
    """The numbered title/url/snippet list of the search results."""
    lines = [f"Web search results for the query: “{query}”", ""]
    for i, r in enumerate(rows, 1):
        lines.append(f"{i}. {r['title']} — {r['url']}")
        if r["snippet"]:
            lines.append(f"   {r['snippet']}")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# worker thread
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# Charts — plain Cairo DrawingAreas in the widget's theme colors, so the
# app stays dependency-free (no matplotlib / goo-canvas) and native-looking
# ---------------------------------------------------------------------------

def _chart_fg(cr, area: Gtk.DrawingArea):
    """The theme's foreground color as (r, g, b) for cairo drawing."""
    c = area.get_style_context().get_color(Gtk.StateFlags.NORMAL)
    return (c.red, c.green, c.blue)


def _chart_frame(cr, w: int, h: int, rgb):
    """A thin rounded border in a faded fg color (the chart's frame)."""
    cr.set_source_rgba(*rgb, 0.3)
    cr.set_line_width(0.5)
    cr.rectangle(0.5, 0.5, w - 1, h - 1)
    cr.stroke()


def _chart_text(cr, x: float, y: float, text: str, size: float,
                rgb, alpha: float = 1.0, bold: bool = False):
    cr.set_source_rgba(*rgb, alpha)
    cr.select_font_face("Sans", cairo.FONT_SLANT_NORMAL,
                        cairo.FONT_WEIGHT_BOLD if bold else cairo.FONT_WEIGHT_NORMAL)
    cr.set_font_size(size)
    cr.move_to(x, y)
    cr.show_text(text)


class PriceChart(Gtk.DrawingArea):
    """A closing-price line chart with a light area fill, grid lines and
    axis labels. set_data(dates, values) then queue_draw; values may have
    None gaps."""

    def __init__(self, height: int = 200):
        super().__init__()
        self.set_size_request(-1, height)
        self._dates: list[str] = []
        self._values: list[float | None] = []
        self.connect("draw", self._on_draw)

    def set_data(self, dates: list[str], values: list[float | None]):
        self._dates = list(dates)
        self._values = list(values)
        self.queue_draw()

    @staticmethod
    def _date_label(s: str) -> str:
        try:
            return time.strftime("%b %d", time.strptime(s[:10], "%Y-%m-%d"))
        except ValueError:
            return s[:10]

    def _on_draw(self, _widget, cr):
        w, h = self.get_allocated_width(), self.get_allocated_height()
        rgb = _chart_fg(cr, self)
        _chart_frame(cr, w, h, rgb)
        pts = [(d, v) for d, v in zip(self._dates, self._values) if v is not None]
        if len(pts) < 2:
            _chart_text(cr, 12, h / 2, "not enough data for a chart", 10, rgb, 0.6)
            return
        left, right, top, bottom = 52, 10, 10, 20
        x0, x1 = left, w - right
        y0, y1 = top, h - bottom
        vals = [v for _, v in pts]
        vmin, vmax = min(vals), max(vals)
        if vmax == vmin:
            vmax, vmin = vmax * 1.01 or 1, vmin * 0.99 or 0
        pad = (vmax - vmin) * 0.08
        vmin, vmax = vmin - pad, vmax + pad
        n = len(pts)

        def px(i: int) -> float:
            return x0 + i * (x1 - x0) / (n - 1)

        def py(v: float) -> float:
            return y1 - (v - vmin) * (y1 - y0) / (vmax - vmin)

        # grid: 4 horizontal lines with price labels, 3 vertical with dates
        cr.set_source_rgba(*rgb, 0.18)
        cr.set_line_width(0.5)
        for i in range(4):
            y = y0 + i * (y1 - y0) / 3
            cr.move_to(x0, y)
            cr.line_to(x1, y)
            cr.stroke()
            _chart_text(cr, x0 - 6 - cr.text_extents(_m_price(vmax - i * (vmax - vmin) / 3)).width,
                        y + 3, _m_price(vmax - i * (vmax - vmin) / 3), 9, rgb, 0.7)
        for i in (0, (n - 1) // 2, n - 1):
            x = px(i)
            cr.move_to(x, y0)
            cr.line_to(x, y1)
            cr.stroke()
            label = self._date_label(pts[i][0])
            ext = cr.text_extents(label)
            tx = x - ext.width / 2 if i == (n - 1) // 2 else (x if i == 0 else x - ext.width)
            _chart_text(cr, tx, y1 + 12, label, 9, rgb, 0.7)

        # area fill under the line
        cr.move_to(px(0), y1)
        for i, (_, v) in enumerate(pts):
            cr.line_to(px(i), py(v))
        cr.line_to(px(n - 1), y1)
        cr.close_path()
        cr.set_source_rgba(*rgb, 0.08)
        cr.fill()

        # the line
        cr.move_to(px(0), py(pts[0][1]))
        for i, (_, v) in enumerate(pts[1:], 1):
            cr.line_to(px(i), py(v))
        cr.set_source_rgba(*rgb, 0.9)
        cr.set_line_width(1.5)
        cr.stroke()

        # last point + its price
        lx, ly = px(n - 1), py(pts[-1][1])
        cr.arc(lx, ly, 2.5, 0, 2 * 3.14159265)
        cr.set_source_rgba(*rgb, 1.0)
        cr.fill()
        label = _m_price(pts[-1][1])
        ext = cr.text_extents(label)
        tx = lx - ext.width - 6 if lx - ext.width - 6 > x0 else lx + 6
        _chart_text(cr, tx, ly - 6, label, 9, rgb, 1.0, bold=True)


class BarChart(Gtk.DrawingArea):
    """A column chart of a few labeled percentages (the fundamentals
    margins). set_bars([(label, value), ...]) then queue_draw."""

    def __init__(self, height: int = 150):
        super().__init__()
        self.set_size_request(-1, height)
        self._bars: list[tuple[str, float]] = []
        self.connect("draw", self._on_draw)

    def set_bars(self, bars: list[tuple[str, float]]):
        self._bars = list(bars)
        self.queue_draw()

    def _on_draw(self, _widget, cr):
        w, h = self.get_allocated_width(), self.get_allocated_height()
        rgb = _chart_fg(cr, self)
        _chart_frame(cr, w, h, rgb)
        if not self._bars:
            _chart_text(cr, 12, h / 2, "no data for a chart", 10, rgb, 0.6)
            return
        top, bottom = 22, 24
        plot_h = h - top - bottom
        maxv = max((v for _, v in self._bars), default=0)
        if maxv <= 0:
            maxv = 1
        slot = w / len(self._bars)
        bar_w = min(slot * 0.5, 64)
        # baseline
        cr.set_source_rgba(*rgb, 0.3)
        cr.set_line_width(0.5)
        cr.move_to(0, top + plot_h + 0.5)
        cr.line_to(w, top + plot_h + 0.5)
        cr.stroke()
        for i, (label, v) in enumerate(self._bars):
            cx = slot * i + slot / 2
            bh = max(0.0, plot_h * v / maxv)
            x, y = cx - bar_w / 2, top + plot_h - bh
            cr.set_source_rgba(*rgb, 0.7)
            cr.rectangle(x, y, bar_w, bh)
            cr.fill()
            val = f"{v:.1f}%"
            ext = cr.text_extents(val)
            _chart_text(cr, cx - ext.width / 2, y - 6, val, 10, rgb, 1.0, bold=True)
            ext = cr.text_extents(label)
            _chart_text(cr, cx - ext.width / 2, h - 13, label, 9, rgb, 0.8)


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
    AI_HINT = ("**AI interaction** — ask anything. I can use every data source "
               "in the app as a tool: the treg catalog and any endpoint, web "
               "search and pages, X, Reddit, News, Markets, Hacker News, "
               "GitHub, OLX.pt and eBay.\n\n"
               "Try: *“what's moving in the market today and why?”*, "
               "*“summarize the top tech news”*, *“what's hot on Hacker News "
               "right now?”*, *“compare Rust web frameworks on GitHub”*, "
               "*“find an iPhone 13 under 300 € on OLX”*, "
               "*“find a used iPhone 13 on eBay and compare it with OLX”*, "
               "*“find an endpoint for backlinks and call it for "
               "example.com”*.")

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
        self._r_posts: list[dict] = []
        self._r_selected: dict | None = None
        self._m_bars: list[dict] = []
        self._m_coins: list[dict] = []
        self._news_items: list[dict] = []
        self._news_selected: dict | None = None
        self._news_label = "News"
        self._hn_items: list[dict] = []
        self._hn_selected: dict | None = None
        self._hn_label = "Hacker News"
        self._gh_items: list[dict] = []
        self._gh_selected: dict | None = None
        self._gh_label = "GitHub"
        self._gh_readme = ""
        self._olx_items: list[dict] = []
        self._olx_selected: dict | None = None
        self._olx_result: dict = {}
        self._olx_label = "OLX.pt"
        self._ebay_items: list[dict] = []
        self._ebay_selected: dict | None = None
        self._ebay_result: dict = {}
        self._ebay_label = "eBay"
        self._palette: Gtk.Dialog | None = None
        self._palette_rows: list[tuple[str, str, str, object]] = []
        self._md_links: dict[Gtk.TextView, list[tuple[int, int, str]]] = {}
        self._ai_messages: list[dict] = []
        self._ai_busy = False

        self._build_ui()
        self.connect("key-press-event", self._on_window_key)
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
        palette_btn = Gtk.Button(label="Search / ask…  Ctrl+K")
        palette_btn.set_tooltip_text(
            "Command palette: jump to a tab, search a source, or ask the AI "
            "(Ctrl+K)")
        palette_btn.connect("clicked", lambda b: self._open_palette())
        top.pack_start(palette_btn, False, False, 0)
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
        self.tabs.set_tooltip_text(
            "Switch tabs with Alt+1…Alt+0 (Alt+Shift+1… for the last tabs)")
        self._add_tab(self._build_browse_tab(), "Browse")
        self._add_tab(self._build_ai_tab(), "AI")
        self._add_tab(self._build_catalog_tab(), "Catalog")
        self._add_tab(self._build_providers_tab(), "Providers")
        self._add_tab(self._build_call_tab(), "Call")
        self._add_tab(self._build_history_tab(), "History")
        self._add_tab(self._build_x_tab(), "X")
        self._add_tab(self._build_reddit_tab(), "Reddit")
        self._add_tab(self._build_markets_tab(), "Markets")
        self._add_tab(self._build_news_tab(), "News")
        self._add_tab(self._build_hn_tab(), "Hacker News")
        self._add_tab(self._build_gh_tab(), "GitHub")
        self._add_tab(self._build_olx_tab(), "OLX")
        self._add_tab(self._build_ebay_tab(), "eBay")
        self.tabs.set_current_page(0)
        root.pack_start(self.tabs, True, True, 0)

        self.statusbar = Gtk.Statusbar()
        self._status_ctx = self.statusbar.get_context_id("main")
        root.pack_start(self.statusbar, False, False, 0)

    def _add_tab(self, widget: Gtk.Widget, label: str) -> None:
        """Append a tab whose label advertises its Alt+<n> shortcut."""
        tab_label = Gtk.Label(label=label)
        shortcut = self._tab_shortcut(self.tabs.get_n_pages())
        if shortcut:
            tab_label.set_tooltip_text(f"{label} — {shortcut}")
        self.tabs.append_page(widget, tab_label)

    @staticmethod
    def _tab_shortcut(index: int) -> str:
        """The keyboard shortcut for a tab index: Alt+1…Alt+9, Alt+0, then
        Alt+Shift+1… for the tabs after the tenth."""
        if index < 0:
            return ""
        if index < 10:
            return f"Alt+{(index + 1) % 10}"
        return f"Alt+Shift+{(index - 10 + 1) % 10}"

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
        self.browse_summarize_btn = Gtk.Button(label="Ask AI")
        self.browse_summarize_btn.set_tooltip_text(
            f"Hand the current results to the AI ({AI_MODEL}) — it can read "
            "pages and call tools")
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

    def _build_ai_tab(self) -> Gtk.Widget:
        """AI interaction: a chat with the local model, which can call every
        data source in the app as a tool (treg catalog/endpoints, web, X,
        Reddit, News, Markets) and answers in Markdown. Tools show up inline
        as they run."""
        w = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)

        head = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        title = Gtk.Label()
        title.set_markup(f"<b>AI interaction</b> — {_esc(AI_MODEL)}")
        title.set_xalign(0.0)
        tools = Gtk.Label()
        tools.set_markup(f"<i>{_esc(tool_summary())} · {_esc(AI_URL)}</i>")
        tools.set_xalign(0.0)
        tools.set_ellipsize(Pango.EllipsizeMode.END)
        self.ai_new_btn = Gtk.Button(label="New chat")
        self.ai_new_btn.set_tooltip_text("Start a fresh conversation")
        self.ai_new_btn.connect("clicked", lambda b: self._ai_new_chat())
        head.pack_start(title, False, False, 0)
        head.pack_start(tools, True, True, 0)
        head.pack_end(self.ai_new_btn, False, False, 0)
        w.pack_start(head, False, False, 0)

        chat = Gtk.Frame()
        self.ai_view = self._textview()
        self.ai_view.connect("button-press-event", self._md_link_click)
        self.ai_scroll = self._scrolled(self.ai_view)
        chat.add(self.ai_scroll)
        w.pack_start(chat, True, True, 0)

        self.ai_status = Gtk.Label(label="ready")
        self.ai_status.set_xalign(0.0)
        self.ai_status.set_ellipsize(Pango.EllipsizeMode.END)
        w.pack_start(self.ai_status, False, False, 0)

        row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        self.ai_entry = Gtk.Entry()
        self.ai_entry.set_placeholder_text(
            "Ask anything — search, read, call tools, check news and markets…")
        self.ai_entry.connect("activate", lambda e: self._ai_send())
        self.ai_send_btn = Gtk.Button(label="Send")
        self.ai_send_btn.connect("clicked", lambda b: self._ai_send())
        row.pack_start(self.ai_entry, True, True, 0)
        row.pack_start(self.ai_send_btn, False, False, 0)
        w.pack_start(row, False, False, 0)

        self._render_md(self.ai_view, self.AI_HINT)
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
        self.x_ask_btn = Gtk.Button(label="Ask AI")
        self.x_ask_btn.set_tooltip_text(
            "Ask the AI about this post — it can load the thread and profile")
        self.x_ask_btn.set_sensitive(False)
        self.x_ask_btn.connect("clicked", lambda b: self._x_ask_ai())
        btn_row.pack_start(self.x_replies_btn, False, False, 0)
        btn_row.pack_start(self.x_profile_btn, False, False, 0)
        btn_row.pack_start(self.x_open_btn, False, False, 0)
        btn_row.pack_start(self.x_ask_btn, False, False, 0)
        w.pack_start(btn_row, False, False, 0)
        return w

    def _build_reddit_tab(self) -> Gtk.Widget:
        w = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)

        row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        self.r_mode = Gtk.ComboBoxText()
        for label in ("Search", "Subreddit", "User"):
            self.r_mode.append_text(label)
        self.r_mode.set_active(0)
        self.r_mode.connect("changed", self._r_mode_changed)
        self.r_entry = Gtk.Entry()
        self.r_entry.set_placeholder_text("search Reddit — keyword or phrase…")
        self.r_entry.connect("activate", lambda e: self._r_go())
        go = Gtk.Button(label="Go")
        go.connect("clicked", lambda b: self._r_go())
        row.pack_start(self.r_mode, False, False, 0)
        row.pack_start(self.r_entry, True, True, 0)
        row.pack_start(go, False, False, 0)
        w.pack_start(row, False, False, 0)

        paned = Gtk.Paned.new(Gtk.Orientation.HORIZONTAL)

        self.r_store = Gtk.ListStore(str, str, str, str, str, str)
        self.r_table = Gtk.TreeView(model=self.r_store)
        for header, col_idx in (("Author", 0), ("Title", 1), ("Subreddit", 2),
                                ("Time", 3), ("Score", 4), ("Comments", 5)):
            self.r_table.append_column(
                Gtk.TreeViewColumn(header, Gtk.CellRendererText(), text=col_idx))
        self.r_table.get_selection().connect("changed", lambda s: self._on_r_selected())
        paned.pack1(self._scrolled(self.r_table), True, True)

        right = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        self.r_image = Gtk.Image()
        right.pack_start(self.r_image, False, False, 0)
        self.r_detail = self._textview()
        self.r_detail.connect("button-press-event", self._md_link_click)
        right.pack_start(self._scrolled(self.r_detail), True, True, 0)
        paned.pack2(right, True, True)
        paned.set_position(560)
        w.pack_start(paned, True, True, 0)

        btn_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        self.r_comments_btn = Gtk.Button(label="Comments")
        self.r_comments_btn.set_sensitive(False)
        self.r_comments_btn.connect("clicked", lambda b: self._r_comments())
        self.r_profile_btn = Gtk.Button(label="Profile")
        self.r_profile_btn.set_sensitive(False)
        self.r_profile_btn.connect("clicked", lambda b: self._r_profile())
        self.r_open_btn = Gtk.Button(label="Open on Reddit")
        self.r_open_btn.set_sensitive(False)
        self.r_open_btn.connect("clicked", lambda b: self._r_open())
        self.r_ask_btn = Gtk.Button(label="Ask AI")
        self.r_ask_btn.set_tooltip_text(
            "Ask the AI about this post — it can read the comments and profile")
        self.r_ask_btn.set_sensitive(False)
        self.r_ask_btn.connect("clicked", lambda b: self._r_ask_ai())
        btn_row.pack_start(self.r_comments_btn, False, False, 0)
        btn_row.pack_start(self.r_profile_btn, False, False, 0)
        btn_row.pack_start(self.r_open_btn, False, False, 0)
        btn_row.pack_start(self.r_ask_btn, False, False, 0)
        w.pack_start(btn_row, False, False, 0)
        return w

    def _build_markets_tab(self) -> Gtk.Widget:
        """Market data, backed by yfinance (no treg calls, no charges):
        live stock quotes with a 30-day chart, daily price history, company
        fundamentals with a margins chart, and crypto (major coins or one
        coin with its 90-day chart)."""
        w = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)

        row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        self.m_mode = Gtk.ComboBoxText()
        for label in ("Stock", "History", "Fundamentals", "Crypto"):
            self.m_mode.append_text(label)
        self.m_mode.set_active(0)
        self.m_mode.connect("changed", self._m_mode_changed)
        self.m_entry = Gtk.Entry()
        self.m_entry.set_placeholder_text("ticker — e.g. AAPL, MSFT, BRK-B…")
        self.m_entry.connect("activate", lambda e: self._m_go())
        self.m_days = Gtk.SpinButton.new_with_range(5, 500, 1)
        self.m_days.set_value(30)
        self.m_days.set_size_request(64, -1)
        self.m_days.set_tooltip_text("days of daily bars")
        go = Gtk.Button(label="Go")
        go.connect("clicked", lambda b: self._m_go())
        row.pack_start(self.m_mode, False, False, 0)
        row.pack_start(self.m_entry, True, True, 0)
        row.pack_start(self.m_days, False, False, 0)
        row.pack_start(go, False, False, 0)
        w.pack_start(row, False, False, 0)

        paned = Gtk.Paned.new(Gtk.Orientation.HORIZONTAL)
        self.m_store = Gtk.ListStore(str, str, str, str, str, str)
        self.m_table = Gtk.TreeView(model=self.m_store)
        self._m_build_columns()
        self.m_table.get_selection().connect("changed", lambda s: self._on_m_selected())
        paned.pack1(self._scrolled(self.m_table), True, True)

        right = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        self.m_chart = PriceChart(height=200)
        right.pack_start(self.m_chart, False, False, 0)
        self.m_barchart = BarChart(height=140)
        self.m_barchart.set_visible(False)
        right.pack_start(self.m_barchart, False, False, 0)
        self.m_detail = self._textview()
        self.m_detail.connect("button-press-event", self._md_link_click)
        right.pack_start(self._scrolled(self.m_detail), True, True, 0)
        paned.pack2(right, True, True)
        paned.set_position(560)
        w.pack_start(paned, True, True, 0)

        btn_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        self.m_history_btn = Gtk.Button(label="History")
        self.m_history_btn.set_tooltip_text("daily bars for the current ticker")
        self.m_history_btn.connect("clicked", lambda b: self._m_jump("history"))
        self.m_fundamentals_btn = Gtk.Button(label="Fundamentals")
        self.m_fundamentals_btn.set_tooltip_text("profile and metrics for the current ticker")
        self.m_fundamentals_btn.connect("clicked", lambda b: self._m_jump("fundamentals"))
        self.m_ask_btn = Gtk.Button(label="Ask AI")
        self.m_ask_btn.set_tooltip_text(
            "Ask the AI about what is on screen — it can pull news, more "
            "history and crypto")
        self.m_ask_btn.set_sensitive(False)
        self.m_ask_btn.connect("clicked", lambda b: self._m_ask_ai())
        btn_row.pack_start(self.m_history_btn, False, False, 0)
        btn_row.pack_start(self.m_fundamentals_btn, False, False, 0)
        btn_row.pack_start(self.m_ask_btn, False, False, 0)
        w.pack_start(btn_row, False, False, 0)
        return w

    def _build_news_tab(self) -> Gtk.Widget:
        """A news front page: Top/Tech/Business aggregate free RSS feeds
        (deduped and ranked, no treg calls); Search uses treg's routed news
        search. A story opens in the pane on the right, where it can be read
        in full (treg web extract), handed to the AI, or the whole list
        digested by the AI."""
        w = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)

        row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        self.news_mode = Gtk.ComboBoxText()
        for label in ("Top", "Tech", "Business", "Search"):
            self.news_mode.append_text(label)
        self.news_mode.set_active(0)
        self.news_mode.connect("changed", self._news_mode_changed)
        self.news_entry = Gtk.Entry()
        self.news_entry.set_placeholder_text("Top stories — no query needed")
        self.news_entry.set_sensitive(False)
        self.news_entry.connect("activate", lambda e: self._news_go())
        self.news_limit = Gtk.SpinButton.new_with_range(5, 100, 5)
        self.news_limit.set_value(25)
        self.news_limit.set_size_request(64, -1)
        self.news_limit.set_tooltip_text("stories to show")
        go = Gtk.Button(label="Go")
        go.connect("clicked", lambda b: self._news_go())
        row.pack_start(self.news_mode, False, False, 0)
        row.pack_start(self.news_entry, True, True, 0)
        row.pack_start(self.news_limit, False, False, 0)
        row.pack_start(go, False, False, 0)
        w.pack_start(row, False, False, 0)

        paned = Gtk.Paned.new(Gtk.Orientation.HORIZONTAL)

        self.news_store = Gtk.ListStore(str, str, str, str)
        self.news_table = Gtk.TreeView(model=self.news_store)
        for header, col_idx in (("Source", 0), ("Headline", 1),
                                ("Time", 2), ("Also", 3)):
            col = Gtk.TreeViewColumn(header, Gtk.CellRendererText(), text=col_idx)
            if col_idx == 1:
                col.set_expand(True)
            self.news_table.append_column(col)
        self.news_table.get_selection().connect(
            "changed", lambda s: self._on_news_selected())
        paned.pack1(self._scrolled(self.news_table), True, True)

        right = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        self.news_image = Gtk.Image()
        # show_all() must not reveal the empty image; show it when one loads
        self.news_image.set_no_show_all(True)
        right.pack_start(self.news_image, False, False, 0)
        self.news_detail = self._textview()
        self.news_detail.connect("button-press-event", self._md_link_click)
        right.pack_start(self._scrolled(self.news_detail), True, True, 0)
        paned.pack2(right, True, True)
        paned.set_position(620)
        w.pack_start(paned, True, True, 0)

        btn_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        self.news_read_btn = Gtk.Button(label="Read article")
        self.news_read_btn.set_tooltip_text(
            "fetch the full article with treg's web extract")
        self.news_read_btn.set_sensitive(False)
        self.news_read_btn.connect("clicked", lambda b: self._news_read())
        self.news_open_btn = Gtk.Button(label="Open in browser")
        self.news_open_btn.set_sensitive(False)
        self.news_open_btn.connect("clicked", lambda b: self._news_open())
        self.news_digest_btn = Gtk.Button(label="AI digest")
        self.news_digest_btn.set_tooltip_text(
            f"Summarize the current stories in the AI tab ({AI_MODEL})")
        self.news_digest_btn.set_sensitive(False)
        self.news_digest_btn.connect("clicked", lambda b: self._news_digest())
        self.news_ask_btn = Gtk.Button(label="Ask AI")
        self.news_ask_btn.set_tooltip_text(
            "Ask the AI about the selected story — it can read the article "
            "and search for more context")
        self.news_ask_btn.set_sensitive(False)
        self.news_ask_btn.connect("clicked", lambda b: self._news_ask_ai())
        btn_row.pack_start(self.news_read_btn, False, False, 0)
        btn_row.pack_start(self.news_open_btn, False, False, 0)
        btn_row.pack_start(self.news_digest_btn, False, False, 0)
        btn_row.pack_start(self.news_ask_btn, False, False, 0)
        w.pack_start(btn_row, False, False, 0)
        self._render_md(
            self.news_detail,
            "*Choose **Top**, **Tech**, **Business** or **Search**, then press "
            "**Go**. The topic views aggregate free feeds; search uses treg.*")
        return w

    def _build_hn_tab(self) -> Gtk.Widget:
        """A Hacker News frontend: Top/New/Best/Ask/Show/Jobs come straight
        from the free official API (no treg calls, no charges), Search uses
        the free Algolia API, and the comment tree is free too. A story opens
        on the right, where it can be read in full (treg web extract), handed
        to the AI, or the whole list digested by the AI."""
        w = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)

        row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        self.hn_mode = Gtk.ComboBoxText()
        for label in ("Top", "New", "Best", "Ask HN", "Show HN", "Jobs",
                      "Search"):
            self.hn_mode.append_text(label)
        self.hn_mode.set_active(0)
        self.hn_mode.connect("changed", self._hn_mode_changed)
        self.hn_entry = Gtk.Entry()
        self.hn_entry.set_placeholder_text("Top stories — no query needed")
        self.hn_entry.set_sensitive(False)
        self.hn_entry.connect("activate", lambda e: self._hn_go())
        self.hn_limit = Gtk.SpinButton.new_with_range(5, 90, 5)
        self.hn_limit.set_value(30)
        self.hn_limit.set_size_request(64, -1)
        self.hn_limit.set_tooltip_text("stories to show")
        go = Gtk.Button(label="Go")
        go.connect("clicked", lambda b: self._hn_go())
        row.pack_start(self.hn_mode, False, False, 0)
        row.pack_start(self.hn_entry, True, True, 0)
        row.pack_start(self.hn_limit, False, False, 0)
        row.pack_start(go, False, False, 0)
        w.pack_start(row, False, False, 0)

        paned = Gtk.Paned.new(Gtk.Orientation.HORIZONTAL)

        self.hn_store = Gtk.ListStore(str, str, str)
        self.hn_table = Gtk.TreeView(model=self.hn_store)
        for header, col_idx in (("Title", 0), ("Points", 1), ("Comments", 2)):
            col = Gtk.TreeViewColumn(header, Gtk.CellRendererText(), text=col_idx)
            if col_idx == 0:
                col.set_expand(True)
            self.hn_table.append_column(col)
        self.hn_table.get_selection().connect(
            "changed", lambda s: self._on_hn_selected())
        paned.pack1(self._scrolled(self.hn_table), True, True)

        self.hn_detail = self._textview()
        self.hn_detail.connect("button-press-event", self._md_link_click)
        paned.pack2(self._scrolled(self.hn_detail), True, True)
        paned.set_position(620)
        w.pack_start(paned, True, True, 0)

        btn_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        self.hn_read_btn = Gtk.Button(label="Read article")
        self.hn_read_btn.set_tooltip_text(
            "fetch the full article with treg's web extract")
        self.hn_read_btn.set_sensitive(False)
        self.hn_read_btn.connect("clicked", lambda b: self._hn_read())
        self.hn_open_btn = Gtk.Button(label="Open link")
        self.hn_open_btn.set_sensitive(False)
        self.hn_open_btn.connect("clicked", lambda b: self._hn_open())
        self.hn_comments_btn = Gtk.Button(label="Comments")
        self.hn_comments_btn.set_tooltip_text(
            "load the comment tree from the free Hacker News API")
        self.hn_comments_btn.set_sensitive(False)
        self.hn_comments_btn.connect("clicked", lambda b: self._hn_comments())
        self.hn_digest_btn = Gtk.Button(label="AI digest")
        self.hn_digest_btn.set_tooltip_text(
            f"Summarize the current stories in the AI tab ({AI_MODEL})")
        self.hn_digest_btn.set_sensitive(False)
        self.hn_digest_btn.connect("clicked", lambda b: self._hn_digest())
        self.hn_ask_btn = Gtk.Button(label="Ask AI")
        self.hn_ask_btn.set_tooltip_text(
            "Ask the AI about the selected story — it can read the link and "
            "the HN discussion and look for more context")
        self.hn_ask_btn.set_sensitive(False)
        self.hn_ask_btn.connect("clicked", lambda b: self._hn_ask_ai())
        btn_row.pack_start(self.hn_read_btn, False, False, 0)
        btn_row.pack_start(self.hn_open_btn, False, False, 0)
        btn_row.pack_start(self.hn_comments_btn, False, False, 0)
        btn_row.pack_start(self.hn_digest_btn, False, False, 0)
        btn_row.pack_start(self.hn_ask_btn, False, False, 0)
        w.pack_start(btn_row, False, False, 0)
        self._render_md(
            self.hn_detail,
            "*Pick **Top**, **New**, **Best**, **Ask HN**, **Show HN**, "
            "**Jobs** or **Search**, then press **Go**. Everything here is "
            "free — the official Hacker News API, no treg calls.*")
        return w

    def _build_gh_tab(self) -> Gtk.Widget:
        """A GitHub frontend backed by the free public API: repository
        search, trending (recent repos by stars), repo detail with README,
        issues/PRs and releases. No treg calls, no charges. Set GITHUB_TOKEN
        to lift the anonymous rate limit. Ask AI / AI digest hand the current
        view to the AI tab."""
        w = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)

        row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        self.gh_mode = Gtk.ComboBoxText()
        for label in ("Search", "Trending", "Repo", "Issues", "Releases"):
            self.gh_mode.append_text(label)
        self.gh_mode.set_active(0)
        self.gh_mode.connect("changed", self._gh_mode_changed)
        self.gh_entry = Gtk.Entry()
        self.gh_entry.set_placeholder_text(
            "search repositories — e.g. “static site generator”")
        self.gh_entry.connect("activate", lambda e: self._gh_go())
        self.gh_limit = Gtk.SpinButton.new_with_range(5, 100, 5)
        self.gh_limit.set_value(25)
        self.gh_limit.set_size_request(64, -1)
        self.gh_limit.set_tooltip_text("rows to show")
        go = Gtk.Button(label="Go")
        go.connect("clicked", lambda b: self._gh_go())
        row.pack_start(self.gh_mode, False, False, 0)
        row.pack_start(self.gh_entry, True, True, 0)
        row.pack_start(self.gh_limit, False, False, 0)
        row.pack_start(go, False, False, 0)
        w.pack_start(row, False, False, 0)

        paned = Gtk.Paned.new(Gtk.Orientation.HORIZONTAL)

        self.gh_store = Gtk.ListStore(str, str)
        self.gh_table = Gtk.TreeView(model=self.gh_store)
        for header, col_idx in (("Repository / issue", 0), ("Details", 1)):
            col = Gtk.TreeViewColumn(header, Gtk.CellRendererText(), text=col_idx)
            if col_idx == 0:
                col.set_expand(True)
            self.gh_table.append_column(col)
        self.gh_table.get_selection().connect(
            "changed", lambda s: self._on_gh_selected())
        paned.pack1(self._scrolled(self.gh_table), True, True)

        self.gh_detail = self._textview()
        self.gh_detail.connect("button-press-event", self._md_link_click)
        paned.pack2(self._scrolled(self.gh_detail), True, True)
        paned.set_position(620)
        w.pack_start(paned, True, True, 0)

        btn_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        self.gh_open_btn = Gtk.Button(label="Open on GitHub")
        self.gh_open_btn.set_sensitive(False)
        self.gh_open_btn.connect("clicked", lambda b: self._gh_open())
        self.gh_digest_btn = Gtk.Button(label="AI digest")
        self.gh_digest_btn.set_tooltip_text(
            f"Summarize the current results in the AI tab ({AI_MODEL})")
        self.gh_digest_btn.set_sensitive(False)
        self.gh_digest_btn.connect("clicked", lambda b: self._gh_digest())
        self.gh_ask_btn = Gtk.Button(label="Ask AI")
        self.gh_ask_btn.set_tooltip_text(
            "Ask the AI about the selected repo or issue — it can read the "
            "README and issues and look for more context")
        self.gh_ask_btn.set_sensitive(False)
        self.gh_ask_btn.connect("clicked", lambda b: self._gh_ask_ai())
        btn_row.pack_start(self.gh_open_btn, False, False, 0)
        btn_row.pack_start(self.gh_digest_btn, False, False, 0)
        btn_row.pack_start(self.gh_ask_btn, False, False, 0)
        w.pack_start(btn_row, False, False, 0)
        self._render_md(
            self.gh_detail,
            "*Search repositories, load **Trending**, or enter a repo as "
            "**owner/name** for **Repo**, **Issues** or **Releases**. Free — "
            "the GitHub public API, no treg calls.*")
        return w

    def _build_olx_tab(self) -> Gtk.Widget:
        """An OLX.pt frontend backed by the site's free public JSON API:
        search classifieds by keyword, category and price, or open a listing
        by id or URL. No treg calls. Ask AI and AI digest hand the current
        view to the AI tab, which can also compare listings with olx_search."""
        w = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)

        row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        self.olx_mode = Gtk.ComboBoxText()
        self.olx_mode.append("search", "Search")
        self.olx_mode.append("listing", "Listing")
        self.olx_mode.set_active_id("search")
        self.olx_mode.connect("changed", self._olx_mode_changed)
        self.olx_entry = Gtk.Entry()
        self.olx_entry.set_placeholder_text(
            "search OLX.pt — leave empty for the latest listings")
        self.olx_entry.connect("activate", lambda e: self._olx_go())
        self.olx_limit = Gtk.SpinButton.new_with_range(5, 50, 5)
        self.olx_limit.set_value(25)
        self.olx_limit.set_size_request(64, -1)
        self.olx_limit.set_tooltip_text("listings to show (OLX's maximum is 50)")
        go = Gtk.Button(label="Go")
        go.connect("clicked", lambda b: self._olx_go())
        row.pack_start(self.olx_mode, False, False, 0)
        row.pack_start(self.olx_entry, True, True, 0)
        row.pack_start(self.olx_limit, False, False, 0)
        row.pack_start(go, False, False, 0)
        w.pack_start(row, False, False, 0)

        filters = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        self.olx_category = Gtk.ComboBoxText()
        for name, cid, _slug in OLX_CATEGORIES:
            self.olx_category.append("" if cid is None else str(cid), name)
        self.olx_category.set_active(0)
        self.olx_category.set_tooltip_text("category filter")
        self.olx_sort = Gtk.ComboBoxText()
        self.olx_sort.append("relevance", "Relevance")
        self.olx_sort.append("cheapest", "Cheapest")
        self.olx_sort.append("expensive", "Most expensive")
        self.olx_sort.set_active_id("relevance")
        self.olx_sort.set_tooltip_text("result order")
        self.olx_price_min = Gtk.SpinButton.new_with_range(0, 10_000_000, 50)
        self.olx_price_min.set_size_request(92, -1)
        self.olx_price_min.set_tooltip_text("minimum price in € (0 = any)")
        self.olx_price_max = Gtk.SpinButton.new_with_range(0, 10_000_000, 50)
        self.olx_price_max.set_size_request(92, -1)
        self.olx_price_max.set_tooltip_text("maximum price in € (0 = any)")
        filters.pack_start(self.olx_category, False, False, 0)
        filters.pack_start(self.olx_sort, False, False, 0)
        filters.pack_start(Gtk.Label(label="price"), False, False, 0)
        filters.pack_start(self.olx_price_min, False, False, 0)
        filters.pack_start(Gtk.Label(label="–"), False, False, 0)
        filters.pack_start(self.olx_price_max, False, False, 0)
        filters.pack_start(Gtk.Label(label="€"), False, False, 0)
        self.olx_filters = filters
        w.pack_start(filters, False, False, 0)

        paned = Gtk.Paned.new(Gtk.Orientation.HORIZONTAL)
        self.olx_store = Gtk.ListStore(str, str)
        self.olx_table = Gtk.TreeView(model=self.olx_store)
        for header, col_idx in (("Listing", 0), ("Price · place", 1)):
            col = Gtk.TreeViewColumn(header, Gtk.CellRendererText(),
                                     text=col_idx)
            if col_idx == 0:
                col.set_expand(True)
            self.olx_table.append_column(col)
        self.olx_table.get_selection().connect(
            "changed", lambda s: self._on_olx_selected())
        paned.pack1(self._scrolled(self.olx_table), True, True)
        self.olx_detail = self._textview()
        self.olx_detail.connect("button-press-event", self._md_link_click)
        paned.pack2(self._scrolled(self.olx_detail), True, True)
        paned.set_position(620)
        w.pack_start(paned, True, True, 0)

        btn_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        self.olx_open_btn = Gtk.Button(label="Open on OLX")
        self.olx_open_btn.set_sensitive(False)
        self.olx_open_btn.connect("clicked", lambda b: self._olx_open())
        self.olx_digest_btn = Gtk.Button(label="AI digest")
        self.olx_digest_btn.set_tooltip_text(
            f"Summarize the current listings in the AI tab ({AI_MODEL})")
        self.olx_digest_btn.set_sensitive(False)
        self.olx_digest_btn.connect("clicked", lambda b: self._olx_digest())
        self.olx_ask_btn = Gtk.Button(label="Ask AI")
        self.olx_ask_btn.set_tooltip_text(
            "Ask the AI about the selected listing — it can compare similar "
            "listings and judge the price")
        self.olx_ask_btn.set_sensitive(False)
        self.olx_ask_btn.connect("clicked", lambda b: self._olx_ask_ai())
        btn_row.pack_start(self.olx_open_btn, False, False, 0)
        btn_row.pack_start(self.olx_digest_btn, False, False, 0)
        btn_row.pack_start(self.olx_ask_btn, False, False, 0)
        w.pack_start(btn_row, False, False, 0)

        self._render_md(
            self.olx_detail,
            "*Search OLX.pt by keyword, or choose a category and leave the "
            "query empty to browse it. Filter by price, sort, then press "
            "**Go**. Free — OLX's public JSON API, no treg calls.*")
        return w

    def _build_ebay_tab(self) -> Gtk.Widget:
        """An eBay frontend routed through treg's SerpApi endpoints: listing
        search by keyword/category, or full listing detail by product id or
        URL. Paid — search and detail are $0.015 per successful call. Ask AI
        and AI digest hand the current view to the AI tab, which can also
        search eBay with ebay_search."""
        w = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)

        row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        self.ebay_mode = Gtk.ComboBoxText()
        self.ebay_mode.append("search", "Search")
        self.ebay_mode.append("listing", "Listing detail")
        self.ebay_mode.set_active_id("search")
        self.ebay_mode.connect("changed", self._ebay_mode_changed)
        self.ebay_entry = Gtk.Entry()
        self.ebay_entry.set_placeholder_text(
            "search eBay.com — e.g. “iphone 13”")
        self.ebay_entry.connect("activate", lambda e: self._ebay_go())
        self.ebay_limit = Gtk.SpinButton.new_with_range(5, 50, 5)
        self.ebay_limit.set_value(25)
        self.ebay_limit.set_size_request(64, -1)
        self.ebay_limit.set_tooltip_text(
            "listings to show (the search fetch is about 60 rows)")
        go = Gtk.Button(label="Go")
        go.set_tooltip_text("Search eBay (paid: $0.015 per successful call)")
        go.connect("clicked", lambda b: self._ebay_go())
        row.pack_start(self.ebay_mode, False, False, 0)
        row.pack_start(self.ebay_entry, True, True, 0)
        row.pack_start(self.ebay_limit, False, False, 0)
        row.pack_start(go, False, False, 0)
        w.pack_start(row, False, False, 0)

        filters = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        self.ebay_category = Gtk.ComboBoxText()
        self.ebay_category.append("", "All categories")
        self.ebay_category.set_active(0)
        self.ebay_category.set_tooltip_text(
            "category filter — filled with the categories eBay offered for "
            "the last search")
        self.ebay_sort = Gtk.ComboBoxText()
        self.ebay_sort.append("relevance", "Relevance")
        self.ebay_sort.append("cheapest", "Cheapest")
        self.ebay_sort.append("expensive", "Most expensive")
        self.ebay_sort.set_active_id("relevance")
        self.ebay_sort.set_tooltip_text(
            "price order, applied to the fetched page")
        filters.pack_start(self.ebay_category, True, True, 0)
        filters.pack_start(self.ebay_sort, False, False, 0)
        self.ebay_filters = filters
        w.pack_start(filters, False, False, 0)

        paned = Gtk.Paned.new(Gtk.Orientation.HORIZONTAL)
        self.ebay_store = Gtk.ListStore(str, str)
        self.ebay_table = Gtk.TreeView(model=self.ebay_store)
        for header, col_idx in (("Listing", 0), ("Price · condition", 1)):
            col = Gtk.TreeViewColumn(header, Gtk.CellRendererText(),
                                     text=col_idx)
            if col_idx == 0:
                col.set_expand(True)
            self.ebay_table.append_column(col)
        self.ebay_table.get_selection().connect(
            "changed", lambda s: self._on_ebay_selected())
        paned.pack1(self._scrolled(self.ebay_table), True, True)
        self.ebay_detail = self._textview()
        self.ebay_detail.connect("button-press-event", self._md_link_click)
        paned.pack2(self._scrolled(self.ebay_detail), True, True)
        paned.set_position(620)
        w.pack_start(paned, True, True, 0)

        btn_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        self.ebay_open_btn = Gtk.Button(label="Open on eBay")
        self.ebay_open_btn.set_sensitive(False)
        self.ebay_open_btn.connect("clicked", lambda b: self._ebay_open())
        self.ebay_full_btn = Gtk.Button(label="Full listing ($0.015)")
        self.ebay_full_btn.set_tooltip_text(
            "Fetch the full listing detail through treg — paid $0.015 per "
            "successful call")
        self.ebay_full_btn.set_sensitive(False)
        self.ebay_full_btn.connect("clicked", lambda b: self._ebay_full())
        self.ebay_digest_btn = Gtk.Button(label="AI digest")
        self.ebay_digest_btn.set_tooltip_text(
            f"Summarize the current eBay listings in the AI tab ({AI_MODEL})")
        self.ebay_digest_btn.set_sensitive(False)
        self.ebay_digest_btn.connect("clicked", lambda b: self._ebay_digest())
        self.ebay_ask_btn = Gtk.Button(label="Ask AI")
        self.ebay_ask_btn.set_tooltip_text(
            "Ask the AI about the selected listing — it can compare similar "
            "listings with ebay_search (paid)")
        self.ebay_ask_btn.set_sensitive(False)
        self.ebay_ask_btn.connect("clicked", lambda b: self._ebay_ask_ai())
        btn_row.pack_start(self.ebay_open_btn, False, False, 0)
        btn_row.pack_start(self.ebay_full_btn, False, False, 0)
        btn_row.pack_start(self.ebay_digest_btn, False, False, 0)
        btn_row.pack_start(self.ebay_ask_btn, False, False, 0)
        w.pack_start(btn_row, False, False, 0)

        self._render_md(
            self.ebay_detail,
            "*Search eBay.com by keyword or browse a category, then press "
            "**Go**. Search is paid through treg — $0.015 per successful "
            "call; **Full listing** is a second $0.015 call. Select a row for "
            "its price, condition, shipping, seller and returns.*")
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

    @staticmethod
    def _scroll_bottom(sw: Gtk.ScrolledWindow) -> None:
        adj = sw.get_vadjustment()
        if adj is not None:
            adj.set_value(adj.get_upper())

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

    # -- AI interaction ---------------------------------------------------------

    def _ai_new_chat(self):
        self._ai_messages = []
        self._ai_busy = False
        self.ai_send_btn.set_sensitive(True)
        self.ai_entry.set_sensitive(True)
        self.ai_status.set_text("ready")
        self._render_md(self.ai_view, self.AI_HINT)
        self._busy("New AI chat")
        self.ai_entry.grab_focus()

    def _ai_ask(self, prompt: str, context: str | None = None,
                display: str | None = None):
        """Send a turn to the AI tab (optionally with hidden context)."""
        self.tabs.set_current_page(1)
        self.ai_entry.set_text("")
        content = prompt if not context else f"{prompt}\n\n{context}"
        self._ai_submit(content, display or prompt)

    def _ai_send(self):
        text = self.ai_entry.get_text().strip()
        if not text:
            return
        self.ai_entry.set_text("")
        self._ai_submit(text)

    def _ai_submit(self, content: str, display: str | None = None):
        if self._ai_busy:
            self._error("the AI is still answering — wait for it to finish")
            return
        if not self._ai_messages and self._text(self.ai_view) == self.AI_HINT:
            self.ai_view.get_buffer().set_text("")
        self._append_ai_turn("You", display or content)
        self._ai_messages.append({"role": "user", "content": content})
        self._ai_busy = True
        self.ai_send_btn.set_sensitive(False)
        self.ai_entry.set_sensitive(False)
        self.ai_status.set_text("thinking…")
        self._busy(f"AI: thinking with {AI_MODEL}…")
        history = [dict(m) for m in self._ai_messages]
        self._spawn(run_agent, self.client, history, self._ai_progress,
                    on_ok=self._ai_done, on_fail=self._ai_failed)

    def _append_ai_turn(self, who: str, text: str):
        buf = self.ai_view.get_buffer()
        if buf.get_char_count():
            buf.insert(buf.get_end_iter(), "\n\n")
        self._insert_md(self.ai_view, buf.get_end_iter(), f"**{who} —** {text}\n")
        self._scroll_bottom(self.ai_scroll)

    def _append_ai_tool(self, line: str):
        buf = self.ai_view.get_buffer()
        end = buf.get_end_iter()
        if buf.get_char_count() and not self._text_ends_with_newline():
            buf.insert(end, "\n")
            end = buf.get_end_iter()
        buf.insert_markup(end, f"<i>{_esc(line)}</i>\n", -1)
        self._scroll_bottom(self.ai_scroll)

    def _text_ends_with_newline(self) -> bool:
        buf = self.ai_view.get_buffer()
        return buf.get_char_count() > 0 and buf.get_text(
            buf.get_iter_at_offset(buf.get_char_count() - 1),
            buf.get_end_iter(), True) == "\n"

    def _ai_progress(self, kind: str, data):
        """Worker-thread progress → main loop."""
        GLib.idle_add(self._ai_event, kind, data)

    def _ai_event(self, kind: str, data) -> bool:
        if kind == "status":
            self.ai_status.set_text(str(data))
            self._busy(f"AI: {data}")
        elif kind == "tool":
            name = str(data.get("name") or "tool")
            args = data.get("arguments") or {}
            shown = ", ".join(f"{k}={json.dumps(v, ensure_ascii=False)[:60]}"
                              for k, v in args.items())
            self._append_ai_tool(f"→ {name}({shown})")
            self.ai_status.set_text(f"running {name}…")
        elif kind == "tool_result":
            preview = str(data.get("result") or "").replace("\n", " ")[:140]
            self._append_ai_tool(f"← {preview}")
        return False

    def _ai_done(self, out: dict):
        self._ai_busy = False
        self.ai_send_btn.set_sensitive(True)
        self.ai_entry.set_sensitive(True)
        self.ai_status.set_text("ready")
        self._ai_messages = out.get("messages") or self._ai_messages
        self._append_ai_turn("AI", out.get("text") or
                             "(the model returned an empty answer)")
        self._busy(f"AI answered ({AI_MODEL})")
        self.ai_entry.grab_focus()

    def _ai_failed(self, msg: str):
        self._ai_busy = False
        self.ai_send_btn.set_sensitive(True)
        self.ai_entry.set_sensitive(True)
        self.ai_status.set_text("failed")
        self._append_ai_tool(f"← (failed) {msg}")
        self._error(f"AI: {msg}")

    def _do_browse_summarize(self):
        rows = getattr(self, "_browse_rows", None)
        if not rows:
            self._error("run a search first, then press Ask AI")
            return
        q = self._browse_last_query
        self._ai_ask(
            f"Summarize what these web search results say about “{q}” in "
            "3–5 sentences of Markdown. Cover the points they agree and "
            "disagree on; do not just list URLs. Use read_page if an "
            "important result needs more detail.",
            _results_block(q, rows))
        self._busy(f"AI is reading the results for “{q}”…")

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
        self.x_ask_btn.set_sensitive(True)
        self._x_show_image(p)

    def _x_show_image(self, p: dict):
        if not p.get("media"):
            self.x_image.set_from_pixbuf(None)
            return
        url = p["media"][0]
        self._spawn(_fetch_image, url,
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

    def _x_ask_ai(self):
        p = self._x_selected
        if not p:
            self._error("select a post first")
            return
        context = (
            "X post:\n"
            f"Author: {p.get('author_name', '')} (@{p.get('author_handle', '')})\n"
            f"Posted: {_x_when(p.get('created'))}\n"
            f"Text: {p.get('text', '')}\n"
            f"Stats: {_count(p.get('likes'))} likes, "
            f"{_count(p.get('retweets'))} reposts, "
            f"{_count(p.get('replies'))} replies\n"
            f"URL: {p.get('url', '')}"
        )
        self._ai_ask(
            "Explain this X post: the claim, the context, who the author is, "
            "and how it is being received. Load the replies or profile if "
            "that helps.",
            context, display=f"Explain this post: {p.get('text', '')[:80]}")

    # -- Reddit tab ---------------------------------------------------------------

    def _r_mode(self) -> str:
        return ("search", "subreddit", "user")[self.r_mode.get_active()]

    def _r_mode_changed(self, combo):
        ph = ("search Reddit — keyword or phrase…",
              "r/name — the subreddit's posts (r/ optional)",
              "u/name — the user's posts (u/ optional)")[combo.get_active()]
        self.r_entry.set_placeholder_text(ph)

    def _r_go(self):
        if self.client is None:
            return
        q = self.r_entry.get_text().strip()
        if not q:
            return
        if self._r_mode() == "subreddit":
            name = q[2:] if q.startswith("r/") else q
            self._busy(f"Posts of r/{name}…")
            self._spawn(_reddit_call, self.client, "scrapecreators.reddit.subreddit.posts",
                        "GET", [("subreddit", name)], None,
                        on_ok=self._r_posts_done,
                        on_fail=lambda m: self._error(f"subreddit: {m}"))
        elif self._r_mode() == "user":
            handle = q[2:] if q.startswith("u/") else q
            self._busy(f"Posts of u/{handle}…")
            self._spawn(_reddit_call, self.client, "tikhub.x.reddit-app-fetch-user-posts",
                        "GET", [("username", handle)], None,
                        on_ok=self._r_posts_done,
                        on_fail=lambda m: self._error(f"posts: {m}"))
        else:
            self._busy(f"Searching Reddit for “{q}”…")
            # scrapecreators' search is the stable one; tikhub's dynamic
            # search fails intermittently with 400 "please retry".
            self._spawn(_reddit_call, self.client, "scrapecreators.reddit.search.posts",
                        "GET", [("query", q)], None,
                        on_ok=self._r_posts_done,
                        on_fail=lambda m: self._error(f"search: {m}"))

    def _r_status(self, n: int, r: httpx.Response, noun: str = "posts"):
        bits = [f"{n} {noun}"]
        if r.headers.get("X-Treg-Cost-Micro") is not None:
            bits.append(f"charged {usd(int(r.headers['X-Treg-Cost-Micro']))}")
        self._busy(" · ".join(bits))

    def _r_posts_done(self, r: httpx.Response):
        if err := _call_error(r):
            self._error(f"Reddit: {err}")
            return
        try:
            body = r.json()
        except ValueError:
            self._error("Reddit: response was not JSON")
            return
        posts = _reddit_posts_of(body)
        if not posts:
            tried = ((body.get("_treg") or {}) if isinstance(body, dict) else {}).get("tried") or []
            note = f" (every provider missed: {len(tried)} tried)" if tried else ""
            self._error(f"no posts found{note}")
            return
        self._r_posts = [_reddit_norm_post(p) for p in posts]
        self._r_fill_posts()
        self._r_status(len(self._r_posts), r)

    def _r_fill_posts(self):
        self.r_store.clear()
        for p in self._r_posts:
            self.r_store.append([
                p["author"], p["title"][:120], p["subreddit"], _reddit_when(p["created"]),
                _count(p["score"]), _count(p["comments"])])

    def _on_r_selected(self):
        model, treeiter = self.r_table.get_selection().get_selected()
        if treeiter is None:
            return
        idx = model.get_path(treeiter).get_indices()[0]
        if idx >= len(self._r_posts):
            return
        self._r_selected = self._r_posts[idx]
        # The feed row already carries the full post, so show it at once;
        # the detail call then enriches it (Markdown body, media, flair).
        self._r_render_detail()
        self._r_load_detail()

    def _r_load_detail(self):
        p = self._r_selected
        if not p or not p["id"] or self.client is None:
            return
        self._busy(f"Loading {p['id']}…")
        self._spawn(_reddit_call, self.client, "treg.reddit.post.detail", "POST", None,
                    json.dumps({"post_id": p["id"]}),
                    on_ok=self._r_detail_done,
                    on_fail=lambda m: self._error(f"full post: {m} (feed data shown)"))

    def _r_detail_done(self, r: httpx.Response):
        if err := _call_error(r):
            self._error(f"full post: {err} (feed data shown)")
            return
        try:
            body = r.json()
        except ValueError:
            self._error("full post: response was not JSON (feed data shown)")
            return
        out = body.get("output") if isinstance(body, dict) else None
        raw = out.get("post") if isinstance(out, dict) else None
        if not isinstance(raw, dict) or not raw.get("id"):
            self._error("full post: no post returned (feed data shown)")
            return
        p = _reddit_norm_post(raw)
        # A newer selection may have come in while this call was in flight.
        if self._r_selected and self._r_selected.get("id") == p["id"]:
            self._r_selected = p
            self._r_render_detail()
        self._r_status(1, r, noun="post")

    def _r_render_detail(self):
        p = self._r_selected
        if not p:
            return
        lines = [f"**{p['title']}**"]
        meta = [f"u/{p['author']}" if p["author"] else "unknown author"]
        if p["subreddit"]:
            meta.append(p["subreddit"])
        if p["created"]:
            meta.append(_reddit_when(p["created"]))
        if p["flair"]:
            meta.append(f"({p['flair']})")
        lines.append("  ·  ".join(meta))
        lines += [""]
        if p["body"]:
            lines += [p["body"], ""]
        stats = [f"{_count(p['score'])} score", f"{_count(p['comments'])} comments"]
        lines.append(" · ".join(stats))
        lines += ["", f"[Open on Reddit]({p['url']})"]
        self._render_md(self.r_detail, "\n".join(lines))
        self.r_comments_btn.set_sensitive(bool(p["id"]))
        self.r_profile_btn.set_sensitive(bool(p["author"]))
        self.r_open_btn.set_sensitive(bool(p["url"]))
        self.r_ask_btn.set_sensitive(True)
        self._r_show_image(p)

    def _r_show_image(self, p: dict):
        if not p.get("image"):
            self.r_image.set_from_pixbuf(None)
            return
        self._spawn(_fetch_image, p["image"],
                    on_ok=lambda data: self._r_image_loaded(data),
                    on_fail=lambda m: self.r_image.set_from_pixbuf(None))

    def _r_image_loaded(self, data):
        if not data:
            self.r_image.set_from_pixbuf(None)
            return
        stream = Gio.MemoryInputStream.new_from_bytes(GLib.Bytes.new(data))
        try:
            pixbuf = GdkPixbuf.Pixbuf.new_from_stream(stream, None)
        except GLib.Error:
            self.r_image.set_from_pixbuf(None)
            return
        w, h = pixbuf.get_width(), pixbuf.get_height()
        if w > 560:
            pixbuf = pixbuf.scale_simple(560, max(1, int(h * 560 / w)),
                                         GdkPixbuf.InterpType.BILINEAR)
        self.r_image.set_from_pixbuf(pixbuf)

    def _r_comments(self):
        p = self._r_selected
        if not p or not p["id"] or self.client is None:
            return
        self._busy(f"Comments for {p['id']}…")
        self._spawn(_reddit_call, self.client, "tikhub.x.reddit-app-fetch-post-comments",
                    "GET", [("post_id", p["id"])], None,
                    on_ok=self._r_comments_done,
                    on_fail=lambda m: self._error(f"comments: {m}"))

    def _r_comments_done(self, r: httpx.Response):
        if err := _call_error(r):
            self._error(f"comments: {err}")
            return
        try:
            body = r.json()
        except ValueError:
            self._error("comments: response was not JSON")
            return
        data = body.get("data") if isinstance(body, dict) else None
        forest = ((data or {}).get("postInfoById") or {}).get("commentForest") or {}
        trees = [t for t in (forest.get("trees") or [])
                 if isinstance(t, dict) and isinstance(t.get("node"), dict)]
        if not trees:
            self._error("no comments returned")
            return
        lines = [f"**Comments** ({len(trees)})", ""]
        for t in trees:
            node = t["node"]
            depth = min(int(t.get("depth") or 0), 5)
            author = ((node.get("authorInfo") or {}).get("name")) or ""
            score = node.get("score") or 0
            lines.append(("↳ " * depth) + f"**u/{author}**  ({_count(score)})")
            md = ((node.get("content") or {}).get("markdown")) or ""
            if md:
                lines += [ln for ln in md.splitlines()]
            lines.append("")
        self._render_md(self.r_detail, "\n".join(lines))
        self._r_status(len(trees), r, noun="comments")

    def _r_profile(self):
        p = self._r_selected
        if not p or not p["author"] or self.client is None:
            return
        self._busy(f"Profile of u/{p['author']}…")
        self._spawn(_reddit_call, self.client, "tikhub.x.reddit-app-fetch-user-profile",
                    "GET", [("username", p["author"])], None,
                    on_ok=self._r_profile_done,
                    on_fail=lambda m: self._error(f"profile: {m}"))

    def _r_profile_done(self, r: httpx.Response):
        if err := _call_error(r):
            self._error(f"profile: {err}")
            return
        try:
            body = r.json()
        except ValueError:
            self._error("profile: response was not JSON")
            return
        user = ((body.get("data") if isinstance(body, dict) else None) or {}).get("redditorInfoByName") or {}
        if not user.get("name"):
            self._error("no profile returned")
            return
        prof = user.get("profile") or {}
        head = user.get("prefixedName") or user["name"]
        if user.get("isVerified"):
            head += "  ✓"
        lines = [f"**{head}**"]
        if prof.get("title"):
            lines.append(prof["title"])
        if prof.get("publicDescriptionText"):
            lines.append(prof["publicDescriptionText"])
        if prof.get("createdAt"):
            lines += ["", f"Joined: {_reddit_when(prof['createdAt'])}"]
        links = [l for l in (prof.get("socialLinks") or [])
                 if isinstance(l, dict) and l.get("outboundUrl")]
        if links:
            lines += ["", "**Links**"]
            lines += [f"[{l.get('title') or l['outboundUrl']}]({l['outboundUrl']})"
                      for l in links]
        icon = user.get("newIcon") or {}
        if icon.get("url"):
            lines += ["", f"[profile picture]({icon['url']})"]
        lines += ["", f"[Open u/{user['name']} on Reddit]"
                  f"(https://www.reddit.com/user/{user['name']}/)"]
        self._render_md(self.r_detail, "\n".join(lines))
        self._r_status(1, r, noun="profile")

    def _r_open(self):
        p = self._r_selected
        if p and p.get("url"):
            try:
                Gio.AppInfo.launch_default_for_uri(p["url"], None)
            except GLib.Error as e:
                self._error(f"could not open {p['url']}: {e.message}")

    def _r_ask_ai(self):
        p = self._r_selected
        if not p:
            self._error("select a post first")
            return
        context = (
            "Reddit post:\n"
            f"Title: {p.get('title', '')}\n"
            f"Subreddit: {p.get('subreddit', '')}\n"
            f"Author: u/{p.get('author', '')}\n"
            f"Posted: {_reddit_when(p.get('created'))}\n"
            f"Score: {_count(p.get('score'))} · comments: {_count(p.get('comments'))}\n"
            f"Body: {p.get('body', '')}\n"
            f"URL: {p.get('url', '')}"
        )
        self._ai_ask(
            "Explain this Reddit post: what is being discussed, the community "
            "reaction, and any useful details. Load the comments if it helps.",
            context, display=f"Explain this post: {p.get('title', '')[:80]}")

    # -- Markets tab ---------------------------------------------------------------

    def _m_mode(self) -> str:
        return _M_MARKET_MODES[self.m_mode.get_active()]

    def _m_mode_changed(self, combo):
        ph = ("ticker — e.g. AAPL, MSFT, BRK-B…",
              "ticker for daily bars — e.g. AAPL",
              "ticker for profile and metrics — e.g. AAPL",
              "coin — BTC, ETH… (blank = major coins)")[combo.get_active()]
        self.m_entry.set_placeholder_text(ph)
        self.m_days.set_sensitive(combo.get_active() == 1)
        self.m_store.clear()
        self._m_build_columns()
        self.m_chart.set_data([], [])
        self.m_barchart.set_bars([])
        self.m_detail.get_buffer().set_text("")
        self._m_sync_visibility()

    def _m_build_columns(self):
        for c in list(self.m_table.get_columns()):
            self.m_table.remove_column(c)
        headers = {
            "stock": ("Field", "Value"),
            "history": ("Date", "Open", "High", "Low", "Close", "Volume"),
            "fundamentals": ("Metric", "Value"),
            "crypto": ("Coin", "Price", "24h", "7d", "Volume"),
        }[self._m_mode()]
        for i, h in enumerate(headers):
            self.m_table.append_column(
                Gtk.TreeViewColumn(h, Gtk.CellRendererText(), text=i))

    def _m_sync_visibility(self):
        fund = self._m_mode() == "fundamentals"
        self.m_barchart.set_visible(fund)
        self.m_chart.set_visible(not fund)

    def _m_jump(self, mode: str):
        self.m_mode.set_active(_M_MARKET_MODES.index(mode))
        self._m_go()

    def _m_go(self):
        mode = self._m_mode()
        q = self.m_entry.get_text().strip()
        if mode in ("stock", "history", "fundamentals"):
            q = q.upper()
            if not q:
                return
        if mode == "stock":
            self._busy(f"Quote for {q}…")
            self._spawn(_m_quote_worker, q,
                        on_ok=self._m_quote_done,
                        on_fail=lambda m: self._error(f"quote: {m}"))
        elif mode == "history":
            days = int(self.m_days.get_value())
            self._busy(f"Daily bars for {q} ({days} days)…")
            self._spawn(_m_history_worker, q, days,
                        on_ok=self._m_history_done,
                        on_fail=lambda m: self._error(f"bars: {m}"))
        elif mode == "fundamentals":
            self._busy(f"Fundamentals for {q}…")
            self._spawn(_m_fundamentals_worker, q,
                        on_ok=self._m_fundamentals_done,
                        on_fail=lambda m: self._error(f"fundamentals: {m}"))
        elif not q:
            self._busy("Major coins — latest prices…")
            self._spawn(_m_coins_worker,
                        on_ok=self._m_coins_done,
                        on_fail=lambda m: self._error(f"coins: {m}"))
        else:
            self._busy(f"Coin {q}…")
            self._spawn(_m_coin_worker, q,
                        on_ok=self._m_coin_done,
                        on_fail=lambda m: self._error(f"coin: {m}"))

    def _m_status(self, n: int, noun: str):
        self.m_ask_btn.set_sensitive(True)
        self._busy(f"{n} {noun} · via yfinance (direct, free)")

    def _m_fill_quote(self, q: dict):
        self.m_store.clear()
        rows = [
            ("Price", _m_price(q.get("price"))),
            ("Change (24h)", f"{_m_price(q['change'])} ({_m_pct(q['change_pct'])})"
             if q.get("change") is not None and q.get("change_pct") is not None else "—"),
            ("Open", _m_price(q.get("open"))),
            ("Day high", _m_price(q.get("day_high"))),
            ("Day low", _m_price(q.get("day_low"))),
            ("Previous close", _m_price(q.get("previous_close"))),
            ("Volume", _count(q["volume"]) if q.get("volume") is not None else "—"),
            ("Market cap", _m_big(q.get("market_cap"))),
            ("52-week high", _m_price(q.get("year_high"))),
            ("52-week low", _m_price(q.get("year_low"))),
            ("50-day avg", _m_price(q.get("fifty_day_avg"))),
            ("200-day avg", _m_price(q.get("two_hundred_day_avg"))),
        ]
        for k, v in rows:
            if v != "—":
                self.m_store.append([k, v, "", "", "", ""])

    def _m_quote_done(self, out: dict):
        q = out["quote"]
        self._m_fill_quote(q)
        self.m_chart.set_data([b["date"] for b in out["bars"]],
                               [b["close"] for b in out["bars"]])
        self._m_sync_visibility()
        sym = q.get("symbol") or self.m_entry.get_text().strip()
        lines = [f"**{sym}**", ""]
        bits = [b for b in (q.get("quote_type"), q.get("exchange"),
                            q.get("currency")) if b]
        if bits:
            lines.append(" · ".join(_esc(b) for b in bits))
            lines.append("")
        lines += ["**30-day close** — chart above",
                  "", f"[Open on Yahoo Finance](https://finance.yahoo.com/quote/{sym})"]
        self._render_md(self.m_detail, "\n".join(lines))
        self._m_status(1, "quote")

    def _m_history_done(self, bars: list):
        self._m_bars = bars
        self.m_store.clear()
        for b in bars:
            self.m_store.append([b["date"], _m_price(b["open"]), _m_price(b["high"]),
                                 _m_price(b["low"]), _m_price(b["close"]),
                                 _count(b["volume"]) if b["volume"] is not None else "—"])
        self.m_chart.set_data([b["date"] for b in bars],
                               [b["close"] for b in bars])
        self._m_sync_visibility()
        self._m_history_detail(None)
        self._m_status(len(bars), "bars")

    def _m_history_detail(self, b: dict | None):
        bars = self._m_bars
        sym = self.m_entry.get_text().strip()
        lines = [f"**{sym}** — {len(bars)} daily bars", ""]
        closes = [x["close"] for x in bars if x["close"] is not None]
        if len(closes) >= 2:
            first, last = closes[0], closes[-1]
            lines.append(f"Close {bars[0]['date']} → {bars[-1]['date']}: "
                         f"{_m_price(first)} → {_m_price(last)}  "
                         f"(**{(last - first) / first * 100:+.1f}%**)")
        highs = [x["high"] for x in bars if x["high"] is not None]
        lows = [x["low"] for x in bars if x["low"] is not None]
        vols = [x["volume"] for x in bars if x["volume"] is not None]
        bits = []
        if highs:
            bits.append(f"Period high: {_m_price(max(highs))}")
        if lows:
            bits.append(f"Period low: {_m_price(min(lows))}")
        if vols:
            bits.append(f"Avg volume: {_count(sum(vols) / len(vols))}")
        if bits:
            lines.append("  ·  ".join(bits))
        if b:
            lines += ["", "**Selected**",
                      f"{b['date']}  ·  O {_m_price(b['open'])}  ·  H {_m_price(b['high'])}  "
                      f"·  L {_m_price(b['low'])}  ·  C {_m_price(b['close'])}  ·  "
                      f"Vol {_count(b['volume']) if b['volume'] is not None else '—'}"]
        lines += ["", f"[Open on Yahoo Finance](https://finance.yahoo.com/quote/{sym})"]
        self._render_md(self.m_detail, "\n".join(lines))

    def _m_fundamentals_done(self, met: dict):
        sym = self.m_entry.get_text().strip()
        self.m_store.clear()
        rows = [
            ("P/E (trailing)", _m_ratio(met.get("trailing_pe"))),
            ("P/E (forward)", _m_ratio(met.get("forward_pe"))),
            ("EPS (ttm)", _m_ratio(met.get("eps"))),
            ("Revenue", _m_big(met.get("revenue"))),
            ("Revenue growth", _m_pct(met["revenue_growth"] * 100)
             if met.get("revenue_growth") is not None else "—"),
            ("Gross margin", _m_frac(met.get("gross_margin"))),
            ("Operating margin", _m_frac(met.get("operating_margin"))),
            ("Profit margin", _m_frac(met.get("profit_margin"))),
            ("ROE", _m_frac(met.get("roe"))),
            ("Debt / equity", _m_ratio(met.get("debt_to_equity"))),
            ("Dividend yield", _m_frac(met.get("dividend_yield"))),
            ("Beta", _m_ratio(met.get("beta"))),
            ("52-week change", _m_pct(met.get("week52_change"))),
            ("Employees", _count(met["employees"])
             if met.get("employees") is not None else "—"),
        ]
        for k, v in rows:
            if v != "—":
                self.m_store.append([k, v, "", "", "", ""])
        bars = [(label, met[key] * 100) for label, key in
                (("Gross margin", "gross_margin"),
                 ("Operating margin", "operating_margin"),
                 ("Profit margin", "profit_margin"))
                if met.get(key) is not None]
        self.m_barchart.set_bars(bars)
        self._m_sync_visibility()
        name = met.get("name") or sym
        lines = [f"**{_esc(name)}**  ({sym})", ""]
        bits = [b for b in (met.get("sector"), met.get("industry"),
                            met.get("country")) if b]
        if bits:
            lines.append("  ·  ".join(_esc(b) for b in bits))
            lines.append("")
        if met.get("market_cap") is not None:
            lines.append(f"Market cap: {_m_big(met['market_cap'])}")
            lines.append("")
        if met.get("website"):
            lines += [f"[{met['website']}]({met['website']})", ""]
        lines += ["**Margins** — chart above, full metrics in the table",
                  "", f"[Open on Yahoo Finance](https://finance.yahoo.com/quote/{sym})"]
        self._render_md(self.m_detail, "\n".join(lines))
        self._m_status(1, "profile")

    def _m_coins_done(self, rows: list):
        self._m_coins = rows
        self.m_store.clear()
        for c in rows:
            self.m_store.append([c["name"], _m_price(c["price"]),
                                 _m_pct(c["change_24h"]), _m_pct(c["change_7d"]),
                                 _count(c["volume"]) if c["volume"] is not None else "—", ""])
        self.m_chart.set_data([], [])
        self.m_chart.set_visible(False)  # the list has no series to chart
        self._render_md(self.m_detail,
                        "**Major coins** — latest prices (one batched Yahoo call).\n\n"
                        "Select a coin for its 90-day chart.")
        self._m_status(len(rows), "coins")

    def _m_coin_done(self, out: dict):
        q = out["quote"]
        name = out.get("name") or q["symbol"]
        self._m_coins = []
        self.m_store.clear()
        self.m_store.append([name, _m_price(q["price"]),
                             _m_pct(q["change_pct"]), "",
                             _count(q["volume"]) if q.get("volume") is not None else "—", ""])
        self.m_chart.set_data([b["date"] for b in out["bars"]],
                               [b["close"] for b in out["bars"]])
        self._m_sync_visibility()
        lines = [f"**{name}**", ""]
        bits = [f"{_m_price(q['price'])}  ·  {_m_pct(q.get('change_pct'))} (24h)"]
        if q.get("market_cap") is not None:
            bits.append(f"Market cap: {_m_big(q['market_cap'])}")
        if q.get("year_high") is not None and q.get("year_low") is not None:
            bits.append(f"52-week: {_m_price(q['year_low'])} – {_m_price(q['year_high'])}")
        lines.append("  ·  ".join(bits))
        lines += ["", "**90-day close** — chart above",
                  "", f"[Open on Yahoo Finance](https://finance.yahoo.com/quote/{q['symbol']})"]
        self._render_md(self.m_detail, "\n".join(lines))
        self._m_status(1, "coin")

    def _on_m_selected(self):
        model, treeiter = self.m_table.get_selection().get_selected()
        if treeiter is None:
            return
        idx = model.get_path(treeiter).get_indices()[0]
        mode = self._m_mode()
        if mode == "history" and idx < len(self._m_bars):
            self._m_history_detail(self._m_bars[idx])
        elif mode == "crypto" and idx < len(self._m_coins):
            self._m_load_coin(self._m_coins[idx])

    def _m_load_coin(self, row: dict):
        self._busy(f"90-day chart for {row['name']}…")
        self._spawn(_m_coin_worker, row["symbol"],
                    on_ok=self._m_coin_done,
                    on_fail=lambda m: self._error(f"coin: {m}"))

    def _m_ask_ai(self):
        mode = self._m_mode()
        sym = self.m_entry.get_text().strip().upper()
        rows = []
        for row in self.m_store:
            cells = [str(c) for c in row if str(c)]
            if cells:
                rows.append(" | ".join(cells))
        context = (
            f"Markets tab — {mode} view"
            + (f" for {sym}" if sym else "")
            + ":\n"
            + "\n".join(rows[:40])
            + "\n\n"
            + self._text(self.m_detail)
        )
        if not rows:
            self._error("load a quote, history or fundamentals first")
            return
        self._ai_ask(
            "Analyze what is on screen: the key numbers, the trend, and what "
            "they mean. Pull fundamentals, more history, news or crypto if it "
            "adds context.",
            context, display=f"Analyze the {mode} view"
            + (f" for {sym}" if sym else ""))

    # -- News tab --------------------------------------------------------------

    def _news_mode(self) -> str:
        idx = self.news_mode.get_active()
        return _NEWS_MODES[idx] if 0 <= idx < len(_NEWS_MODES) else "top"

    def _news_mode_changed(self, combo):
        mode = self._news_mode()
        placeholders = {
            "top": "Top stories — no query needed",
            "tech": "Technology stories — no query needed",
            "business": "Business stories — no query needed",
            "search": "search the news — keyword or phrase…",
        }
        self.news_entry.set_placeholder_text(placeholders.get(mode, ""))
        self.news_entry.set_sensitive(mode == "search")
        if mode == "search":
            self.news_entry.grab_focus()
        self._news_items = []
        self._news_selected = None
        self._news_label = NEWS_TOPIC_LABELS.get(mode, "News")
        self.news_store.clear()
        self.news_image.set_from_pixbuf(None)
        self.news_image.set_visible(False)
        self.news_read_btn.set_sensitive(False)
        self.news_open_btn.set_sensitive(False)
        self.news_digest_btn.set_sensitive(False)
        self.news_ask_btn.set_sensitive(False)
        hint = (f"*{self._news_label} — press **Go** to load the stories.*"
                if mode != "search" else
                "*Type a query and press **Go** to search the news through treg.*")
        self._render_md(self.news_detail, hint)

    def _news_go(self):
        mode = self._news_mode()
        limit = int(self.news_limit.get_value())
        if mode == "search":
            if self.client is None:
                self._error("news search needs a treg token — run `treg login`; "
                            "Top, Tech and Business work without one")
                return
            q = self.news_entry.get_text().strip()
            if not q:
                self._error("enter a query to search the news")
                return
            self._busy(f"Searching news for “{q}”…")
            self._spawn(search_news, self.client, q, limit,
                        on_ok=lambda out: self._news_loaded(out, f"Search: {q}"),
                        on_fail=lambda m: self._error(f"news search: {m}"))
            return
        label = NEWS_TOPIC_LABELS.get(mode, "News")
        self._busy(f"Loading {label.lower()}…")
        self._spawn(fetch_topic, mode, limit,
                    on_ok=lambda out: self._news_loaded(out, label),
                    on_fail=lambda m: self._error(f"news: {m}"))

    def _news_loaded(self, out: dict, label: str):
        self._news_label = label
        self._news_items = out.get("items") or []
        self._news_selected = None
        self.news_store.clear()
        for it in self._news_items:
            sources = it.get("sources") or ([it.get("source")]
                                            if it.get("source") else [])
            self.news_store.append([
                sources[0] if sources else "",
                it.get("title") or "(untitled)",
                news_when(it.get("ts"), it.get("published") or ""),
                f"+{len(sources) - 1}" if len(sources) > 1 else "",
            ])
        self.news_image.set_from_pixbuf(None)
        self.news_image.set_visible(False)
        self.news_read_btn.set_sensitive(False)
        self.news_open_btn.set_sensitive(False)
        self.news_digest_btn.set_sensitive(bool(self._news_items))
        self.news_ask_btn.set_sensitive(False)
        if not self._news_items:
            self._render_md(self.news_detail,
                            f"*{label} returned no stories — try again.*")
            self._error(f"no news stories for {label}")
            return
        self._render_md(
            self.news_detail,
            f"**{label}** — {len(self._news_items)} stories\n\n"
            "*Select a story to see it here, **Read article** to fetch the "
            "full text, **Ask AI** about it, or **AI digest** to summarize "
            "the whole list.*")
        bits = [f"{len(self._news_items)} stories"]
        if out.get("feeds"):
            bits.append(f"from {out['feeds']} feeds")
        if out.get("served_by"):
            bits.append(f"via {out['served_by']}")
        if out.get("cost_micro") is not None:
            bits.append(f"charged {usd(out['cost_micro'])}")
        errors = out.get("errors") or []
        if errors:
            bits.append(f"{len(errors)} feed(s) unavailable")
        self._busy(" · ".join(bits))

    def _on_news_selected(self):
        model, treeiter = self.news_table.get_selection().get_selected()
        if treeiter is None:
            return
        idx = model.get_path(treeiter).get_indices()[0]
        if idx >= len(self._news_items):
            return
        self._news_selected = self._news_items[idx]
        self._news_render_detail()

    def _news_render_detail(self):
        p = self._news_selected
        if not p:
            return
        sources = p.get("sources") or []
        lines = [f"**{p.get('title') or '(untitled)'}**", ""]
        meta = [s for s in (p.get("source_label") or p.get("source"),
                            news_when(p.get("ts"), p.get("published") or ""))
                if s]
        if meta:
            lines.append(" · ".join(_esc(m) for m in meta))
        if len(sources) > 1:
            lines.append(f"Also covered by: {_esc(', '.join(sources[1:]))}")
        if p.get("snippet"):
            lines += ["", _esc(p["snippet"])]
        lines += ["", f"[Open the article]({p.get('url') or ''})"]
        self._render_md(self.news_detail, "\n".join(lines))
        self.news_read_btn.set_sensitive(
            bool(p.get("url") and self.client is not None))
        self.news_open_btn.set_sensitive(bool(p.get("url")))
        self.news_ask_btn.set_sensitive(True)
        self._news_show_image(p)

    def _news_show_image(self, p: dict):
        if not p.get("image"):
            self.news_image.set_from_pixbuf(None)
            self.news_image.set_visible(False)
            return
        self._spawn(_fetch_image, p["image"],
                    on_ok=self._news_image_loaded,
                    on_fail=self._news_image_failed)

    def _news_image_failed(self, _msg: str):
        self.news_image.set_from_pixbuf(None)
        self.news_image.set_visible(False)

    def _news_image_loaded(self, data):
        if not data:
            self._news_image_failed("")
            return
        stream = Gio.MemoryInputStream.new_from_bytes(GLib.Bytes.new(data))
        try:
            pixbuf = GdkPixbuf.Pixbuf.new_from_stream(stream, None)
        except GLib.Error:
            self._news_image_failed("")
            return
        w, h = pixbuf.get_width(), pixbuf.get_height()
        if w > 560:
            pixbuf = pixbuf.scale_simple(560, max(1, int(h * 560 / w)),
                                         GdkPixbuf.InterpType.BILINEAR)
        self.news_image.set_from_pixbuf(pixbuf)
        self.news_image.set_visible(True)

    def _news_read(self):
        p = self._news_selected
        if not p or not p.get("url"):
            return
        if self.client is None:
            self._error("reading an article needs a treg token — run `treg login`")
            return
        url = p["url"]
        self.news_read_btn.set_sensitive(False)
        self._busy(f"Reading {url}…")
        self._spawn(self.client.call, "treg.web.extract", "POST", None,
                    json.dumps({"url": url}),
                    on_ok=lambda r: self._news_read_done(url, r),
                    on_fail=self._news_read_failed)

    def _news_read_failed(self, msg: str):
        self.news_read_btn.set_sensitive(True)
        self._error(f"read: {msg}")

    def _news_read_done(self, url: str, r: httpx.Response):
        self.news_read_btn.set_sensitive(True)
        try:
            body = r.json()
        except ValueError:
            body = None
        p = self._news_selected or {}
        head = f"# {p.get('title') or 'Article'}\n\n"
        meta = [s for s in (p.get("source_label") or p.get("source"),
                            news_when(p.get("ts"), p.get("published") or ""))
                if s]
        if meta:
            head += f"*{' · '.join(_esc(m) for m in meta)}*\n\n"
        if r.status_code >= 400 or body is None:
            detail = body.get("detail", "") if isinstance(body, dict) else ""
            reason = _esc(detail) if detail else f"HTTP {r.status_code}"
            self._render_md(self.news_detail,
                            head + f"*(could not read the article) {reason}*")
            self._error(f"read: HTTP {r.status_code}")
            return
        bits = [url]
        if r.headers.get("X-Treg-Served-By"):
            bits.append(f"via {r.headers['X-Treg-Served-By']}")
        if r.headers.get("X-Treg-Cost-Micro") is not None:
            bits.append(f"charged {usd(int(r.headers['X-Treg-Cost-Micro']))}")
        self._render_md(self.news_detail, head + _page_markdown(body),
                        base_url=url)
        self._busy(" · ".join(bits))

    def _news_open(self):
        p = self._news_selected
        if p and p.get("url"):
            try:
                Gio.AppInfo.launch_default_for_uri(p["url"], None)
            except GLib.Error as e:
                self._error(f"could not open {p['url']}: {e.message}")

    def _news_digest(self):
        rows = self._news_items
        if not rows:
            self._error("load some stories first, then press AI digest")
            return
        n = min(len(rows), 12)
        self._ai_ask(
            f"Write a digest of the {n} current {self._news_label} stories "
            "below. You can call the news tools for anything more recent.",
            digest_prompt(self._news_label, rows),
            display=f"Write a digest of the current {self._news_label} stories.")
        self._busy(f"AI is digesting {n} stories…")

    def _news_ask_ai(self):
        p = self._news_selected
        if not p:
            self._error("select a story first")
            return
        sources = ", ".join(p.get("sources") or [])
        context = (
            "Story:\n"
            f"Title: {p.get('title', '')}\n"
            f"Sources: {sources or p.get('source_label') or p.get('source') or 'unknown'}\n"
            f"Published: {p.get('published') or ''}\n"
            f"URL: {p.get('url') or ''}\n"
            f"Snippet: {p.get('snippet') or ''}"
        )
        self._ai_ask(
            "Explain this news story: what happened, who is involved, the "
            "numbers that matter, and the likely impact. Read the full "
            "article and look for related coverage if it helps.",
            context,
            display=f"Explain this story: {p.get('title', '')}")

    def _news_digest_failed(self, msg: str):
        self.news_digest_btn.set_sensitive(bool(self._news_items))
        self._render_md(self.news_detail, f"*(digest failed) {_esc(msg)}*")
        self._error(f"digest: {msg}")

    # -- Hacker News tab -------------------------------------------------------

    def _hn_mode(self) -> str:
        idx = self.hn_mode.get_active()
        return _HN_MODES[idx] if 0 <= idx < len(_HN_MODES) else "top"

    def _hn_mode_changed(self, combo):
        mode = self._hn_mode()
        placeholders = {
            "top": "Top stories — no query needed",
            "new": "Newest submissions — no query needed",
            "best": "Best stories — no query needed",
            "ask": "Ask HN — no query needed",
            "show": "Show HN — no query needed",
            "jobs": "Jobs — no query needed",
            "search": "search Hacker News — keyword or phrase…",
        }
        self.hn_entry.set_placeholder_text(placeholders.get(mode, ""))
        self.hn_entry.set_sensitive(mode == "search")
        if mode == "search":
            self.hn_entry.grab_focus()
        self._hn_items = []
        self._hn_selected = None
        self._hn_label = HN_FEED_LABELS.get(mode, "Hacker News")
        self.hn_store.clear()
        self.hn_read_btn.set_sensitive(False)
        self.hn_open_btn.set_sensitive(False)
        self.hn_comments_btn.set_sensitive(False)
        self.hn_digest_btn.set_sensitive(False)
        self.hn_ask_btn.set_sensitive(False)
        hint = (f"*{self._hn_label} — press **Go** to load the stories.*"
                if mode != "search" else
                "*Type a query and press **Go** to search Hacker News "
                "(free Algolia API).*")
        self._render_md(self.hn_detail, hint)

    def _hn_go(self):
        mode = self._hn_mode()
        limit = int(self.hn_limit.get_value())
        if mode == "search":
            q = self.hn_entry.get_text().strip()
            if not q:
                self._error("enter a query to search Hacker News")
                return
            self._busy(f"Searching Hacker News for “{q}”…")
            self._spawn(hn_search, q, limit,
                        on_ok=lambda out: self._hn_loaded(out, f"Search: {q}"),
                        on_fail=lambda m: self._error(f"Hacker News search: {m}"))
            return
        label = HN_FEED_LABELS.get(mode, "Hacker News")
        self._busy(f"Loading {label.lower()} from Hacker News…")
        self._spawn(hn_feed, mode, limit,
                    on_ok=lambda out: self._hn_loaded(out, label),
                    on_fail=lambda m: self._error(f"Hacker News: {m}"))

    def _hn_loaded(self, rows: list[dict], label: str):
        self._hn_label = label
        self._hn_items = rows or []
        self._hn_selected = None
        self.hn_store.clear()
        for it in self._hn_items:
            self.hn_store.append([
                it.get("title") or "(untitled)",
                _count(it.get("score")),
                _count(it.get("comments")),
            ])
        self.hn_read_btn.set_sensitive(False)
        self.hn_open_btn.set_sensitive(False)
        self.hn_comments_btn.set_sensitive(False)
        self.hn_digest_btn.set_sensitive(bool(self._hn_items))
        self.hn_ask_btn.set_sensitive(False)
        if not self._hn_items:
            self._render_md(self.hn_detail,
                            f"*{label} returned no stories — try again.*")
            self._error(f"no Hacker News stories for {label}")
            return
        self._render_md(
            self.hn_detail,
            f"**{label}** — {len(self._hn_items)} stories\n\n"
            "*Select a story for its details, **Read article** for the full "
            "text (treg), **Comments** for the free discussion, **Ask AI** "
            "about it, or **AI digest** to summarize the list.*")
        self._busy(f"{len(self._hn_items)} stories · Hacker News (free)")

    def _on_hn_selected(self):
        model, treeiter = self.hn_table.get_selection().get_selected()
        if treeiter is None:
            return
        idx = model.get_path(treeiter).get_indices()[0]
        if idx >= len(self._hn_items):
            return
        self._hn_selected = self._hn_items[idx]
        self._hn_render_detail()

    def _hn_render_detail(self):
        p = self._hn_selected
        if not p:
            return
        lines = [f"**{p.get('title') or '(untitled)'}**", ""]
        meta = [f"{p.get('score') or 0} points",
                f"{p.get('comments') or 0} comments"]
        if p.get("site"):
            meta.append(p["site"])
        if p.get("by"):
            meta.append(f"by {p['by']}")
        when = news_when(p.get("ts"))
        if when:
            meta.append(when)
        lines.append(" · ".join(_esc(m) for m in meta))
        if p.get("text"):
            lines += ["", _esc(p["text"])]
        links = [f"[Open the article]({p.get('url') or ''})"]
        if p.get("hn_url"):
            links.append(f"[Discuss on Hacker News]({p['hn_url']})")
        lines += ["", " · ".join(links)]
        self._render_md(self.hn_detail, "\n".join(lines))
        self.hn_read_btn.set_sensitive(
            bool(not p.get("self_post") and p.get("url")
                 and self.client is not None))
        self.hn_open_btn.set_sensitive(bool(p.get("url")))
        self.hn_comments_btn.set_sensitive(bool(p.get("comments")))
        self.hn_ask_btn.set_sensitive(True)

    def _hn_read(self):
        p = self._hn_selected
        if not p or not p.get("url"):
            return
        if self.client is None:
            self._error("reading an article needs a treg token — run `treg login`")
            return
        url = p["url"]
        self.hn_read_btn.set_sensitive(False)
        self._busy(f"Reading {url}…")
        self._spawn(self.client.call, "treg.web.extract", "POST", None,
                    json.dumps({"url": url}),
                    on_ok=lambda r: self._hn_read_done(url, r),
                    on_fail=self._hn_read_failed)

    def _hn_read_failed(self, msg: str):
        self.hn_read_btn.set_sensitive(True)
        self._error(f"read: {msg}")

    def _hn_read_done(self, url: str, r: httpx.Response):
        self.hn_read_btn.set_sensitive(True)
        try:
            body = r.json()
        except ValueError:
            body = None
        p = self._hn_selected or {}
        head = f"# {p.get('title') or 'Article'}\n\n"
        meta = [f"{p.get('score') or 0} points"]
        if p.get("site"):
            meta.append(p["site"])
        when = news_when(p.get("ts"))
        if when:
            meta.append(when)
        head += f"*{' · '.join(_esc(m) for m in meta)}*\n\n"
        if r.status_code >= 400 or body is None:
            detail = body.get("detail", "") if isinstance(body, dict) else ""
            reason = _esc(detail) if detail else f"HTTP {r.status_code}"
            self._render_md(self.hn_detail,
                            head + f"*(could not read the article) {reason}*")
            self._error(f"read: HTTP {r.status_code}")
            return
        bits = [url]
        if r.headers.get("X-Treg-Served-By"):
            bits.append(f"via {r.headers['X-Treg-Served-By']}")
        if r.headers.get("X-Treg-Cost-Micro") is not None:
            bits.append(f"charged {usd(int(r.headers['X-Treg-Cost-Micro']))}")
        self._render_md(self.hn_detail, head + _page_markdown(body),
                        base_url=url)
        self._busy(" · ".join(bits))

    def _hn_open(self):
        p = self._hn_selected
        if p and p.get("url"):
            try:
                Gio.AppInfo.launch_default_for_uri(p["url"], None)
            except GLib.Error as e:
                self._error(f"could not open {p['url']}: {e.message}")

    def _hn_comments(self):
        p = self._hn_selected
        if not p or p.get("id") is None:
            return
        self.hn_comments_btn.set_sensitive(False)
        self._busy(f"Loading comments for “{p.get('title', '')}”…")
        self._spawn(hn_comment_tree, p["id"], 60,
                    on_ok=lambda out: self._hn_comments_done(p, out),
                    on_fail=self._hn_comments_failed)

    def _hn_comments_failed(self, msg: str):
        self.hn_comments_btn.set_sensitive(True)
        self._error(f"comments: {msg}")

    def _hn_comments_done(self, p: dict, out: dict):
        self.hn_comments_btn.set_sensitive(True)
        rows = out.get("comments") or []
        total = out.get("total") or len(rows)
        lines = [f"**{p.get('title') or 'Story'}**", "",
                 f"*Comments — {len(rows)} of {total} loaded (free Hacker News API)*",
                 ""]
        if not rows:
            lines.append("*(no comments yet)*")
        for c in rows:
            depth = min(int(c.get("depth") or 0), 8)
            head = ("↳ " * depth) + f"**{_esc(c.get('by') or '?')}**"
            when = news_when(c.get("ts"))
            if when:
                head += f"  ({_esc(when)})"
            lines.append(head)
            for ln in (c.get("text") or "").splitlines():
                lines.append(("↳ " * depth) + _esc(ln))
            lines.append("")
        self._render_md(self.hn_detail, "\n".join(lines))
        self._busy(f"{len(rows)} comments · Hacker News (free)")

    def _hn_digest(self):
        rows = self._hn_items
        if not rows:
            self._error("load some stories first, then press AI digest")
            return
        n = min(len(rows), 12)
        self._ai_ask(
            f"Write a digest of the {n} current Hacker News {self._hn_label} "
            "stories below. You can call the Hacker News tools for anything "
            "more recent.",
            hn_digest_prompt(self._hn_label, rows),
            display=f"Write a digest of the current Hacker News "
                    f"{self._hn_label} stories.")
        self._busy(f"AI is digesting {n} Hacker News stories…")

    def _hn_ask_ai(self):
        p = self._hn_selected
        if not p:
            self._error("select a story first")
            return
        context = (
            "Hacker News story:\n"
            f"Title: {p.get('title', '')}\n"
            f"Points: {p.get('score') or 0}\n"
            f"Comments: {p.get('comments') or 0}\n"
            f"Author: {p.get('by') or ''}\n"
            f"Site: {p.get('site') or ''}\n"
            f"Posted: {news_when(p.get('ts')) or ''}\n"
            f"URL: {p.get('url') or ''}\n"
            f"HN discussion: {p.get('hn_url') or ''}\n"
            f"Text: {p.get('text') or ''}"
        )
        self._ai_ask(
            "Explain this Hacker News story: what it is, why the tech "
            "community is interested, the key technical claims, and the state "
            "of the discussion. Read the link and the HN thread if it helps, "
            "and pull related news, coverage or market context when relevant.",
            context,
            display=f"Explain this Hacker News story: {p.get('title', '')}")

    # -- GitHub tab ------------------------------------------------------------

    def _gh_mode(self) -> str:
        idx = self.gh_mode.get_active()
        return _GH_MODES[idx] if 0 <= idx < len(_GH_MODES) else "search"

    def _gh_mode_changed(self, combo):
        mode = self._gh_mode()
        placeholders = {
            "search": "search repositories — e.g. “static site generator”",
            "trending": "no query needed — recent repos by stars (last 7 days)",
            "repo": "owner/name or a github.com URL",
            "issues": "owner/name — open issues and pull requests",
            "releases": "owner/name — releases",
        }
        self.gh_entry.set_placeholder_text(placeholders.get(mode, ""))
        self.gh_entry.set_sensitive(mode != "trending")
        if mode != "trending":
            self.gh_entry.grab_focus()
        self._gh_items = []
        self._gh_selected = None
        self._gh_readme = ""
        self._gh_label = {"trending": "Trending · last 7 days"}.get(
            mode, "GitHub")
        self.gh_store.clear()
        self.gh_open_btn.set_sensitive(False)
        self.gh_digest_btn.set_sensitive(False)
        self.gh_ask_btn.set_sensitive(False)
        hint = {
            "trending": "*Press **Go** for the repos created in the last 7 "
                        "days, ranked by stars.*",
            "search": "*Type a query and press **Go** to search repositories.*",
            "repo": "*Enter **owner/name** (or a github.com URL) and press "
                    "**Go** for the README and stats.*",
            "issues": "*Enter **owner/name** and press **Go** for open issues "
                      "and pull requests.*",
            "releases": "*Enter **owner/name** and press **Go** for releases.*",
        }.get(mode, "")
        self._render_md(self.gh_detail, hint)

    def _gh_go(self):
        mode = self._gh_mode()
        limit = int(self.gh_limit.get_value())
        if mode == "trending":
            self._busy("Loading trending repositories…")
            self._spawn(gh_trending, 7, limit,
                        on_ok=lambda out: self._gh_loaded(
                            out, "Trending · last 7 days"),
                        on_fail=lambda m: self._error(f"GitHub: {m}"))
            return
        q = self.gh_entry.get_text().strip()
        if not q:
            self._error("enter a search query or a repo as owner/name")
            return
        if mode == "repo":
            self._busy(f"Loading {q}…")
            self._spawn(gh_repo, q, on_ok=self._gh_repo_loaded,
                        on_fail=lambda m: self._error(f"GitHub: {m}"))
            return
        if mode == "search":
            self._busy(f"Searching GitHub for “{q}”…")
            self._spawn(gh_search, q, limit,
                        on_ok=lambda out: self._gh_loaded(out, f"Search: {q}"),
                        on_fail=lambda m: self._error(f"GitHub: {m}"))
            return
        if mode == "issues":
            self._busy(f"Loading issues for {q}…")
            self._spawn(gh_issues, q, limit,
                        on_ok=lambda out: self._gh_loaded(
                            out, f"Issues: {q}"),
                        on_fail=lambda m: self._error(f"GitHub: {m}"))
            return
        self._busy(f"Loading releases for {q}…")
        self._spawn(gh_releases, q, limit,
                    on_ok=lambda out: self._gh_loaded(out, f"Releases: {q}"),
                    on_fail=lambda m: self._error(f"GitHub: {m}"))

    def _gh_loaded(self, rows: list[dict], label: str):
        self._gh_label = label
        self._gh_items = rows or []
        self._gh_selected = None
        self._gh_readme = ""
        self.gh_store.clear()
        for it in self._gh_items:
            self.gh_store.append([it.get("title") or "(untitled)",
                                  it.get("meta") or ""])
        self.gh_open_btn.set_sensitive(False)
        self.gh_ask_btn.set_sensitive(False)
        self.gh_digest_btn.set_sensitive(bool(self._gh_items))
        if not self._gh_items:
            self._render_md(self.gh_detail,
                            f"*{label} returned nothing — try again.*")
            self._error(f"GitHub: no results for {label}")
            return
        self._render_md(
            self.gh_detail,
            f"**{label}** — {len(self._gh_items)} rows\n\n"
            "*Select a row for detail, **Open on GitHub** in the browser, "
            "**Ask AI** about it, or **AI digest** to summarize the list.*")
        self.gh_table.get_selection().select_path(
            Gtk.TreePath.new_from_string("0"))
        self._busy(f"{len(self._gh_items)} results · GitHub (free)")

    def _gh_repo_loaded(self, out: dict):
        row = out.get("repo") or {}
        self._gh_label = f"Repo: {row.get('full_name') or ''}"
        self._gh_items = [row] if row else []
        self._gh_selected = row or None
        self._gh_readme = out.get("readme") or ""
        self.gh_store.clear()
        if row:
            self.gh_store.append([row.get("title") or "",
                                  row.get("meta") or ""])
        self.gh_open_btn.set_sensitive(bool(row))
        self.gh_ask_btn.set_sensitive(bool(row))
        self.gh_digest_btn.set_sensitive(bool(row))
        if not row:
            self._error("GitHub: no repository found")
            return
        self._gh_render_detail()
        self._busy(f"{row.get('full_name')} · GitHub (free)")

    def _on_gh_selected(self):
        model, treeiter = self.gh_table.get_selection().get_selected()
        if treeiter is None:
            return
        idx = model.get_path(treeiter).get_indices()[0]
        if idx >= len(self._gh_items):
            return
        self._gh_selected = self._gh_items[idx]
        self._gh_render_detail()

    def _gh_render_detail(self):
        p = self._gh_selected
        if not p:
            return
        lines = [f"**{p.get('title') or '(untitled)'}**", ""]
        if p.get("subtitle"):
            lines += [_esc(p["subtitle"]), ""]
        if p.get("meta"):
            lines += [f"*{_esc(p['meta'])}*", ""]
        if p.get("body"):
            lines += [_esc(p["body"]), ""]
        if p.get("url"):
            lines.append(f"[Open on GitHub]({p['url']})")
        if self._gh_readme:
            lines += ["", "---", "", self._gh_readme[:30000]]
        url = p.get("url") or ""
        self._render_md(self.gh_detail, "\n".join(lines),
                        base_url=(url + "/") if url else None)
        self.gh_open_btn.set_sensitive(bool(url))
        self.gh_ask_btn.set_sensitive(True)

    def _gh_open(self):
        p = self._gh_selected
        if p and p.get("url"):
            try:
                Gio.AppInfo.launch_default_for_uri(p["url"], None)
            except GLib.Error as e:
                self._error(f"could not open {p['url']}: {e.message}")

    def _gh_digest(self):
        rows = self._gh_items
        if not rows:
            self._error("load some repositories first, then press AI digest")
            return
        n = min(len(rows), 12)
        self._ai_ask(
            f"Write a digest of these GitHub {self._gh_label} results below. "
            "You can call the github_* tools for anything more detailed.",
            gh_digest_prompt(self._gh_label, rows),
            display=f"Write a digest of the GitHub {self._gh_label} results.")
        self._busy(f"AI is digesting {n} GitHub rows…")

    def _gh_ask_ai(self):
        p = self._gh_selected
        if not p:
            self._error("select a repository or issue first")
            return
        summary = p.get("body") or p.get("subtitle") or ""
        context = (
            f"GitHub tab — {self._gh_label}\n"
            f"Item: {p.get('title') or ''}\n"
            f"Meta: {p.get('meta') or ''}\n"
            f"URL: {p.get('url') or ''}\n"
            + (f"Summary: {summary}\n" if summary else "")
            + "\nOn screen:\n" + self._text(self.gh_detail)[:8000]
        )
        self._ai_ask(
            "Explain this GitHub item: what the project (or issue/release) "
            "is, how active and notable it is, the key technical points, and "
            "anything a developer should know. Use the github_* tools for "
            "issues, releases or related projects if it helps.",
            context,
            display=f"Explain this GitHub item: {p.get('title', '')}")

    # -- OLX tab ---------------------------------------------------------------

    def _olx_mode_changed(self, *args):
        listing = (self.olx_mode.get_active_id() or "search") == "listing"
        self.olx_entry.set_placeholder_text(
            "OLX listing ID or URL — e.g. 674732003" if listing else
            "search OLX.pt — leave empty for the latest listings")
        self.olx_filters.set_sensitive(not listing)
        self.olx_limit.set_sensitive(not listing)
        self._olx_items = []
        self._olx_selected = None
        self._olx_result = {}
        self._olx_label = "OLX.pt"
        self.olx_store.clear()
        self.olx_open_btn.set_sensitive(False)
        self.olx_digest_btn.set_sensitive(False)
        self.olx_ask_btn.set_sensitive(False)
        self._render_md(
            self.olx_detail,
            "*Paste an OLX.pt URL or numeric id and press **Go** for the "
            "full listing.*" if listing else
            "*Search OLX.pt by keyword, or choose a category and leave the "
            "query empty to browse it. Filter by price, sort, then press "
            "**Go**. Free — OLX's public JSON API, no treg calls.*")

    def _olx_go(self):
        mode = self.olx_mode.get_active_id() or "search"
        value = self.olx_entry.get_text().strip()
        if mode == "listing":
            if not value:
                self._error("enter an OLX listing id or URL")
                return
            self._busy(f"Loading OLX listing {value[:60]}…")
            self._spawn(olx_listing, value,
                        on_ok=self._olx_listing_loaded,
                        on_fail=lambda m: self._error(f"OLX: {m}"))
            return
        cat_id = self.olx_category.get_active_id() or None
        pmin = int(self.olx_price_min.get_value()) or None
        pmax = int(self.olx_price_max.get_value()) or None
        sort = self.olx_sort.get_active_id() or "relevance"
        limit = int(self.olx_limit.get_value())
        self._busy(f"Searching OLX.pt for “{value}”…" if value
                   else "Loading OLX.pt listings…")
        self._spawn(
            lambda: olx_search(value, category=cat_id, price_min=pmin,
                               price_max=pmax, sort=sort, limit=limit),
            on_ok=self._olx_loaded,
            on_fail=lambda m: self._error(f"OLX: {m}"))

    def _olx_loaded(self, out: dict):
        self._olx_result = out or {}
        self._olx_items = (out or {}).get("rows") or []
        self._olx_selected = None
        self._olx_label = self._olx_result_label(out or {})
        self.olx_store.clear()
        for it in self._olx_items:
            self.olx_store.append([it.get("title") or "(untitled)",
                                   it.get("meta") or ""])
        self.olx_open_btn.set_sensitive(False)
        self.olx_ask_btn.set_sensitive(False)
        self.olx_digest_btn.set_sensitive(bool(self._olx_items))
        if not self._olx_items:
            self._render_md(
                self.olx_detail,
                f"*No OLX.pt listings for {_esc(self._olx_label)} — try a "
                "broader query or another category.*")
            self._error(f"OLX: no listings for {self._olx_label}")
            return
        total = int((out or {}).get("total") or len(self._olx_items))
        self._render_md(
            self.olx_detail,
            f"**{_esc(self._olx_label)}** — {len(self._olx_items)} of "
            f"{total:,} listings\n\n"
            "*Select a row for the full listing, **Open on OLX** in the "
            "browser, **Ask AI** to judge it, or **AI digest** to summarize "
            "the list.*")
        self.olx_table.get_selection().select_path(
            Gtk.TreePath.new_from_string("0"))
        self._busy(f"{len(self._olx_items)} of {total:,} listings · "
                   "OLX.pt (free)")

    def _olx_result_label(self, out: dict) -> str:
        q = out.get("query") or ""
        cat = out.get("category") or ""
        if q and cat and cat != "All categories":
            return f"{q} · {cat}"
        if q:
            return f"Search: {q}"
        if cat and cat != "All categories":
            return f"Category: {cat}"
        return "Latest listings"

    def _olx_listing_loaded(self, row: dict):
        self._olx_items = [row] if row else []
        self._olx_selected = row or None
        self._olx_result = {"rows": self._olx_items, "total": 1,
                            "query": "", "category": ""}
        self._olx_label = f"Listing {row.get('id')}" if row else "Listing"
        self.olx_store.clear()
        if row:
            self.olx_store.append([row.get("title") or "",
                                   row.get("meta") or ""])
        self.olx_open_btn.set_sensitive(bool(row and row.get("url")))
        self.olx_ask_btn.set_sensitive(bool(row))
        self.olx_digest_btn.set_sensitive(bool(row))
        if not row:
            self._error("OLX: no listing found")
            return
        self._olx_render_detail()
        self._busy(f"{row.get('title', '')[:60]} · OLX.pt (free)")

    def _on_olx_selected(self):
        model, it = self.olx_table.get_selection().get_selected()
        if it is None:
            return
        idx = model.get_path(it).get_indices()[0]
        if idx >= len(self._olx_items):
            return
        self._olx_selected = self._olx_items[idx]
        self._olx_render_detail()

    def _olx_render_detail(self):
        p = self._olx_selected
        if not p:
            return
        price = p.get("price") or {}
        price_label = price.get("label") or "Price not stated"
        lines = [f"**{_esc(p.get('title') or '(untitled)')}**", ""]
        head = f"**{_esc(price_label)}**"
        if price.get("negotiable"):
            head += " · negotiable"
        if p.get("promoted"):
            head += " · promoted"
        lines += [head, ""]
        loc = p.get("location") or {}
        when = news_when(p["ts"]) if p.get("ts") else ""
        bits = []
        if loc.get("label"):
            bits.append(_esc(loc["label"]))
        if when:
            bits.append(f"posted {_esc(when)}")
        if p.get("category"):
            bits.append(_esc(p["category"]))
        if bits:
            lines += [" · ".join(bits), ""]
        seller = p.get("seller") or {}
        if seller.get("name"):
            kind = "business" if p.get("is_business") else "private"
            lines += [f"Seller: **{_esc(seller['name'])}** ({kind})", ""]
        attrs = p.get("attrs") or {}
        if attrs:
            lines.append("**Details**")
            for k, v in attrs.items():
                lines.append(f"- **{_esc(k)}:** {_esc(v)}")
            lines.append("")
        desc = p.get("description") or ""
        if desc:
            lines += ["**Description**", "", _esc(desc), ""]
        photos = p.get("photos") or []
        if photos:
            links = " · ".join(f"[{i}]({u})"
                               for i, u in enumerate(photos[:6], 1))
            lines += [f"**Photos ({p.get('photo_count') or len(photos)}):** "
                      f"{links}", ""]
        if p.get("url"):
            lines.append(f"[Open on OLX]({p['url']})")
        self._render_md(self.olx_detail, "\n".join(lines))
        self.olx_open_btn.set_sensitive(bool(p.get("url")))
        self.olx_ask_btn.set_sensitive(True)

    def _olx_open(self):
        p = self._olx_selected
        if p and p.get("url"):
            try:
                Gio.AppInfo.launch_default_for_uri(p["url"], None)
            except GLib.Error as e:
                self._error(f"could not open {p['url']}: {e.message}")

    def _olx_digest(self):
        rows = self._olx_items
        if not rows:
            self._error("search for some listings first, then press AI digest")
            return
        out = self._olx_result or {}
        total = out.get("total")
        self._ai_ask(
            "Write a digest of these OLX.pt listings below. You can call "
            "olx_search to compare prices or find more options.",
            olx_digest_prompt(self._olx_label, rows, total),
            display=f"Write a digest of the OLX.pt {self._olx_label} listings.")
        self._busy(f"AI is digesting {min(len(rows), 15)} OLX listings…")

    def _olx_ask_ai(self):
        p = self._olx_selected
        if not p:
            self._error("select a listing first")
            return
        loc = (p.get("location") or {}).get("label") or ""
        attrs = "; ".join(f"{k}: {v}"
                          for k, v in (p.get("attrs") or {}).items())
        context = (
            f"OLX.pt tab — {self._olx_label}\n"
            f"Listing: {p.get('title') or ''}\n"
            f"Price: {p.get('price_label') or 'not stated'}\n"
            f"Location: {loc}\n"
            f"Category: {p.get('category') or ''}\n"
            f"Seller: {(p.get('seller') or {}).get('name') or ''}"
            f" ({'business' if p.get('is_business') else 'private'})\n"
            f"Details: {attrs}\n"
            f"URL: {p.get('url') or ''}\n"
            f"\nDescription:\n{(p.get('description') or '')[:4000]}\n"
            f"\nOn screen:\n{self._text(self.olx_detail)[:8000]}"
        )
        self._ai_ask(
            "Explain this OLX.pt listing: what is being sold, whether the "
            "asking price looks fair for the Portuguese market, what to check "
            "before buying, and what to ask the seller. Use olx_search to "
            "compare similar listings.",
            context,
            display=f"Explain this OLX.pt listing: {p.get('title', '')}")

    # -- eBay tab --------------------------------------------------------------

    def _ebay_mode_changed(self, *args):
        listing = (self.ebay_mode.get_active_id() or "search") == "listing"
        self.ebay_entry.set_placeholder_text(
            "eBay product id or URL — e.g. 336847604413" if listing else
            "search eBay.com — e.g. “iphone 13”")
        self.ebay_filters.set_sensitive(not listing)
        self.ebay_limit.set_sensitive(not listing)
        self._ebay_items = []
        self._ebay_selected = None
        self._ebay_result = {}
        self._ebay_label = "eBay"
        self._ebay_fill_categories([])
        self.ebay_store.clear()
        self.ebay_open_btn.set_sensitive(False)
        self.ebay_full_btn.set_sensitive(False)
        self.ebay_digest_btn.set_sensitive(False)
        self.ebay_ask_btn.set_sensitive(False)
        self._render_md(
            self.ebay_detail,
            "*Paste an eBay product id or listing URL and press **Go** for "
            "the full listing. Paid through treg — $0.015 per successful "
            "call.*" if listing else
            "*Search eBay.com by keyword or browse a category, then press "
            "**Go**. Search is paid through treg — $0.015 per successful "
            "call; **Full listing** is a second $0.015 call. Select a row for "
            "its price, condition, shipping, seller and returns.*")

    def _ebay_fill_categories(self, categories: list[dict],
                              selected: str | None = None):
        selected = str(selected or "")
        self.ebay_category.remove_all()
        self.ebay_category.append("", "All categories")
        ids = {""}
        for cat in categories or []:
            cid = str(cat.get("id") or "")
            name = cat.get("name") or f"Category {cid}"
            if not cid or cid in ids:
                continue
            ids.add(cid)
            self.ebay_category.append(cid, name)
        if selected and selected not in ids:
            self.ebay_category.append(selected, f"Category {selected}")
            ids.add(selected)
        if not self.ebay_category.set_active_id(selected):
            self.ebay_category.set_active(0)

    def _ebay_go(self):
        if self.client is None:
            self._error("eBay: no treg token — run `treg login` or set "
                        "TREG_TOKEN")
            return
        mode = self.ebay_mode.get_active_id() or "search"
        value = self.ebay_entry.get_text().strip()
        if mode == "listing":
            if not value:
                self._error("enter an eBay product id or listing URL")
                return
            self._busy(f"Loading eBay listing {value[:60]}… "
                       "(paid $0.015 via treg)")
            self._spawn(
                lambda: ebay_listing(self.client, value),
                on_ok=self._ebay_listing_loaded,
                on_fail=lambda m: self._error(f"eBay: {m}"))
            return
        cat_id = self.ebay_category.get_active_id() or None
        if not value and not cat_id:
            self._error("enter a search or choose a category")
            return
        sort = self.ebay_sort.get_active_id() or "relevance"
        limit = int(self.ebay_limit.get_value())
        self._busy(f"Searching eBay for “{value}”… (paid $0.015 via treg)"
                   if value else "Loading eBay category… (paid $0.015 via treg)")
        self._spawn(
            lambda: ebay_search(self.client, value, category_id=cat_id,
                                sort=sort, limit=limit),
            on_ok=self._ebay_loaded,
            on_fail=lambda m: self._error(f"eBay: {m}"))

    def _ebay_loaded(self, out: dict):
        self._ebay_result = out or {}
        self._ebay_items = (out or {}).get("rows") or []
        self._ebay_selected = None
        self._ebay_label = self._ebay_result_label(out or {})
        self._ebay_fill_categories(
            (out or {}).get("categories") or [],
            (out or {}).get("category_id"))
        self.ebay_store.clear()
        for it in self._ebay_items:
            self.ebay_store.append([it.get("title") or "(untitled)",
                                    it.get("meta") or ""])
        self.ebay_open_btn.set_sensitive(False)
        self.ebay_ask_btn.set_sensitive(False)
        self.ebay_full_btn.set_sensitive(False)
        self.ebay_digest_btn.set_sensitive(bool(self._ebay_items))
        if not self._ebay_items:
            self._render_md(
                self.ebay_detail,
                f"*No eBay listings for {_esc(self._ebay_label)} — try a "
                "broader query or another category.*")
            self._error(f"eBay: no listings for {self._ebay_label}")
            return
        total = int((out or {}).get("total") or len(self._ebay_items))
        available = int((out or {}).get("available")
                        or len(self._ebay_items))
        cost = usd_amount((out or {}).get("cost_usd"))
        self._render_md(
            self.ebay_detail,
            f"**{_esc(self._ebay_label)}** — {len(self._ebay_items)} of "
            f"{total:,} listings\n\n"
            f"*Fetched {available} rows; charged {cost} through treg. Select "
            "a row for its price, condition, shipping and seller, **Full "
            "listing** for the item specifics and description ($0.015), "
            "**Open on eBay** in the browser, **Ask AI** to judge it, or "
            "**AI digest** to summarize the list.*")
        self.ebay_table.get_selection().select_path(
            Gtk.TreePath.new_from_string("0"))
        self._busy(f"{len(self._ebay_items)} of {total:,} listings · eBay "
                   f"(treg) · charged {cost}")

    def _ebay_result_label(self, out: dict) -> str:
        q = out.get("query") or ""
        cat = out.get("category") or ""
        sort = out.get("sort") or "relevance"
        if q:
            label = f"Search: {q}"
        elif cat and cat != "All categories":
            label = f"Category: {cat}"
        else:
            label = "eBay listings"
        if sort == "cheapest":
            label += " · cheapest"
        elif sort == "expensive":
            label += " · most expensive"
        return label

    def _ebay_listing_loaded(self, row: dict):
        self._ebay_items = [row] if row else []
        self._ebay_selected = row or None
        self._ebay_result = {"rows": self._ebay_items, "total": 1,
                             "query": "", "category": ""}
        self._ebay_label = f"Listing {row.get('id')}" if row else "Listing"
        self.ebay_store.clear()
        if row:
            self.ebay_store.append([row.get("title") or "",
                                    row.get("meta") or ""])
        self.ebay_open_btn.set_sensitive(bool(row and row.get("url")))
        self.ebay_ask_btn.set_sensitive(bool(row))
        self.ebay_digest_btn.set_sensitive(bool(row))
        self.ebay_full_btn.set_sensitive(False)
        if not row:
            self._error("eBay: no listing found")
            return
        self._ebay_render_detail()
        cost = usd_amount(row.get("cost_usd"))
        self._busy(f"{row.get('title', '')[:60]} · eBay (treg) · "
                   f"charged {cost}")

    def _on_ebay_selected(self):
        model, it = self.ebay_table.get_selection().get_selected()
        if it is None:
            return
        idx = model.get_path(it).get_indices()[0]
        if idx >= len(self._ebay_items):
            return
        self._ebay_selected = self._ebay_items[idx]
        self._ebay_render_detail()

    def _ebay_render_detail(self):
        p = self._ebay_selected
        if not p:
            return
        price = p.get("price") or {}
        price_label = price.get("label") or p.get("price_label") \
            or "Price not stated"
        lines = [f"**{_esc(p.get('title') or '(untitled)')}**", ""]
        head = f"**{_esc(price_label)}**"
        if p.get("old_price_label"):
            head += f" · was {_esc(p['old_price_label'])}"
        if p.get("sponsored"):
            head += " · sponsored"
        lines += [head, ""]
        bits = []
        if p.get("condition"):
            bits.append(_esc(p["condition"]))
        if p.get("buying_format_text"):
            bits.append(_esc(p["buying_format_text"]))
        if p.get("location"):
            bits.append(_esc(p["location"]))
        if bits:
            lines += [" · ".join(bits), ""]
        extra = []
        if p.get("shipping_label"):
            extra.append(f"Shipping: **{_esc(p['shipping_label'])}**")
        if p.get("returns"):
            extra.append(f"Returns: **{_esc(p['returns'])}**")
        if p.get("watchers"):
            extra.append(f"Watchers: {_esc(p['watchers'])}")
        if p.get("sold"):
            extra.append(f"Sold: {_esc(p['sold'])}")
        if p.get("rating") not in (None, ""):
            r = f"Rating: {p.get('rating')}"
            if p.get("reviews") not in (None, ""):
                r += f" ({p['reviews']} reviews)"
            extra.append(r)
        if p.get("banner_status"):
            extra.append(_esc(p["banner_status"]))
        if p.get("extensions"):
            extra.append(" · ".join(_esc(x) for x in p["extensions"]))
        for line in extra:
            lines.append(f"- {line}")
        if extra:
            lines.append("")
        seller = p.get("seller") or {}
        if seller.get("name") or seller.get("username"):
            name = seller.get("name") or seller.get("username")
            head = f"Seller: **{_esc(name)}**"
            if seller.get("username") and seller["username"] != name:
                head += f" ({_esc(seller['username'])})"
            lines.append(head)
            fb = []
            if seller.get("positive_percent") not in (None, ""):
                fb.append(f"{seller['positive_percent']}% positive")
            if seller.get("feedback_count") not in (None, ""):
                fb.append(f"{seller['feedback_count']} feedback")
            if seller.get("sold_count") not in (None, ""):
                fb.append(f"{seller['sold_count']} sold")
            if seller.get("joined_at"):
                fb.append(_esc(seller["joined_at"]))
            if fb:
                lines.append(" · ".join(fb))
            if seller.get("bio"):
                lines.append("")
                lines.append(_esc(seller["bio"][:800]))
            lines.append("")
        options = p.get("shipping_options") or []
        if len(options) > 1:
            lines.append("**Shipping options**")
            for opt in options[:6]:
                label = opt.get("label") or ""
                via = opt.get("via") or ""
                lines.append("- " + " · ".join(x for x in (label, via) if x))
            lines.append("")
        specs = p.get("specs") or []
        if specs:
            lines.append("**Item specifics**")
            for spec in specs[:40]:
                lines.append(f"- **{_esc(spec.get('name', ''))}:** "
                             f"{_esc(spec.get('value', ''))}")
            lines.append("")
        desc = p.get("description") or ""
        if desc:
            lines += ["**Description**", "", _esc(desc), ""]
        photos = p.get("photos") or []
        if photos:
            links = " · ".join(f"[{i}]({u})"
                               for i, u in enumerate(photos[:6], 1))
            lines += [f"**Photos ({len(photos)}):** {links}", ""]
        source = p.get("source") or "SerpApi eBay"
        if p.get("full"):
            cost = usd_amount(p.get("cost_usd"))
            lines += [f"*{_esc(source)} via treg — charged {cost}.*", ""]
        else:
            cost = usd_amount((self._ebay_result or {}).get("cost_usd"))
            lines += ["*From the eBay search results via treg — the search "
                      f"was charged {cost}.*", ""]
        if p.get("url"):
            lines.append(f"[Open on eBay]({p['url']})")
        self._render_md(self.ebay_detail, "\n".join(lines))
        self.ebay_open_btn.set_sensitive(bool(p.get("url")))
        self.ebay_ask_btn.set_sensitive(True)
        self.ebay_full_btn.set_sensitive(
            bool(p.get("id")) and not p.get("full"))

    def _ebay_full(self):
        p = self._ebay_selected
        if not p or not p.get("id"):
            self._error("select an eBay listing first")
            return
        if p.get("full"):
            self._error("this listing is already fully loaded")
            return
        if self.client is None:
            self._error("eBay: no treg token — run `treg login` or set "
                        "TREG_TOKEN")
            return
        self._busy(f"Loading full eBay listing {p['id']}… "
                   "(paid $0.015 via treg)")
        self._spawn(
            lambda: ebay_listing(self.client, p["id"], base=p),
            on_ok=self._ebay_full_loaded,
            on_fail=lambda m: self._error(f"eBay: {m}"))

    def _ebay_full_loaded(self, row: dict):
        if not row:
            self._error("eBay: no full listing returned")
            return
        idx = None
        for i, it in enumerate(self._ebay_items):
            if str(it.get("id")) == str(row.get("id")):
                idx = i
                break
        if idx is None:
            self._ebay_items = [row]
            idx = 0
        else:
            self._ebay_items[idx] = row
        self._ebay_selected = row
        model_it = self.ebay_store.get_iter(
            Gtk.TreePath.new_from_string(str(idx)))
        self.ebay_store.set(model_it, 0, row.get("title") or "",
                            1, row.get("meta") or "")
        self.ebay_table.get_selection().select_path(
            Gtk.TreePath.new_from_string(str(idx)))
        self._ebay_render_detail()
        cost = usd_amount(row.get("cost_usd"))
        self._busy(f"{row.get('title', '')[:60]} · eBay (treg) · "
                   f"charged {cost}")

    def _ebay_open(self):
        p = self._ebay_selected
        if p and p.get("url"):
            try:
                Gio.AppInfo.launch_default_for_uri(p["url"], None)
            except GLib.Error as e:
                self._error(f"could not open {p['url']}: {e.message}")

    def _ebay_digest(self):
        rows = self._ebay_items
        if not rows:
            self._error("search for some eBay listings first, then press AI "
                        "digest")
            return
        out = self._ebay_result or {}
        total = out.get("total")
        self._ai_ask(
            "Write a digest of these eBay listings below. You can call "
            "ebay_search to compare prices or find more options; it is paid "
            "($0.015 per search), so use it only if needed.",
            ebay_digest_prompt(self._ebay_label, rows, total),
            display=f"Write a digest of the eBay {self._ebay_label} listings.")
        self._busy(f"AI is digesting {min(len(rows), 15)} eBay listings…")

    def _ebay_ask_ai(self):
        p = self._ebay_selected
        if not p:
            self._error("select an eBay listing first")
            return
        seller = p.get("seller") or {}
        specs = "; ".join(f"{s.get('name')}: {s.get('value')}"
                          for s in (p.get("specs") or [])[:30])
        shipping = p.get("shipping_label") or ""
        if p.get("shipping_options"):
            shipping = "; ".join(
                " · ".join(x for x in (o.get("label"), o.get("via")) if x)
                for o in p["shipping_options"][:4])
        context = (
            f"eBay tab — {self._ebay_label}\n"
            f"Listing: {p.get('title') or ''}\n"
            f"Price: {p.get('price_label') or 'not stated'}\n"
            f"Condition: {p.get('condition') or ''}\n"
            f"Buying format: {p.get('buying_format_text') or ''}\n"
            f"Shipping: {shipping}\n"
            f"Location: {p.get('location') or ''}\n"
            f"Returns: {p.get('returns') or ''}\n"
            f"Seller: {seller.get('name') or seller.get('username') or ''}"
            f" ({seller.get('positive_percent') or '?'}% positive, "
            f"{seller.get('feedback_count') or '?'} feedback)\n"
            f"Details: {specs}\n"
            f"URL: {p.get('url') or ''}\n"
            f"\nDescription:\n{(p.get('description') or '')[:4000]}\n"
            f"\nOn screen:\n{self._text(self.ebay_detail)[:8000]}"
        )
        self._ai_ask(
            "Explain this eBay listing: what is being sold, whether the "
            "asking price and shipping look fair for the item's condition, "
            "how trustworthy the seller looks, what to check before buying, "
            "and what to ask the seller. Use ebay_search to compare similar "
            "listings if that helps; it is paid ($0.015 per search).",
            context,
            display=f"Explain this eBay listing: {p.get('title', '')}")

    # -- command palette (Ctrl+K) ---------------------------------------------

    def _tab_index(self, label: str) -> int:
        for i in range(self.tabs.get_n_pages()):
            if self.tabs.get_tab_label_text(self.tabs.get_nth_page(i)) == label:
                return i
        return 0

    def _palette_static(self) -> list[tuple[str, str, str, object]]:
        labels = [self.tabs.get_tab_label_text(self.tabs.get_nth_page(i))
                  for i in range(self.tabs.get_n_pages())]
        rows = [(f"Go to {label}",
                 f"switch to the {label} tab ({self._tab_shortcut(i)})",
                 "tab", i) for i, label in enumerate(labels)]
        rows += [
            ("New AI chat", "clear the conversation", "ai_new", None),
            ("Refresh team and balance",
             "reload the team and prepaid balance", "refresh", None),
        ]
        return rows

    def _build_palette(self):
        dlg = Gtk.Dialog()
        dlg.set_title("Command palette")
        dlg.set_transient_for(self)
        dlg.set_destroy_with_parent(True)
        dlg.set_modal(True)
        dlg.set_default_size(700, 460)
        dlg.add_button("Close", Gtk.ResponseType.CLOSE)
        dlg.connect("response", lambda d, r: d.hide())
        box = dlg.get_content_area()
        box.set_spacing(6)
        box.set_border_width(8)
        self.palette_entry = Gtk.Entry()
        self.palette_entry.set_placeholder_text(
            "Go to a tab, search a source, open a repo, or ask the AI…")
        box.pack_start(self.palette_entry, False, False, 0)
        self.palette_store = Gtk.ListStore(str, str)
        self.palette_view = Gtk.TreeView(model=self.palette_store)
        self.palette_view.set_can_focus(False)
        for header, idx in (("Action", 0), ("Details", 1)):
            col = Gtk.TreeViewColumn(header, Gtk.CellRendererText(), text=idx)
            if idx == 0:
                col.set_expand(True)
            self.palette_view.append_column(col)
        self.palette_view.connect(
            "row-activated",
            lambda v, p, c: self._palette_run(p.get_indices()[0]))
        box.pack_start(self._scrolled(self.palette_view), True, True, 0)
        self.palette_entry.connect("changed", lambda e: self._palette_filter())
        self.palette_entry.connect(
            "activate", lambda e: self._palette_run_selected())
        self.palette_entry.connect("key-press-event", self._palette_key)
        self._palette = dlg

    def _open_palette(self):
        if self._palette is None:
            self._build_palette()
        self.palette_entry.set_text("")
        self._palette_filter()
        self._palette.show_all()
        self.palette_entry.grab_focus()

    def _on_window_key(self, widget, event) -> bool:
        if (event.state & Gdk.ModifierType.CONTROL_MASK
                and event.keyval in (Gdk.KEY_k, Gdk.KEY_K)):
            self._open_palette()
            return True
        # Alt+<n> switches tabs, but leave Ctrl+Alt+<n> and Super+Alt+<n>
        # alone for the desktop environment.
        blocked = Gdk.ModifierType.CONTROL_MASK | Gdk.ModifierType.SUPER_MASK
        if event.state & Gdk.ModifierType.MOD1_MASK and not (event.state & blocked):
            index = self._alt_tab_index(event)
            if index is not None:
                if 0 <= index < self.tabs.get_n_pages():
                    self.tabs.set_current_page(index)
                return True
        return False

    @staticmethod
    def _digit_order() -> list[int]:
        # The digit row's unshifted keyvals in tab order: 1…9, 0.
        return [getattr(Gdk, f"KEY_{n}") for n in range(1, 10)] + [Gdk.KEY_0]

    def _alt_tab_index(self, event) -> int | None:
        """Alt+1…Alt+9, Alt+0 → tabs 0…9; Alt+Shift+1… → tabs 10, 11, …
        Uses the unshifted keyval so Shift+1 (which arrives as '!') still
        maps to tab ten."""
        base = self._base_keyval(event)
        order = self._digit_order()
        if base not in order:
            return None
        index = order.index(base)
        if event.state & Gdk.ModifierType.SHIFT_MASK:
            index += 10
        return index

    def _base_keyval(self, event) -> int:
        """The unshifted keyval of a key event (Shift+1 → 1)."""
        try:
            keymap = Gdk.Keymap.get_for_display(self.get_display())
            ok, keyval, _group, _level, _consumed = \
                keymap.translate_keyboard_state(
                    event.hardware_keycode, 0, event.group)
            if ok:
                return keyval
        except (AttributeError, TypeError, GLib.Error):
            pass
        return event.keyval

    def _palette_key(self, entry, event) -> bool:
        if event.state & Gdk.ModifierType.MOD1_MASK:
            if self._on_window_key(self, event):
                self._palette.hide()
                return True
        if event.keyval == Gdk.KEY_Escape:
            self._palette.hide()
            return True
        if event.keyval in (Gdk.KEY_Down, Gdk.KEY_Up):
            self._palette_move(1 if event.keyval == Gdk.KEY_Down else -1)
            return True
        return False

    def _palette_move(self, delta: int):
        n = len(self.palette_store)
        if not n:
            return
        model, it = self.palette_view.get_selection().get_selected()
        cur = model.get_path(it)[0] if it is not None else -1
        path = 0 if cur < 0 else cur + delta
        path = max(0, min(path, n - 1))
        self.palette_view.get_selection().select_path(
            Gtk.TreePath.new_from_string(str(path)))
        self.palette_view.scroll_to_cell(path, None, False, 0.0, 0.0)

    def _palette_filter(self):
        q = self.palette_entry.get_text().strip()
        rows: list[tuple[str, str, str, object]] = []
        if q:
            rows.append((f"Ask AI: {q}", "send this question to the AI tab",
                         "ask", q))
            slug = gh_repo_slug(q)
            if slug:
                rows.append((f"Open GitHub repo: {slug}",
                             "README, stats and issues", "gh_repo", slug))
            rows.append((f"Search GitHub for: {q}", "repositories",
                         "gh_search", q))
            rows.append((f"Search the catalog for: {q}", "treg endpoints",
                         "catalog", q))
            rows.append((f"Search the news for: {q}", "treg news search",
                         "news", q))
            rows.append((f"Search Hacker News for: {q}", "free Algolia index",
                         "hn", q))
            rows.append((f"Search OLX.pt for: {q}", "classifieds in Portugal",
                         "olx_search", q))
            if q.isdigit() or "olx.pt" in q:
                rows.append((f"Open OLX listing: {q}",
                             "price, place and details", "olx_listing", q))
            rows.append((f"Search eBay for: {q}", "listings (paid, $0.015)",
                         "ebay_search", q))
            if q.isdigit() or "ebay." in q:
                rows.append((f"Open eBay listing: {q}",
                             "full detail (paid, $0.015)", "ebay_listing", q))
            rows.append((f"Browse the web for: {q}", "treg web search",
                         "browse", q))
            if re.fullmatch(r"[A-Za-z][A-Za-z0-9.\-^=]{0,5}", q):
                rows.append((f"Market quote: {q.upper()}",
                             "stocks and ETFs", "markets", q))
        rows += [r for r in self._palette_static()
                 if not q or q.lower() in r[0].lower()]
        self._palette_rows = rows
        self.palette_store.clear()
        for display, detail, _action, _payload in rows:
            self.palette_store.append([display, detail])
        if rows:
            self.palette_view.get_selection().select_path(
                Gtk.TreePath.new_from_string("0"))
        else:
            self.palette_view.get_selection().unselect_all()

    def _palette_run_selected(self):
        model, it = self.palette_view.get_selection().get_selected()
        if it is None:
            return
        self._palette_run(model.get_path(it)[0])

    def _palette_run(self, index: int):
        if not (0 <= index < len(self._palette_rows)):
            return
        _display, _detail, action, payload = self._palette_rows[index]
        self._palette.hide()
        self._palette_do(action, payload)

    def _palette_do(self, action: str, payload):
        if action == "tab":
            self.tabs.set_current_page(int(payload))
        elif action == "ai_new":
            self.tabs.set_current_page(self._tab_index("AI"))
            self._ai_new_chat()
        elif action == "refresh":
            self._refresh_identity()
        elif action == "ask":
            self._ai_ask(str(payload))
        elif action == "browse":
            self.tabs.set_current_page(self._tab_index("Browse"))
            self.browse_query.set_text(str(payload))
            self._do_browse_search()
        elif action == "catalog":
            self.tabs.set_current_page(self._tab_index("Catalog"))
            self.search_edit.set_text(str(payload))
            self._do_search()
        elif action == "news":
            self.tabs.set_current_page(self._tab_index("News"))
            self.news_mode.set_active(_NEWS_MODES.index("search"))
            self.news_entry.set_text(str(payload))
            self._news_go()
        elif action == "hn":
            self.tabs.set_current_page(self._tab_index("Hacker News"))
            self.hn_mode.set_active(_HN_MODES.index("search"))
            self.hn_entry.set_text(str(payload))
            self._hn_go()
        elif action == "gh_search":
            self.tabs.set_current_page(self._tab_index("GitHub"))
            self.gh_mode.set_active(_GH_MODES.index("search"))
            self.gh_entry.set_text(str(payload))
            self._gh_go()
        elif action == "gh_repo":
            self.tabs.set_current_page(self._tab_index("GitHub"))
            self.gh_mode.set_active(_GH_MODES.index("repo"))
            self.gh_entry.set_text(str(payload))
            self._gh_go()
        elif action == "olx_search":
            self.tabs.set_current_page(self._tab_index("OLX"))
            self.olx_mode.set_active_id("search")
            self.olx_entry.set_text(str(payload))
            self._olx_go()
        elif action == "olx_listing":
            self.tabs.set_current_page(self._tab_index("OLX"))
            self.olx_mode.set_active_id("listing")
            self.olx_entry.set_text(str(payload))
            self._olx_go()
        elif action == "ebay_search":
            self.tabs.set_current_page(self._tab_index("eBay"))
            self.ebay_mode.set_active_id("search")
            self.ebay_entry.set_text(str(payload))
            self._ebay_go()
        elif action == "ebay_listing":
            self.tabs.set_current_page(self._tab_index("eBay"))
            self.ebay_mode.set_active_id("listing")
            self.ebay_entry.set_text(str(payload))
            self._ebay_go()
        elif action == "markets":
            self.tabs.set_current_page(self._tab_index("Markets"))
            self.m_mode.set_active(0)
            self.m_entry.set_text(str(payload))
            self._m_go()

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
    win = MainWindow(application=app)
    win.show_all()  # re-shows everything, so re-apply the Markets mode visibility
    win._m_sync_visibility()


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