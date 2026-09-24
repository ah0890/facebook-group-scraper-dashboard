from datetime import timedelta

from django import template
from django.utils.html import format_html

register = template.Library()

BADGE_CLASSES = {
    # run / group statuses
    "NEVER": "badge-soft-secondary",
    "QUEUED": "badge-soft-info",
    "RUNNING": "badge-soft-primary",
    "COMPLETED": "badge-soft-success",
    "PARTIAL": "badge-soft-warning",
    "FAILED": "badge-soft-danger",
    "STOPPED": "badge-soft-warning",
    "SKIPPED": "badge-soft-secondary",
    # log levels
    "DEBUG": "badge-soft-secondary",
    "INFO": "badge-soft-info",
    "SUCCESS": "badge-soft-success",
    "WARNING": "badge-soft-warning",
    "ERROR": "badge-soft-danger",
    # url checks / session
    "OK": "badge-soft-success",
    "AUTHENTICATED": "badge-soft-success",
    "NOT_AUTH": "badge-soft-danger",
    "WAITING": "badge-soft-warning",
    "CHECKING": "badge-soft-info",
    "UNKNOWN": "badge-soft-secondary",
    "MOCK": "badge-soft-info",
}


@register.filter
def badge_class(status: str) -> str:
    return BADGE_CLASSES.get(str(status).upper(), "badge-soft-secondary")


@register.simple_tag
def status_badge(status, label=None):
    label = label or str(status).replace("_", " ").title()
    return format_html('<span class="badge {}">{}</span>', badge_class(status), label)


@register.filter
def duration(value) -> str:
    if not value:
        return "—"
    if not isinstance(value, timedelta):
        return str(value)
    seconds = int(value.total_seconds())
    hours, rest = divmod(seconds, 3600)
    minutes, secs = divmod(rest, 60)
    if hours:
        return f"{hours}h {minutes:02d}m"
    if minutes:
        return f"{minutes}m {secs:02d}s"
    return f"{secs}s"


@register.filter
def short_url(url: str) -> str:
    """``https://www.facebook.com/groups/abc/`` -> ``facebook.com/groups/abc``."""
    text = str(url or "")
    for prefix in ("https://", "http://", "www."):
        if text.startswith(prefix):
            text = text[len(prefix):]
    return text.rstrip("/")


@register.simple_tag(takes_context=True)
def query_transform(context, **kwargs) -> str:
    """Return the current querystring with some parameters replaced (for pagination)."""
    query = context["request"].GET.copy()
    for key, value in kwargs.items():
        if value in (None, ""):
            query.pop(key, None)
        else:
            query[key] = value
    return query.urlencode()
