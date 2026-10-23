"""News for the News tab: aggregation, dedupe, ranking and search.

The topic views (Top, Tech, Business) fetch public RSS/Atom feeds directly
— no treg calls, no charges, and they work without a treg token. Search
goes through treg's routed news search (`treg.web.search.news`), which is
the way to search for an arbitrary query.

Everything is synchronous and blocking — call it from a worker thread,
never from the GUI thread. Functions return plain dicts and lists so the
GUI thread can render them directly (no third-party feed parser needed).
"""

from __future__ import annotations

import html
import json
import re
import time
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from urllib.parse import parse_qsl, urlencode, urljoin, urlsplit, urlunsplit

import httpx

from .client import TregError

UA = "Mozilla/5.0 (X11; Linux x86_64) treg-console/1.1"
TIMEOUT = 15.0
MAX_PER_FEED = 60

#: (source, feed URL) per topic. Keep the lists short: each view fetches
#: every feed in parallel, and a handful of outlets is enough to make a
#: useful, deduped front page.
FEEDS: dict[str, list[tuple[str, str]]] = {
    "top": [
        ("BBC News", "https://feeds.bbci.co.uk/news/rss.xml"),
        ("NPR", "https://feeds.npr.org/1001/rss.xml"),
        ("Al Jazeera", "https://www.aljazeera.com/xml/rss/all.xml"),
        ("Guardian World", "https://www.theguardian.com/world/rss"),
        ("NYT World", "https://rss.nytimes.com/services/xml/rss/nyt/World.xml"),
    ],
    "tech": [
        ("Ars Technica", "https://feeds.arstechnica.com/arstechnica/index"),
        ("TechCrunch", "https://techcrunch.com/feed/"),
        ("Wired", "https://www.wired.com/feed/rss"),
        ("Hacker News", "https://hnrss.org/frontpage"),
        ("Guardian Tech", "https://www.theguardian.com/uk/technology/rss"),
    ],
    "business": [
        ("CNBC", "https://www.cnbc.com/id/100003114/device/rss/rss.html"),
        ("MarketWatch", "https://feeds.content.dowjones.io/public/rss/mw_topstories"),
        ("Financial Times", "https://www.ft.com/rss/home"),
        ("NYT Business", "https://rss.nytimes.com/services/xml/rss/nyt/Business.xml"),
        ("Guardian Business", "https://www.theguardian.com/uk/business/rss"),
    ],
}

TOPIC_LABELS = {
    "top": "Top stories",
    "tech": "Technology",
    "business": "Business",
    "search": "Search",
}

_DATE_FORMATS = (
    "%a, %d %b %Y %H:%M:%S %z",
    "%a, %d %b %Y %H:%M:%S %Z",
    "%a, %d %b %Y %H:%M:%S",
    "%Y-%m-%dT%H:%M:%S%z",
    "%Y-%m-%dT%H:%M:%SZ",
    "%Y-%m-%dT%H:%M:%S",
    "%Y-%m-%d %H:%M:%S UTC",
    "%Y-%m-%d %H:%M:%S",
    "%Y-%m-%d",
)

_RELATIVE_RE = re.compile(
    r"(\d+|an?)\s*(minute|hour|day|week|month|year)s?\s+ago", re.I)
_TRACKING_KEYS = {
    "fbclid", "gclid", "mc_cid", "mc_eid", "ocid", "ref", "referrer",
    "cmpid", "smid", "partner", "at_medium", "at_campaign",
}
_TAG_RE = re.compile(r"<[^>]+>")
_IMG_RE = re.compile(r"<img[^>]+src=[\"']([^\"']+)[\"']", re.I)
_SPACE_RE = re.compile(r"\s+")


def _local(tag: str) -> str:
    """An XML tag's local name, lowercased (namespaces stripped)."""
    return tag.rsplit("}", 1)[-1].lower()


def _plain(text: str) -> str:
    """HTML fragment → one line of plain text."""
    text = html.unescape(_TAG_RE.sub(" ", str(text or "")))
    return _SPACE_RE.sub(" ", text).strip()


def _snippet(text: str, limit: int = 300) -> str:
    text = _plain(text)
    if len(text) <= limit:
        return text
    cut = text[:limit]
    if " " in cut:
        cut = cut.rsplit(" ", 1)[0]
    return cut.rstrip(" ,.;:") + "…"


