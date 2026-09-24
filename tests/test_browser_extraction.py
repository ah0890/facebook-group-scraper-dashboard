"""Runs the real in-page extraction script in Chromium against a local HTML fixture.

No network access and no Facebook: the fixture mimics the structure of a group
feed. Skipped automatically when Playwright's Chromium is not installed.
"""

import tempfile
from pathlib import Path

from django.test import SimpleTestCase

from scraper.browser import BrowserManager
from scraper.exceptions import BrowserLaunchError
from scraper.facebook import EXTRACT_POSTS_JS
from scraper.parser import parse_raw_post

FEED_FIXTURE = """
<html><body>
<div role="feed">
  <div>
    <div role="article">
      <h3><a href="https://www.facebook.com/profile.php?id=1&__cft__=x">Ayesha Khan</a></h3>
      <a href="https://www.facebook.com/groups/demo/posts/1234567890/?__cft__[0]=abc" aria-label="3h">3h</a>
      <div data-ad-preview="message"><div>Looking for a reliable plumber in Phase 5.</div><div>Any suggestions?</div></div>
      <img src="https://example.com/photo.jpg" width="600" height="400">
      <span aria-label="All reactions: 1.2K"></span>
      <span>45 comments</span><span>7 shares</span>
    </div>
  </div>
  <div>
    <div role="article">
      <h3><a href="https://www.facebook.com/profile.php?id=2">Bilal Ahmed</a></h3>
      <a href="https://www.facebook.com/groups/demo/permalink/555/">Yesterday at 3:15 PM</a>
      <div data-ad-comet-preview="message">Selling a used laptop.</div>
    </div>
  </div>
  <div><div role="article"><h3>Suggested groups</h3></div></div>
</div>
</body></html>
"""


class BrowserExtractionTests(SimpleTestCase):
    def test_extraction_script_against_fixture(self):
        with tempfile.TemporaryDirectory() as profile:
            browser = BrowserManager(Path(profile), headless=True, timeout_ms=15000)
            try:
                browser.start()
            except BrowserLaunchError as exc:
                self.skipTest(f"Chromium not available: {exc}")
            try:
                page = browser.ensure_page()
                page.set_content(FEED_FIXTURE)
                data = page.evaluate(EXTRACT_POSTS_JS)
            finally:
                browser.close()

        self.assertEqual(data["dom"], 3)
        self.assertEqual(len(data["posts"]), 2)  # the "Suggested groups" unit is skipped
        first, second = (parse_raw_post(raw) for raw in data["posts"])

        self.assertEqual(first.author_name, "Ayesha Khan")
        self.assertEqual(first.facebook_post_id, "1234567890")
        self.assertEqual(first.post_url, "https://www.facebook.com/groups/demo/posts/1234567890/")
        self.assertEqual(first.post_text, "Looking for a reliable plumber in Phase 5.\nAny suggestions?")
        self.assertEqual((first.likes_count, first.comments_count, first.shares_count), (1200, 45, 7))
        self.assertEqual(first.media_url, "https://example.com/photo.jpg")
        self.assertIsNotNone(first.post_timestamp)
        self.assertNotIn("__cft__", first.author_url)

        self.assertEqual(second.author_name, "Bilal Ahmed")
        self.assertEqual(second.facebook_post_id, "555")
        self.assertEqual(second.post_text, "Selling a used laptop.")
        self.assertIsNotNone(second.post_timestamp)
