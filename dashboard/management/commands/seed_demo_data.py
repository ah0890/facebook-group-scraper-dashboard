import random
from datetime import timedelta

from django.core.management.base import BaseCommand
from django.db import transaction
from django.utils import timezone

from dashboard.models import (
    Group,
    GroupStatus,
    LogLevel,
    Phase,
    Post,
    RunGroup,
    RunStatus,
    ScraperLog,
    ScraperRun,
    ScraperSetting,
)
from scraper.mock import generate_feed
from scraper.parser import build_dedup_key, parse_raw_post

# (name, slug, enabled). Slugs containing "flaky"/"broken"/"private" trigger the mock
# client's simulated failures, so the demo shows retries and failed groups too.
DEMO_GROUPS = [
    ("Bahria Town Lahore Families", "demo-bahria-town-lahore-families", True),
    ("DHA Karachi Community", "demo-dha-karachi-community", True),
    ("Islamabad Tech Jobs", "demo-islamabad-tech-jobs", True),
    ("Python Developers Pakistan", "demo-python-developers-pakistan", True),
    ("Home Chefs Lahore", "demo-home-chefs-lahore", True),
    ("Freelancers Hub Pakistan", "demo-freelancers-hub-pakistan", True),
    ("Rawalpindi Rentals & Property", "demo-rawalpindi-rentals-property", True),
    ("Used Cars Lahore", "demo-used-cars-lahore-flaky", True),
    ("Old Classifieds Archive", "demo-old-classifieds-broken", False),
    ("Private Book Club", "demo-private-book-club", False),
]
DEMO_PREFIX = "https://www.facebook.com/groups/demo-"


