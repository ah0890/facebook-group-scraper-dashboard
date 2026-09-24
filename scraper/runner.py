"""The scraper run loop.

``execute_run(run_id)`` processes one queued ``ScraperRun`` synchronously. It is
called from a background thread by ``scraper.manager`` (web UI) or directly by
the ``run_scraper`` management command. Tests call it with a fake client.

Guarantees:

* Groups are processed sequentially (1 worker). A failing group is retried up to
  ``retry_count`` times for transient errors, then marked FAILED; the run moves on.
* Posts are saved every ``save_frequency`` posts and at the end of every group.
* Stop requests are honoured at safe checkpoints (between scrolls, during waits);
  pending posts are flushed before the run is marked STOPPED.
* Group progress (``completed_round``) is written as soon as a group finishes, so
  an interrupted round resumes without repeating completed groups.
"""

from __future__ import annotations

import logging
import os
import threading
import time
from typing import Callable, Protocol

from django.db import DatabaseError
from django.db.models import F
from django.utils import timezone

from .exceptions import (
    AuthenticationRequired,
    BrowserClosedError,
    BrowserLaunchError,
    GroupUnavailableError,
    RecoverableScrapeError,
    StopRequested,
    StorageError,
)
from .logger import RunLogger
from .models import MODE_MOCK, ExtractionResult, ScrapeConfig
from .parser import build_dedup_key, parse_raw_post
from .storage import GroupPostStore, refresh_group_totals, write_snapshot

pylog = logging.getLogger("scraper")

FATAL_ERRORS = (AuthenticationRequired, BrowserLaunchError, BrowserClosedError)
EMPTY_SCROLLS_BEFORE_END = 3
KNOWN_SCROLLS_BEFORE_CAUGHT_UP = 2
RETRY_BACKOFF_SECONDS = 5.0  # multiplied by the attempt number, capped at 30s


class ScraperClient(Protocol):
    def start(self) -> None: ...
    def ensure_authenticated(self) -> None: ...
    def open_group(self, url: str) -> None: ...
    def extract_posts(self) -> ExtractionResult: ...
    def scroll(self) -> None: ...
    def close(self) -> None: ...


ClientFactory = Callable[..., ScraperClient]


def default_client_factory(cfg: ScrapeConfig, *, log, wait) -> ScraperClient:
    if cfg.mode == MODE_MOCK:
        from .mock import MockFacebookClient

        return MockFacebookClient(cfg, log=log, wait=wait)
    from .facebook import FacebookClient

    return FacebookClient(cfg, log=log, wait=wait)


class RunControl:
    """Cooperative stop handling + heartbeat for one run.

    Stop can be requested in-process (``stop_event``) or from another process via
    ``ScraperRun.stop_requested`` (e.g. the web UI stopping a CLI run).
    """

    HEARTBEAT_INTERVAL = 5.0
    STOP_POLL_INTERVAL = 2.0

    def __init__(self, run_id: int, stop_event: threading.Event | None = None):
        self.run_id = run_id
        self.stop_event = stop_event or threading.Event()
        self._last_beat = 0.0
        self._last_poll = 0.0

    def stop_requested(self) -> bool:
        if self.stop_event.is_set():
            return True
        now = time.monotonic()
        if now - self._last_poll >= self.STOP_POLL_INTERVAL:
            self._last_poll = now
            from dashboard.models import ScraperRun

            try:
                flag = ScraperRun.objects.filter(pk=self.run_id).values_list("stop_requested", flat=True).first()
            except DatabaseError:
                flag = False
            if flag:
                self.stop_event.set()
                return True
        return False

    def beat(self, force: bool = False) -> None:
        now = time.monotonic()
        if not force and now - self._last_beat < self.HEARTBEAT_INTERVAL:
            return
        self._last_beat = now
        from dashboard.models import ScraperRun

        try:
            ScraperRun.objects.filter(pk=self.run_id).update(heartbeat_at=timezone.now())
        except DatabaseError:
            pylog.warning("Heartbeat update failed", exc_info=True)

    def check(self) -> None:
        """Checkpoint: raise ``StopRequested`` if a stop was requested."""
        self.beat()
        if self.stop_requested():
            raise StopRequested()

    def wait(self, seconds: float) -> None:
        """Sleep in small slices, staying responsive to stop requests."""
        deadline = time.monotonic() + max(0.0, seconds)
        while True:
            self.check()
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return
            self.stop_event.wait(min(0.5, remaining))


# ---------------------------------------------------------------------------
# Run
# ---------------------------------------------------------------------------


