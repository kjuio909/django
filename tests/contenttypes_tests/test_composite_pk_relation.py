import unittest

from django.contrib.contenttypes.models import ContentType
from django.db import connection
from django.db.models import Q
from django.test import TestCase
from django.test.utils import CaptureQueriesContext

from .models import (
    Answer,
    GFKCompositeAttachment,
    GFKCompositeTarget,
    Question,
)


class CompositeGenericRelationQueryBase(TestCase):
    @classmethod
    def setUpTestData(cls):
        # Two targets that share exactly one primary key component (code=1).
        cls.target_a = GFKCompositeTarget.objects.create(code=1, tenant="1")
        cls.target_b = GFKCompositeTarget.objects.create(code=1, tenant="2")
        # A third target that has no source at all.
        cls.target_c = GFKCompositeTarget.objects.create(code=2, tenant="1")
        # Sources cross-referencing the targets. target_a is referenced twice.
        cls.source_a1 = GFKCompositeAttachment.objects.create(
            text="a1", n=10, content_object=cls.target_a
        )
        cls.source_a2 = GFKCompositeAttachment.objects.create(
            text="a2", n=20, content_object=cls.target_a
        )
        cls.source_b = GFKCompositeAttachment.objects.create(
            text="b", n=30, content_object=cls.target_b
        )
        cls.content_type = ContentType.objects.get_for_model(GFKCompositeTarget)

    def _targets(self, qs):
        return set(qs.values_list("code", "tenant"))

    def _pks(self, targets):
        return {target.pk for target in targets}


@unittest.skipUnless(connection.vendor == "sqlite", "SQLite-only cross relation tests")
class CompositeGenericRelationFilterTests(CompositeGenericRelationQueryBase):
    def test_filter_returns_each_target_at_most_once(self):
        targets = list(
            GFKCompositeTarget.objects.filter(
                attachments__text__startswith="a"
            ).order_by("pk")
        )
        # target_a has two matching sources but appears exactly once.
        self.assertEqual(self._pks(targets), {self.target_a.pk})

    def test_count_equals_number_of_returned_targets(self):
        qs = GFKCompositeTarget.objects.filter(attachments__n__gte=10)
        self.assertEqual(qs.count(), len(list(qs)))
        self.assertEqual(
            self._targets(qs),
            {self.target_a.pk, self.target_b.pk},
        )

    def test_target_with_two_sources_is_counted_once(self):
        qs = GFKCompositeTarget.objects.filter(
            Q(attachments__text="a1") | Q(attachments__text="a2")
        )
        self.assertEqual(qs.count(), 1)
        self.assertEqual(list(qs), [self.target_a])

    def test_exact_lookup(self):
        self.assertEqual(
            self._targets(GFKCompositeTarget.objects.filter(attachments__text="a1")),
            {self.target_a.pk},
        )

    def test_comparison_lookups(self):
        self.assertEqual(
            self._targets(GFKCompositeTarget.objects.filter(attachments__n__gt=15)),
            {self.target_a.pk, self.target_b.pk},
        )
        self.assertEqual(
            self._targets(GFKCompositeTarget.objects.filter(attachments__n__gte=30)),
            {self.target_b.pk},
        )
        self.assertEqual(
            self._targets(GFKCompositeTarget.objects.filter(attachments__n__lt=15)),
            {self.target_a.pk},
        )
        self.assertEqual(
            self._targets(GFKCompositeTarget.objects.filter(attachments__n__lte=10)),
            {self.target_a.pk},
        )

    def test_range_lookup(self):
        self.assertEqual(
            self._targets(
                GFKCompositeTarget.objects.filter(attachments__n__range=(15, 25))
            ),
            {self.target_a.pk},
        )
        self.assertEqual(
            self._targets(
                GFKCompositeTarget.objects.filter(attachments__n__range=(15, 30))
            ),
            {self.target_a.pk, self.target_b.pk},
        )

    def test_multiple_source_conditions_are_and_ed(self):
        qs = GFKCompositeTarget.objects.filter(
            attachments__text="a1", attachments__n=10
        )
        self.assertEqual(self._targets(qs), {self.target_a.pk})
        # Relaxing just one condition must not broaden the result.
        self.assertFalse(
            GFKCompositeTarget.objects.filter(
                attachments__text="a1", attachments__n=99
            ).exists()
        )

    def test_target_and_source_conditions_both_hold(self):
        # The shared code=1 must not let a source match the other target.
        qs = GFKCompositeTarget.objects.filter(code=1, tenant="1", attachments__n=30)
        self.assertFalse(qs.exists())
        qs = GFKCompositeTarget.objects.filter(code=1, tenant="2", attachments__n=30)
        self.assertEqual(self._targets(qs), {self.target_b.pk})

    def test_cross_query_combines_with_queryset_filter(self):
        base = GFKCompositeTarget.objects.filter(code=1)
        self.assertEqual(
            self._targets(base.filter(attachments__n__gte=20)),
            {self.target_a.pk, self.target_b.pk},
        )
        self.assertEqual(
            self._targets(base.filter(attachments__n__gte=20, tenant="1")),
            {self.target_a.pk},
        )

    def test_results_independent_of_traversal_order_and_source_count(self):
        qs = GFKCompositeTarget.objects.filter(attachments__n__gte=10)
        unordered = self._targets(qs)
        for ordering in ("code", "-code", "tenant", "-tenant", "pk", "-pk"):
            self.assertEqual(self._targets(qs.order_by(ordering)), unordered)
        # Adding another matching source to target_b doesn't change the set of
        # targets or inflate the count.
        GFKCompositeAttachment.objects.create(
            text="b2", n=31, content_object=self.target_b
        )
        self.assertEqual(qs.count(), 2)
        self.assertEqual(unordered, {self.target_a.pk, self.target_b.pk})

    def test_shared_component_is_not_enough_to_match(self):
        # A reference sharing code=1 but with an unrelated tenant must not
        # match either target through the cross relation.
        GFKCompositeAttachment.objects.create(text="stranger")
        GFKCompositeAttachment.objects.filter(text="stranger").update(
            object_id='[1, "someone-else"]', content_type=self.content_type
        )
        qs = GFKCompositeTarget.objects.filter(attachments__text="stranger")
        self.assertFalse(qs.exists())
        self.assertEqual(qs.count(), 0)


