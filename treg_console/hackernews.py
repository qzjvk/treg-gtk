"""Hacker News for the Hacker News tab and the AI tools.

Free and direct — no treg calls, no charges, and it works without a treg
token. Two public sources are used:

  * the official Firebase API (story lists, items, comment trees) —
    https://github.com/HackerNews/API
  * the Algolia search API for arbitrary queries —
    https://hn.algolia.com/api

Everything is synchronous and blocking — call it from a worker thread,
never from the GUI thread. Functions return plain dicts and lists so the
GUI thread can render them directly.
"""

from __future__ import annotations

import html
import re
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import urlsplit

import httpx

from .client import TregError
from .news import parse_time, relative_time

UA = "treg-console/1.3 (+https://treg.to)"
TIMEOUT = 15.0
MAX_FETCH = 90  # at most this many items in one list view
BASE = "https://hacker-news.firebaseio.com/v0"
ALGOLIA = "https://hn.algolia.com/api/v1"
ITEM_URL = "https://news.ycombinator.com/item?id={}"

#: mode -> Firebase list name. The order matches the combo box.
FEEDS = {
    "top": "topstories",
    "new": "newstories",
    "best": "beststories",
    "ask": "askstories",
    "show": "showstories",
    "jobs": "jobstories",
}

FEED_LABELS = {
    "top": "Top stories",
    "new": "Newest",
    "best": "Best",
    "ask": "Ask HN",
    "show": "Show HN",
    "jobs": "Jobs",
    "search": "Search",
}

_TAG_RE = re.compile(r"<[^>]+>")
_SPACE_RE = re.compile(r"\s+")


def _plain(text: object) -> str:
    """Hacker News' HTML fragment → one line of plain text."""
    text = html.unescape(_TAG_RE.sub(" ", str(text or "")))
    return _SPACE_RE.sub(" ", text).strip()


def _snippet(text: object, limit: int = 280) -> str:
    s = _plain(text)
    if len(s) <= limit:
        return s
    cut = s[:limit]
    if " " in cut:
        cut = cut.rsplit(" ", 1)[0]
    return cut.rstrip(" ,.;:") + "…"


def site_of(url: str) -> str:
    """The hostname of a URL without the www. prefix ('' when none)."""
    try:
        host = urlsplit(str(url or "")).netloc.lower()
    except ValueError:
        return ""
    return host[4:] if host.startswith("www.") else host


# -- HTTP -------------------------------------------------------------------

def _get_json(url: str, params: dict | None = None) -> object:
    try:
        r = httpx.get(url, params=params or None, timeout=TIMEOUT,
                      follow_redirects=True, headers={"User-Agent": UA})
    except httpx.HTTPError as e:
        raise TregError(f"Hacker News request failed: {e}") from e
    if r.status_code >= 400:
        raise TregError(f"Hacker News returned HTTP {r.status_code}")
    try:
        return r.json()
    except ValueError:
        raise TregError("Hacker News returned non-JSON")


def item(item_id: int) -> dict:
    """One raw Hacker News item (story, comment, poll…) by id ({} on a miss)."""
    doc = _get_json(f"{BASE}/item/{int(item_id)}.json")
    return doc if isinstance(doc, dict) else {}


def _item_safe(item_id: int) -> dict | None:
    """item() that returns None instead of raising (for parallel fetches)."""
    try:
        raw = item(item_id)
    except TregError:
        return None
    return raw or None


def _fetch_items(ids: list) -> list:
    """Fetch items in parallel, preserving the input order (None for misses)."""
    ids = [i for i in ids if isinstance(i, int)]
    if not ids:
        return []
    with ThreadPoolExecutor(max_workers=min(10, len(ids))) as pool:
        return list(pool.map(_item_safe, ids))


# -- normalization ----------------------------------------------------------

def story_row(raw: dict) -> dict | None:
    """A raw Firebase item → a story row (None when it isn't a live story)."""
    if not isinstance(raw, dict) or raw.get("deleted") or raw.get("dead"):
        return None
    sid = raw.get("id")
    title = _plain(raw.get("title"))
    if sid is None or not title:
        return None
    url = str(raw.get("url") or "").strip()
    return {
        "id": int(sid),
        "title": title,
        "url": url or ITEM_URL.format(sid),
        "hn_url": ITEM_URL.format(sid),
        "site": site_of(url) if url else "",
        "score": int(raw.get("score") or 0),
        "by": str(raw.get("by") or ""),
        "ts": float(raw["time"]) if raw.get("time") else None,
        "comments": int(raw.get("descendants") or 0),
        "text": _snippet(raw.get("text")),
        "type": str(raw.get("type") or "story"),
        "self_post": not url,
    }


def item_view(item_id: int) -> dict:
    """One item (story, Ask/Show post or comment) as a plain dict."""
    raw = item(item_id)
    if not raw:
        raise TregError(f"no Hacker News item {item_id}")
    url = str(raw.get("url") or "").strip()
    return {
        "id": raw.get("id"),
        "type": str(raw.get("type") or ""),
        "title": _plain(raw.get("title")),
        "by": str(raw.get("by") or ""),
        "ts": float(raw["time"]) if raw.get("time") else None,
        "score": int(raw.get("score") or 0),
        "comments": int(raw.get("descendants") or 0),
        "site": site_of(url) if url else "",
        "url": url or ITEM_URL.format(raw.get("id")),
        "hn_url": ITEM_URL.format(raw.get("id")),
        "text": _plain(raw.get("text")),
        "kids": len(raw.get("kids") or []),
    }


