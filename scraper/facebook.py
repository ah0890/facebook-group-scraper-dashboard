"""Playwright client for reading posts from Facebook groups the user can access.

Scope and intent: this client only does what a person does in a normal browser
window – open a group the logged-in account can already see, scroll at a calm
pace and read the visible posts. It does not handle credentials, solve or bypass
CAPTCHAs/checkpoints, hide automation, rotate identities or work around rate
limits. When Facebook asks for login or verification, the run stops and the user
resolves it manually in the visible browser.
"""

from __future__ import annotations

import time
from typing import Callable

from .browser import BrowserManager
from .exceptions import (
    AuthenticationRequired,
    BrowserClosedError,
    GroupUnavailableError,
    MissingElementError,
    NavigationError,
    NetworkError,
    PageTimeoutError,
    ScraperError,
    SessionExpired,
)
from .models import ExtractionResult, ScrapeConfig

FACEBOOK_HOME = "https://www.facebook.com/"
FEED_SELECTOR = 'div[role="feed"]'

# Runs inside the page. Reads what is rendered in the group feed; it does not
# click, submit or modify anything.
EXTRACT_POSTS_JS = r"""
() => {
  const feed = document.querySelector('div[role="feed"]');
  if (!feed) return { dom: 0, posts: [] };
  const units = Array.from(feed.children);
  const postRe = /\/groups\/[^/]+\/(posts|permalink)\/|story_fbid=|multi_permalinks=|\/posts\/pfbid/;
  const posts = [];
  for (const unit of units) {
    const article = unit.querySelector('[role="article"]') || unit.querySelector('[aria-posinset]') || unit;
    const anchors = Array.from(article.querySelectorAll('a[href]'));
    const postAnchor = anchors.find(a => postRe.test(a.href));
    const msgEl = article.querySelector(
      '[data-ad-preview="message"], [data-ad-comet-preview="message"], [data-ad-rendering-role="story_message"]'
    );
    const text = msgEl ? (msgEl.innerText || '') : '';
    const heading = article.querySelector('h2, h3, h4, [data-ad-rendering-role="profile_name"]');
    const authorEl = heading ? (heading.querySelector('a') || heading) : null;
    const author = authorEl ? (authorEl.innerText || '').split('\n')[0].trim() : '';
    const authorUrl = authorEl && authorEl.href ? authorEl.href : '';
    const tsText = postAnchor ? (postAnchor.getAttribute('aria-label') || postAnchor.innerText || '') : '';
    const img = Array.from(article.querySelectorAll('img')).find(
      i => (i.naturalWidth || i.width) >= 200 && i.src && i.src.startsWith('http')
    );
    const labels = Array.from(article.querySelectorAll('[aria-label]'))
      .map(e => e.getAttribute('aria-label'))
      .filter(t => t && t.length < 120).slice(0, 60);
    const snippets = Array.from(article.querySelectorAll('span'))
      .map(s => (s.innerText || '').trim())
      .filter(t => t && t.length < 40 && /\d/.test(t)).slice(0, 80);
    if (!text.trim() && !postAnchor) continue;
    posts.push({
      author, author_url: authorUrl, text: text.trim(),
      post_url: postAnchor ? postAnchor.href : '', timestamp_text: tsText.trim(),
      media_url: img ? img.src : '', labels, snippets, source: 'feed'
    });
  }
  return { dom: units.length, posts };
}
"""

LogFn = Callable[[str, str], None]
WaitFn = Callable[[float], None]


def _noop_log(level: str, message: str) -> None:
    pass


def map_playwright_error(exc: Exception, context: str) -> ScraperError:
    """Translate a Playwright exception into the scraper's error hierarchy."""
    from playwright.sync_api import TimeoutError as PlaywrightTimeoutError

    message = str(exc)
    first_line = message.splitlines()[0] if message else exc.__class__.__name__
    if isinstance(exc, PlaywrightTimeoutError):
        return PageTimeoutError(f"Timed out while {context}.")
    if "has been closed" in message or "Target closed" in message:
        return BrowserClosedError("The browser window was closed during the run.")
    if any(code in message for code in ("ERR_INTERNET_DISCONNECTED", "ERR_NAME_NOT_RESOLVED",
                                         "ERR_CONNECTION", "ERR_NETWORK", "ERR_TIMED_OUT", "ERR_PROXY")):
        return NetworkError(f"Network error while {context}: {first_line}")
    return NavigationError(f"Error while {context}: {first_line}")


def is_logged_in(browser: BrowserManager) -> bool:
    """True when the profile holds a Facebook session cookie for the user."""
    try:
        return any(cookie.get("name") == "c_user" for cookie in browser.cookies_for(FACEBOOK_HOME))
    except Exception:
        return False


