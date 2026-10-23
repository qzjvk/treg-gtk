"""OLX.pt for the OLX tab and the AI tools.

Free and direct through OLX.pt's public JSON API — the same endpoint the
website itself uses. No treg calls, no charges and no token. The API blocks
httpx's default User-Agent, so a descriptive one is sent; be gentle (one
request per search, no crawling loops).

Price sorting uses the API's `filter_float_price:asc|desc` and is applied
once more to the returned batch so promoted listings (which OLX pins to
fixed slots) don't break the visible order. OLX's date sorting is not
reliable through this API, so the tab offers relevance and price only.

Everything is synchronous and blocking — call it from a worker thread,
never from the GUI thread. Functions return plain dicts and lists.
"""

from __future__ import annotations

import html
import re
from urllib.parse import urlsplit

import httpx

from .client import TregError
from .news import parse_time, relative_time

API = "https://www.olx.pt/api/v1/offers"
SITE = "https://www.olx.pt"
UA = "treg-console/1.5 (+https://treg.to)"
TIMEOUT = 20.0
PAGE_TIMEOUT = 30.0
MAX_LIMIT = 50

#: (display name, category id, url slug) for OLX.pt's top-level categories.
#: The ids are embedded in each category page and are stable.
CATEGORIES = (
    ("All categories", None, ""),
    ("Cars, motos & boats", 362, "carros-motos-e-barcos"),
    ("Real estate", 16, "imoveis"),
    ("Tech & computers", 11, "tecnologia-e-informatica"),
    ("Jobs", 190, "emprego"),
    ("Home & garden", 13, "moveis-casa-e-jardim"),
    ("Fashion", 14, "moda"),
    ("Baby & children", 99, "bebes-criancas"),
    ("Sports & leisure", 12, "desporto-e-lazer"),
    ("Animals", 10, "animais"),
    ("Services", 191, "servicos"),
    ("Buy & sell", 185, "compra-venda"),
    ("Equipment & tools", 4918, "equipamentos-e-ferramentas"),
    ("Parts & accessories", 5478, "pecas-e-acessorios"),
    ("Agriculture", 4800, "agricultura"),
    ("Leisure", 26, "lazer"),
)

CATEGORY_BY_ID = {cid: name for name, cid, _slug in CATEGORIES if cid}

#: Common English/Portuguese words for the AI and the command palette.
_CATEGORY_ALIASES = {
    "car": 362, "cars": 362, "vehicles": 362, "motos": 362,
    "motorcycles": 362, "auto": 362,
    "property": 16, "properties": 16, "realestate": 16, "real-estate": 16,
    "houses": 16, "apartments": 16, "rentals": 16, "imoveis": 16,
    "electronics": 11, "computers": 11, "phones": 11, "tech": 11,
    "jobs": 190, "work": 190, "employment": 190,
    "home": 13, "garden": 13, "furniture": 13,
    "fashion": 14, "clothes": 14, "clothing": 14,
    "baby": 99, "kids": 99, "children": 99,
    "sports": 12, "sport": 12,
    "animals": 10, "pets": 10,
    "services": 191,
    "buy": 185, "sell": 185, "marketplace": 185,
    "tools": 4918, "equipment": 4918,
    "parts": 5478, "accessories": 5478,
    "agriculture": 4800, "farming": 4800,
    "leisure": 26, "games": 26,
}

#: semantic sort name -> the API's sort_by value
SORTS = {
    "relevance": "relevance:desc",
    "cheapest": "filter_float_price:asc",
    "price:asc": "filter_float_price:asc",
    "expensive": "filter_float_price:desc",
    "price:desc": "filter_float_price:desc",
}

_BR_RE = re.compile(r"(?i)<\s*br\s*/?\s*>\s*")
_TAG_RE = re.compile(r"<[^>]+>")
_SPACE_RE = re.compile(r"[ \t]+")
_MULTI_NL_RE = re.compile(r"\n{3,}")
_SKU_RE = re.compile(r'"sku"\s*:\s*"(\d+)"')
_ID_LABEL_RE = re.compile(r"ID:\s*(?:<!--\s*-->)?\s*(\d{5,})")
_ID_JSON_RE = re.compile(r'"id"\s*:\s*(\d{5,})')


# -- text helpers -----------------------------------------------------------

