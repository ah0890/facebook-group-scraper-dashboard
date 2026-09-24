"""
Django settings for the Facebook Group Scraper Dashboard.

All environment-specific values are read from environment variables (optionally
loaded from a local ``.env`` file). See ``.env.example`` for the full list.
"""

import os
from pathlib import Path
from urllib.parse import urlparse

from django.core.exceptions import ImproperlyConfigured
from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent.parent

load_dotenv(BASE_DIR / ".env")


def env(name: str, default: str = "") -> str:
    value = os.environ.get(name)
    return value if value else default


def env_bool(name: str, default: bool = False) -> bool:
    value = env(name)
    if not value:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def env_list(name: str, default: str = "") -> list[str]:
    return [item.strip() for item in env(name, default).split(",") if item.strip()]


def resolve_path(value: str) -> Path:
    path = Path(value)
    return path if path.is_absolute() else BASE_DIR / path


# ---------------------------------------------------------------------------
# Core
# ---------------------------------------------------------------------------

DEBUG = env_bool("DEBUG", False)

SECRET_KEY = env("DJANGO_SECRET_KEY")
if not SECRET_KEY or SECRET_KEY == "change-me":
    if not DEBUG:
        raise ImproperlyConfigured(
            "DJANGO_SECRET_KEY must be set to a unique value when DEBUG is off. "
            "Copy .env.example to .env and set a secret key."
        )
    # Development-only fallback. Never used when DEBUG=False.
    SECRET_KEY = "django-insecure-dev-only-key-do-not-use-in-production"

ALLOWED_HOSTS = env_list("ALLOWED_HOSTS", "127.0.0.1,localhost")
CSRF_TRUSTED_ORIGINS = env_list("CSRF_TRUSTED_ORIGINS", "")

INSTALLED_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "django.contrib.humanize",
    "dashboard.apps.DashboardConfig",
]

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
]

ROOT_URLCONF = "config.urls"

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [BASE_DIR / "templates"],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
                "dashboard.context_processors.app_context",
            ],
        },
    },
]

WSGI_APPLICATION = "config.wsgi.application"
ASGI_APPLICATION = "config.asgi.application"


# ---------------------------------------------------------------------------
# Database (SQLite only in this version)
# ---------------------------------------------------------------------------


def sqlite_path_from_url(url: str) -> Path:
    parsed = urlparse(url)
    if parsed.scheme != "sqlite":
        raise ImproperlyConfigured(
            f"Only sqlite:/// DATABASE_URLs are supported in this version (got {parsed.scheme!r})."
        )
    # sqlite:///db.sqlite3 -> relative to BASE_DIR
    # sqlite:////abs/path.db or sqlite:///C:/data/x.db -> absolute
    raw = parsed.path[1:] if parsed.path.startswith("/") else parsed.path
    return resolve_path(raw or "db.sqlite3")


DATABASE_PATH = sqlite_path_from_url(env("DATABASE_URL", "sqlite:///db.sqlite3"))

DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.sqlite3",
        "NAME": DATABASE_PATH,
        "OPTIONS": {
            # The scraper worker writes while the web server reads. A generous busy
            # timeout plus WAL mode (enabled in dashboard.apps) keeps them cooperating.
            "timeout": 30,
            "transaction_mode": "IMMEDIATE",
        },
        # File-based test DB: the threaded scraper tests need real SQLite locking
        # (shared-cache in-memory databases do not honour the busy timeout).
        "TEST": {"NAME": BASE_DIR / "test_db.sqlite3"},
    }
}

DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"


# ---------------------------------------------------------------------------
# Auth & security
# ---------------------------------------------------------------------------

AUTH_PASSWORD_VALIDATORS = [
    {"NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator"},
    {"NAME": "django.contrib.auth.password_validation.MinimumLengthValidator"},
    {"NAME": "django.contrib.auth.password_validation.CommonPasswordValidator"},
    {"NAME": "django.contrib.auth.password_validation.NumericPasswordValidator"},
]

LOGIN_URL = "login"
LOGIN_REDIRECT_URL = "dashboard"
LOGOUT_REDIRECT_URL = "login"

SESSION_COOKIE_HTTPONLY = True
CSRF_COOKIE_HTTPONLY = False  # dashboard.js reads the token from a <meta> tag instead
X_FRAME_OPTIONS = "DENY"
SECURE_CONTENT_TYPE_NOSNIFF = True
SECURE_REFERRER_POLICY = "same-origin"

if not DEBUG:
    SESSION_COOKIE_SECURE = env_bool("SECURE_COOKIES", True)
    CSRF_COOKIE_SECURE = env_bool("SECURE_COOKIES", True)


# ---------------------------------------------------------------------------
# Internationalization
# ---------------------------------------------------------------------------

LANGUAGE_CODE = "en-us"
TIME_ZONE = env("TIME_ZONE", "UTC")
USE_I18N = True
USE_TZ = True


# ---------------------------------------------------------------------------
# Static files
# ---------------------------------------------------------------------------

STATIC_URL = "static/"
STATICFILES_DIRS = [BASE_DIR / "static"]
STATIC_ROOT = BASE_DIR / "staticfiles"


# ---------------------------------------------------------------------------
# Scraper
# ---------------------------------------------------------------------------

SCRAPER_MODE = env("SCRAPER_MODE", "mock").lower()
if SCRAPER_MODE not in {"mock", "playwright"}:
    raise ImproperlyConfigured("SCRAPER_MODE must be 'mock' or 'playwright'.")

BROWSER_PROFILE_DIR = env("BROWSER_PROFILE_DIR", "browser_data/facebook_profile")
EXPORT_DIR = env("EXPORT_DIR", "exports")

# A run whose worker has not sent a heartbeat for this long is treated as crashed.
SCRAPER_STALE_AFTER_SECONDS = int(env("SCRAPER_STALE_AFTER_SECONDS", "180"))


# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "formatters": {
        "simple": {"format": "%(asctime)s %(levelname)-7s %(name)s: %(message)s"},
    },
    "handlers": {
        "console": {"class": "logging.StreamHandler", "formatter": "simple"},
    },
    "loggers": {
        "scraper": {"handlers": ["console"], "level": env("LOG_LEVEL", "INFO"), "propagate": False},
        "dashboard": {"handlers": ["console"], "level": env("LOG_LEVEL", "INFO"), "propagate": False},
    },
}