def parse_time(value) -> float | None:
    """A feed/search timestamp → Unix seconds (UTC). Handles ISO dates,
    RFC-822 feed dates, epoch seconds/millis and relative strings like
    '2 hours ago'. None when it cannot be parsed."""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        v = float(value)
        if v > 1e11:  # milliseconds
            v /= 1000.0
        return v
    s = str(value).strip()
    if not s:
        return None
    low = s.lower()
    if low in ("now", "just now", "today"):
        return time.time()
    if low == "yesterday":
        return time.time() - 86_400
    m = _RELATIVE_RE.search(low)
    if m:
        n = 1 if m.group(1).lower() in ("a", "an") else int(m.group(1))
        unit = m.group(2).lower()
        seconds = {
            "minute": 60, "hour": 3600, "day": 86_400,
            "week": 604_800, "month": 2_592_000, "year": 31_536_000,
        }[unit]
        return time.time() - n * seconds
    iso = s[:-1] + "+00:00" if s.endswith("Z") else s
    try:
        dt = datetime.fromisoformat(iso)
    except ValueError:
        dt = None
    if dt is not None:
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.timestamp()
    for fmt in _DATE_FORMATS:
        try:
            dt = datetime.strptime(s, fmt)
        except ValueError:
            continue
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.timestamp()
    return None


def relative_time(ts: float | None, fallback: str = "") -> str:
    """A timestamp → a compact '12m ago' / '3h ago' / 'Oct 21' label."""
    if ts is None:
        return fallback
    delta = max(0.0, time.time() - ts)
    if delta < 90:
        return "just now"
    if delta < 3600:
        return f"{int(delta // 60)}m ago"
    if delta < 86_400:
        return f"{int(delta // 3600)}h ago"
    if delta < 7 * 86_400:
        return f"{int(delta // 86_400)}d ago"
    try:
        dt = datetime.fromtimestamp(ts, timezone.utc)
    except (OverflowError, OSError, ValueError):
        return fallback
    if dt.year == datetime.now(timezone.utc).year:
        return dt.strftime("%b %d")
    return dt.strftime("%b %d, %Y")


def canonical_url(url: str) -> str:
    """A URL stripped of tracking parameters and host noise, for dedupe."""
    try:
        parts = urlsplit(str(url or "").strip())
    except ValueError:
        return str(url or "").strip()
    if not parts.netloc:
        return str(url or "").strip()
    host = parts.netloc.lower()
    if host.startswith("www."):
        host = host[4:]
    query = []
    for k, v in parse_qsl(parts.query, keep_blank_values=True):
        kl = k.lower()
        if kl.startswith("utm_") or kl in _TRACKING_KEYS:
            continue
        query.append((k, v))
    path = parts.path.rstrip("/") or "/"
    return urlunsplit((parts.scheme.lower() or "https", host, path,
                       urlencode(query), ""))


def _title_key(title: str) -> str:
    """Normalized title for near-duplicate matching."""
    s = _plain(title).lower()
    s = re.sub(r"^(live|breaking|update|exclusive)\s*[:\-–—]\s*", "", s)
    return " ".join(re.sub(r"[^a-z0-9]+", " ", s).split())


def _similar(a: str, b: str) -> bool:
    """True when two normalized titles share most of their words."""
    wa, wb = set(a.split()), set(b.split())
    if not wa or not wb:
        return False
    return len(wa & wb) / len(wa | wb) >= 0.8


def _text(elem: ET.Element, *names: str) -> str:
    """First child whose local name is in `names` (text or Atom href)."""
    wanted = {n.lower() for n in names}
    for child in elem:
        if _local(child.tag) not in wanted:
            continue
        if child.get("href"):
            return child.get("href") or ""
        if child.text and child.text.strip():
            return child.text.strip()
    return ""


def _image_of(elem: ET.Element, description: str) -> str | None:
    """The first plausible image URL of a feed item."""
    for node in elem.iter():
        name = _local(node.tag)
        url = (node.get("url") or "").strip()
        if not url:
            continue
        if name in ("thumbnail", "image"):
            return url
        if name in ("content", "enclosure"):
            kind = (node.get("type") or "").lower()
            medium = (node.get("medium") or "").lower()
            if medium == "image" or kind.startswith("image/"):
                return url
    m = _IMG_RE.search(description or "")
    return m.group(1).strip() if m else None


def _item_url(elem: ET.Element) -> str:
    url = _text(elem, "link")
    if url:
        return urljoin("https://example.invalid/", url)
    for child in elem:
        if _local(child.tag) == "link" and child.get("href"):
            return child.get("href") or ""
    return _text(elem, "guid", "id")