@unittest.skipUnless(connection.vendor == "sqlite", "SQLite-only cross relation tests")
class CompositeGenericRelationExcludeTests(CompositeGenericRelationQueryBase):
    def test_exclude_drops_only_targets_with_a_matching_source(self):
        self.assertEqual(
            self._targets(GFKCompositeTarget.objects.exclude(attachments__text="a1")),
            {self.target_b.pk, self.target_c.pk},
        )

    def test_exclude_target_with_two_sources_is_still_one_exclusion(self):
        self.assertEqual(
            self._targets(
                GFKCompositeTarget.objects.exclude(
                    Q(attachments__text="a1") | Q(attachments__text="a2")
                )
            ),
            {self.target_b.pk, self.target_c.pk},
        )

    def test_exclude_comparison(self):
        self.assertEqual(
            self._targets(
                GFKCompositeTarget.objects.exclude(attachments__n__gte=15)
            ),
            # target_a (n=10/20) and target_b (n=30) both have a high source;
            # only target_c, with no source, survives.
            {self.target_c.pk},
        )

    def test_exclude_does_not_match_nonexistent_source(self):
        self.assertEqual(
            GFKCompositeTarget.objects.exclude(attachments__text="zzz").count(),
            3,
        )

    def test_exclude_combined_with_filter(self):
        self.assertEqual(
            self._targets(
                GFKCompositeTarget.objects.filter(code=1).exclude(
                    attachments__text="a1"
                )
            ),
            {self.target_b.pk},
        )

    def test_exclude_leaves_sources_in_place(self):
        GFKCompositeTarget.objects.exclude(attachments__text="a1")
        self.assertEqual(GFKCompositeAttachment.objects.count(), 3)