def _hit_row(hit: dict) -> dict | None:
    """An Algolia search hit → a story row."""
    if not isinstance(hit, dict):
        return None
    title = _plain(hit.get("title") or hit.get("story_title"))
    sid = hit.get("objectID")
    if not title or sid is None:
        return None
    try:
        sid = int(sid)
    except (TypeError, ValueError):
        return None
    url = str(hit.get("url") or "").strip()
    return {
        "id": sid,
        "title": title,
        "url": url or ITEM_URL.format(sid),
        "hn_url": ITEM_URL.format(sid),
        "site": site_of(url) if url else "",
        "score": int(hit.get("points") or 0),
        "by": str(hit.get("author") or ""),
        "ts": parse_time(hit.get("created_at")),
        "comments": int(hit.get("num_comments") or 0),
        "text": _snippet(hit.get("story_text")),
        "type": "story",
        "self_post": not url,
    }


# -- views ------------------------------------------------------------------

def feed(kind: str = "top", limit: int = 25) -> list[dict]:
    """The front page of one HN list (top/new/best/ask/show/jobs), ranked
    by HN's own order. Rows as in story_row()."""
    list_name = FEEDS.get(kind)
    if not list_name:
        raise TregError(f"unknown Hacker News feed {kind!r}")
    limit = max(1, min(int(limit), MAX_FETCH))
    ids = _get_json(f"{BASE}/{list_name}.json")
    if not isinstance(ids, list):
        raise TregError("Hacker News returned no story list")
    items = _fetch_items(list(ids)[:limit])
    return [r for r in (story_row(it) for it in items if it) if r]


def search(query: str, limit: int = 25) -> list[dict]:
    """Search HN stories by keyword through the free Algolia API."""
    q = (query or "").strip()
    if not q:
        raise TregError("enter a query to search Hacker News")
    limit = max(1, min(int(limit), MAX_FETCH))
    doc = _get_json(f"{ALGOLIA}/search",
                    {"query": q, "tags": "story", "hitsPerPage": limit})
    hits = doc.get("hits") if isinstance(doc, dict) else None
    if not isinstance(hits, list):
        raise TregError("Hacker News search returned no results")
    return [r for r in (_hit_row(h) for h in hits) if r]


def _walk(ids: list, depth: int, out: list[dict], limit: int,
          max_depth: int) -> None:
    """Depth-first walk of a comment forest, fetching each sibling batch in
    parallel but recursing into children in HN's own order."""
    if depth > max_depth or not ids or len(out) >= limit:
        return
    batch = [i for i in ids if isinstance(i, int)][: max(1, limit - len(out))]
    for raw in _fetch_items(batch):
        if len(out) >= limit:
            return
        if not raw or raw.get("deleted") or raw.get("dead"):
            continue
        text = _plain(raw.get("text"))
        if not text:
            continue
        out.append({
            "id": raw.get("id"),
            "by": str(raw.get("by") or ""),
            "ts": float(raw["time"]) if raw.get("time") else None,
            "depth": depth,
            "text": text,
            "kids": len(raw.get("kids") or []),
            "hn_url": ITEM_URL.format(raw.get("id")),
        })
        _walk(raw.get("kids") or [], depth + 1, out, limit, max_depth)


def comment_tree(item_id: int, limit: int = 60, max_depth: int = 8) -> dict:
    """A story's comment tree, flattened: comments carry a `depth` so the
    GUI can indent replies. {id, title, total, loaded, comments}."""
    root = item(item_id)
    if not root:
        raise TregError(f"no Hacker News item {item_id}")
    limit = max(1, min(int(limit), 200))
    out: list[dict] = []
    _walk(root.get("kids") or [], 1, out, limit, max_depth)
    return {
        "id": root.get("id"),
        "title": _plain(root.get("title")),
        "total": int(root.get("descendants") or 0),
        "loaded": len(out),
        "comments": out,
    }


def digest_prompt(label: str, rows: list[dict], limit: int = 12) -> str:
    """The local-model prompt for a digest of the current HN stories."""
    lines = [f"Hacker News — {label}", ""]
    for i, row in enumerate(rows[:limit], 1):
        meta = [f"{row.get('score') or 0} points"]
        if row.get("comments"):
            meta.append(f"{row['comments']} comments")
        if row.get("site"):
            meta.append(row["site"])
        if row.get("by"):
            meta.append(f"by {row['by']}")
        when = relative_time(row.get("ts"))
        if when:
            meta.append(when)
        lines.append(f"{i}. {row.get('title', '')} — {', '.join(meta)}")
        if row.get("text"):
            lines.append(f"   {row['text']}")
    lines += [
        "",
        "Write a concise digest of what the Hacker News community is talking "
        "about, in 4-6 sentences of Markdown. Group related stories, name the "
        "projects, companies and technologies involved, and call out the "
        "stories with the strongest discussion. Do not list URLs or repeat the "
        "titles verbatim.",
    ]
    return "\n".join(lines)
