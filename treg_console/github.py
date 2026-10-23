"""GitHub for the GitHub tab and the AI tools.

Free and direct through the public REST API (https://api.github.com) — no
treg calls, no charges, and it works without a treg token. Unauthenticated
requests are rate-limited (60/hour); export `GITHUB_TOKEN` to raise that to
5,000/hour. "Trending" is GitHub's own search over recently created repos
sorted by stars — there is no official trending endpoint.

Everything is synchronous and blocking — call it from a worker thread,
never from the GUI thread. Functions return plain dicts and lists.
"""

from __future__ import annotations

import base64
import datetime as _dt
import os
import time
from urllib.parse import urlsplit

import httpx

from .client import TregError
from .news import parse_time, relative_time

API = "https://api.github.com"
TIMEOUT = 20.0
MAX_PER_PAGE = 100

TOKEN = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN") or ""

UA = "treg-console/1.5 (+https://treg.to)"


def _headers() -> dict:
    headers = {
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": UA,
    }
    if TOKEN:
        headers["Authorization"] = f"Bearer {TOKEN}"
    return headers


def _get(path: str, params: dict | None = None) -> object:
    url = path if path.startswith(("http://", "https://")) else API + path
    try:
        r = httpx.get(url, params=params or None, timeout=TIMEOUT,
                      follow_redirects=True, headers=_headers())
    except httpx.HTTPError as e:
        raise TregError(f"GitHub request failed: {e}") from e
    if r.status_code == 404:
        raise TregError("GitHub: not found — check the owner/repo or query")
    if r.status_code == 403 and "rate limit" in r.text.lower():
        reset = r.headers.get("X-RateLimit-Reset") or ""
        when = ""
        if reset.isdigit():
            when = time.strftime(" (resets at %H:%M)",
                                 time.localtime(int(reset)))
        hint = "set GITHUB_TOKEN to raise it to 5,000/hour" if not TOKEN else \
            "try again later"
        raise TregError(f"GitHub rate limit reached{when} — {hint}")
    if r.status_code >= 400:
        detail = ""
        try:
            body = r.json()
            if isinstance(body, dict):
                detail = str(body.get("message") or "")
        except ValueError:
            pass
        raise TregError(f"GitHub returned HTTP {r.status_code}"
                        + (f": {detail}" if detail else ""))
    try:
        return r.json()
    except ValueError:
        raise TregError("GitHub returned non-JSON")


# -- helpers ----------------------------------------------------------------

def _snippet(text: object, limit: int = 280) -> str:
    s = " ".join(str(text or "").split())
    if len(s) <= limit:
        return s
    cut = s[:limit]
    if " " in cut:
        cut = cut.rsplit(" ", 1)[0]
    return cut.rstrip(" ,.;:") + "…"


def _num(n) -> str:
    try:
        n = int(n or 0)
    except (TypeError, ValueError):
        return "0"
    if n >= 1_000_000:
        return f"{n / 1_000_000:.1f}m".replace(".0", "")
    if n >= 1_000:
        return f"{n / 1_000:.1f}k".replace(".0", "")
    return str(n)


def repo_slug(value: str) -> str:
    """'owner/name', a github.com URL or 'owner/name.git' → 'owner/name'.

    Returns '' when the value does not look like a repository.
    """
    s = str(value or "").strip()
    if not s:
        return ""
    if "://" in s or s.startswith("github.com/"):
        try:
            parts = urlsplit(s if "://" in s else "https://" + s)
        except ValueError:
            return ""
        if parts.netloc.lower() not in ("github.com", "www.github.com"):
            return ""
        s = parts.path.strip("/")
    s = s.rstrip("/")
    if s.endswith(".git"):
        s = s[:-4]
    bits = [b for b in s.split("/") if b]
    if len(bits) < 2:
        return ""
    return f"{bits[0]}/{bits[1]}"


def _when(ts) -> str:
    return relative_time(ts) if ts else ""


# -- normalization ----------------------------------------------------------