@unittest.skipUnless(connection.vendor == "sqlite", "SQLite-only cross relation tests")
class CompositeGenericRelationInvalidReferenceTests(CompositeGenericRelationQueryBase):
    """Bad references never participate in the join and never raise."""

    def _set_reference(self, source, reference):
        if reference is None:
            GFKCompositeAttachment.objects.filter(pk=source.pk).update(
                object_id=None, content_type=self.content_type
            )
        else:
            GFKCompositeAttachment.objects.filter(pk=source.pk).update(
                object_id=reference, content_type=self.content_type
            )
        source.refresh_from_db()

    def _assert_row_unchanged(self, source, reference):
        row = (
            GFKCompositeAttachment.objects.filter(pk=source.pk)
            .values("content_type_id", "object_id")
            .get()
        )
        self.assertEqual(row["content_type_id"], self.content_type.pk)
        self.assertEqual(row["object_id"], reference)

    def _assert_reference_is_ignored(self, source, reference):
        self._set_reference(source, reference)
        # Database state is read back verbatim before querying.
        self._assert_row_unchanged(source, reference)
        # No target is reached through the broken reference.
        self.assertFalse(
            GFKCompositeTarget.objects.filter(
                attachments__pk=source.pk
            ).exists()
        )
        # Excluding it doesn't exclude any target.
        self.assertEqual(
            GFKCompositeTarget.objects.exclude(
                attachments__pk=source.pk
            ).count(),
            3,
        )
        # Counting matches is unaffected.
        self.assertEqual(
            GFKCompositeTarget.objects.filter(attachments__n__gte=0).count(),
            2,
        )
        # ...and the database row is still untouched afterwards.
        self._assert_row_unchanged(source, reference)

    def test_empty_reference_is_ignored(self):
        source = GFKCompositeAttachment.objects.create(text="empty")
        self._assert_reference_is_ignored(source, None)

    def test_bad_json_is_ignored(self):
        source = GFKCompositeAttachment.objects.create(text="bad")
        self._assert_reference_is_ignored(source, "{not json")

    def test_wrong_component_count_is_ignored(self):
        source = GFKCompositeAttachment.objects.create(text="short")
        self._assert_reference_is_ignored(source, "[1]")
        self._assert_reference_is_ignored(source, '[1, "x", 3]')

    def test_conversion_failure_is_ignored(self):
        source = GFKCompositeAttachment.objects.create(text="conv")
        # The first component is an integer field; a string can't convert.
        self._assert_reference_is_ignored(source, '["oops", "1"]')

    def test_null_element_does_not_match_not_null_column(self):
        source = GFKCompositeAttachment.objects.create(text="null-elem")
        self._assert_reference_is_ignored(source, "[1, null]")

    def test_wrong_content_type_is_ignored(self):
        Question.objects.create(text="q")
        source = GFKCompositeAttachment.objects.create(text="wrong-ct")
        GFKCompositeAttachment.objects.filter(pk=source.pk).update(
            object_id='[1, "1"]',
            content_type=ContentType.objects.get_for_model(Question),
        )
        source.refresh_from_db()
        # A reference whose values happen to equal target_a's key must not
        # match because the content type points at a different model.
        self.assertFalse(
            GFKCompositeTarget.objects.filter(
                attachments__pk=source.pk
            ).exists()
        )
        self.assertEqual(
            GFKCompositeTarget.objects.filter(attachments__n__gte=0).count(),
            2,
        )

    def test_nonexistent_object_is_ignored(self):
        source = GFKCompositeAttachment.objects.create(text="missing")
        self._assert_reference_is_ignored(source, '[7, "missing"]')

    def test_shared_component_does_not_rescue_bad_reference(self):
        source = GFKCompositeAttachment.objects.create(text="shared")
        # Shares code=1 with both real targets, but the tenant is unknown.
        self._assert_reference_is_ignored(source, '[1, "someone-else"]')

    def test_empty_array_is_ignored(self):
        source = GFKCompositeAttachment.objects.create(text="empty-array")
        self._assert_reference_is_ignored(source, "[]")

    def test_bad_references_never_raise_while_evaluating_all_targets(self):
        for reference in (
            None,
            "",
            "[]",
            "{bad",
            "[1]",
            '[1, "x", 3]',
            '["oops", "1"]',
            "123",
            '"a scalar"',
            '{"code": 1}',
            "[1, null]",
            "[1, 1]",
            "@",
            "None",
        ):
            source = GFKCompositeAttachment.objects.create(text="g")
            self._set_reference(source, reference)
            # Filtering, excluding, counting and prefetching all succeed.
            with self.subTest(reference=reference):
                self.assertFalse(
                    GFKCompositeTarget.objects.filter(
                        attachments__pk=source.pk
                    ).exists()
                )
                list(
                    GFKCompositeTarget.objects.exclude(
                        attachments__pk=source.pk
                    )
                )
                GFKCompositeTarget.objects.filter(attachments__n__isnull=False)
                list(
                    GFKCompositeTarget.objects.prefetch_related("attachments")
                )


