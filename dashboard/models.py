from __future__ import annotations

from datetime import timedelta
from pathlib import Path

from django.conf import settings
from django.core.validators import MaxValueValidator, MinValueValidator
from django.db import models
from django.db.models import Q
from django.utils import timezone

from scraper.models import MODE_MOCK, MODE_PLAYWRIGHT, ScrapeConfig

from .validators import normalize_group_url, validate_facebook_group_url


# ---------------------------------------------------------------------------
# Choices
# ---------------------------------------------------------------------------


class GroupStatus(models.TextChoices):
    NEVER = "NEVER", "Never run"
    QUEUED = "QUEUED", "Queued"
    RUNNING = "RUNNING", "Running"
    COMPLETED = "COMPLETED", "Completed"
    FAILED = "FAILED", "Failed"
    SKIPPED = "SKIPPED", "Skipped"


class RunStatus(models.TextChoices):
    QUEUED = "QUEUED", "Queued"
    RUNNING = "RUNNING", "Running"
    COMPLETED = "COMPLETED", "Completed"
    PARTIAL = "PARTIAL", "Partial"
    FAILED = "FAILED", "Failed"
    STOPPED = "STOPPED", "Stopped"


ACTIVE_RUN_STATUSES = (RunStatus.QUEUED, RunStatus.RUNNING)


class Phase(models.TextChoices):
    IDLE = "IDLE", "Idle"
    SEEDING = "SEEDING", "Seeding"
    INITIALIZING = "INITIALIZING", "Initializing"
    AUTHENTICATING = "AUTHENTICATING", "Authenticating"
    FETCHING = "FETCHING", "Fetching"
    SCROLLING = "SCROLLING", "Scrolling"
    PARSING = "PARSING", "Parsing"
    SAVING = "SAVING", "Saving"
    COMPLETED = "COMPLETED", "Completed"
    FAILED = "FAILED", "Failed"
    STOPPED = "STOPPED", "Stopped"


class LogLevel(models.TextChoices):
    DEBUG = "DEBUG", "Debug"
    INFO = "INFO", "Info"
    SUCCESS = "SUCCESS", "Success"
    WARNING = "WARNING", "Warning"
    ERROR = "ERROR", "Error"


class ScraperMode(models.TextChoices):
    MOCK = MODE_MOCK, "Mock (simulated data)"
    PLAYWRIGHT = MODE_PLAYWRIGHT, "Playwright (real browser)"


# ---------------------------------------------------------------------------
# Groups & posts
# ---------------------------------------------------------------------------


class Group(models.Model):
    name = models.CharField(max_length=200)
    facebook_url = models.URLField(
        "Facebook URL", max_length=500, unique=True, validators=[validate_facebook_group_url]
    )
    enabled = models.BooleanField(default=True, db_index=True)
    notes = models.TextField(blank=True)

    last_run_at = models.DateTimeField(null=True, blank=True)
    last_status = models.CharField(max_length=12, choices=GroupStatus.choices, default=GroupStatus.NEVER)
    last_error = models.TextField(blank=True)
    total_posts = models.PositiveIntegerField(default=0)
    # Number of the last scraping round this group completed. Used to work out
    # which groups still need scraping, so interrupted rounds resume cleanly.
    completed_round = models.PositiveIntegerField(default=0)

    url_check_status = models.CharField(max_length=12, blank=True)
    url_check_message = models.CharField(max_length=300, blank=True)
    url_checked_at = models.DateTimeField(null=True, blank=True)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["name"]

    def __str__(self) -> str:
        return self.name

    def save(self, *args, **kwargs):
        try:
            self.facebook_url = normalize_group_url(self.facebook_url)
        except Exception:
            pass  # full_clean()/forms report invalid URLs; don't mask them here
        super().save(*args, **kwargs)


