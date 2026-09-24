from __future__ import annotations

import io
import sqlite3
from datetime import datetime, time as dt_time
from pathlib import Path

from django.conf import settings
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.paginator import Paginator
from django.db import connection
from django.db.models import Q
from django.http import HttpResponse, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views.decorators.http import require_GET, require_POST

from scraper.manager import manager

from . import services
from .exports import posts_to_json, write_csv
from .forms import ClearLogsForm, GroupForm, LogFilterForm, PostFilterForm, ScraperSettingForm
from .models import (
    BrowserSession,
    Group,
    Phase,
    Post,
    RunGroup,
    RunStatus,
    ScraperLog,
    ScraperRun,
    ScraperSetting,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _wants_json(request) -> bool:
    return (request.headers.get("x-requested-with") == "XMLHttpRequest"
            or "application/json" in request.headers.get("accept", ""))


def _respond(request, ok: bool, message: str, redirect_to: str, /, **extra):
    """JSON for fetch() callers, flash message + redirect for plain form posts.

    ``extra`` is merged into the JSON payload (it may repeat ``message``).
    """
    if _wants_json(request):
        return JsonResponse({**extra, "ok": ok, "message": message}, status=200 if ok else 400)
    (messages.success if ok else messages.error)(request, message)
    return redirect(redirect_to)


def _paginate(request, queryset, default_per_page: int = 25):
    try:
        per_page = int(request.GET.get("per_page", default_per_page))
    except ValueError:
        per_page = default_per_page
    per_page = per_page if per_page in (25, 50, 100, 200) else default_per_page
    return Paginator(queryset, per_page).get_page(request.GET.get("page"))


def _day_bounds(day):
    tz = timezone.get_current_timezone()
    start = timezone.make_aware(datetime.combine(day, dt_time.min), tz)
    end = timezone.make_aware(datetime.combine(day, dt_time.max), tz)
    return start, end


def _fmt_time(value) -> str | None:
    return timezone.localtime(value).strftime("%H:%M:%S") if value else None


def _fmt_datetime(value) -> str | None:
    return timezone.localtime(value).strftime("%Y-%m-%d %H:%M") if value else None


# ---------------------------------------------------------------------------
# Dashboard & stats
# ---------------------------------------------------------------------------


@login_required
def dashboard(request):
    active_run = manager.active_run()
    context = {
        "summary": services.dashboard_summary(),
        "active_run": active_run,
        "current_phase": active_run.current_phase if active_run else Phase.IDLE,
        "recent_runs": ScraperRun.objects.all()[:10],
        "batch": services.next_batch(),
        "session": BrowserSession.get(),
    }
    return render(request, "dashboard/dashboard.html", context)


@login_required
def stats(request):
    context = {
        "stats": services.stats_summary(),
        "chart_data": services.chart_data(days=30),
        "top_groups": Group.objects.order_by("-total_posts")[:10],
    }
    return render(request, "dashboard/stats.html", context)


# ---------------------------------------------------------------------------
# Groups
# ---------------------------------------------------------------------------


@login_required
def group_list(request):
    if request.method == "POST":
        form = GroupForm(request.POST)
        if form.is_valid():
            group = form.save(commit=False)
            group.completed_round = services.round_for_new_group()
            group.save()
            messages.success(request, f"Group “{group.name}” added.")
            return redirect("groups")
    else:
        form = GroupForm()

    groups = Group.objects.all()
    q = request.GET.get("q", "").strip()
    if q:
        groups = groups.filter(Q(name__icontains=q) | Q(facebook_url__icontains=q))
    state = request.GET.get("state", "")
    if state == "enabled":
        groups = groups.filter(enabled=True)
    elif state == "disabled":
        groups = groups.filter(enabled=False)

    return render(request, "dashboard/groups.html", {
        "form": form,
        "groups": groups,
        "q": q,
        "state": state,
        "show_form": request.method == "POST" or request.GET.get("add") == "1",
        "active_run": manager.active_run(),
    })


@login_required
def group_edit(request, pk: int):
    group = get_object_or_404(Group, pk=pk)
    form = GroupForm(request.POST or None, instance=group)
    if request.method == "POST" and form.is_valid():
        form.save()
        messages.success(request, f"Group “{group.name}” updated.")
        return redirect("groups")
    return render(request, "dashboard/group_form.html", {"form": form, "group": group})


@login_required
@require_POST
def group_delete(request, pk: int):
    group = get_object_or_404(Group, pk=pk)
    active = manager.active_run()
    if active and RunGroup.objects.filter(run=active, group=group).exists():
        return _respond(request, False, "This group is part of the running scrape. Stop the run first.", "groups")
    name = group.name
    group.delete()
    return _respond(request, True, f"Group “{name}” and its posts were deleted.", "groups")


@login_required
@require_POST
def group_toggle(request, pk: int):
    group = get_object_or_404(Group, pk=pk)
    if not group.enabled:
        group.completed_round = max(group.completed_round, services.round_for_new_group())
    group.enabled = not group.enabled
    group.save(update_fields=["enabled", "completed_round", "updated_at"])
    state = "enabled" if group.enabled else "disabled"
    return _respond(request, True, f"Group “{group.name}” {state}.", "groups", enabled=group.enabled)


@login_required
@require_POST
def group_run(request, pk: int):
    group = get_object_or_404(Group, pk=pk)
    try:
        run = manager.start(group_ids=[group.pk], trigger="group")
    except (services.RunAlreadyActive, services.NoGroupsToScrape, services.BrowserBusy) as exc:
        return _respond(request, False, str(exc), "groups")
    return _respond(request, True, f"Run #{run.pk} started for “{group.name}”.", "run_scraper", run_id=run.pk)


@login_required
@require_POST
def group_test(request, pk: int):
    group = get_object_or_404(Group, pk=pk)
    try:
        result = manager.start_browser_task("test_url", group)
    except services.BrowserBusy as exc:
        return _respond(request, False, str(exc), "groups")
    return _respond(request, result["status"] in ("OK", "PENDING"), result["message"], "groups", **result)


# ---------------------------------------------------------------------------
# Posts
# ---------------------------------------------------------------------------


def _filtered_posts(request):
    form = PostFilterForm(request.GET or None)
    posts = Post.objects.select_related("group")
    if form.is_valid():
        data = form.cleaned_data
        if data["q"]:
            posts = posts.filter(Q(post_text__icontains=data["q"]) | Q(author_name__icontains=data["q"]))
        if data["group"]:
            posts = posts.filter(group=data["group"])
        if data["author"]:
            posts = posts.filter(author_name__icontains=data["author"])
        field = "post_timestamp" if data["date_field"] == "posted" else "collected_at"
        if data["date_from"]:
            posts = posts.filter(**{f"{field}__gte": _day_bounds(data["date_from"])[0]})
        if data["date_to"]:
            posts = posts.filter(**{f"{field}__lte": _day_bounds(data["date_to"])[1]})
        if data["sort"]:
            posts = posts.order_by(data["sort"], "-id")
    return form, posts


@login_required
def post_list(request):
    form, posts = _filtered_posts(request)
    page = _paginate(request, posts)
    return render(request, "dashboard/posts.html", {
        "form": form,
        "page": page,
        "total": page.paginator.count,
        "querystring": request.GET.urlencode(),
    })


def _export_filename(ext: str) -> str:
    return f"facebook_posts_{timezone.localtime():%Y%m%d_%H%M%S}.{ext}"


@login_required
@require_GET
def export_csv(request):
    _, posts = _filtered_posts(request)
    buffer = io.StringIO()
    write_csv(posts.iterator(chunk_size=500), buffer)
    # UTF-8 BOM so Excel opens non-ASCII text correctly.
    response = HttpResponse("﻿" + buffer.getvalue(), content_type="text/csv; charset=utf-8")
    response["Content-Disposition"] = f'attachment; filename="{_export_filename("csv")}"'
    return response


@login_required
@require_GET
def export_json(request):
    _, posts = _filtered_posts(request)
    response = HttpResponse(posts_to_json(posts.iterator(chunk_size=500)),
                            content_type="application/json; charset=utf-8")
    response["Content-Disposition"] = f'attachment; filename="{_export_filename("json")}"'
    return response


# ---------------------------------------------------------------------------
# Run scraper
# ---------------------------------------------------------------------------


@login_required
def run_scraper(request):
    active_run = manager.active_run()
    latest_run = active_run or ScraperRun.objects.first()
    setting = ScraperSetting.get_active()
    return render(request, "dashboard/run_scraper.html", {
        "active_run": active_run,
        "latest_run": latest_run,
        "run_groups": active_run.run_groups.select_related("group") if active_run else [],
        "batch": services.next_batch(setting),
        "setting": setting,
        "session": BrowserSession.get(),
        "browser_busy": manager.browser_busy(),
    })


@login_required
@require_POST
def run_start(request):
    try:
        run = manager.start(trigger="web")
    except (services.RunAlreadyActive, services.NoGroupsToScrape, services.BrowserBusy) as exc:
        return _respond(request, False, str(exc), "run_scraper")
    return _respond(request, True, f"Run #{run.pk} started with {run.total_groups} groups.", "run_scraper",
                    run_id=run.pk)


@login_required
@require_POST
def run_stop(request):
    run = manager.stop()
    if run is None:
        return _respond(request, False, "No scraper run is in progress.", "run_scraper")
    return _respond(request, True, f"Stopping run #{run.pk} – finishing the current step and saving progress.",
                    "run_scraper", run_id=run.pk)


@login_required
@require_POST
def run_reset_settings(request):
    services.reset_settings_to_defaults()
    return _respond(request, True, "Scraper settings reset to your saved defaults.", "run_scraper")


@login_required
def run_detail(request, pk: int):
    run = get_object_or_404(ScraperRun, pk=pk)
    return render(request, "dashboard/run_detail.html", {
        "run": run,
        "run_groups": run.run_groups.select_related("group"),
        "active_run": manager.active_run(),
    })


# ---------------------------------------------------------------------------
# Browser session
# ---------------------------------------------------------------------------


@login_required
@require_POST
def browser_authenticate(request):
    try:
        result = manager.start_browser_task("authenticate")
    except services.BrowserBusy as exc:
        return _respond(request, False, str(exc), "run_scraper")
    return _respond(request, True, result["message"], "run_scraper", **result)


@login_required
@require_POST
def browser_check(request):
    try:
        result = manager.start_browser_task("check")
    except services.BrowserBusy as exc:
        return _respond(request, False, str(exc), "run_scraper")
    return _respond(request, True, result["message"], "run_scraper", **result)


# ---------------------------------------------------------------------------
# Logs
# ---------------------------------------------------------------------------


@login_required
def logs(request):
    form = LogFilterForm(request.GET or None)
    entries = ScraperLog.objects.select_related("group").order_by("-id")
    if form.is_valid():
        data = form.cleaned_data
        if data["q"]:
            entries = entries.filter(message__icontains=data["q"])
        if data["run"]:
            entries = entries.filter(run=data["run"])
        if data["group"]:
            entries = entries.filter(group=data["group"])
        if data["level"]:
            entries = entries.filter(level=data["level"])
        if data["phase"]:
            entries = entries.filter(phase=data["phase"])
        if data["date"]:
            entries = entries.filter(timestamp__range=_day_bounds(data["date"]))
    page = _paginate(request, entries, default_per_page=100)
    retention = ScraperSetting.get_active().log_retention_days
    return render(request, "dashboard/logs.html", {
        "form": form,
        "page": page,
        "total": page.paginator.count,
        "clear_form": ClearLogsForm(initial={"days": retention}),
    })


@login_required
@require_POST
def logs_clear(request):
    form = ClearLogsForm(request.POST)
    if not form.is_valid():
        return _respond(request, False, "Please confirm the deletion by ticking the confirmation box.", "logs")
    deleted = services.cleanup_logs(form.cleaned_data["days"])
    return _respond(request, True, f"Deleted {deleted} log entries.", "logs", deleted=deleted)


# ---------------------------------------------------------------------------
# Settings & defaults
# ---------------------------------------------------------------------------


@login_required
def settings_view(request):
    setting = ScraperSetting.get_active()
    if request.method == "POST" and request.POST.get("action") == "load_defaults":
        services.reset_settings_to_defaults()
        messages.success(request, "Settings replaced with your saved defaults.")
        return redirect("settings")
    form = ScraperSettingForm(request.POST or None, instance=setting)
    if request.method == "POST" and form.is_valid():
        form.save()
        messages.success(request, "Settings saved. They apply to the next run.")
        return redirect("settings")
    return render(request, "dashboard/settings.html", {
        "form": form,
        "database_path": settings.DATABASE_PATH,
        "env_mode": settings.SCRAPER_MODE,
        "active_run": manager.active_run(),
    })


@login_required
def defaults_view(request):
    defaults = ScraperSetting.get_defaults()
    if request.method == "POST" and request.POST.get("action") == "reset":
        defaults.reset_to_factory()
        defaults.save()
        messages.success(request, "Defaults restored to the safe application defaults.")
        return redirect("defaults")
    form = ScraperSettingForm(request.POST or None, instance=defaults)
    if request.method == "POST" and form.is_valid():
        form.save()
        messages.success(request, "Defaults saved. Use “Reset to Defaults” on the Run Scraper page to apply them.")
        return redirect("defaults")
    return render(request, "dashboard/defaults.html", {"form": form})


# ---------------------------------------------------------------------------
# Database info
# ---------------------------------------------------------------------------


def _file_size(path: Path) -> int:
    try:
        return path.stat().st_size
    except OSError:
        return 0


@login_required
def db_view(request):
    if request.method == "POST":
        if manager.active_run() is not None:
            messages.error(request, "Stop the scraper before optimising the database.")
        else:
            with connection.cursor() as cursor:
                cursor.execute("PRAGMA optimize;")
                cursor.execute("PRAGMA wal_checkpoint(TRUNCATE);")
            messages.success(request, "Database optimised and write-ahead log checkpointed.")
        return redirect("db")

    db_path = Path(settings.DATABASE_PATH)
    with connection.cursor() as cursor:
        cursor.execute("PRAGMA journal_mode;")
        journal_mode = cursor.fetchone()[0]
    tables = [
        ("Groups", Group.objects.count()),
        ("Posts", Post.objects.count()),
        ("Scraper runs", ScraperRun.objects.count()),
        ("Run group entries", RunGroup.objects.count()),
        ("Log entries", ScraperLog.objects.count()),
    ]
    return render(request, "dashboard/db.html", {
        "db_path": db_path,
        "db_size": _file_size(db_path),
        "wal_size": _file_size(db_path.with_name(db_path.name + "-wal")),
        "sqlite_version": sqlite3.sqlite_version,
        "journal_mode": journal_mode,
        "tables": tables,
        "oldest_log": ScraperLog.objects.order_by("id").values_list("timestamp", flat=True).first(),
    })


# ---------------------------------------------------------------------------
# JSON API (polled by dashboard.js)
# ---------------------------------------------------------------------------


def _serialize_run(run: ScraperRun | None) -> dict | None:
    if run is None:
        return None
    duration = run.duration
    return {
        "id": run.pk,
        "status": run.status,
        "status_label": run.get_status_display(),
        "phase": run.current_phase,
        "is_active": run.is_active,
        "mode": run.mode,
        "round": run.round_number,
        "started_at": _fmt_datetime(run.started_at),
        "finished_at": _fmt_datetime(run.finished_at),
        "duration_seconds": int(duration.total_seconds()) if duration else None,
        "total_groups": run.total_groups,
        "completed_groups": run.completed_groups,
        "failed_groups": run.failed_groups,
        "skipped_groups": run.skipped_groups,
        "total_posts": run.total_posts,
        "duplicate_posts": run.duplicate_posts,
        "progress": run.progress_percent,
        "current_group": run.current_group.name if run.current_group_id else None,
        "stop_requested": run.stop_requested,
        "error_message": run.error_message,
    }


@login_required
@require_GET
def api_status(request):
    active = manager.active_run()
    run_param = request.GET.get("run")
    if run_param and run_param.isdigit():
        run = ScraperRun.objects.filter(pk=int(run_param)).select_related("current_group").first()
    else:
        run = active or ScraperRun.objects.select_related("current_group").first()

    try:
        since = int(request.GET.get("since", 0))
    except ValueError:
        since = 0

    logs_payload, groups_payload = [], []
    if run is not None and request.GET.get("logs", "1") != "0":
        qs = ScraperLog.objects.filter(run=run).select_related("group")
        if since:
            entries = list(qs.filter(id__gt=since).order_by("id")[:500])
        elif request.GET.get("full") == "1":
            entries = list(qs.order_by("id")[:5000])
        else:
            entries = list(qs.order_by("-id")[:300])[::-1]
        logs_payload = [
            {"id": e.pk, "time": _fmt_time(e.timestamp), "level": e.level, "phase": e.phase,
             "group": e.group.name if e.group_id else None, "message": e.message}
            for e in entries
        ]
        groups_payload = [
            {"id": rg.pk, "group_id": rg.group_id, "name": rg.group.name, "status": rg.status,
             "status_label": rg.get_status_display(), "posts": rg.posts_collected, "duplicates": rg.duplicates,
             "attempts": rg.attempts, "error": rg.error_message}
            for rg in run.run_groups.select_related("group")
        ]

    session = BrowserSession.get()
    return JsonResponse({
        "server_time": _fmt_time(timezone.now()),
        "phase": active.current_phase if active else Phase.IDLE,
        "active_run_id": active.pk if active else None,
        "run": _serialize_run(run),
        "groups": groups_payload,
        "logs": logs_payload,
        "browser": {
            "status": session.status,
            "status_label": session.get_status_display(),
            "message": session.message,
            "task": session.task,
            "busy": manager.browser_busy(),
            "checked_at": _fmt_datetime(session.checked_at),
        },
        "run_statuses": [s for s, _ in RunStatus.choices],
    })
