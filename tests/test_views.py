from io import StringIO

from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.test import TestCase, TransactionTestCase
from django.urls import reverse
from django.utils import timezone

from dashboard import services
from dashboard.models import Group, Post, RunStatus, ScraperLog, ScraperRun
from scraper.runner import execute_run

from .helpers import fast_mock_factory, fast_settings, make_group

PAGES = ["dashboard", "stats", "groups", "posts", "run_scraper", "logs", "settings", "defaults", "db"]


class PageTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.user = get_user_model().objects.create_user("admin", password="pass-12345-word", is_staff=True)
        fast_settings()
        make_group("Alpha", slug="alpha")
        run = services.queue_run()
        execute_run(run.pk, client_factory=fast_mock_factory)
        cls.finished_run = ScraperRun.objects.get(pk=run.pk)

    def setUp(self):
        self.client.force_login(self.user)

    def test_all_pages_render(self):
        for name in PAGES:
            with self.subTest(page=name):
                response = self.client.get(reverse(name))
                self.assertEqual(response.status_code, 200)
        self.assertEqual(self.client.get(reverse("run_detail", args=[self.finished_run.pk])).status_code, 200)
        self.assertEqual(self.client.get(reverse("group_edit", args=[Group.objects.get().pk])).status_code, 200)

    def test_pages_require_login(self):
        self.client.logout()
        for name in PAGES + ["api_status"]:
            with self.subTest(page=name):
                response = self.client.get(reverse(name))
                self.assertEqual(response.status_code, 302)
                self.assertIn(reverse("login"), response["Location"])

    def test_control_endpoints_require_post(self):
        for name in ["run_start", "run_stop", "run_reset_settings", "browser_authenticate", "logs_clear"]:
            with self.subTest(endpoint=name):
                self.assertEqual(self.client.get(reverse(name)).status_code, 405)

    def test_post_text_is_escaped(self):
        post = Post.objects.first()
        Post.objects.filter(pk=post.pk).update(post_text="<script>alert('x')</script>")
        response = self.client.get(reverse("posts"))
        self.assertNotContains(response, "<script>alert('x')</script>")
        self.assertContains(response, "&lt;script&gt;")

    def test_api_status(self):
        response = self.client.get(reverse("api_status"))
        data = response.json()
        self.assertEqual(data["phase"], "IDLE")
        self.assertIsNone(data["active_run_id"])
        self.assertEqual(data["run"]["id"], self.finished_run.pk)
        self.assertEqual(data["run"]["status"], RunStatus.COMPLETED)
        self.assertTrue(data["logs"])
        self.assertEqual(data["groups"][0]["status"], "COMPLETED")
        last_id = data["logs"][-1]["id"]
        self.assertEqual(self.client.get(reverse("api_status"), {"since": last_id}).json()["logs"], [])

    def test_start_refused_when_run_active(self):
        run = services.queue_run()
        ScraperRun.objects.filter(pk=run.pk).update(status=RunStatus.RUNNING, heartbeat_at=timezone.now())
        response = self.client.post(reverse("run_start"), HTTP_ACCEPT="application/json")
        self.assertEqual(response.status_code, 400)
        self.assertIn("already in progress", response.json()["message"])

    def test_stop_without_active_run(self):
        response = self.client.post(reverse("run_stop"), HTTP_ACCEPT="application/json")
        self.assertEqual(response.status_code, 400)

    def test_mock_authenticate(self):
        response = self.client.post(reverse("browser_authenticate"), HTTP_ACCEPT="application/json")
        self.assertEqual(response.json()["status"], "MOCK")

    def test_run_scraper_page_shows_next_batch(self):
        make_group("Beta", slug="beta")
        response = self.client.get(reverse("run_scraper"))
        self.assertContains(response, "The only remaining group will be scraped to complete Round #1.")


class CommandTests(TransactionTestCase):
    """run_scraper executes in a worker thread, so data must be committed (no wrapping transaction)."""

    def test_seed_demo_data(self):
        out = StringIO()
        call_command("seed_demo_data", "--days", "3", stdout=out)
        self.assertEqual(Group.objects.count(), 10)
        self.assertTrue(Post.objects.exists())
        self.assertTrue(ScraperRun.objects.filter(trigger="demo").exists())
        batch = services.next_batch()
        self.assertTrue(batch.groups)

    def test_run_scraper_command(self):
        fast_settings(posts_per_group=5)
        make_group("Alpha", slug="alpha")
        out = StringIO()
        call_command("run_scraper", "--quiet", stdout=out)
        self.assertIn("COMPLETED", out.getvalue())
        self.assertEqual(Post.objects.count(), 5)
        self.assertTrue(ScraperLog.objects.exists())

    def test_export_command(self):
        import tempfile
        from pathlib import Path

        fast_settings(posts_per_group=3)
        make_group("Alpha", slug="alpha")
        call_command("run_scraper", "--quiet", stdout=StringIO())
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "out.json"
            call_command("export_posts", "--format", "json", "--output", str(target), stdout=StringIO())
            self.assertIn('"count": 3', target.read_text(encoding="utf-8"))