def _plain(text: object) -> str:
    """OLX's HTML description → readable plain text, keeping line breaks."""
    s = _BR_RE.sub("\n", str(text or ""))
    s = _TAG_RE.sub(" ", s)
    s = html.unescape(s)
    s = _SPACE_RE.sub(" ", s)
    s = _MULTI_NL_RE.sub("\n\n", s)
    return s.strip()


def _snippet(text: object, limit: int = 240) -> str:
    s = " ".join(_plain(text).split())
    if len(s) <= limit:
        return s
    cut = s[:limit]
    if " " in cut:
        cut = cut.rsplit(" ", 1)[0]
    return cut.rstrip(" ,.;:") + "…"


# -- normalization ----------------------------------------------------------

def _price(raw: dict) -> dict | None:
    """The structured price from the listing's params (None when absent)."""
    for p in raw.get("params") or []:
        if not isinstance(p, dict):
            continue
        if p.get("key") != "price" and p.get("type") != "price":
            continue
        v = p.get("value")
        if not isinstance(v, dict):
            continue
        return {
            "value": v.get("value"),
            "currency": v.get("currency") or "EUR",
            "label": v.get("label") or "",
            "negotiable": bool(v.get("negotiable")),
            "budget": bool(v.get("budget")),
        }
    return None


def _attrs(raw: dict) -> dict:
    """The listing's extra parameters (condition, rooms, mileage, …)."""
    out: dict[str, str] = {}
    for p in raw.get("params") or []:
        if not isinstance(p, dict):
            continue
        key = str(p.get("key") or "").strip()
        if not key or key == "price":
            continue
        v = p.get("value")
        if isinstance(v, dict):
            label = v.get("label") or v.get("key") or v.get("value")
        else:
            label = v
        if label is None or str(label).strip() == "":
            continue
        out[str(p.get("name") or key).strip()] = str(label).strip()
    return out


def _location(raw: dict) -> dict:
    loc = raw.get("location") or {}
    city = loc.get("city") or {}
    region = loc.get("region") or {}
    city_name = city.get("name") or ""
    region_name = region.get("name") or ""
    return {
        "city": city_name,
        "city_id": city.get("id"),
        "region": region_name,
        "region_id": region.get("id"),
        "label": city_name or region_name,
    }


def _photos(raw: dict, limit: int = 12) -> list[str]:
    out: list[str] = []
    for p in raw.get("photos") or []:
        if not isinstance(p, dict):
            continue
        link = p.get("link")
        if link:
            out.append(str(link).replace("{width}x{height}", "1000x1000"))
    return out[:limit]


def _seller(raw: dict) -> dict:
    user = raw.get("user") or {}
    return {
        "id": user.get("id"),
        "name": user.get("name") or "",
        "created": user.get("created") or "",
        "business": bool(raw.get("business")),
    }


def listing_row(raw: dict, detail: bool = False) -> dict | None:
    """One API listing → the row shape used by the tab and the tools."""
    if not isinstance(raw, dict) or not raw.get("id"):
        return None
    price = _price(raw)
    loc = _location(raw)
    cat = raw.get("category") or {}
    cat_id = cat.get("id")
    ts = parse_time(raw.get("created_time"))
    title = str(raw.get("title") or "").strip() or "(untitled)"
    desc = _plain(raw.get("description"))
    meta_bits: list[str] = []
    if price and price.get("label"):
        meta_bits.append(price["label"])
    if loc["label"]:
        meta_bits.append(loc["label"])
    if ts:
        meta_bits.append(relative_time(ts))
    return {
        "title": title,
        "subtitle": _snippet(raw.get("description"), 240),
        "meta": " · ".join(meta_bits),
        "id": raw.get("id"),
        "url": raw.get("url") or "",
        "description": desc if detail else _snippet(raw.get("description"), 600),
        "price": price,
        "price_label": (price or {}).get("label") or "",
        "location": loc,
        "category_id": cat_id,
        "category": CATEGORY_BY_ID.get(cat_id, ""),
        "offer_type": raw.get("offer_type") or "",
        "status": raw.get("status") or "",
        "is_business": bool(raw.get("business")),
        "seller": _seller(raw),
        "attrs": _attrs(raw),
        "photos": _photos(raw),
        "photo_count": len(raw.get("photos") or []),
        "created": raw.get("created_time") or "",
        "refreshed": raw.get("last_refresh_time") or "",
        "ts": ts,
        "promoted": False,
    }


# -- categories -------------------------------------------------------------

