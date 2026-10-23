"""Unit tests for the eBay source: search parameters and normalization,
price sorting, listing id resolution, detail normalization, merging a search
row with detail, and the digest prompt.

Run with:  python3 -m unittest discover -s tests -v
"""

from __future__ import annotations

import json
import unittest

from treg_console import ebay
from treg_console.client import TregError


class FakeResponse:
    def __init__(self, doc, status=200, cost_micro=15000):
        self.status_code = status
        self.headers = {"X-Treg-Cost-Micro": str(cost_micro)}
        self._doc = doc
        self.text = json.dumps(doc)

    def json(self):
        return self._doc


class FakeClient:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def call(self, target, method="GET", params=None, body=None):
        self.calls.append((target, method, params, body))
        return self.responses.pop(0)


def _raw(pid, price, title="Item", condition="Used",
         location="Located in United States", seller="alice"):
    return {
        "product_id": str(pid),
        "title": title,
        "subtitle": "Apple iPhone 13 ·",
        "condition": condition,
        "price": ({"raw": f"${price:,.2f}", "extracted": float(price)}
                  if price is not None else None),
        "shipping": "Free delivery",
        "location": location,
        "returns": "Free returns",
        "buying_format": "buy_it_now",
        "buying_format_text": "Buy It Now",
        "thumbnail": f"https://i.ebayimg.com/{pid}.webp",
        "seller": {
            "username": seller,
            "reviews": 1234,
            "positive_feedback_in_percentage": 99.4,
        },
    }


SEARCH_DOC = {
    "search_information": {"total_results": 1234},
    "categories": [
        {"name": "All", "link": "..."},
        {"name": "Cell Phones & Accessories", "id": 15032},
        {"name": "Smart Watches", "id": 178893},
    ],
    "organic_results": [
        _raw(111, 300, "Phone A"),
        _raw(222, 100, "Phone B", condition="New",
             location="Located in China", seller="bob"),
        _raw(333, 200, "Phone C", condition="For parts"),
    ],
}


PRODUCT_DOC = {
    "product_results": {
        "product_id": "336847604413",
        "title": "UNLOCKED Apple iPhone 12 Pro",
        "short_description": "256GB Blue. Sold as-is.",
        "full_description_link": (
            "https://itm.ebaydesc.com/itmdesc/336847604413"
            "?seller=cocosprinkles&x=1"),
        "condition": "Used",
        "watch_count": 13,
        "rating": 4.58,
        "review_count": 38,
        "banner_status": "In 3 carts",
        "buy": {
            "options": ["buy_it_now"],
            "buy_it_now": {
                "price": {"amount": 208.99, "currency": "USD"},
                "list_price": {"amount": 219.99, "currency": "USD"},
            },
        },
        "shipping": {
            "from": "Brooklyn, New York, United States",
            "status": "available",
            "options": [
                {"via": "USPS Ground Advantage",
                 "price": {"amount": 9.99, "currency": "USD"}},
            ],
        },
        "returns": [[
            {"title": "Returns",
             "snippets": [
                 {"text": "No returns, but backed by"},
                 {"text": "eBay Money back guarantee"},
             ]},
        ]],
        "media": [{
            "type": "image",
            "image": [
                {"link": "https://i.ebayimg.com/a/s-l500.webp",
                 "size": {"width": 500}},
                {"link": "https://i.ebayimg.com/a/s-l1600.webp",
                 "size": {"width": 1600}},
            ],
        }],
        "specifications": {"groups": [{
            "type": "about_this_item",
            "title": "Item specifics",
            "sections": [{"fields": [
                {"title": "Brand", "value": "Apple"},
                {"title": "Model", "value": "iPhone 12 Pro"},
                {"title": "Storage Capacity", "value": "256 GB"},
            ]}],
        }]},
    },
    "seller_results": {
        "type": "user",
        "name": "The Coco Store",
        "bio": "A store.",
        "joined_at": "Joined Jan 2008",
        "sold_count": 131000,
        "rating": 42074,
        "positive_feedback": 99.8,
    },
}


