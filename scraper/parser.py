"""Turn raw feed data into clean ``ScrapedPost`` objects.

Everything here is pure Python (no browser, no database), so it is easy to test.
The raw dicts come from the in-page extraction script in ``facebook.py`` (or from
the mock client, which produces the same shape).
"""

from __future__ import annotations

import hashlib
import re
from datetime import datetime, timedelta, timezone as dt_timezone
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

from .models import ScrapedPost

# ---------------------------------------------------------------------------
# Post IDs & URLs
# ---------------------------------------------------------------------------

_POST_ID_PATTERNS = [
    re.compile(r"/groups/[^/]+/posts/(\d+|pfbid[A-Za-z0-9]+)"),
    re.compile(r"/groups/[^/]+/permalink/(\d+)"),
    re.compile(r"/posts/(\d+|pfbid[A-Za-z0-9]+)"),
    re.compile(r"/permalink/(\d+)"),
    re.compile(r"[?&]story_fbid=(\d+|pfbid[A-Za-z0-9]+)"),
    re.compile(r"[?&]multi_permalinks=(\d+)"),
    re.compile(r"[?&]fbid=(\d+)"),
]

_GROUP_IN_URL = re.compile(r"/groups/([^/?#]+)")


def extract_post_id(url: str) -> str:
    """Return the Facebook post ID contained in a post URL, or ``""``."""
    if not url:
        return ""
    for pattern in _POST_ID_PATTERNS:
        match = pattern.search(url)
        if match:
            return match.group(1)
    return ""


def canonical_post_url(url: str) -> str:
    """Strip tracking parameters (``__cft__``, ``__tn__`` ...) and build a stable URL."""
    if not url:
        return ""
    parsed = urlparse(url)
    post_id = extract_post_id(url)
    group_match = _GROUP_IN_URL.search(parsed.path)
    if post_id and group_match:
        return f"https://www.facebook.com/groups/{group_match.group(1)}/posts/{post_id}/"
    query = [(k, v) for k, v in parse_qsl(parsed.query) if not k.startswith("__")]
    return urlunparse(parsed._replace(query=urlencode(query), fragment=""))


def clean_url(url: str) -> str:
    """Remove tracking parameters from any Facebook URL (e.g. author profile links)."""
    if not url:
        return ""
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"}:
        return ""
    query = [(k, v) for k, v in parse_qsl(parsed.query) if not k.startswith("__")]
    return urlunparse(parsed._replace(query=urlencode(query), fragment=""))


# ---------------------------------------------------------------------------
# Counts
# ---------------------------------------------------------------------------

_NUMBER = r"(\d[\d.,]*\s*[KkMm]?)"
_COUNT_PATTERNS = {
    "likes": [
        re.compile(r"All reactions:?\s*" + _NUMBER),
        re.compile(_NUMBER + r"\s+(?:reactions?|likes?)\b", re.IGNORECASE),
    ],
    "comments": [re.compile(_NUMBER + r"\s+comments?\b", re.IGNORECASE)],
    "shares": [re.compile(_NUMBER + r"\s+shares?\b", re.IGNORECASE)],
}


def parse_count(value: Any) -> int:
    """Parse engagement counts such as ``"12"``, ``"1,234"``, ``"1.2K"`` or ``"3M"``."""
    if value is None:
        return 0
    if isinstance(value, (int, float)):
        return max(0, int(value))
    text = str(value).strip().replace(" ", "")
    match = re.match(r"^(\d[\d.,]*)([KkMm]?)$", text)
    if not match:
        return 0
    number, suffix = match.groups()
    multiplier = {"k": 1_000, "m": 1_000_000}.get(suffix.lower(), 1)
    if multiplier > 1:
        number = number.replace(",", ".")
        try:
            return int(round(float(number) * multiplier))
        except ValueError:
            return 0
    digits = re.sub(r"[.,]", "", number)
    return int(digits) if digits.isdigit() else 0


def extract_counts(texts: list[str]) -> dict[str, int]:
    """Find likes/comments/shares in aria-labels and short text snippets."""
    counts = {"likes": 0, "comments": 0, "shares": 0}
    for text in texts or []:
        if not text or len(text) > 200:
            continue
        for key, patterns in _COUNT_PATTERNS.items():
            for pattern in patterns:
                match = pattern.search(text)
                if match:
                    counts[key] = max(counts[key], parse_count(match.group(1)))
    return counts


# ---------------------------------------------------------------------------
# Timestamps
# ---------------------------------------------------------------------------

_RELATIVE_UNITS = {
    "s": "seconds", "sec": "seconds", "secs": "seconds", "second": "seconds", "seconds": "seconds",
    "m": "minutes", "min": "minutes", "mins": "minutes", "minute": "minutes", "minutes": "minutes",
    "h": "hours", "hr": "hours", "hrs": "hours", "hour": "hours", "hours": "hours",
    "d": "days", "day": "days", "days": "days",
    "w": "weeks", "wk": "weeks", "wks": "weeks", "week": "weeks", "weeks": "weeks",
    "y": "years", "yr": "years", "yrs": "years", "year": "years", "years": "years",
}
_RELATIVE_RE = re.compile(r"^(\d+)\s*([a-z]+)(?:\s+ago)?$")
_WEEKDAY_PREFIX = re.compile(r"^(monday|tuesday|wednesday|thursday|friday|saturday|sunday),?\s+", re.IGNORECASE)
_ABSOLUTE_FORMATS = [
    "%B %d, %Y at %I:%M %p", "%B %d, %Y at %H:%M", "%B %d, %Y",
    "%d %B %Y at %H:%M", "%d %B %Y at %I:%M %p", "%d %B %Y",
    "%b %d, %Y at %I:%M %p", "%b %d, %Y",
]
_NO_YEAR_FORMATS = ["%B %d at %I:%M %p", "%B %d at %H:%M", "%d %B at %H:%M", "%B %d", "%d %B", "%b %d"]
_TIME_FORMATS = ["%I:%M %p", "%H:%M"]


