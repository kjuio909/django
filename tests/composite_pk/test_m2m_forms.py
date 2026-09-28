"""
Many-to-many relations to models with a composite primary key must work
through the regular ModelForm flow: an unbound form renders the current
relation, the same full key explains initial data/error redisplay/saved
state, and invalid input leaves the relation untouched.

The only form entry point exercised here is
``django.forms.models.modelform_factory(model, fields=...)``; everything
else uses the public ORM.
"""

import json

from django.core.exceptions import ValidationError
from django.forms.models import ModelChoiceField, modelform_factory
from django.test import TestCase
from django.utils.html import escape

from .models import Book, Bookmark, Shelf, Tag, Tenant

ShelfForm = modelform_factory(Shelf, fields=["name", "books"])
BookmarkForm = modelform_factory(Bookmark, fields=["name", "tags"])

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


class CompositePKM2MFormBase(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.tenant_1 = Tenant.objects.create(id=1, name="tenant 1")
        cls.tenant_2 = Tenant.objects.create(id=2, name="tenant 2")
        cls.books = {}
        for isbn in SPECIAL_ISBNS:
            cls.books[isbn] = Book.objects.create(tenant=cls.tenant_1, isbn=isbn)
        # A second target that shares the second component with an existing one
        # but differs in the first (declaration order: tenant_id, isbn).
        cls.book_comma_1 = cls.books[","]
        cls.book_comma_2 = Book.objects.create(tenant=cls.tenant_2, isbn=",")

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


class UnboundFormTests(CompositePKM2MFormBase):
    def test_choices_carry_full_key_in_declaration_order(self):
        shelf = self.make_shelf()
        form = ShelfForm(instance=shelf)
        values = [str(value) for value, _ in form.fields["books"].choices]
        self.assertCountEqual(values, self.encoded(*Book.objects.order_by("pk")))
        # The declaration order of CompositePrimaryKey("tenant_id", "isbn")
        # is preserved: first the integer tenant id, then the text isbn.
        decoded = {tuple(part) for part in map(json.loads, values)}
        self.assertIn(("1", ""), decoded)
        self.assertIn(("2", ","), decoded)
        self.assertNotIn((",", "2"), decoded)

    def test_special_values_are_all_distinct_choice_values(self):
        shelf = self.make_shelf()
        form = ShelfForm(instance=shelf)
        values = [str(value) for value, _ in form.fields["books"].choices]
        self.assertEqual(len(values), len(set(values)))
        html = str(form["books"])
        for isbn in SPECIAL_ISBNS:
            expected = Book._meta.pk.value_to_string((self.tenant_1.id, isbn))
            # The full encoded key (HTML-escaped by the widget) must appear;
            # no two special values collapse onto the same option.
            self.assertIn(expected, values)
            self.assertIn(self.esc(expected), html)
        # Empty string, the text "None" and a literal "@" are different keys;
        # a JSON null component is never offered as a choice.
        self.assertIn(json.dumps(["1", ""], ensure_ascii=False), values)
        self.assertIn(json.dumps(["1", "None"], ensure_ascii=False), values)
        self.assertIn(json.dumps(["1", "@"], ensure_ascii=False), values)
        self.assertNotIn(json.dumps(["1", None]), values)

    def test_unbound_form_selects_current_relation(self):
        shelf = self.make_shelf(self.book_comma_1, self.books[""])
        form = ShelfForm(instance=shelf)
        html = str(form["books"])
        for expected in self.encoded(self.book_comma_1, self.books[""]):
            self.assertIn('value="%s" selected' % self.esc(expected), html)
        for other in (self.book_comma_2, self.books["None"]):
            self.assertNotIn(
                'value="%s" selected' % self.esc(self.encoded(other)[0]),
                html,
            )

    def test_initial_objects_interpreted_by_full_key(self):
        form = ShelfForm(
            instance=Shelf(name="shelf"),
            initial={"books": [self.book_comma_1, self.book_comma_2]},
        )
        html = str(form["books"])
        for expected in self.encoded(self.book_comma_1, self.book_comma_2):
            self.assertIn('value="%s" selected' % self.esc(expected), html)

    def test_rebuilding_form_keeps_same_result(self):
        shelf = self.make_shelf(self.books["@"], self.books["héllo世界"])
        rendered = [str(ShelfForm(instance=shelf)["books"]) for _ in range(3)]
        self.assertEqual(rendered[1:], rendered[:-1])


class ValidSubmissionTests(CompositePKM2MFormBase):
    def test_cleaned_data_is_exact_existing_targets(self):
        shelf = self.make_shelf()
        targets = [self.books[""], self.books["None"], self.book_comma_2]
        form = ShelfForm(
            {"name": "shelf", "books": self.encoded(*targets)}, instance=shelf
        )
        self.assertTrue(form.is_valid(), form.errors)
        self.assertCountEqual(form.cleaned_data["books"], targets)
        cleaned_pks = set(form.cleaned_data["books"].values_list("pk", flat=True))
        self.assertEqual(cleaned_pks, {target.pk for target in targets})

    def test_save_seen_by_forward_reverse_and_cross_queries(self):
        shelf = self.make_shelf(self.books["/"])
        other_shelf = self.make_shelf(self.books["/"])
        targets = [self.book_comma_1, self.books[""], self.books["héllo世界"]]
        form = ShelfForm(
            {"name": "shelf", "books": self.encoded(*targets)}, instance=shelf
        )
        self.assertTrue(form.is_valid(), form.errors)
        form.save()

        shelf.refresh_from_db()
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
        targets = [self.book_comma_1, self.books["42"]]
        data = {"name": "shelf", "books": self.encoded(*targets)}
        for _ in range(3):
            form = ShelfForm(data, instance=Shelf.objects.get(pk=shelf.pk))
            self.assertTrue(form.is_valid(), form.errors)
            form.save()
        shelf.refresh_from_db()
        self.assertCountEqual(shelf.books.all(), targets)
        self.assertEqual(
            Shelf.books.through.objects.filter(shelf=shelf).count(), len(targets)
        )

    def test_save_only_replaces_this_relation(self):
        tag_1 = Tag.objects.create(name="tag 1")
        tag_2 = Tag.objects.create(name="tag 2")
        shelf = self.make_shelf(self.books[","], tags=(tag_1, tag_2))
        form = ShelfForm(
            {
                "name": "shelf",
                "books": self.encoded(self.books["@"], self.books[""]),
            },
            instance=shelf,
        )
        self.assertTrue(form.is_valid(), form.errors)
        form.save()
        shelf.refresh_from_db()
        self.assertCountEqual(shelf.books.all(), [self.books["@"], self.books[""]])
        # The other many-to-many field of the same object is unchanged.
        self.assertCountEqual(shelf.tags.all(), [tag_1, tag_2])

    def test_legal_replace_keeps_target_sharing_a_component(self):
        shelf = self.make_shelf(self.book_comma_1)
        other_shelf = self.make_shelf(self.book_comma_2)
        form = ShelfForm(
            {"name": "shelf", "books": self.encoded(self.book_comma_2)}, instance=shelf
        )
        self.assertTrue(form.is_valid(), form.errors)
        form.save()
        # (1, ",") still exists, keeps its primary key and its other links.
        self.assertEqual(
            Book.objects.get(pk=self.book_comma_1.pk).pk, self.book_comma_1.pk
        )
        other_shelf.refresh_from_db()
        self.assertCountEqual(other_shelf.books.all(), [self.book_comma_2])
        shelf.refresh_from_db()
        self.assertCountEqual(shelf.books.all(), [self.book_comma_2])

    def test_saved_set_rendered_with_same_key(self):
        shelf = self.make_shelf(self.books["\\"], self.books["/"])
        form = ShelfForm(
            {
                "name": "shelf",
                "books": self.encoded(self.books["\\"], self.books["/"]),
            },
            instance=shelf,
        )
        self.assertTrue(form.is_valid(), form.errors)
        form.save()
        fresh = ShelfForm(instance=Shelf.objects.get(pk=shelf.pk))
        html = str(fresh["books"])
        for expected in self.encoded(self.books["\\"], self.books["/"]):
            self.assertIn('value="%s" selected' % self.esc(expected), html)

    def test_has_changed_uses_full_key(self):
        field = ShelfForm.base_fields["books"]
        comma_pk = self.book_comma_1.pk
        encoded = self.encoded(self.book_comma_1)[0]
        self.assertFalse(field.has_changed([comma_pk], [encoded]))
        self.assertTrue(field.has_changed([comma_pk], self.encoded(self.book_comma_2)))

    def test_optional_form_can_clear_relation(self):
        tag = Tag.objects.create(name="keep")
        shelf = self.make_shelf(self.book_comma_1, tags=(tag,))
        OptionalForm = modelform_factory(Shelf, fields=["name", "books"])
        OptionalForm.base_fields["books"].required = False

        # An unselected multiple select omits the key from submitted data.
        form = OptionalForm({"name": "shelf"}, instance=shelf)
        self.assertTrue(form.is_valid(), form.errors)
        form.save()
        shelf.refresh_from_db()
        self.assertFalse(shelf.books.exists())
        # The other relation is untouched.
        self.assertCountEqual(shelf.tags.all(), [tag])


class InvalidSubmissionTests(CompositePKM2MFormBase):
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

    def assert_invalid_choice(self, form, field_name="books"):
        self.assertFalse(form.is_valid())
        codes = {
            error.code
            for error in form.errors.as_data()[field_name]
            for error in error.error_list
        }
        self.assertEqual(codes, {"invalid_choice"})
        with self.assertRaises(ValueError):
            form.save()

    def test_invalid_values(self):
        shelf = self.make_shelf()
        through = Shelf.books.through
        for value in self.INVALID_VALUES:
            with self.subTest(value=value):
                before = set(through.objects.values_list("book_tenant_id", "book_isbn"))
                form = ShelfForm({"name": "shelf", "books": [value]}, instance=shelf)
                self.assert_invalid_choice(form)
                # Nothing was written to the through table.
                after = set(through.objects.values_list("book_tenant_id", "book_isbn"))
                self.assertEqual(before, after)
                shelf.refresh_from_db()
                self.assertFalse(shelf.books.exists())

    def test_one_bad_value_among_many_is_atomic(self):
        # Prefetch the current relation before the failed submission.
        shelf = Shelf.objects.prefetch_related("books").get(
            pk=self.make_shelf(self.book_comma_1, self.books[""]).pk
        )
        prefetched = list(shelf.books.all())
        original_pks = set(shelf.books.values_list("pk", flat=True))
        through = Shelf.books.through
        before_rows = set(
            through.objects.values_list("shelf_id", "book_tenant_id", "book_isbn")
        )

        data = {
            "name": "changed name",
            "books": [
                *self.encoded(self.book_comma_1, self.books[""], self.books["None"]),
                '["1", "no-such-isbn"]',
            ],
        }
        form = ShelfForm(data, instance=shelf)
        self.assertFalse(form.is_valid())
        self.assertIn("invalid_choice", self.first_code(form))
        with self.assertRaises(ValueError):
            form.save()

        # Through table, the instance relation, its scalar fields and the
        # prefetched collection all stay in their pre-operation state: no
        # partial replacement, not even of the submitted scalar field.
        self.assertEqual(
            set(through.objects.values_list("shelf_id", "book_tenant_id", "book_isbn")),
            before_rows,
        )
        shelf.refresh_from_db()
        self.assertEqual(shelf.name, "shelf")
        self.assertEqual(set(shelf.books.values_list("pk", flat=True)), original_pks)
        self.assertEqual(list(shelf.books.all()), prefetched)

        # Re-read with a fresh form on the same instance: the original set.
        fresh = ShelfForm(instance=Shelf.objects.get(pk=shelf.pk))
        html = str(fresh["books"])
        for expected in self.encoded(self.book_comma_1, self.books[""]):
            self.assertIn('value="%s" selected' % self.esc(expected), html)
        self.assertNotIn("no-such-isbn", html)

    @staticmethod
    def first_code(form):
        errors = form.errors.as_data()["books"]
        return {error.code for batch in errors for error in batch.error_list}

    def test_retry_after_failure_succeeds(self):
        shelf = self.make_shelf(self.books[""])
        bad_data = {
            "name": "shelf",
            "books": [*self.encoded(self.book_comma_1), '["1", "ghost"]'],
        }
        form = ShelfForm(bad_data, instance=shelf)
        self.assertFalse(form.is_valid())

        # Retry with the corrected full key on a freshly built form.
        good_data = {
            "name": "shelf",
            "books": self.encoded(self.book_comma_1, self.books[""]),
        }
        form = ShelfForm(good_data, instance=Shelf.objects.get(pk=shelf.pk))
        self.assertTrue(form.is_valid(), form.errors)
        form.save()
        shelf.refresh_from_db()
        self.assertCountEqual(shelf.books.all(), [self.book_comma_1, self.books[""]])

    def test_redisplay_after_error_keeps_submitted_full_key(self):
        shelf = self.make_shelf(self.books[""])
        data = {
            "name": "shelf",
            "books": [*self.encoded(self.book_comma_1), '["1", "ghost"]'],
        }
        form = ShelfForm(data, instance=shelf)
        self.assertFalse(form.is_valid())
        # The still-valid submitted value remains selected, keyed identically.
        html = str(form["books"])
        good = self.encoded(self.book_comma_1)[0]
        self.assertIn('value="%s" selected' % self.esc(good), html)
        # The instance relation was not modified.
        shelf.refresh_from_db()
        self.assertCountEqual(shelf.books.all(), [self.books[""]])


class DeletionTests(CompositePKM2MFormBase):
    def test_choices_and_relation_after_delete(self):
        doomed = self.books["@"]
        keeper = self.books[""]
        shelf = self.make_shelf(doomed, keeper)
        doomed_pk = doomed.pk
        encoded_doomed = self.encoded(doomed)[0]
        doomed.delete()

        # Regenerated choices no longer list the deleted target.
        form = ShelfForm(instance=Shelf.objects.get(pk=shelf.pk))
        values = [str(value) for value, _ in form.fields["books"].choices]
        self.assertNotIn(encoded_doomed, values)
        # The relation disappears with the target by cascade.
        shelf.refresh_from_db()
        self.assertCountEqual(shelf.books.all(), [keeper])
        self.assertFalse(
            Shelf.books.through.objects.filter(
                book_tenant_id=doomed_pk[0], book_isbn=doomed_pk[1]
            ).exists()
        )

        # A stale value pointing at the deleted row is still invalid_choice.
        form = ShelfForm(
            {"name": "shelf", "books": [encoded_doomed]},
            instance=Shelf.objects.get(pk=shelf.pk),
        )
        self.assertFalse(form.is_valid())
        codes = {
            error.code
            for batch in form.errors.as_data()["books"]
            for error in batch.error_list
        }
        self.assertEqual(codes, {"invalid_choice"})


class ModelChoiceFieldCompositeTests(CompositePKM2MFormBase):
    def test_single_value_roundtrip(self):
        field = ModelChoiceField(queryset=Book.objects.all())
        book = self.book_comma_1
        encoded = Book._meta.pk.value_to_string(book.pk)
        self.assertEqual(field.prepare_value(book), encoded)
        self.assertEqual(field.to_python(encoded), book)

    def test_invalid_single_value(self):
        field = ModelChoiceField(queryset=Book.objects.all())
        for value in ["garbled", "[1]", '["1", "ghost"]', '["x", "y"]']:
            with self.subTest(value=value):
                with self.assertRaises(ValidationError) as cm:
                    field.to_python(value)
                self.assertEqual(cm.exception.code, "invalid_choice")


class SinglePrimaryKeyCompatTests(TestCase):
    """Single-field primary key relations keep their existing semantics."""

    @classmethod
    def setUpTestData(cls):
        cls.tag_1 = Tag.objects.create(id=1, name="tag 1")
        cls.tag_2 = Tag.objects.create(id=2, name="tag 2")
        cls.tag_3 = Tag.objects.create(id=3, name="tag 3")

    def test_choices_clean_save_errors_and_cache(self):
        bookmark = Bookmark.objects.create(name="bm")
        form = BookmarkForm(instance=bookmark)
        self.assertCountEqual(
            [str(value) for value, _ in form.fields["tags"].choices], ["1", "2", "3"]
        )

        form = BookmarkForm({"name": "bm", "tags": ["1", "3"]}, instance=bookmark)
        self.assertTrue(form.is_valid(), form.errors)
        form.save()
        bookmark.refresh_from_db()
        self.assertCountEqual(bookmark.tags.all(), [self.tag_1, self.tag_3])
        self.assertCountEqual(self.tag_1.bookmarks.all(), [bookmark])

        # Repeated save is idempotent.
        form = BookmarkForm({"name": "bm", "tags": ["1", "3"]}, instance=bookmark)
        form.is_valid()
        form.save()
        self.assertEqual(bookmark.tags.count(), 2)

        # Unknown id -> invalid_choice; unconvertible id -> invalid_pk_value.
        form = BookmarkForm({"name": "bm", "tags": ["999"]}, instance=bookmark)
        self.assertFalse(form.is_valid())
        self.assertEqual(
            {e.code for b in form.errors.as_data()["tags"] for e in b.error_list},
            {"invalid_choice"},
        )
        form = BookmarkForm({"name": "bm", "tags": ["abc"]}, instance=bookmark)
        self.assertFalse(form.is_valid())
        self.assertEqual(
            {e.code for b in form.errors.as_data()["tags"] for e in b.error_list},
            {"invalid_pk_value"},
        )
        bookmark.refresh_from_db()
        self.assertCountEqual(bookmark.tags.all(), [self.tag_1, self.tag_3])
