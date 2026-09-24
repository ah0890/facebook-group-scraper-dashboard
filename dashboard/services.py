"""Database-level application logic shared by views, the scraper worker and
management commands: run queueing, rounds/next batch, crash recovery, stats."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from datetime import timedelta

from django.conf import settings
from django.db import transaction
from django.db.models import Avg, Count, F, Min, Q, Sum
from django.db.models.functions import TruncDate
from django.utils import timezone

from .models import (
    ACTIVE_RUN_STATUSES,
    Group,
    GroupStatus,
    LogLevel,
    Phase,
    Post,
    RunGroup,
    RunStatus,
    ScraperLog,
    ScraperRun,
    ScraperSetting,
)


class RunAlreadyActive(Exception):
    pass


class NoGroupsToScrape(Exception):
    pass


class BrowserBusy(Exception):
    pass


# ---------------------------------------------------------------------------
# Rounds & next batch
# ---------------------------------------------------------------------------
#
# A "round" is one pass over every enabled group. Each group remembers the last
# round it completed (Group.completed_round). The current round is the lowest
# unfinished one; its remaining groups are scraped in batches of
# ``groups_per_run``. Because progress is stored per group as soon as the group
# finishes, a crashed or stopped run simply leaves those groups "remaining" and
# the next run picks up exactly where the previous one left off.


@dataclass
class Batch:
    round_number: int
    remaining_count: int
    enabled_count: int
    groups: list[Group] = field(default_factory=list)

    @property
    def message(self) -> str:
        if not self.enabled_count:
            return "No enabled groups. Add or enable groups to start scraping."
        size = len(self.groups)
        if size >= self.remaining_count:
            prefix = f"All {size} remaining groups" if size > 1 else "The only remaining group"
            return f"{prefix} will be scraped to complete Round #{self.round_number}."
        return (f"{size} of {self.remaining_count} remaining groups will be scraped in this batch "
                f"(Round #{self.round_number}).")


def current_round_base() -> int:
    """The completed_round shared by the groups still waiting in the current round."""
    value = Group.objects.filter(enabled=True).aggregate(m=Min("completed_round"))["m"]
    return value or 0


def next_batch(setting: ScraperSetting | None = None) -> Batch:
    setting = setting or ScraperSetting.get_active()
    enabled = Group.objects.filter(enabled=True)
    enabled_count = enabled.count()
    if not enabled_count:
        return Batch(round_number=1, remaining_count=0, enabled_count=0)
    base = current_round_base()
    remaining = enabled.filter(completed_round__lte=base).order_by(F("last_run_at").asc(nulls_first=True), "id")
    return Batch(
        round_number=base + 1,
        remaining_count=remaining.count(),
        enabled_count=enabled_count,
        groups=list(remaining[: setting.groups_per_run]),
    )


def round_for_new_group() -> int:
    """New/re-enabled groups join the current round instead of lagging behind it."""
    return current_round_base()


# ---------------------------------------------------------------------------
# Runs
# ---------------------------------------------------------------------------


def queue_run(*, group_ids: list[int] | None = None, trigger: str = "web", mode: str | None = None,
              live_run_id: int | None = None) -> ScraperRun:
    """Create a QUEUED run with its group queue. Does not start any worker."""
    recover_stale_runs(live_run_id=live_run_id)
    with transaction.atomic():
        if ScraperRun.objects.filter(status__in=ACTIVE_RUN_STATUSES).exists():
            raise RunAlreadyActive("A scraper run is already in progress.")

        setting = ScraperSetting.get_active()
        batch = next_batch(setting)
        if group_ids:
            by_id = Group.objects.in_bulk(group_ids)
            groups = [by_id[i] for i in group_ids if i in by_id]
        else:
            groups = batch.groups
        if not groups:
            raise NoGroupsToScrape("There are no enabled groups to scrape.")

        run = ScraperRun.objects.create(
            status=RunStatus.QUEUED,
            current_phase=Phase.SEEDING,
            mode=mode or setting.scraper_mode,
            trigger=trigger,
            round_number=batch.round_number,
            total_groups=len(groups),
        )
        RunGroup.objects.bulk_create(
            RunGroup(run=run, group=group, position=index) for index, group in enumerate(groups, start=1)
        )
        Group.objects.filter(pk__in=[g.pk for g in groups]).update(last_status=GroupStatus.QUEUED)
    return run


def get_active_run() -> ScraperRun | None:
    return ScraperRun.objects.filter(status__in=ACTIVE_RUN_STATUSES).order_by("-id").first()


def recover_stale_runs(live_run_id: int | None = None) -> list[ScraperRun]:
    """Close runs whose worker is gone (server restart, crash, killed CLI).

    A run is stale if it claims to belong to this process but no live worker
    thread has it, or if its heartbeat is older than SCRAPER_STALE_AFTER_SECONDS.
    Groups completed before the interruption keep their progress.
    """
    threshold = timezone.now() - timedelta(seconds=settings.SCRAPER_STALE_AFTER_SECONDS)
    recovered = []
    for run in ScraperRun.objects.filter(status__in=ACTIVE_RUN_STATUSES):
        if live_run_id is not None and run.pk == live_run_id:
            continue
        orphaned_here = run.worker_pid is not None and run.worker_pid == os.getpid()
        last_sign_of_life = run.heartbeat_at or run.started_at
        if orphaned_here or last_sign_of_life < threshold:
            mark_interrupted(run)
            recovered.append(run)
    return recovered


def mark_interrupted(run: ScraperRun, reason: str | None = None) -> None:
    reason = reason or ("Run interrupted – the worker stopped unexpectedly (server restart or crash). "
                        "Posts and completed groups were saved; remaining groups will be picked up by the next run.")
    now = timezone.now()
    with transaction.atomic():
        pending = RunGroup.objects.filter(run=run, status__in=[GroupStatus.QUEUED, GroupStatus.RUNNING])
        group_ids = list(pending.values_list("group_id", flat=True))
        skipped = pending.update(status=GroupStatus.SKIPPED, finished_at=now, error_message="Run interrupted")
        Group.objects.filter(pk__in=group_ids, last_status__in=[GroupStatus.QUEUED, GroupStatus.RUNNING]).update(
            last_status=GroupStatus.SKIPPED
        )
        run.refresh_from_db()
        run.status = RunStatus.PARTIAL if run.completed_groups else RunStatus.FAILED
        run.current_phase = Phase.FAILED
        run.finished_at = now
        run.skipped_groups += skipped
        run.error_message = reason
        run.current_group = None
        run.save()
        ScraperLog.objects.create(run=run, level=LogLevel.WARNING, phase=Phase.FAILED, message=reason)


def request_stop(run: ScraperRun) -> None:
    ScraperRun.objects.filter(pk=run.pk).update(stop_requested=True)
    ScraperLog.objects.create(
        run=run, level=LogLevel.WARNING, phase=run.current_phase,
        message="Stop requested by user – finishing the current step and saving progress…",
    )


def reset_settings_to_defaults() -> ScraperSetting:
    active = ScraperSetting.get_active()
    active.copy_from(ScraperSetting.get_defaults())
    active.save()
    return active


# ---------------------------------------------------------------------------
# Logs
# ---------------------------------------------------------------------------


def cleanup_logs(days: int) -> int:
    """Delete log rows older than ``days``. Returns the number deleted."""
    cutoff = timezone.now() - timedelta(days=max(0, days))
    deleted, _ = ScraperLog.objects.filter(timestamp__lt=cutoff).delete()
    return deleted


# ---------------------------------------------------------------------------
# Dashboard & stats
# ---------------------------------------------------------------------------


def _start_of_today():
    return timezone.localtime().replace(hour=0, minute=0, second=0, microsecond=0)


def dashboard_summary() -> dict:
    today = _start_of_today()
    last_run = ScraperRun.objects.exclude(status__in=ACTIVE_RUN_STATUSES).order_by("-started_at").first()
    return {
        "total_groups": Group.objects.count(),
        "enabled_groups": Group.objects.filter(enabled=True).count(),
        "total_posts": Post.objects.count(),
        "posts_today": Post.objects.filter(collected_at__gte=today).count(),
        "total_runs": ScraperRun.objects.count(),
        "last_run": last_run,
    }


def average_run_duration(runs) -> timedelta | None:
    durations = [r.finished_at - r.started_at for r in runs if r.finished_at and r.started_at]
    if not durations:
        return None
    return sum(durations, timedelta()) / len(durations)


def stats_summary() -> dict:
    now = timezone.localtime()
    today = _start_of_today()
    week_start = today - timedelta(days=today.weekday())
    month_start = today.replace(day=1)

    total_posts = Post.objects.count()
    groups_with_posts = Group.objects.filter(total_posts__gt=0).count()
    finished = ScraperRun.objects.exclude(status__in=ACTIVE_RUN_STATUSES)

    return {
        "total_groups": Group.objects.count(),
        "active_groups": Group.objects.filter(enabled=True).count(),
        "total_posts": total_posts,
        "posts_today": Post.objects.filter(collected_at__gte=today).count(),
        "posts_week": Post.objects.filter(collected_at__gte=week_start).count(),
        "posts_month": Post.objects.filter(collected_at__gte=month_start).count(),
        "successful_runs": finished.filter(status=RunStatus.COMPLETED).count(),
        "partial_runs": finished.filter(status=RunStatus.PARTIAL).count(),
        "failed_runs": finished.filter(status=RunStatus.FAILED).count(),
        "stopped_runs": finished.filter(status=RunStatus.STOPPED).count(),
        "avg_posts_per_group": round(total_posts / groups_with_posts, 1) if groups_with_posts else 0,
        "avg_run_duration": average_run_duration(finished.only("started_at", "finished_at")[:500]),
        "avg_posts_per_run": round(finished.aggregate(a=Avg("total_posts"))["a"] or 0, 1),
        "total_engagement": Post.objects.aggregate(
            likes=Sum("likes_count"), comments=Sum("comments_count"), shares=Sum("shares_count")
        ),
        "generated_at": now,
    }


def chart_data(days: int = 30) -> dict:
    today = timezone.localdate()
    start_date = today - timedelta(days=days - 1)
    start = _start_of_today() - timedelta(days=days - 1)
    labels = [(start_date + timedelta(days=i)) for i in range(days)]
    label_strings = [d.strftime("%b %d") for d in labels]

    tz = timezone.get_current_timezone()
    posts_by_day = dict(
        Post.objects.filter(collected_at__gte=start)
        .annotate(day=TruncDate("collected_at", tzinfo=tz))
        .values_list("day")
        .annotate(n=Count("id"))
        .values_list("day", "n")
    )

    run_rows = (
        ScraperRun.objects.filter(started_at__gte=start)
        .annotate(day=TruncDate("started_at", tzinfo=tz))
        .values("day")
        .annotate(
            ok=Count("id", filter=Q(status=RunStatus.COMPLETED)),
            partial=Count("id", filter=Q(status__in=[RunStatus.PARTIAL, RunStatus.STOPPED])),
            failed=Count("id", filter=Q(status=RunStatus.FAILED)),
        )
    )
    runs_by_day = {row["day"]: row for row in run_rows}

    by_group = list(
        Group.objects.filter(total_posts__gt=0).order_by("-total_posts").values_list("name", "total_posts")[:12]
    )

    return {
        "days": label_strings,
        "posts": [posts_by_day.get(d, 0) for d in labels],
        "runs_ok": [runs_by_day.get(d, {}).get("ok", 0) for d in labels],
        "runs_partial": [runs_by_day.get(d, {}).get("partial", 0) for d in labels],
        "runs_failed": [runs_by_day.get(d, {}).get("failed", 0) for d in labels],
        "group_names": [name for name, _ in by_group],
        "group_posts": [count for _, count in by_group],
    }