def _parse_time_of_day(text: str) -> tuple[int, int] | None:
    for fmt in _TIME_FORMATS:
        try:
            parsed = datetime.strptime(text.strip().upper(), fmt)
            return parsed.hour, parsed.minute
        except ValueError:
            continue
    return None


def parse_timestamp(text: str, now: datetime | None = None) -> datetime | None:
    """Best-effort parse of Facebook's timestamp strings.

    Handles relative forms ("5m", "3 hrs", "2d", "Just now"), "Yesterday at 3:15 PM",
    absolute dates with or without a year, and ISO-8601. Returns an aware datetime
    in ``now``'s timezone, or ``None`` if the text is not recognised.
    """
    if not text:
        return None
    now = now or datetime.now(dt_timezone.utc)
    tz = now.tzinfo or dt_timezone.utc
    value = " ".join(str(text).split()).strip()
    lowered = value.lower().replace("·", "").strip()

    if lowered in {"just now", "now", "a few seconds ago"}:
        return now

    match = _RELATIVE_RE.match(lowered)
    if match:
        amount, unit = int(match.group(1)), match.group(2)
        unit_name = _RELATIVE_UNITS.get(unit)
        if unit_name == "years":
            return now - timedelta(days=365 * amount)
        if unit_name:
            return now - timedelta(**{unit_name: amount})

    if lowered.startswith("yesterday"):
        base = now - timedelta(days=1)
        time_part = lowered.split(" at ", 1)[1] if " at " in lowered else ""
        hm = _parse_time_of_day(time_part) if time_part else None
        if hm:
            return base.replace(hour=hm[0], minute=hm[1], second=0, microsecond=0)
        return base.replace(hour=12, minute=0, second=0, microsecond=0)

    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=tz)
    except ValueError:
        pass

    value = _WEEKDAY_PREFIX.sub("", value)
    for fmt in _ABSOLUTE_FORMATS:
        try:
            return datetime.strptime(value, fmt).replace(tzinfo=tz)
        except ValueError:
            continue
    for fmt in _NO_YEAR_FORMATS:
        try:
            parsed = datetime.strptime(f"{value} {now.year}", f"{fmt} %Y").replace(tzinfo=tz)
        except ValueError:
            continue
        # "December 30" seen in January refers to last year.
        return parsed.replace(year=now.year - 1) if parsed > now + timedelta(days=1) else parsed
    return None


# ---------------------------------------------------------------------------
# Text & posts
# ---------------------------------------------------------------------------

_SEE_MORE_RE = re.compile(r"(…|\.\.\.)?\s*See (more|less)\s*$", re.IGNORECASE)


def clean_text(text: str) -> str:
    if not text:
        return ""
    lines = [" ".join(line.split()) for line in str(text).replace("\r", "").split("\n")]
    cleaned = "\n".join(line for line in lines if line).strip()
    return _SEE_MORE_RE.sub("", cleaned).strip()


def content_hash(author: str, text: str) -> str:
    normalised = f"{(author or '').strip().lower()}|{' '.join((text or '').lower().split())[:2000]}"
    return hashlib.sha1(normalised.encode("utf-8")).hexdigest()


def build_dedup_key(group_id: int, post: ScrapedPost) -> str:
    """Stable per-group key: the Facebook post ID when known, else a content hash."""
    if post.facebook_post_id:
        return f"{group_id}:id:{post.facebook_post_id}"[:120]
    return f"{group_id}:h:{content_hash(post.author_name, post.post_text)}"


def parse_raw_post(raw: dict[str, Any], now: datetime | None = None) -> ScrapedPost | None:
    """Convert one raw extraction dict into a ``ScrapedPost`` (or ``None`` if empty)."""
    if not isinstance(raw, dict):
        return None

    author = " ".join(str(raw.get("author") or "").split())[:255]
    text = clean_text(raw.get("text") or "")
    post_url = canonical_post_url(str(raw.get("post_url") or ""))
    post_id = str(raw.get("post_id") or "") or extract_post_id(str(raw.get("post_url") or ""))

    if not (text or post_id or raw.get("media_url")):
        return None  # nothing identifying – e.g. a "suggested groups" unit or skeleton loader

    timestamp_text = " ".join(str(raw.get("timestamp_text") or "").split())[:100]
    counts = extract_counts(list(raw.get("labels") or []) + list(raw.get("snippets") or []))
    media_url = str(raw.get("media_url") or "")
    if not media_url.startswith(("http://", "https://")):
        media_url = ""

    return ScrapedPost(
        facebook_post_id=post_id[:64],
        author_name=author,
        author_url=clean_url(str(raw.get("author_url") or ""))[:1000],
        post_text=text,
        post_url=post_url[:1000],
        post_timestamp=parse_timestamp(timestamp_text, now),
        timestamp_text=timestamp_text,
        media_url=media_url[:2000],
        likes_count=counts["likes"],
        comments_count=counts["comments"],
        shares_count=counts["shares"],
        raw_data={
            "timestamp_text": timestamp_text,
            "labels": list(raw.get("labels") or [])[:20],
            "source": raw.get("source", "feed"),
        },
    )
