import json

from django.contrib.contenttypes.models import ContentType
from django.db.models import Count, Prefetch, Q
from django.test import TestCase

from .models import Entry, Gadget, Tenant, Widget


class CompositePKGenericRelationReportTests(TestCase):
    """
    Report-style queries (annotate, values projection, ordering, slicing)
    over a GenericRelation from a composite-primary-key target.
    """

    maxDiff = None

    @classmethod
    def setUpTestData(cls):
        cls.tenant_1 = Tenant.objects.create(name="t1")
        cls.tenant_2 = Tenant.objects.create(name="t2")
        # Two targets sharing one composite key component ("shared"), plus a
        # third target with a unique component.
        cls.widget_1 = Widget.objects.create(
            tenant=cls.tenant_1, id="shared", name="alpha"
        )
        cls.widget_2 = Widget.objects.create(
            tenant=cls.tenant_2, id="shared", name="beta"
        )
        cls.widget_3 = Widget.objects.create(
            tenant=cls.tenant_1, id="unique", name="gamma"
        )
        cls.widget_ct = ContentType.objects.get_for_model(Widget)

        # Sources are cross-written: widget_1 and widget_2 share the "shared"
        # id component, so a component-wise join would mix them up.
        for widget, labels in [
            (cls.widget_1, ["hot", "hot", "cold"]),
            (cls.widget_2, ["hot"]),
            (cls.widget_3, ["cold"]),
        ]:
            for label in labels:
                Entry.objects.create(content_object=widget, label=label)

    def report(self, **kwargs):
        return (
            Widget.objects.annotate(source_count=Count("entries"))
            .values("tenant_id", "id", "name", "source_count")
            .order_by("-source_count", "tenant_id", "id")
        )

    def test_report_projects_full_pk_and_count(self):
        self.assertEqual(
            list(self.report()),
            [
                {
                    "tenant_id": self.tenant_1.id,
                    "id": "shared",
                    "name": "alpha",
                    "source_count": 3,
                },
                {
                    "tenant_id": self.tenant_1.id,
                    "id": "unique",
                    "name": "gamma",
                    "source_count": 1,
                },
                {
                    "tenant_id": self.tenant_2.id,
                    "id": "shared",
                    "name": "beta",
                    "source_count": 1,
                },
            ],
        )

    def test_report_filter_by_source_label(self):
        self.assertEqual(
            list(
                Widget.objects.filter(entries__label="hot")
                .annotate(source_count=Count("entries", filter=Q(entries__label="hot")))
                .values("tenant_id", "id", "name", "source_count")
                .order_by("-source_count", "tenant_id", "id")
            ),
            [
                {
                    "tenant_id": self.tenant_1.id,
                    "id": "shared",
                    "name": "alpha",
                    "source_count": 2,
                },
                {
                    "tenant_id": self.tenant_2.id,
                    "id": "shared",
                    "name": "beta",
                    "source_count": 1,
                },
            ],
        )

    def test_report_combines_target_and_source_conditions(self):
        # Both conditions apply to the same target; the other target sharing
        # the "shared" component is not affected.
        self.assertEqual(
            list(
                Widget.objects.filter(name="alpha", entries__label="hot")
                .annotate(source_count=Count("entries", filter=Q(entries__label="hot")))
                .values("tenant_id", "id", "name", "source_count")
            ),
            [
                {
                    "tenant_id": self.tenant_1.id,
                    "id": "shared",
                    "name": "alpha",
                    "source_count": 2,
                }
            ],
        )

    def test_report_filter_join_and_count_join_do_not_multiply(self):
        # The filter join and the aggregate join on the same relation must
        # not multiply the count.
        self.assertEqual(
            list(
                Widget.objects.filter(entries__label="hot")
                .annotate(source_count=Count("entries"))
                .values("tenant_id", "id", "source_count")
                .order_by("tenant_id", "id")
            ),
            [
                {"tenant_id": self.tenant_1.id, "id": "shared", "source_count": 2},
                {"tenant_id": self.tenant_2.id, "id": "shared", "source_count": 1},
            ],
        )

    def test_report_reevaluation_filtering_and_slicing_are_stable(self):
        qs = self.report()
        first = list(qs)
        self.assertEqual(list(qs), first)
        self.assertEqual(list(qs.filter(source_count__gte=1)), first)
        self.assertEqual(list(qs[:2]), first[:2])
        self.assertEqual(list(qs[1:]), first[1:])

    def test_report_zero_count_targets_kept_or_excluded(self):
        Widget.objects.create(tenant=self.tenant_2, id="lonely", name="lonely")
        counts = {row["id"]: row["source_count"] for row in self.report()}
        self.assertEqual(counts["lonely"], 0)
        # A filter on the sources excludes targets without matching sources.
        self.assertNotIn(
            "lonely",
            {
                row["id"]
                for row in Widget.objects.filter(entries__label="hot").values("id")
            },
        )

    def test_prefetch_matches_report_and_direct_access(self):
        widgets = {
            w.pk: w
            for w in Widget.objects.annotate(source_count=Count("entries"))
            .prefetch_related("entries")
            .order_by("tenant_id", "id")
        }
        self.assertEqual(
            {pk: sorted(e.label for e in w.entries.all()) for pk, w in widgets.items()},
            {
                self.widget_1.pk: ["cold", "hot", "hot"],
                self.widget_2.pk: ["hot"],
                self.widget_3.pk: ["cold"],
            },
        )
        for pk, w in widgets.items():
            with self.subTest(pk=pk):
                self.assertEqual(w.source_count, len(w.entries.all()))
                self.assertEqual(w.source_count, w.entries.count())
                fresh = Widget.objects.get(pk=pk)
                self.assertEqual(
                    sorted(e.label for e in fresh.entries.all()),
                    sorted(e.label for e in w.entries.all()),
                )

    def test_prefetch_with_custom_queryset(self):
        widgets = (
            Widget.objects.prefetch_related(
                Prefetch("entries", queryset=Entry.objects.filter(label="hot"))
            )
            .order_by("tenant_id", "id")
        )
        self.assertEqual(
            {w.pk: [e.label for e in w.entries.all()] for w in widgets},
            {
                self.widget_1.pk: ["hot", "hot"],
                self.widget_2.pk: ["hot"],
                self.widget_3.pk: [],
            },
        )

    def test_reverse_filter_by_target_instance(self):
        self.assertEqual(
            list(
                Entry.objects.filter(widget=self.widget_1)
                .values_list("label", flat=True)
                .order_by("label")
            ),
            ["cold", "hot", "hot"],
        )
        self.assertEqual(
            list(
                Entry.objects.filter(widget=self.widget_2)
                .values_list("label", flat=True)
            ),
            ["hot"],
        )

    def test_reverse_filter_by_target_instance_in(self):
        self.assertEqual(
            list(
                Entry.objects.filter(widget__in=[self.widget_1, self.widget_2])
                .values_list("label", flat=True)
                .order_by("label")
            ),
            ["cold", "hot", "hot", "hot"],
        )

    def test_reverse_filter_by_target_subquery(self):
        self.assertEqual(
            list(
                Entry.objects.filter(
                    widget__in=Widget.objects.filter(tenant=self.tenant_1)
                )
                .values_list("label", flat=True)
                .order_by("label")
            ),
            ["cold", "cold", "hot", "hot"],
        )

    def test_delete_source_only_reduces_owning_target(self):
        def counts():
            return {
                (row["tenant_id"], row["id"]): row["source_count"]
                for row in self.report()
            }

        before = counts()
        Entry.objects.filter(widget=self.widget_1, label="hot").first().delete()
        after = counts()
        self.assertEqual(after[self.widget_1.pk], before[self.widget_1.pk] - 1)
        self.assertEqual(after[self.widget_2.pk], before[self.widget_2.pk])
        self.assertEqual(after[self.widget_3.pk], before[self.widget_3.pk])

    def test_delete_target_cascades_to_its_sources_only(self):
        entry_count = Entry.objects.count()
        self.widget_1.delete()
        self.assertEqual(Entry.objects.count(), entry_count - 3)
        self.assertEqual(Widget.objects.count(), 2)
        self.assertEqual(
            list(
                Widget.objects.annotate(source_count=Count("entries"))
                .values("tenant_id", "id", "source_count")
                .order_by("tenant_id", "id")
            ),
            [
                {"tenant_id": self.tenant_1.id, "id": "unique", "source_count": 1},
                {"tenant_id": self.tenant_2.id, "id": "shared", "source_count": 1},
            ],
        )


