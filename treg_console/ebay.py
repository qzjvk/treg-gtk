"""eBay for the eBay tab and the AI tools.

Routed through treg's SerpApi eBay endpoints:

* ``serpapi.x.ebay-search`` — keyword/category search ($0.015 per success)
* ``serpapi.x.ebay-product`` — one listing by product id ($0.015 per success)

No direct eBay API and no scraping: eBay has no keyless official API and
blocks non-browser clients. The actual charge is read from treg's
``X-Treg-Cost-Micro`` response header and returned as ``cost_usd``.

A search answers with one page (SerpApi sends about 60 listings). The
requested limit and price order are applied to that batch; the full listing
detail is a second paid call, so the tab asks for it only when needed.

Everything is synchronous and blocking — call it from a worker thread, never
from the GUI thread. Functions return plain dicts and lists.
"""

from __future__ import annotations

import re
from urllib.parse import parse_qs, urlsplit

from .client import TregClient, TregError

SEARCH_ENDPOINT = "serpapi.x.ebay-search"
DETAIL_ENDPOINT = "serpapi.x.ebay-product"
SITE = "https://www.ebay.com"
MAX_LIMIT = 50
SEARCH_COST_USD = 0.015
DETAIL_COST_USD = 0.015

#: eBay sites whose /itm/<id> URLs we accept.
EBAY_HOSTS = {
    "ebay.com", "ebay.co.uk", "ebay.de", "ebay.fr", "ebay.it", "ebay.es",
    "ebay.nl", "ebay.be", "ebay.at", "ebay.ch", "ebay.ca", "ebay.com.au",
    "ebay.ie", "ebay.pl",
}

SORTS = {
    "relevance": "relevance",
    "best": "relevance",
    "cheapest": "cheapest",
    "price:asc": "cheapest",
    "expensive": "expensive",
    "price:desc": "expensive",
}

_FORMAT_LABELS = {
    "buy_it_now": "Buy It Now",
    "auction": "Auction",
    "best_offer": "Best Offer",
    "classified": "Classified",
}

_MONEY_SYMBOLS = {
    "USD": "$", "EUR": "€", "GBP": "£", "CAD": "C$", "AUD": "A$",
    "JPY": "¥", "CHF": "CHF ", "SEK": "SEK ", "PLN": "PLN ",
}

_ITEM_PATH_RE = re.compile(r"/itm/(?:[^/?#]+/)*(\d{6,})")


# -- small helpers ----------------------------------------------------------

def _text(value) -> str:
    if value is None:
        return ""
    return str(value).strip()


def _number(value) -> float | None:
    if value in (None, ""):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    match = re.search(r"-?\d[\d,]*(?:\.\d+)?", str(value))
    if not match:
        return None
    try:
        return float(match.group().replace(",", ""))
    except ValueError:
        return None


def _int(value) -> int | None:
    if value in (None, ""):
        return None
    try:
        return int(str(value).replace(",", "").strip())
    except (TypeError, ValueError):
        return None


def _currency(label) -> str:
    """The ISO code implied by a price label, when it can be told."""
    s = _text(label)
    if not s:
        return ""
    if "€" in s:
        return "EUR"
    if "£" in s:
        return "GBP"
    upper = s.upper()
    for code in ("USD", "EUR", "GBP", "CAD", "AUD", "JPY", "CHF", "SEK", "PLN"):
        if re.search(rf"\b{code}\b", upper):
            return code
    if "C $" in upper or "CAD $" in upper:
        return "CAD"
    if "AU $" in upper or "AUD $" in upper:
        return "AUD"
    if "$" in s:
        return "USD"
    return ""


def _format_money(value, currency) -> str:
    amount = _number(value)
    if amount is None:
        return ""
    code = _text(currency).upper()
    symbol = _MONEY_SYMBOLS.get(code)
    if symbol:
        return f"{symbol}{amount:,.2f}"
    return f"{amount:,.2f} {code}".strip()