def repo_row(raw: dict) -> dict | None:
    """A GitHub repository object → a display/tool row."""
    if not isinstance(raw, dict) or not raw.get("full_name"):
        return None
    owner = (raw.get("owner") or {}).get("login") or ""
    ts = parse_time(raw.get("pushed_at") or raw.get("updated_at"))
    meta_bits = [f"{_num(raw.get('stargazers_count'))} stars",
                 f"{_num(raw.get('forks_count'))} forks"]
    if raw.get("language"):
        meta_bits.append(str(raw["language"]))
    if ts:
        meta_bits.append(f"updated {relative_time(ts)}")
    return {
        "title": raw["full_name"],
        "subtitle": _snippet(raw.get("description"), 220),
        "meta": " · ".join(meta_bits),
        "id": raw.get("id"),
        "full_name": raw["full_name"],
        "name": raw.get("name") or "",
        "owner": owner,
        "description": raw.get("description") or "",
        "url": raw.get("html_url") or "",
        "stars": int(raw.get("stargazers_count") or 0),
        "forks": int(raw.get("forks_count") or 0),
        "open_issues": int(raw.get("open_issues_count") or 0),
        "watchers": int(raw.get("subscribers_count") or 0),
        "language": raw.get("language") or "",
        "topics": list(raw.get("topics") or []),
        "license": ((raw.get("license") or {}).get("spdx_id")
                    or (raw.get("license") or {}).get("name") or ""),
        "default_branch": raw.get("default_branch") or "",
        "archived": bool(raw.get("archived")),
        "fork": bool(raw.get("fork")),
        "ts": ts,
    }


def _issue_row(raw: dict, repo: str) -> dict | None:
    if not isinstance(raw, dict) or not raw.get("number"):
        return None
    is_pr = "pull_request" in raw
    labels = [str((l or {}).get("name")) for l in (raw.get("labels") or [])
              if isinstance(l, dict) and l.get("name")]
    ts = parse_time(raw.get("updated_at") or raw.get("created_at"))
    meta_bits = ["pull request" if is_pr else "issue",
                 str(raw.get("state") or "open")]
    if raw.get("comments"):
        meta_bits.append(f"{_num(raw['comments'])} comments")
    if ts:
        meta_bits.append(f"updated {relative_time(ts)}")
    return {
        "title": f"#{raw['number']} {raw.get('title') or ''}".strip(),
        "subtitle": " · ".join(labels) or _snippet(raw.get("body"), 160),
        "meta": " · ".join(meta_bits),
        "number": int(raw["number"]),
        "repo": repo,
        "url": raw.get("html_url") or "",
        "state": raw.get("state") or "",
        "user": (raw.get("user") or {}).get("login") or "",
        "labels": labels,
        "comments": int(raw.get("comments") or 0),
        "is_pr": is_pr,
        "body": _snippet(raw.get("body"), 1200),
        "ts": ts,
    }


def _release_row(raw: dict, repo: str) -> dict | None:
    if not isinstance(raw, dict):
        return None
    tag = raw.get("tag_name") or ""
    name = raw.get("name") or tag
    if not tag and not name:
        return None
    ts = parse_time(raw.get("published_at") or raw.get("created_at"))
    bits = [tag] if tag else []
    author = (raw.get("author") or {}).get("login") or ""
    if author:
        bits.append(author)
    if raw.get("prerelease"):
        bits.append("prerelease")
    if ts:
        bits.append(relative_time(ts))
    return {
        "title": name,
        "subtitle": _snippet(raw.get("body"), 200),
        "meta": " · ".join(bits),
        "tag": tag,
        "repo": repo,
        "url": raw.get("html_url") or "",
        "author": author,
        "prerelease": bool(raw.get("prerelease")),
        "body": _snippet(raw.get("body"), 2000),
        "ts": ts,
    }


def _readme(slug: str) -> str:
    """The repo's README as plain Markdown ('' when there is none)."""
    try:
        doc = _get(f"/repos/{slug}/readme")
    except TregError:
        return ""
    content = doc.get("content") if isinstance(doc, dict) else ""
    if not content:
        return ""
    try:
        raw = base64.b64decode(content)
        return raw.decode("utf-8", "replace")
    except (ValueError, TypeError):
        return ""


