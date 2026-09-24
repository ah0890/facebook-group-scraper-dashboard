"""Validation and normalisation of Facebook group URLs."""

import re
from urllib.parse import urlparse

from django.core.exceptions import ValidationError

ALLOWED_HOSTS = {"facebook.com", "www.facebook.com", "m.facebook.com", "web.facebook.com", "mbasic.facebook.com"}

# Group identifiers are either numeric IDs or vanity slugs (letters, digits, dots, dashes, underscores).
GROUP_PATH_RE = re.compile(r"^/groups/([A-Za-z0-9._-]{2,100})/?", re.IGNORECASE)


def normalize_group_url(value: str) -> str:
    """Return the canonical ``https://www.facebook.com/groups/<id>/`` form of a group URL.

    Raises ``ValidationError`` if the URL is not a Facebook group URL.
    """
    value = (value or "").strip()
    if not value:
        raise ValidationError("Enter a Facebook group URL.")
    if "://" not in value:
        value = "https://" + value

    parsed = urlparse(value)
    if parsed.scheme not in {"http", "https"}:
        raise ValidationError("The URL must start with http:// or https://.")

    host = (parsed.hostname or "").lower()
    if host not in ALLOWED_HOSTS:
        raise ValidationError("Only facebook.com group URLs are allowed.")

    match = GROUP_PATH_RE.match(parsed.path or "")
    if not match:
        raise ValidationError(
            "This does not look like a Facebook group URL. Expected https://www.facebook.com/groups/<group-id>/"
        )

    group_id = match.group(1)
    if group_id.lower() in {"feed", "discover", "joins", "create", "search"}:
        raise ValidationError("Link to a specific group, not a Facebook groups overview page.")

    return f"https://www.facebook.com/groups/{group_id}/"


def validate_facebook_group_url(value: str) -> None:
    normalize_group_url(value)


def group_slug(url: str) -> str:
    """Extract the group identifier from a (normalised) group URL."""
    match = GROUP_PATH_RE.match(urlparse(url).path or "")
    return match.group(1) if match else ""