class CompositePKGenericRelationSpecialComponentTests(TestCase):
    """
    References whose components are empty strings, "None", or contain
    characters significant to the encoding still resolve to a unique target.
    """

    @classmethod
    def setUpTestData(cls):
        cls.tenant = Tenant.objects.create(name="t")
        cls.ids = ["", "None", "@", "a/b", "x,y", "日本語", "plain"]
        cls.widgets = {
            id_: Widget.objects.create(tenant=cls.tenant, id=id_, name=f"w{i}")
            for i, id_ in enumerate(cls.ids)
        }
        for widget in cls.widgets.values():
            Entry.objects.create(content_object=widget, label="hot")

    def test_report_locates_each_target(self):
        self.assertEqual(
            {
                row["id"]: row["source_count"]
                for row in Widget.objects.annotate(source_count=Count("entries"))
                .values("id", "source_count")
            },
            {id_: 1 for id_ in self.ids},
        )

    def test_filter_and_projection_locate_unique_target(self):
        for id_ in self.ids:
            with self.subTest(id=id_):
                widget = Widget.objects.get(pk=(self.tenant.id, id_))
                self.assertEqual(
                    list(
                        Entry.objects.filter(widget=widget).values_list(
                            "label", flat=True
                        )
                    ),
                    ["hot"],
                )
                self.assertEqual(
                    [e.label for e in widget.entries.all()],
                    ["hot"],
                )

    def test_prefetch_locates_each_target(self):
        self.assertEqual(
            {
                w.id: [e.label for e in w.entries.all()]
                for w in Widget.objects.prefetch_related("entries")
            },
            {id_: ["hot"] for id_ in self.ids},
        )


