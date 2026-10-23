"""Unit tests for the GitHub tab's normalization helpers (network-free).

Run with:  python3 -m unittest discover -s tests -v
"""

from __future__ import annotations

import unittest

from treg_console import github


class RepoSlugTest(unittest.TestCase):
    def test_plain_slug(self):
        self.assertEqual(github.repo_slug("mesonbuild/meson"), "mesonbuild/meson")

    def test_url_with_extra_path(self):
        self.assertEqual(
            github.repo_slug("https://github.com/mesonbuild/meson/tree/master"),
            "mesonbuild/meson")

    def test_git_suffix_and_trailing_slash(self):
        self.assertEqual(github.repo_slug("https://github.com/o/r.git/"), "o/r")

    def test_no_scheme_host(self):
        self.assertEqual(github.repo_slug("github.com/o/r"), "o/r")

    def test_junk_is_empty(self):
        self.assertEqual(github.repo_slug(""),
                         "")
        self.assertEqual(github.repo_slug("justoneword"), "")
        self.assertEqual(github.repo_slug("https://gitlab.com/o/r"), "")


class RepoRowTest(unittest.TestCase):
    def test_normalizes_a_repository(self):
        row = github.repo_row({
            "id": 1, "full_name": "o/r", "name": "r",
            "owner": {"login": "o"}, "description": "a thing",
            "html_url": "https://github.com/o/r",
            "stargazers_count": 12345, "forks_count": 67,
            "open_issues_count": 8, "language": "Python",
            "topics": ["cli"], "license": {"spdx_id": "MIT"},
            "pushed_at": "2023-11-14T22:13:20Z",
        })
        self.assertEqual(row["title"], "o/r")
        self.assertEqual(row["stars"], 12345)
        self.assertEqual(row["forks"], 67)
        self.assertEqual(row["language"], "Python")
        self.assertEqual(row["license"], "MIT")
        self.assertIn("12.3k stars", row["meta"])
        self.assertIn("Python", row["meta"])
        self.assertIsNotNone(row["ts"])

    def test_missing_full_name_is_dropped(self):
        self.assertIsNone(github.repo_row({"id": 1}))

    def test_num_formats(self):
        self.assertEqual(github._num(999), "999")
        self.assertEqual(github._num(1234), "1.2k")
        self.assertEqual(github._num(1_234_567), "1.2m")
        self.assertEqual(github._num(None), "0")


class IssueRowTest(unittest.TestCase):
    def test_issue_and_pr_are_distinguished(self):
        issue = github._issue_row({
            "number": 7, "title": "Bug", "html_url": "u", "state": "open",
            "user": {"login": "alice"}, "labels": [{"name": "bug"}],
            "comments": 3, "body": "steps", "updated_at": "2023-11-14T22:13:20Z",
        }, "o/r")
        self.assertFalse(issue["is_pr"])
        self.assertEqual(issue["title"], "#7 Bug")
        self.assertEqual(issue["labels"], ["bug"])
        self.assertIn("issue", issue["meta"])

        pr = github._issue_row({
            "number": 8, "title": "Feature", "state": "open",
            "pull_request": {"url": "x"},
        }, "o/r")
        self.assertTrue(pr["is_pr"])
        self.assertIn("pull request", pr["meta"])

    def test_missing_number_is_dropped(self):
        self.assertIsNone(github._issue_row({"title": "x"}, "o/r"))


class ReleaseRowTest(unittest.TestCase):
    def test_normalizes_a_release(self):
        row = github._release_row({
            "tag_name": "v1.0", "name": "First", "html_url": "u",
            "author": {"login": "alice"}, "body": "notes",
            "published_at": "2023-11-14T22:13:20Z", "prerelease": False,
        }, "o/r")
        self.assertEqual(row["title"], "First")
        self.assertEqual(row["tag"], "v1.0")
        self.assertEqual(row["author"], "alice")
        self.assertIn("v1.0", row["meta"])

    def test_empty_release_is_dropped(self):
        self.assertIsNone(github._release_row({}, "o/r"))


class DigestPromptTest(unittest.TestCase):
    def test_lists_titles_and_meta(self):
        rows = [{"title": "o/r", "meta": "10 stars · Python",
                 "subtitle": "a thing"}]
        prompt = github.digest_prompt("Trending", rows)
        self.assertIn("o/r", prompt)
        self.assertIn("10 stars", prompt)
        self.assertIn("a thing", prompt)


if __name__ == "__main__":
    unittest.main()