@unittest.skipUnless(connection.vendor == "sqlite", "SQLite-only cross relation tests")
class CompositeGenericRelationSpecialValuesTests(TestCase):
    """
    Special component values are stored, read back and matched through the
    cross relation without confusion between the values.
    """

    SPECIAL_TENANTS = [
        "",  # empty string
        "None",  # the literal string None
        "literal@at",  # a literal @
        "a/b",  # a slash
        "a,b",  # a comma
        "café ★ → 日本語",  # unicode
        'quote "x"',
    ]

    def _round_trip(self, tenant, code):
        target = GFKCompositeTarget.objects.create(code=code, tenant=tenant)
        source = GFKCompositeAttachment.objects.create(
            text=tenant, n=code, content_object=target
        )
        # Read the raw reference back from the database before doing anything.
        source.refresh_from_db()
        raw = (
            GFKCompositeAttachment.objects.filter(pk=source.pk)
            .values("object_id", "content_type_id")
            .get()
        )
        return target, source, raw

    def test_special_values_filter_exclude_count_prefetch(self):
        created = []
        for code, tenant in enumerate(self.SPECIAL_TENANTS, start=10):
            target, source, raw = self._round_trip(tenant, code)
            created.append((code, tenant, target, source, raw))

        for code, tenant, target, source, raw in created:
            with self.subTest(tenant=tenant):
                # Direct read agrees with the stored reference.
                self.assertEqual(
                    GFKCompositeAttachment.objects.get(pk=source.pk).object_id,
                    raw["object_id"],
                )
                source.refresh_from_db()
                self.assertEqual(source.content_object, target)

                # Filter through the reverse relation hits exactly this target.
                qs = GFKCompositeTarget.objects.filter(attachments__n=code)
                self.assertEqual(qs.count(), 1)
                self.assertEqual(list(qs), [target])

                # Excluding a different source's value keeps this target.
                self.assertIn(
                    target.pk,
                    set(
                        GFKCompositeTarget.objects.exclude(
                            attachments__n=code + 1000
                        ).values_list("code", "tenant")
                    ),
                )
                # Excluding this source's value removes exactly this target.
                self.assertNotIn(
                    target.pk,
                    set(
                        GFKCompositeTarget.objects.exclude(
                            attachments__n=code
                        ).values_list("code", "tenant")
                    ),
                )

        # Prefetch attributes sources to the right targets regardless of the
        # characters used in the components.
        prefetched = list(
            GFKCompositeTarget.objects.filter(
                attachments__n__gte=10
            ).prefetch_related("attachments")
        )
        by_pk = {target.pk: target for target in prefetched}
        for code, tenant, target, source, raw in created:
            self.assertEqual(list(by_pk[target.pk].attachments.all()), [source])

        # Delete each source; the corresponding target is afterwards excluded
        # by the cross relation while the others remain.
        for code, tenant, target, source, raw in created:
            source.delete()
            self.assertFalse(
                GFKCompositeTarget.objects.filter(attachments__n=code).exists()
            )
            self.assertTrue(
                GFKCompositeTarget.objects.filter(pk=target.pk).exists()
            )