class Post(models.Model):
    group = models.ForeignKey(Group, on_delete=models.CASCADE, related_name="posts")
    run = models.ForeignKey(
        "ScraperRun", on_delete=models.SET_NULL, null=True, blank=True, related_name="posts",
        help_text="The run that first collected this post.",
    )
    facebook_post_id = models.CharField(max_length=64, blank=True, db_index=True)
    # "<group_id>:id:<facebook id>" when the post ID is known, otherwise a content hash.
    dedup_key = models.CharField(max_length=120, unique=True)

    author_name = models.CharField(max_length=255, blank=True, db_index=True)
    author_url = models.URLField(max_length=1000, blank=True)
    post_text = models.TextField(blank=True)
    post_url = models.URLField(max_length=1000, blank=True)
    post_timestamp = models.DateTimeField(null=True, blank=True, db_index=True)
    timestamp_text = models.CharField(max_length=100, blank=True)
    collected_at = models.DateTimeField(default=timezone.now, db_index=True)
    media_url = models.URLField(max_length=2000, blank=True)
    likes_count = models.PositiveIntegerField(default=0)
    comments_count = models.PositiveIntegerField(default=0)
    shares_count = models.PositiveIntegerField(default=0)
    raw_data = models.JSONField(default=dict, blank=True)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-collected_at", "-id"]
        indexes = [
            models.Index(fields=["group", "-collected_at"], name="post_group_collected_idx"),
        ]
        constraints = [
            models.UniqueConstraint(
                fields=["group", "facebook_post_id"],
                condition=~Q(facebook_post_id=""),
                name="unique_facebook_post_per_group",
            )
        ]

    def __str__(self) -> str:
        preview = (self.post_text or "").strip().replace("\n", " ")[:60]
        return f"{self.author_name or 'Unknown'}: {preview}"


# ---------------------------------------------------------------------------
# Runs & logs
# ---------------------------------------------------------------------------


class ScraperRun(models.Model):
    started_at = models.DateTimeField(default=timezone.now, db_index=True)
    finished_at = models.DateTimeField(null=True, blank=True)
    status = models.CharField(max_length=10, choices=RunStatus.choices, default=RunStatus.QUEUED, db_index=True)
    current_phase = models.CharField(max_length=16, choices=Phase.choices, default=Phase.IDLE)
    mode = models.CharField(max_length=12, choices=ScraperMode.choices, default=MODE_MOCK)
    trigger = models.CharField(max_length=12, default="web", help_text="web, cli or group")
    round_number = models.PositiveIntegerField(default=1)

    total_groups = models.PositiveIntegerField(default=0)
    completed_groups = models.PositiveIntegerField(default=0)
    failed_groups = models.PositiveIntegerField(default=0)
    skipped_groups = models.PositiveIntegerField(default=0)
    total_posts = models.PositiveIntegerField(default=0, help_text="New posts saved by this run.")
    duplicate_posts = models.PositiveIntegerField(default=0)

    current_group = models.ForeignKey(Group, on_delete=models.SET_NULL, null=True, blank=True, related_name="+")
    error_message = models.TextField(blank=True)

    stop_requested = models.BooleanField(default=False)
    heartbeat_at = models.DateTimeField(null=True, blank=True)
    worker_pid = models.PositiveIntegerField(null=True, blank=True)

    class Meta:
        ordering = ["-started_at", "-id"]

    def __str__(self) -> str:
        return f"Run #{self.pk} ({self.status})"

    @property
    def is_active(self) -> bool:
        return self.status in ACTIVE_RUN_STATUSES

    @property
    def duration(self) -> timedelta | None:
        if not self.started_at:
            return None
        end = self.finished_at or (timezone.now() if self.is_active else None)
        return (end - self.started_at) if end else None

    @property
    def progress_percent(self) -> int:
        if not self.total_groups:
            return 0
        done = self.completed_groups + self.failed_groups + self.skipped_groups
        return min(100, round(done * 100 / self.total_groups))


