"""CSV / JSON export of posts."""

from __future__ import annotations

import csv
import json
from typing import IO, Iterable

from django.core.serializers.json import DjangoJSONEncoder

from .models import Post

EXPORT_FIELDS = [
    "id", "group", "group_url", "facebook_post_id", "author_name", "author_url", "post_text", "post_url",
    "post_timestamp", "timestamp_text", "collected_at", "media_url", "likes_count", "comments_count",
    "shares_count",
]

# Cells starting with these characters can be interpreted as formulas by spreadsheet apps.
_FORMULA_PREFIXES = ("=", "+", "-", "@", "\t", "\r")


def serialize_post(post: Post) -> dict:
    return {
        "id": post.pk,
        "group": post.group.name,
        "group_url": post.group.facebook_url,
        "facebook_post_id": post.facebook_post_id,
        "author_name": post.author_name,
        "author_url": post.author_url,
        "post_text": post.post_text,
        "post_url": post.post_url,
        "post_timestamp": post.post_timestamp.isoformat() if post.post_timestamp else None,
        "timestamp_text": post.timestamp_text,
        "collected_at": post.collected_at.isoformat() if post.collected_at else None,
        "media_url": post.media_url,
        "likes_count": post.likes_count,
        "comments_count": post.comments_count,
        "shares_count": post.shares_count,
    }


def _csv_safe(value) -> str:
    if value is None:
        return ""
    text = str(value)
    return "'" + text if text.startswith(_FORMULA_PREFIXES) else text


def write_csv(posts: Iterable[Post], fh: IO[str]) -> int:
    writer = csv.writer(fh)
    writer.writerow(EXPORT_FIELDS)
    count = 0
    for post in posts:
        row = serialize_post(post)
        writer.writerow([_csv_safe(row[name]) for name in EXPORT_FIELDS])
        count += 1
    return count


def posts_to_json(posts: Iterable[Post]) -> str:
    items = [serialize_post(p) for p in posts]
    return json.dumps({"count": len(items), "posts": items}, cls=DjangoJSONEncoder, ensure_ascii=False, indent=2)
