from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.test import TestCase
from django.urls import reverse

from dashboard.forms import GroupForm
from dashboard.models import Group, GroupStatus
from dashboard.validators import normalize_group_url

from .helpers import make_group


class GroupUrlValidationTests(TestCase):
    def test_normalizes_variants_to_canonical_url(self):
        cases = {
            "https://www.facebook.com/groups/pythonpk": "https://www.facebook.com/groups/pythonpk/",
            "http://m.facebook.com/groups/123456789/?ref=share": "https://www.facebook.com/groups/123456789/",
            "facebook.com/groups/some.group-name/posts/1/": "https://www.facebook.com/groups/some.group-name/",
            "https://web.facebook.com/groups/abc_def#top": "https://www.facebook.com/groups/abc_def/",
        }
        for raw, expected in cases.items():
            with self.subTest(raw=raw):
                self.assertEqual(normalize_group_url(raw), expected)

    def test_rejects_non_group_urls(self):
        for bad in [
            "",
            "https://example.com/groups/abc/",
            "https://www.facebook.com/profile.php?id=1",
            "https://www.facebook.com/groups/",
            "https://www.facebook.com/groups/feed/",
            "ftp://www.facebook.com/groups/abc/",
            "https://facebook.com.evil.com/groups/abc/",
            "javascript:alert(1)",
        ]:
            with self.subTest(url=bad):
                with self.assertRaises(ValidationError):
                    normalize_group_url(bad)


class GroupModelTests(TestCase):
    def test_create_group_defaults(self):
        group = make_group("Lahore Families", slug="lahore-families")
        self.assertTrue(group.enabled)
        self.assertEqual(group.last_status, GroupStatus.NEVER)
        self.assertEqual(group.total_posts, 0)
        self.assertEqual(group.facebook_url, "https://www.facebook.com/groups/lahore-families/")

    def test_save_normalizes_url(self):
        group = Group.objects.create(name="X", facebook_url="https://m.facebook.com/groups/xyz?ref=1")
        self.assertEqual(group.facebook_url, "https://www.facebook.com/groups/xyz/")

    def test_form_rejects_duplicate_url(self):
        make_group("One", slug="dup-group")
        form = GroupForm(data={"name": "Two", "facebook_url": "https://m.facebook.com/groups/dup-group", "enabled": True})
        self.assertFalse(form.is_valid())
        self.assertIn("facebook_url", form.errors)

    def test_form_rejects_invalid_url(self):
        form = GroupForm(data={"name": "Bad", "facebook_url": "https://example.com/groups/x/", "enabled": True})
        self.assertFalse(form.is_valid())


class GroupViewTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user("admin", password="pass-12345-word")
        self.client.force_login(self.user)

    def test_add_group_via_page(self):
        response = self.client.post(reverse("groups"), {
            "name": "  Python   Devs ", "facebook_url": "facebook.com/groups/pydevs", "enabled": "on",
        })
        self.assertRedirects(response, reverse("groups"))
        group = Group.objects.get()
        self.assertEqual(group.name, "Python Devs")
        self.assertEqual(group.facebook_url, "https://www.facebook.com/groups/pydevs/")

    def test_new_group_joins_current_round(self):
        make_group("Old", slug="old", completed_round=4)
        self.client.post(reverse("groups"), {"name": "New", "facebook_url": "https://www.facebook.com/groups/new/",
                                             "enabled": "on"})
        self.assertEqual(Group.objects.get(name="New").completed_round, 4)

    def test_toggle_and_delete(self):
        group = make_group()
        self.client.post(reverse("group_toggle", args=[group.pk]))
        group.refresh_from_db()
        self.assertFalse(group.enabled)
        self.client.post(reverse("group_delete", args=[group.pk]))
        self.assertFalse(Group.objects.exists())

    def test_edit_group(self):
        group = make_group()
        response = self.client.post(reverse("group_edit", args=[group.pk]), {
            "name": "Renamed", "facebook_url": group.facebook_url, "enabled": "on", "notes": "hello",
        })
        self.assertRedirects(response, reverse("groups"))
        group.refresh_from_db()
        self.assertEqual((group.name, group.notes), ("Renamed", "hello"))

    def test_delete_requires_post(self):
        group = make_group()
        self.assertEqual(self.client.get(reverse("group_delete", args=[group.pk])).status_code, 405)
        self.assertTrue(Group.objects.filter(pk=group.pk).exists())

    def test_url_test_in_mock_mode(self):
        group = make_group(slug="demo-broken-thing")
        response = self.client.post(reverse("group_test", args=[group.pk]), HTTP_ACCEPT="application/json")
        data = response.json()
        self.assertEqual(data["status"], "ERROR")
        group.refresh_from_db()
        self.assertEqual(group.url_check_status, "ERROR")