def _clean_location(value) -> str:
    s = _text(value)
    return re.sub(r"^(?:located in|from)\s+", "", s, flags=re.I).strip()


def _set_meta(row: dict) -> None:
    bits: list[str] = []
    if row.get("price_label"):
        bits.append(row["price_label"])
    if row.get("condition"):
        bits.append(row["condition"])
    if row.get("location"):
        bits.append(row["location"])
    if row.get("sponsored"):
        bits.append("sponsored")
    row["meta"] = " · ".join(bits)


def _sort_key(sort) -> str:
    return SORTS.get(_text(sort).lower(), "relevance")


# -- treg calls -------------------------------------------------------------

def _error_detail(response) -> str:
    try:
        body = response.json()
    except ValueError:
        return _text(getattr(response, "text", ""))[:300]
    if isinstance(body, dict):
        for key in ("detail", "error", "message"):
            if body.get(key):
                return _text(body[key])
        errors = body.get("errors")
        if isinstance(errors, list) and errors:
            first = errors[0]
            if isinstance(first, dict):
                return _text(first.get("message") or first.get("longMessage"))
            return _text(first)
    return _text(body)[:300]


def _cost_usd(response, default: float = 0.0) -> float:
    raw = response.headers.get("X-Treg-Cost-Micro")
    try:
        return float(raw) / 1_000_000 if raw not in (None, "") else default
    except (TypeError, ValueError):
        return default


def _call(client: TregClient | None, target: str, method: str = "GET",
          params: list[tuple[str, str]] | None = None,
          body: str | None = None,
          default_cost: float = 0.0) -> tuple[dict, float]:
    if client is None:
        raise TregError("eBay: this needs a treg token — run `treg login` "
                        "or set TREG_TOKEN")
    try:
        r = client.call(target, method, params or None, body)
    except TregError:
        raise
    except Exception as e:
        raise TregError(f"eBay: treg call failed: {e}") from e
    if r.status_code >= 400:
        detail = _error_detail(r)
        raise TregError(
            f"eBay: {target} returned HTTP {r.status_code}"
            + (f": {detail}" if detail else ""),
            status=r.status_code)
    try:
        doc = r.json()
    except ValueError:
        raise TregError("eBay: treg returned non-JSON")
    if not isinstance(doc, dict):
        raise TregError("eBay: treg returned an unexpected response")
    if doc.get("error"):
        raise TregError(f"eBay: {doc['error']}")
    return doc, _cost_usd(r, default_cost)


# -- search normalization ---------------------------------------------------

def _search_price(raw: dict) -> dict:
    price = raw.get("price")
    value = None
    label = ""
    if isinstance(price, dict):
        value = _number(price.get("extracted"))
        label = _text(price.get("raw"))
    elif price is not None:
        label = _text(price)
        value = _number(label)
    currency = _currency(label)
    if not label and value is not None:
        label = _format_money(value, currency)
    return {"value": value, "currency": currency, "label": label}


def _search_shipping(raw) -> str:
    if isinstance(raw, dict):
        return _text(raw.get("raw")) or _format_money(
            raw.get("extracted"), _currency(raw.get("raw")))
    return _text(raw)


