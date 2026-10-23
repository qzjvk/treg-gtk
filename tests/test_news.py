"""Unit tests for the News tab's feed parsing and story ranking.

Run with:  python3 -m unittest discover -s tests -v
"""

from __future__ import annotations

import time
import unittest

from treg_console import news


RSS = """<?xml version="1.0"?>
<rss version="2.0" xmlns:media="http://search.yahoo.com/mrss/">
  <channel>
    <title>Example</title>
    <item>
      <title>Docker is weird</title>
      <link>https://example.com/docker?utm_source=rss</link>
      <pubDate>Thu, 23 Oct 2026 05:00:00 +0000</pubDate>
      <description><![CDATA[<p>A container story.</p>]]></description>
      <media:thumbnail url="https://example.com/docker.jpg" />
    </item>
    <item>
      <title>No link here</title>
      <pubDate>Thu, 23 Oct 2026 04:00:00 +0000</pubDate>
    </item>
  </channel>
</rss>
"""

ATOM = """<?xml version="1.0"?>
<feed xmlns="http://www.w3.org/2005/Atom">
  <title>Example Atom</title>
  <entry>
    <title>LLMs and verification</title>
    <link href="https://example.org/llms" />
    <published>2026-10-23T06:00:00Z</published>
    <summary>Formal verification gets cheaper.</summary>
  </entry>
</feed>
"""


class ParseTimeTest(unittest.TestCase):
    def test_iso_and_zulu(self):
        self.assertIsNotNone(news.parse_time("2026-10-23T06:00:00Z"))
        self.assertIsNotNone(news.parse_time("2026-10-23T06:00:00+02:00"))

    def test_rfc822(self):
        ts = news.parse_time("Thu, 23 Oct 2026 05:00:00 +0000")
        self.assertIsNotNone(ts)

    def test_relative(self):
        ts = news.parse_time("2 hours ago")
        self.assertIsNotNone(ts)
        self.assertAlmostEqual(time.time() - ts, 7200, delta=10)

    def test_epoch_millis(self):
        self.assertEqual(news.parse_time(1_700_000_000_000), 1_700_000_000)

    def test_garbage(self):
        self.assertIsNone(news.parse_time("not a date"))
        self.assertIsNone(news.parse_time(""))


class FeedParseTest(unittest.TestCase):
    def test_rss(self):
        rows = news.parse_feed(RSS, "Example")
        self.assertEqual(len(rows), 1)  # the item without a link is dropped
        row = rows[0]
        self.assertEqual(row["title"], "Docker is weird")
        self.assertEqual(row["source"], "Example")
        self.assertEqual(row["snippet"], "A container story.")
        self.assertEqual(row["image"], "https://example.com/docker.jpg")
        self.assertIsNotNone(row["ts"])

    def test_atom(self):
        rows = news.parse_feed(ATOM, "Atom")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["url"], "https://example.org/llms")
        self.assertEqual(rows[0]["snippet"], "Formal verification gets cheaper.")


class CanonicalUrlTest(unittest.TestCase):
    def test_strips_tracking_and_www(self):
        a = news.canonical_url("https://www.Example.com/a/b/?utm_source=x&id=2")
        b = news.canonical_url("https://example.com/a/b?id=2")
        self.assertEqual(a, b)

    def test_keeps_meaningful_query(self):
        self.assertIn("id=2", news.canonical_url("https://example.com/x?id=2"))


class AggregateTest(unittest.TestCase):
    def test_dedupes_and_counts_sources(self):
        items = [
            {"title": "Docker is weird", "url": "https://a.com/story?utm_source=rss",
             "source": "Ars Technica", "ts": time.time() - 60, "position": 1},
            {"title": "Docker is weird", "url": "https://b.com/story",
             "source": "TechCrunch", "ts": time.time() - 30, "position": 3,
             "snippet": "A longer container story."},
            {"title": "Something entirely different", "url": "https://c.com/x",
             "source": "Wired", "ts": time.time() - 7200},
        ]
        rows = news.aggregate(items, limit=10)
        self.assertEqual(len(rows), 2)
        docker = next(r for r in rows if r["title"] == "Docker is weird")
        self.assertEqual(docker["sources"], ["Ars Technica", "TechCrunch"])
        self.assertEqual(docker["source_label"], "Ars Technica + TechCrunch")
        self.assertEqual(docker["snippet"], "A longer container story.")

    def test_merges_similar_titles_with_different_urls(self):
        rows = news.aggregate([
            {"title": "Breaking: Mars rover finds ice", "url": "https://a.com/1",
             "source": "BBC News"},
            {"title": "Mars rover finds ice", "url": "https://b.com/2",
             "source": "NPR"},
        ])
        self.assertEqual(len(rows), 1)
        self.assertEqual(len(rows[0]["sources"]), 2)

    def test_recency_outranks_stale(self):
        rows = news.aggregate([
            {"title": "Old story about nothing", "url": "https://a.com/old",
             "source": "BBC News", "ts": time.time() - 3 * 86400},
            {"title": "Fresh story about nothing", "url": "https://a.com/new",
             "source": "BBC News", "ts": time.time() - 60},
        ])
        self.assertEqual(rows[0]["title"], "Fresh story about nothing")

    def test_drops_rows_without_title_or_url(self):
        rows = news.aggregate([
            {"title": "", "url": "https://a.com"},
            {"title": "No URL", "url": ""},
        ])
        self.assertEqual(rows, [])


class DigestTest(unittest.TestCase):
    def test_prompt_lists_stories(self):
        prompt = news.digest_prompt("Top stories", [
            {"title": "Docker is weird", "source": "Ars Technica",
             "snippet": "A container story.", "ts": time.time()},
        ])
        self.assertIn("Docker is weird", prompt)
        self.assertIn("Ars Technica", prompt)
        self.assertIn("digest", prompt.lower())


class SearchTest(unittest.TestCase):
    class Response:
        status_code = 200
        headers = {"X-Treg-Served-By": "tinyfish.web.search.news",
                   "X-Treg-Cost-Micro": "0"}

        @staticmethod
        def json():
            return {"output": {"results": [
                {"title": "Mars rover finds ice",
                 "url": "https://example.com/mars?utm_source=x",
                 "publisher": "BBC News", "snippet": "A discovery.",
                 "date": "2 hours ago", "position": 1},
                {"title": "Markets rally", "link": "https://example.com/markets",
                 "source": "CNBC", "summary": "Stocks climbed.",
                 "published_at": "2026-10-23 04:00:00 UTC", "position": 2},
            ]}}

    class Client:
        def __init__(self):
            self.calls = []

        def call(self, target, method, params, body):
            self.calls.append((target, method, body))
            return SearchTest.Response()

    def test_normalizes_shapes_and_reports_provider(self):
        client = self.Client()
        out = news.search_news(client, "space", limit=10)
        self.assertEqual(client.calls[0][0], "treg.web.search.news")
        self.assertEqual(out["served_by"], "tinyfish.web.search.news")
        self.assertEqual(out["cost_micro"], 0)
        self.assertEqual(len(out["items"]), 2)
        mars = next(r for r in out["items"] if "Mars" in r["title"])
        self.assertEqual(mars["sources"], ["BBC News"])
        self.assertEqual(mars["url"], "https://example.com/mars?utm_source=x")

    def test_empty_query_is_rejected(self):
        with self.assertRaises(news.TregError):
            news.search_news(self.Client(), "   ")


if __name__ == "__main__":
    unittest.main()
