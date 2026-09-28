"""
Many-to-many relations to models with a composite primary key must work
through the admin site: registering the models with
``django.contrib.admin.site`` and editing the relation from the add and
change pages must render the current relation, bind it to exactly the
existing targets, save atomically and report a stable ``invalid_choice``
error for any value that can't identify one.

The only entry point exercised here is ``django.contrib.admin.site``;
everything else uses the public ORM.
"""

import json

from django.contrib import admin
from django.contrib.admin.sites import site as admin_site
from django.contrib.admin.utils import quote
from django.contrib.auth.models import User
from django.test import TestCase, override_settings
from django.urls import path, reverse
from django.utils.html import escape

from .models import Book, Bookmark, Shelf, Tag, Tenant

# ISBN values chosen so that each one must survive encoding as a distinct
# choice value and round-trip back to the same object.
SPECIAL_ISBNS = [
    "42",  # digits stay a string component of a composite key
    "",  # empty string
    "None",  # the literal text "None"
    "@",  # the literal token used elsewhere to mark null
    "\\",  # backslash
    "/",  # slash
    ",",  # comma (a natural separator)
    '"',  # double quote
    "'",  # single quote
    "héllo世界",  # unicode
]

INVALID_VALUES = [
    '["999", "missing"]',  # nonexistent key
    '["1", ",tampered"]',  # tampered component
    "garbled",  # not JSON at all
    "1,",  # superficially comma-shaped but undecodable
    "null",  # JSON null instead of an array
    "42",  # JSON scalar instead of an array
    "{}",  # JSON object instead of an array
    "[1]",  # too few components
    "[1, 2, 3]",  # too many components
    '["not-an-int", "x"]',  # first component can't become an int
    "[1, 2]",  # second component has the wrong type
    '[1e999, "x"]',  # component overflows an integer
    '[1, ["x"]]',  # non-scalar component
    "[1, null]",  # empty (null) component
    "[]",  # empty payload
    "",  # empty string
]


class BookAdmin(admin.ModelAdmin):
    search_fields = ("isbn", "title")
    ordering = ("tenant_id", "isbn")


class ShelfAdmin(admin.ModelAdmin):
    pass


class BookmarkAdmin(admin.ModelAdmin):
    pass


admin_site.register(Book, BookAdmin)
admin_site.register(Tag, admin.ModelAdmin)
admin_site.register(Tenant, admin.ModelAdmin)
admin_site.register(Shelf, ShelfAdmin)
admin_site.register(Bookmark, BookmarkAdmin)

urlpatterns = [path("admin/", admin_site.urls)]


