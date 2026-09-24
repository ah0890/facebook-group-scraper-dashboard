"""Exception hierarchy for the scraper engine.

The runner uses these types to decide what to do next:

* ``RecoverableScrapeError`` – retry the group (up to ``retry_count``), then move on.
* ``GroupUnavailableError`` – do not retry, mark the group failed, move on.
* ``AuthenticationRequired`` / ``BrowserLaunchError`` / ``BrowserClosedError`` –
  fatal for the whole run: nothing else can succeed without a working session.
* ``StopRequested`` – the user pressed Stop; save progress and end cleanly.
"""


class ScraperError(Exception):
    """Base class for all scraper errors."""

    recoverable = False


class StopRequested(ScraperError):
    """Raised at a safe checkpoint after the user asked the run to stop."""


class BrowserLaunchError(ScraperError):
    """Chromium could not be started (not installed, profile locked, ...)."""


class BrowserClosedError(ScraperError):
    """The browser window/context was closed while the scraper was using it."""


class AuthenticationRequired(ScraperError):
    """The persistent profile is not logged in to Facebook."""


class SessionExpired(AuthenticationRequired):
    """Facebook redirected to the login page in the middle of a run."""


class GroupUnavailableError(ScraperError):
    """The group cannot be viewed with this account (not a member, removed, private)."""


class RecoverableScrapeError(ScraperError):
    """A transient problem; the group can be retried."""

    recoverable = True


class PageTimeoutError(RecoverableScrapeError):
    """The page (or an expected element) did not load in time."""


class NavigationError(RecoverableScrapeError):
    """Navigation failed (bad response, redirect loop, ...)."""


class NetworkError(RecoverableScrapeError):
    """A network-level failure (DNS, connection reset, offline)."""


class MissingElementError(RecoverableScrapeError):
    """The page loaded but the expected structure (e.g. the feed) was not found."""


class StorageError(ScraperError):
    """Saving collected data failed."""
