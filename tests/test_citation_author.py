"""Verify citation author parsing for Reddit, YouTube, and Instagram URLs."""

from __future__ import annotations

import unittest
from unittest.mock import patch

from pipeline.citation_author import (
    apply_authors,
    author_from_url,
    classify_instagram_url,
    youtube_video_id,
)
from pipeline.db import _grain_columns
from pipeline.raw_citations import FIELDNAMES, citation_row


class InstagramClassificationTests(unittest.TestCase):
    def test_profile_url_exposes_username(self) -> None:
        result = classify_instagram_url(
            "https://www.instagram.com/mayoclinic/", "instagram.com"
        )
        self.assertEqual(result["url_type"], "Profile")
        self.assertEqual(result["embedded_username"], "mayoclinic")
        self.assertIsNone(result["shortcode"])

    def test_post_url_without_username(self) -> None:
        result = classify_instagram_url(
            "https://www.instagram.com/reel/AbCdEf123/", "www.instagram.com"
        )
        self.assertEqual(result["url_type"], "Post")
        self.assertEqual(result["shortcode"], "AbCdEf123")
        self.assertIsNone(result["embedded_username"])

    def test_post_url_with_embedded_username(self) -> None:
        result = classify_instagram_url(
            "https://www.instagram.com/mayoclinic/p/AbCdEf123/", "instagram.com"
        )
        self.assertEqual(result["url_type"], "Post")
        self.assertEqual(result["shortcode"], "AbCdEf123")
        self.assertEqual(result["embedded_username"], "mayoclinic")


class AuthorFromUrlTests(unittest.TestCase):
    def test_reddit_subreddit(self) -> None:
        self.assertEqual(
            author_from_url("https://www.reddit.com/r/breastcancer/comments/abc/"),
            "r/breastcancer",
        )

    def test_reddit_old_subdomain(self) -> None:
        self.assertEqual(
            author_from_url("https://old.reddit.com/r/AskDocs/comments/xyz/title/"),
            "r/AskDocs",
        )

    def test_reddit_without_subreddit_is_blank(self) -> None:
        self.assertIsNone(author_from_url("https://redd.it/abc123"))

    def test_youtube_handle(self) -> None:
        self.assertEqual(
            author_from_url("https://www.youtube.com/@MayoClinic/videos"),
            "@MayoClinic",
        )

    def test_youtube_user_and_custom_paths(self) -> None:
        self.assertEqual(
            author_from_url("https://www.youtube.com/c/MayoClinic"),
            "@MayoClinic",
        )
        self.assertEqual(
            author_from_url("https://www.youtube.com/user/mayoclinic"),
            "@mayoclinic",
        )

    def test_youtube_watch_url_has_no_path_author(self) -> None:
        self.assertIsNone(
            author_from_url("https://www.youtube.com/watch?v=dQw4w9WgXcQ")
        )
        self.assertEqual(youtube_video_id("https://youtu.be/dQw4w9WgXcQ"), "dQw4w9WgXcQ")

    def test_instagram_profile_and_embedded_post(self) -> None:
        self.assertEqual(
            author_from_url("https://www.instagram.com/ergon/"),
            "@ergon",
        )
        self.assertEqual(
            author_from_url("https://www.instagram.com/ergon/reel/AbCdEf123/"),
            "@ergon",
        )

    def test_instagram_shortcode_only_is_blank(self) -> None:
        self.assertIsNone(author_from_url("https://www.instagram.com/p/AbCdEf123/"))

    def test_other_domains_are_blank(self) -> None:
        self.assertIsNone(author_from_url("https://www.ergon.com/news"))


