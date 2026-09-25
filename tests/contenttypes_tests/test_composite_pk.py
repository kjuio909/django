from django.contrib.contenttypes.models import ContentType
from django.contrib.contenttypes.prefetch import GenericPrefetch
from django.db import connection
from django.test import TestCase

from .models import (
    GFKCompositeAttachment,
    GFKCompositeDateTarget,
    GFKCompositeTarget,
)


class GFKCompositeTargetTestCase(TestCase):
    @classmethod
    def setUpTestData(cls):
        # Two targets sharing exactly one primary key component (code=1).
        cls.target_a = GFKCompositeTarget.objects.create(code=1, tenant="1")
        cls.target_b = GFKCompositeTarget.objects.create(code=1, tenant="2")
        # Two sources cross-referencing them.
        cls.source_a = GFKCompositeAttachment.objects.create(
            text="a", content_object=cls.target_a
        )
        cls.source_b = GFKCompositeAttachment.objects.create(
            text="b", content_object=cls.target_b
        )
        cls.content_type = ContentType.objects.get_for_model(GFKCompositeTarget)

    def _reload_sources(self):
        self.source_a.refresh_from_db()
        self.source_b.refresh_from_db()

    def test_reference_is_stored_as_json_array_text(self):
        self.assertEqual(
            GFKCompositeAttachment.objects.get(pk=self.source_a.pk).object_id,
            '[1, "1"]',
        )
        self.assertEqual(
            GFKCompositeAttachment.objects.get(pk=self.source_b.pk).object_id,
            '[1, "2"]',
        )

    def test_read_returns_unique_target(self):
        self._reload_sources()
        self.assertEqual(self.source_a.content_object, self.target_a)
        self.assertEqual(self.source_b.content_object, self.target_b)
        # They must never be confused, even though code=1 is shared.
        self.assertNotEqual(
            self.source_a.content_object.pk, self.source_b.content_object.pk
        )

    def test_integer_and_numeric_string_are_distinct_components(self):
        # A target whose components are (int 1, string "1") must not match a
        # tampered reference [1, 1] (int 1, int 1).
        GFKCompositeAttachment.objects.filter(pk=self.source_a.pk).update(
            object_id="[1, 1]", content_type=self.content_type
        )
        self.source_a.refresh_from_db()
        self.assertIsNone(self.source_a.content_object)

    def test_query_by_reference_does_not_match_the_other_target(self):
        ref_a = GFKCompositeAttachment.objects.get(pk=self.source_a.pk).object_id
        ref_b = GFKCompositeAttachment.objects.get(pk=self.source_b.pk).object_id
        self.assertEqual(
            GFKCompositeAttachment.objects.filter(object_id=ref_a).count(), 1
        )
        self.assertEqual(
            GFKCompositeAttachment.objects.filter(object_id=ref_b).count(), 1
        )

    def test_prefetch_returns_unique_targets(self):
        sources = list(
            GFKCompositeAttachment.objects.prefetch_related("content_object").order_by(
                "pk"
            )
        )
        by_text = {source.text: source.content_object for source in sources}
        self.assertEqual(by_text["a"], self.target_a)
        self.assertEqual(by_text["b"], self.target_b)
        # Repeated access keeps returning the same fetched instance.
        self.assertIs(sources[0].content_object, sources[0].content_object)

    def test_reverse_relation_is_isolated(self):
        self.assertEqual(list(self.target_a.attachments.all()), [self.source_a])
        self.assertEqual(list(self.target_b.attachments.all()), [self.source_b])

    def test_prefetch_reverse_relation(self):
        targets = list(
            GFKCompositeTarget.objects.prefetch_related("attachments").order_by("pk")
        )
        result = {target.pk: list(target.attachments.all()) for target in targets}
        self.assertEqual(result[(1, "1")], [self.source_a])
        self.assertEqual(result[(1, "2")], [self.source_b])

    def test_prefetch_with_custom_querysets(self):
        sources = list(
            GFKCompositeAttachment.objects.prefetch_related(
                GenericPrefetch(
                    "content_object",
                    [GFKCompositeTarget.objects.filter(tenant="1")],
                )
            ).order_by("pk")
        )
        by_text = {source.text: source.content_object for source in sources}
        self.assertEqual(by_text["a"], self.target_a)
        # target_b is excluded by the custom queryset.
        self.assertIsNone(by_text["b"])

    def test_delete_source_leaves_targets_and_other_source(self):
        self.source_a.delete()
        self.assertTrue(GFKCompositeTarget.objects.filter(pk=self.target_a.pk).exists())
        self.assertTrue(GFKCompositeTarget.objects.filter(pk=self.target_b.pk).exists())
        self.assertTrue(
            GFKCompositeAttachment.objects.filter(pk=self.source_b.pk).exists()
        )

    def test_delete_target_only_cascades_to_its_sources(self):
        self.target_a.delete()
        # The other target and its source are untouched.
        self.assertTrue(GFKCompositeTarget.objects.filter(pk=self.target_b.pk).exists())
        self.assertTrue(
            GFKCompositeAttachment.objects.filter(pk=self.source_b.pk).exists()
        )
        self.assertFalse(
            GFKCompositeAttachment.objects.filter(pk=self.source_a.pk).exists()
        )

    def test_empty_reference_clears_content_type(self):
        source = GFKCompositeAttachment.objects.create(
            text="none", content_object=self.target_a
        )
        source.content_object = None
        source.save()
        source.refresh_from_db()
        self.assertIsNone(source.object_id)
        self.assertIsNone(source.content_type_id)
        self.assertIsNone(source.content_object)

    def test_reassign_keeps_single_relation(self):
        self.source_a.content_object = self.target_b
        self.source_a.save()
        self.source_a.refresh_from_db()
        self.source_b.refresh_from_db()
        self.assertEqual(self.source_a.content_object, self.target_b)
        self.assertEqual(self.source_b.content_object, self.target_b)
        # No source points at target_a anymore.
        self.assertEqual(list(self.target_a.attachments.all()), [])
        self.assertEqual(
            list(self.target_b.attachments.order_by("pk")),
            list(GFKCompositeAttachment.objects.order_by("pk")),
        )

    def test_special_values_round_trip(self):
        special_tenants = [
            "",
            "None",
            "literal@at",
            "a/b",
            "a,b",
            "café ★ → 日本語",
            'quote "x"',
        ]
        sources = []
        for code, tenant in enumerate(special_tenants, start=10):
            target = GFKCompositeTarget.objects.create(code=code, tenant=tenant)
            sources.append(
                GFKCompositeAttachment.objects.create(
                    text=tenant, content_object=target
                )
            )

        # Save, refresh, read back one by one.
        for source, code, tenant in zip(
            sources, range(10, 10 + len(special_tenants)), special_tenants
        ):
            with self.subTest(tenant=tenant):
                source.refresh_from_db()
                self.assertEqual(source.content_object.tenant, tenant)
                self.assertEqual(source.content_object.code, code)

        # And all at once via prefetch.
        prefetched = {
            source.text: source.content_object
            for source in GFKCompositeAttachment.objects.filter(
                pk__in=[source.pk for source in sources]
            ).prefetch_related("content_object")
        }
        for tenant in special_tenants:
            self.assertEqual(prefetched[tenant].tenant, tenant)


