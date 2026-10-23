"""Unit tests for the OLX.pt source: normalization, categories, search
parameters, price sorting, listing id resolution and the digest prompt.

Run with:  python3 -m unittest discover -s tests -v
"""

from __future__ import annotations

import unittest
from unittest.mock import patch

from treg_console import olx
from treg_console.client import TregError


RAW = {
    "id": 123,
    "title": "iPhone 13 128 GB",
    "url": "https://www.olx.pt/d/anuncio/iphone-13-IDabc.html",
    "description": "Bom estado<br />\nCom caixa &amp; carregador",
    "created_time": "2026-10-23T09:08:09+01:00",
    "last_refresh_time": "2026-10-23T09:11:26+01:00",
    "category": {"id": 11, "type": "goods"},
    "business": False,
    "user": {"id": 9, "name": "Ana", "created": "2020-01-01T00:00:00+00:00"},
    "location": {
        "city": {"id": 1, "name": "Lisboa"},
        "region": {"id": 2, "name": "Lisboa"},
    },
    "params": [
        {"key": "condition", "name": "Condição",
         "value": {"label": "Usado"}},
        {"key": "price", "name": "Preço", "type": "price",
         "value": {"value": 250, "currency": "EUR", "label": "250 €",
                   "negotiable": True}},
    ],
    "photos": [
        {"link": "https://img.olx.pt/a;s={width}x{height}"},
        {"link": "https://img.olx.pt/b;s={width}x{height}"},
    ],
}


def _raw(pid: int, price: float | None, title: str = "Item") -> dict:
    raw = {
        "id": pid,
        "title": title,
        "url": f"https://www.olx.pt/d/anuncio/{pid}",
        "description": "desc",
        "created_time": "2026-10-23T09:08:09+01:00",
        "category": {"id": 11, "type": "goods"},
        "params": [],
        "photos": [],
    }
    if price is not None:
        raw["params"].append({
            "key": "price", "name": "Preço", "type": "price",
            "value": {"value": price, "currency": "EUR",
                      "label": f"{price:g} €"},
        })
    return raw


class TextTest(unittest.TestCase):
    def test_plain_keeps_breaks_and_unescapes(self):
        self.assertEqual(olx._plain("Bom<br />\nCom &amp; caixa"),
                         "Bom\nCom & caixa")
        self.assertEqual(olx._plain("<p>Hello</p> world"), "Hello world")

    def test_snippet_is_flat_and_short(self):
        s = olx._snippet("a " * 200, 20)
        self.assertLessEqual(len(s), 21)
        self.assertNotIn("\n", s)
        self.assertTrue(s.endswith("…"))


class RowTest(unittest.TestCase):
    def test_normalizes_a_listing(self):
        row = olx.listing_row(RAW)
        self.assertEqual(row["title"], "iPhone 13 128 GB")
        self.assertEqual(row["price"]["value"], 250)
        self.assertTrue(row["price"]["negotiable"])
        self.assertEqual(row["price_label"], "250 €")
        self.assertEqual(row["location"]["label"], "Lisboa")
        self.assertEqual(row["location"]["city_id"], 1)
        self.assertEqual(row["category"], "Tech & computers")
        self.assertEqual(row["attrs"], {"Condição": "Usado"})
        self.assertEqual(row["seller"]["name"], "Ana")
        self.assertFalse(row["is_business"])
        self.assertEqual(row["photo_count"], 2)
        self.assertIn("1000x1000", row["photos"][0])
        self.assertNotIn("{width}", row["photos"][0])
        self.assertIn("250 €", row["meta"])
        self.assertIn("Lisboa", row["meta"])
        self.assertIn("Bom estado", row["subtitle"])

    def test_detail_keeps_the_full_description(self):
        raw = dict(RAW, description="Line one<br />Line two")
        row = olx.listing_row(raw, detail=True)
        self.assertEqual(row["description"], "Line one\nLine two")
        short = olx.listing_row(raw)
        self.assertEqual(short["description"], "Line one Line two")

    def test_missing_price_is_none(self):
        row = olx.listing_row(_raw(1, None))
        self.assertIsNone(row["price"])
        self.assertEqual(row["price_label"], "")

    def test_rejects_bad_rows(self):
        self.assertIsNone(olx.listing_row({}))
        self.assertIsNone(olx.listing_row("nope"))


class CategoryTest(unittest.TestCase):
    def test_ids_names_slugs_and_aliases(self):
        self.assertEqual(olx.resolve_category(362), 362)
        self.assertEqual(olx.resolve_category("11"), 11)
        self.assertEqual(olx.resolve_category("cars"), 362)
        self.assertEqual(olx.resolve_category("carros-motos-e-barcos"), 362)
        self.assertEqual(olx.resolve_category("Real estate"), 16)
        self.assertEqual(olx.resolve_category("tech"), 11)
        self.assertEqual(olx.resolve_category("pets"), 10)
        self.assertEqual(olx.resolve_category("Leisure"), 26)
        self.assertIsNone(olx.resolve_category(None))
        self.assertIsNone(olx.resolve_category(""))
        self.assertIsNone(olx.resolve_category("all"))

    def test_unknown_category_is_an_error(self):
        with self.assertRaises(TregError):
            olx.resolve_category("spaceships")

    def test_categories_tool_excludes_the_all_row(self):
        cats = olx.categories()
        self.assertEqual(len(cats), 15)
        self.assertTrue(all(c["id"] for c in cats))
        self.assertIn("Cars, motos & boats", [c["name"] for c in cats])


