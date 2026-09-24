import os
from datetime import timedelta
from unittest import mock

from django.test import TestCase
from django.utils import timezone

from dashboard import services
from dashboard.models import (
    Group,
    GroupStatus,
    Phase,
    Post,
    RunGroup,
    RunStatus,
    ScraperLog,
    ScraperRun,
)
from scraper import runner
from scraper.exceptions import AuthenticationRequired
from scraper.mock import MockFacebookClient
from scraper.runner import execute_run

from .helpers import fast_mock_factory, fast_settings, make_group


class RunCreationTests(TestCase):
    def setUp(self):
        fast_settings(groups_per_run=2)
        self.a = make_group("A", slug="a")
        self.b = make_group("B", slug="b")
        self.c = make_group("C", slug="c")
        make_group("Disabled", slug="disabled", enabled=False)

    def test_queue_run_creates_run_and_queue(self):
        run = services.queue_run()
        self.assertEqual(run.status, RunStatus.QUEUED)
        self.assertEqual(run.total_groups, 2)
        self.assertEqual(run.round_number, 1)
        entries = list(run.run_groups.order_by("position"))
        self.assertEqual([e.group.name for e in entries], ["A", "B"])
        self.assertTrue(all(e.status == GroupStatus.QUEUED for e in entries))
        self.a.refresh_from_db()
        self.assertEqual(self.a.last_status, GroupStatus.QUEUED)

    def test_cannot_start_second_run_while_active(self):
        run = services.queue_run()
        ScraperRun.objects.filter(pk=run.pk).update(status=RunStatus.RUNNING, heartbeat_at=timezone.now())
        with self.assertRaises(services.RunAlreadyActive):
            services.queue_run()

    def test_no_enabled_groups(self):
        Group.objects.update(enabled=False)
        with self.assertRaises(services.NoGroupsToScrape):
            services.queue_run()

    def test_specific_groups(self):
        run = services.queue_run(group_ids=[self.c.pk], trigger="group")
        self.assertEqual([e.group_id for e in run.run_groups.all()], [self.c.pk])
        self.assertEqual(run.trigger, "group")

    def test_next_batch_message(self):
        batch = services.next_batch()
        self.assertEqual(batch.round_number, 1)
        self.assertEqual(batch.remaining_count, 3)
        self.assertEqual(batch.message, "2 of 3 remaining groups will be scraped in this batch (Round #1).")
        Group.objects.filter(pk=self.a.pk).update(completed_round=1)
        self.assertEqual(services.next_batch().message, "All 2 remaining groups will be scraped to complete Round #1.")


class RunExecutionTests(TestCase):
    def setUp(self):
        fast_settings()
        runner_patch = mock.patch.object(runner, "RETRY_BACKOFF_SECONDS", 0)
        runner_patch.start()
        self.addCleanup(runner_patch.stop)

    def test_successful_run(self):
        a = make_group("Alpha", slug="alpha")
        b = make_group("Beta", slug="beta")
        run = services.queue_run()
        run = execute_run(run.pk, client_factory=fast_mock_factory)

        self.assertEqual(run.status, RunStatus.COMPLETED)
        self.assertEqual(run.current_phase, Phase.COMPLETED)
        self.assertEqual((run.completed_groups, run.failed_groups), (2, 0))
        self.assertEqual(run.total_posts, 20)
        self.assertIsNotNone(run.finished_at)
        self.assertEqual(Post.objects.filter(group=a).count(), 10)
        for group in (a, b):
            group.refresh_from_db()
            self.assertEqual(group.last_status, GroupStatus.COMPLETED)
            self.assertEqual(group.total_posts, 10)
            self.assertEqual(group.completed_round, 1)
            self.assertIsNotNone(group.last_run_at)
        self.assertTrue(run.logs.filter(message__startswith="Scroll 01 | DOM:").exists())
        self.assertTrue(run.logs.filter(level="SUCCESS", message__contains="Post collected").exists())

    def test_rerun_deduplicates(self):
        make_group("Alpha", slug="alpha")
        execute_run(services.queue_run().pk, client_factory=fast_mock_factory)
        Group.objects.update(completed_round=0)
        run = execute_run(services.queue_run().pk, client_factory=fast_mock_factory)
        self.assertEqual(run.status, RunStatus.COMPLETED)
        self.assertEqual(run.total_posts, 0)
        self.assertGreater(run.duplicate_posts, 0)
        self.assertEqual(Post.objects.count(), 10)
        self.assertTrue(run.logs.filter(message__startswith="Caught up").exists())

    def test_failing_group_does_not_stop_run(self):
        good = make_group("Good", slug="good")
        bad = make_group("Broken", slug="demo-broken")
        run = execute_run(services.queue_run().pk, client_factory=fast_mock_factory)
        self.assertEqual(run.status, RunStatus.PARTIAL)
        self.assertEqual((run.completed_groups, run.failed_groups), (1, 1))
        entry = RunGroup.objects.get(run=run, group=bad)
        self.assertEqual(entry.status, GroupStatus.FAILED)
        self.assertEqual(entry.attempts, 2)  # 1 try + retry_count=1
        bad.refresh_from_db()
        good.refresh_from_db()
        self.assertEqual(bad.completed_round, 0)  # failed groups stay in the round
        self.assertEqual(good.completed_round, 1)
        self.assertTrue(run.logs.filter(level="ERROR", message__contains="Group failed").exists())

    def test_flaky_group_succeeds_on_retry(self):
        make_group("Flaky", slug="demo-flaky")
        run = execute_run(services.queue_run().pk, client_factory=fast_mock_factory)
        self.assertEqual(run.status, RunStatus.COMPLETED)
        self.assertEqual(run.run_groups.get().attempts, 2)
        self.assertTrue(run.logs.filter(level="WARNING", message__contains="Retrying").exists())

    def test_unavailable_group_is_not_retried(self):
        make_group("Private", slug="demo-private")
        run = execute_run(services.queue_run().pk, client_factory=fast_mock_factory)
        self.assertEqual(run.status, RunStatus.FAILED)
        self.assertEqual(run.run_groups.get().attempts, 1)

    def test_authentication_failure_aborts_run(self):
        make_group("A", slug="a")
        make_group("B", slug="b")

        class NoAuthClient(MockFacebookClient):
            def ensure_authenticated(self):
                raise AuthenticationRequired("Not logged in to Facebook.")

        run = execute_run(services.queue_run().pk,
                          client_factory=lambda cfg, *, log, wait: NoAuthClient(cfg, log=log, wait=wait, speed=0))
        self.assertEqual(run.status, RunStatus.FAILED)
        self.assertIn("Not logged in", run.error_message)
        self.assertEqual(run.skipped_groups, 2)
        self.assertFalse(RunGroup.objects.filter(run=run).exclude(status=GroupStatus.SKIPPED).exists())

    def test_browser_launch_failure_marks_run_failed(self):
        make_group("A", slug="a")

        def factory(cfg, *, log, wait):
            from scraper.exceptions import BrowserLaunchError

            raise BrowserLaunchError("Could not launch Chromium: not installed")

        run = execute_run(services.queue_run().pk, client_factory=factory)
        self.assertEqual(run.status, RunStatus.FAILED)
        self.assertIn("Chromium", run.error_message)

    def test_unexpected_exception_never_leaves_run_running(self):
        make_group("A", slug="a")

        def factory(cfg, *, log, wait):
            raise RuntimeError("boom")

        run = execute_run(services.queue_run().pk, client_factory=factory)
        self.assertEqual(run.status, RunStatus.FAILED)
        self.assertIn("boom", run.error_message)

    def test_snapshot_written_after_each_group(self):
        import tempfile

        make_group("A", slug="a")
        with tempfile.TemporaryDirectory() as tmp:
            fast_settings(save_after_every_group=True, export_dir=tmp)
            run = execute_run(services.queue_run().pk, client_factory=fast_mock_factory)
            self.assertTrue(os.path.exists(os.path.join(tmp, "fetched_posts.json")))
        self.assertTrue(run.logs.filter(message="Saved progress -> fetched_posts.json").exists())

    def test_scroll_limit_respected(self):
        fast_settings(posts_per_group=500, scroll_limit=3)
        make_group("A", slug="a")
        run = execute_run(services.queue_run().pk, client_factory=fast_mock_factory)
        self.assertEqual(run.logs.filter(message__regex=r"^Scroll \d+ \|").count(), 3)
        self.assertTrue(run.logs.filter(message__startswith="Scroll limit (3) reached").exists())