def search_row(raw: dict) -> dict | None:
    """One SerpApi eBay search result → the row shape used by the UI/tools."""
    if not isinstance(raw, dict):
        return None
    product_id = _text(raw.get("product_id"))
    if not product_id:
        return None
    price = _search_price(raw)
    seller_raw = raw.get("seller") if isinstance(raw.get("seller"), dict) else {}
    seller = {
        "username": _text(seller_raw.get("username")),
        "name": "",
        "feedback_count": seller_raw.get("reviews"),
        "positive_percent": seller_raw.get("positive_feedback_in_percentage"),
        "sold_count": None,
    }
    old = raw.get("old_price")
    if isinstance(old, dict):
        old_label = _text(old.get("raw")) or _format_money(
            old.get("extracted"), _currency(old.get("raw")))
    else:
        old_label = _text(old)
    thumbnail = _text(raw.get("thumbnail"))
    extensions = [_text(x) for x in (raw.get("extensions") or []) if _text(x)]
    condition = _text(raw.get("condition"))
    row = {
        "title": _text(raw.get("title")) or "(untitled)",
        "subtitle": _text(raw.get("subtitle")) or condition,
        "id": product_id,
        "url": f"{SITE}/itm/{product_id}",
        "description": _text(raw.get("subtitle")),
        "price": price,
        "price_label": price["label"],
        "old_price_label": old_label,
        "condition": condition,
        "shipping_label": _search_shipping(raw.get("shipping")),
        "shipping_options": [],
        "location": _clean_location(raw.get("location")),
        "returns": _text(raw.get("returns")),
        "seller": seller,
        "buying_format": _text(raw.get("buying_format")),
        "buying_format_text": _text(raw.get("buying_format_text")),
        "sponsored": bool(raw.get("sponsored")),
        "watchers": _text(raw.get("watchers")),
        "sold": _text(raw.get("quantity_sold")),
        "rating": raw.get("rating"),
        "reviews": raw.get("reviews"),
        "extensions": extensions,
        "thumbnail": thumbnail,
        "photos": [thumbnail] if thumbnail else [],
        "specs": [],
        "source": "SerpApi eBay search",
        "endpoint": SEARCH_ENDPOINT,
        "full": False,
    }
    _set_meta(row)
    return row


def _sort_rows(rows: list[dict], sort) -> list[dict]:
    key = _sort_key(sort)
    if key == "relevance":
        return rows
    reverse = key == "expensive"

    def price_of(row: dict):
        return (row.get("price") or {}).get("value")

    priced = [r for r in rows if price_of(r) is not None]
    unpriced = [r for r in rows if price_of(r) is None]
    priced.sort(key=price_of, reverse=reverse)
    return priced + unpriced


def _categories(doc: dict) -> list[dict]:
    out: list[dict] = []
    seen: set[int] = set()
    for raw in doc.get("categories") or []:
        if not isinstance(raw, dict):
            continue
        cid = _int(raw.get("id"))
        name = _text(raw.get("name"))
        if cid is None or not name or cid in seen:
            continue
        seen.add(cid)
        out.append({"name": name, "id": cid})
    return out


def _category_id(value) -> int | None:
    if value in (None, ""):
        return None
    cid = _int(value)
    if cid is None or cid <= 0:
        raise TregError("eBay: category_id must be a numeric id from a "
                        "previous search")
    return cid


def _limit(value) -> int:
    try:
        n = int(value)
    except (TypeError, ValueError):
        raise TregError("eBay: limit must be an integer")
    if n < 1:
        raise TregError("eBay: limit must be at least 1")
    return min(n, MAX_LIMIT)


def search(client: TregClient | None, query: str = "", category_id=None,
           sort: str = "relevance", limit: int = 25) -> dict:
    """Search eBay through treg. Costs $0.015 per successful call."""
    q = _text(query)
    cid = _category_id(category_id)
    if not q and cid is None:
        raise TregError("eBay: enter a search or choose a category")
    limit = _limit(limit)
    params: list[tuple[str, str]] = [("engine", "ebay")]
    if q:
        params.append(("_nkw", q))
    if cid is not None:
        params.append(("category_id", str(cid)))

    doc, cost = _call(client, SEARCH_ENDPOINT, "GET", params,
                      default_cost=SEARCH_COST_USD)
    info = doc.get("search_information") or {}
    raw_rows = doc.get("organic_results") or []
    if not isinstance(raw_rows, list):
        raw_rows = []
    all_rows = [r for r in (search_row(raw) for raw in raw_rows) if r]
    rows = _sort_rows(all_rows, sort)[:limit]
    categories = _categories(doc)
    category = ""
    if cid is not None:
        category = next((c["name"] for c in categories if c["id"] == cid), "")
    total = _int(info.get("total_results"))
    if total is None:
        total = len(all_rows)
    return {
        "query": q,
        "category": category or "All categories",
        "category_id": cid,
        "sort": _sort_key(sort),
        "limit": limit,
        "count": len(rows),
        "available": len(all_rows),
        "total": total,
        "currency": next((r["price"]["currency"] for r in rows
                          if r["price"].get("currency")), ""),
        "categories": categories,
        "rows": rows,
        "source": "SerpApi eBay search",
        "endpoint": SEARCH_ENDPOINT,
        "cost_usd": cost,
    }


