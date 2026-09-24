from datetime import timedelta
from io import StringIO

from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from dashboard.forms import ScraperSettingForm
from dashboard.models import ScraperLog, ScraperRun, ScraperSetting
from dashboard.services import reset_settings_to_defaults
from scraper.logger import RunLogger


class LoggerTests(TestCase):
    def setUp(self):
        self.run = ScraperRun.objects.create()

    def test_log_creation_with_phase_and_levels(self):
        log = RunLogger(self.run, "INFO")
        log.set_phase("FETCHING")
        log.info("hello")
        log.success("+ [A] Post collected")
        log.warning("careful")
        log.error("bad")
        log.debug("hidden at INFO level")
        entries = list(ScraperLog.objects.filter(run=self.run).values_list("level", "phase", "message"))
        self.assertEqual(entries, [
            ("INFO", "FETCHING", "hello"),
            ("SUCCESS", "FETCHING", "+ [A] Post collected"),
            ("WARNING", "FETCHING", "careful"),
            ("ERROR", "FETCHING", "bad"),
        ])
        self.run.refresh_from_db()
        self.assertEqual(self.run.current_phase, "FETCHING")

    def test_debug_level_keeps_debug_logs(self):
        RunLogger(self.run, "DEBUG").debug("visible")
        self.assertTrue(ScraperLog.objects.filter(level="DEBUG").exists())

    def test_echo_callback(self):
        lines = []
        RunLogger(self.run, "INFO", echo=lambda level, msg: lines.append((level, msg))).success("done")
        self.assertEqual(lines, [("SUCCESS", "done")])


class LogCleanupTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user("admin", password="pass-12345-word")
        self.client.force_login(self.user)
        old = ScraperLog.objects.create(message="old")
        ScraperLog.objects.filter(pk=old.pk).update(timestamp=timezone.now() - timedelta(days=40))
        ScraperLog.objects.create(message="new")

    def test_clear_requires_confirmation(self):
        response = self.client.post(reverse("logs_clear"), {"days": 30})
        self.assertRedirects(response, reverse("logs"))
        self.assertEqual(ScraperLog.objects.count(), 2)

    def test_clear_old_logs(self):
        self.client.post(reverse("logs_clear"), {"days": 30, "confirm": "on"})
        self.assertEqual(list(ScraperLog.objects.values_list("message", flat=True)), ["new"])

    def test_cleanup_command(self):
        out = StringIO()
        call_command("cleanup_logs", "--days", "30", stdout=out)
        self.assertIn("Deleted 1", out.getvalue())
        self.assertEqual(ScraperLog.objects.count(), 1)

    def test_logs_page_filters(self):
        response = self.client.get(reverse("logs"), {"q": "new"})
        self.assertEqual(response.context["total"], 1)


class SettingsTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user("admin", password="pass-12345-word")
        self.client.force_login(self.user)

    def test_factory_defaults(self):
        setting = ScraperSetting.get_active()
        self.assertEqual((setting.posts_per_group, setting.groups_per_run, setting.scroll_limit,
                          setting.scroll_delay, setting.retry_count, setting.headless), (20, 10, 20, 2.0, 2, False))
        self.assertEqual(setting.scraper_mode, "mock")

    def _form_data(self, **overrides):
        data = {name: getattr(ScraperSetting.get_active(), name) for name in ScraperSetting.CONFIG_FIELDS}
        data = {k: v for k, v in data.items() if v is not False}
        data.update(overrides)
        return data

    def test_settings_form_validation(self):
        form = ScraperSettingForm(data=self._form_data(scroll_delay=0.1, posts_per_group=0),
                                  instance=ScraperSetting.get_active())
        self.assertFalse(form.is_valid())
        self.assertIn("scroll_delay", form.errors)
        self.assertIn("posts_per_group", form.errors)

    def test_save_settings_page(self):
        response = self.client.post(reverse("settings"), self._form_data(posts_per_group=35))
        self.assertRedirects(response, reverse("settings"))
        self.assertEqual(ScraperSetting.get_active().posts_per_group, 35)

    def test_defaults_save_reset_and_apply(self):
        self.client.post(reverse("defaults"), self._form_data(posts_per_group=50, groups_per_run=3))
        defaults = ScraperSetting.get_defaults()
        self.assertEqual((defaults.posts_per_group, defaults.groups_per_run), (50, 3))

        # "Reset to Defaults" copies saved defaults into the active settings.
        self.client.post(reverse("run_reset_settings"))
        self.assertEqual(ScraperSetting.get_active().posts_per_group, 50)

        # "Reset Defaults" restores the factory values.
        self.client.post(reverse("defaults"), {"action": "reset"})
        self.assertEqual(ScraperSetting.get_defaults().posts_per_group, 20)

    def test_to_config_resolves_relative_paths(self):
        cfg = ScraperSetting.get_active().to_config()
        self.assertTrue(cfg.profile_dir.is_absolute())
        self.assertTrue(cfg.export_dir.is_absolute())
        self.assertEqual(cfg.page_timeout_ms, 45_000)

    def test_reset_settings_service(self):
        active = ScraperSetting.get_active()
        active.posts_per_group = 99
        active.save()
        reset_settings_to_defaults()
        self.assertEqual(ScraperSetting.get_active().posts_per_group, 20)
