import json

from django.contrib.admin.helpers import ACTION_CHECKBOX_NAME
from django.contrib.admin.models import (
    ADDITION,
    CHANGE,
    DELETION,
    LogEntry,
)
from django.contrib.admin.utils import quote
from django.contrib.auth.models import Permission, User
from django.contrib.contenttypes.models import ContentType
from django.test import TestCase, override_settings
from django.urls import reverse

from .models import CompositePKIntModel, CompositePKModel


@override_settings(ROOT_URLCONF="admin_views.urls")
class AdminCompositePKTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.obj = CompositePKModel.objects.create(foo="f,o,o", bar="b/a/r")
        cls.unicode_obj = CompositePKModel.objects.create(foo="héllo", bar="世界")
        cls.empty_obj = CompositePKModel.objects.create(foo="", bar="x")
        cls.int_obj = CompositePKIntModel.objects.create(foo="tenant", num=1)
        cls.ct = ContentType.objects.get_for_model(CompositePKModel)
        cls.int_ct = ContentType.objects.get_for_model(CompositePKIntModel)
        cls.superuser = User.objects.create_superuser(
            username="super", password="secret", email="super@example.com"
        )
        cls.user = User.objects.create_user(
            username="user",
            password="secret",
            email="user@example.com",
            is_staff=True,
        )

    def setUp(self):
        self.client.force_login(self.superuser)

    def url(self, name, obj):
        return reverse(
            f"admin:admin_views_{name}",
            args=(quote(obj.pk),),
        )

    def changelist_url(self, model=CompositePKModel):
        return reverse(f"admin:admin_views_{model._meta.model_name}_changelist")

    # -- Changelist -------------------------------------------------------

    def test_changelist_links_carry_full_composite_key(self):
        response = self.client.get(self.changelist_url())
        self.assertEqual(response.status_code, 200)
        for obj in (self.obj, self.unicode_obj, self.empty_obj):
            change_url = self.url("compositepkmodel_change", obj)
            self.assertContains(response, change_url)
        # The comma in a value is quoted in the href.
        self.assertNotContains(response, "/compositepkmodel/f,o,o,")
        self.assertContains(response, self.url("compositepkmodel_change", self.obj))

    def test_changelist_checkbox_carries_full_composite_key(self):
        response = self.client.get(self.changelist_url())
        self.assertContains(
            response,
            'value="%s"' % quote(self.obj.pk),
        )

    # -- Change view ------------------------------------------------------

    def test_change_view_get(self):
        response = self.client.get(self.url("compositepkmodel_change", self.obj))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'value="f,o,o"', count=1)
        # Delete and history links on the page carry the composite key.
        self.assertContains(response, self.url("compositepkmodel_delete", self.obj))
        self.assertContains(response, self.url("compositepkmodel_history", self.obj))

    def test_change_view_post(self):
        response = self.client.post(
            self.url("compositepkmodel_change", self.obj),
            {"foo": "f,o,o", "bar": "b/a/r", "name": "new name"},
        )
        self.assertRedirects(response, self.changelist_url())
        self.obj.refresh_from_db()
        self.assertEqual(self.obj.name, "new name")
        entry = LogEntry.objects.filter(
            content_type=self.ct, action_flag=CHANGE
        ).latest("id")
        self.assertEqual(entry.object_id, '["f,o,o", "b/a/r"]')
        self.assertEqual(entry.get_edited_object(), self.obj)

    def test_change_view_continue_redirect(self):
        response = self.client.post(
            self.url("compositepkmodel_change", self.obj),
            {
                "foo": "f,o,o",
                "bar": "b/a/r",
                "name": "new name",
                "_continue": "Save and continue",
            },
        )
        self.assertRedirects(
            response, self.url("compositepkmodel_change", self.obj), 302, 200
        )

    # -- Add view ---------------------------------------------------------

    def test_add_view_get(self):
        response = self.client.get(reverse("admin:admin_views_compositepkmodel_add"))
        self.assertEqual(response.status_code, 200)

    def test_add_view_post(self):
        response = self.client.post(
            reverse("admin:admin_views_compositepkmodel_add"),
            {"foo": "new,f", "bar": "n/b"},
        )
        new_obj = CompositePKModel.objects.get(foo="new,f", bar="n/b")
        self.assertRedirects(response, self.changelist_url())
        entry = LogEntry.objects.filter(
            content_type=self.ct, action_flag=ADDITION
        ).latest("id")
        self.assertEqual(entry.object_id, '["new,f", "n/b"]')
        self.assertEqual(entry.get_edited_object(), new_obj)

    def test_add_view_continue_redirect(self):
        response = self.client.post(
            reverse("admin:admin_views_compositepkmodel_add"),
            {"foo": "another", "bar": "b", "_continue": "Save and continue"},
        )
        new_obj = CompositePKModel.objects.get(foo="another")
        self.assertRedirects(
            response,
            self.url("compositepkmodel_change", new_obj),
            302,
            200,
            fetch_redirect_response=False,
        )

    def test_add_view_popup_value_is_quoted(self):
        response = self.client.post(
            reverse("admin:admin_views_compositepkmodel_add"),
            {"foo": "p,o", "bar": "p/o", "_popup": "1"},
        )
        self.assertEqual(response.status_code, 200)
        self.assertJSONEqual(
            response.context["popup_response_data"],
            {"value": quote(("p,o", "p/o")), "obj": "p,o,p/o"},
        )

    def test_change_view_popup_new_value_is_quoted(self):
        response = self.client.post(
            self.url("compositepkmodel_change", self.obj),
            {"foo": "f,o,o", "bar": "b/a/r", "name": "n", "_popup": "1"},
        )
        self.assertEqual(response.status_code, 200)
        data = json.loads(response.context["popup_response_data"])
        self.assertEqual(data["action"], "change")
        self.assertEqual(data["new_value"], quote(self.obj.pk))

    # -- Delete view ------------------------------------------------------

    def test_delete_view_get(self):
        response = self.client.get(self.url("compositepkmodel_delete", self.obj))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, self.url("compositepkmodel_change", self.obj))

    def test_delete_view_post_deletes_only_that_object(self):
        response = self.client.post(
            self.url("compositepkmodel_delete", self.obj), {"post": "yes"}
        )
        self.assertRedirects(response, self.changelist_url())
        self.assertFalse(CompositePKModel.objects.filter(pk=self.obj.pk).exists())
        # Other rows are untouched.
        self.assertTrue(
            CompositePKModel.objects.filter(pk=self.unicode_obj.pk).exists()
        )
        entry = LogEntry.objects.filter(
            content_type=self.ct, action_flag=DELETION
        ).latest("id")
        self.assertEqual(entry.object_id, '["f,o,o", "b/a/r"]')

    def test_delete_selected_action(self):
        response = self.client.post(
            self.changelist_url(),
            {
                "action": "delete_selected",
                ACTION_CHECKBOX_NAME: [quote(self.obj.pk)],
                "index": "0",
            },
        )
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "f,o,o,b/a/r")
        response = self.client.post(
            self.changelist_url(),
            {
                "action": "delete_selected",
                ACTION_CHECKBOX_NAME: [quote(self.obj.pk)],
                "post": "yes",
            },
        )
        self.assertRedirects(response, self.changelist_url())
        self.assertFalse(CompositePKModel.objects.filter(pk=self.obj.pk).exists())
        self.assertTrue(
            CompositePKModel.objects.filter(pk=self.unicode_obj.pk).exists()
        )
        entry = LogEntry.objects.filter(
            content_type=self.ct, action_flag=DELETION
        ).latest("id")
        self.assertEqual(entry.object_id, '["f,o,o", "b/a/r"]')

    def test_delete_selected_with_tampered_value_deletes_nothing(self):
        before = set(CompositePKModel.objects.values_list("pk"))
        response = self.client.post(
            self.changelist_url(),
            {
                "action": "delete_selected",
                ACTION_CHECKBOX_NAME: ["f,o,o,b/a/r"],  # unquoted comma
                "post": "yes",
            },
        )
        self.assertEqual(response.status_code, 302)
        self.assertEqual(set(CompositePKModel.objects.values_list("pk")), before)

    # -- History ----------------------------------------------------------

    def test_history_lists_addition_and_change(self):
        LogEntry.objects.log_actions(
            self.superuser.pk, [self.obj], ADDITION, change_message=[{"added": {}}]
        )
        LogEntry.objects.log_actions(
            self.superuser.pk,
            [self.obj],
            CHANGE,
            change_message=[{"changed": {"fields": ["Name"]}}],
        )
        response = self.client.get(self.url("compositepkmodel_history", self.obj))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Added.")
        self.assertContains(response, "Changed Name.")

    def test_deletion_log_is_linked_to_object(self):
        self.client.post(self.url("compositepkmodel_delete", self.obj), {"post": "yes"})
        entries = LogEntry.objects.filter(
            content_type=self.ct,
            object_id='["f,o,o", "b/a/r"]',
        ).order_by("action_flag")
        self.assertEqual(
            list(entries.values_list("action_flag", flat=True)), [DELETION]
        )

    # -- Invalid object ids ----------------------------------------------

    def assert_does_not_exist_response(self, url):
        before = set(CompositePKModel.objects.values_list("pk"))
        response = self.client.get(url)
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.url, reverse("admin:index"))
        # No rows were read-modified or deleted.
        self.assertEqual(set(CompositePKModel.objects.values_list("pk")), before)
        return response

    def test_missing_part(self):
        self.assert_does_not_exist_response(
            reverse("admin:admin_views_compositepkmodel_change", args=("f_2Co_2Co",))
        )

    def test_extra_part(self):
        self.assert_does_not_exist_response(
            reverse("admin:admin_views_compositepkmodel_change", args=("a,b,c",))
        )

    def test_nonexistent_parts(self):
        self.assert_does_not_exist_response(
            reverse("admin:admin_views_compositepkmodel_change", args=("no,such",))
        )

    def test_invalid_part_conversion(self):
        response = self.client.get(
            reverse(
                "admin:admin_views_compositepkintmodel_change",
                args=("tenant,notanint",),
            )
        )
        self.assertEqual(response.status_code, 302)
        self.assertTrue(CompositePKIntModel.objects.filter(pk=self.int_obj.pk).exists())

    def test_integer_part_is_converted(self):
        url = reverse(
            "admin:admin_views_compositepkintmodel_change",
            args=(quote(self.int_obj.pk),),
        )
        response = self.client.get(url)
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'value="1"')

    def test_tampered_id_post_does_not_write(self):
        before = set(CompositePKModel.objects.values_list("pk"))
        response = self.client.post(
            reverse("admin:admin_views_compositepkmodel_delete", args=("f_2Co_2Co",)),
            {"post": "yes"},
        )
        self.assertEqual(response.status_code, 302)
        self.assertEqual(set(CompositePKModel.objects.values_list("pk")), before)

    def test_change_does_not_match_partial_components(self):
        # "f,o,o" contains a comma, which must be quoted; an unquoted comma in
        # the URL is the part separator, so this points at a different (absent)
        # key rather than self.obj.
        response = self.client.get(
            reverse(
                "admin:admin_views_compositepkmodel_change",
                args=("f,o,o,b/a/r",),
            )
        )
        self.assertEqual(response.status_code, 302)

    def test_at_sign_value_distinct_from_null_marker(self):
        at_obj = CompositePKModel.objects.create(foo="@", bar="z")
        url = self.url("compositepkmodel_change", at_obj)
        # The "@" in the value is quoted; only a bare "@" part marks None.
        self.assertIn("_40", url)
        response = self.client.get(url)
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'value="@"', count=1)

    # -- LogEntry API -----------------------------------------------------

    def test_get_edited_object(self):
        entry = LogEntry.objects.log_actions(
            self.superuser.pk, [self.obj], CHANGE, single_object=True
        )
        self.assertEqual(entry.get_edited_object(), self.obj)

    def test_index_links_recent_action(self):
        LogEntry.objects.log_actions(
            self.superuser.pk, [self.unicode_obj], ADDITION, single_object=True
        )
        response = self.client.get(reverse("admin:index"))
        self.assertEqual(response.status_code, 200)
        self.assertContains(
            response,
            self.url("compositepkmodel_change", self.unicode_obj),
        )

    def test_get_admin_url(self):
        entry = LogEntry.objects.log_actions(
            self.superuser.pk, [self.unicode_obj], CHANGE, single_object=True
        )
        self.assertEqual(
            entry.get_admin_url(),
            self.url("compositepkmodel_change", self.unicode_obj),
        )

    def test_get_admin_url_after_deletion(self):
        entry = LogEntry.objects.log_actions(
            self.superuser.pk, [self.obj], DELETION, single_object=True
        )
        self.assertEqual(
            entry.get_admin_url(),
            self.url("compositepkmodel_change", self.obj),
        )

    # -- Permissions ------------------------------------------------------

    def test_permission_denied_paths(self):
        self.client.force_login(self.user)
        cases = [
            self.changelist_url(),
            reverse("admin:admin_views_compositepkmodel_add"),
            self.url("compositepkmodel_change", self.obj),
            self.url("compositepkmodel_delete", self.obj),
            self.url("compositepkmodel_history", self.obj),
        ]
        for url in cases:
            with self.subTest(url=url):
                self.assertEqual(self.client.get(url).status_code, 403)
        # POSTs are denied as well.
        self.assertEqual(
            self.client.post(
                self.url("compositepkmodel_change", self.obj),
                {"foo": "x", "bar": "y"},
            ).status_code,
            403,
        )
        self.assertEqual(
            self.client.post(
                self.url("compositepkmodel_delete", self.obj), {"post": "yes"}
            ).status_code,
            403,
        )

    def test_view_permission_allows_lookups_but_not_add(self):
        self.user.user_permissions.add(
            Permission.objects.get(codename="view_compositepkmodel")
        )
        self.client.force_login(self.user)
        self.assertEqual(self.client.get(self.changelist_url()).status_code, 200)
        self.assertEqual(
            self.client.get(self.url("compositepkmodel_history", self.obj)).status_code,
            200,
        )
        self.assertEqual(
            self.client.get(
                reverse("admin:admin_views_compositepkmodel_add")
            ).status_code,
            403,
        )
