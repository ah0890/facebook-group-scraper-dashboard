from pathlib import Path

from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone

from dashboard.exports import posts_to_json, write_csv
from dashboard.models import Post, ScraperSetting


class Command(BaseCommand):
    help = "Export collected posts to CSV or JSON in the export directory."

    def add_arguments(self, parser):
        parser.add_argument("--format", choices=["csv", "json"], default="csv")
        parser.add_argument("--group", type=int, help="Only export posts from this group ID.")
        parser.add_argument("--output", help="Output file path (default: <export dir>/facebook_posts_<timestamp>.<fmt>).")

    def handle(self, *args, **options):
        posts = Post.objects.select_related("group").order_by("group__name", "-collected_at")
        if options["group"]:
            posts = posts.filter(group_id=options["group"])

        fmt = options["format"]
        if options["output"]:
            target = Path(options["output"])
        else:
            export_dir = ScraperSetting.get_active().to_config().export_dir
            target = export_dir / f"facebook_posts_{timezone.localtime():%Y%m%d_%H%M%S}.{fmt}"
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            if fmt == "csv":
                with target.open("w", encoding="utf-8-sig", newline="") as fh:
                    count = write_csv(posts.iterator(chunk_size=500), fh)
            else:
                count = posts.count()
                target.write_text(posts_to_json(posts.iterator(chunk_size=500)), encoding="utf-8")
        except OSError as exc:
            raise CommandError(f"Could not write {target}: {exc}") from exc
        self.stdout.write(self.style.SUCCESS(f"Exported {count} posts to {target}"))