# -- listing detail ---------------------------------------------------------

def resolve_product_id(value) -> str:
    """A numeric eBay product id, an ``v1|id|0`` id, or an eBay listing URL."""
    s = _text(value)
    if not s:
        raise TregError("eBay: enter a product id or an eBay listing URL")
    if s.isdigit():
        return s
    m = re.fullmatch(r"v1\|(\d+)\|\d+", s)
    if m:
        return m.group(1)
    if not (s.startswith(("http://", "https://")) or "ebay." in s):
        raise TregError("eBay: enter a numeric product id or an eBay URL")
    url = s if s.startswith(("http://", "https://")) else \
        "https://" + s.lstrip("/")
    parts = urlsplit(url)
    host = (parts.hostname or "").lower()
    if host.startswith("www."):
        host = host[4:]
    if host not in EBAY_HOSTS and not host.endswith(".ebay.com"):
        raise TregError("eBay: enter an ebay.com listing URL")
    m = _ITEM_PATH_RE.search(parts.path)
    if not m:
        raise TregError("eBay: could not find a listing id in that URL")
    return m.group(1)


def _media_photos(media) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for item in media or []:
        if not isinstance(item, dict):
            continue
        if item.get("type") not in (None, "image"):
            continue
        images = item.get("image") or item.get("images") or []
        if isinstance(images, dict):
            images = [images]
        best = ""
        best_width = -1
        for im in images or []:
            if not isinstance(im, dict):
                continue
            link = _text(im.get("link") or im.get("url"))
            size = im.get("size") if isinstance(im.get("size"), dict) else {}
            width = _int(size.get("width")) or 0
            if link and width >= best_width:
                best, best_width = link, width
        if best and best not in seen:
            seen.add(best)
            out.append(best)
    return out


def _collect_text(value, seen: set[str] | None = None) -> str:
    """Flatten SerpApi's nested snippets into one readable sentence."""
    seen = seen if seen is not None else set()
    out: list[str] = []

    def walk(obj):
        if isinstance(obj, str):
            s = obj.strip()
            if s and s not in seen:
                seen.add(s)
                out.append(s)
        elif isinstance(obj, list):
            for x in obj:
                walk(x)
        elif isinstance(obj, dict):
            if obj.get("text"):
                walk(obj["text"])
            else:
                for key in ("snippets", "items", "groups", "list", "values"):
                    if obj.get(key) is not None:
                        walk(obj[key])

    walk(value)
    return " · ".join(out)


def _flatten_specs(value) -> list[dict]:
    out: list[dict] = []
    seen: set[str] = set()

    def walk(obj):
        if isinstance(obj, list):
            for x in obj:
                walk(x)
            return
        if not isinstance(obj, dict):
            return
        title = _text(obj.get("title") or obj.get("name"))
        val = obj.get("value")
        if title and val not in (None, "") and title not in seen:
            seen.add(title)
            out.append({"name": title, "value": _text(val)})
            return
        for key in ("groups", "sections", "fields", "items", "list"):
            if obj.get(key) is not None:
                walk(obj[key])

    walk(value)
    return out[:80]


