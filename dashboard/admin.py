from django.contrib import admin

from .models import BrowserSession, Group, Post, RunGroup, ScraperLog, ScraperRun, ScraperSetting


@admin.register(Group)
class GroupAdmin(admin.ModelAdmin):
    list_display = ("name", "facebook_url", "enabled", "last_status", "total_posts", "completed_round", "last_run_at")
    list_filter = ("enabled", "last_status")
    search_fields = ("name", "facebook_url", "notes")
    list_editable = ("enabled",)
    readonly_fields = ("total_posts", "last_run_at", "created_at", "updated_at", "url_checked_at")


@admin.register(Post)
class PostAdmin(admin.ModelAdmin):
    list_display = ("short_text", "author_name", "group", "post_timestamp", "likes_count", "comments_count",
                    "shares_count", "collected_at")
    list_filter = ("group", "collected_at")
    search_fields = ("post_text", "author_name", "facebook_post_id")
    date_hierarchy = "collected_at"
    raw_id_fields = ("run",)
    readonly_fields = ("dedup_key", "created_at", "updated_at")
    list_select_related = ("group",)

    @admin.display(description="Text")
    def short_text(self, obj):
        return (obj.post_text or "")[:80]


class RunGroupInline(admin.TabularInline):
    model = RunGroup
    extra = 0
    fields = ("position", "group", "status", "posts_collected", "duplicates", "attempts", "started_at",
              "finished_at", "error_message")
    readonly_fields = fields
    can_delete = False


@admin.register(ScraperRun)
class ScraperRunAdmin(admin.ModelAdmin):
    list_display = ("id", "started_at", "finished_at", "status", "current_phase", "mode", "trigger",
                    "round_number", "total_groups", "completed_groups", "failed_groups", "total_posts")
    list_filter = ("status", "mode", "trigger")
    search_fields = ("error_message",)
    date_hierarchy = "started_at"
    inlines = [RunGroupInline]
    readonly_fields = ("heartbeat_at", "worker_pid")


@admin.register(ScraperLog)
class ScraperLogAdmin(admin.ModelAdmin):
    list_display = ("timestamp", "level", "phase", "run", "group", "message")
    list_filter = ("level", "phase")
    search_fields = ("message",)
    raw_id_fields = ("run", "group")
    date_hierarchy = "timestamp"
    list_select_related = ("group",)


@admin.register(ScraperSetting)
class ScraperSettingAdmin(admin.ModelAdmin):
    list_display = ("key", "scraper_mode", "posts_per_group", "groups_per_run", "scroll_limit", "scroll_delay",
                    "headless", "updated_at")


@admin.register(BrowserSession)
class BrowserSessionAdmin(admin.ModelAdmin):
    list_display = ("status", "message", "task", "checked_at")