def resolve_category(value) -> int | None:
    """A category id, display name, slug or common alias → id (None = all)."""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value or None
    s = str(value).strip()
    if not s or s.lower() in ("all", "any", "*", "todas", "todos"):
        return None
    if s.isdigit():
        return int(s) or None
    low = s.lower()
    for name, cid, slug in CATEGORIES:
        if cid is None:
            continue
        if low == name.lower() or low == slug.lower():
            return cid
    if low in _CATEGORY_ALIASES:
        return _CATEGORY_ALIASES[low]
    hits = [cid for name, cid, _slug in CATEGORIES
            if cid and low in name.lower()]
    if len(hits) == 1:
        return hits[0]
    raise TregError(f"OLX: unknown category {value!r} — "
                    "ask for olx_categories to list them")


def categories() -> list[dict]:
    """OLX.pt's top-level categories (name, id, slug)."""
    return [{"name": name, "id": cid, "slug": slug}
            for name, cid, slug in CATEGORIES if cid]


# -- HTTP -------------------------------------------------------------------

def _headers() -> dict:
    return {
        "User-Agent": UA,
        "Accept": "application/json",
        "Accept-Language": "pt-PT,pt;q=0.9,en;q=0.6",
    }


def _get(path: str, params: dict | None = None) -> object:
    url = API + path
    try:
        r = httpx.get(url, params=params or None, headers=_headers(),
                      timeout=TIMEOUT, follow_redirects=True)
    except httpx.HTTPError as e:
        raise TregError(f"OLX.pt request failed: {e}") from e
    if r.status_code == 404:
        raise TregError("OLX.pt: listing not found")
    if r.status_code == 403:
        raise TregError("OLX.pt refused the request (HTTP 403) — "
                        "try again shortly")
    if r.status_code == 429:
        raise TregError("OLX.pt rate limit — wait a moment and try again")
    if r.status_code >= 400:
        detail = ""
        try:
            body = r.json()
            if isinstance(body, dict):
                detail = str(body.get("detail") or body.get("title") or "")
                val = body.get("validation")
                if not detail and isinstance(val, list) and val:
                    first = val[0] if isinstance(val[0], dict) else {}
                    detail = str(first.get("message") or "")
        except ValueError:
            pass
        raise TregError(f"OLX.pt returned HTTP {r.status_code}"
                        + (f": {detail}" if detail else ""))
    try:
        return r.json()
    except ValueError:
        raise TregError("OLX.pt returned non-JSON")


def _get_page(url: str) -> str:
    headers = {
        "User-Agent": UA,
        "Accept": "text/html,application/xhtml+xml",
        "Accept-Language": "pt-PT,pt;q=0.9,en;q=0.6",
    }
    try:
        r = httpx.get(url, headers=headers, timeout=PAGE_TIMEOUT,
                      follow_redirects=True)
    except httpx.HTTPError as e:
        raise TregError(f"OLX.pt page request failed: {e}") from e
    if r.status_code == 404:
        raise TregError("OLX.pt: listing not found")
    if r.status_code >= 400:
        raise TregError(f"OLX.pt returned HTTP {r.status_code}")
    return r.text


# -- search / listing -------------------------------------------------------

def _number(value, what: str) -> float | None:
    if value in (None, ""):
        return None
    try:
        n = float(value)
    except (TypeError, ValueError):
        raise TregError(f"OLX: {what} must be a number")
    if n < 0:
        raise TregError(f"OLX: {what} cannot be negative")
    return n


def _sort_value(sort: str) -> str:
    s = str(sort or "relevance").strip().lower()
    return SORTS.get(s, s if ":" in s else SORTS["relevance"])


def _sort_rows(rows: list[dict], sort: str) -> list[dict]:
    """Apply the price order to the fetched batch (promoted pins ignored)."""
    key = str(sort or "relevance").strip().lower()
    if key in ("cheapest", "price:asc"):
        reverse = False
    elif key in ("expensive", "price:desc"):
        reverse = True
    else:
        return rows

    def price_of(row: dict) -> float | None:
        v = (row.get("price") or {}).get("value")
        try:
            return float(v)
        except (TypeError, ValueError):
            return None

    priced = [r for r in rows if price_of(r) is not None]
    unpriced = [r for r in rows if price_of(r) is None]
    priced.sort(key=price_of, reverse=reverse)
    return priced + unpriced


