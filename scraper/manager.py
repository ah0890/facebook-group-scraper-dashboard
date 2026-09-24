"""Background execution for the web UI.

One ``ScraperManager`` per web process owns at most one scraper thread and one
browser-task thread (authenticate / session check / URL test). Both use the same
persistent Chromium profile, which can only be opened once, so they are mutually
exclusive.
"""

from __future__ import annotations

import logging
import threading

from django.db import connections

from .models import MODE_MOCK
from .runner import execute_run

pylog = logging.getLogger("scraper")


class ScraperManager:
    def __init__(self):
        self._lock = threading.RLock()
        self._thread: threading.Thread | None = None
        self._run_id: int | None = None
        self._stop_event: threading.Event | None = None
        self._browser_thread: threading.Thread | None = None
        self._browser_cancel = threading.Event()

    # -- state ------------------------------------------------------------------

    def is_running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def live_run_id(self) -> int | None:
        return self._run_id if self.is_running() else None

    def browser_busy(self) -> bool:
        return self._browser_thread is not None and self._browser_thread.is_alive()

    def active_run(self):
        """The active run (if any), after closing runs whose worker has died."""
        from dashboard import services

        services.recover_stale_runs(live_run_id=self.live_run_id())
        return services.get_active_run()

    # -- scraper ------------------------------------------------------------------

    def start(self, *, group_ids: list[int] | None = None, trigger: str = "web"):
        from dashboard import services

        with self._lock:
            if self.is_running():
                raise services.RunAlreadyActive("A scraper run is already in progress.")
            if self.browser_busy():
                raise services.BrowserBusy("A browser task is in progress. Wait for it to finish first.")
            run = services.queue_run(group_ids=group_ids, trigger=trigger, live_run_id=None)
            self._stop_event = threading.Event()
            self._run_id = run.pk
            self._thread = threading.Thread(
                target=self._worker, args=(run.pk, self._stop_event), name=f"scraper-run-{run.pk}", daemon=True
            )
            self._thread.start()
            return run

    def _worker(self, run_id: int, stop_event: threading.Event) -> None:
        try:
            execute_run(run_id, stop_event=stop_event)
        except Exception:
            pylog.exception("Scraper worker crashed (run %s)", run_id)
        finally:
            connections.close_all()

    def stop(self):
        """Ask the active run to stop at the next safe checkpoint."""
        from dashboard import services

        with self._lock:
            run = self.active_run()
            if run is None:
                return None
            services.request_stop(run)
            if self._run_id == run.pk and self._stop_event is not None:
                self._stop_event.set()
            return run

    def join(self, timeout: float | None = None) -> None:
        if self._thread is not None:
            self._thread.join(timeout)

    # -- browser tasks ------------------------------------------------------------

    def start_browser_task(self, kind: str, group=None) -> dict:
        """Start ``authenticate``, ``check`` or ``test_url``.

        Returns ``{"async": bool, "status": ..., "message": ...}``. In mock mode the
        result is immediate; in Playwright mode the task runs in the background and
        its result appears in ``BrowserSession`` (polled by the UI).
        """
        from dashboard import services
        from dashboard.models import BrowserSession, ScraperSetting

        if kind not in {"authenticate", "check", "test_url"}:
            raise ValueError(f"Unknown browser task {kind!r}")
        with self._lock:
            if self.browser_busy():
                raise services.BrowserBusy("Another browser task is already running.")
            cfg = ScraperSetting.get_active().to_config()
            if cfg.mode == MODE_MOCK:
                return self._mock_browser_task(kind, group)
            if self.is_running() or services.get_active_run() is not None:
                raise services.BrowserBusy(
                    "Stop the scraper first – the browser profile can only be used by one browser at a time."
                )
            waiting = {"authenticate": "Opening browser…", "check": "Checking session…",
                       "test_url": f"Testing {group.name if group else 'URL'}…"}[kind]
            BrowserSession.objects.get_or_create(pk=1)
            BrowserSession.objects.filter(pk=1).update(task=kind, message=waiting)
            if kind != "test_url":
                BrowserSession.record(BrowserSession.Status.CHECKING, waiting, task=kind)
            self._browser_cancel.clear()
            self._browser_thread = threading.Thread(
                target=self._browser_worker, args=(kind, cfg, group.pk if group else None),
                name=f"browser-{kind}", daemon=True,
            )
            self._browser_thread.start()
            return {"async": True, "status": "PENDING", "message": waiting}

    def cancel_browser_task(self) -> None:
        self._browser_cancel.set()

    def _browser_worker(self, kind: str, cfg, group_id: int | None) -> None:
        from dashboard.models import BrowserSession, Group

        from . import facebook

        def report(status: str, message: str) -> None:
            BrowserSession.record(status, message, task=kind if status == "WAITING" else "")

        try:
            if kind == "authenticate":
                facebook.authenticate_interactively(cfg, report=report, should_cancel=self._browser_cancel.is_set)
            elif kind == "check":
                ok, message = facebook.check_session(cfg)
                report("AUTHENTICATED" if ok else "NOT_AUTH", message)
            elif kind == "test_url":
                group = Group.objects.get(pk=group_id)
                status, message = facebook.test_group_url(cfg, group.facebook_url)
                _save_url_check(group, status, message)
                BrowserSession.objects.filter(pk=1).update(message=f"URL test – {group.name}: {message}"[:500])
        except Exception as exc:
            pylog.exception("Browser task %s failed", kind)
            report("ERROR", f"{kind} failed: {exc}")
        finally:
            BrowserSession.objects.filter(pk=1).update(task="")
            connections.close_all()

    def _mock_browser_task(self, kind: str, group) -> dict:
        from dashboard.models import BrowserSession

        if kind == "test_url":
            url = group.facebook_url
            if "broken" in url:
                status, message = "ERROR", "Simulated: the page failed to load."
            elif "private" in url:
                status, message = "WARNING", "Simulated: this account is not a member of the group."
            else:
                status, message = "OK", "Mock mode: URL is a valid group URL (feed simulated)."
            _save_url_check(group, status, message)
            return {"async": False, "status": status, "message": message}

        message = ("Mock mode is active – no Facebook login is needed. Switch the scraper mode to "
                   "Playwright in Settings to use a real browser session.")
        BrowserSession.record(BrowserSession.Status.MOCK, message)
        return {"async": False, "status": "MOCK", "message": message}


def _save_url_check(group, status: str, message: str) -> None:
    from django.utils import timezone

    from dashboard.models import Group

    Group.objects.filter(pk=group.pk).update(
        url_check_status=status, url_check_message=message[:300], url_checked_at=timezone.now()
    )


manager = ScraperManager()
