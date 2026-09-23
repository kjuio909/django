from django.contrib.admin.helpers import ACTION_CHECKBOX_NAME
from django.contrib.admin.models import (
    ADDITION,
    CHANGE,
    DELETION,
    LogEntry,
)
from django.contrib.admin.utils import quote, unquote, unquote_pk
from django.contrib.auth.models import Permission, User
from django.contrib.contenttypes.models import ContentType
from django.test import TestCase, override_settings
from django.urls import reverse

from .models import CompositePKFKModel, CompositePKModel, CompositePKParentModel


@override_settings(ROOT_URLCONF="admin_views.urls")
class CompositePKAdminTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.superuser = User.objects.create_superuser(
            username="super", password="secret", email="super@example.com"
        )
        cls.viewuser = User.objects.create_user(
            username="viewuser", password="secret", is_staff=True
        )
        cls.viewuser.user_permissions.add(
            Permission.objects.get(codename="view_compositepkmodel"),
            Permission.objects.get(codename="view_compositepkfkmodel"),
        )
        cls.obj1 = CompositePKModel.objects.create(name="a/b,c", num=1, text="one")
        cls.obj2 = CompositePKModel.objects.create(name="ünïcode", num=2, text="two")
        cls.obj3 = CompositePKModel.objects.create(name="a/b,c", num=3, text="three")
        cls.empty = CompositePKModel.objects.create(name="", num=4, text="empty")
        cls.parent = CompositePKParentModel.objects.create(name="parent")
        cls.fkobj = CompositePKFKModel.objects.create(
            parent=cls.parent, code="x/y,z", label="fk"
        )

    def setUp(self):
        self.client.force_login(self.superuser)

    # -- quoting helpers ----------------------------------------------------

    def test_quote_roundtrip(self):
        # A literal comma is escaped inside components, so a raw comma can only
        # be the component separator.
        encoded = quote(self.obj1.pk)
        self.assertEqual(encoded, "a_2Fb_2Cc,1")
        self.assertEqual(
            unquote_pk(encoded, CompositePKModel._meta.pk), ["a/b,c", "1"]
        )
        # Unicode is left untouched and reversible.
        self.assertEqual(
            unquote_pk(quote(self.obj2.pk), CompositePKModel._meta.pk),
            ["ünïcode", "2"],
        )

    def test_quote_scalar_unchanged(self):
        self.assertEqual(quote(42), 42)
        self.assertEqual(quote("a/b"), "a_2Fb")
        self.assertEqual(unquote("a_2Fb"), "a/b")

    # -- registration -------------------------------------------------------

    def test_registered(self):
        from admin_views import admin as test_admin

        self.assertIn(CompositePKModel, test_admin.site._registry)

    # -- changelist ---------------------------------------------------------

    def test_changelist_GET(self):
        url = reverse("admin:admin_views_compositepkmodel_changelist")
        response = self.client.get(url)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["cl"].result_count, 4)

    def test_changelist_rows_link_to_change(self):
        url = reverse("admin:admin_views_compositepkmodel_changelist")
        response = self.client.get(url)
        # Same name component on two rows must still produce distinct links.
        self.assertContains(
            response,
            quote(self.obj1.pk) + "/change/",
        )
        self.assertContains(
            response,
            quote(self.obj3.pk) + "/change/",
        )

    # -- add ----------------------------------------------------------------

    def test_add_GET(self):
        response = self.client.get(
            reverse("admin:admin_views_compositepkmodel_add")
        )
        self.assertEqual(response.status_code, 200)

    def test_add_POST_redirects_and_logs(self):
        response = self.client.post(
            reverse("admin:admin_views_compositepkmodel_add"),
            {"name": "new", "num": "4", "text": "four", "_save": "1"},
        )
        new_obj = CompositePKModel.objects.get(name="new", num=4)
        self.assertRedirects(
            response,
            reverse("admin:admin_views_compositepkmodel_changelist"),
        )
        entry = LogEntry.objects.get(
            content_type=ContentType.objects.get_for_model(CompositePKModel),
            action_flag=ADDITION,
        )
        self.assertEqual(
            entry.object_id,
            CompositePKModel._meta.pk.value_to_string(new_obj),
        )
        self.assertEqual(entry.get_edited_object(), new_obj)

    def test_add_POST_continue_redirects_to_change(self):
        response = self.client.post(
            reverse("admin:admin_views_compositepkmodel_add"),
            {"name": "new2", "num": "5", "_continue": "1"},
        )
        self.assertRedirects(
            response,
            reverse(
                "admin:admin_views_compositepkmodel_change",
                args=(quote(("new2", 5)),),
            ),
            fetch_redirect_response=False,
        )

    # -- change -------------------------------------------------------------

    def test_change_GET(self):
        response = self.client.get(
            reverse(
                "admin:admin_views_compositepkmodel_change",
                args=(quote(self.obj1.pk),),
            )
        )
        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, "admin/change_form.html")
        self.assertEqual(response.context["original"], self.obj1)

    def test_change_GET_fk_component(self):
        response = self.client.get(
            reverse(
                "admin:admin_views_compositepkfkmodel_change",
                args=(quote(self.fkobj.pk),),
            )
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["original"], self.fkobj)

    def test_change_GET_locates_only_matching_object(self):
        # Two rows share the "a/b,c" name; only the matching num is loaded.
        response = self.client.get(
            reverse(
                "admin:admin_views_compositepkmodel_change",
                args=(quote(self.obj1.pk),),
            )
        )
        self.assertEqual(response.context["original"].num, 1)

    def test_change_GET_empty_string_component(self):
        # An empty leading component still round-trips unambiguously.
        response = self.client.get(
            reverse(
                "admin:admin_views_compositepkmodel_change",
                args=(quote(self.empty.pk),),
            )
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["original"], self.empty)

    def test_change_POST_redirects_and_logs(self):
        response = self.client.post(
            reverse(
                "admin:admin_views_compositepkmodel_change",
                args=(quote(self.obj1.pk),),
            ),
            {"name": self.obj1.name, "num": str(self.obj1.num), "text": "updated"},
        )
        self.assertEqual(response.status_code, 302)
        self.obj1.refresh_from_db()
        self.assertEqual(self.obj1.text, "updated")
        entry = LogEntry.objects.get(
            content_type=ContentType.objects.get_for_model(CompositePKModel),
            action_flag=CHANGE,
        )
        self.assertEqual(entry.object_id, '["a/b,c", "1"]')

    def test_change_POST_does_not_touch_other_rows(self):
        self.client.post(
            reverse(
                "admin:admin_views_compositepkmodel_change",
                args=(quote(self.obj1.pk),),
            ),
            {"name": self.obj1.name, "num": str(self.obj1.num), "text": "x"},
        )
        self.obj2.refresh_from_db()
        self.assertEqual(self.obj2.text, "two")
        self.obj3.refresh_from_db()
        self.assertEqual(self.obj3.text, "three")

    # -- nonexistent / malformed object_id ----------------------------------

    def _assert_does_not_exist_redirect(self, view, args):
        response = self.client.get(reverse(view, args=args), follow=True)
        self.assertRedirects(response, reverse("admin:index"))
        return [m.message for m in response.context["messages"]]

    def test_change_nonexistent(self):
        messages_ = self._assert_does_not_exist_redirect(
            "admin:admin_views_compositepkmodel_change", (quote(("nope", 9)),)
        )
        self.assertEqual(len(messages_), 1)
        self.assertIn("doesn’t exist", messages_[0])

    def test_change_missing_component(self):
        # The component separator is present in the path but a component was
        # dropped; no object is ever looked up.
        response = self.client.get(
            reverse("admin:admin_views_compositepkmodel_change", args=("abc",)),
            follow=True,
        )
        self.assertRedirects(response, reverse("admin:index"))
        self.assertEqual(CompositePKModel.objects.count(), 4)

    def test_change_extra_component(self):
        response = self.client.get(
            reverse(
                "admin:admin_views_compositepkmodel_change",
                args=("a,1,2",),
            ),
            follow=True,
        )
        self.assertRedirects(response, reverse("admin:index"))

    def test_change_invalid_component_type(self):
        response = self.client.get(
            reverse(
                "admin:admin_views_compositepkmodel_change",
                args=("name,not-an-int",),
            ),
            follow=True,
        )
        self.assertRedirects(response, reverse("admin:index"))

    def test_delete_nonexistent(self):
        response = self.client.get(
            reverse(
                "admin:admin_views_compositepkmodel_delete",
                args=(quote(("nope", 9)),),
            ),
            follow=True,
        )
        self.assertRedirects(response, reverse("admin:index"))

    def test_delete_POST_with_tampered_key_deletes_nothing(self):
        response = self.client.post(
            reverse(
                "admin:admin_views_compositepkmodel_delete",
                args=("a_2Fb_2Cc,bogus",),
            ),
            {"post": "yes"},
            follow=True,
        )
        self.assertRedirects(response, reverse("admin:index"))
        # No row matched the malformed key; other objects are untouched.
        self.assertEqual(CompositePKModel.objects.count(), 4)
        self.assertTrue(CompositePKModel.objects.filter(pk=self.obj1.pk).exists())
        self.assertTrue(CompositePKModel.objects.filter(pk=self.obj3.pk).exists())

    def test_history_nonexistent(self):
        response = self.client.get(
            reverse(
                "admin:admin_views_compositepkmodel_history",
                args=(quote(("nope", 9)),),
            ),
            follow=True,
        )
        self.assertRedirects(response, reverse("admin:index"))

    # -- delete -------------------------------------------------------------

    def test_delete_GET(self):
        response = self.client.get(
            reverse(
                "admin:admin_views_compositepkmodel_delete",
                args=(quote(self.obj2.pk),),
            )
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["object"], self.obj2)

    def test_delete_POST_deletes_only_matching_object(self):
        response = self.client.post(
            reverse(
                "admin:admin_views_compositepkmodel_delete",
                args=(quote(self.obj1.pk),),
            ),
            {"post": "yes"},
        )
        self.assertEqual(response.status_code, 302)
        self.assertFalse(CompositePKModel.objects.filter(pk=self.obj1.pk).exists())
        # The other row sharing the same name component is untouched.
        self.assertTrue(CompositePKModel.objects.filter(pk=self.obj3.pk).exists())

    def test_delete_POST_writes_log(self):
        self.client.post(
            reverse(
                "admin:admin_views_compositepkmodel_delete",
                args=(quote(self.obj1.pk),),
            ),
            {"post": "yes"},
        )
        entry = LogEntry.objects.get(action_flag=DELETION)
        self.assertEqual(entry.object_id, '["a/b,c", "1"]')
        # The URL is reversed from the stored key without a database lookup,
        # matching the behavior for single-field primary keys.
        self.assertEqual(
            entry.get_admin_url(),
            reverse(
                "admin:admin_views_compositepkmodel_change",
                args=(quote(self.obj1.pk),),
            ),
        )

    # -- history ------------------------------------------------------------

    def test_history_lists_this_objects_entries(self):
        from django.contrib.admin.models import LogEntry

        ct = ContentType.objects.get_for_model(CompositePKModel)
        # Log a different object with an overlapping component to make sure the
        # filter is fully qualified.
        LogEntry.objects.log_actions(
            self.superuser.pk, [self.obj3], CHANGE, "[]"
        )
        LogEntry.objects.log_actions(
            self.superuser.pk, [self.obj1], ADDITION, "[]"
        )
        LogEntry.objects.log_actions(
            self.superuser.pk, [self.obj1], CHANGE, "[]"
        )
        response = self.client.get(
            reverse(
                "admin:admin_views_compositepkmodel_history",
                args=(quote(self.obj1.pk),),
            )
        )
        self.assertEqual(
            list(response.context["action_list"].object_list),
            list(
                LogEntry.objects.filter(
                    content_type=ct, object_id='["a/b,c", "1"]'
                ).order_by("action_time")
            ),
        )

    def test_history_link_in_change_form(self):
        response = self.client.get(
            reverse(
                "admin:admin_views_compositepkmodel_change",
                args=(quote(self.obj1.pk),),
            )
        )
        self.assertContains(
            response,
            reverse(
                "admin:admin_views_compositepkmodel_history",
                args=(quote(self.obj1.pk),),
            ),
        )
        # The delete link also carries the complete composite key.
        self.assertContains(
            response,
            reverse(
                "admin:admin_views_compositepkmodel_delete",
                args=(quote(self.obj1.pk),),
            ),
        )

    # -- LogEntry API -------------------------------------------------------

    def test_log_entry_get_admin_url_for_existing_object(self):
        entry = LogEntry.objects.log_actions(
            self.superuser.pk, [self.obj1], CHANGE, "[]", single_object=True
        )
        self.assertEqual(
            entry.get_admin_url(),
            reverse(
                "admin:admin_views_compositepkmodel_change",
                args=(quote(self.obj1.pk),),
            ),
        )

    def test_log_entry_format_is_stable_and_reversible(self):
        entry = LogEntry.objects.log_actions(
            self.superuser.pk, [self.obj1], CHANGE, "[]", single_object=True
        )
        # JSON array of component string representations.
        self.assertEqual(entry.object_id, '["a/b,c", "1"]')
        # to_python reverses it back to typed component values.
        self.assertEqual(
            tuple(CompositePKModel._meta.pk.to_python(entry.object_id)),
            self.obj1.pk,
        )

    # -- permissions --------------------------------------------------------

    def test_changelist_requires_view_permission(self):
        self.client.force_login(self.viewuser)
        # The view user has view permission on this model, so the changelist is
        # visible but no add button is rendered.
        response = self.client.get(
            reverse("admin:admin_views_compositepkmodel_changelist")
        )
        self.assertEqual(response.status_code, 200)

    def test_add_forbidden_without_add_permission(self):
        user = User.objects.create_user(
            username="noperm", password="secret", is_staff=True
        )
        self.client.force_login(user)
        response = self.client.get(
            reverse("admin:admin_views_compositepkmodel_add")
        )
        # A staff user without the add permission is denied.
        self.assertEqual(response.status_code, 403)

    def test_change_POST_forbidden_without_change_permission(self):
        self.client.force_login(self.viewuser)
        response = self.client.post(
            reverse(
                "admin:admin_views_compositepkmodel_change",
                args=(quote(self.obj1.pk),),
            ),
            {"name": self.obj1.name, "num": str(self.obj1.num)},
        )
        self.assertEqual(response.status_code, 403)

    def test_delete_forbidden_without_delete_permission(self):
        self.client.force_login(self.viewuser)
        response = self.client.post(
            reverse(
                "admin:admin_views_compositepkmodel_delete",
                args=(quote(self.obj1.pk),),
            ),
            {"post": "yes"},
        )
        self.assertEqual(response.status_code, 403)
        self.assertTrue(CompositePKModel.objects.filter(pk=self.obj1.pk).exists())

    # -- changelist actions -------------------------------------------------

    def test_delete_selected_action(self):
        response = self.client.post(
            reverse("admin:admin_views_compositepkmodel_changelist"),
            {
                "action": "delete_selected",
                ACTION_CHECKBOX_NAME: [quote(self.obj1.pk), quote(self.obj2.pk)],
                "index": "1",
            },
        )
        # Confirmation page.
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Are you sure")
        response = self.client.post(
            reverse("admin:admin_views_compositepkmodel_changelist"),
            {
                "action": "delete_selected",
                ACTION_CHECKBOX_NAME: [quote(self.obj1.pk), quote(self.obj2.pk)],
                "post": "yes",
            },
        )
        self.assertEqual(response.status_code, 302)
        self.assertEqual(CompositePKModel.objects.count(), 2)
        self.assertTrue(CompositePKModel.objects.filter(pk=self.obj3.pk).exists())
        self.assertTrue(CompositePKModel.objects.filter(pk=self.empty.pk).exists())

    def test_delete_selected_action_ignores_tampered_values(self):
        self.client.post(
            reverse("admin:admin_views_compositepkmodel_changelist"),
            {
                "action": "delete_selected",
                ACTION_CHECKBOX_NAME: [quote(self.obj1.pk), "bogus,1"],
                "post": "yes",
            },
        )
        # Only the genuinely selected row is deleted; the tampered value
        # matches nothing and cannot affect another row.
        self.assertFalse(CompositePKModel.objects.filter(pk=self.obj1.pk).exists())
        self.assertTrue(CompositePKModel.objects.filter(pk=self.obj2.pk).exists())
        self.assertTrue(CompositePKModel.objects.filter(pk=self.obj3.pk).exists())

    def test_change_form_action_resolves_full_key(self):
        # An action submitted from the change form receives a queryset
        # containing only the object identified by the complete composite key.
        response = self.client.post(
            reverse(
                "admin:admin_views_compositepkmodel_change",
                args=(quote(self.obj1.pk),),
            ),
            {
                "CHANGE_FORM-action": "show_composite_key",
                ACTION_CHECKBOX_NAME: [str(quote(self.obj1.pk))],
                "index": "0",
            },
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.content.decode(), str(self.obj1.pk))