@override_settings(ROOT_URLCONF="composite_pk.test_admin")
class CompositePKM2MAdminBase(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.superuser = User.objects.create_superuser(
            "root", "root@example.com", "password"
        )
        cls.tenant_1 = Tenant.objects.create(id=1, name="tenant 1")
        cls.tenant_2 = Tenant.objects.create(id=2, name="tenant 2")
        cls.books = {}
        for isbn in SPECIAL_ISBNS:
            cls.books[isbn] = Book.objects.create(
                tenant=cls.tenant_1, isbn=isbn, title=isbn
            )
        # A second target that shares the second component with an existing
        # one but differs in the first.
        cls.book_comma_1 = cls.books[","]
        cls.book_comma_2 = Book.objects.create(
            tenant=cls.tenant_2, isbn=",", title=","
        )
        cls.tag = Tag.objects.create(name="tag")

    def setUp(self):
        self.client.force_login(self.superuser)

    def encoded(self, *books):
        return [Book._meta.pk.value_to_string(book.pk) for book in books]

    def change_url(self, obj):
        return reverse("admin:composite_pk_shelf_change", args=[obj.pk])

    def make_shelf(self, *books, tags=()):
        shelf = Shelf.objects.create(name="shelf")
        if books:
            shelf.books.add(*books)
        if tags:
            shelf.tags.add(*tags)
        return shelf

    def save_data(self, *books, name="shelf", tags=()):
        return {
            "name": name,
            "books": self.encoded(*books),
            "tags": [str(tag.pk) for tag in tags],
            "_save": "Save",
        }

    def error_codes(self, response, field="books"):
        form = response.context["adminform"].form
        return {
            error.code
            for batch in form.errors.as_data()[field]
            for error in batch.error_list
        }


class AddChangePageTests(CompositePKM2MAdminBase):
    def test_choices_carry_full_key_in_declaration_order(self):
        response = self.client.get(reverse("admin:composite_pk_shelf_add"))
        self.assertEqual(response.status_code, 200)
        form = response.context["adminform"].form
        choice_values = [str(value) for value, _ in form.fields["books"].choices]
        self.assertCountEqual(
            choice_values,
            self.encoded(*Book.objects.order_by("tenant_id", "isbn")),
        )
        # Declaration order: tenant_id first, isbn second.
        decoded = {tuple(part) for part in map(json.loads, choice_values)}
        self.assertIn(("1", ""), decoded)
        self.assertIn(("2", ","), decoded)
        self.assertNotIn((",", "2"), decoded)

    def test_special_values_are_distinct_and_html_escaped(self):
        response = self.client.get(reverse("admin:composite_pk_shelf_add"))
        html = response.content.decode()
        form = response.context["adminform"].form
        values = [str(value) for value, _ in form.fields["books"].choices]
        self.assertEqual(len(values), len(set(values)))
        for isbn in SPECIAL_ISBNS:
            expected = Book._meta.pk.value_to_string((self.tenant_1.id, isbn))
            self.assertIn(expected, values)
            self.assertIn(escape(expected), html)
        # A JSON null component is never offered.
        self.assertNotIn(json.dumps(["1", None]), values)

    def test_change_page_selects_current_relation(self):
        shelf = self.make_shelf(self.book_comma_1, self.books[""])
        response = self.client.get(self.change_url(shelf))
        self.assertEqual(response.status_code, 200)
        html = response.content.decode()
        for expected in self.encoded(self.book_comma_1, self.books[""]):
            self.assertIn('value="%s" selected' % escape(expected), html)
        for other in (self.book_comma_2, self.books["None"]):
            self.assertNotIn(
                'value="%s" selected' % escape(self.encoded(other)[0]), html
            )

    def test_related_add_link_has_no_to_field(self):
        # The relation can only be addressed by the whole composite key, so
        # no component field name may leak into the popup URL.
        response = self.client.get(reverse("admin:composite_pk_shelf_add"))
        self.assertContains(
            response,
            "/admin/composite_pk/book/add/?_popup=1&amp;"
            "_source_model=composite_pk.shelf",
        )
        self.assertNotContains(response, "book/add/?_to_field=")


class SaveTests(CompositePKM2MAdminBase):
    def test_add_page_saves_relation(self):
        targets = [self.book_comma_1, self.books[""], self.books["héllo世界"]]
        response = self.client.post(
            reverse("admin:composite_pk_shelf_add"),
            self.save_data(*targets, name="new shelf", tags=[self.tag]),
        )
        self.assertEqual(response.status_code, 302, response.content[:1000])
        shelf = Shelf.objects.get(name="new shelf")
        self.assertCountEqual(shelf.books.all(), targets)

    def test_add_page_invalid_creates_nothing(self):
        before = Shelf.objects.count()
        response = self.client.post(
            reverse("admin:composite_pk_shelf_add"),
            {
                "name": "never saved",
                "books": ['["1", "ghost"]'],
                "tags": [str(self.tag.pk)],
                "_save": "Save",
            },
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(Shelf.objects.count(), before)
        self.assertFalse(Shelf.objects.filter(name="never saved").exists())

    def test_clean_binding_and_save(self):
        shelf = self.make_shelf()
        targets = [self.books[""], self.books["None"], self.book_comma_2]
        response = self.client.post(
            self.change_url(shelf), self.save_data(*targets, tags=[self.tag])
        )
        self.assertEqual(response.status_code, 302, response.content[:1000])
        shelf.refresh_from_db()
        self.assertCountEqual(shelf.books.all(), targets)
        # Reverse and cross-relation queries see the same collection.
        for book in targets:
            self.assertIn(shelf, book.shelves.all())
        self.assertCountEqual(Book.objects.filter(shelves=shelf), targets)
        self.assertCountEqual(
            Shelf.objects.filter(books__in=targets).distinct(), [shelf]
        )

    def test_repeated_submission_is_idempotent(self):
        shelf = self.make_shelf()
        targets = [self.book_comma_1, self.books["42"]]
        for _ in range(3):
            response = self.client.post(
                self.change_url(shelf), self.save_data(*targets, tags=[self.tag])
            )
            self.assertEqual(response.status_code, 302)
        self.assertEqual(
            Shelf.books.through.objects.filter(shelf=shelf).count(), len(targets)
        )

    def test_save_replaces_only_this_relation(self):
        tag_1 = Tag.objects.create(name="tag 1")
        tag_2 = Tag.objects.create(name="tag 2")
        shelf = self.make_shelf(self.books[","], tags=(tag_1, tag_2))
        other_shelf = self.make_shelf(self.book_comma_2)
        response = self.client.post(
            self.change_url(shelf),
            self.save_data(
                self.books["@"], self.books[""], tags=[tag_1, tag_2]
            ),
        )
        self.assertEqual(response.status_code, 302)
        shelf.refresh_from_db()
        self.assertCountEqual(shelf.books.all(), [self.books["@"], self.books[""]])
        # The other M2M field of the same object is untouched.
        self.assertCountEqual(shelf.tags.all(), [tag_1, tag_2])
        # The shared-component target keeps its link on the other holder.
        other_shelf.refresh_from_db()
        self.assertCountEqual(other_shelf.books.all(), [self.book_comma_2])

    def test_replace_between_targets_sharing_a_component(self):
        shelf = self.make_shelf(self.book_comma_1)
        response = self.client.post(
            self.change_url(shelf), self.save_data(self.book_comma_2, tags=[self.tag])
        )
        self.assertEqual(response.status_code, 302)
        shelf.refresh_from_db()
        self.assertCountEqual(shelf.books.all(), [self.book_comma_2])
        self.assertEqual(
            Book.objects.get(pk=self.book_comma_1.pk).pk, self.book_comma_1.pk
        )

    def test_reload_prefetch_and_cross_filter_agree(self):
        shelf = self.make_shelf(self.books["\\"])
        targets = [self.books["/"], self.books["héllo世界"]]
        response = self.client.post(
            self.change_url(shelf), self.save_data(*targets, tags=[self.tag])
        )
        self.assertEqual(response.status_code, 302)
        # Re-rendering the page selects the new set only.
        response = self.client.get(self.change_url(Shelf.objects.get(pk=shelf.pk)))
        html = response.content.decode()
        for expected in self.encoded(*targets):
            self.assertIn('value="%s" selected' % escape(expected), html)
        self.assertNotIn(
            'value="%s" selected' % escape(self.encoded(self.books["\\"])[0]), html
        )
        # Prefetch and cross-relation filtering see the same set.
        prefetched = Shelf.objects.prefetch_related("books").get(pk=shelf.pk)
        self.assertCountEqual(prefetched.books.all(), targets)
        self.assertCountEqual(Book.objects.filter(shelves=shelf), targets)


class InvalidSubmissionTests(CompositePKM2MAdminBase):
    def test_invalid_values(self):
        shelf = self.make_shelf()
        through = Shelf.books.through
        for value in INVALID_VALUES:
            with self.subTest(value=value):
                before = set(
                    through.objects.values_list(
                        "shelf_id", "book_tenant_id", "book_isbn"
                    )
                )
                data = {
                    "name": "shelf",
                    "books": [value],
                    "tags": [str(self.tag.pk)],
                    "_save": "Save",
                }
                response = self.client.post(self.change_url(shelf), data)
                self.assertEqual(response.status_code, 200)
                self.assertEqual(self.error_codes(response), {"invalid_choice"})
                # Nothing was written.
                self.assertEqual(
                    set(
                        through.objects.values_list(
                            "shelf_id", "book_tenant_id", "book_isbn"
                        )
                    ),
                    before,
                )
                shelf.refresh_from_db()
                self.assertFalse(shelf.books.exists())

    def test_one_bad_value_among_many_is_atomic(self):
        shelf = Shelf.objects.prefetch_related("books").get(
            pk=self.make_shelf(self.book_comma_1, self.books[""]).pk
        )
        prefetched = list(shelf.books.all())
        through = Shelf.books.through
        before = set(
            through.objects.values_list("shelf_id", "book_tenant_id", "book_isbn")
        )
        data = self.save_data(
            self.book_comma_1,
            self.books[""],
            self.books["None"],
            tags=[self.tag],
            name="changed name",
        )
        data["books"].append('["1", "no-such-isbn"]')
        response = self.client.post(self.change_url(shelf), data)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.error_codes(response), {"invalid_choice"})
        # Through rows, scalar fields and the prefetched collection stay
        # in their pre-operation state.
        self.assertEqual(
            set(
                through.objects.values_list(
                    "shelf_id", "book_tenant_id", "book_isbn"
                )
            ),
            before,
        )
        shelf.refresh_from_db()
        self.assertEqual(shelf.name, "shelf")
        self.assertEqual(
            set(shelf.books.values_list("pk", flat=True)),
            {self.book_comma_1.pk, self.books[""].pk},
        )
        self.assertEqual(list(shelf.books.all()), prefetched)
        # Re-rendering keeps the original set, not the bad submission
        # (the bad value appears only in the error message, never as an
        # option).
        html = response.content.decode()
        for expected in self.encoded(self.book_comma_1, self.books[""]):
            self.assertIn('value="%s" selected' % escape(expected), html)
        self.assertNotIn(
            '<option value="%s"' % escape('["1", "no-such-isbn"]'), html
        )

    def test_retry_after_failure_succeeds(self):
        shelf = self.make_shelf(self.books[""])
        bad_data = self.save_data(self.book_comma_1, tags=[self.tag])
        bad_data["books"].append('["1", "ghost"]')
        response = self.client.post(self.change_url(shelf), bad_data)
        self.assertEqual(response.status_code, 200)
        response = self.client.post(
            self.change_url(Shelf.objects.get(pk=shelf.pk)),
            self.save_data(
                self.book_comma_1, self.books[""], tags=[self.tag]
            ),
        )
        self.assertEqual(response.status_code, 302, response.content[:500])
        shelf.refresh_from_db()
        self.assertCountEqual(
            shelf.books.all(), [self.book_comma_1, self.books[""]]
        )


class DeletedTargetTests(CompositePKM2MAdminBase):
    def test_deleted_target_disappears_and_cannot_rebind(self):
        doomed = self.books["@"]
        shelf = self.make_shelf(doomed, self.books[""])
        encoded_doomed = self.encoded(doomed)[0]
        doomed_pk = doomed.pk
        doomed.delete()

        response = self.client.get(self.change_url(Shelf.objects.get(pk=shelf.pk)))
        values = [
            str(value)
            for value, _ in response.context["adminform"].form.fields[
                "books"
            ].choices
        ]
        self.assertNotIn(encoded_doomed, values)
        # The relation disappeared with the target by cascade.
        shelf.refresh_from_db()
        self.assertCountEqual(shelf.books.all(), [self.books[""]])
        self.assertFalse(
            Shelf.books.through.objects.filter(
                book_tenant_id=doomed_pk[0], book_isbn=doomed_pk[1]
            ).exists()
        )
        # The stale option no longer binds.
        response = self.client.post(
            self.change_url(shelf),
            {
                "name": "shelf",
                "books": [encoded_doomed],
                "tags": [str(self.tag.pk)],
                "_save": "Save",
            },
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.error_codes(response), {"invalid_choice"})


class WidgetVariantBase(CompositePKM2MAdminBase):
    attribute = None

    def setUp(self):
        super().setUp()
        setattr(ShelfAdmin, self.attribute, self.value)
        self.addCleanup(setattr, ShelfAdmin, self.attribute, ())


class RawIdWidgetTests(WidgetVariantBase):
    attribute = "raw_id_fields"
    value = ("books",)

    def test_renders_full_keys(self):
        shelf = self.make_shelf(self.book_comma_1, self.books[""])
        response = self.client.get(self.change_url(shelf))
        self.assertEqual(response.status_code, 200)
        html = response.content.decode()
        self.assertIn("vManyToManyRawIdAdminField", html)
        for expected in self.encoded(self.book_comma_1, self.books[""]):
            self.assertIn(escape(expected), html)

    def test_save_splits_keys_at_json_boundaries(self):
        shelf = self.make_shelf()
        targets = [
            self.book_comma_1,
            self.book_comma_2,
            self.books[""],
            self.books["héllo世界"],
        ]
        data = {
            "name": "shelf",
            # Keys that contain commas must not be split at those commas.
            "books": ",".join(self.encoded(*targets)),
            "tags": [str(self.tag.pk)],
            "_save": "Save",
        }
        response = self.client.post(self.change_url(shelf), data)
        self.assertEqual(response.status_code, 302, response.content[:1000])
        shelf.refresh_from_db()
        self.assertCountEqual(shelf.books.all(), targets)

    def test_malformed_value_is_invalid_choice(self):
        shelf = self.make_shelf()
        response = self.client.post(
            self.change_url(shelf),
            {
                "name": "shelf",
                "books": '["1", "x"],garbled',
                "tags": [str(self.tag.pk)],
                "_save": "Save",
            },
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.error_codes(response), {"invalid_choice"})
        shelf.refresh_from_db()
        self.assertFalse(shelf.books.exists())


class FilterHorizontalWidgetTests(WidgetVariantBase):
    attribute = "filter_horizontal"
    value = ("books",)

    def test_render_and_save(self):
        shelf = self.make_shelf()
        response = self.client.get(self.change_url(shelf))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "selectfilter")
        targets = [self.book_comma_2, self.books["@"], self.books[""]]
        response = self.client.post(
            self.change_url(shelf), self.save_data(*targets, tags=[self.tag])
        )
        self.assertEqual(response.status_code, 302, response.content[:1000])
        shelf.refresh_from_db()
        self.assertCountEqual(shelf.books.all(), targets)


