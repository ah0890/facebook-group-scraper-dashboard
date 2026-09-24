"""Persistence for scraped posts.

Posts are buffered per group and flushed in small transactions (every
``save_frequency`` new posts and always at the end of a group), so a crash or a
stop request loses at most one small batch and never leaves half-written rows.
"""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path

from django.db import DatabaseError, IntegrityError, transaction
from django.db.models import F
from django.utils import timezone

from .exceptions import StorageError
from .models import ScrapedPost
from .parser import build_dedup_key


class GroupPostStore:
    """Deduplicating write buffer for one group within one run."""

    def __init__(self, run, group, save_frequency: int = 5):
        from dashboard.models import Post

        self.run = run
        self.group = group
        self.save_frequency = max(1, int(save_frequency))
        self._known = set(Post.objects.filter(group=group).values_list("dedup_key", flat=True))
        self._pending_new: list[tuple[str, ScrapedPost]] = []
        self._pending_updates: dict[str, ScrapedPost] = {}
        self.new_count = 0        # unique new posts accepted (saved + pending)
        self.saved_count = 0      # new posts actually written
        self.duplicate_count = 0  # posts that were already in the database

    @property
    def is_first_fetch(self) -> bool:
        return not self._known

    @property
    def pending(self) -> int:
        return len(self._pending_new)

    def add(self, post: ScrapedPost) -> bool:
        """Queue a post. Returns ``True`` if it is new, ``False`` if already saved."""
        key = build_dedup_key(self.group.pk, post)
        if key in self._known:
            self._pending_updates[key] = post
            self.duplicate_count += 1
            return False
        self._known.add(key)
        self._pending_new.append((key, post))
        self.new_count += 1
        return True

    def should_flush(self) -> bool:
        return len(self._pending_new) >= self.save_frequency

    def flush(self) -> int:
        """Write pending posts. Returns the number of new rows written.

        On a database error the buffer is kept so a later flush can retry, and a
        ``StorageError`` is raised.
        """
        from dashboard.models import Post, ScraperRun

        if not self._pending_new and not self._pending_updates:
            return 0

        saved = 0
        now = timezone.now()
        try:
            with transaction.atomic():
                for key, post in self._pending_new:
                    try:
                        with transaction.atomic():  # savepoint: one bad row must not abort the batch
                            Post.objects.create(
                                group=self.group,
                                run=self.run,
                                dedup_key=key,
                                facebook_post_id=post.facebook_post_id,
                                author_name=post.author_name,
                                author_url=post.author_url,
                                post_text=post.post_text,
                                post_url=post.post_url,
                                post_timestamp=post.post_timestamp,
                                timestamp_text=post.timestamp_text,
                                media_url=post.media_url,
                                likes_count=post.likes_count,
                                comments_count=post.comments_count,
                                shares_count=post.shares_count,
                                raw_data=post.raw_data,
                                collected_at=now,
                            )
                        saved += 1
                    except IntegrityError:
                        # Saved concurrently (e.g. by a CLI run) – treat as a duplicate.
                        self.new_count -= 1
                        self.duplicate_count += 1

                for key, post in self._pending_updates.items():
                    # Engagement changes over time; refresh counts we could read.
                    changes = {
                        name: getattr(post, name)
                        for name in ("likes_count", "comments_count", "shares_count")
                        if getattr(post, name)
                    }
                    if changes:
                        Post.objects.filter(dedup_key=key).update(updated_at=now, **changes)

                if self.run is not None:
                    ScraperRun.objects.filter(pk=self.run.pk).update(
                        total_posts=F("total_posts") + saved,
                        duplicate_posts=F("duplicate_posts") + len(self._pending_updates),
                        heartbeat_at=now,
                    )
        except DatabaseError as exc:
            raise StorageError(f"Database error while saving posts: {exc}") from exc

        self._pending_new.clear()
        self._pending_updates.clear()
        self.saved_count += saved
        return saved


def save_posts(run, group, posts: list[ScrapedPost]) -> tuple[int, int]:
    """Convenience helper: store posts immediately. Returns ``(saved, duplicates)``."""
    store = GroupPostStore(run, group, save_frequency=max(1, len(posts)))
    for post in posts:
        store.add(post)
    store.flush()
    refresh_group_totals(group)
    return store.saved_count, store.duplicate_count


def refresh_group_totals(group) -> int:
    from dashboard.models import Group, Post

    total = Post.objects.filter(group=group).count()
    Group.objects.filter(pk=group.pk).update(total_posts=total)
    group.total_posts = total
    return total


def write_snapshot(run, export_dir: Path, filename: str = "fetched_posts.json") -> Path:
    """Atomically write all posts collected by ``run`` to a JSON file.

    Written to a temporary file first and then renamed, so readers never see a
    half-written file even if the process dies mid-write.
    """
    from dashboard.exports import serialize_post
    from dashboard.models import Post

    export_dir = Path(export_dir)
    export_dir.mkdir(parents=True, exist_ok=True)
    target = export_dir / filename

    posts = Post.objects.filter(run=run).select_related("group").order_by("group__name", "-post_timestamp")
    payload = {
        "run_id": run.pk,
        "generated_at": timezone.now().isoformat(),
        "count": posts.count(),
        "posts": [serialize_post(p) for p in posts],
    }
    fd, tmp_path = tempfile.mkstemp(dir=export_dir, prefix=".fetched_", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, ensure_ascii=False, indent=2)
        os.replace(tmp_path, target)
    except OSError:
        if os.path.exists(tmp_path):
            os.unlink(tmp_path)
        raise
    return target
