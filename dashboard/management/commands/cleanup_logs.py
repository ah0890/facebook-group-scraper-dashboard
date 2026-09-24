from datetime import timedelta

from django.core.management.base import BaseCommand
from django.utils import timezone

from dashboard.models import ScraperLog, ScraperSetting
from dashboard.services import cleanup_logs


class Command(BaseCommand):
    help = "Delete scraper log entries older than the configured retention period."

    def add_arguments(self, parser):
        parser.add_argument("--days", type=int, help="Override the retention period (days).")
        parser.add_argument("--dry-run", action="store_true", help="Only report how many entries would be deleted.")

    def handle(self, *args, **options):
        days = options["days"] if options["days"] is not None else ScraperSetting.get_active().log_retention_days
        if options["dry_run"]:
            cutoff = timezone.now() - timedelta(days=days)
            count = ScraperLog.objects.filter(timestamp__lt=cutoff).count()
            self.stdout.write(f"{count} log entries are older than {days} days (dry run, nothing deleted).")
            return
        deleted = cleanup_logs(days)
        self.stdout.write(self.style.SUCCESS(f"Deleted {deleted} log entries older than {days} days."))