class AutocompleteWidgetTests(WidgetVariantBase):
    attribute = "autocomplete_fields"
    value = ("books",)

    def test_endpoint_returns_form_encoded_keys(self):
        response = self.client.get(
            reverse("admin:autocomplete")
            + "?app_label=composite_pk&model_name=shelf&field_name=books&term="
        )
        self.assertEqual(response.status_code, 200)
        ids = {item["id"] for item in response.json()["results"]}
        self.assertIn(self.encoded(self.book_comma_1)[0], ids)
        self.assertIn(self.encoded(self.book_comma_2)[0], ids)

    def test_selected_relation_renders(self):
        shelf = self.make_shelf(self.book_comma_1, self.books[""])
        response = self.client.get(self.change_url(shelf))
        self.assertEqual(response.status_code, 200)
        html = response.content.decode()
        for expected in self.encoded(self.book_comma_1, self.books[""]):
            self.assertIn('value="%s" selected' % escape(expected), html)


class PopupTests(CompositePKM2MAdminBase):
    def test_choose_row_carries_form_encoded_key(self):
        response = self.client.get("/admin/composite_pk/book/?_popup=1")
        self.assertEqual(response.status_code, 200)
        expected = self.encoded(self.book_comma_1)[0]
        self.assertContains(response, 'data-popup-opener="%s"' % escape(expected))
        # The change URL still quotes the key for routing; the opener
        # value is independent of it.
        self.assertContains(response, "/1,_2C/change/")

    def test_add_another_popup_returns_form_encoded_key(self):
        response = self.client.post(
            reverse("admin:composite_pk_book_add"),
            {
                "tenant": str(self.tenant_1.pk),
                "isbn": "p,o/p",
                "title": "popup",
                "_popup": "1",
            },
        )
        self.assertEqual(response.status_code, 200)
        data = json.loads(response.context["popup_response_data"])
        self.assertEqual(data["value"], '["1", "p,o/p"]')

    def test_change_popup_returns_form_encoded_key(self):
        book = self.books[","]
        response = self.client.post(
            reverse(
                "admin:composite_pk_book_change", args=[quote(book.pk)]
            ),
            {
                "tenant": str(self.tenant_1.pk),
                "isbn": ",",
                "title": "new title",
                "_popup": "1",
            },
        )
        self.assertEqual(response.status_code, 200)
        data = json.loads(response.context["popup_response_data"])
        encoded = self.encoded(book)[0]
        self.assertEqual(data["value"], encoded)
        self.assertEqual(data["new_value"], encoded)