class GFKInvalidReferenceTestCase(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.target = GFKCompositeTarget.objects.create(code=1, tenant="1")
        cls.content_type = ContentType.objects.get_for_model(GFKCompositeTarget)
        cls.source = GFKCompositeAttachment.objects.create(text="ok")
        GFKCompositeAttachment.objects.filter(pk=cls.source.pk).update(
            content_type=cls.content_type
        )

    def _set_reference(self, reference):
        GFKCompositeAttachment.objects.filter(pk=self.source.pk).update(
            object_id=reference
        )
        self.source.refresh_from_db()

    def _assert_row_unchanged(self, reference):
        row = (
            GFKCompositeAttachment.objects.filter(pk=self.source.pk)
            .values("content_type_id", "object_id")
            .get()
        )
        self.assertEqual(row["content_type_id"], self.content_type.pk)
        self.assertEqual(row["object_id"], reference)

    def test_nonexistent_target(self):
        self._set_reference('[7, "missing"]')
        self.assertIsNone(self.source.content_object)
        self._assert_row_unchanged('[7, "missing"]')

    def test_bad_json(self):
        self._set_reference("{not json")
        self.assertIsNone(self.source.content_object)
        self._assert_row_unchanged("{not json")

    def test_wrong_number_of_components(self):
        self._set_reference("[1]")
        self.assertIsNone(self.source.content_object)
        self._assert_row_unchanged("[1]")

        self._set_reference('[1, "x", 3]')
        self.assertIsNone(self.source.content_object)
        self._assert_row_unchanged('[1, "x", 3]')

    def test_conversion_failure(self):
        # The code component is an integer; a non-numeric string can't be
        # converted.
        self._set_reference('["oops", "1"]')
        self.assertIsNone(self.source.content_object)
        self._assert_row_unchanged('["oops", "1"]')

    def test_shared_component_still_does_not_match(self):
        # Shares code=1 with the real target but has a different tenant.
        self._set_reference('[1, "someone-else"]')
        self.assertIsNone(self.source.content_object)
        self._assert_row_unchanged('[1, "someone-else"]')

    def test_null_component(self):
        # None must be distinguished from both "None" and ""; it cannot match
        # a NOT NULL component.
        self._set_reference("[1, null]")
        self.assertIsNone(self.source.content_object)
        self._assert_row_unchanged("[1, null]")

    def test_json_object_is_not_a_valid_reference(self):
        self._set_reference('{"code": 1, "tenant": "1"}')
        self.assertIsNone(self.source.content_object)
        self._assert_row_unchanged('{"code": 1, "tenant": "1"}')

    def test_invalid_references_do_not_match_under_prefetch(self):
        good = GFKCompositeAttachment.objects.create(text="good")
        GFKCompositeAttachment.objects.filter(pk=good.pk).update(
            object_id='[1, "1"]', content_type=self.content_type
        )
        self._set_reference("{bad")
        result = {
            source.text: source.content_object
            for source in GFKCompositeAttachment.objects.prefetch_related(
                "content_object"
            ).filter(pk__in=[good.pk, self.source.pk])
        }
        self.assertEqual(result["good"], self.target)
        self.assertIsNone(result["ok"])

    def test_unencodable_target_is_rejected_in_constructor(self):
        # Building a reference to a target that can't be encoded raises a
        # ValueError rather than silently storing something unusable.
        date_target = GFKCompositeDateTarget.objects.create(id=1, day="2026-09-25")
        with self.assertRaisesMessage(ValueError, "unencodable"):
            GFKCompositeAttachment(content_object=date_target)

    def test_unsaved_target_is_rejected_without_mutation(self):
        source = GFKCompositeAttachment.objects.create(
            text="saved", content_object=self.target
        )
        source.refresh_from_db()
        original_ct = source.content_type_id
        original_ref = source.object_id

        unsaved = GFKCompositeTarget(code=2, tenant="unsaved")
        with self.assertRaisesMessage(ValueError, "unsaved"):
            source.content_object = unsaved

        # Failure doesn't rewrite the content type, the reference column, or
        # the cached relation.
        self.assertEqual(source.content_type_id, original_ct)
        self.assertEqual(source.object_id, original_ref)
        self.assertEqual(source.content_object, self.target)

        # And the database row is untouched.
        row = (
            GFKCompositeAttachment.objects.filter(pk=source.pk)
            .values("content_type_id", "object_id")
            .get()
        )
        self.assertEqual(row["content_type_id"], original_ct)
        self.assertEqual(row["object_id"], original_ref)

    def test_unencodable_target_is_rejected_without_mutation(self):
        source = GFKCompositeAttachment.objects.create(
            text="date",
        )
        date_target = GFKCompositeDateTarget.objects.create(id=1, day="2026-09-25")
        with self.assertRaisesMessage(ValueError, "unencodable"):
            source.content_object = date_target
        self.assertIsNone(source.content_type_id)
        self.assertIsNone(source.object_id)
        self.assertIsNone(source.content_object)

    def test_database_column_is_text(self):
        # The reference persists in a single text column.
        with connection.cursor() as cursor:
            cursor.execute(
                "PRAGMA table_info(%s)" % GFKCompositeAttachment._meta.db_table
            )
            column_types = {row[1]: row[2] for row in cursor.fetchall()}
        self.assertEqual(column_types["object_id"].upper(), "TEXT")
