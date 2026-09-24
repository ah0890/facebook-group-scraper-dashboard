"""Plain data structures used by the scraper engine.

These are deliberately free of Django so the browser/parsing code can be reasoned
about (and tested) in isolation. The database models live in ``dashboard.models``.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

MODE_MOCK = "mock"
MODE_PLAYWRIGHT = "playwright"


@dataclass(frozen=True)
class ScrapeConfig:
    """An immutable snapshot of the scraper settings taken when a run starts."""

    mode: str = MODE_MOCK
    posts_per_group: int = 20
    groups_per_run: int = 10
    scroll_limit: int = 20
    scroll_delay: float = 2.0
    page_timeout: int = 45
    retry_count: int = 2
    save_frequency: int = 5
    save_after_every_group: bool = True
    headless: bool = False
    browser_type: str = "chromium"
    profile_dir: Path = Path("browser_data/facebook_profile")
    export_dir: Path = Path("exports")
    log_level: str = "INFO"
    auth_wait_seconds: int = 300

    @property
    def page_timeout_ms(self) -> int:
        return int(self.page_timeout * 1000)


@dataclass
class ScrapedPost:
    """A single post as extracted from a group feed, after parsing."""

    facebook_post_id: str = ""
    author_name: str = ""
    author_url: str = ""
    post_text: str = ""
    post_url: str = ""
    post_timestamp: datetime | None = None
    timestamp_text: str = ""
    media_url: str = ""
    likes_count: int = 0
    comments_count: int = 0
    shares_count: int = 0
    raw_data: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["post_timestamp"] = self.post_timestamp.isoformat() if self.post_timestamp else None
        return data


@dataclass
class ExtractionResult:
    """What one extraction pass over the page returned."""

    dom_count: int
    raw_posts: list[dict[str, Any]]