class CompositePKGenericRelationInvalidReferenceTests(TestCase):
    """
    Sources whose stored reference is malformed, points at a missing target,
    or has the wrong content type never match, and don't break reporting,
    ordering, counting, or prefetching for the remaining rows.
    """

    @classmethod
    def setUpTestData(cls):
        cls.tenant = Tenant.objects.create(name="t")
        cls.widget = Widget.objects.create(tenant=cls.tenant, id="ok", name="ok")
        cls.other_widget = Widget.objects.create(
            tenant=cls.tenant, id="other", name="other"
        )
        cls.widget_ct = ContentType.objects.get_for_model(Widget)
        cls.gadget_ct = ContentType.objects.get_for_model(Gadget)
        Entry.objects.create(content_object=cls.widget, label="hot")
        cls.valid_reference = json.dumps([cls.tenant.id, "ok"])

    def make_invalid_entries(self):
        widget_ct = self.widget_ct
        return [
            # Not valid JSON.
            Entry.objects.create(content_type=widget_ct, object_id="not json"),
            # Valid JSON, but not an array.
            Entry.objects.create(content_type=widget_ct, object_id='{"a": 1}'),
            # Too few components.
            Entry.objects.create(content_type=widget_ct, object_id='["ok"]'),
            # Too many components.
            Entry.objects.create(
                content_type=widget_ct, object_id='["ok", 1, "extra"]'
            ),
            # A component of the wrong JSON type.
            Entry.objects.create(content_type=widget_ct, object_id='[1.5, "ok"]'),
            # A well-formed reference to a nonexistent target.
            Entry.objects.create(
                content_type=widget_ct, object_id='["ok", 99999]'
            ),
            # An empty reference.
            Entry.objects.create(content_type=widget_ct, object_id=""),
            Entry.objects.create(content_type=widget_ct, object_id=None),
            # A reference with the wrong content type. Its object_id is valid
            # for the Gadget primary key but no Gadget with it exists, so it
            # matches no target and doesn't bleed into the Widget report.
            Entry.objects.create(
                content_type=self.gadget_ct, object_id="99999"
            ),
        ]

    def test_invalid_references_do_not_match(self):
        self.make_invalid_entries()
        self.assertEqual(
            list(
                Widget.objects.annotate(source_count=Count("entries"))
                .values("id", "source_count")
                .order_by("id")
            ),
            [
                {"id": "ok", "source_count": 1},
                {"id": "other", "source_count": 0},
            ],
        )
        self.assertEqual(
            list(
                Widget.objects.filter(entries__label="hot")
                .values_list("id", flat=True)
                .distinct()
            ),
            ["ok"],
        )

    def test_invalid_references_do_not_break_prefetch(self):
        self.make_invalid_entries()
        self.assertEqual(
            {
                w.id: [e.label for e in w.entries.all()]
                for w in Widget.objects.prefetch_related("entries")
            },
            {"ok": ["hot"], "other": []},
        )

    def test_invalid_references_resolve_to_none(self):
        self.make_invalid_entries()
        entries = Entry.objects.filter(content_type=self.widget_ct).exclude(
            object_id=self.valid_reference
        )
        for entry in entries:
            with self.subTest(object_id=entry.object_id):
                self.assertIsNone(entry.content_object)
        # The valid reference still resolves, and its cache is populated.
        valid = Entry.objects.get(
            content_type=self.widget_ct, object_id=self.valid_reference
        )
        self.assertEqual(valid.content_object, self.widget)

    def test_invalid_references_prefetch_content_object(self):
        invalid = self.make_invalid_entries()
        valid = Entry.objects.get(
            content_type=self.widget_ct, object_id=self.valid_reference
        )
        resolved = {
            entry.pk: entry.content_object
            for entry in Entry.objects.prefetch_related("content_object")
        }
        self.assertEqual(resolved.pop(valid.pk), self.widget)
        for entry in invalid:
            with self.subTest(object_id=entry.object_id):
                self.assertIsNone(resolved.pop(entry.pk))
        self.assertEqual(resolved, {})

    def test_invalid_references_do_not_write_or_change_caches(self):
        self.make_invalid_entries()
        entry_count = Entry.objects.count()
        for entry in Entry.objects.filter(content_type=self.widget_ct):
            entry.content_object
        self.assertEqual(Entry.objects.count(), entry_count)

    def test_invalid_references_are_unresolved_in_reverse_lookups(self):
        invalid = self.make_invalid_entries()
        valid = Entry.objects.get(
            content_type=self.widget_ct, object_id=self.valid_reference
        )
        # Unresolved references show up through the reverse relation's isnull
        # lookup and survive an exclude by the target.
        unresolved = set(
            Entry.objects.filter(widget__isnull=True).values_list("pk", flat=True)
        )
        self.assertIn(
            valid.pk,
            set(
                Entry.objects.filter(widget__isnull=False).values_list(
                    "pk", flat=True
                )
            ),
        )
        for entry in invalid:
            with self.subTest(object_id=entry.object_id):
                self.assertIn(entry.pk, unresolved)
        excluded = set(
            Entry.objects.exclude(widget=self.widget).values_list("pk", flat=True)
        )
        self.assertNotIn(valid.pk, excluded)


