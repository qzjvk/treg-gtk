"""Thin REST client for the treg tool catalog (https://treg.to).

All methods are synchronous and blocking — call them from a worker thread,
never from the GUI thread.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from urllib.parse import quote

import httpx

DEFAULT_BASE_URL = "https://treg.to"
CONFIG_PATH = Path.home() / ".treg" / "config.json"


class TregError(RuntimeError):
    """A treg call failed. `status` is the HTTP status when one was received."""

    def __init__(self, message: str, status: int | None = None, body: object = None):
        super().__init__(message)
        self.status = status
        self.body = body


def load_credentials() -> tuple[str, str]:
    """(base_url, token) from the treg CLI config, with env overrides."""
    base_url, token = DEFAULT_BASE_URL, ""
    if CONFIG_PATH.is_file():
        try:
            raw = json.loads(CONFIG_PATH.read_text())
            base_url = raw.get("base_url") or base_url
            token = raw.get("token") or ""
        except (ValueError, OSError):
            pass
    return (
        os.environ.get("TREG_URL") or base_url,
        os.environ.get("TREG_TOKEN") or token,
    )


def _usd(micro: int | float | None) -> str:
    if micro is None:
        return "—"
    return f"${micro / 1_000_000:,.4f}".rstrip("0").rstrip(".") if micro else "$0"


class TregClient:
    def __init__(self, base_url: str, token: str, timeout: float = 90.0):
        if not token:
            raise TregError("no treg token — run `treg login` or set TREG_TOKEN")
        self.base_url = base_url.rstrip("/")
        self._http = httpx.Client(
            base_url=self.base_url,
            headers={"X-Treg-Token": token},
            timeout=timeout,
        )

    def close(self) -> None:
        self._http.close()

    # -- identity & money ---------------------------------------------------

    def me(self) -> dict:
        """GET /auth/me — who the token is, and its org."""
        return self._get("/auth/me")

    def orgs(self) -> list[dict]:
        """GET /orgs — teams this token belongs to."""
        return self._get("/orgs")

    def balance(self, org_id: int, limit: int = 20) -> dict:
        """GET /orgs/{id}/balance — prepaid balance, blocks and recent ledger."""
        return self._get(f"/orgs/{org_id}/balance", params={"limit": limit})

    # -- catalog ------------------------------------------------------------

    def platforms(self) -> dict:
        """GET /catalog/platforms — every platform, busiest first.
        Returns {platforms: [...], providers: [...], generated_from}."""
        return self._get("/catalog/platforms")

    def platform(self, slug: str) -> dict:
        """GET /catalog/platforms/{slug} — one platform: capabilities and all its endpoints."""
        return self._get(f"/catalog/platforms/{quote(slug, safe='')}")

    def search(self, query: str, limit: int = 25, *, engine: str = "agent",
               added_within_days: int | None = None, sort: str | None = None) -> dict:
        """GET /catalog/search — ranked free-text search over the catalog."""
        params: dict = {"q": query, "limit": limit, "engine": engine}
        if added_within_days is not None:
            params["added_within_days"] = added_within_days
        if sort:
            params["sort"] = sort
        return self._get("/catalog/search", params=params)

    def endpoint(self, endpoint_id: str) -> dict:
        """GET /catalog/endpoints/{id} — one endpoint in full (params, cost, siblings)."""
        return self._get(f"/catalog/endpoints/{quote(endpoint_id, safe='')}")

    # -- calls ---------------------------------------------------------------

    def call(self, target: str, method: str = "GET", params: list[tuple[str, str]] | None = None,
             body: str | None = None, headers: dict[str, str] | None = None) -> httpx.Response:
        """POST/GET /call/{target} — a catalog endpoint id or a team tool URL.

        Returns the raw httpx.Response so the caller can read the X-Treg-*
        headers (cost, call id, async descriptor) alongside the body.
        """
        req_headers = dict(headers or {})
        if body is not None:
            try:
                json.loads(body)
                req_headers.setdefault("content-type", "application/json")
            except ValueError:
                pass
        return self._http.request(method, f"/call/{target.strip('/')}",
                                  params=params or None, content=body, headers=req_headers)

    def calls(self, limit: int = 25) -> list[dict]:
        """GET /calls — the proxied-call audit log."""
        return self._get("/calls", params={"limit": limit})

    def call_result(self, call_id: str) -> dict:
        """GET /calls/{id}/result — the stored answer of an earlier call."""
        return self._get(f"/calls/{quote(call_id, safe='')}/result")

    # -- internals ------------------------------------------------------------

    def _get(self, path: str, params: dict | None = None) -> object:
        r = self._http.get(path, params=params)
        if r.status_code >= 400:
            raise TregError(_error_text(r), status=r.status_code, body=_safe_json(r))
        try:
            return r.json()
        except ValueError:
            return r.text


def _safe_json(r: httpx.Response) -> object:
    try:
        return r.json()
    except ValueError:
        return r.text[:500]


def _error_text(r: httpx.Response) -> str:
    data = _safe_json(r)
    if isinstance(data, dict):
        detail = data.get("detail")
        if detail:
            return f"HTTP {r.status_code}: {detail if isinstance(detail, str) else json.dumps(detail)}"
    return f"HTTP {r.status_code}: {r.text[:300]}"


# ---------------------------------------------------------------------------
# async task polling (mirrors the treg CLI's descriptor semantics)
# ---------------------------------------------------------------------------

def json_path(doc: object, dotted: str) -> object:
    """Read a dotted path (a.b.0.c) out of a decoded JSON document."""
    cur = doc
    for part in dotted.split("."):
        if isinstance(cur, dict):
            cur = cur.get(part)
        elif isinstance(cur, list) and part.isdigit() and int(part) < len(cur):
            cur = cur[int(part)]
        else:
            return None
    return cur


def classify(descriptor: dict, doc: object) -> str:
    rule = descriptor.get("status") or {}
    v = json_path(doc, str(rule.get("path") or ""))
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

    task_id = json_path(submitted, str(descriptor.get("id_from") or ""))
    if task_id in (None, ""):
        return {"error": "submission response does not contain the async task id",
                "response": submission}
    task_id = str(task_id)

    poll = descriptor.get("poll") or {}
    if poll.get("endpoint"):
        rule = poll["param"]
        target, params = str(rule["in"]), [(str(rule["name"]), task_id)]
    else:
        url = json_path(submitted, str(poll.get("url_from") or ""))
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
        return str(json_path(doc, str(status_rule.get("path") or "")))

    # The submission may already be the finished answer: no poll needed.
    if descriptor.get("terminal_on_submission") is True and classify(descriptor, submitted) == "success":
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

        outcome = classify(descriptor, doc)
        status = status_of(doc)
        if outcome == "success":
            out: dict = {"response": r, "status": status, "task_id": task_id}
            rule = descriptor.get("result") or {}
            if rule.get("path"):
                value = json_path(doc, str(rule["path"]))
                out["result"] = value
                cands = value if isinstance(value, list) else [value]
                out["result_url"] = next(
                    (c for c in cands if isinstance(c, str) and c.startswith(("http://", "https://"))),
                    None)
            else:
                fr = rule.get("fetch_param") or {}
                fv = json_path(doc, str(fr.get("value_from") or ""))
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