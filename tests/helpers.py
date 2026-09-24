"""Shared test helpers. Nothing here touches Facebook."""

from dashboard.models import Group, ScraperSetting
from scraper.mock import MockFacebookClient


def fast_settings(**overrides) -> ScraperSetting:
    """Active settings tuned for tests: mock mode, no delays."""
    values = {
        "scraper_mode": "mock",
        "posts_per_group": 10,
        "groups_per_run": 10,
        "scroll_limit": 10,
        "scroll_delay": 0,
        "retry_count": 1,
        "save_frequency": 3,
        "save_after_every_group": False,
        "log_level": "DEBUG",
    }
    values.update(overrides)
    setting = ScraperSetting.get_active()
    ScraperSetting.objects.filter(pk=setting.pk).update(**values)
    setting.refresh_from_db()
    return setting


def make_group(name="Test Group", slug=None, **kwargs) -> Group:
    slug = slug or name.lower().replace(" ", "-")
    return Group.objects.create(name=name, facebook_url=f"https://www.facebook.com/groups/{slug}/", **kwargs)


def fast_mock_factory(cfg, *, log, wait):
    return MockFacebookClient(cfg, log=log, wait=wait, speed=0)


class CallbackMockClient(MockFacebookClient):
    """Mock client that calls ``on_extract(client, n)`` after each extraction.

    ``n`` counts extractions within the current group; ``client.current_url`` is the group URL.
    """

    def __init__(self, *args, on_extract=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.on_extract = on_extract
        self.current_url = ""
        self.extractions = 0

    def open_group(self, url):
        self.current_url = url
        self.extractions = 0
        super().open_group(url)

    def extract_posts(self):
        result = super().extract_posts()
        self.extractions += 1
        if self.on_extract:
            self.on_extract(self, self.extractions)
        return result