def _detail_price(product: dict) -> tuple[dict, str]:
    buy = product.get("buy") if isinstance(product.get("buy"), dict) else {}
    buy_now = buy.get("buy_it_now") if isinstance(buy.get("buy_it_now"), dict) else {}
    price = buy_now.get("price")
    if not isinstance(price, dict):
        price = buy.get("current_bid") or buy.get("price")
    if not isinstance(price, dict):
        price = {}
    value = _number(price.get("amount"))
    currency = _text(price.get("currency")).upper()
    label = _format_money(value, currency)
    old = buy_now.get("list_price") if isinstance(buy_now.get("list_price"), dict) else {}
    old_label = _format_money(old.get("amount"), old.get("currency"))
    return ({"value": value, "currency": currency, "label": label}, old_label)


def _detail_shipping(shipping: dict) -> tuple[str, list[dict]]:
    options: list[dict] = []
    for raw in shipping.get("options") or []:
        if not isinstance(raw, dict):
            continue
        price = raw.get("price") if isinstance(raw.get("price"), dict) else {}
        value = _number(price.get("amount"))
        currency = _text(price.get("currency")).upper()
        label = "Free delivery" if value == 0 else _format_money(value, currency)
        via = _text(raw.get("via"))
        options.append({
            "label": label,
            "via": via,
            "value": value,
            "currency": currency,
        })
    if options:
        first = options[0]
        label = " · ".join(x for x in (first["label"], first["via"]) if x)
    elif _text(shipping.get("status")).lower() == "available":
        label = "Shipping available"
    else:
        label = ""
    return label, options


def _seller_username(product: dict, seller: dict) -> str:
    for key in ("username", "seller_username"):
        if _text(seller.get(key)):
            return _text(seller.get(key))
    link = _text(product.get("full_description_link"))
    if link:
        values = parse_qs(urlsplit(link).query).get("seller")
        if values:
            return _text(values[0])
    return ""


def _detail_row(product_id: str, product: dict, seller_doc: dict) -> dict:
    price, old_label = _detail_price(product)
    shipping = product.get("shipping") if isinstance(product.get("shipping"), dict) else {}
    shipping_label, shipping_options = _detail_shipping(shipping)
    seller = {
        "username": _seller_username(product, seller_doc),
        "name": _text(seller_doc.get("name")),
        "feedback_count": seller_doc.get("rating"),
        "positive_percent": seller_doc.get("positive_feedback"),
        "sold_count": seller_doc.get("sold_count"),
        "joined_at": _text(seller_doc.get("joined_at")),
        "type": _text(seller_doc.get("type")),
        "bio": _text(seller_doc.get("bio")),
        "logo": _text(seller_doc.get("logo_link")),
    }
    buy = product.get("buy") if isinstance(product.get("buy"), dict) else {}
    formats = [_text(x) for x in (buy.get("options") or []) if _text(x)]
    if not formats and isinstance(buy.get("buy_it_now"), dict):
        formats = ["buy_it_now"]
    format_text = " · ".join(_FORMAT_LABELS.get(x, x.replace("_", " ").title())
                             for x in formats)
    condition = _text(product.get("condition"))
    photos = _media_photos(product.get("media"))
    description = _text(product.get("short_description"))
    row = {
        "title": _text(product.get("title")) or "(untitled)",
        "subtitle": condition or description[:200],
        "id": product_id,
        "url": f"{SITE}/itm/{product_id}",
        "description": description,
        "full_description_url": _text(product.get("full_description_link")),
        "price": price,
        "price_label": price["label"],
        "old_price_label": old_label,
        "condition": condition,
        "shipping_label": shipping_label,
        "shipping_options": shipping_options,
        "location": _clean_location(shipping.get("from")),
        "returns": _collect_text(product.get("returns")),
        "seller": seller,
        "buying_format": ", ".join(formats),
        "buying_format_text": format_text,
        "sponsored": False,
        "watchers": _text(product.get("watch_count")),
        "sold": "",
        "rating": product.get("rating"),
        "reviews": product.get("review_count"),
        "extensions": [],
        "thumbnail": photos[0] if photos else "",
        "photos": photos,
        "specs": _flatten_specs(product.get("specifications")),
        "banner_status": _text(product.get("banner_status")),
        "source": "SerpApi eBay product",
        "endpoint": DETAIL_ENDPOINT,
        "full": True,
    }
    _set_meta(row)
    return row


