"""Chromium lifecycle using a persistent Playwright profile.

The profile directory keeps the user's own Facebook login between runs, so the
user authenticates manually once (in a visible window) and the session is reused.
No credentials are ever handled by this code.
"""

from __future__ import annotations

import logging
from pathlib import Path

from .exceptions import BrowserLaunchError

pylog = logging.getLogger("scraper")

VIEWPORT = {"width": 1280, "height": 900}


class BrowserManager:
    """Start/stop Chromium with a persistent user-data directory.

    Usage::

        with BrowserManager(profile_dir, headless=False, timeout_ms=45000) as browser:
            browser.page.goto("https://www.facebook.com/")
    """

    def __init__(self, profile_dir: Path, *, headless: bool = False, timeout_ms: int = 45_000):
        self.profile_dir = Path(profile_dir)
        self.headless = headless
        self.timeout_ms = timeout_ms
        self._playwright = None
        self.context = None
        self.page = None

    def start(self) -> "BrowserManager":
        try:
            from playwright.sync_api import Error as PlaywrightError, sync_playwright
        except ImportError as exc:  # pragma: no cover - dependency missing
            raise BrowserLaunchError("Playwright is not installed. Run: pip install -r requirements.txt") from exc

        self.profile_dir.mkdir(parents=True, exist_ok=True)
        try:
            self._playwright = sync_playwright().start()
            self.context = self._playwright.chromium.launch_persistent_context(
                user_data_dir=str(self.profile_dir),
                headless=self.headless,
                viewport=VIEWPORT,
                locale="en-US",
            )
        except PlaywrightError as exc:
            self.close()
            message = str(exc)
            if "Executable doesn't exist" in message or "playwright install" in message:
                hint = "Chromium is not installed. Run: playwright install chromium"
            elif "ProcessSingleton" in message or "user data directory is already in use" in message.lower():
                hint = "The browser profile is already in use. Close other Chromium windows using it and retry."
            else:
                hint = message.splitlines()[0] if message else "Unknown error"
            raise BrowserLaunchError(f"Could not launch Chromium: {hint}") from exc
        except Exception as exc:
            self.close()
            raise BrowserLaunchError(f"Could not launch Chromium: {exc}") from exc

        self.context.set_default_timeout(self.timeout_ms)
        self.context.set_default_navigation_timeout(self.timeout_ms)
        self.page = self.context.pages[0] if self.context.pages else self.context.new_page()
        return self

    @property
    def is_open(self) -> bool:
        try:
            return self.context is not None and bool(self.context.pages)
        except Exception:
            return False

    def ensure_page(self):
        """Return a usable page, opening a new tab if the user closed ours."""
        if self.page is None or self.page.is_closed():
            if self.context is None:
                raise BrowserLaunchError("Browser is not running.")
            self.page = self.context.pages[0] if self.context.pages else self.context.new_page()
        return self.page

    def cookies_for(self, url: str) -> list[dict]:
        return self.context.cookies(url) if self.context else []

    def close(self) -> None:
        for closer in (
            lambda: self.context and self.context.close(),
            lambda: self._playwright and self._playwright.stop(),
        ):
            try:
                closer()
            except Exception:  # closing must never raise
                pylog.debug("Error while closing browser", exc_info=True)
        self.context = None
        self.page = None
        self._playwright = None

    def __enter__(self) -> "BrowserManager":
        return self.start()

    def __exit__(self, *exc_info) -> None:
        self.close()
