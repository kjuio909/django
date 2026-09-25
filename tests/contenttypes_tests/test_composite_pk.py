from django.contrib.contenttypes.fields import GenericForeignKey
from django.contrib.contenttypes.models import ContentType
from django.contrib.contenttypes.prefetch import GenericPrefetch
from django.db import connection
from django.db.models import Count, Q
from django.test import TestCase
from django.test.utils import CaptureQueriesContext

from .models import (
    Answer,
    GFKCompositeAttachment,
    GFKCompositeDateTarget,
    GFKCompositeTarget,
    Question,
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


class GFKCompositeRelationQueryTestCase(TestCase):
    """
    Queries across the reverse generic relation (GenericRelation) of a model
    whose primary key is composite. The join must happen in the database.
    """

    @classmethod
    def setUpTestData(cls):
        # Two targets sharing exactly one primary key component (code=1),
        # plus a third sharing the other component (tenant="1") with the
        # first one.
        cls.target_a = GFKCompositeTarget.objects.create(code=1, tenant="1")
        cls.target_b = GFKCompositeTarget.objects.create(code=1, tenant="2")
        cls.target_c = GFKCompositeTarget.objects.create(code=2, tenant="1")
        # Sources are saved crosswise; target_a has two matching sources,
        # target_b has one, and target_c has none.
        cls.source_apple = GFKCompositeAttachment.objects.create(
            text="apple", content_object=cls.target_a
        )
        cls.source_apricot = GFKCompositeAttachment.objects.create(
            text="apricot", content_object=cls.target_a
        )
        cls.source_banana = GFKCompositeAttachment.objects.create(
            text="banana", content_object=cls.target_b
        )

    def _snapshot_database(self):
        sources = list(
            GFKCompositeAttachment.objects.order_by("pk").values(
                "pk", "text", "content_type_id", "object_id"
            )
        )
        targets = list(
            GFKCompositeTarget.objects.order_by("pk").values("code", "tenant")
        )
        return sources, targets

    def test_filter_by_source_field_returns_each_target_once(self):
        # target_a has two matching sources; it must be counted only once.
        qs = GFKCompositeTarget.objects.filter(
            attachments__text__startswith="a"
        ).distinct()
        with self.assertNumQueries(1):
            result = list(qs)
        self.assertEqual(result, [self.target_a])
        self.assertEqual(qs.count(), 1)
        self.assertEqual(qs.count(), len(result))
        # The shared code=1 component doesn't pull in target_b.
        self.assertNotIn(self.target_b, result)

    def test_filter_exact_comparison_and_range(self):
        self.assertEqual(
            list(GFKCompositeTarget.objects.filter(attachments__text="banana")),
            [self.target_b],
        )
        self.assertEqual(
            list(
                GFKCompositeTarget.objects.filter(
                    attachments__id__gt=self.source_apricot.pk
                )
            ),
            [self.target_b],
        )
        self.assertEqual(
            list(
                GFKCompositeTarget.objects.filter(
                    attachments__id__lt=self.source_apricot.pk
                )
            ),
            [self.target_a],
        )
        self.assertEqual(
            list(
                GFKCompositeTarget.objects.filter(
                    attachments__id__range=(
                        self.source_apple.pk,
                        self.source_apricot.pk,
                    )
                ).distinct()
            ),
            [self.target_a],
        )
        self.assertCountEqual(
            GFKCompositeTarget.objects.filter(
                attachments__text__in=["apple", "banana"]
            ).distinct(),
            [self.target_a, self.target_b],
        )
        # Multiple conditions on the source combine on the same join.
        self.assertEqual(
            list(
                GFKCompositeTarget.objects.filter(
                    attachments__text__startswith="a",
                    attachments__id__gte=self.source_apple.pk,
                ).distinct()
            ),
            [self.target_a],
        )

    def test_target_and_source_conditions_must_both_hold(self):
        self.assertEqual(
            list(
                GFKCompositeTarget.objects.filter(
                    tenant="1", attachments__text="apple"
                )
            ),
            [self.target_a],
        )
        # The source condition alone would match target_a, but its tenant
        # is "1", not "2"; neither condition may be relaxed.
        self.assertEqual(
            list(
                GFKCompositeTarget.objects.filter(
                    tenant="2", attachments__text="apple"
                )
            ),
            [],
        )
        self.assertEqual(
            list(
                GFKCompositeTarget.objects.filter(
                    Q(code=2) & Q(attachments__text="apple")
                )
            ),
            [],
        )
        # Cross-relation filters compose with regular queryset filters.
        self.assertEqual(
            list(
                GFKCompositeTarget.objects.filter(tenant="1").filter(
                    attachments__text="apple"
                )
            ),
            [self.target_a],
        )
        self.assertEqual(
            list(
                GFKCompositeTarget.objects.filter(
                    Q(attachments__text="banana") | Q(tenant="1")
                ).distinct()
            ),
            [self.target_a, self.target_b, self.target_c],
        )

    def test_exclude_only_excludes_targets_with_matching_sources(self):
        self.assertCountEqual(
            GFKCompositeTarget.objects.exclude(attachments__text="apple"),
            [self.target_b, self.target_c],
        )
        # target_a is excluded once even though two of its sources match.
        self.assertCountEqual(
            GFKCompositeTarget.objects.exclude(attachments__text__startswith="a"),
            [self.target_b, self.target_c],
        )
        # Nothing matches, so nothing is excluded.
        self.assertCountEqual(
            GFKCompositeTarget.objects.exclude(attachments__text="missing"),
            [self.target_a, self.target_b, self.target_c],
        )
        # Target and source conditions must hold together in the subquery.
        self.assertCountEqual(
            GFKCompositeTarget.objects.exclude(
                Q(attachments__text="apple") & Q(tenant="1")
            ),
            [self.target_b, self.target_c],
        )
        self.assertCountEqual(
            GFKCompositeTarget.objects.exclude(
                Q(attachments__text="apple") & Q(tenant="2")
            ),
            [self.target_a, self.target_b, self.target_c],
        )

    def test_prefetch_attributes_sources_to_correct_targets(self):
        targets = list(
            GFKCompositeTarget.objects.filter(attachments__text__startswith="a")
            .distinct()
            .prefetch_related("attachments")
        )
        self.assertEqual(targets, [self.target_a])
        # Both matching sources belong to target_a, none to the target that
        # shares its code component.
        self.assertCountEqual(
            targets[0].attachments.all(), [self.source_apple, self.source_apricot]
        )
        all_targets = {
            target.pk: list(target.attachments.all())
            for target in GFKCompositeTarget.objects.prefetch_related("attachments")
        }
        self.assertCountEqual(
            all_targets[(1, "1")], [self.source_apple, self.source_apricot]
        )
        self.assertEqual(all_targets[(1, "2")], [self.source_banana])
        self.assertEqual(all_targets[(2, "1")], [])

    def test_filter_prefetch_and_direct_read_agree(self):
        filtered = GFKCompositeTarget.objects.filter(attachments__text="apple").get()
        prefetched = {
            source.text: source.content_object
            for source in GFKCompositeAttachment.objects.prefetch_related(
                "content_object"
            )
        }
        self.assertEqual(filtered, self.target_a)
        self.assertEqual(prefetched["apple"], filtered)
        self.source_apple.refresh_from_db()
        self.assertEqual(self.source_apple.content_object, filtered)

    def test_annotate_count_across_relation(self):
        counts = {
            target.pk: target.source_count
            for target in GFKCompositeTarget.objects.annotate(
                source_count=Count("attachments")
            )
        }
        self.assertEqual(counts, {(1, "1"): 2, (1, "2"): 1, (2, "1"): 0})

    def test_invalid_references_are_not_joined(self):
        other_content_type = ContentType.objects.get_for_model(GFKCompositeDateTarget)
        content_type = ContentType.objects.get_for_model(GFKCompositeTarget)
        cases = {
            # Null reference: neither content type nor object id.
            "null-reference": (None, None),
            # Null reference with a content type set.
            "null-object-id": (content_type, None),
            # Content type of another model, well-formed reference.
            "wrong-content-type": (other_content_type, '[1, "1"]'),
            # Not JSON at all.
            "bad-json": (content_type, "{not json"),
            # Wrong number of components.
            "too-few-components": (content_type, "[1]"),
            "too-many-components": (content_type, '[1, "1", 3]'),
            # A component that can't be converted to the field type.
            "conversion-failure": (content_type, '["oops", "1"]'),
            # Well-formed but no such target.
            "nonexistent-target": (content_type, '[7, "missing"]'),
            # Shares code=1 with real targets but matches none of them.
            "shared-component-only": (content_type, '[1, "someone-else"]'),
            # A null component can't match the NOT NULL tenant column.
            "null-component": (content_type, "[1, null]"),
            # An integer can't stand in for the string tenant.
            "type-confused": (content_type, "[1, 1]"),
        }
        sources = [
            GFKCompositeAttachment.objects.create(text=text) for text in cases
        ]
        for source in sources:
            content_type_value, object_id = cases[source.text]
            GFKCompositeAttachment.objects.filter(pk=source.pk).update(
                content_type=content_type_value, object_id=object_id
            )
        before = self._snapshot_database()
        texts = list(cases)
        with self.subTest("filter"):
            self.assertEqual(
                list(GFKCompositeTarget.objects.filter(attachments__text__in=texts)),
                [],
            )
            self.assertEqual(
                GFKCompositeTarget.objects.filter(attachments__text__in=texts).count(),
                0,
            )
        with self.subTest("exclude"):
            self.assertCountEqual(
                GFKCompositeTarget.objects.exclude(attachments__text__in=texts),
                [self.target_a, self.target_b, self.target_c],
            )
        with self.subTest("prefetch"):
            prefetched = {
                source.text: source.content_object
                for source in GFKCompositeAttachment.objects.filter(
                    text__in=texts
                ).prefetch_related("content_object")
            }
            self.assertEqual(prefetched, {text: None for text in texts})
        # The broken rows were left untouched and no query wrote anything.
        self.assertEqual(self._snapshot_database(), before)

    def test_special_value_components(self):
        special_tenants = [
            "",
            "None",
            "literal@at",
            "a/b",
            "a,b",
            "café ★ → 日本語",
            'quote "x"',
        ]
        content_type = ContentType.objects.get_for_model(GFKCompositeTarget)
        for index, tenant in enumerate(special_tenants, start=10):
            target = GFKCompositeTarget.objects.create(code=index, tenant=tenant)
            # A sibling sharing the code component must never be matched.
            GFKCompositeTarget.objects.create(code=index, tenant="other")
            GFKCompositeAttachment.objects.create(
                text="ref-%s" % index, content_object=target
            )
        before = self._snapshot_database()
        for index, tenant in enumerate(special_tenants, start=10):
            with self.subTest(tenant=tenant):
                source = GFKCompositeAttachment.objects.get(text="ref-%s" % index)
                # The reference round-trips through the database unchanged.
                self.assertEqual(
                    source.object_id,
                    GenericForeignKey.encode_composite_pk(
                        GFKCompositeTarget._meta.pk, (index, tenant)
                    ),
                )
                self.assertEqual(source.content_type_id, content_type.pk)
                self.assertEqual(source.content_object.pk, (index, tenant))
                filtered = list(
                    GFKCompositeTarget.objects.filter(
                        attachments__text="ref-%s" % index
                    )
                )
                self.assertEqual(
                    filtered,
                    [GFKCompositeTarget.objects.get(code=index, tenant=tenant)],
                )
                excluded = GFKCompositeTarget.objects.exclude(
                    attachments__text="ref-%s" % index
                )
                self.assertNotIn((index, tenant), [t.pk for t in excluded])
                self.assertIn(
                    (index, "other"), [t.pk for t in excluded],
                )
        self.assertEqual(self._snapshot_database(), before)

    def test_empty_string_component_is_not_confused_with_null(self):
        target = GFKCompositeTarget.objects.create(code=20, tenant="")
        source = GFKCompositeAttachment.objects.create(
            text="empty", content_object=target
        )
        # A null reference doesn't match the empty-string component.
        GFKCompositeAttachment.objects.create(text="nullref")
        self.assertEqual(
            list(GFKCompositeTarget.objects.filter(attachments__text="empty")),
            [target],
        )
        self.assertEqual(
            list(GFKCompositeTarget.objects.filter(attachments__text="nullref")),
            [],
        )
        source.refresh_from_db()
        self.assertEqual(source.object_id, '[20, ""]')
        self.assertEqual(source.content_object, target)

    def test_queries_do_not_write_to_the_database(self):
        before = self._snapshot_database()
        with CaptureQueriesContext(connection) as captured:
            list(GFKCompositeTarget.objects.filter(attachments__text="apple"))
            list(GFKCompositeTarget.objects.exclude(attachments__text="apple"))
            GFKCompositeTarget.objects.filter(
                attachments__text__startswith="a"
            ).distinct().count()
            list(GFKCompositeTarget.objects.prefetch_related("attachments"))
            list(GFKCompositeAttachment.objects.prefetch_related("content_object"))
        for query in captured.captured_queries:
            self.assertFalse(
                query["sql"]
                .lstrip("(")
                .upper()
                .startswith(("INSERT", "UPDATE", "DELETE", "CREATE", "DROP")),
                query["sql"],
            )
        self.assertEqual(self._snapshot_database(), before)

    def test_delete_source_updates_query_results(self):
        self.source_apple.delete()
        self.assertEqual(
            list(GFKCompositeTarget.objects.filter(attachments__text="apple")),
            [],
        )
        # target_a still matches through its remaining source.
        self.assertEqual(
            list(
                GFKCompositeTarget.objects.filter(
                    attachments__text__startswith="a"
                ).distinct()
            ),
            [self.target_a],
        )
        self.assertTrue(
            GFKCompositeTarget.objects.filter(pk=self.target_a.pk).exists()
        )

    def test_delete_target_cascades_only_to_its_sources(self):
        self.target_a.delete()
        self.assertFalse(
            GFKCompositeAttachment.objects.filter(
                pk__in=[self.source_apple.pk, self.source_apricot.pk]
            ).exists()
        )
        self.assertTrue(
            GFKCompositeAttachment.objects.filter(pk=self.source_banana.pk).exists()
        )
        self.assertEqual(
            list(GFKCompositeTarget.objects.filter(attachments__text="banana")),
            [self.target_b],
        )
        self.assertCountEqual(
            GFKCompositeTarget.objects.exclude(attachments__text="banana"),
            [self.target_c],
        )


class SinglePKGenericRelationQueryTestCase(TestCase):
    """Single-field primary key generic relation queries are unchanged."""

    @classmethod
    def setUpTestData(cls):
        cls.question = Question.objects.create(text="q")
        cls.other_question = Question.objects.create(text="other")
        cls.answer_a = Answer.objects.create(text="apple", question=cls.question)
        cls.answer_b = Answer.objects.create(text="apricot", question=cls.question)

    def test_filter_dedup_count_and_prefetch(self):
        qs = Question.objects.filter(answer_set__text__startswith="a").distinct()
        self.assertEqual(list(qs), [self.question])
        self.assertEqual(qs.count(), 1)
        questions = list(
            Question.objects.prefetch_related("answer_set").order_by("pk")
        )
        self.assertCountEqual(
            questions[0].answer_set.all(), [self.answer_a, self.answer_b]
        )
        self.assertEqual(list(questions[1].answer_set.all()), [])

    def test_exclude(self):
        self.assertEqual(
            list(Question.objects.exclude(answer_set__text="apple")),
            [self.other_question],
        )