class RunGroup(models.Model):
    """Per-run progress for one group (the run's queue)."""

    run = models.ForeignKey(ScraperRun, on_delete=models.CASCADE, related_name="run_groups")
    group = models.ForeignKey(Group, on_delete=models.CASCADE, related_name="run_entries")
    position = models.PositiveIntegerField(default=0)
    status = models.CharField(max_length=12, choices=GroupStatus.choices, default=GroupStatus.QUEUED)
    posts_collected = models.PositiveIntegerField(default=0)
    duplicates = models.PositiveIntegerField(default=0)
    attempts = models.PositiveIntegerField(default=0)
    started_at = models.DateTimeField(null=True, blank=True)
    finished_at = models.DateTimeField(null=True, blank=True)
    error_message = models.TextField(blank=True)

    class Meta:
        ordering = ["run", "position"]
        constraints = [models.UniqueConstraint(fields=["run", "group"], name="unique_group_per_run")]

    def __str__(self) -> str:
        return f"{self.group} in run #{self.run_id}"


class ScraperLog(models.Model):
    run = models.ForeignKey(ScraperRun, on_delete=models.CASCADE, null=True, blank=True, related_name="logs")
    timestamp = models.DateTimeField(default=timezone.now, db_index=True)
    level = models.CharField(max_length=8, choices=LogLevel.choices, default=LogLevel.INFO, db_index=True)
    phase = models.CharField(max_length=16, choices=Phase.choices, blank=True)
    group = models.ForeignKey(Group, on_delete=models.SET_NULL, null=True, blank=True, related_name="logs")
    message = models.TextField()

    class Meta:
        ordering = ["id"]
        indexes = [
            models.Index(fields=["run", "id"], name="log_run_id_idx"),
        ]

    def __str__(self) -> str:
        return f"[{self.level}] {self.message[:80]}"


# ---------------------------------------------------------------------------
# Settings
# ---------------------------------------------------------------------------


def default_scraper_mode() -> str:
    return settings.SCRAPER_MODE


def default_profile_dir() -> str:
    return settings.BROWSER_PROFILE_DIR


def default_export_dir() -> str:
    return settings.EXPORT_DIR


