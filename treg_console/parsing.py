"""Response parsing and normalization shared by the GUI and the AI tools.

Every function here is pure (no GUI, no network) except `reddit_call`, which
performs one retried treg call. Keeping these helpers in one module lets the
tabs and the AI agent present provider responses in the same shape.
"""

from __future__ import annotations

import json
import re
import time

import httpx

from .client import TregClient


def pretty_json(obj: object) -> str:
    try:
        return json.dumps(obj, indent=2, ensure_ascii=False)
    except (TypeError, ValueError):
        return str(obj)


# ---------------------------------------------------------------------------
# X (Twitter)
# ---------------------------------------------------------------------------

def x_norm_post(p: dict) -> dict:
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


def x_when(created) -> str:
    if created is None:
        return ""
    if isinstance(created, (int, float)) and not isinstance(created, bool):
        return time.strftime("%Y-%m-%d %H:%M", time.gmtime(created))
    return str(created)


# ---------------------------------------------------------------------------
# Reddit
# ---------------------------------------------------------------------------

def reddit_norm_post(p: dict) -> dict:
    """Normalize one Reddit post row to the tab's fixed shape. Two native
    shapes exist: the app's SubredditPost objects (postTitle, content,
    authorInfo) and scrapecreators' flat rows (title, selftext, author)."""
    def first(*keys):
        for k in keys:
            v = p.get(k)
            if v is not None:
                return v
        return None
    pid = str(first("name", "id") or "")
    if pid and not pid.startswith("t3_"):
        pid = f"t3_{pid}"  # the tikhub endpoints need the fullname
    author = first("author")
    if not author:
        author = ((p.get("authorInfo") or {}).get("name")) or ""
    sub = p.get("subreddit")
    if isinstance(sub, dict):
        sub = sub.get("prefixedName") or sub.get("name") or ""
    else:
        sub = sub or ""
    if sub and not sub.startswith("r/"):
        sub = f"r/{sub}"
    flair = p.get("flair")
    flair = flair.get("text") if isinstance(flair, dict) else p.get("link_flair_text")
    permalink = first("permalink")
    url = first("url") or (f"https://www.reddit.com{permalink}" if permalink else "")
    return {
        "id": pid,
        "title": first("title", "postTitle") or "",
        "body": first("selftext") or ((p.get("content") or {}).get("markdown")) or "",
        "author": author,
        "subreddit": sub,
        "created": first("created_utc", "createdAt"),
        "score": first("score") or 0,
        "comments": first("num_comments", "commentCount") or 0,
        "url": url or "",
        "flair": flair or "",
        "self_post": bool(first("isSelfPost")) or str(first("domain") or "").startswith("self."),
        "image": reddit_post_image(p),
    }


def reddit_when(created) -> str:
    if created is None:
        return ""
    if isinstance(created, (int, float)) and not isinstance(created, bool):
        return time.strftime("%Y-%m-%d %H:%M", time.gmtime(created))
    m = re.match(r"(\d{4}-\d{2}-\d{2})T(\d{2}:\d{2})", str(created))
    return f"{m.group(1)} {m.group(2)}" if m else str(created)


def reddit_post_image(p: dict) -> str | None:
    """First image URL of a post: the media still, a gallery image, or the
    thumbnail. Self (text) posts have no image."""
    if p.get("isSelfPost"):
        return None
    m = p.get("media")
    if isinstance(m, dict):
        still = (m.get("still") or {}).get("source")
        if isinstance(still, dict) and still.get("url"):
            return still["url"]
        images = m.get("images")
        if isinstance(images, list) and images and isinstance(images[0], dict):
            src = (images[0].get("source") or {}).get("url")
            if src:
                return src
    t = p.get("thumbnail")
    if isinstance(t, dict) and t.get("url"):
        return t["url"]
    if isinstance(t, str) and t:
        return t
    return None


def reddit_posts_of(body: object) -> list[dict]:
    """Extract post rows from the three Reddit response shapes:
    scrapecreators' flat `posts`, the dynamic search's edge/children posts,
    and the user feed's edge nodes."""
    if not isinstance(body, dict):
        return []
    if isinstance(body.get("posts"), list):
        return [p for p in body["posts"] if isinstance(p, dict)]
    data = body.get("data")
    if not isinstance(data, dict):
        return []
    posts: list[dict] = []
    search = data.get("search")
    main = (((search or {}).get("dynamic") or {}).get("components") or {}).get("main") or {}
    for edge in main.get("edges") or []:
        if not isinstance(edge, dict):
            continue
        node = edge.get("node") or {}
        children = node.get("children") if isinstance(node, dict) else None
        for child in children if children is not None else (edge.get("children") or []):
            if isinstance(child, dict) and isinstance(child.get("post"), dict):
                posts.append(child["post"])
    feed = (data.get("postFeed") or {}).get("elements") or {}
    for edge in feed.get("edges") or []:
        if isinstance(edge, dict) and isinstance(edge.get("node"), dict):
            posts.append(edge["node"])
    return posts


def call_error(r: httpx.Response) -> str | None:
    """Error text when a /call response came back with an error status
    (providers return 200 for real misses, but upstream failures like
    tikhub's 400 "please retry" arrive as error statuses). Else None."""
    if r.status_code < 400:
        return None
    try:
        body = r.json()
    except ValueError:
        return f"HTTP {r.status_code}"
    detail = body.get("detail") if isinstance(body, dict) else None
    if isinstance(detail, dict):
        return str(detail.get("message") or json.dumps(detail)[:200])
    if detail:
        return str(detail)
    return f"HTTP {r.status_code}: {str(body)[:200]}"


def reddit_call(client: TregClient, target: str, method: str,
                params: list[tuple[str, str]] | None,
                body: str | None) -> httpx.Response:
    """One Reddit call with a single retry after 2s. The providers (notably
    tikhub) intermittently fail with 400 "please retry", and treg's routing
    502s when every candidate for a team tool misses — a short retry
    usually clears both. Runs in a worker thread."""
    r = client.call(target, method, params, body)
    if call_error(r) is not None:
        time.sleep(2.0)
        r = client.call(target, method, params, body)
    return r


# ---------------------------------------------------------------------------
# web responses
# ---------------------------------------------------------------------------

def search_results(body: object) -> list[dict]:
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


def page_markdown(body: object) -> str:
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