class SinglePKGenericRelationReportTests(TestCase):
    """The same report queries keep working for single-field primary keys."""

    @classmethod
    def setUpTestData(cls):
        cls.gadget_1 = Gadget.objects.create(name="g1")
        cls.gadget_2 = Gadget.objects.create(name="g2")
        for gadget, labels in [
            (cls.gadget_1, ["hot", "hot", "cold"]),
            (cls.gadget_2, ["cold"]),
        ]:
            for label in labels:
                Entry.objects.create(content_object=gadget, label=label)

    def test_report(self):
        qs = (
            Gadget.objects.annotate(source_count=Count("entries"))
            .values("id", "name", "source_count")
            .order_by("-source_count", "id")
        )
        expected = [
            {"id": self.gadget_1.id, "name": "g1", "source_count": 3},
            {"id": self.gadget_2.id, "name": "g2", "source_count": 1},
        ]
        self.assertEqual(list(qs), expected)
        self.assertEqual(list(qs), expected)
        self.assertEqual(list(qs[:1]), expected[:1])
        self.assertEqual(list(qs.filter(source_count__gte=1)), expected)

    def test_reverse_filter_and_prefetch(self):
        self.assertEqual(
            list(
                Entry.objects.filter(gadget=self.gadget_1)
                .values_list("label", flat=True)
                .order_by("label")
            ),
            ["cold", "hot", "hot"],
        )
        self.assertEqual(
            {
                g.name: sorted(e.label for e in g.entries.all())
                for g in Gadget.objects.prefetch_related("entries")
            },
            {"g1": ["cold", "hot", "hot"], "g2": ["cold"]},
        )

    def test_delete_source_and_target(self):
        Entry.objects.filter(gadget=self.gadget_1, label="hot").first().delete()
        self.assertEqual(
            Gadget.objects.get(pk=self.gadget_1.pk).entries.count(), 2
        )
        self.gadget_1.delete()
        self.assertEqual(Gadget.objects.count(), 1)
        self.assertEqual(
            list(Entry.objects.values_list("label", flat=True)), ["cold"]
        )