class ApplyAuthorsTests(unittest.TestCase):
    def test_url_parse_fills_author(self) -> None:
        rows = [
            {"url": "https://www.reddit.com/r/cancer/", "hostname": "www.reddit.com"}
        ]
        apply_authors(rows, live_lookup=False)
        self.assertEqual(rows[0]["author"], "r/cancer")

    def test_keeps_previous_live_author_when_url_has_none(self) -> None:
        rows = [
            {
                "url": "https://www.instagram.com/p/AbCdEf123/",
                "hostname": "www.instagram.com",
                "author": "@kept_handle",
            }
        ]
        apply_authors(rows, live_lookup=False)
        self.assertEqual(rows[0]["author"], "@kept_handle")

    def test_instagram_shortcode_lookup_is_cached(self) -> None:
        calls: list[str] = []

        def resolve(shortcode: str) -> str | None:
            calls.append(shortcode)
            return f"@{shortcode}"

        rows = [
            {"url": "https://www.instagram.com/reel/SameCode/", "hostname": "instagram.com"},
            {"url": "https://www.instagram.com/p/SameCode/", "hostname": "instagram.com"},
        ]
        apply_authors(
            rows,
            live_lookup=True,
            resolve_instagram=resolve,
            resolve_youtube=lambda _url: None,
            delay_seconds=0,
        )
        self.assertEqual(calls, ["SameCode"])
        self.assertEqual(rows[0]["author"], "@SameCode")
        self.assertEqual(rows[1]["author"], "@SameCode")

    def test_youtube_oembed_lookup_uses_video_id_cache(self) -> None:
        calls: list[str] = []

        def resolve(url: str) -> str | None:
            calls.append(url)
            return "@MayoClinic"

        rows = [
            {"url": "https://www.youtube.com/watch?v=abc123", "hostname": "www.youtube.com"},
            {"url": "https://youtu.be/abc123", "hostname": "youtu.be"},
        ]
        apply_authors(
            rows,
            live_lookup=True,
            resolve_instagram=lambda _code: None,
            resolve_youtube=resolve,
            delay_seconds=0,
        )
        self.assertEqual(len(calls), 1)
        self.assertEqual(rows[0]["author"], "@MayoClinic")
        self.assertEqual(rows[1]["author"], "@MayoClinic")

    def test_instagram_cools_down_then_continues(self) -> None:
        calls: list[str] = []
        sleeps: list[float] = []

        def resolve(_shortcode: str) -> str | None:
            calls.append(_shortcode)
            return None

        rows = [
            {"url": f"https://www.instagram.com/p/{code}/", "hostname": "instagram.com"}
            for code in ("aaa", "bbb", "ccc", "ddd")
        ]
        apply_authors(
            rows,
            live_lookup=True,
            resolve_instagram=resolve,
            resolve_youtube=lambda _url: None,
            delay_seconds=0,
            cooldown_seconds=12.0,
            sleep=sleeps.append,
        )
        self.assertEqual(calls, ["aaa", "bbb", "ccc", "ddd"])
        self.assertEqual(sleeps, [12.0])
        self.assertTrue(all(row["author"] is None for row in rows))

    def test_instagram_stops_at_time_budget(self) -> None:
        calls: list[str] = []
        elapsed = [0.0]

        def resolve(shortcode: str) -> str | None:
            calls.append(shortcode)
            elapsed[0] += 40.0
            return f"@{shortcode}"

        rows = [
            {"url": f"https://www.instagram.com/p/{code}/", "hostname": "instagram.com"}
            for code in ("aaa", "bbb", "ccc", "aaa")
        ]
        apply_authors(
            rows,
            live_lookup=True,
            resolve_instagram=resolve,
            resolve_youtube=lambda _url: None,
            delay_seconds=0,
            instagram_budget=60.0,
            clock=lambda: elapsed[0],
        )
        self.assertEqual(calls, ["aaa", "bbb"])
        self.assertEqual(
            [row["author"] for row in rows], ["@aaa", "@bbb", None, "@aaa"]
        )

    def test_incremental_lookup_skips_older_rows(self) -> None:
        calls: list[str] = []

        def resolve(shortcode: str) -> str | None:
            calls.append(shortcode)
            return f"@{shortcode}"

        rows = [
            {
                "date": "2026-09-01",
                "url": "https://www.instagram.com/p/OldOnly/",
                "hostname": "instagram.com",
            },
            {
                "date": "2026-09-01",
                "url": "https://www.instagram.com/p/Shared/",
                "hostname": "instagram.com",
            },
            {
                "date": "2026-10-01",
                "url": "https://www.instagram.com/reel/Shared/",
                "hostname": "instagram.com",
            },
            {
                "date": "2026-10-01",
                "url": "https://www.youtube.com/watch?v=newvid",
                "hostname": "www.youtube.com",
            },
        ]
        apply_authors(
            rows,
            live_lookup=True,
            resolve_instagram=resolve,
            resolve_youtube=lambda _url: "@channel",
            delay_seconds=0,
            live_from="2026-09-30",
        )
        self.assertEqual(calls, ["Shared"])
        self.assertIsNone(rows[0]["author"])
        self.assertEqual(rows[1]["author"], "@Shared")
        self.assertEqual(rows[2]["author"], "@Shared")
        self.assertEqual(rows[3]["author"], "@channel")


class CitationExportTests(unittest.TestCase):
    def test_fieldnames_match_sql_grain(self) -> None:
        self.assertEqual(FIELDNAMES, _grain_columns("fact_raw_citations"))

    def test_citation_row_includes_url_author(self) -> None:
        row = citation_row(
            day="2026-09-20",
            topic="Energy & Specialty Solutions",
            platform="ChatGPT",
            category="earned_media",
            mentioned="Ergon",
            url="https://www.reddit.com/r/breastcancer/comments/abc/",
            hostname="www.reddit.com",
            path="/r/breastcancer/comments/abc/",
            tags="oncology",
            region="United States",
        )
        self.assertEqual(row["author"], "r/breastcancer")

    def test_live_lookup_env_off(self) -> None:
        rows = [
            {"url": "https://www.instagram.com/p/AbCdEf123/", "hostname": "instagram.com"}
        ]
        with patch.dict("os.environ", {"CITATION_AUTHOR_LIVE_LOOKUP": "0"}):
            apply_authors(rows, resolve_instagram=lambda _code: "@should_not_run")
        self.assertIsNone(rows[0]["author"])


if __name__ == "__main__":
    unittest.main()