def execute_run(run_id: int, *, stop_event: threading.Event | None = None,
                client_factory: ClientFactory | None = None,
                echo: Callable[[str, str], None] | None = None):
    """Process a queued run to completion (or stop/failure). Returns the final run."""
    from dashboard.models import GroupStatus, Phase, RunGroup, RunStatus, ScraperRun, ScraperSetting

    run = ScraperRun.objects.get(pk=run_id)
    setting = ScraperSetting.get_active()
    cfg = setting.to_config(mode=run.mode)
    control = RunControl(run.pk, stop_event)
    log = RunLogger(run, cfg.log_level, echo)
    factory = client_factory or default_client_factory

    ScraperRun.objects.filter(pk=run.pk).update(
        status=RunStatus.RUNNING, started_at=timezone.now(), worker_pid=os.getpid(), heartbeat_at=timezone.now()
    )
    run.refresh_from_db()

    client: ScraperClient | None = None
    stopped = False
    fatal_error = ""

    try:
        log.set_phase(Phase.SEEDING)
        queue = list(RunGroup.objects.filter(run=run, status=GroupStatus.QUEUED).select_related("group"))
        _log_banner(log, run, cfg, len(queue))
        _cleanup_old_logs(log, setting.log_retention_days)
        control.check()

        log.set_phase(Phase.INITIALIZING)
        client = factory(cfg, log=log.log, wait=control.wait)
        client.start()
        control.check()

        log.set_phase(Phase.AUTHENTICATING)
        client.ensure_authenticated()
        _record_session(cfg, ok=True, message=f"Session verified by run #{run.pk}.")

        for entry in queue:
            control.check()
            _process_group(run, entry, client, cfg, control, log)

    except StopRequested:
        stopped = True
        log.warning("Stop requested – progress saved, ending run.")
    except AuthenticationRequired as exc:
        fatal_error = str(exc)
        log.error(f"Authentication required: {exc}")
        _record_session(cfg, ok=False, message=str(exc))
    except (BrowserLaunchError, BrowserClosedError) as exc:
        fatal_error = str(exc)
        log.error(str(exc))
    except Exception as exc:  # last line of defence – never leave a run stuck in RUNNING
        pylog.exception("Unexpected error in run %s", run.pk)
        fatal_error = f"Unexpected error: {exc.__class__.__name__}: {exc}"
        log.error(fatal_error)
    finally:
        if client is not None:
            try:
                client.close()
            except Exception:
                pylog.debug("Error closing client", exc_info=True)
        run = _finalize_run(run, log, stopped=stopped, fatal_error=fatal_error)
    return run


def _log_banner(log: RunLogger, run, cfg: ScrapeConfig, queued: int) -> None:
    if cfg.mode == MODE_MOCK:
        mode = "MOCK (simulated data – Facebook is not contacted)"
    else:
        mode = f"PLAYWRIGHT (Chromium, {'headless' if cfg.headless else 'visible'})"
    saving = f"every {cfg.save_frequency} posts"
    if cfg.save_after_every_group:
        saving += " + after every group -> fetched_posts.json"
    log.info("Facebook Group Fetcher")
    log.info("Phase    : SEEDING")
    log.info(f"Mode     : {mode}")
    log.info(f"Round    : #{run.round_number}")
    log.info(f"Groups   : {queued} queued this run")
    log.info("Workers  : 1")
    log.info(f"Target   : {cfg.posts_per_group} posts/group | scroll limit {cfg.scroll_limit} | "
             f"delay {cfg.scroll_delay:g}s | retries {cfg.retry_count}")
    log.info(f"Saving   : {saving}")
    log.rule()


def _cleanup_old_logs(log: RunLogger, days: int) -> None:
    from dashboard.services import cleanup_logs

    try:
        deleted = cleanup_logs(days)
        if deleted:
            log.debug(f"Removed {deleted} log entries older than {days} days")
    except DatabaseError:
        pylog.warning("Log cleanup failed", exc_info=True)


def _record_session(cfg: ScrapeConfig, *, ok: bool, message: str) -> None:
    from dashboard.models import BrowserSession

    if cfg.mode == MODE_MOCK:
        return
    status = BrowserSession.Status.AUTHENTICATED if ok else BrowserSession.Status.NOT_AUTHENTICATED
    try:
        BrowserSession.record(status, message)
    except DatabaseError:
        pass


# ---------------------------------------------------------------------------
# Group
# ---------------------------------------------------------------------------