class SearchTest(unittest.TestCase):
    def test_builds_olx_parameters_and_normalizes_rows(self):
        captured = {}

        def fake_get(path, params=None):
            captured["path"] = path
            captured["params"] = params
            return {"data": [RAW],
                    "metadata": {"visible_total_count": 1234,
                                 "source": {"promoted": [0]}}}

        with patch("treg_console.olx._get", side_effect=fake_get):
            out = olx.search("iphone 13", category="tech", price_min=100,
                             price_max=300, sort="cheapest", limit=10)

        params = captured["params"]
        self.assertEqual(captured["path"], "")
        self.assertEqual(params["query"], "iphone 13")
        self.assertEqual(params["category_id"], 11)
        self.assertEqual(params["filter_float_price:from"], 100.0)
        self.assertEqual(params["filter_float_price:to"], 300.0)
        self.assertEqual(params["sort_by"], "filter_float_price:asc")
        self.assertEqual(params["limit"], 10)
        self.assertEqual(out["total"], 1234)
        self.assertEqual(out["category"], "Tech & computers")
        self.assertEqual(out["query"], "iphone 13")
        self.assertTrue(out["rows"][0]["promoted"])

    def test_price_sorts_the_batch_and_leaves_unpriced_last(self):
        data = [_raw(1, 300), _raw(2, 100), _raw(3, None)]

        def fake_get(path, params=None):
            return {"data": data, "metadata": {}}

        with patch("treg_console.olx._get", side_effect=fake_get):
            cheap = olx.search("x", sort="cheapest", limit=10)
            dear = olx.search("x", sort="expensive", limit=10)

        self.assertEqual([r["id"] for r in cheap["rows"]], [2, 1, 3])
        self.assertEqual([r["id"] for r in dear["rows"]], [1, 2, 3])

    def test_honours_the_requested_limit(self):
        data = [_raw(i, i * 10) for i in range(1, 6)]

        def fake_get(path, params=None):
            return {"data": data, "metadata": {}}

        with patch("treg_console.olx._get", side_effect=fake_get):
            out = olx.search("x", limit=2)
        self.assertEqual(len(out["rows"]), 2)

    def test_rejects_a_bad_price_range(self):
        with self.assertRaises(TregError):
            olx.search("x", price_min=500, price_max=100)

    def test_unknown_sort_falls_back_to_relevance(self):
        captured = {}

        def fake_get(path, params=None):
            captured["params"] = params
            return {"data": [], "metadata": {}}

        with patch("treg_console.olx._get", side_effect=fake_get):
            out = olx.search("x", sort="whatever")
        self.assertEqual(captured["params"]["sort_by"], "relevance:desc")
        self.assertEqual(out["rows"], [])


class ListingTest(unittest.TestCase):
    def test_numeric_id_needs_no_page_fetch(self):
        self.assertEqual(olx.resolve_listing_id("674732003"), 674732003)

    def test_url_is_resolved_from_the_page(self):
        page = '<script>{"sku":"674732003","x":1}</script>'
        with patch("treg_console.olx._get_page", return_value=page) as get:
            lid = olx.resolve_listing_id(
                "https://www.olx.pt/d/anuncio/x-IDabc.html")
        self.assertEqual(lid, 674732003)
        get.assert_called_once()

    def test_labelled_id_is_a_fallback(self):
        page = "<span>ID: <!-- -->12345</span>"
        with patch("treg_console.olx._get_page", return_value=page):
            self.assertEqual(
                olx.resolve_listing_id("olx.pt/d/anuncio/x-IDabc.html"),
                12345)

    def test_bad_value_is_an_error(self):
        with self.assertRaises(TregError):
            olx.resolve_listing_id("just-a-slug")

    def test_non_olx_url_is_rejected_without_fetching(self):
        with patch("treg_console.olx._get_page") as get:
            with self.assertRaises(TregError):
                olx.resolve_listing_id("https://example.com/x")
        get.assert_not_called()

    def test_listing_fetches_the_detail(self):
        captured = {}

        def fake_get(path, params=None):
            captured["path"] = path
            return {"data": RAW}

        with patch("treg_console.olx._get", side_effect=fake_get):
            row = olx.listing(123)
        self.assertEqual(captured["path"], "/123")
        self.assertEqual(row["id"], 123)
        self.assertIn("Bom estado", row["description"])


class DigestTest(unittest.TestCase):
    def test_digest_prompt_lists_rows_and_instructions(self):
        rows = [olx.listing_row(RAW)]
        prompt = olx.digest_prompt("Search: iphone", rows, total=2273)
        self.assertIn("OLX.pt — Search: iphone", prompt)
        self.assertIn("2,273 listings", prompt)
        self.assertIn("iPhone 13 128 GB", prompt)
        self.assertIn("250 €", prompt)
        self.assertIn("digest", prompt.lower())


if __name__ == "__main__":
    unittest.main()
