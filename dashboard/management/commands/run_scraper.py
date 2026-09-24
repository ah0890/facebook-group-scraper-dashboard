import threading

from django.core.management.base import BaseCommand, CommandError

from dashboard import services
from scraper.runner import execute_run

STYLE_BY_LEVEL = {"SUCCESS": "SUCCESS", "WARNING": "WARNING", "ERROR": "ERROR"}


class Command(BaseCommand):
    help = "Run the scraper once from the command line (the next batch of groups, or specific groups)."

    def add_arguments(self, parser):
        parser.add_argument("--mode", choices=["mock", "playwright"], help="Override the configured scraper mode.")
        parser.add_argument("--groups", help="Comma-separated group IDs to scrape instead of the next batch.")
        parser.add_argument("--quiet", action="store_true", help="Only print the final summary.")

    def handle(self, *args, **options):
        group_ids = None
        if options["groups"]:
            try:
                group_ids = [int(x) for x in options["groups"].split(",") if x.strip()]
            except ValueError as exc:
                raise CommandError("--groups must be a comma-separated list of numeric IDs") from exc

        try:
            run = services.queue_run(group_ids=group_ids, trigger="cli", mode=options["mode"])
        except (services.RunAlreadyActive, services.NoGroupsToScrape) as exc:
            raise CommandError(str(exc)) from exc

        self.stdout.write(f"Queued run #{run.pk} ({run.mode}) with {run.total_groups} group(s). Press Ctrl+C to stop.")
        echo = None if options["quiet"] else self._echo
        stop_event = threading.Event()
        result = {}

        def work():
            from django.db import connections

            try:
                result["run"] = execute_run(run.pk, stop_event=stop_event, echo=echo)
            finally:
                connections.close_all()

        worker = threading.Thread(target=work, name=f"cli-run-{run.pk}")
        worker.start()
        try:
            while worker.is_alive():
                worker.join(0.5)
        except KeyboardInterrupt:
            self.stdout.write(self.style.WARNING("\nStop requested – saving progress…"))
            stop_event.set()
            worker.join()

        final = result.get("run")
        if final is None:
            raise CommandError(f"Run #{run.pk} ended unexpectedly – check the logs.")
        self.stdout.write(
            f"Run #{final.pk}: {final.status} – {final.completed_groups}/{final.total_groups} groups, "
            f"{final.total_posts} new posts"
        )

    def _echo(self, level: str, message: str) -> None:
        style = getattr(self.style, STYLE_BY_LEVEL.get(level, ""), None)
        self.stdout.write(style(message) if style else message)