class SearchTest(unittest.TestCase):
    def test_builds_params_and_normalizes_rows(self):
        client = FakeClient([FakeResponse(SEARCH_DOC)])
        out = ebay.search(client, "iphone 13", sort="cheapest", limit=2)

        target, method, params, _body = client.calls[0]
        self.assertEqual(target, ebay.SEARCH_ENDPOINT)
        self.assertEqual(method, "GET")
        self.assertEqual(dict(params)["engine"], "ebay")
        self.assertEqual(dict(params)["_nkw"], "iphone 13")
        self.assertEqual(out["total"], 1234)
        self.assertEqual(out["available"], 3)
        self.assertEqual(out["count"], 2)
        self.assertEqual(out["cost_usd"], 0.015)
        self.assertEqual(out["sort"], "cheapest")
        self.assertEqual([r["id"] for r in out["rows"]], ["222", "333"])
        self.assertEqual(out["categories"][0],
                         {"name": "Cell Phones & Accessories", "id": 15032})
        first = out["rows"][0]
        self.assertEqual(first["title"], "Phone B")
        self.assertEqual(first["price"]["value"], 100.0)
        self.assertEqual(first["price_label"], "$100.00")
        self.assertEqual(first["condition"], "New")
        self.assertEqual(first["location"], "China")
        self.assertEqual(first["seller"]["username"], "bob")
        self.assertEqual(first["seller"]["positive_percent"], 99.4)
        self.assertEqual(first["url"],
                         "https://www.ebay.com/itm/222")
        self.assertIn("$100.00", first["meta"])
        self.assertIn("New", first["meta"])

    def test_expensive_sort_and_limit(self):
        client = FakeClient([FakeResponse(SEARCH_DOC)])
        out = ebay.search(client, "x", sort="expensive", limit=2)
        self.assertEqual([r["id"] for r in out["rows"]], ["111", "333"])

    def test_unpriced_rows_go_last(self):
        doc = dict(SEARCH_DOC)
        doc["organic_results"] = [_raw(1, None), _raw(2, 10), _raw(3, 20)]
        client = FakeClient([FakeResponse(doc)])
        out = ebay.search(client, "x", sort="cheapest", limit=3)
        self.assertEqual([r["id"] for r in out["rows"]], ["2", "3", "1"])

    def test_category_only_search_omits_the_keyword(self):
        doc = {"search_information": {"total_results": 0},
               "categories": [{"name": "Cell Phones", "id": 15032}],
               "organic_results": []}
        client = FakeClient([FakeResponse(doc)])
        out = ebay.search(client, "", category_id=15032)
        params = dict(client.calls[0][2])
        self.assertNotIn("_nkw", params)
        self.assertEqual(params["category_id"], "15032")
        self.assertEqual(out["category"], "Cell Phones")
        self.assertEqual(out["total"], 0)
        self.assertEqual(out["rows"], [])

    def test_no_query_and_no_category_is_an_error(self):
        client = FakeClient([])
        with self.assertRaises(TregError):
            ebay.search(client, "")
        self.assertEqual(client.calls, [])

    def test_unknown_sort_falls_back_to_relevance(self):
        client = FakeClient([FakeResponse(SEARCH_DOC)])
        out = ebay.search(client, "x", sort="whatever")
        self.assertEqual(out["sort"], "relevance")
        self.assertEqual([r["id"] for r in out["rows"]], ["111", "222", "333"])

    def test_http_error_is_reported(self):
        client = FakeClient([FakeResponse({"detail": "upstream failed"},
                                          status=502)])
        with self.assertRaises(TregError) as ctx:
            ebay.search(client, "x")
        self.assertIn("HTTP 502", str(ctx.exception))
        self.assertIn("upstream failed", str(ctx.exception))

    def test_serpapi_error_field_is_reported(self):
        client = FakeClient([FakeResponse({"error": "invalid query"})])
        with self.assertRaises(TregError) as ctx:
            ebay.search(client, "x")
        self.assertIn("invalid query", str(ctx.exception))

    def test_bad_limit_is_an_error(self):
        client = FakeClient([])
        with self.assertRaises(TregError):
            ebay.search(client, "x", limit=0)
        self.assertEqual(client.calls, [])


