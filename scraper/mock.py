"""Mock scraper client – exercises the whole pipeline without touching Facebook.

The fake feed is deterministic per group and moves with the clock: every group
"receives" a new post every few minutes. Re-running soon after a run therefore
shows deduplication ("already saved"), and later runs pick up new posts – just
like the real thing.

Special group URLs for demos/tests:

* URLs containing ``broken``  -> every attempt fails with a navigation error (retries, then FAILED)
* URLs containing ``flaky``   -> the first attempt times out, the retry succeeds
* URLs containing ``private`` -> "not a member" (FAILED without retries)
"""

from __future__ import annotations

import hashlib
import random
import time
from datetime import datetime, timedelta, timezone as dt_timezone
from typing import Callable

from .exceptions import GroupUnavailableError, NavigationError, PageTimeoutError
from .models import ExtractionResult, ScrapeConfig

POST_INTERVAL_SECONDS = 300  # a new post appears in every mock group every 5 minutes
FEED_DEPTH = 80

AUTHORS = [
    "Ayesha Khan", "Bilal Ahmed", "Sara Malik", "Usman Tariq", "Hina Raza", "Omar Farooq",
    "Fatima Noor", "Hamza Sheikh", "Zainab Ali", "Ali Hassan", "Maryam Iqbal", "Daniyal Qureshi",
    "Mehwish Anwar", "Kashif Butt", "Nida Aslam", "Imran Javed", "Sana Mirza", "Tariq Mehmood",
    "Rabia Saleem", "Faisal Chaudhry", "Emma Wilson", "James Carter", "Priya Sharma", "Lucas Martin",
]

OPENERS = [
    "Can anyone recommend", "Looking for", "Quick question about", "Sharing an update on",
    "Heads up everyone:", "Does anyone know", "Just wanted to say thanks for", "Selling:",
    "Urgent help needed with", "Has anyone tried",
]
TOPICS = [
    "a reliable plumber in Phase 5", "good schools near the main boulevard", "the new park timings",
    "a 2-bed apartment for rent", "weekend farmers market vendors", "the water supply schedule",
    "a trustworthy car mechanic", "home tuition for O-levels", "solar panel installers",
    "the community clean-up drive on Sunday", "a lost cat (grey, white paws)", "gym memberships",
    "fresh homemade biryani orders", "power outage updates", "a used laptop in good condition",
    "freelance web developers", "Python meetups this month", "the traffic diversion near the flyover",
]
CLOSERS = [
    "Please DM me.", "Thanks in advance!", "Any suggestions appreciated 🙏", "Comment below.",
    "Prices are negotiable.", "Serious buyers only.", "Will share more details soon.", "",
]


def _seed_for(url: str) -> int:
    return int(hashlib.md5(url.encode("utf-8")).hexdigest()[:8], 16)


def _relative_label(delta: timedelta) -> str:
    seconds = max(0, int(delta.total_seconds()))
    if seconds < 60:
        return "Just now"
    if seconds < 3600:
        return f"{seconds // 60}m"
    if seconds < 86400:
        return f"{seconds // 3600}h"
    if seconds < 7 * 86400:
        return f"{seconds // 86400}d"
    return f"{seconds // (7 * 86400)}w"


def generate_feed(group_url: str, now: datetime | None = None, depth: int = FEED_DEPTH) -> list[dict]:
    """Return the mock feed for a group at time ``now`` (newest first) as raw dicts."""
    now = now or datetime.now(dt_timezone.utc)
    seed = _seed_for(group_url)
    slug = group_url.rstrip("/").rsplit("/", 1)[-1] or "group"
    newest_slot = int(now.timestamp()) // POST_INTERVAL_SECONDS
    posts = []
    for offset in range(depth):
        slot = newest_slot - offset
        rng = random.Random(seed * 1_000_003 + slot)
        posted_at = datetime.fromtimestamp(slot * POST_INTERVAL_SECONDS, dt_timezone.utc)
        author = rng.choice(AUTHORS)
        text = f"{rng.choice(OPENERS)} {rng.choice(TOPICS)}. {rng.choice(CLOSERS)}".strip()
        if rng.random() < 0.3:
            text += "\n\n" + f"{rng.choice(OPENERS)} {rng.choice(TOPICS)}."
        post_id = str(10**15 + (seed % 10**6) * 10**8 + slot % 10**8)
        likes, comments, shares = rng.randint(0, 250), rng.randint(0, 60), rng.randint(0, 15)
        posts.append({
            "author": author,
            "author_url": f"https://www.facebook.com/profile.php?id={rng.randint(10**9, 10**10)}",
            "text": text,
            "post_url": f"https://www.facebook.com/groups/{slug}/posts/{post_id}/?__cft__[0]=mock",
            "timestamp_text": _relative_label(now - posted_at),
            "media_url": f"https://picsum.photos/seed/{post_id}/600/400" if rng.random() < 0.35 else "",
            "labels": [f"All reactions: {likes}", f"{comments} comments", f"{shares} shares"],
            "snippets": [],
            "source": "mock",
        })
    return posts


class MockFacebookClient:
    """Drop-in replacement for ``FacebookClient`` that simulates a browser."""

    def __init__(self, cfg: ScrapeConfig, *, log: Callable[[str, str], None] | None = None,
                 wait: Callable[[float], None] = time.sleep, speed: float = 1.0):
        self.cfg = cfg
        self.log = log or (lambda level, message: None)
        self.wait = wait
        self.speed = speed
        self._attempts: dict[str, int] = {}
        self._feed: list[dict] = []
        self._visible = 0
        self._rng = random.Random()

    def _pause(self, seconds: float) -> None:
        if self.speed > 0:
            self.wait(seconds * self.speed)

    def start(self) -> None:
        self.log("INFO", "Launching simulated Chromium (mock mode – Facebook is not contacted)")
        self._pause(0.6)
        self.log("SUCCESS", "Mock browser ready")

    def ensure_authenticated(self) -> None:
        self._pause(0.4)
        self.log("SUCCESS", "Mock session authenticated")

    def open_group(self, url: str) -> None:
        attempt = self._attempts.get(url, 0) + 1
        self._attempts[url] = attempt
        self._pause(1.0)
        if "broken" in url:
            raise NavigationError("Simulated navigation failure (mock URL contains 'broken').")
        if "private" in url:
            raise GroupUnavailableError("This account is not a member of the group (simulated).")
        if "flaky" in url and attempt == 1:
            raise PageTimeoutError("Timed out while loading the group page (simulated, will succeed on retry).")
        self._rng = random.Random(_seed_for(url) + attempt)
        self._feed = generate_feed(url)
        self._visible = self._rng.randint(3, 7)

    def extract_posts(self) -> ExtractionResult:
        self._pause(0.2)
        visible = self._feed[: self._visible]
        return ExtractionResult(dom_count=len(visible) + self._rng.randint(0, 2), raw_posts=list(visible))

    def scroll(self) -> None:
        self._visible = min(len(self._feed), self._visible + self._rng.randint(3, 6))

    def close(self) -> None:
        self._feed = []