def _merge_base(row: dict, base: dict) -> None:
    """Keep useful search fields when the detail page omits them."""
    for key in ("title", "condition", "shipping_label", "location", "returns",
                "buying_format", "buying_format_text", "thumbnail",
                "old_price_label", "watchers", "sold", "rating", "reviews",
                "banner_status", "description", "extensions", "sponsored"):
        if not row.get(key) and base.get(key):
            row[key] = base[key]
    if not row.get("price_label") and base.get("price_label"):
        row["price"] = base.get("price") or row.get("price")
        row["price_label"] = base["price_label"]
    if not row.get("photos") and base.get("photos"):
        row["photos"] = list(base["photos"])
        row["thumbnail"] = row["photos"][0] if row["photos"] else ""
    seller = row.setdefault("seller", {})
    base_seller = base.get("seller") or {}
    for key, value in base_seller.items():
        if not seller.get(key) and value not in (None, ""):
            seller[key] = value
    _set_meta(row)


def listing(client: TregClient | None, value, base: dict | None = None) -> dict:
    """One eBay listing in full through treg. Costs $0.015 per success.

    `base` is an optional search row; any fields the detail page leaves empty
    are filled from it without another call.
    """
    product_id = resolve_product_id(value)
    params = [("engine", "ebay_product"), ("product_id", product_id),
              ("ebay_domain", "ebay.com")]
    doc, cost = _call(client, DETAIL_ENDPOINT, "GET", params,
                      default_cost=DETAIL_COST_USD)
    product = doc.get("product_results")
    if not isinstance(product, dict) or not product:
        raise TregError(f"eBay: no details for listing {product_id}")
    seller_doc = doc.get("seller_results")
    if not isinstance(seller_doc, dict):
        seller_doc = {}
    row = _detail_row(product_id, product, seller_doc)
    if base:
        _merge_base(row, base)
    row["cost_usd"] = cost
    return row


def digest_prompt(label: str, rows: list[dict], total: int | None = None,
                  limit: int = 15) -> str:
    """The local-model prompt for a digest of the current eBay rows."""
    head = f"eBay — {label}"
    if total:
        head += f" ({total:,} listings in total)"
    lines = [head, ""]
    for i, row in enumerate(rows[:limit], 1):
        price = row.get("price_label") or "price not stated"
        bits = [price]
        if row.get("condition"):
            bits.append(row["condition"])
        if row.get("location"):
            bits.append(row["location"])
        if row.get("shipping_label"):
            bits.append(row["shipping_label"])
        seller = row.get("seller") or {}
        if seller.get("username"):
            feedback = ""
            if seller.get("positive_percent") not in (None, ""):
                feedback = f", {seller['positive_percent']}% positive"
            bits.append(f"seller {seller['username']}{feedback}")
        if row.get("sponsored"):
            bits.append("sponsored")
        lines.append(f"{i}. {row.get('title', '')} — {' · '.join(bits)}")
        if row.get("description"):
            lines.append(f"   {row['description'][:240]}")
    lines += [
        "",
        "Write a concise digest in 4-6 sentences of Markdown. Compare the "
        "prices with the conditions and sellers, note the price range, "
        "shipping and returns, and flag sponsored listings, auctions, "
        "refurbished or parts-only items, zero-feedback sellers and anything "
        "that looks like an unrealistic price. Prices are in the currencies "
        "shown; do not convert them. Do not list URLs or repeat titles "
        "verbatim.",
    ]
    return "\n".join(lines)
