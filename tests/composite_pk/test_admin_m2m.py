"""
Many-to-many relations to models with a composite primary key must be
editable through ``django.contrib.admin.site``: after registering the
models, the admin add and change pages render, validate and save exactly
the same set of objects, including after a failed submission is retried.

Every assertion here goes through the admin pages (HTTP) or the public ORM;
the form layer is covered separately in test_m2m_forms.py.
"""

import json

from django.contrib import admin
from django.contrib.auth.models import Permission, User
from django.test import TestCase, override_settings
from django.urls import path
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


class ShelfAdmin(admin.ModelAdmin):
    filter_horizontal = ["books", "tags"]


class ShelfRawIdAdmin(admin.ModelAdmin):
    raw_id_fields = ["books"]
    filter_horizontal = ["tags"]


class ShelfAutocompleteAdmin(admin.ModelAdmin):
    autocomplete_fields = ["books"]
    filter_horizontal = ["tags"]


class BookAdmin(admin.ModelAdmin):
    search_fields = ["isbn", "title"]
    ordering = ("tenant_id", "isbn")


class TagAdmin(admin.ModelAdmin):
    search_fields = ["name"]


class BookmarkAdmin(admin.ModelAdmin):
    filter_horizontal = ["tags"]


# The public entry point is django.contrib.admin.site. Separate (non-default)
# AdminSites are used here so registering the models can't affect other test
# modules; one test class below exercises the global default site itself,
# registering and unregistering around itself.
filtered_site = admin.AdminSite(name="composite_m2m_filtered")
raw_id_site = admin.AdminSite(name="composite_m2m_raw_id")
autocomplete_site = admin.AdminSite(name="composite_m2m_autocomplete")

for site in (filtered_site, raw_id_site, autocomplete_site):
    site.register(Book, BookAdmin)
    site.register(Tag, TagAdmin)

filtered_site.register(Shelf, ShelfAdmin)
filtered_site.register(Bookmark, BookmarkAdmin)
raw_id_site.register(Shelf, ShelfRawIdAdmin)
autocomplete_site.register(Shelf, ShelfAutocompleteAdmin)

urlpatterns = [
    path("admin/", filtered_site.urls),
    path("raw-id-admin/", raw_id_site.urls),
    path("autocomplete-admin/", autocomplete_site.urls),
]


