import threading
import time

from django.test import TestCase, TransactionTestCase

from dashboard import services
from dashboard.models import GroupStatus, Phase, Post, RunGroup, RunStatus, ScraperRun
from scraper.manager import ScraperManager
from scraper.runner import RunControl, execute_run
from scraper.exceptions import StopRequested

from .helpers import CallbackMockClient, fast_settings, make_group


class StopMechanismTests(TestCase):
    def setUp(self):
        fast_settings(posts_per_group=30, save_frequency=100)
        self.first = make_group("First", slug="first")
        self.second = make_group("Second", slug="second")
        self.third = make_group("Third", slug="third")

    def _run_with_hook(self, hook):
        stop_event = threading.Event()
        run = services.queue_run()

        def factory(cfg, *, log, wait):
            return CallbackMockClient(cfg, log=log, wait=wait, speed=0,
                                      on_extract=lambda client, n: hook(client, n, stop_event, run))

        return execute_run(run.pk, stop_event=stop_event, client_factory=factory)

    def test_stop_event_ends_run_and_keeps_collected_posts(self):
        # Stop during the second group's third scroll.
        def hook(client, n, stop_event, run):
            if "second" in client.current_url and n == 3:
                stop_event.set()

        run = self._run_with_hook(hook)
        self.assertEqual(run.status, RunStatus.STOPPED)
        self.assertEqual(run.current_phase, Phase.STOPPED)
        self.assertEqual(run.completed_groups, 1)

        statuses = dict(RunGroup.objects.filter(run=run).values_list("group__name", "status"))
        self.assertEqual(statuses, {"First": GroupStatus.COMPLETED, "Second": GroupStatus.SKIPPED,
                                    "Third": GroupStatus.SKIPPED})
        # Posts from the interrupted group were flushed even though save_frequency was not reached.
        self.assertGreater(Post.objects.filter(group=self.second).count(), 0)
        self.assertEqual(run.total_posts, Post.objects.count())
        # The stopped group stays in the current round so the next run repeats it.
        self.second.refresh_from_db()
        self.assertEqual(self.second.completed_round, 0)
        self.assertCountEqual([g.name for g in services.next_batch().groups], ["Second", "Third"])

    def test_database_stop_flag_is_honoured(self):
        # Stop requested from "another process" through the DB flag.
        def hook(client, n, stop_event, run):
            if "first" in client.current_url and n == 2:
                ScraperRun.objects.filter(pk=run.pk).update(stop_requested=True)

        original_interval = RunControl.STOP_POLL_INTERVAL
        RunControl.STOP_POLL_INTERVAL = 0
        try:
            run = self._run_with_hook(hook)
        finally:
            RunControl.STOP_POLL_INTERVAL = original_interval
        self.assertEqual(run.status, RunStatus.STOPPED)
        self.assertEqual(run.completed_groups, 0)

    def test_run_control_wait_raises_when_stopped(self):
        run = services.queue_run()
        event = threading.Event()
        control = RunControl(run.pk, event)
        control.wait(0.01)  # no stop: returns normally
        event.set()
        start = time.monotonic()
        with self.assertRaises(StopRequested):
            control.wait(30)
        self.assertLess(time.monotonic() - start, 1)


class ManagerThreadTests(TransactionTestCase):
    """End-to-end: background thread started and stopped through the manager."""

    def test_start_and_stop_background_run(self):
        fast_settings(scroll_delay=0.5, posts_per_group=50, scroll_limit=50)
        for name in ("One", "Two", "Three"):
            make_group(name, slug=name.lower())

        manager = ScraperManager()
        run = manager.start()
        self.assertTrue(manager.is_running())
        with self.assertRaises(services.RunAlreadyActive):
            manager.start()

        deadline = time.monotonic() + 15
        while time.monotonic() < deadline and not ScraperRun.objects.get(pk=run.pk).logs.filter(
                message__startswith="Scroll 01").exists():
            time.sleep(0.1)

        stopped = manager.stop()
        self.assertEqual(stopped.pk, run.pk)
        manager.join(timeout=15)
        self.assertFalse(manager.is_running())

        run.refresh_from_db()
        self.assertEqual(run.status, RunStatus.STOPPED)
        self.assertIsNotNone(run.finished_at)
        self.assertTrue(run.stop_requested)
        self.assertTrue(run.logs.filter(message__startswith="Stop requested by user").exists())
        self.assertIsNone(manager.stop())  # nothing left to stop