class RecoveryTests(TestCase):
    def setUp(self):
        fast_settings(groups_per_run=10)

    def test_completed_groups_not_repeated_after_interruption(self):
        a, b, c = (make_group(n, slug=n.lower()) for n in ("A", "B", "C"))
        run = services.queue_run()
        # Simulate a crash after the first group finished.
        RunGroup.objects.filter(run=run, group=a).update(status=GroupStatus.COMPLETED)
        Group.objects.filter(pk=a.pk).update(completed_round=1, last_status=GroupStatus.COMPLETED)
        ScraperRun.objects.filter(pk=run.pk).update(
            status=RunStatus.RUNNING, completed_groups=1, worker_pid=os.getpid() + 1,
            heartbeat_at=timezone.now() - timedelta(hours=1),
        )

        recovered = services.recover_stale_runs()
        self.assertEqual([r.pk for r in recovered], [run.pk])
        run.refresh_from_db()
        self.assertEqual(run.status, RunStatus.PARTIAL)
        self.assertEqual(run.skipped_groups, 2)
        self.assertTrue(ScraperLog.objects.filter(run=run, message__startswith="Run interrupted").exists())

        batch = services.next_batch()
        self.assertEqual([g.name for g in batch.groups], ["B", "C"])
        self.assertEqual(batch.message, "All 2 remaining groups will be scraped to complete Round #1.")

    def test_orphaned_run_in_this_process_is_recovered_immediately(self):
        make_group("A", slug="a")
        run = services.queue_run()
        ScraperRun.objects.filter(pk=run.pk).update(status=RunStatus.RUNNING, worker_pid=os.getpid(),
                                                     heartbeat_at=timezone.now())
        services.recover_stale_runs(live_run_id=None)
        run.refresh_from_db()
        self.assertEqual(run.status, RunStatus.FAILED)

    def test_live_run_is_not_recovered(self):
        make_group("A", slug="a")
        run = services.queue_run()
        ScraperRun.objects.filter(pk=run.pk).update(status=RunStatus.RUNNING, worker_pid=os.getpid(),
                                                     heartbeat_at=timezone.now())
        self.assertEqual(services.recover_stale_runs(live_run_id=run.pk), [])

    def test_recent_run_from_other_process_is_not_recovered(self):
        make_group("A", slug="a")
        run = services.queue_run()
        ScraperRun.objects.filter(pk=run.pk).update(status=RunStatus.RUNNING, worker_pid=os.getpid() + 1,
                                                     heartbeat_at=timezone.now())
        self.assertEqual(services.recover_stale_runs(), [])
