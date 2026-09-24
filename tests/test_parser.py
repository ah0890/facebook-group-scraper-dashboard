from datetime import datetime, timedelta, timezone

from django.test import SimpleTestCase

from scraper.mock import generate_feed
from scraper.parser import (
    build_dedup_key,
    canonical_post_url,
    clean_text,
    extract_counts,
    extract_post_id,
    parse_count,
    parse_raw_post,
    parse_timestamp,
)

NOW = datetime(2026, 3, 15, 12, 0, tzinfo=timezone.utc)


class PostIdTests(SimpleTestCase):
    def test_extracts_ids_from_common_url_shapes(self):
        cases = {
            "https://www.facebook.com/groups/abc/posts/123456789/?__cft__[0]=x": "123456789",
            "https://www.facebook.com/groups/123/permalink/987654321/": "987654321",
            "https://www.facebook.com/permalink.php?story_fbid=555&id=1": "555",
            "https://www.facebook.com/groups/abc/?multi_permalinks=777": "777",
            "https://www.facebook.com/groups/abc/posts/pfbid02AbCdEf/": "pfbid02AbCdEf",
            "https://www.facebook.com/groups/abc/": "",
            "": "",
        }
        for url, expected in cases.items():
            with self.subTest(url=url):
                self.assertEqual(extract_post_id(url), expected)

    def test_canonical_url_strips_tracking(self):
        self.assertEqual(
            canonical_post_url("https://www.facebook.com/groups/abc/posts/42/?__cft__[0]=AZ&__tn__=R"),
            "https://www.facebook.com/groups/abc/posts/42/",
        )


class CountTests(SimpleTestCase):
    def test_parse_count(self):
        for text, expected in {"12": 12, "1,234": 1234, "1.2K": 1200, "3M": 3_000_000, "2.5k": 2500,
                               "": 0, None: 0, "abc": 0, 7: 7}.items():
            with self.subTest(text=text):
                self.assertEqual(parse_count(text), expected)

    def test_extract_counts_from_labels(self):
        counts = extract_counts(["All reactions: 1.1K", "45 comments", "3 shares", "Like"])
        self.assertEqual(counts, {"likes": 1100, "comments": 45, "shares": 3})


class TimestampTests(SimpleTestCase):
    def test_relative(self):
        self.assertEqual(parse_timestamp("5m", NOW), NOW - timedelta(minutes=5))
        self.assertEqual(parse_timestamp("3 hrs", NOW), NOW - timedelta(hours=3))
        self.assertEqual(parse_timestamp("2d", NOW), NOW - timedelta(days=2))
        self.assertEqual(parse_timestamp("1w", NOW), NOW - timedelta(weeks=1))
        self.assertEqual(parse_timestamp("Just now", NOW), NOW)

    def test_yesterday(self):
        self.assertEqual(parse_timestamp("Yesterday at 3:15 PM", NOW), datetime(2026, 3, 14, 15, 15, tzinfo=timezone.utc))

    def test_absolute(self):
        self.assertEqual(parse_timestamp("March 3 at 10:00 AM", NOW), datetime(2026, 3, 3, 10, 0, tzinfo=timezone.utc))
        self.assertEqual(parse_timestamp("Monday, March 2, 2025 at 5:00 PM", NOW),
                         datetime(2025, 3, 2, 17, 0, tzinfo=timezone.utc))
        # A date later than "now" without a year belongs to last year.
        self.assertEqual(parse_timestamp("December 30", NOW).year, 2025)

    def test_unknown(self):
        self.assertIsNone(parse_timestamp("sometime", NOW))
        self.assertIsNone(parse_timestamp("", NOW))


class RawPostTests(SimpleTestCase):
    def test_parse_raw_post(self):
        post = parse_raw_post({
            "author": " Ayesha  Khan ",
            "text": "Looking for a plumber\n\n  in Phase 5 … See more",
            "post_url": "https://www.facebook.com/groups/abc/posts/99/?__cft__=1",
            "timestamp_text": "2h",
            "labels": ["All reactions: 12", "4 comments"],
            "media_url": "javascript:alert(1)",
        }, now=NOW)
        self.assertEqual(post.author_name, "Ayesha Khan")
        self.assertEqual(post.post_text, "Looking for a plumber\nin Phase 5")
        self.assertEqual(post.facebook_post_id, "99")
        self.assertEqual(post.post_url, "https://www.facebook.com/groups/abc/posts/99/")
        self.assertEqual(post.post_timestamp, NOW - timedelta(hours=2))
        self.assertEqual((post.likes_count, post.comments_count), (12, 4))
        self.assertEqual(post.media_url, "")  # non-http media URLs are dropped

    def test_empty_units_are_ignored(self):
        self.assertIsNone(parse_raw_post({"author": "Someone"}))
        self.assertIsNone(parse_raw_post("not a dict"))

    def test_dedup_key_prefers_post_id_and_falls_back_to_hash(self):
        with_id = parse_raw_post({"text": "a", "post_url": "https://www.facebook.com/groups/g/posts/5/"})
        without_id = parse_raw_post({"author": "A", "text": "Same   text"})
        same_text = parse_raw_post({"author": "a", "text": "same text"})
        self.assertEqual(build_dedup_key(1, with_id), "1:id:5")
        self.assertTrue(build_dedup_key(1, without_id).startswith("1:h:"))
        self.assertEqual(build_dedup_key(1, without_id), build_dedup_key(1, same_text))
        self.assertNotEqual(build_dedup_key(1, without_id), build_dedup_key(2, without_id))

    def test_clean_text(self):
        self.assertEqual(clean_text("  hello   world \n\n\n bye "), "hello world\nbye")

    def test_mock_feed_parses_completely(self):
        feed = generate_feed("https://www.facebook.com/groups/demo/", now=NOW, depth=20)
        posts = [parse_raw_post(raw, now=NOW) for raw in feed]
        self.assertTrue(all(p and p.facebook_post_id and p.post_timestamp for p in posts))
        self.assertEqual(len({p.facebook_post_id for p in posts}), 20)