# -- views ------------------------------------------------------------------

def search_repos(query: str, limit: int = 25, sort: str = "stars") -> list[dict]:
    """Search repositories through GitHub's search API (free)."""
    q = (query or "").strip()
    if not q:
        raise TregError("enter a query to search GitHub")
    limit = max(1, min(int(limit), MAX_PER_PAGE))
    sort = sort if sort in ("stars", "forks", "updated", "help-wanted-issues") \
        else "stars"
    doc = _get("/search/repositories", {
        "q": q, "sort": sort, "order": "desc", "per_page": limit,
    })
    items = doc.get("items") if isinstance(doc, dict) else None
    if not isinstance(items, list):
        raise TregError("GitHub search returned no results")
    return [r for r in (repo_row(i) for i in items) if r]


def trending(days: int = 7, limit: int = 25) -> list[dict]:
    """Recently created repos sorted by stars — the closest thing to the
    trending page the free API offers."""
    days = max(1, min(int(days), 365))
    limit = max(1, min(int(limit), MAX_PER_PAGE))
    since = (_dt.date.today() - _dt.timedelta(days=days)).isoformat()
    doc = _get("/search/repositories", {
        "q": f"created:>={since}", "sort": "stars", "order": "desc",
        "per_page": limit,
    })
    items = doc.get("items") if isinstance(doc, dict) else None
    if not isinstance(items, list):
        raise TregError("GitHub trending returned no results")
    return [r for r in (repo_row(i) for i in items) if r]


def repo(value: str) -> dict:
    """One repository with its README: {repo, readme}."""
    slug = repo_slug(value)
    if not slug:
        raise TregError("enter a repo as owner/name or a github.com URL")
    raw = _get(f"/repos/{slug}")
    row = repo_row(raw) if isinstance(raw, dict) else None
    if row is None:
        raise TregError(f"GitHub: no repository {slug}")
    return {"repo": row, "readme": _readme(slug)}


def issues(value: str, limit: int = 25, state: str = "open") -> list[dict]:
    """Open (or closed) issues and pull requests of a repo."""
    slug = repo_slug(value)
    if not slug:
        raise TregError("enter a repo as owner/name or a github.com URL")
    state = state if state in ("open", "closed", "all") else "open"
    limit = max(1, min(int(limit), MAX_PER_PAGE))
    doc = _get(f"/repos/{slug}/issues", {
        "state": state, "sort": "updated", "direction": "desc",
        "per_page": limit,
    })
    if not isinstance(doc, list):
        raise TregError("GitHub returned no issues")
    return [r for r in (_issue_row(i, slug) for i in doc) if r]


def releases(value: str, limit: int = 20) -> list[dict]:
    """A repo's releases, newest first."""
    slug = repo_slug(value)
    if not slug:
        raise TregError("enter a repo as owner/name or a github.com URL")
    limit = max(1, min(int(limit), MAX_PER_PAGE))
    doc = _get(f"/repos/{slug}/releases", {"per_page": limit})
    if not isinstance(doc, list):
        raise TregError("GitHub returned no releases")
    return [r for r in (_release_row(i, slug) for i in doc) if r]


def digest_prompt(label: str, rows: list[dict], limit: int = 12) -> str:
    """The local-model prompt for a digest of the current GitHub rows."""
    lines = [f"GitHub — {label}", ""]
    for i, row in enumerate(rows[:limit], 1):
        lines.append(f"{i}. {row.get('title', '')} — {row.get('meta', '')}")
        if row.get("subtitle"):
            lines.append(f"   {row['subtitle']}")
    lines += [
        "",
        "Write a concise digest in 4-6 sentences of Markdown. Group related "
        "projects, say what each one is for and why it stands out (stars, "
        "activity, language, the problem it solves), and call out anything "
        "notable in issues or releases. Do not list URLs or repeat titles "
        "verbatim.",
    ]
    return "\n".join(lines)
