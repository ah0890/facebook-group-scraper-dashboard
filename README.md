# Facebook Group Scraper Dashboard

A self-hosted Django dashboard for collecting posts from **Facebook groups you are a member of and are
authorised to collect data from**. You manage groups, start and stop scraper runs, watch live logs, browse
and export collected posts, and review run history and statistics, all from one admin interface.

Scraping uses Playwright with a normal, visible Chromium window and **your own manually logged-in browser
profile**. A **mock mode** simulates the whole pipeline without contacting Facebook, so you can demo,
develop and test the app safely.

> **Responsible use.** Only collect data from groups where you are allowed to, and follow Facebook's terms
> and applicable privacy law. This project deliberately does **not** implement CAPTCHA solving, checkpoint or
> 2FA bypassing, stealth/anti-detection tricks, credential handling or rate-limit evasion. If Facebook asks
> for login or verification, the run stops and you resolve it yourself in the browser.

---

## Contents

- [Features](#features)
- [Architecture](#architecture)
- [Requirements](#requirements)
- [Installation](#installation)
- [Running the application](#running-the-application)
- [Mock mode](#mock-mode)
- [Facebook authentication](#facebook-authentication)
- [Starting a scraper run](#starting-a-scraper-run)
- [Exporting data](#exporting-data)
- [Command-line tools](#command-line-tools)
- [Configuration](#configuration)
- [Recovery and data safety](#recovery-and-data-safety)
- [Testing](#testing)
- [Troubleshooting](#troubleshooting)
- [Security notes](#security-notes)
- [Limitations](#limitations)

---

## Features

| Area | What you get |
| --- | --- |
| **Dashboard** | Summary cards (groups, enabled, posts, today, runs, last run, live status), next batch, recent runs |
| **Groups** | Add / edit / delete, enable/disable, test URL, run a single group, URL validation and normalisation |
| **Run Scraper** | Start / Stop / Reset to Defaults / Configure, live phase, progress bar, per-group status badges, "Next Batch" preview, browser session card, terminal-style **live logs** |
| **Posts** | Search, filter by group / author / date (posted or collected), sorting, pagination, **CSV & JSON export** of the filtered set |
| **Logs** | Filter by run, group, level, phase, date and text; clear old logs (with confirmation) |
| **Stats** | Posts today / this week / this month, run outcomes, average posts per group, average run duration, Chart.js charts |
| **Settings / Defaults** | Active settings plus saved defaults; "Reset Defaults" restores safe factory values |
| **DB** | SQLite file info, row counts, optimise/checkpoint |
| **Admin** | Django admin for every model |
| **Engine** | Sequential groups, calm scrolling, retries with backoff, deduplication, incremental saving, crash recovery, cooperative stop |

Scraper phases shown in the UI: `IDLE → SEEDING → INITIALIZING → AUTHENTICATING → FETCHING → SCROLLING → PARSING → SAVING → COMPLETED / FAILED / STOPPED`.

---

## Architecture

```text
facebook-group-scraper-dashboard/
├── manage.py
├── config/                 Django project (settings from .env, urls, wsgi/asgi)
├── dashboard/              Django app: the web UI and data model
│   ├── models.py           Group, Post, ScraperRun, RunGroup, ScraperLog, ScraperSetting, BrowserSession
│   ├── services.py         Run queueing, rounds / next batch, crash recovery, stats
│   ├── views.py / urls.py  Pages, control endpoints, JSON status API
│   ├── forms.py            Group, settings and filter forms
│   ├── exports.py          CSV / JSON export
│   ├── validators.py       Facebook group URL validation and normalisation
│   ├── admin.py            Django admin registrations
│   └── management/commands run_scraper, seed_demo_data, cleanup_logs, export_posts, authenticate
├── scraper/                Scraper engine (plain Python plus a thin ORM layer)
│   ├── runner.py           execute_run(): the sequential run loop, retries, stop handling
│   ├── manager.py          Background thread management for the web UI
│   ├── browser.py          Chromium with a persistent Playwright profile
│   ├── facebook.py         Real-browser client plus authenticate / check session / test URL
│   ├── mock.py             Simulated client for SCRAPER_MODE=mock
│   ├── parser.py           Raw feed data to ScrapedPost (IDs, counts, timestamps), pure functions
│   ├── storage.py          Buffered, deduplicating saves; atomic JSON snapshot
│   ├── logger.py           RunLogger that writes ScraperLog rows for the live panel
│   ├── models.py           Dataclasses: ScrapeConfig, ScrapedPost
│   └── exceptions.py       Error types that decide retry / skip / abort
├── templates/              base.html, dashboard/*.html, partials/*.html, registration/login.html
├── static/                 css/dashboard.css, js/dashboard.js (vanilla JS)
├── tests/                  87 tests; none touch Facebook
├── exports/                JSON snapshots and CLI exports (git-ignored)
└── browser_data/           Persistent Chromium profile (git-ignored)
```

### How a run works

1. **Start Scraper** calls `ScraperManager.start()`, which checks that no run is active, creates a `ScraperRun`
   plus one `RunGroup` per queued group, and starts a background thread. Django keeps serving requests.
2. The thread calls `scraper.runner.execute_run()`. It opens the browser (or the mock client), checks the login,
   then processes groups **one at a time**. For each group it opens the page, then repeatedly extracts visible
   posts, parses them, deduplicates them, saves them in batches, scrolls, and waits `scroll_delay` seconds.
3. Every log line is a `ScraperLog` row. The browser polls `/api/status/` (every 1.5 s while running) and
   appends new lines to the terminal panel. No WebSockets are needed.
4. **Stop Scraper** sets a thread-safe `threading.Event` and a `stop_requested` flag in the database, so runs
   started from the CLI can be stopped from the web UI too. The worker notices at the next checkpoint (between
   scrolls or during a wait), flushes pending posts, marks the remaining groups `SKIPPED` and the run `STOPPED`.

### Rounds and batches

A round is one pass over all enabled groups. Each group stores the last round it
completed. A run takes the next `groups_per_run` groups that still need the current round, preferring groups
that have never run or ran longest ago. That's where messages like *"All 8 remaining groups will be scraped to
complete Round #3"* come from.

---

## Requirements

- Python **3.12 or newer** (developed and tested on 3.13)
- Windows, macOS or Linux
- About 400 MB of disk for Playwright's Chromium

---

## Installation

```bash
# 1. Create and activate a virtual environment
python -m venv .venv

# Windows (PowerShell or cmd)
.venv\Scripts\activate
# macOS / Linux
source .venv/bin/activate

# 2. Install dependencies
pip install -r requirements.txt

# 3. Install the Chromium browser used by Playwright
playwright install chromium

# 4. Configure the environment
#    Windows: copy .env.example .env     macOS/Linux: cp .env.example .env
#    Then set DJANGO_SECRET_KEY in .env, for example with:
python -c "import secrets; print(secrets.token_urlsafe(50))"

# 5. Create the database
python manage.py migrate

# 6. Create your login for the dashboard
python manage.py createsuperuser
```

> On Windows, if `python` resolves to a broken install, use the launcher: `py -3.13 -m venv .venv`.

---

## Running the application

```bash
python manage.py runserver
```

Open <http://127.0.0.1:8000/> and sign in with the user you created. Every page requires login.

---

## Mock mode

Mock mode (`SCRAPER_MODE=mock`, the default in `.env.example`) replaces the browser with a simulator:

- Generates realistic groups, authors, post text, timestamps, media and engagement counts
- Simulates page loads, scrolling and a feed that grows over time (a new post every 5 minutes per group),
  so re-runs show both **new posts** and **deduplication** ("already saved", "caught up")
- Produces the same live logs, phases, progress and incremental saves as a real run
- Special demo URLs: a slug containing `flaky` fails once and then succeeds on retry, `broken` always fails,
  and `private` is reported as "not a member"

Load demo data (10 groups plus three weeks of run history for the charts):

```bash
python manage.py seed_demo_data            # groups + history
python manage.py seed_demo_data --no-history
python manage.py seed_demo_data --reset    # remove and regenerate demo data
```

Then open **Run Scraper → Start Scraper** and watch the live logs.

You can switch modes at any time in **Settings → Scraper mode**. The environment variable only sets the
initial value.

---

## Facebook authentication

The app never asks for, stores or types your Facebook password. You log in yourself, once, in a real browser
window. The session lives in the persistent profile directory (`browser_data/facebook_profile/` by default)
and is reused by later runs.

1. In **Settings**, set **Scraper mode** to *Playwright (real browser)*.
2. On **Run Scraper**, click **Open Browser / Authenticate**. A Chromium window opens on facebook.com.
3. Log in normally, including any 2FA or verification Facebook asks for.
4. The dashboard detects the login, marks the session **Authenticated** and closes the window.
   **Check session** re-verifies it later.

CLI alternative: `python manage.py authenticate` (or `--check`).

If the session expires during a visible (non-headless) run, the scraper pauses and waits for you to log in
again, for up to *Wait for manual login* seconds. In headless mode it stops with a clear message instead.

---

## Starting a scraper run

1. **Groups → Add group**: enter a name and the group URL, for example `https://www.facebook.com/groups/<id>/`.
   URLs are validated and normalised; `m.` and `web.` links are accepted. Use **Test URL** (the plug icon)
   to confirm the account can see the group's feed.
2. Review **Settings** (sensible defaults: 20 posts per group, 10 groups per run, 20 scrolls, 2 s delay,
   2 retries).
3. **Run Scraper → Start Scraper**. The *Next Batch* table becomes the live queue, with a status badge per
   group (`Queued`, `Running`, `Completed`, `Failed`, `Skipped`).
4. **Stop Scraper** at any time. The current step finishes, collected posts are saved and the run is marked
   `STOPPED`.

To scrape a single group right away, use the ▶ button on the Groups page.

Each group runs in one of two modes:

- **FIRST fetch**: the group has no saved posts yet; scrape up to *posts per group*.
- **UPDATE fetch**: scrape up to *posts per group* new posts, and stop early once the feed only shows posts
  that are already saved ("caught up").

---

## Exporting data

- **Posts page → Export CSV / Export JSON** downloads exactly the posts matching the current filters.
  CSV is UTF-8 with a BOM so Excel opens it cleanly, and cells that start with `=`, `+`, `-` or `@` are
  escaped against formula injection.
- **Automatic snapshot**: with *Export JSON snapshot after every group* enabled, `exports/fetched_posts.json`
  is rewritten atomically after each group with the current run's posts.
- **CLI**: `python manage.py export_posts --format csv|json [--group ID] [--output path]`

---

## Command-line tools

| Command | Purpose |
| --- | --- |
| `python manage.py run_scraper [--mode mock\|playwright] [--groups 1,2] [--quiet]` | Run the next batch (or given groups) in the terminal; Ctrl+C stops safely |
| `python manage.py seed_demo_data [--reset] [--no-history] [--days N]` | Create demo groups and history |
| `python manage.py cleanup_logs [--days N] [--dry-run]` | Delete logs older than the retention period |
| `python manage.py export_posts --format csv\|json` | Export posts to the export directory |
| `python manage.py authenticate [--check]` | Log in manually, or check the saved session |

Runs started from the CLI appear live in the dashboard and can be stopped from it.

---

## Configuration

### Environment (`.env`)

| Variable | Default | Description |
| --- | --- | --- |
| `DJANGO_SECRET_KEY` | none | Required when `DEBUG=False` |
| `DEBUG` | `False` | Development mode |
| `ALLOWED_HOSTS` | `127.0.0.1,localhost` | Comma-separated |
| `CSRF_TRUSTED_ORIGINS` | empty | Needed when serving behind a proxy or custom domain |
| `TIME_ZONE` | `UTC` | Used for "today", charts and timestamps |
| `DATABASE_URL` | `sqlite:///db.sqlite3` | SQLite only in this version |
| `SCRAPER_MODE` | `mock` | Initial mode: `mock` or `playwright` |
| `BROWSER_PROFILE_DIR` | `browser_data/facebook_profile` | Persistent Chromium profile |
| `EXPORT_DIR` | `exports` | Snapshot and export directory |
| `SCRAPER_STALE_AFTER_SECONDS` | `180` | Heartbeat age after which a run counts as crashed |
| `LOG_LEVEL` | `INFO` | Console logging level |

### In-app settings (Settings page)

| Setting | Default | Notes |
| --- | --- | --- |
| Posts per group | 20 | 1–500 |
| Groups per run | 10 | 1–200 |
| Scroll limit | 20 | Maximum scrolls per group |
| Scroll delay | 2.0 s | Minimum 1 s |
| Page timeout | 45 s | Navigation and feed wait |
| Retry count | 2 | Retries for transient errors, with 5 s × attempt backoff |
| Save every N posts | 5 | Database write batch size |
| JSON snapshot after every group | on | `fetched_posts.json` |
| Headless browser | off | Visible mode lets you re-authenticate mid-run |
| Profile / export directory | from `.env` | Relative paths resolve against the project root |
| Log level / retention | INFO / 30 days | Old logs are pruned at the start of each run |

### Defaults page

*Save Defaults* stores your baseline. *Reset to Defaults* (Run Scraper page) copies it into
the active settings. *Reset Defaults* restores the factory values above.

---

## Recovery and data safety

- Posts are written in small transactions (every *N* posts and at the end of every group), and each row
  in its own savepoint, so a crash loses at most one small batch and never leaves partial rows.
- Deduplication is enforced in code and by the database: `dedup_key` is unique, and so is
  `(group, facebook_post_id)` when an ID exists. Posts without an ID fall back to a hash of author and text.
  Re-collected posts refresh their engagement counts.
- A group's round progress is recorded as soon as the group finishes. If the server crashes or restarts
  mid-run, the run is detected as interrupted (orphaned worker, or no heartbeat for
  `SCRAPER_STALE_AFTER_SECONDS`) and closed as `PARTIAL` or `FAILED`. The next run continues with the groups
  still left in the round and does not repeat completed ones.
- One failing group never stops the run. Transient errors (timeouts, network problems, missing feed) are
  retried; unavailable groups fail immediately. Only session or browser problems abort a run.
- The JSON snapshot is written to a temporary file and renamed into place.

---

## Testing

```bash
python manage.py test
```

87 tests cover group creation and URL validation, post deduplication, run creation and status changes,
retries and failures, crash recovery, log creation and cleanup, settings and defaults, CSV and JSON export,
the stop mechanism (in-process event, database flag, and a real background thread), views, the JSON API,
and the management commands. They use the mock client and never contact Facebook. One test runs the real
in-page extraction script in headless Chromium against a local HTML fixture, and skips itself if Chromium
is not installed.

---

## Troubleshooting

| Problem | Fix |
| --- | --- |
| `ImproperlyConfigured: DJANGO_SECRET_KEY must be set` | Create `.env` from `.env.example` and set a key, or set `DEBUG=True` for local development |
| "Chromium is not installed" | `playwright install chromium` inside the virtual environment |
| "The browser profile is already in use" | Close other Chromium windows using `browser_data/facebook_profile` (for example a stuck authentication window) |
| "Not logged in to Facebook" | Use **Open Browser / Authenticate**, or turn off headless mode so the run can wait for you |
| "Facebook is asking for account verification" | Complete the checkpoint yourself in the authentication window; the scraper will not bypass it |
| Group fails with "not a member" | Join the group with your account in the browser first |
| "Group feed not found" on every group | Facebook changed its page layout; the selectors live in `scraper/facebook.py` (`EXTRACT_POSTS_JS`) |
| A run stays "Running" after a crash | It closes automatically once its heartbeat is older than `SCRAPER_STALE_AFTER_SECONDS`, or immediately when this server process owned it |
| `database is locked` | Rare with WAL mode. Avoid opening the database in other tools while a run is active |
| Live logs stop updating | Check the terminal running `runserver` for errors; the page retries polling automatically |

---

## Security notes

- Every page and endpoint requires Django login; scraper, browser and deletion controls accept **POST only**
  and are CSRF-protected.
- The secret key comes from the environment; the app refuses to start without one when `DEBUG=False`.
- No Facebook credentials are handled, stored or logged. Only a status message (for example "Authenticated")
  is kept in the database.
- Browser profiles (cookies, session), databases, exports and `.env` are git-ignored. Treat
  `browser_data/` as a password: anyone with that folder can use your Facebook session.
- Group URLs are validated against an allow-list of facebook.com hosts and a `/groups/<id>` path.
- Scraped text is never rendered as HTML: templates auto-escape it, and the live log uses `textContent`.
  External links use `rel="noopener noreferrer"`.
- `X-Frame-Options: DENY`, `nosniff`, a same-origin referrer policy and secure cookies (when `DEBUG=False`)
  are enabled.
- Intended for local or trusted-network use. Before exposing it more widely, put it behind HTTPS and a
  production WSGI server.

---

## Limitations

- **Facebook's markup changes often.** Extraction uses best-effort selectors (`role="feed"`, message
  containers, post permalinks). Expect to adjust `EXTRACT_POSTS_JS` from time to time. Timestamps and
  engagement counts are not always present in the DOM, so some posts will have them empty.
- Only posts visible in the group feed are collected: no comments, reactions breakdowns or member lists.
- One worker and one browser profile per server process. Runs are sequential by design.
- The background worker is a thread in the web process. A server restart interrupts the current run, but
  recovery picks up where it left off. For unattended scheduling, use `python manage.py run_scraper` from
  cron or Task Scheduler.
- SQLite only. It is fine for one user and hundreds of thousands of posts; switching to PostgreSQL needs a
  small settings change.
- The UI loads Bootstrap, Bootstrap Icons, Chart.js and Google Fonts from public CDNs, so it needs internet
  access to look right.

---

## License

MIT (see [LICENSE](LICENSE)).
