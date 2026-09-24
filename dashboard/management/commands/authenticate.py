from django.core.management.base import BaseCommand

from dashboard.models import BrowserSession, ScraperSetting
from scraper import facebook


class Command(BaseCommand):
    help = ("Open Chromium with the persistent profile so you can log in to Facebook manually "
            "(or check the saved session with --check).")

    def add_arguments(self, parser):
        parser.add_argument("--check", action="store_true", help="Only check whether the saved session is logged in.")

    def handle(self, *args, **options):
        cfg = ScraperSetting.get_active().to_config(mode="playwright")
        if options["check"]:
            ok, message = facebook.check_session(cfg)
            BrowserSession.record("AUTHENTICATED" if ok else "NOT_AUTH", message)
            self.stdout.write((self.style.SUCCESS if ok else self.style.WARNING)(message))
            return

        def report(status: str, message: str) -> None:
            BrowserSession.record(status, message)
            self.stdout.write(message)

        self.stdout.write(f"Profile directory: {cfg.profile_dir}")
        ok = facebook.authenticate_interactively(cfg, report=report)
        self.stdout.write(self.style.SUCCESS("Done – session saved.") if ok else self.style.WARNING("Not authenticated."))