def parse_feed(data: bytes | str, source: str) -> list[dict]:
    """Parse an RSS 2.0 / Atom / RSS 1.0 document into story rows."""
    try:
        root = ET.fromstring(data)
    except ET.ParseError:
        return []
    rows: list[dict] = []
    for elem in root.iter():
        if _local(elem.tag) not in ("item", "entry"):
            continue
        title = _plain(_text(elem, "title"))
        url = _item_url(elem).strip()
        if not title or not url:
            continue
        raw_desc = _text(elem, "description", "summary", "content", "encoded")
        published = _text(elem, "pubdate", "published", "updated", "date",
                          "created")
        snippet = _snippet(raw_desc)
        if _title_key(snippet) == _title_key(title):
            snippet = ""
        rows.append({
            "title": title,
            "url": url,
            "source": source,
            "snippet": snippet,
            "published": published,
            "ts": parse_time(published),
            "image": _image_of(elem, raw_desc),
            "position": None,
        })
        if len(rows) >= MAX_PER_FEED:
            break
    return rows


def aggregate(items: list[dict], limit: int = 25) -> list[dict]:
    """Dedupe stories across sources, merge duplicates, then rank.

    Duplicates match on canonical URL or a near-identical title. A merged
    row keeps the first (usually highest-ranked) title/URL, the richest
    snippet and image, the newest timestamp, and the union of sources.
    Rank rewards corroboration (how many feeds carried the story), recency
    and the provider/front-page position when one is known.
    """
    groups: list[dict] = []
    for raw in items:
        if not isinstance(raw, dict):
            continue
        title = _plain(raw.get("title") or "")
        url = str(raw.get("url") or "").strip()
        if not title or not url:
            continue
        key = _title_key(title)
        canon = canonical_url(url)
        hit = None
        for g in groups:
            if canon and canon == g["_canon"]:
                hit = g
                break
            if _similar(key, g["_key"]):
                hit = g
                break
        source = str(raw.get("source") or "").strip()
        ts = parse_time(raw.get("ts")) if raw.get("ts") is not None else None
        pos = raw.get("position")
        if hit is None:
            groups.append({
                "title": title,
                "url": url,
                "snippet": _snippet(raw.get("snippet") or ""),
                "sources": [source] if source else [],
                "published": str(raw.get("published") or ""),
                "ts": ts,
                "image": raw.get("image"),
                "position": pos,
                "_canon": canon,
                "_key": key,
            })
            continue
        if source and source not in hit["sources"]:
            hit["sources"].append(source)
        if len(raw.get("snippet") or "") > len(hit["snippet"]):
            hit["snippet"] = _snippet(raw["snippet"])
        if not hit.get("image") and raw.get("image"):
            hit["image"] = raw["image"]
        if ts and (not hit["ts"] or ts > hit["ts"]):
            hit["ts"] = ts
        if isinstance(pos, (int, float)) and not isinstance(pos, bool):
            if hit.get("position") is None or pos < hit["position"]:
                hit["position"] = pos

    now = time.time()
    for g in groups:
        age_h = max(0.0, (now - (g["ts"] or now)) / 3600.0)
        recency = 1.0 / (1.0 + age_h / 12.0)
        pos = g.get("position")
        order = max(0.0, 1.0 - (float(pos) - 1.0) / 50.0) if pos else 0.5
        corroboration = min(len(g["sources"]), 4)
        g["rank"] = round(corroboration * 1.2 + recency + order * 0.6, 3)
        g["source_label"] = _source_label(g["sources"])
        g.pop("_canon", None)
        g.pop("_key", None)

    groups.sort(key=lambda g: (g["rank"], g["ts"] or 0), reverse=True)
    return groups[:limit]


def _source_label(sources: list[str]) -> str:
    if not sources:
        return ""
    if len(sources) == 1:
        return sources[0]
    if len(sources) == 2:
        return f"{sources[0]} + {sources[1]}"
    return f"{sources[0]} +{len(sources) - 1}"


def _read_feed(source: str, url: str) -> list[dict]:
    r = httpx.get(
        url, timeout=TIMEOUT, follow_redirects=True,
        headers={"User-Agent": UA,
                 "Accept": "application/rss+xml, application/atom+xml, "
                           "application/xml, text/xml;q=0.9, */*;q=0.8"})
    if r.status_code >= 400:
        raise TregError(f"HTTP {r.status_code}")
    return parse_feed(r.content, source)