class Command(BaseCommand):
    help = "Create demo groups (and optionally a few weeks of simulated run history) for mock mode."

    def add_arguments(self, parser):
        parser.add_argument("--reset", action="store_true", help="Delete existing demo groups, posts and demo runs first.")
        parser.add_argument("--no-history", action="store_true", help="Only create groups, no historical runs/posts.")
        parser.add_argument("--days", type=int, default=21, help="Days of simulated history (default 21).")

    def handle(self, *args, **options):
        ScraperSetting.get_active()
        ScraperSetting.get_defaults()

        if options["reset"]:
            deleted_runs = ScraperRun.objects.filter(trigger="demo").delete()[0]
            deleted = Group.objects.filter(facebook_url__startswith=DEMO_PREFIX).delete()[0]
            self.stdout.write(f"Removed {deleted} demo group rows/posts and {deleted_runs} demo run rows.")

        created = 0
        for name, slug, enabled in DEMO_GROUPS:
            _, was_created = Group.objects.get_or_create(
                facebook_url=f"https://www.facebook.com/groups/{slug}/",
                defaults={"name": name, "enabled": enabled, "notes": "Demo group (mock mode)"},
            )
            created += was_created
        self.stdout.write(self.style.SUCCESS(f"Demo groups ready ({created} created)."))

        if options["no_history"]:
            return
        if ScraperRun.objects.filter(trigger="demo").exists():
            self.stdout.write("Demo history already exists – use --reset to regenerate it.")
            return
        runs, posts = self._create_history(max(1, options["days"]))
        self.stdout.write(self.style.SUCCESS(f"Created {runs} historical runs with {posts} posts."))
        self.stdout.write("Next: python manage.py runserver, then open Run Scraper and press Start.")

    @transaction.atomic
    def _create_history(self, days: int) -> tuple[int, int]:
        rng = random.Random(42)
        groups = list(Group.objects.filter(facebook_url__startswith=DEMO_PREFIX, enabled=True).order_by("id"))
        if not groups:
            return 0, 0
        now = timezone.now()
        run_count = post_count = 0
        rounds = 0
        cursor = 0  # rotate through groups like real batches do

        for day in range(days, 0, -1):
            for _ in range(rng.choice([1, 1, 2])):
                started = (now - timedelta(days=day)).replace(hour=rng.randint(7, 21), minute=rng.randint(0, 59))
                batch = [groups[(cursor + i) % len(groups)] for i in range(rng.randint(3, 5))]
                cursor += len(batch)
                if cursor >= len(groups):
                    rounds += 1
                    cursor %= len(groups)
                roll = rng.random()
                status = RunStatus.FAILED if roll < 0.07 else RunStatus.PARTIAL if roll < 0.2 else RunStatus.COMPLETED

                run = ScraperRun.objects.create(
                    started_at=started, status=status, mode="mock", trigger="demo", round_number=rounds + 1,
                    total_groups=len(batch),
                    current_phase=Phase.FAILED if status == RunStatus.FAILED else Phase.COMPLETED,
                )
                elapsed = timedelta()
                completed = failed = new_posts = 0
                for position, group in enumerate(batch, start=1):
                    ok = status == RunStatus.COMPLETED or (status == RunStatus.PARTIAL and position != len(batch))
                    group_time = started + elapsed
                    elapsed += timedelta(seconds=rng.randint(25, 90))
                    saved = 0
                    if ok:
                        feed = generate_feed(group.facebook_url, now=group_time)[: rng.randint(4, 14)]
                        new = []
                        for raw in feed:
                            post = parse_raw_post(raw, now=group_time)
                            if post is None:
                                continue
                            new.append(Post(
                                group=group, run=run, dedup_key=build_dedup_key(group.pk, post),
                                facebook_post_id=post.facebook_post_id, author_name=post.author_name,
                                author_url=post.author_url, post_text=post.post_text, post_url=post.post_url,
                                post_timestamp=post.post_timestamp, timestamp_text=post.timestamp_text,
                                media_url=post.media_url, likes_count=post.likes_count,
                                comments_count=post.comments_count, shares_count=post.shares_count,
                                raw_data=post.raw_data, collected_at=group_time,
                            ))
                        before = Post.objects.filter(group=group).count()
                        Post.objects.bulk_create(new, ignore_conflicts=True)
                        saved = Post.objects.filter(group=group).count() - before
                        completed += 1
                        new_posts += saved
                    else:
                        failed += 1
                    RunGroup.objects.create(
                        run=run, group=group, position=position,
                        status=GroupStatus.COMPLETED if ok else GroupStatus.FAILED,
                        posts_collected=saved, attempts=1 if ok else 3, started_at=group_time,
                        finished_at=started + elapsed,
                        error_message="" if ok else "Timed out while loading the group page (simulated).",
                    )
                    Group.objects.filter(pk=group.pk).update(
                        last_run_at=started + elapsed,
                        last_status=GroupStatus.COMPLETED if ok else GroupStatus.FAILED,
                    )

                run.finished_at = started + elapsed
                run.completed_groups, run.failed_groups, run.total_posts = completed, failed, new_posts
                if status == RunStatus.FAILED:
                    run.error_message = "Could not launch Chromium (simulated failure for demo history)."
                elif failed:
                    run.error_message = f"{failed} group(s) failed – see logs for details."
                run.save()
                ScraperLog.objects.bulk_create([
                    ScraperLog(run=run, timestamp=started, phase=Phase.SEEDING, message="Facebook Group Fetcher"),
                    ScraperLog(run=run, timestamp=started, phase=Phase.SEEDING,
                               message=f"Groups   : {len(batch)} queued this run"),
                    ScraperLog(run=run, timestamp=run.finished_at, phase=run.current_phase,
                               level=LogLevel.SUCCESS if status == RunStatus.COMPLETED else LogLevel.WARNING,
                               message=f"Run #{run.pk} {status} | new posts: {new_posts} (demo history)"),
                ])
                run_count += 1
                post_count += new_posts

        # `rounds` full passes are done; groups before `cursor` also finished the current (partial) round.
        # This leaves the round part-way done so the Run Scraper page shows a realistic "next batch".
        for index, group in enumerate(groups):
            group.completed_round = rounds + 1 if index < cursor else rounds
            group.total_posts = Post.objects.filter(group=group).count()
            group.save(update_fields=["completed_round", "total_posts"])
        return run_count, post_count