def search(query: str = "", category=None, region_id=None, city_id=None,
           price_min=None, price_max=None, sort: str = "relevance",
           limit: int = 25, offset: int = 0) -> dict:
    """Search OLX.pt (or browse a category when the query is empty)."""
    q = str(query or "").strip()
    cat_id = resolve_category(category)
    pmin = _number(price_min, "minimum price")
    pmax = _number(price_max, "maximum price")
    if pmin is not None and pmax is not None and pmin > pmax:
        raise TregError("OLX: minimum price is above the maximum")
    try:
        limit = max(1, min(int(limit), MAX_LIMIT))
        offset = max(0, int(offset))
    except (TypeError, ValueError):
        raise TregError("OLX: limit and offset must be integers")

    params: dict = {"offset": offset, "limit": limit,
                    "sort_by": _sort_value(sort)}
    if q:
        params["query"] = q
    if cat_id:
        params["category_id"] = cat_id
    try:
        if region_id:
            params["region_id"] = int(region_id)
        if city_id:
            params["city_id"] = int(city_id)
    except (TypeError, ValueError):
        raise TregError("OLX: region_id and city_id must be integers")
    if pmin is not None:
        params["filter_float_price:from"] = pmin
    if pmax is not None:
        params["filter_float_price:to"] = pmax

    doc = _get("", params)
    data = doc.get("data") if isinstance(doc, dict) else None
    if not isinstance(data, list):
        raise TregError("OLX.pt returned no listings")
    meta = doc.get("metadata") or {}
    promoted = set((meta.get("source") or {}).get("promoted") or [])
    rows: list[dict] = []
    for i, raw in enumerate(data):
        row = listing_row(raw)
        if row is None:
            continue
        if i in promoted:
            row["promoted"] = True
            row["meta"] = (row["meta"] + " · promoted").lstrip(" ·")
        rows.append(row)
    rows = _sort_rows(rows, sort)[:limit]
    total = (meta.get("visible_total_count")
             or meta.get("total_elements") or len(rows))
    return {
        "query": q,
        "category": CATEGORY_BY_ID.get(cat_id, "") if cat_id else
        "All categories",
        "category_id": cat_id,
        "sort": str(sort or "relevance"),
        "offset": offset,
        "count": len(rows),
        "total": int(total),
        "rows": rows,
    }


def resolve_listing_id(value) -> int:
    """A numeric id, or an olx.pt URL whose page carries the numeric id."""
    s = str(value or "").strip()
    if not s:
        raise TregError("OLX: enter a listing id or URL")
    if s.isdigit():
        return int(s)
    if s.startswith(("http://", "https://")):
        url = s
    elif "olx.pt" in s:
        url = "https://" + s.lstrip("/")
    else:
        raise TregError("OLX: enter a numeric listing id or an olx.pt URL")
    host = (urlsplit(url).hostname or "").lower()
    if host != "olx.pt" and not host.endswith(".olx.pt"):
        raise TregError("OLX: enter an olx.pt URL")
    page = _get_page(url)
    for rx in (_SKU_RE, _ID_LABEL_RE, _ID_JSON_RE):
        m = rx.search(page)
        if m:
            return int(m.group(1))
    raise TregError("OLX: could not find the listing id in that URL")


def listing(value) -> dict:
    """One listing in full, by numeric id or olx.pt URL."""
    lid = resolve_listing_id(value)
    doc = _get(f"/{lid}")
    raw = doc.get("data") if isinstance(doc, dict) else None
    row = listing_row(raw, detail=True) if isinstance(raw, dict) else None
    if row is None:
        raise TregError(f"OLX: no listing {lid}")
    return row


def digest_prompt(label: str, rows: list[dict], total: int | None = None,
                  limit: int = 15) -> str:
    """The local-model prompt for a digest of the current OLX rows."""
    head = f"OLX.pt — {label}"
    if total:
        head += f" ({total:,} listings in total)"
    lines = [head, ""]
    for i, row in enumerate(rows[:limit], 1):
        bits = [row.get("price_label") or "price not stated"]
        loc = (row.get("location") or {}).get("label") or ""
        if loc:
            bits.append(loc)
        if row.get("promoted"):
            bits.append("promoted")
        lines.append(f"{i}. {row.get('title', '')} — {' · '.join(bits)}")
        if row.get("subtitle"):
            lines.append(f"   {row['subtitle']}")
    lines += [
        "",
        "Write a concise digest in 4-6 sentences of Markdown. Group the "
        "listings by what they are, give the price range, point out the most "
        "interesting options and their locations, and flag anything that "
        "looks like a scam, a parts-only listing or an unrealistic price. Do "
        "not list URLs or repeat titles verbatim.",
    ]
    return "\n".join(lines)