def fetch_topic(topic: str, limit: int = 25) -> dict:
    """Fetch a topic's feeds in parallel and return ranked, deduped rows.

    Returns {items, feeds, errors}; `errors` lists the outlets that could
    not be fetched, so one dead feed never sinks the whole view.
    """
    feeds = FEEDS.get(topic)
    if not feeds:
        raise TregError(f"unknown news topic {topic!r}")
    items: list[dict] = []
    errors: list[str] = []
    with ThreadPoolExecutor(max_workers=min(8, len(feeds))) as pool:
        futures = {pool.submit(_read_feed, source, url): source
                   for source, url in feeds}
        for fut in as_completed(futures):
            source = futures[fut]
            try:
                items.extend(fut.result())
            except Exception as e:  # one bad feed must not sink the view
                errors.append(f"{source}: {e}")
    return {
        "items": aggregate(items, limit=max(1, int(limit))),
        "feeds": len(feeds),
        "errors": errors,
    }


# -- treg-backed search -----------------------------------------------------

def _response_error(r: httpx.Response) -> str:
    try:
        body = r.json()
    except ValueError:
        return r.text[:240]
    if isinstance(body, dict):
        detail = body.get("detail")
        if isinstance(detail, str) and detail:
            return detail
        if detail is not None:
            return json.dumps(detail)[:240]
    return f"HTTP {r.status_code}"


def _search_rows(doc: object) -> list[dict]:
    """Story rows from the varied treg news response shapes."""
    out = doc.get("output", doc) if isinstance(doc, dict) else doc
    if isinstance(out, dict):
        for key in ("results", "items", "articles", "news", "rows"):
            rows = out.get(key)
            if isinstance(rows, list):
                return [r for r in rows if isinstance(r, dict)]
    if isinstance(out, list):
        return [r for r in out if isinstance(r, dict)]
    return []


def _search_item(row: dict) -> dict:
    def first(*keys):
        for k in keys:
            v = row.get(k)
            if v not in (None, ""):
                return v
        return None

    url = str(first("url", "link", "href") or "").strip()
    source = str(first("publisher", "source", "site_name", "site", "domain")
                 or "").strip()
    raw_date = first("published_at", "published", "date", "age", "created",
                     "created_at")
    snippet = _snippet(first("snippet", "summary", "description", "content")
                       or "")
    title = _plain(first("title", "headline", "name") or "")
    if _title_key(snippet) == _title_key(title):
        snippet = ""
    return {
        "title": title,
        "url": url,
        "source": source,
        "snippet": snippet,
        "published": str(raw_date or ""),
        "ts": parse_time(raw_date),
        "image": first("image", "thumbnail", "image_url"),
        "position": first("position", "rank"),
    }


def search_news(client, query: str, limit: int = 25) -> dict:
    """Search recent news through treg (blocking).

    Returns {items, served_by, cost_micro, errors}; charged by the routed
    provider that served the call.
    """
    q = (query or "").strip()
    if not q:
        raise TregError("enter a query to search the news")
    body = json.dumps({"q": q, "limit": max(1, min(int(limit), 100))})
    r = client.call("treg.web.search.news", "POST", None, body)
    if r.status_code >= 400:
        raise TregError(f"HTTP {r.status_code}: {_response_error(r)}",
                        status=r.status_code)
    try:
        doc = r.json()
    except ValueError:
        raise TregError("the news search response was not JSON")
    rows = [_search_item(row) for row in _search_rows(doc)]
    cost = r.headers.get("X-Treg-Cost-Micro")
    return {
        "items": aggregate(rows, limit=max(1, int(limit))),
        "served_by": r.headers.get("X-Treg-Served-By") or "",
        "cost_micro": int(cost) if cost and cost.isdigit() else None,
        "errors": [],
        "query": q,
    }


def digest_prompt(label: str, rows: list[dict], limit: int = 12) -> str:
    """The local-model prompt for a digest of the current stories."""
    lines = [f"News stories — {label}", ""]
    for i, row in enumerate(rows[:limit], 1):
        line = f"{i}. {row.get('title', '')}"
        source = row.get("source_label") or row.get("source")
        if source:
            line += f" — {source}"
        when = relative_time(row.get("ts"), row.get("published") or "")
        if when:
            line += f" ({when})"
        lines.append(line)
        if row.get("snippet"):
            lines.append(f"   {row['snippet']}")
    lines += [
        "",
        "Write a concise news digest in 4-6 sentences of Markdown. Group "
        "related stories together, name the key people, companies and "
        "numbers, and note disagreement between sources where it matters. "
        "Do not list URLs or repeat the headlines verbatim.",
    ]
    return "\n".join(lines)