def _process_group(run, entry, client: ScraperClient, cfg: ScrapeConfig, control: RunControl,
                   log: RunLogger) -> None:
    from dashboard.models import Group, GroupStatus, ScraperRun

    group = entry.group
    log.set_group(group)
    now = timezone.now()
    ScraperRun.objects.filter(pk=run.pk).update(current_group=group, heartbeat_at=now)
    entry.status, entry.started_at = GroupStatus.RUNNING, now
    entry.save(update_fields=["status", "started_at"])
    Group.objects.filter(pk=group.pk).update(last_status=GroupStatus.RUNNING)

    store = GroupPostStore(run, group, cfg.save_frequency)
    first_fetch = store.is_first_fetch
    log.rule()
    log.info(f"Group : {group.name}")
    log.info(f"URL   : {group.facebook_url}")
    if first_fetch:
        log.info(f"Mode  : FIRST fetch - scraping up to {cfg.posts_per_group} posts")
    else:
        log.info(f"Mode  : UPDATE fetch - up to {cfg.posts_per_group} new posts, stops when caught up")

    seen: set[str] = set()
    error: Exception | None = None
    attempt = 0
    try:
        while True:
            attempt += 1
            entry.attempts = attempt
            entry.save(update_fields=["attempts"])
            try:
                _scrape_feed(client, cfg, group, store, seen, control, log, update_mode=not first_fetch)
                error = None
                break
            except RecoverableScrapeError as exc:
                error = exc
                _flush(store, entry, log)
                if attempt > cfg.retry_count:
                    break
                backoff = min(30.0, RETRY_BACKOFF_SECONDS * attempt)
                log.warning(f"{exc} Retrying in {backoff:g}s (attempt {attempt + 1}/{cfg.retry_count + 1})")
                control.wait(backoff)
            except (GroupUnavailableError, StorageError) as exc:
                error = exc
                break
            except (StopRequested, *FATAL_ERRORS):
                raise
            except Exception as exc:  # unexpected page structure etc. – fail this group, keep the run going
                pylog.exception("Unexpected error while scraping %s", group.facebook_url)
                error = exc
                break
    except StopRequested:
        _flush(store, entry, log)
        _finish_group(run, entry, GroupStatus.SKIPPED, store,
                      f"Stopped by user after {store.saved_count} new posts")
        raise
    except FATAL_ERRORS as exc:
        _flush(store, entry, log)
        _finish_group(run, entry, GroupStatus.FAILED, store, str(exc))
        raise

    _flush(store, entry, log)
    if error is not None:
        _finish_group(run, entry, GroupStatus.FAILED, store, str(error))
        tries = f" after {attempt} attempts" if attempt > 1 else ""
        log.error(f"✗ Group failed{tries}: {error}")
    else:
        _finish_group(run, entry, GroupStatus.COMPLETED, store)
        log.success(f"✓ Group complete – {store.saved_count} new posts, {store.duplicate_count} already saved")

    if cfg.save_after_every_group:
        _write_snapshot(run, cfg, log)
    log.set_group(None)


def _scrape_feed(client: ScraperClient, cfg: ScrapeConfig, group, store: GroupPostStore, seen: set[str],
                 control: RunControl, log: RunLogger, *, update_mode: bool) -> None:
    from dashboard.models import Phase

    log.set_phase(Phase.FETCHING)
    client.open_group(group.facebook_url)

    target = cfg.posts_per_group
    empty_scrolls = 0
    known_scrolls = 0
    for scroll_no in range(1, cfg.scroll_limit + 1):
        control.check()
        log.set_phase(Phase.PARSING)
        result = client.extract_posts()

        fresh = []
        known = 0
        for raw in result.raw_posts:
            if store.new_count >= target:
                break
            post = parse_raw_post(raw)
            if post is None:
                continue
            key = build_dedup_key(group.pk, post)
            if key in seen:
                continue
            seen.add(key)
            if store.add(post):
                fresh.append(post)
            else:
                known += 1

        line = (f"Scroll {scroll_no:02d} | DOM: {result.dom_count:02d} | "
                f"collected: {store.new_count:02d} | +{len(fresh)}")
        if known:
            line += f" | {known} already saved"
        log.info(line)
        for post in fresh:
            log.success(f"+ [{post.author_name or 'Unknown author'}] Post collected")
            log.debug(f"  {(post.post_text or '')[:100]!r}")

        if store.should_flush():
            log.set_phase(Phase.SAVING)
            saved = store.flush()
            log.debug(f"Saved {saved} posts to the database")

        if store.new_count >= target:
            log.info(f"Target reached: {target} posts")
            return
        if fresh:
            empty_scrolls = known_scrolls = 0
        elif known:
            known_scrolls += 1
            if update_mode and known_scrolls >= KNOWN_SCROLLS_BEFORE_CAUGHT_UP:
                log.info("Caught up with previously saved posts – moving on")
                return
        else:
            empty_scrolls += 1
        if empty_scrolls + known_scrolls >= EMPTY_SCROLLS_BEFORE_END:
            log.info(f"No new posts after {EMPTY_SCROLLS_BEFORE_END} scrolls – end of available feed")
            return
        if scroll_no == cfg.scroll_limit:
            log.warning(f"Scroll limit ({cfg.scroll_limit}) reached with {store.new_count}/{target} posts")
            return

        log.set_phase(Phase.SCROLLING)
        client.scroll()
        control.wait(cfg.scroll_delay)