def on_login_page(page) -> bool:
    try:
        return "/login" in page.url or page.locator('input[name="pass"]').count() > 0
    except Exception:
        return False


def on_checkpoint(page) -> bool:
    try:
        return "/checkpoint" in page.url
    except Exception:
        return False


class FacebookClient:
    """Real-browser implementation of the scraper client interface.

    Interface shared with ``MockFacebookClient``::

        start() / ensure_authenticated() / open_group(url) / extract_posts()
        scroll() / close()
    """

    def __init__(self, cfg: ScrapeConfig, *, log: LogFn | None = None, wait: WaitFn = time.sleep,
                 headless: bool | None = None):
        self.cfg = cfg
        self.log = log or _noop_log
        self.wait = wait
        self.headless = cfg.headless if headless is None else headless
        self.browser = BrowserManager(cfg.profile_dir, headless=self.headless, timeout_ms=cfg.page_timeout_ms)

    # -- lifecycle ----------------------------------------------------------

    def start(self) -> None:
        mode = "headless" if self.headless else "visible"
        self.log("INFO", f"Launching Chromium ({mode}) with profile {self.cfg.profile_dir.name}/")
        self.browser.start()
        self.log("SUCCESS", "Browser ready")

    def close(self) -> None:
        self.browser.close()

    # -- session --------------------------------------------------------------

    def _goto(self, url: str, context: str):
        from playwright.sync_api import Error as PlaywrightError

        page = self.browser.ensure_page()
        try:
            return page.goto(url, wait_until="domcontentloaded")
        except PlaywrightError as exc:
            raise map_playwright_error(exc, context) from exc

    def ensure_authenticated(self) -> None:
        self._goto(FACEBOOK_HOME, "opening Facebook")
        page = self.browser.ensure_page()
        if on_checkpoint(page):
            raise AuthenticationRequired(
                "Facebook is asking for account verification. Resolve it manually via "
                "'Open Browser / Authenticate', then start the run again."
            )
        if is_logged_in(self.browser) and not on_login_page(page):
            self.log("SUCCESS", "Facebook session active (persistent profile)")
            return
        if self.headless:
            raise AuthenticationRequired(
                "Not logged in to Facebook. Use 'Open Browser / Authenticate' on the Run Scraper page "
                "(or switch off headless mode), then start the run again."
            )

        wait_for = self.cfg.auth_wait_seconds
        self.log("WARNING", f"Not logged in. Please log in manually in the browser window "
                            f"(waiting up to {wait_for // 60} min)…")
        deadline = time.monotonic() + wait_for
        while time.monotonic() < deadline:
            self.wait(2)  # raises StopRequested if the user presses Stop
            if not self.browser.is_open:
                raise BrowserClosedError("The browser window was closed before login completed.")
            if is_logged_in(self.browser) and not on_login_page(self.browser.ensure_page()):
                self.log("SUCCESS", "Login detected – session saved to the browser profile")
                return
        raise AuthenticationRequired("Timed out waiting for manual login.")

    # -- scraping ---------------------------------------------------------------

    def open_group(self, url: str) -> None:
        from playwright.sync_api import Error as PlaywrightError

        response = self._goto(url, "loading the group page")
        page = self.browser.ensure_page()

        if on_checkpoint(page):
            raise AuthenticationRequired("Facebook is asking for account verification. Resolve it manually.")
        if on_login_page(page):
            raise SessionExpired("Facebook session expired (redirected to login). Please re-authenticate.")
        if response is not None and response.status >= 500:
            raise NavigationError(f"Facebook returned HTTP {response.status}.")

        try:
            page.wait_for_selector(FEED_SELECTOR, state="attached", timeout=self.cfg.page_timeout_ms)
        except PlaywrightError as exc:
            try:
                body = page.inner_text("body", timeout=5000).lower()
            except PlaywrightError:
                body = ""
            if "content isn't available" in body or "page isn't available" in body:
                raise GroupUnavailableError(
                    "Group is not available to this account (removed, private or wrong URL)."
                ) from exc
            if "join group" in body:
                raise GroupUnavailableError(
                    "This account is not a member of the group. Join it in the browser first."
                ) from exc
            error = map_playwright_error(exc, "waiting for the group feed")
            if isinstance(error, PageTimeoutError):
                raise MissingElementError("Group feed not found (page layout may have changed).") from exc
            raise error from exc
        self.wait(1.5)  # let the first posts render

    def _expand_see_more(self, page) -> None:
        """Expand truncated posts so the full text is read (max a few per pass)."""
        try:
            buttons = page.locator(f'{FEED_SELECTOR} div[role="button"]:text-is("See more")')
            for index in range(min(buttons.count(), 5)):
                try:
                    buttons.nth(index).click(timeout=1500)
                except Exception:
                    continue
        except Exception:
            pass

    def extract_posts(self) -> ExtractionResult:
        from playwright.sync_api import Error as PlaywrightError

        page = self.browser.ensure_page()
        self._expand_see_more(page)
        try:
            data = page.evaluate(EXTRACT_POSTS_JS)
        except PlaywrightError as exc:
            raise map_playwright_error(exc, "reading posts") from exc
        return ExtractionResult(dom_count=int(data.get("dom", 0)), raw_posts=list(data.get("posts", [])))

    def scroll(self) -> None:
        from playwright.sync_api import Error as PlaywrightError

        try:
            self.browser.ensure_page().evaluate("window.scrollBy(0, Math.round(window.innerHeight * 0.85))")
        except PlaywrightError as exc:
            raise map_playwright_error(exc, "scrolling") from exc