@unittest.skipUnless(connection.vendor == "sqlite", "SQLite-only cross relation tests")
class CompositeGenericRelationConsistencyTests(CompositeGenericRelationQueryBase):
    def test_three_entry_points_return_the_same_object(self):
        # Direct read.
        self.assertEqual(
            GFKCompositeAttachment.objects.get(pk=self.source_a1.pk).content_object,
            self.target_a,
        )
        # Prefetch.
        prefetched = {
            source.pk: source.content_object
            for source in GFKCompositeAttachment.objects.prefetch_related(
                "content_object"
            )
        }
        self.assertEqual(prefetched[self.source_a1.pk], self.target_a)
        self.assertEqual(prefetched[self.source_b.pk], self.target_b)
        # Cross relation query.
        self.assertEqual(
            GFKCompositeTarget.objects.filter(attachments__text="a1").get(),
            self.target_a,
        )

    def test_reverse_prefetch_attributes_sources_correctly(self):
        targets = list(
            GFKCompositeTarget.objects.prefetch_related("attachments").order_by("pk")
        )
        result = {target.pk: set(target.attachments.all()) for target in targets}
        self.assertEqual(result[self.target_a.pk], {self.source_a1, self.source_a2})
        self.assertEqual(result[self.target_b.pk], {self.source_b})
        self.assertEqual(result[self.target_c.pk], set())

    def test_prefetch_after_cross_filter(self):
        targets = list(
            GFKCompositeTarget.objects.filter(attachments__text__startswith="a")
            .prefetch_related("attachments")
            .order_by("pk")
        )
        self.assertEqual([target.pk for target in targets], [self.target_a.pk])
        self.assertEqual(
            set(targets[0].attachments.all()), {self.source_a1, self.source_a2}
        )

    def test_direct_manager_access_matches_cross_query(self):
        self.assertEqual(
            set(self.target_a.attachments.all()),
            {self.source_a1, self.source_a2},
        )
        self.assertEqual(set(self.target_b.attachments.all()), {self.source_b})
        self.assertEqual(set(self.target_c.attachments.all()), set())

    def test_delete_source_keeps_target_and_other_sources(self):
        self.source_a1.delete()
        # a2 still references target_a.
        self.assertEqual(
            self._targets(
                GFKCompositeTarget.objects.filter(
                    attachments__text__startswith="a"
                )
            ),
            {self.target_a.pk},
        )
        self.assertTrue(
            GFKCompositeTarget.objects.filter(pk=self.target_a.pk).exists()
        )

    def test_delete_target_cascades_only_to_its_sources(self):
        self.target_a.delete()
        self.assertFalse(
            GFKCompositeAttachment.objects.filter(
                pk__in=[self.source_a1.pk, self.source_a2.pk]
            ).exists()
        )
        self.assertTrue(
            GFKCompositeAttachment.objects.filter(pk=self.source_b.pk).exists()
        )
        self.assertTrue(
            GFKCompositeTarget.objects.filter(pk=self.target_b.pk).exists()
        )

    def test_queries_are_read_only(self):
        target_count = GFKCompositeTarget.objects.count()
        source_count = GFKCompositeAttachment.objects.count()
        with CaptureQueriesContext(connection) as context:
            list(GFKCompositeTarget.objects.filter(attachments__text="a1"))
            GFKCompositeTarget.objects.filter(attachments__text="a1").count()
            list(GFKCompositeTarget.objects.exclude(attachments__text="a1"))
            list(
                GFKCompositeTarget.objects.prefetch_related("attachments")
            )
        for query in context.captured_queries:
            self.assertTrue(query["sql"].upper().startswith("SELECT"), query["sql"])
        self.assertEqual(GFKCompositeTarget.objects.count(), target_count)
        self.assertEqual(GFKCompositeAttachment.objects.count(), source_count)


@unittest.skipUnless(connection.vendor == "sqlite", "SQLite-only cross relation tests")
class SinglePrimaryKeyGenericRelationUnchangedTests(TestCase):
    """Single-field primary key generic relations keep their existing shape."""

    @classmethod
    def setUpTestData(cls):
        cls.question = Question.objects.create(text="q")
        cls.answer_1 = Answer.objects.create(text="a", question=cls.question)
        cls.answer_2 = Answer.objects.create(text="b", question=cls.question)
        cls.other_question = Question.objects.create(text="other")
        Answer.objects.create(text="c", question=cls.other_question)

    def test_existing_querying_and_prefetch_unchanged(self):
        # The reverse manager and prefetch still work.
        self.assertEqual(
            set(self.question.answer_set.all()), {self.answer_1, self.answer_2}
        )
        prefetched = Question.objects.prefetch_related("answer_set").get(
            pk=self.question.pk
        )
        self.assertEqual(
            set(prefetched.answer_set.all()), {self.answer_1, self.answer_2}
        )
        # Cross relation filtering works (and doesn't force distinct).
        self.assertTrue(
            Question.objects.filter(answer_set__text="a").exists()
        )
        self.assertFalse(
            Question.objects.filter(answer_set__text="missing").exists()
        )
        # Exclude still keeps the unrelated question.
        self.assertIn(
            self.other_question,
            Question.objects.exclude(answer_set__text="a"),
        )

    def test_single_pk_relation_is_not_force_distinct(self):
        query = Question.objects.filter(answer_set__text="a").query
        self.assertFalse(query.distinct)