def _flush(store: GroupPostStore, entry, log: RunLogger) -> None:
    """Save pending posts and record group counters; never raises."""
    from dashboard.models import Phase

    try:
        if store.pending:
            log.set_phase(Phase.SAVING)
        store.flush()
    except StorageError as exc:
        log.error(str(exc))
    try:
        entry.posts_collected = store.saved_count
        entry.duplicates = store.duplicate_count
        entry.save(update_fields=["posts_collected", "duplicates"])
    except DatabaseError:
        pylog.warning("Could not update group progress", exc_info=True)


def _finish_group(run, entry, status: str, store: GroupPostStore, error: str = "") -> None:
    from dashboard.models import Group, GroupStatus, ScraperRun

    now = timezone.now()
    entry.status = status
    entry.finished_at = now
    entry.error_message = error
    entry.posts_collected = store.saved_count
    entry.duplicates = store.duplicate_count
    entry.save()

    group = entry.group
    refresh_group_totals(group)
    updates = {"last_status": status, "last_run_at": now, "last_error": error}
    if status == GroupStatus.COMPLETED:
        updates["completed_round"] = max(group.completed_round, run.round_number)
    Group.objects.filter(pk=group.pk).update(**updates)

    counter = {
        GroupStatus.COMPLETED: "completed_groups",
        GroupStatus.FAILED: "failed_groups",
        GroupStatus.SKIPPED: "skipped_groups",
    }[status]
    ScraperRun.objects.filter(pk=run.pk).update(**{counter: F(counter) + 1}, heartbeat_at=now)


def _write_snapshot(run, cfg: ScrapeConfig, log: RunLogger) -> None:
    from dashboard.models import Phase

    log.set_phase(Phase.SAVING)
    try:
        path = write_snapshot(run, cfg.export_dir)
        log.info(f"Saved progress -> {path.name}")
    except (OSError, DatabaseError) as exc:
        log.warning(f"Could not write JSON snapshot: {exc}")


# ---------------------------------------------------------------------------
# Finalisation
# ---------------------------------------------------------------------------


def _finalize_run(run, log: RunLogger, *, stopped: bool, fatal_error: str):
    from dashboard.models import Group, GroupStatus, Phase, RunGroup, RunStatus, ScraperRun

    now = timezone.now()
    try:
        pending = RunGroup.objects.filter(run=run, status__in=[GroupStatus.QUEUED, GroupStatus.RUNNING])
        pending_group_ids = list(pending.values_list("group_id", flat=True))
        reason = "Run stopped" if stopped else ("Run aborted: " + fatal_error if fatal_error else "Not processed")
        skipped = pending.update(status=GroupStatus.SKIPPED, finished_at=now, error_message=reason[:500])
        Group.objects.filter(
            pk__in=pending_group_ids, last_status__in=[GroupStatus.QUEUED, GroupStatus.RUNNING]
        ).update(last_status=GroupStatus.SKIPPED)

        run.refresh_from_db()
        run.skipped_groups += skipped
        if stopped:
            status, phase = RunStatus.STOPPED, Phase.STOPPED
        elif fatal_error:
            status = RunStatus.PARTIAL if run.completed_groups else RunStatus.FAILED
            phase = Phase.FAILED
        elif run.failed_groups == 0 and run.completed_groups == run.total_groups:
            status, phase = RunStatus.COMPLETED, Phase.COMPLETED
        elif run.completed_groups:
            status, phase = RunStatus.PARTIAL, Phase.COMPLETED
        else:
            status, phase = RunStatus.FAILED, Phase.FAILED

        error_message = fatal_error
        if not error_message and run.failed_groups:
            error_message = f"{run.failed_groups} group(s) failed – see logs for details."

        log.set_group(None)
        log.set_phase(phase)
        ScraperRun.objects.filter(pk=run.pk).update(
            status=status, current_phase=phase, finished_at=now, skipped_groups=run.skipped_groups,
            error_message=error_message, current_group=None, heartbeat_at=now,
        )
        run.refresh_from_db()

        duration = run.duration
        seconds = int(duration.total_seconds()) if duration else 0
        summary = (f"Run #{run.pk} {status} | groups: {run.completed_groups} completed, "
                   f"{run.failed_groups} failed, {run.skipped_groups} skipped | new posts: {run.total_posts} | "
                   f"duplicates: {run.duplicate_posts} | duration: {seconds // 60}m {seconds % 60:02d}s")
        log.rule()
        if status == RunStatus.COMPLETED:
            log.success(summary)
        elif status == RunStatus.FAILED:
            log.error(summary)
        else:
            log.warning(summary)
    except DatabaseError:
        pylog.exception("Could not finalise run %s", run.pk)
    return run