class ListingTest(unittest.TestCase):
    def test_resolves_ids_and_urls(self):
        self.assertEqual(ebay.resolve_product_id("336847604413"),
                         "336847604413")
        self.assertEqual(
            ebay.resolve_product_id(
                "https://www.ebay.com/itm/336847604413?_skw=x"),
            "336847604413")
        self.assertEqual(ebay.resolve_product_id("v1|336847604413|0"),
                         "336847604413")
        self.assertEqual(
            ebay.resolve_product_id("ebay.com/itm/336847604413"),
            "336847604413")

    def test_bad_url_is_rejected_without_a_call(self):
        client = FakeClient([])
        with self.assertRaises(TregError):
            ebay.listing(client, "https://example.com/itm/123456789")
        self.assertEqual(client.calls, [])

    def test_detail_normalization(self):
        client = FakeClient([FakeResponse(PRODUCT_DOC)])
        row = ebay.listing(
            client, "https://www.ebay.com/itm/336847604413?_skw=x")

        target, method, params, _body = client.calls[0]
        self.assertEqual(target, ebay.DETAIL_ENDPOINT)
        self.assertEqual(method, "GET")
        self.assertEqual(dict(params), {
            "engine": "ebay_product",
            "product_id": "336847604413",
            "ebay_domain": "ebay.com",
        })
        self.assertEqual(row["title"], "UNLOCKED Apple iPhone 12 Pro")
        self.assertEqual(row["price_label"], "$208.99")
        self.assertEqual(row["old_price_label"], "$219.99")
        self.assertEqual(row["condition"], "Used")
        self.assertEqual(row["shipping_label"], "$9.99 · USPS Ground Advantage")
        self.assertEqual(row["location"], "Brooklyn, New York, United States")
        self.assertIn("No returns", row["returns"])
        self.assertIn("eBay Money back guarantee", row["returns"])
        self.assertEqual(row["seller"]["username"], "cocosprinkles")
        self.assertEqual(row["seller"]["name"], "The Coco Store")
        self.assertEqual(row["seller"]["positive_percent"], 99.8)
        self.assertEqual(row["seller"]["feedback_count"], 42074)
        self.assertEqual(row["specs"][0], {"name": "Brand", "value": "Apple"})
        self.assertIn("256GB Blue", row["description"])
        self.assertEqual(row["photos"],
                         ["https://i.ebayimg.com/a/s-l1600.webp"])
        self.assertTrue(row["full"])
        self.assertEqual(row["cost_usd"], 0.015)
        self.assertIn("$208.99", row["meta"])

    def test_merges_a_search_row_when_detail_fields_are_missing(self):
        doc = {
            "product_results": {
                "product_id": "336847604413",
                "title": "Thing",
                "condition": "",
                "buy": {},
                "shipping": {},
                "returns": [],
                "media": [],
                "specifications": {},
            },
            "seller_results": {},
        }
        base = ebay.search_row(_raw(336847604413, 42, "Thing"))
        client = FakeClient([FakeResponse(doc)])
        row = ebay.listing(client, "336847604413", base=base)
        self.assertEqual(row["price_label"], "$42.00")
        self.assertEqual(row["condition"], "Used")
        self.assertEqual(row["shipping_label"], "Free delivery")
        self.assertEqual(row["seller"]["username"], "alice")
        self.assertEqual(row["photos"], ["https://i.ebayimg.com/336847604413.webp"])

    def test_missing_product_results_is_an_error(self):
        client = FakeClient([FakeResponse({"product_results": {}})])
        with self.assertRaises(TregError):
            ebay.listing(client, "336847604413")


class DigestTest(unittest.TestCase):
    def test_digest_prompt_lists_rows_and_instructions(self):
        rows = [ebay.search_row(_raw(1, 250, "iPhone 13"))]
        prompt = ebay.digest_prompt("Search: iphone", rows, total=2273)
        self.assertIn("eBay — Search: iphone", prompt)
        self.assertIn("2,273 listings", prompt)
        self.assertIn("iPhone 13", prompt)
        self.assertIn("$250.00", prompt)
        self.assertIn("seller alice", prompt)
        self.assertIn("digest", prompt.lower())


if __name__ == "__main__":
    unittest.main()
