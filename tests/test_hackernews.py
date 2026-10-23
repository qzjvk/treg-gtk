"""Unit tests for the Hacker News tab's normalization and prompts.

These are network-free: they exercise the pure helpers only.

Run with:  python3 -m unittest discover -s tests -v
"""

from __future__ import annotations

import unittest

from treg_console import hackernews


class PlainTextTest(unittest.TestCase):
    def test_strips_tags_and_unescapes_entities(self):
        raw = '<p>Hello <a href="x">world</a> and &#x27;quotes&#x27;</p>\n<p>again</p>'
        self.assertEqual(hackernews._plain(raw), "Hello world and 'quotes' again")

    def test_snippet_cuts_on_a_word_boundary(self):
        out = hackernews._snippet("one two three four five", limit=9)
        self.assertTrue(out.endswith("…"))
        self.assertNotIn("  ", out)
        self.assertLessEqual(len(out), 10)


class SiteOfTest(unittest.TestCase):
    def test_drops_www_and_keeps_host(self):
        self.assertEqual(hackernews.site_of("https://www.example.com/a/b"), "example.com")
        self.assertEqual(hackernews.site_of("http://news.ycombinator.com"), "news.ycombinator.com")

    def test_blank_for_junk(self):
        self.assertEqual(hackernews.site_of(""), "")
        self.assertEqual(hackernews.site_of("not a url"), "")


class StoryRowTest(unittest.TestCase):
    def test_normalizes_a_link_story(self):
        row = hackernews.story_row({
            "id": 42, "title": "Show HN: thing", "url": "https://www.example.com/x",
            "score": 120, "by": "alice", "time": 1_700_000_000, "descendants": 33,
            "type": "story",
        })
        self.assertEqual(row["id"], 42)
        self.assertEqual(row["title"], "Show HN: thing")
        self.assertEqual(row["site"], "example.com")
        self.assertEqual(row["score"], 120)
        self.assertEqual(row["comments"], 33)
        self.assertFalse(row["self_post"])
        self.assertEqual(row["hn_url"], "https://news.ycombinator.com/item?id=42")

    def test_self_post_uses_the_item_url(self):
        row = hackernews.story_row({
            "id": 7, "title": "Ask HN: anything?", "text": "<p>question</p>",
            "by": "bob", "time": 1, "type": "story",
        })
        self.assertTrue(row["self_post"])
        self.assertEqual(row["url"], "https://news.ycombinator.com/item?id=7")
        self.assertEqual(row["text"], "question")
        self.assertEqual(row["site"], "")

    def test_deleted_and_titleless_items_are_dropped(self):
        self.assertIsNone(hackernews.story_row({"id": 1, "deleted": True, "title": "x"}))
        self.assertIsNone(hackernews.story_row({"id": 2, "dead": True, "title": "x"}))
        self.assertIsNone(hackernews.story_row({"id": 3, "by": "carol"}))


class HitRowTest(unittest.TestCase):
    def test_algolia_hit_becomes_a_story_row(self):
        row = hackernews._hit_row({
            "objectID": "99", "title": "Rust in the kernel",
            "url": "https://lwn.net/x", "points": 200, "author": "dave",
            "created_at": "2023-11-14T22:13:20Z", "num_comments": 88,
        })
        self.assertEqual(row["id"], 99)
        self.assertEqual(row["score"], 200)
        self.assertEqual(row["comments"], 88)
        self.assertEqual(row["site"], "lwn.net")
        self.assertIsNotNone(row["ts"])

    def test_hit_without_title_or_id_is_dropped(self):
        self.assertIsNone(hackernews._hit_row({"objectID": "1"}))
        self.assertIsNone(hackernews._hit_row({"title": "x"}))


class DigestPromptTest(unittest.TestCase):
    def test_prompt_names_the_stories_and_meta(self):
        rows = [{"title": "Story one", "score": 5, "comments": 2,
                 "site": "example.com", "by": "eve", "ts": None}]
        prompt = hackernews.digest_prompt("Top stories", rows)
        self.assertIn("Story one", prompt)
        self.assertIn("5 points", prompt)
        self.assertIn("2 comments", prompt)


if __name__ == "__main__":
    unittest.main()