# ---------------------------------------------------------------------------
# Stand-alone browser tasks (used by the dashboard's browser buttons and CLI)
# ---------------------------------------------------------------------------

ReportFn = Callable[[str, str], None]


def authenticate_interactively(cfg: ScrapeConfig, *, report: ReportFn,
                               should_cancel: Callable[[], bool] = lambda: False) -> bool:
    """Open a visible browser so the user can log in to Facebook themselves.

    ``report(status, message)`` receives ``WAITING``/``AUTHENTICATED``/``NOT_AUTH``/``ERROR``.
    Returns ``True`` once a logged-in session is detected in the persistent profile.
    """
    from playwright.sync_api import Error as PlaywrightError

    browser = BrowserManager(cfg.profile_dir, headless=False, timeout_ms=cfg.page_timeout_ms)
    try:
        browser.start()
        page = browser.ensure_page()
        try:
            page.goto(FACEBOOK_HOME, wait_until="domcontentloaded")
        except PlaywrightError:
            pass  # the user can still navigate manually
        if is_logged_in(browser) and not on_login_page(page):
            report("AUTHENTICATED", "Already logged in – session is stored in the browser profile.")
            time.sleep(2)
            return True

        report("WAITING", f"Browser opened. Log in to Facebook in that window "
                          f"(waiting up to {cfg.auth_wait_seconds // 60} min).")
        deadline = time.monotonic() + cfg.auth_wait_seconds
        while time.monotonic() < deadline:
            if should_cancel():
                report("NOT_AUTH", "Authentication cancelled.")
                return False
            if not browser.is_open:
                report("NOT_AUTH", "Browser was closed before login completed.")
                return False
            if is_logged_in(browser) and not on_login_page(browser.ensure_page()):
                report("AUTHENTICATED", "Logged in – session saved to the browser profile.")
                time.sleep(2)
                return True
            time.sleep(1)
        report("NOT_AUTH", "Timed out waiting for login.")
        return False
    except ScraperError as exc:
        report("ERROR", str(exc))
        return False
    finally:
        browser.close()


def check_session(cfg: ScrapeConfig) -> tuple[bool, str]:
    """Headless check of whether the persistent profile is logged in."""
    browser = BrowserManager(cfg.profile_dir, headless=True, timeout_ms=cfg.page_timeout_ms)
    try:
        browser.start()
        client_page = browser.ensure_page()
        client_page.goto(FACEBOOK_HOME, wait_until="domcontentloaded")
        if on_checkpoint(client_page):
            return False, "Facebook requires account verification – open the browser to resolve it."
        if is_logged_in(browser) and not on_login_page(client_page):
            return True, "Session active."
        return False, "Not logged in. Use 'Open Browser / Authenticate'."
    except ScraperError as exc:
        return False, str(exc)
    except Exception as exc:
        return False, f"Session check failed: {str(exc).splitlines()[0] if str(exc) else exc}"
    finally:
        browser.close()


def test_group_url(cfg: ScrapeConfig, url: str) -> tuple[str, str]:
    """Open a group URL headlessly and report ``(status, message)``.

    ``status`` is ``OK``, ``WARNING`` or ``ERROR``.
    """
    client = FacebookClient(cfg, headless=True)
    try:
        client.start()
        client.open_group(url)
        result = client.extract_posts()
        title = client.browser.ensure_page().title()
        return "OK", f"Feed visible ({len(result.raw_posts)} posts rendered). Page: {title[:120]}"
    except AuthenticationRequired as exc:
        return "WARNING", str(exc)
    except GroupUnavailableError as exc:
        return "ERROR", str(exc)
    except ScraperError as exc:
        return "ERROR", str(exc)
    except Exception as exc:
        return "ERROR", f"Unexpected error: {exc}"
    finally:
        client.close()
