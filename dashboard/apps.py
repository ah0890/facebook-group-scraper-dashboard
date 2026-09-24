from django.apps import AppConfig
from django.db.backends.signals import connection_created


def _configure_sqlite(sender, connection, **kwargs):
    """Enable WAL so the web server can read while the scraper worker writes."""
    if connection.vendor != "sqlite":
        return
    with connection.cursor() as cursor:
        cursor.execute("PRAGMA journal_mode=WAL;")
        cursor.execute("PRAGMA synchronous=NORMAL;")
        cursor.execute("PRAGMA foreign_keys=ON;")


class DashboardConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "dashboard"
    verbose_name = "Scraper Dashboard"

    def ready(self):
        connection_created.connect(_configure_sqlite, dispatch_uid="dashboard.sqlite_pragmas")