class ScraperSetting(models.Model):
    """Scraper configuration.

    Two rows exist: ``active`` (used by runs, edited on the Settings page) and
    ``defaults`` (edited on the Defaults page, applied with "Reset to Defaults").
    The model field defaults are the safe *factory* defaults.
    """

    ACTIVE = "active"
    DEFAULTS = "defaults"
    KEY_CHOICES = [(ACTIVE, "Active settings"), (DEFAULTS, "Saved defaults")]

    CONFIG_FIELDS = [
        "scraper_mode", "posts_per_group", "groups_per_run", "scroll_limit", "scroll_delay",
        "page_timeout", "retry_count", "save_frequency", "save_after_every_group", "headless",
        "browser_type", "profile_dir", "export_dir", "log_level", "log_retention_days",
        "auth_wait_seconds",
    ]

    key = models.CharField(max_length=10, choices=KEY_CHOICES, unique=True)

    # Scraper
    scraper_mode = models.CharField(max_length=12, choices=ScraperMode.choices, default=default_scraper_mode)
    posts_per_group = models.PositiveIntegerField(default=20, validators=[MinValueValidator(1), MaxValueValidator(500)])
    groups_per_run = models.PositiveIntegerField(default=10, validators=[MinValueValidator(1), MaxValueValidator(200)])
    scroll_limit = models.PositiveIntegerField(default=20, validators=[MinValueValidator(1), MaxValueValidator(200)])
    scroll_delay = models.FloatField(
        "Scroll delay (seconds)", default=2.0, validators=[MinValueValidator(1.0), MaxValueValidator(60)],
        help_text="Pause between scrolls. Keep this humane – at least 1 second.",
    )
    page_timeout = models.PositiveIntegerField(
        "Page timeout (seconds)", default=45, validators=[MinValueValidator(10), MaxValueValidator(180)]
    )
    retry_count = models.PositiveIntegerField(default=2, validators=[MaxValueValidator(5)])
    save_frequency = models.PositiveIntegerField(
        "Save every N posts", default=5, validators=[MinValueValidator(1), MaxValueValidator(100)],
        help_text="Posts are written to the database in batches of this size (and always at the end of a group).",
    )
    save_after_every_group = models.BooleanField(
        "Export JSON snapshot after every group", default=True,
        help_text="Writes fetched_posts.json to the export directory after each group.",
    )

    # Browser
    headless = models.BooleanField(
        "Headless browser", default=False,
        help_text="Visible mode lets you log in manually if the session expires.",
    )
    browser_type = models.CharField(max_length=20, choices=[("chromium", "Chromium")], default="chromium")
    profile_dir = models.CharField("Persistent profile directory", max_length=500, default=default_profile_dir)
    auth_wait_seconds = models.PositiveIntegerField(
        "Wait for manual login (seconds)", default=300, validators=[MinValueValidator(30), MaxValueValidator(1800)]
    )

    # Storage
    export_dir = models.CharField("Export directory", max_length=500, default=default_export_dir)

    # Logging
    log_level = models.CharField(
        max_length=8, choices=[(c, c) for c in ("DEBUG", "INFO", "WARNING", "ERROR")], default="INFO"
    )
    log_retention_days = models.PositiveIntegerField(
        "Keep logs for (days)", default=30, validators=[MinValueValidator(1), MaxValueValidator(3650)]
    )

    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "scraper setting"

    def __str__(self) -> str:
        return self.get_key_display()

    # -- access helpers -----------------------------------------------------

    @classmethod
    def get_active(cls) -> "ScraperSetting":
        obj, _ = cls.objects.get_or_create(key=cls.ACTIVE)
        return obj

    @classmethod
    def get_defaults(cls) -> "ScraperSetting":
        obj, _ = cls.objects.get_or_create(key=cls.DEFAULTS)
        return obj

    def copy_from(self, other: "ScraperSetting") -> None:
        for name in self.CONFIG_FIELDS:
            setattr(self, name, getattr(other, name))

    def reset_to_factory(self) -> None:
        for name in self.CONFIG_FIELDS:
            setattr(self, name, self._meta.get_field(name).get_default())

    def to_config(self, mode: str | None = None) -> ScrapeConfig:
        return ScrapeConfig(
            mode=mode or self.scraper_mode,
            posts_per_group=self.posts_per_group,
            groups_per_run=self.groups_per_run,
            scroll_limit=self.scroll_limit,
            scroll_delay=self.scroll_delay,
            page_timeout=self.page_timeout,
            retry_count=self.retry_count,
            save_frequency=self.save_frequency,
            save_after_every_group=self.save_after_every_group,
            headless=self.headless,
            browser_type=self.browser_type,
            profile_dir=resolve_dir(self.profile_dir),
            export_dir=resolve_dir(self.export_dir),
            log_level=self.log_level,
            auth_wait_seconds=self.auth_wait_seconds,
        )


def resolve_dir(value: str) -> Path:
    path = Path(value)
    return path if path.is_absolute() else Path(settings.BASE_DIR) / path


class BrowserSession(models.Model):
    """Singleton describing the persistent browser profile's login state.

    No credentials or cookies are stored here – only a status for the UI.
    """

    class Status(models.TextChoices):
        UNKNOWN = "UNKNOWN", "Not checked"
        CHECKING = "CHECKING", "Checking…"
        WAITING = "WAITING", "Waiting for login"
        AUTHENTICATED = "AUTHENTICATED", "Authenticated"
        NOT_AUTHENTICATED = "NOT_AUTH", "Not logged in"
        ERROR = "ERROR", "Error"
        MOCK = "MOCK", "Mock mode"

    status = models.CharField(max_length=16, choices=Status.choices, default=Status.UNKNOWN)
    task = models.CharField(max_length=20, blank=True, help_text="Browser task currently running, if any.")
    message = models.CharField(max_length=500, blank=True)
    checked_at = models.DateTimeField(null=True, blank=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "browser session"

    def __str__(self) -> str:
        return self.get_status_display()

    @classmethod
    def get(cls) -> "BrowserSession":
        obj, _ = cls.objects.get_or_create(pk=1)
        return obj

    @classmethod
    def record(cls, status: str, message: str = "", task: str = "") -> None:
        cls.get()
        cls.objects.filter(pk=1).update(
            status=status, message=message[:500], task=task, checked_at=timezone.now(), updated_at=timezone.now()
        )
