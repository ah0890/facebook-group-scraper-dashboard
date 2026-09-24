import csv
import io
import json
import tempfile
from pathlib import Path

from django.contrib.auth import get_user_model
from django.db import IntegrityError, transaction
from django.test import TestCase
from django.urls import reverse

from dashboard.exports import write_csv
from dashboard.models import Post, ScraperRun
from scraper.models import ScrapedPost
from scraper.storage import GroupPostStore, save_posts, write_snapshot

from .helpers import make_group


def scraped(post_id="", text="Hello world", author="Ayesha", likes=1):
    return ScrapedPost(facebook_post_id=post_id, author_name=author, post_text=text, likes_count=likes,
                       post_url=f"https://www.facebook.com/groups/g/posts/{post_id}/" if post_id else "")


class DeduplicationTests(TestCase):
    def setUp(self):
        self.group = make_group()
        self.run = ScraperRun.objects.create(total_groups=1)

    def test_same_post_id_saved_once_and_counts_refreshed(self):
        saved, dup = save_posts(self.run, self.group, [scraped("111", likes=5)])
        self.assertEqual((saved, dup), (1, 0))
        saved, dup = save_posts(self.run, self.group, [scraped("111", text="edited", likes=9)])
        self.assertEqual((saved, dup), (0, 1))
        post = Post.objects.get()
        self.assertEqual(post.likes_count, 9)  # engagement refreshed
        self.assertEqual(post.post_text, "Hello world")  # original content kept

    def test_posts_without_id_deduplicate_by_content(self):
        save_posts(self.run, self.group, [scraped(text="Same text"), scraped(text="same   TEXT")])
        self.assertEqual(Post.objects.count(), 1)

    def test_same_id_in_different_groups_is_allowed(self):
        other = make_group("Other", slug="other")
        save_posts(self.run, self.group, [scraped("222")])
        save_posts(self.run, other, [scraped("222")])
        self.assertEqual(Post.objects.count(), 2)

    def test_database_constraint_blocks_duplicates(self):
        save_posts(self.run, self.group, [scraped("333")])
        with self.assertRaises(IntegrityError), transaction.atomic():
            Post.objects.create(group=self.group, facebook_post_id="333", dedup_key="different-key")

    def test_store_buffers_until_save_frequency(self):
        store = GroupPostStore(self.run, self.group, save_frequency=3)
        store.add(scraped("1"))
        store.add(scraped("2"))
        self.assertFalse(store.should_flush())
        self.assertEqual(Post.objects.count(), 0)
        store.add(scraped("3"))
        self.assertTrue(store.should_flush())
        self.assertEqual(store.flush(), 3)
        self.run.refresh_from_db()
        self.assertEqual(self.run.total_posts, 3)
        self.group.refresh_from_db()

    def test_group_total_posts_updated(self):
        save_posts(self.run, self.group, [scraped("1"), scraped("2")])
        self.group.refresh_from_db()
        self.assertEqual(self.group.total_posts, 2)

    def test_snapshot_written_atomically(self):
        save_posts(self.run, self.group, [scraped("1"), scraped("2")])
        with tempfile.TemporaryDirectory() as tmp:
            path = write_snapshot(self.run, Path(tmp))
            data = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(data["count"], 2)
            self.assertEqual(sorted(p["facebook_post_id"] for p in data["posts"]), ["1", "2"])
            self.assertEqual([p.name for p in Path(tmp).iterdir()], ["fetched_posts.json"])  # no temp files left


class ExportTests(TestCase):
    def setUp(self):
        user = get_user_model().objects.create_user("admin", password="pass-12345-word")
        self.client.force_login(user)
        self.group = make_group("Group A", slug="group-a")
        self.other = make_group("Group B", slug="group-b")
        run = ScraperRun.objects.create()
        save_posts(run, self.group, [scraped("1", text="=HYPERLINK(\"http://evil\")"), scraped("2", text="python jobs")])
        save_posts(run, self.other, [scraped("3", text="other group")])

    def test_csv_export(self):
        response = self.client.get(reverse("export_csv"))
        self.assertEqual(response.status_code, 200)
        self.assertIn("attachment;", response["Content-Disposition"])
        rows = list(csv.DictReader(io.StringIO(response.content.decode("utf-8-sig"))))
        self.assertEqual(len(rows), 3)
        formula = next(r for r in rows if r["facebook_post_id"] == "1")
        self.assertTrue(formula["post_text"].startswith("'="))  # spreadsheet formula injection neutralised

    def test_csv_export_respects_filters(self):
        response = self.client.get(reverse("export_csv"), {"group": self.other.pk})
        rows = list(csv.DictReader(io.StringIO(response.content.decode("utf-8-sig"))))
        self.assertEqual([r["post_text"] for r in rows], ["other group"])

    def test_json_export(self):
        response = self.client.get(reverse("export_json"), {"q": "python"})
        self.assertEqual(response.status_code, 200)
        data = json.loads(response.content)
        self.assertEqual(data["count"], 1)
        self.assertEqual(data["posts"][0]["group"], "Group A")
        self.assertEqual(data["posts"][0]["post_text"], "python jobs")

    def test_write_csv_header(self):
        buffer = io.StringIO()
        count = write_csv(Post.objects.all(), buffer)
        self.assertEqual(count, 3)
        self.assertTrue(buffer.getvalue().startswith("id,group,group_url,facebook_post_id"))

    def test_posts_page_filters_and_paginates(self):
        response = self.client.get(reverse("posts"), {"author": "ayesha", "group": self.group.pk})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["total"], 2)

    def test_exports_require_login(self):
        self.client.logout()
        response = self.client.get(reverse("export_json"))
        self.assertEqual(response.status_code, 302)