class CompositePKM2MAdminBase(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.tenant_1 = Tenant.objects.create(id=1, name="tenant 1")
        cls.tenant_2 = Tenant.objects.create(id=2, name="tenant 2")
        cls.books = {}
        for isbn in SPECIAL_ISBNS:
            cls.books[isbn] = Book.objects.create(tenant=cls.tenant_1, isbn=isbn)
        # A second target that shares the second component with an existing one
        # but differs in the first (declaration order: tenant_id, isbn).
        cls.book_comma_2 = Book.objects.create(tenant=cls.tenant_2, isbn=",")
        cls.tag = Tag.objects.create(name="default tag")
        cls.superuser = User.objects.create_superuser(
            username="super", password="secret", email="super@example.com"
        )

    def setUp(self):
        self.client.force_login(self.superuser)

    def encoded(self, *books):
        return [Book._meta.pk.value_to_string(book.pk) for book in books]

    @staticmethod
    def esc(value):
        return escape(value)

    def make_shelf(self, *books, name="shelf", tags=()):
        shelf = Shelf.objects.create(name=name)
        if books:
            shelf.books.add(*books)
        if tags:
            shelf.tags.add(*tags)
        return shelf

    def change_url(self, shelf, prefix="/admin"):
        return f"{prefix}/composite_pk/shelf/{shelf.pk}/change/"

    def add_url(self, prefix="/admin"):
        return f"{prefix}/composite_pk/shelf/add/"

    def assert_invalid_choice(self, response, field="books"):
        self.assertEqual(response.status_code, 200)
        codes = {
            error.code
            for batch in response.context["adminform"].form.errors.as_data()[field]
            for error in batch.error_list
        }
        self.assertEqual(codes, {"invalid_choice"})


@override_settings(ROOT_URLCONF="composite_pk.test_admin_m2m")
class AdminChoiceRenderingTests(CompositePKM2MAdminBase):
    def test_choices_carry_full_key_in_declaration_order(self):
        shelf = self.make_shelf()
        response = self.client.get(self.change_url(shelf))
        self.assertEqual(response.status_code, 200)
        values = [
            str(value)
            for value, _ in response.context["adminform"].form.fields["books"].choices
        ]
        self.assertCountEqual(values, self.encoded(*Book.objects.order_by("pk")))
        # Declaration order of CompositePrimaryKey("tenant_id", "isbn") is
        # preserved: first the integer tenant id, then the text isbn.
        decoded = {tuple(part) for part in map(json.loads, values)}
        self.assertIn(("1", ""), decoded)
        self.assertIn(("2", ","), decoded)
        self.assertNotIn((",", "2"), decoded)

    def test_special_values_are_all_distinct_rendered_options(self):
        shelf = self.make_shelf()
        response = self.client.get(self.change_url(shelf))
        field = response.context["adminform"].form.fields["books"]
        values = [str(value) for value, _ in field.choices]
        self.assertEqual(len(values), len(set(values)))
        html = response.content.decode()
        for isbn in SPECIAL_ISBNS:
            expected = Book._meta.pk.value_to_string((self.tenant_1.id, isbn))
            self.assertIn(expected, values)
            # HTML-escaped by the widget; no two special values collapse.
            self.assertIn(self.esc(expected), html)
        # Empty string, the text "None" and a literal "@" are different keys.
        self.assertIn(json.dumps(["1", ""], ensure_ascii=False), values)
        self.assertIn(json.dumps(["1", "None"], ensure_ascii=False), values)
        self.assertIn(json.dumps(["1", "@"], ensure_ascii=False), values)

    def test_change_page_selects_current_relation(self):
        shelf = self.make_shelf(self.book_comma_2, self.books[""])
        response = self.client.get(self.change_url(shelf))
        html = response.content.decode()
        for expected in self.encoded(self.book_comma_2, self.books[""]):
            self.assertIn('value="%s" selected' % self.esc(expected), html)
        for other in (self.books[","], self.books["None"]):
            self.assertNotIn(
                'value="%s" selected' % self.esc(self.encoded(other)[0]),
                html,
            )

    def test_add_related_url_targets_whole_composite_key(self):
        shelf = self.make_shelf()
        response = self.client.get(self.change_url(shelf))
        html = response.content.decode()
        # The "+ add" popup must not carry a _to_field parameter pointing at a
        # single component of the composite primary key.
        self.assertIn(
            "/admin/composite_pk/book/add/?_popup=1"
            "&amp;_source_model=composite_pk.shelf",
            html,
        )
        self.assertNotIn("book/add/?_to_field", html)


@override_settings(ROOT_URLCONF="composite_pk.test_admin_m2m")
class AdminValidSubmissionTests(CompositePKM2MAdminBase):
    def post_shelf(self, shelf, books, name=None, tags=None, prefix="/admin"):
        payload = {"name": name if name is not None else shelf.name}
        if books is not None:
            payload["books"] = books
        if tags is None:
            tags = [self.tag]
        payload["tags"] = [str(tag.pk) for tag in tags]
        return self.client.post(self.change_url(shelf, prefix), payload)

    def test_add_page_creates_relation(self):
        tag = Tag.objects.create(name="tag")
        targets = [self.books[""], self.book_comma_2]
        response = self.client.post(
            self.add_url(),
            {
                "name": "new shelf",
                "books": self.encoded(*targets),
                "tags": [str(tag.pk)],
            },
        )
        self.assertEqual(response.status_code, 302)
        shelf = Shelf.objects.get(name="new shelf")
        self.assertCountEqual(shelf.books.all(), targets)
        self.assertCountEqual(shelf.tags.all(), [tag])

    def test_save_seen_by_forward_reverse_and_cross_queries(self):
        other_shelf = self.make_shelf(self.books["/"])
        shelf = self.make_shelf(self.books["/"])
        targets = [self.book_comma_2, self.books[""], self.books["héllo世界"]]
        response = self.post_shelf(shelf, self.encoded(*targets), name="renamed")
        self.assertEqual(response.status_code, 302)

        shelf.refresh_from_db()
        self.assertEqual(shelf.name, "renamed")
        # Forward relation.
        self.assertCountEqual(shelf.books.all(), targets)
        # Reverse relation.
        for book in targets:
            self.assertIn(shelf, book.shelves.all())
        # Cross-relation queries both ways.
        self.assertCountEqual(Book.objects.filter(shelves=shelf), targets)
        self.assertCountEqual(
            Shelf.objects.filter(books__in=targets).distinct(), [shelf]
        )
        # The same relation on another holder is untouched.
        self.assertCountEqual(other_shelf.books.all(), [self.books["/"]])

    def test_repeated_submission_is_idempotent(self):
        shelf = self.make_shelf()
        targets = [self.book_comma_2, self.books["42"]]
        data = {
            "name": "shelf",
            "books": self.encoded(*targets),
            "tags": [str(self.tag.pk)],
        }
        for _ in range(3):
            response = self.client.post(
                self.change_url(Shelf.objects.get(pk=shelf.pk)), data
            )
            self.assertEqual(response.status_code, 302)
        shelf.refresh_from_db()
        self.assertCountEqual(shelf.books.all(), targets)
        self.assertEqual(
            Shelf.books.through.objects.filter(shelf=shelf).count(), len(targets)
        )

    def test_save_only_replaces_this_field_and_keeps_shared_components(self):
        tag_1 = Tag.objects.create(name="tag 1")
        tag_2 = Tag.objects.create(name="tag 2")
        shelf = self.make_shelf(self.books[","], tags=(tag_1, tag_2))
        other_shelf = self.make_shelf(self.book_comma_2)
        response = self.post_shelf(
            shelf,
            self.encoded(self.books["@"], self.books[""]),
            tags=[tag_1, tag_2],
        )
        self.assertEqual(response.status_code, 302)
        shelf.refresh_from_db()
        self.assertCountEqual(shelf.books.all(), [self.books["@"], self.books[""]])
        # The other many-to-many field of the same object is unchanged.
        self.assertCountEqual(shelf.tags.all(), [tag_1, tag_2])
        # The replaced target that shares one component is untouched.
        self.assertEqual(Book.objects.get(pk=self.books[","].pk).pk, self.books[","].pk)
        other_shelf.refresh_from_db()
        self.assertCountEqual(other_shelf.books.all(), [self.book_comma_2])

    def test_saved_set_rerendered_with_same_key(self):
        shelf = self.make_shelf(self.books["\\"], self.books["/"])
        response = self.post_shelf(
            shelf, self.encoded(self.books["\\"], self.books["/"])
        )
        self.assertEqual(response.status_code, 302)
        fresh = self.client.get(self.change_url(Shelf.objects.get(pk=shelf.pk)))
        html = fresh.content.decode()
        for expected in self.encoded(self.books["\\"], self.books["/"]):
            self.assertIn('value="%s" selected' % self.esc(expected), html)

    def test_prefetch_and_reverse_match_after_save(self):
        shelf = self.make_shelf()
        targets = [self.books["None"], self.books["@"]]
        self.post_shelf(shelf, self.encoded(*targets))
        prefetched = Shelf.objects.prefetch_related("books").get(pk=shelf.pk)
        self.assertCountEqual(prefetched.books.all(), targets)
        for book in targets:
            self.assertEqual(list(book.shelves.all()), [shelf])

    def test_required_relation_cannot_be_cleared(self):
        # Shelf.books has no blank=True, so the admin field is required:
        # omitting it on submit is rejected and the relation is left intact.
        tag = Tag.objects.create(name="keep")
        shelf = self.make_shelf(self.book_comma_2, tags=(tag,))
        response = self.client.post(
            self.change_url(shelf), {"name": "shelf", "tags": [str(tag.pk)]}
        )
        self.assertEqual(response.status_code, 200)
        self.assertIn("books", response.context["adminform"].form.errors)
        shelf.refresh_from_db()
        self.assertCountEqual(shelf.books.all(), [self.book_comma_2])
        self.assertCountEqual(shelf.tags.all(), [tag])


@override_settings(ROOT_URLCONF="composite_pk.test_admin_m2m")
class AdminInvalidSubmissionTests(CompositePKM2MAdminBase):
    def post_raw(self, shelf, values, name="shelf"):
        return self.client.post(
            self.change_url(shelf),
            {"name": name, "books": values, "tags": [str(self.tag.pk)]},
        )

    def test_invalid_values(self):
        shelf = self.make_shelf()
        through = Shelf.books.through
        for value in INVALID_VALUES:
            with self.subTest(value=value):
                before = set(through.objects.values_list("book_tenant_id", "book_isbn"))
                response = self.post_raw(shelf, [value], name="changed")
                self.assert_invalid_choice(response)
                self.assertEqual(
                    set(through.objects.values_list("book_tenant_id", "book_isbn")),
                    before,
                )
                shelf.refresh_from_db()
                self.assertEqual(shelf.name, "shelf")
                self.assertFalse(shelf.books.exists())

    def test_one_bad_value_among_many_is_atomic(self):
        shelf = Shelf.objects.prefetch_related("books").get(
            pk=self.make_shelf(self.book_comma_2, self.books[""]).pk
        )
        prefetched = list(shelf.books.all())
        original_pks = set(shelf.books.values_list("pk", flat=True))
        through = Shelf.books.through
        before_rows = set(
            through.objects.values_list("shelf_id", "book_tenant_id", "book_isbn")
        )

        response = self.post_raw(
            shelf,
            [
                *self.encoded(self.book_comma_2, self.books[""], self.books["None"]),
                '["1", "no-such-isbn"]',
            ],
            name="changed name",
        )
        self.assertEqual(response.status_code, 200)
        # Through table, scalar fields and the prefetched collection all stay
        # in their pre-operation state.
        self.assertEqual(
            set(through.objects.values_list("shelf_id", "book_tenant_id", "book_isbn")),
            before_rows,
        )
        shelf.refresh_from_db()
        self.assertEqual(shelf.name, "shelf")
        self.assertEqual(set(shelf.books.values_list("pk", flat=True)), original_pks)
        self.assertEqual(list(shelf.books.all()), prefetched)

        # Re-rendering a fresh form on the same instance shows the old set.
        fresh = self.client.get(self.change_url(Shelf.objects.get(pk=shelf.pk)))
        html = fresh.content.decode()
        for expected in self.encoded(self.book_comma_2, self.books[""]):
            self.assertIn('value="%s" selected' % self.esc(expected), html)
        self.assertNotIn("no-such-isbn", html)

    def test_still_valid_value_stays_selected_after_error(self):
        shelf = self.make_shelf(self.books[""])
        response = self.post_raw(
            shelf, [*self.encoded(self.book_comma_2), '["1", "ghost"]']
        )
        self.assertEqual(response.status_code, 200)
        html = response.content.decode()
        good = self.encoded(self.book_comma_2)[0]
        self.assertIn('value="%s" selected' % self.esc(good), html)
        # The instance relation was not modified.
        shelf.refresh_from_db()
        self.assertCountEqual(shelf.books.all(), [self.books[""]])

    def test_retry_after_failure_succeeds(self):
        shelf = self.make_shelf(self.books[""])
        bad = self.post_raw(shelf, [*self.encoded(self.book_comma_2), '["1", "ghost"]'])
        self.assertEqual(bad.status_code, 200)
        good = self.client.post(
            self.change_url(Shelf.objects.get(pk=shelf.pk)),
            {
                "name": "shelf",
                "books": self.encoded(self.book_comma_2, self.books[""]),
                "tags": [str(self.tag.pk)],
            },
        )
        self.assertEqual(good.status_code, 302)
        shelf.refresh_from_db()
        self.assertCountEqual(shelf.books.all(), [self.book_comma_2, self.books[""]])

    def test_deleted_target_cannot_rebind(self):
        doomed = self.books["@"]
        encoded_doomed = self.encoded(doomed)[0]
        shelf = self.make_shelf(doomed, self.books[""])
        doomed.delete()

        # The choice is no longer offered.
        response = self.client.get(self.change_url(Shelf.objects.get(pk=shelf.pk)))
        values = [
            str(value)
            for value, _ in response.context["adminform"].form.fields["books"].choices
        ]
        self.assertNotIn(encoded_doomed, values)

        # Submitting the stale value fails with the same invalid_choice error.
        response = self.post_raw(Shelf.objects.get(pk=shelf.pk), [encoded_doomed])
        self.assert_invalid_choice(response)


@override_settings(ROOT_URLCONF="composite_pk.test_admin_m2m")
class AdminDeletionTests(CompositePKM2MAdminBase):
    def test_delete_target_cascades_other_targets_still_editable(self):
        doomed = self.books["@"]
        keeper = self.books[""]
        shelf = self.make_shelf(doomed, keeper)
        other_shelf = self.make_shelf(doomed)
        doomed_pk = doomed.pk
        doomed.delete()

        shelf.refresh_from_db()
        self.assertCountEqual(shelf.books.all(), [keeper])
        self.assertFalse(
            Shelf.books.through.objects.filter(
                book_tenant_id=doomed_pk[0], book_isbn=doomed_pk[1]
            ).exists()
        )
        # Other targets can still be edited in the admin.
        tag = Tag.objects.create(name="t")
        response = self.client.post(
            self.change_url(shelf),
            {
                "name": "shelf",
                "books": self.encoded(keeper, self.books["/"]),
                "tags": [str(tag.pk)],
            },
        )
        self.assertEqual(response.status_code, 302)
        shelf.refresh_from_db()
        self.assertCountEqual(shelf.books.all(), [keeper, self.books["/"]])
        # Other holders keep editing too.
        other_shelf.refresh_from_db()
        self.assertFalse(other_shelf.books.exists())

    def test_delete_holder_removes_through_rows_only(self):
        keeper = self.books[""]
        shelf = self.make_shelf(keeper)
        shelf_id = shelf.pk
        shelf.delete()
        self.assertFalse(Shelf.books.through.objects.filter(shelf_id=shelf_id).exists())
        # The target itself survives.
        self.assertTrue(Book.objects.filter(pk=keeper.pk).exists())


@override_settings(ROOT_URLCONF="composite_pk.test_admin_m2m")
class AdminRawIdWidgetTests(CompositePKM2MAdminBase):
    def change_url(self, shelf, prefix="/raw-id-admin"):
        return super().change_url(shelf, prefix)

    def add_url(self, prefix="/raw-id-admin"):
        return super().add_url(prefix)

    def test_render_uses_tab_separated_full_keys(self):
        shelf = self.make_shelf(self.books[""], self.book_comma_2)
        response = self.client.get(self.change_url(shelf))
        self.assertEqual(response.status_code, 200)
        html = response.content.decode()
        # Composite keys are JSON arrays that may contain commas, so the
        # raw-id field joins them with a tab and flags itself for JavaScript.
        self.assertIn('data-composite-pk="1"', html)
        expected_value = "\t".join(self.encoded(self.books[""], self.book_comma_2))
        self.assertIn('value="%s"' % escape(expected_value), html)
        # The lookup popup links to the target changelist without restricting
        # itself to a single key component (the JS appends _popup=1 itself);
        # the opener model is carried so an object added from inside the popup
        # returns with the form field's key encoding.
        self.assertIn(
            'href="/raw-id-admin/composite_pk/book/?_source_model=composite_pk.shelf" '
            'class="related-lookup"',
            html,
        )
        self.assertNotIn("book/?_to_field", html)

    def test_tab_separated_submission_saves_relation(self):
        shelf = self.make_shelf()
        value = "\t".join(self.encoded(self.books["@"], self.books[","]))
        response = self.client.post(
            self.change_url(shelf),
            {"name": "shelf", "books": value, "tags": [str(self.tag.pk)]},
        )
        self.assertEqual(response.status_code, 302, response.content[:200])
        shelf.refresh_from_db()
        self.assertCountEqual(shelf.books.all(), [self.books["@"], self.books[","]])

    def test_malformed_value_is_invalid_choice(self):
        shelf = self.make_shelf(self.books[""])
        value = "\t".join(self.encoded(self.books["@"]) + ["garbled"])
        response = self.client.post(
            self.change_url(shelf),
            {"name": "shelf", "books": value, "tags": [str(self.tag.pk)]},
        )
        self.assert_invalid_choice(response)
        shelf.refresh_from_db()
        self.assertCountEqual(shelf.books.all(), [self.books[""]])

    def test_lookup_popup_opener_uses_form_encoding(self):
        self.make_shelf()
        response = self.client.get("/raw-id-admin/composite_pk/book/?_popup=1")
        self.assertEqual(response.status_code, 200)
        html = response.content.decode()
        for isbn in ("", "@", ","):
            expected = Book._meta.pk.value_to_string((self.tenant_1.id, isbn))
            self.assertIn('data-popup-opener="%s"' % escape(expected), html)
        # The Python tuple repr is never used as an opener value.
        self.assertNotIn("data-popup-opener=&quot;(1,", html)

    def test_add_link_inside_lookup_popup_preserves_source_model(self):
        self.make_shelf()
        response = self.client.get(
            "/raw-id-admin/composite_pk/book/"
            "?_popup=1&_source_model=composite_pk.shelf"
        )
        self.assertEqual(response.status_code, 200)
        html = response.content.decode().replace("&amp;", "&")
        # The popup's own "Add book" link keeps both the popup flag and the
        # opener model as real query parameters, and never references a
        # single key component.
        import re
        from urllib.parse import parse_qs, urlsplit

        (raw_href,) = re.findall(r'<a href="([^"]*book/add/[^"]*)"', html)
        params = parse_qs(urlsplit(raw_href.replace("&amp;", "&")).query)
        self.assertEqual(params.get("_popup"), ["1"])
        self.assertEqual(params.get("_source_model"), ["composite_pk.shelf"])
        self.assertNotIn("_to_field", params)

        # Following that link and submitting answers with the form encoding.
        follow = self.client.post(
            "/raw-id-admin/composite_pk/book/add/?_popup=1"
            "&_source_model=composite_pk.shelf",
            {
                "tenant": str(self.tenant_1.id),
                "isbn": "from,lookup",
                "title": "t",
                "_popup": "1",
                "_source_model": "composite_pk.shelf",
            },
        )
        self.assertEqual(follow.status_code, 200)
        data = json.loads(follow.context["popup_response_data"])
        self.assertEqual(data["value"], json.dumps(["1", "from,lookup"]))


@override_settings(ROOT_URLCONF="composite_pk.test_admin_m2m")
class AdminPopupTests(CompositePKM2MAdminBase):
    def book_add_url(self):
        return "/admin/composite_pk/book/add/"

    def test_popup_add_from_m2m_returns_form_encoded_value(self):
        response = self.client.post(
            self.book_add_url() + "?_popup=1",
            {
                "tenant": str(self.tenant_1.id),
                "isbn": "new,isbn",
                "title": "t",
                "_popup": "1",
                "_source_model": "composite_pk.Shelf",
            },
        )
        self.assertEqual(response.status_code, 200)
        data = json.loads(response.context["popup_response_data"])
        self.assertEqual(data["value"], json.dumps(["1", "new,isbn"]))

    def test_popup_value_is_json_reversible_for_every_special_isbn(self):
        # The empty string is excluded: Book.isbn has no blank=True, so the
        # admin form rejects it on input; it still round-trips as an existing
        # choice value, covered elsewhere.
        for isbn in SPECIAL_ISBNS:
            if isbn == "":
                continue
            with self.subTest(isbn=isbn):
                response = self.client.post(
                    self.book_add_url() + "?_popup=1",
                    {
                        "tenant": str(self.tenant_1.id),
                        "isbn": isbn,
                        "title": "t",
                        "_popup": "1",
                        "_source_model": "composite_pk.Shelf",
                    },
                )
                value = json.loads(response.context["popup_response_data"])["value"]
                decoded = tuple(Book._meta.pk.to_python(value))
                self.assertEqual(decoded, (self.tenant_1.id, isbn))

    def test_popup_change_from_m2m_uses_form_encoding(self):
        from django.contrib.admin.utils import quote

        book = self.books["@"]
        response = self.client.post(
            f"/admin/composite_pk/book/{quote(book.pk)}/change/",
            {
                "tenant": str(self.tenant_1.id),
                "isbn": "renamed",
                "title": "t",
                "_popup": "1",
                "_source_model": "composite_pk.Shelf",
            },
        )
        self.assertEqual(response.status_code, 200)
        data = json.loads(response.context["popup_response_data"])
        self.assertEqual(data["new_value"], json.dumps(["1", "renamed"]))


@override_settings(ROOT_URLCONF="composite_pk.test_admin_m2m")
class AdminAutocompleteTests(CompositePKM2MAdminBase):
    def test_autocomplete_json_uses_form_encoding(self):
        response = self.client.get(
            "/autocomplete-admin/autocomplete/",
            {
                "app_label": "composite_pk",
                "model_name": "shelf",
                "field_name": "books",
                "term": "",
            },
        )
        self.assertEqual(response.status_code, 200)
        ids = {result["id"] for result in response.json()["results"]}
        for isbn in ("", "@", ","):
            self.assertIn(Book._meta.pk.value_to_string((self.tenant_1.id, isbn)), ids)

    def test_selected_relation_renders_and_saves(self):
        shelf = self.make_shelf(self.books[""], self.book_comma_2)
        response = self.client.get(self.change_url(shelf, prefix="/autocomplete-admin"))
        self.assertEqual(response.status_code, 200)
        html = response.content.decode()
        for expected in self.encoded(self.books[""], self.book_comma_2):
            self.assertIn('value="%s" selected' % escape(expected), html)

        response = self.client.post(
            self.change_url(shelf, prefix="/autocomplete-admin"),
            {
                "name": "shelf",
                "books": self.encoded(self.books["@"]),
                "tags": [str(self.tag.pk)],
            },
        )
        self.assertEqual(response.status_code, 302)
        shelf.refresh_from_db()
        self.assertCountEqual(shelf.books.all(), [self.books["@"]])


@override_settings(ROOT_URLCONF="composite_pk.test_admin_m2m")
class SinglePrimaryKeyCompatTests(TestCase):
    """Single-field primary key admin forms keep their existing semantics."""

    @classmethod
    def setUpTestData(cls):
        cls.tag_1 = Tag.objects.create(id=1, name="tag 1")
        cls.tag_2 = Tag.objects.create(id=2, name="tag 2")
        cls.superuser = User.objects.create_superuser(
            username="super", password="secret", email="super@example.com"
        )

    def setUp(self):
        self.client.force_login(self.superuser)

    def test_filtered_m2m_choices_save_errors(self):
        bookmark = Bookmark.objects.create(name="bm")
        url = f"/admin/composite_pk/bookmark/{bookmark.pk}/change/"
        response = self.client.get(url)
        self.assertCountEqual(
            [
                str(value)
                for value, _ in response.context["adminform"]
                .form.fields["tags"]
                .choices
            ],
            ["1", "2"],
        )
        response = self.client.post(url, {"name": "bm", "tags": ["1"]})
        self.assertEqual(response.status_code, 302)
        bookmark.refresh_from_db()
        self.assertCountEqual(bookmark.tags.all(), [self.tag_1])

        # Unknown id -> invalid_choice.
        response = self.client.post(url, {"name": "bm", "tags": ["999"]})
        self.assertEqual(response.status_code, 200)
        codes = {
            error.code
            for batch in response.context["adminform"].form.errors.as_data()["tags"]
            for error in batch.error_list
        }
        self.assertEqual(codes, {"invalid_choice"})
        bookmark.refresh_from_db()
        self.assertCountEqual(bookmark.tags.all(), [self.tag_1])

    def test_view_only_user_cannot_edit(self):
        from django.contrib.contenttypes.models import ContentType

        user = User.objects.create_user(
            username="viewonly", password="secret", is_staff=True
        )
        book_ct = ContentType.objects.get_for_model(Book)
        shelf_ct = ContentType.objects.get_for_model(Shelf)
        user.user_permissions.add(
            Permission.objects.get(codename="view_shelf", content_type=shelf_ct),
            Permission.objects.get(codename="view_book", content_type=book_ct),
        )
        self.client.force_login(user)
        response = self.client.get("/admin/composite_pk/shelf/add/")
        self.assertEqual(response.status_code, 403)


@override_settings(ROOT_URLCONF="composite_pk.default_admin_site_urls")
class DefaultAdminSiteTests(CompositePKM2MAdminBase):
    """Exercises the public entry point django.contrib.admin.site itself."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        for model, model_admin in (
            (Book, BookAdmin),
            (Tag, TagAdmin),
            (Shelf, ShelfAdmin),
            (Bookmark, BookmarkAdmin),
        ):
            if not admin.site.is_registered(model):
                admin.site.register(model, model_admin)

    @classmethod
    def tearDownClass(cls):
        for model in (Shelf, Bookmark, Book, Tag):
            if admin.site.is_registered(model):
                admin.site.unregister(model)
        super().tearDownClass()

    def change_url(self, shelf, prefix="/default-admin"):
        return super().change_url(shelf, prefix)

    def add_url(self, prefix="/default-admin"):
        return super().add_url(prefix)

    def test_add_and_change_pages_edit_the_same_relation(self):
        # Add page: create a holder with its relation in one submission.
        targets = [self.books["@"], self.book_comma_2]
        response = self.client.post(
            self.add_url(),
            {
                "name": "via default site",
                "books": self.encoded(*targets),
                "tags": [str(self.tag.pk)],
            },
        )
        self.assertEqual(response.status_code, 302)
        shelf = Shelf.objects.get(name="via default site")
        self.assertCountEqual(shelf.books.all(), targets)

        # Change page: the unbound page shows the current set.
        response = self.client.get(self.change_url(shelf))
        html = response.content.decode()
        for expected in self.encoded(*targets):
            self.assertIn('value="%s" selected' % self.esc(expected), html)

        # Replace the set; forward, reverse and cross-relation queries agree.
        new_targets = [self.books[""], self.books["None"]]
        response = self.client.post(
            self.change_url(shelf),
            {
                "name": "via default site",
                "books": self.encoded(*new_targets),
                "tags": [str(self.tag.pk)],
            },
        )
        self.assertEqual(response.status_code, 302)
        shelf.refresh_from_db()
        self.assertCountEqual(shelf.books.all(), new_targets)
        for book in new_targets:
            self.assertIn(shelf, book.shelves.all())
        self.assertCountEqual(Book.objects.filter(shelves=shelf), new_targets)

        # A bad value fails atomically with invalid_choice and the page keeps
        # showing the previous set.
        response = self.client.post(
            self.change_url(shelf),
            {
                "name": "changed",
                "books": [*self.encoded(*new_targets), '["1", "ghost"]'],
                "tags": [str(self.tag.pk)],
            },
        )
        self.assert_invalid_choice(response)
        shelf.refresh_from_db()
        self.assertEqual(shelf.name, "via default site")
        self.assertCountEqual(shelf.books.all(), new_targets)