class SinglePrimaryKeyCompatTests(CompositePKM2MAdminBase):
    def bookmark_url(self, bookmark):
        return reverse("admin:composite_pk_bookmark_change", args=[bookmark.pk])

    def test_choices_clean_save_and_errors_unchanged(self):
        bookmark = Bookmark.objects.create(name="bm")
        response = self.client.get(reverse("admin:composite_pk_bookmark_add"))
        self.assertEqual(response.status_code, 200)
        self.assertCountEqual(
            [
                str(value)
                for value, _ in response.context["adminform"].form.fields[
                    "tags"
                ].choices
            ],
            [str(self.tag.pk)],
        )
        response = self.client.post(
            self.bookmark_url(bookmark),
            {"name": "bm", "tags": [str(self.tag.pk)], "_save": "Save"},
        )
        self.assertEqual(response.status_code, 302)
        bookmark.refresh_from_db()
        self.assertCountEqual(bookmark.tags.all(), [self.tag])
        # Repeated save stays idempotent.
        self.client.post(
            self.bookmark_url(bookmark),
            {"name": "bm", "tags": [str(self.tag.pk)], "_save": "Save"},
        )
        self.assertEqual(bookmark.tags.count(), 1)
        # Unknown id: invalid_choice; unconvertible id: invalid_pk_value.
        response = self.client.post(
            self.bookmark_url(bookmark),
            {"name": "bm", "tags": ["999"], "_save": "Save"},
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.error_codes(response, "tags"), {"invalid_choice"})
        response = self.client.post(
            self.bookmark_url(bookmark),
            {"name": "bm", "tags": ["abc"], "_save": "Save"},
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.error_codes(response, "tags"), {"invalid_pk_value"})
        bookmark.refresh_from_db()
        self.assertCountEqual(bookmark.tags.all(), [self.tag])

    def test_single_pk_related_link_keeps_to_field(self):
        response = self.client.get(reverse("admin:composite_pk_shelf_add"))
        self.assertContains(
            response,
            "/admin/composite_pk/tag/add/?_to_field=id&amp;_popup=1",
        )
