import json

from django.core import serializers
from django.db import IntegrityError, connection
from django.db.models import Prefetch
from django.test import TestCase, TransactionTestCase

from .models import Board, Label, Member, Post, Tag, Tenant, User  # noqa: F401


class CompositePKM2MBase(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.tenant_1 = Tenant.objects.create(id=1, name="tenant 1")
        cls.tenant_2 = Tenant.objects.create(id=2, name="tenant 2")
        # member_1 (1, 1) and member_2 (2, 1) share their second component
        # but belong to different tenants.
        cls.member_1 = Member.objects.create(tenant=cls.tenant_1, id=1, name="m1")
        cls.member_2 = Member.objects.create(tenant=cls.tenant_2, id=1, name="m2")
        cls.member_3 = Member.objects.create(tenant=cls.tenant_1, id=2, name="m3")


class ManyToManyCompositeSourceTests(CompositePKM2MBase):
    """Many-to-many where the holder has a composite primary key."""

    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.tag_1 = Tag.objects.create(id=1, name="tag 1")
        cls.tag_2 = Tag.objects.create(id=2, name="tag 2")
        cls.tag_3 = Tag.objects.create(id=3, name="tag 3")

    def test_intermediary_model_columns(self):
        through = Member.tags.field.remote_field.through
        self.assertEqual(
            [field.column for field in through._meta.local_concrete_fields],
            ["id", "member_tenant_id", "member_id", "tag_id"],
        )
        member_field = through._meta.get_field("member")
        self.assertEqual(len(member_field.local_related_fields), 2)
        self.assertEqual(
            [field.column for field in member_field.local_related_fields],
            ["member_tenant_id", "member_id"],
        )
        self.assertEqual(
            through._meta.unique_together,
            (("member_tenant_id", "member_id", "tag"),),
        )

    def test_add_and_read(self):
        self.member_1.tags.add(self.tag_1, self.tag_2)
        # Sharing the second component with another object must not link it.
        self.member_2.tags.add(self.tag_3)

        self.assertCountEqual(
            self.member_1.tags.values_list("id", flat=True),
            [self.tag_1.id, self.tag_2.id],
        )
        self.assertSequenceEqual(
            self.member_2.tags.values_list("id", flat=True), [self.tag_3.id]
        )
        self.assertCountEqual(
            self.tag_1.tagged_members.values_list("pk", flat=True),
            [self.member_1.pk],
        )
        self.assertEqual(self.member_1.tags.count(), 2)

    def test_add_is_idempotent(self):
        self.member_1.tags.add(self.tag_1)
        self.member_1.tags.add(self.tag_1)
        self.member_1.tags.add(self.tag_1.id)
        self.assertEqual(self.member_1.tags.count(), 1)
        self.assertEqual(Member.tags.through.objects.count(), 1)

    def test_add_by_primary_key_value(self):
        self.member_1.tags.add(self.tag_2.id)
        self.assertSequenceEqual(
            self.member_1.tags.values_list("id", flat=True), [self.tag_2.id]
        )

    def test_add_nonexistent_target_fails_atomically(self):
        self.member_1.tags.add(self.tag_1)
        snapshot = list(
            Member.tags.through.objects.values_list(
                "member_tenant_id", "member_id", "tag_id"
            )
        )
        with self.assertRaises(Tag.DoesNotExist):
            self.member_1.tags.add(self.tag_1, 9999)
        self.assertEqual(
            list(
                Member.tags.through.objects.values_list(
                    "member_tenant_id", "member_id", "tag_id"
                )
            ),
            snapshot,
        )

    def test_unsaved_source_is_rejected(self):
        unsaved = Member(tenant=self.tenant_1)
        with self.assertRaises(ValueError):
            unsaved.tags.add(self.tag_1)
        self.assertEqual(Member.tags.through.objects.count(), 0)

    def test_remove(self):
        self.member_1.tags.add(self.tag_1, self.tag_2)
        self.member_2.tags.add(self.tag_1)
        self.member_1.tags.remove(self.tag_1)
        self.assertSequenceEqual(
            self.member_1.tags.values_list("id", flat=True), [self.tag_2.id]
        )
        # The other relation to the same target survives.
        self.assertSequenceEqual(
            self.member_2.tags.values_list("id", flat=True), [self.tag_1.id]
        )

    def test_clear(self):
        self.member_1.tags.add(self.tag_1)
        self.member_2.tags.add(self.tag_1)
        self.member_1.tags.clear()
        self.assertEqual(self.member_1.tags.count(), 0)
        self.assertEqual(self.member_2.tags.count(), 1)

    def test_set(self):
        self.member_1.tags.add(self.tag_1)
        self.member_1.tags.set([self.tag_2, self.tag_3])
        self.assertCountEqual(
            self.member_1.tags.values_list("id", flat=True),
            [self.tag_2.id, self.tag_3.id],
        )

    def test_set_clear(self):
        self.member_1.tags.add(self.tag_1)
        self.member_1.tags.set([self.tag_2], clear=True)
        self.assertSequenceEqual(
            self.member_1.tags.values_list("id", flat=True), [self.tag_2.id]
        )

    def test_set_failure_is_atomic(self):
        self.member_1.tags.add(self.tag_1, self.tag_2)
        snapshot = set(
            Member.tags.through.objects.values_list(
                "member_tenant_id", "member_id", "tag_id"
            )
        )
        with self.assertRaises(Tag.DoesNotExist):
            self.member_1.tags.set([self.tag_3, 9999])
        self.assertEqual(
            set(
                Member.tags.through.objects.values_list(
                    "member_tenant_id", "member_id", "tag_id"
                )
            ),
            snapshot,
        )

    def test_create(self):
        tag = self.member_1.tags.create(name="created")
        self.assertEqual(list(self.member_1.tags.all()), [tag])

    def test_get_or_create(self):
        # Existing target: no relation exists yet, so it is not added until
        # get_or_create creates it (which it doesn't), leaving the relation
        # unchanged.
        self.member_1.tags.add(self.tag_1)
        tag, created = self.member_1.tags.get_or_create(
            id=self.tag_1.id, defaults={"name": "tag 1"}
        )
        self.assertIs(created, False)
        self.assertEqual(tag, self.tag_1)
        self.assertEqual(self.member_1.tags.count(), 1)

        tag, created = self.member_1.tags.get_or_create(name="fresh")
        self.assertIs(created, True)
        self.assertIn(tag, self.member_1.tags.all())

    def test_update_or_create(self):
        self.member_1.tags.add(self.tag_1)
        tag, created = self.member_1.tags.update_or_create(
            id=self.tag_1.id, defaults={"name": "renamed"}
        )
        self.assertIs(created, False)
        self.tag_1.refresh_from_db()
        self.assertEqual(self.tag_1.name, "renamed")
        self.assertEqual(self.member_1.tags.count(), 1)

        tag, created = self.member_1.tags.update_or_create(
            name="new one", defaults={"id": 50}
        )
        self.assertIs(created, True)
        self.assertEqual(self.member_1.tags.count(), 2)

    def test_cross_relation_filtering(self):
        self.member_1.tags.add(self.tag_1)
        self.member_2.tags.add(self.tag_1)

        self.assertCountEqual(
            Tag.objects.filter(tagged_members=self.member_1).values_list(
                "id", flat=True
            ),
            [self.tag_1.id],
        )
        self.assertCountEqual(
            Member.objects.filter(tags=self.tag_1).values_list("pk", flat=True),
            [self.member_1.pk, self.member_2.pk],
        )
        # Filtering by the full composite key only matches the right member.
        self.assertSequenceEqual(
            Tag.objects.filter(tagged_members=self.member_1.pk).values_list(
                "id", flat=True
            ),
            [self.tag_1.id],
        )
        # A scalar (one shared component) cannot be used to filter a relation
        # that spans several columns: the full key is required.
        with self.assertRaises(ValueError):
            Tag.objects.filter(tagged_members=self.member_2.pk[0]).exists()

    def test_cross_relation_filter_in(self):
        self.member_1.tags.add(self.tag_1)
        self.member_2.tags.add(self.tag_2)

        tags = Tag.objects.filter(
            tagged_members__in=[self.member_1, self.member_2]
        ).distinct()
        self.assertCountEqual(tags.values_list("id", flat=True), [1, 2])
        members = Member.objects.filter(tags__in=[self.tag_1, self.tag_2]).distinct()
        self.assertCountEqual(members.values_list("pk", flat=True), [(1, 1), (2, 1)])

    def test_results_are_deduplicated(self):
        self.member_1.tags.add(self.tag_1)
        self.assertEqual(
            Tag.objects.filter(
                tagged_members__in=[self.member_1, self.member_1]
            ).distinct().count(),
            1,
        )

    def test_prefetch_forward(self):
        self.member_1.tags.add(self.tag_1, self.tag_2)
        self.member_2.tags.add(self.tag_3)

        members = list(
            Member.objects.prefetch_related("tags").order_by("tenant_id", "id")
        )
        result = {
            member.pk: set(member.tags.values_list("id", flat=True))
            for member in members
        }
        self.assertEqual(
            result,
            {
                (1, 1): {self.tag_1.id, self.tag_2.id},
                (1, 2): set(),
                (2, 1): {self.tag_3.id},
            },
        )

    def test_prefetch_forward_batched(self):
        # All source rows are fetched in a single query.
        with self.assertNumQueries(2):
            list(Member.objects.prefetch_related("tags").order_by("tenant_id", "id"))

    def test_prefetch_reverse(self):
        self.member_1.tags.add(self.tag_1, self.tag_2)
        self.member_2.tags.add(self.tag_2)

        tags = list(Tag.objects.prefetch_related("tagged_members").order_by("id"))
        with self.assertNumQueries(0):
            result = {
                tag.id: set(member.pk for member in tag.tagged_members.all())
                for tag in tags
            }
        self.assertEqual(
            result,
            {
                self.tag_1.id: {(1, 1)},
                self.tag_2.id: {(1, 1), (2, 1)},
                self.tag_3.id: set(),
            },
        )

    def test_prefetch_with_custom_queryset(self):
        self.member_1.tags.add(self.tag_1, self.tag_2)
        members = Member.objects.prefetch_related(
            Prefetch("tags", queryset=Tag.objects.filter(name="tag 1"), to_attr="one")
        )
        member = members.get(pk=self.member_1.pk)
        self.assertEqual([tag.id for tag in member.one], [self.tag_1.id])

    def test_prefetched_cache_dropped_after_successful_write(self):
        self.member_1.tags.add(self.tag_1)
        member = Member.objects.prefetch_related("tags").get(pk=self.member_1.pk)
        self.assertEqual(len(member.tags.all()), 1)
        member.tags.add(self.tag_2)
        self.assertEqual(len(member.tags.all()), 2)
        member.tags.remove(self.tag_2)
        self.assertEqual(len(member.tags.all()), 1)
        member.tags.clear()
        self.assertEqual(len(member.tags.all()), 0)

    def test_prefetched_cache_retained_after_failed_write(self):
        self.member_1.tags.add(self.tag_1)
        member = Member.objects.prefetch_related("tags").get(pk=self.member_1.pk)
        cached = list(member.tags.all())
        with self.assertRaises(Tag.DoesNotExist):
            member.tags.add(self.tag_1, 9999)
        self.assertEqual(list(member.tags.all()), cached)

    def test_delete_source(self):
        self.member_1.tags.add(self.tag_1, self.tag_2)
        self.member_2.tags.add(self.tag_1)
        through = Member.tags.through
        self.assertEqual(through.objects.count(), 3)

        self.member_1.delete()

        self.assertFalse(Member.objects.filter(pk=self.member_1.pk).exists())
        self.assertFalse(
            through.objects.filter(member_tenant_id=1, member_id=1).exists()
        )
        # The other member's relation and the tags remain.
        self.assertTrue(
            through.objects.filter(member_tenant_id=2, member_id=1).exists()
        )
        self.assertEqual(Tag.objects.count(), 3)

    def test_delete_target(self):
        self.member_1.tags.add(self.tag_1, self.tag_2)
        self.member_2.tags.add(self.tag_1)
        self.tag_1.delete()
        self.assertSequenceEqual(
            self.member_1.tags.values_list("id", flat=True), [self.tag_2.id]
        )
        self.assertSequenceEqual(self.member_2.tags.values_list("id", flat=True), [])

    def test_refresh_from_db(self):
        self.member_1.tags.add(self.tag_1)
        member = Member.objects.get(pk=self.member_1.pk)
        self.member_1.tags.add(self.tag_2)
        member.refresh_from_db()
        self.assertCountEqual(
            member.tags.values_list("id", flat=True), [self.tag_1.id, self.tag_2.id]
        )


class ManyToManyBothCompositeTests(CompositePKM2MBase):
    """Many-to-many where both sides have composite primary keys."""

    special_slugs = ["", "None", "@", "\\", "/", ",", "café, ☃", "a,b/c\\d@e"]

    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.label_1 = Label.objects.create(tenant=cls.tenant_1, slug="red")
        cls.label_2 = Label.objects.create(tenant=cls.tenant_2, slug="red")
        cls.label_3 = Label.objects.create(tenant=cls.tenant_1, slug="blue")

    def test_intermediary_model_columns(self):
        through = Member.labels.field.remote_field.through
        self.assertEqual(
            [field.name for field in through._meta.local_concrete_fields],
            [
                "id",
                "label_tenant_id",
                "member_tenant_id",
                "member_id",
                "label_slug",
            ],
        )
        self.assertEqual(
            through._meta.unique_together,
            (
                (
                    "member_tenant_id",
                    "member_id",
                    "label_tenant_id",
                    "label_slug",
                ),
            ),
        )

    def test_full_composite_key_matching(self):
        # Same slug, different tenant: both are distinct targets.
        self.member_1.labels.add(self.label_1)
        self.member_2.labels.add(self.label_2)
        self.member_1.labels.add(self.label_3)

        self.assertCountEqual(
            self.member_1.labels.values_list("pk", flat=True),
            [self.label_1.pk, self.label_3.pk],
        )
        self.assertSequenceEqual(
            self.member_2.labels.values_list("pk", flat=True), [self.label_2.pk]
        )
        # Reverse reads separate objects sharing a component.
        self.assertSequenceEqual(
            self.label_1.members.values_list("pk", flat=True), [self.member_1.pk]
        )
        self.assertSequenceEqual(
            self.label_2.members.values_list("pk", flat=True), [self.member_2.pk]
        )

    def test_add_same_component_different_key(self):
        self.member_1.labels.add(self.label_1)
        # Same slug ("red") but a different tenant must be a new relation.
        self.member_1.labels.add(self.label_2)
        self.assertEqual(self.member_1.labels.count(), 2)
        self.assertEqual(Member.labels.through.objects.count(), 2)

    def test_add_duplicate_full_key(self):
        self.member_1.labels.add(self.label_1)
        self.member_1.labels.add(self.label_1)
        self.member_1.labels.add(self.label_1.pk)
        self.assertEqual(self.member_1.labels.count(), 1)

    def test_add_by_raw_tuple(self):
        self.member_1.labels.add(self.label_1.pk)
        self.assertSequenceEqual(
            self.member_1.labels.values_list("pk", flat=True), [self.label_1.pk]
        )

    def test_add_nonexistent_full_key(self):
        with self.assertRaises(Label.DoesNotExist):
            self.member_1.labels.add((self.tenant_1.id, "missing"))

    def test_add_nonexistent_full_key_leaves_no_rows(self):
        # A key that shares the slug with an object in another tenant is
        # unknown here and must not create a dangling row.
        self.member_1.labels.add(self.label_1)
        with self.assertRaises(Label.DoesNotExist):
            self.member_1.labels.add((self.tenant_2.id, "blue"))
        self.assertEqual(Member.labels.through.objects.count(), 1)

    def test_invalid_tuple_shape(self):
        # Bad key shapes are rejected before entering any write transaction.
        for bad in [(self.tenant_1.id,), (self.tenant_1.id, "red", "extra")]:
            with self.subTest(bad=bad):
                with self.assertRaises(ValueError):
                    self.member_1.labels.add(bad)

    def test_invalid_tuple_shape_writes_nothing(self):
        with self.assertRaises(ValueError):
            self.member_1.labels.add(None)
        self.assertEqual(Member.labels.through.objects.count(), 0)

    def test_special_characters_roundtrip(self):
        labels = [
            Label.objects.create(tenant=self.tenant_1, slug=slug)
            for slug in self.special_slugs
        ]
        self.member_1.labels.add(*labels)

        retrieved = set(self.member_1.labels.values_list("slug", flat=True))
        self.assertEqual(retrieved, set(self.special_slugs))
        # Empty-string and component-bearing keys are not collapsed.
        rows = set(
            Member.labels.through.objects.values_list(
                "member_tenant_id", "member_id", "label_tenant_id", "label_slug"
            )
        )
        self.assertEqual(rows, {(1, 1, 1, slug) for slug in self.special_slugs})

    def test_text_representation_distinguishes_full_keys(self):
        self.member_1.labels.add(self.label_1, self.label_3)
        data = serializers.serialize(
            "json", self.member_1.labels.order_by("slug")
        )
        pks = [obj["pk"] for obj in json.loads(data)]
        self.assertEqual(pks, [[1, "blue"], [1, "red"]])

    def test_set_and_remove_with_tuples(self):
        self.member_1.labels.add(self.label_1, self.label_3)
        self.member_1.labels.set([self.label_2, self.label_3])
        self.assertCountEqual(
            self.member_1.labels.values_list("pk", flat=True),
            [self.label_2.pk, self.label_3.pk],
        )
        self.member_1.labels.remove(self.label_2.pk)
        self.assertSequenceEqual(
            self.member_1.labels.values_list("pk", flat=True), [self.label_3.pk]
        )

    def test_get_or_create_full_key_requires_existing_target(self):
        # The related manager only matches existing targets by the full
        # composite key and never silently creates one in a different tenant.
        # An unknown full key behaves like a normal lookup miss, and an
        # attempted create of an existing shared-component key conflicts.
        self.assertFalse(
            self.member_1.labels.filter(
                pk=(self.tenant_1.id, "red")
            ).exists()
        )
        with self.assertRaises(IntegrityError):
            self.member_1.labels.create(tenant=self.tenant_1, slug="red")

    def test_get_or_create_links_full_key(self):
        self.member_1.labels.add(self.label_1)
        label, created = self.member_1.labels.get_or_create(pk=self.label_1.pk)
        self.assertIs(created, False)
        self.assertEqual(label, self.label_1)
        self.assertEqual(self.member_1.labels.count(), 1)

        # A brand new target is created and linked.
        label, created = self.member_1.labels.get_or_create(
            tenant=self.tenant_1, slug="fresh"
        )
        self.assertIs(created, True)
        self.assertEqual(label.pk, (self.tenant_1.id, "fresh"))
        self.assertEqual(self.member_1.labels.count(), 2)

    def test_update_or_create_existing_full_key(self):
        self.member_1.labels.add(self.label_1)
        label, created = self.member_1.labels.update_or_create(
            pk=self.label_1.pk, defaults={}
        )
        self.assertIs(created, False)
        self.assertEqual(label, self.label_1)
        self.assertEqual(self.member_1.labels.count(), 1)

        label, created = self.member_1.labels.update_or_create(
            tenant=self.tenant_1, slug="fresh"
        )
        self.assertIs(created, True)
        self.assertEqual(label.pk, (self.tenant_1.id, "fresh"))
        self.assertEqual(self.member_1.labels.count(), 2)

    def test_cross_filter_full_key(self):
        self.member_1.labels.add(self.label_1)
        self.member_2.labels.add(self.label_2)

        self.assertSequenceEqual(
            Label.objects.filter(members=self.member_1).values_list("pk", flat=True),
            [self.label_1.pk],
        )
        self.assertCountEqual(
            Member.objects.filter(labels__in=[self.label_1, self.label_2]).values_list(
                "pk", flat=True
            ),
            [self.member_1.pk, self.member_2.pk],
        )

    def test_prefetch_full_keys(self):
        self.member_1.labels.add(self.label_1, self.label_3)
        self.member_2.labels.add(self.label_2)

        members = list(Member.objects.prefetch_related("labels"))
        grouped = {
            member.pk: set(member.labels.values_list("pk", flat=True))
            for member in members
        }
        self.assertEqual(
            grouped,
            {
                self.member_1.pk: {self.label_1.pk, self.label_3.pk},
                self.member_2.pk: {self.label_2.pk},
                self.member_3.pk: set(),
            },
        )

    def test_delete_either_end(self):
        self.member_1.labels.add(self.label_1)
        self.member_2.labels.add(self.label_2)
        through = Member.labels.through

        self.label_1.delete()
        self.assertFalse(
            through.objects.filter(label_tenant_id=1, label_slug="red").exists()
        )
        self.assertTrue(
            through.objects.filter(label_tenant_id=2, label_slug="red").exists()
        )

        self.member_2.delete()
        self.assertFalse(
            through.objects.filter(member_tenant_id=2, member_id=1).exists()
        )
        # Other end objects remain usable.
        self.assertTrue(Label.objects.filter(pk=self.label_2.pk).exists())
        self.assertTrue(Member.objects.filter(pk=self.member_1.pk).exists())


class ManyToManyCompositeTargetWithUuidTests(CompositePKM2MBase):
    """Many-to-many targeting a composite model with a UUID component."""

    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.post_1 = Post.objects.create(tenant=cls.tenant_1)
        cls.post_2 = Post.objects.create(tenant=cls.tenant_2)

    def test_add_uuid_target(self):
        self.member_1.posts.add(self.post_1)
        self.member_2.posts.add(self.post_2)

        self.assertSequenceEqual(
            self.member_1.posts.values_list("pk", flat=True), [self.post_1.pk]
        )
        self.assertSequenceEqual(
            self.post_1.member_authors.values_list("pk", flat=True),
            [self.member_1.pk],
        )
        self.assertEqual(self.member_1.posts.count(), 1)

    def test_add_by_string_uuid(self):
        self.member_1.posts.add((self.tenant_1.id, str(self.post_1.id)))
        self.assertEqual(self.member_1.posts.count(), 1)
        self.assertEqual(
            self.member_1.posts.values_list("pk", flat=True).get(), self.post_1.pk
        )

    def test_add_nonexistent_uuid(self):
        import uuid

        missing = uuid.uuid4()
        with self.assertRaises(Post.DoesNotExist):
            self.member_1.posts.add((self.tenant_1.id, missing))
        self.assertEqual(Member.posts.through.objects.count(), 0)

    def test_get_or_create_uuid(self):
        self.member_1.posts.add(self.post_1)
        post, created = self.member_1.posts.get_or_create(pk=self.post_1.pk)
        self.assertIs(created, False)
        self.assertEqual(post, self.post_1)


class ManyToManySelfReferentialCompositeTests(CompositePKM2MBase):
    def test_self_referential(self):
        self.member_1.friends.add(self.member_2)
        self.assertSequenceEqual(
            self.member_1.friends.values_list("pk", flat=True), [self.member_2.pk]
        )
        # Symmetrical reverse relation.
        self.assertSequenceEqual(
            self.member_2.friends.values_list("pk", flat=True), [self.member_1.pk]
        )
        self.assertEqual(self.member_1.friends.count(), 1)
        # Adding again creates no duplicate rows.
        self.member_1.friends.add(self.member_2)
        self.assertEqual(Member.friends.through.objects.count(), 2)

    def test_full_key_separation(self):
        # member_1 (1, 1) and member_2 (2, 1) share their second component.
        self.member_3.friends.add(self.member_1)
        self.assertSequenceEqual(
            self.member_3.friends.values_list("pk", flat=True), [self.member_1.pk]
        )
        self.assertFalse(
            self.member_3.friends.filter(pk=self.member_2.pk).exists()
        )

    def test_remove_symmetrical(self):
        self.member_1.friends.add(self.member_2)
        self.member_1.friends.remove(self.member_2)
        self.assertEqual(self.member_1.friends.count(), 0)
        self.assertEqual(self.member_2.friends.count(), 0)

    def test_prefetch_self(self):
        self.member_1.friends.add(self.member_2, self.member_3)
        members = Member.objects.prefetch_related("friends")
        member_1 = members.get(pk=self.member_1.pk)
        self.assertCountEqual(
            member_1.friends.values_list("pk", flat=True),
            [self.member_2.pk, self.member_3.pk],
        )


class ManyToManySingleFieldRelationKeepsSemanticsTests(CompositePKM2MBase):
    """A single-field primary key relation keeps its original semantics."""

    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.tag_1 = Tag.objects.create(id=1, name="tag 1")
        cls.tag_2 = Tag.objects.create(id=2, name="tag 2")
        cls.board = Board.objects.create(name="board")

    def test_single_field_source_to_composite_target(self):
        # Board has a regular AutoField primary key; its m2m targets
        # composite-pk Members.
        self.board.members.add(self.member_1, self.member_2)
        self.assertCountEqual(
            self.board.members.values_list("pk", flat=True),
            [self.member_1.pk, self.member_2.pk],
        )
        self.assertSequenceEqual(
            self.member_1.boards.values_list("id", flat=True), [self.board.id]
        )
        self.board.members.add(self.member_1)
        self.assertEqual(self.board.members.count(), 2)
        self.board.members.remove(self.member_2)
        self.assertSequenceEqual(
            self.board.members.values_list("pk", flat=True), [self.member_1.pk]
        )

    def test_composite_source_to_single_field_target(self):
        self.member_1.tags.add(self.tag_1)
        self.assertEqual(self.member_1.tags.get().id, self.tag_1.id)
        self.assertEqual(
            self.member_1.tags.get(pk=self.tag_1.id).id, self.tag_1.id
        )
        self.assertEqual(self.member_1.tags.count(), 1)


class CompositeM2MDatabaseConstraintTests(TransactionTestCase):
    available_apps = ["composite_pk"]

    def setUp(self):
        super().setUp()
        self.tenant_1 = Tenant.objects.create(id=1, name="tenant 1")
        self.tenant_2 = Tenant.objects.create(id=2, name="tenant 2")
        self.member_1 = Member.objects.create(tenant=self.tenant_1, id=1, name="m1")

    def test_database_foreign_key_enforces_full_key(self):
        # An unknown key is rejected by the composite foreign key.
        with connection.cursor() as cursor:
            with self.assertRaises(IntegrityError):
                cursor.execute(
                    "INSERT INTO composite_pk_member_tags "
                    "(member_tenant_id, member_id, tag_id) "
                    "VALUES (%s, %s, %s)",
                    [self.tenant_1.id, self.member_1.id, 999999],
                )
            with self.assertRaises(IntegrityError):
                cursor.execute(
                    "INSERT INTO composite_pk_member_labels "
                    "(member_tenant_id, member_id, label_tenant_id, label_slug) "
                    "VALUES (%s, %s, %s, %s)",
                    [self.tenant_1.id, self.member_1.id, self.tenant_2.id, "red"],
                )
            # Sharing only one component with an existing label is still a
            # dangling composite key.
            Label.objects.create(tenant=self.tenant_1, slug="red")
            with self.assertRaises(IntegrityError):
                cursor.execute(
                    "INSERT INTO composite_pk_member_labels "
                    "(member_tenant_id, member_id, label_tenant_id, label_slug) "
                    "VALUES (%s, %s, %s, %s)",
                    [self.tenant_1.id, self.member_1.id, self.tenant_2.id, "red"],
                )

    def test_database_foreign_key_declaration(self):
        # The created table carries composite foreign keys spanning all the
        # referenced primary key columns.
        with connection.cursor() as cursor:
            keys = cursor.execute(
                "PRAGMA foreign_key_list(composite_pk_member_labels)"
            ).fetchall()
        # Each row: (id, seq, table, from_column, to_column, ...).
        grouped = {}
        for row in keys:
            grouped.setdefault(row[0], []).append((row[3], row[4]))
        all_pairs = [pair for pairs in grouped.values() for pair in pairs]
        self.assertIn(("member_tenant_id", "tenant_id"), all_pairs)
        self.assertIn(("member_id", "id"), all_pairs)
        self.assertIn(("label_tenant_id", "tenant_id"), all_pairs)
        self.assertIn(("label_slug", "slug"), all_pairs)
        # Each foreign key spans more than one column.
        self.assertEqual(len(grouped), 2)
        self.assertTrue(all(len(pairs) == 2 for pairs in grouped.values()))


class CompositeM2MSerializationTests(CompositePKM2MBase):
    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.tag_1 = Tag.objects.create(id=1, name="tag 1")
        cls.label_1 = Label.objects.create(tenant=cls.tenant_1, slug="red")

    def test_serialize_relation(self):
        self.member_1.tags.add(self.tag_1)
        data = serializers.serialize(
            "json", Member.objects.filter(pk=self.member_1.pk)
        )
        fields = json.loads(data)[0]["fields"]
        self.assertEqual(fields["tags"], [self.tag_1.id])

        self.member_1.labels.add(self.label_1)
        data = serializers.serialize(
            "json",
            Member.objects.filter(pk=self.member_1.pk),
        )
        self.assertEqual(json.loads(data)[0]["fields"]["labels"], [[1, "red"]])

    def test_deserialize_restores_relation(self):
        self.member_1.tags.add(self.tag_1)
        data = serializers.serialize("json", [self.member_1])
        self.member_1.tags.clear()
        deserialized = list(serializers.deserialize("json", data))
        self.assertEqual(len(deserialized), 1)
        deserialized[0].save()
        self.assertSequenceEqual(
            self.member_1.tags.values_list("id", flat=True), [self.tag_1.id]
        )
