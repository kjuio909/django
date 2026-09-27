from django.test import TestCase

from .models import Country, Post, PostTag, Role, Tag, Team, Tenant, User


class CompositePKManyToManyTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.tenant_1 = Tenant.objects.create()
        cls.tenant_2 = Tenant.objects.create()
        cls.tag_1 = Tag.objects.create(title="x")
        cls.tag_2 = Tag.objects.create(title="y")
        cls.post_1 = Post.objects.create(tenant=cls.tenant_1)
        # Two posts share the second primary key component (the UUID is unique,
        # but a custom SmallIntegerField id is also exercised elsewhere).
        cls.post_2 = Post.objects.create(tenant=cls.tenant_2)
        cls.post_tag_1 = PostTag.objects.create()
        cls.user_1 = User.objects.create(
            tenant=cls.tenant_1, id=1, email="u1@example.com"
        )
        cls.user_2 = User.objects.create(
            tenant=cls.tenant_1, id=2, email="u2@example.com"
        )
        cls.user_other_tenant = User.objects.create(
            tenant=cls.tenant_2, id=3, email="u3@example.com"
        )

    # -- relation to a composite primary key (forward on a single-PK model) --

    def test_forward_add_and_read(self):
        self.post_tag_1.posts.add(self.post_1, self.post_2)
        self.assertCountEqual(
            self.post_tag_1.posts.values_list("pk", flat=True),
            [self.post_1.pk, self.post_2.pk],
        )

    def test_add_by_full_primary_key_tuple(self):
        self.post_tag_1.posts.add(self.post_1.pk, self.post_2.pk)
        self.assertEqual(
            list(self.post_tag_1.posts.order_by("pk").values_list("pk", flat=True)),
            sorted([self.post_1.pk, self.post_2.pk]),
        )

    def test_duplicate_add_does_not_create_rows(self):
        self.post_tag_1.posts.add(self.post_1)
        self.post_tag_1.posts.add(self.post_1)
        self.post_tag_1.posts.add(self.post_1.pk)
        self.assertEqual(self.post_tag_1.posts.count(), 1)
        through = PostTag.posts.through
        self.assertEqual(
            through.objects.filter(posttag=self.post_tag_1).count(), 1
        )

    def test_partial_component_does_not_collide(self):
        # self.post_1 and self.post_2 are in different tenants. Matching must
        # use the full tuple, so adding one never matches the other.
        self.post_tag_1.posts.add(self.post_1)
        self.post_tag_1.posts.add(self.post_2)
        self.assertEqual(self.post_tag_1.posts.count(), 2)
        self.post_tag_1.posts.remove(self.post_2.pk)
        self.assertEqual(list(self.post_tag_1.posts.all()), [self.post_1])

    def test_reverse_read(self):
        self.post_tag_1.posts.add(self.post_1)
        self.assertCountEqual(
            self.post_1.post_tags.values_list("pk", flat=True),
            [self.post_tag_1.pk],
        )

    def test_cross_relation_filter(self):
        self.post_tag_1.posts.add(self.post_1, self.post_2)
        post_tag_2 = PostTag.objects.create()
        post_tag_2.posts.add(self.post_2)
        self.assertCountEqual(
            PostTag.objects.filter(posts=self.post_1).values_list("pk", flat=True),
            [self.post_tag_1.pk],
        )
        self.assertCountEqual(
            PostTag.objects.filter(posts=self.post_2).values_list("pk", flat=True),
            [self.post_tag_1.pk, post_tag_2.pk],
        )
        # Filtering by a shared component alone must not match the other
        # tenant's post.
        self.assertFalse(
            PostTag.objects.filter(posts=(self.tenant_2.pk, self.post_1.id)).exists()
        )

    def test_filter_by_full_key_tuple(self):
        self.post_tag_1.posts.add(self.post_1, self.post_2)
        self.assertCountEqual(
            PostTag.objects.filter(posts=self.post_1.pk).values_list("pk", flat=True),
            [self.post_tag_1.pk],
        )
        self.assertCountEqual(
            PostTag.objects.filter(posts__in=[self.post_1.pk, self.post_2.pk])
            .distinct()
            .values_list("pk", flat=True),
            [self.post_tag_1.pk],
        )

    def test_remove(self):
        self.post_tag_1.posts.add(self.post_1, self.post_2)
        self.post_tag_1.posts.remove(self.post_1)
        self.assertEqual(list(self.post_tag_1.posts.all()), [self.post_2])
        self.post_tag_1.posts.remove(self.post_2.pk)
        self.assertEqual(self.post_tag_1.posts.count(), 0)

    def test_clear(self):
        self.post_tag_1.posts.add(self.post_1, self.post_2)
        self.post_tag_1.posts.clear()
        self.assertEqual(self.post_tag_1.posts.count(), 0)
        # The other end and unrelated rows are untouched.
        self.assertTrue(Post.objects.filter(pk=self.post_1.pk).exists())

    def test_set(self):
        self.post_tag_1.posts.add(self.post_1)
        self.post_tag_1.posts.set([self.post_2])
        self.assertEqual(list(self.post_tag_1.posts.all()), [self.post_2])
        self.post_tag_1.posts.set([self.post_1.pk, self.post_2.pk])
        self.assertEqual(self.post_tag_1.posts.count(), 2)
        self.post_tag_1.posts.set([])
        self.assertEqual(self.post_tag_1.posts.count(), 0)

    def test_set_keeps_existing_with_different_value_type(self):
        self.post_tag_1.posts.add(self.post_1)
        # Existing relation given as a tuple with string components must be
        # recognized and not re-created.
        self.post_tag_1.posts.set([tuple(map(str, self.post_1.pk))])
        self.assertEqual(self.post_tag_1.posts.count(), 1)

    def test_create_and_get_or_create(self):
        post = self.post_tag_1.posts.create(tenant=self.tenant_1)
        self.assertTrue(self.post_tag_1.posts.filter(pk=post.pk).exists())
        fetched, created = self.post_tag_1.posts.get_or_create(
            tenant_id=post.tenant_id, id=post.id
        )
        self.assertFalse(created)
        self.assertEqual(fetched, post)
        self.assertEqual(self.post_tag_1.posts.count(), 1)

    def test_get_or_create_matches_by_full_key(self):
        self.post_tag_1.posts.add(self.post_1)
        # Same second component but different tenant: must create a new post
        # rather than reuse self.post_1.
        post, created = self.post_tag_1.posts.get_or_create(
            tenant=self.tenant_2, id=self.post_1.id
        )
        self.assertTrue(created)
        self.assertNotEqual(post.pk, self.post_1.pk)
        self.assertTrue(self.post_tag_1.posts.filter(pk=post.pk).exists())

    def test_update_or_create(self):
        self.post_tag_1.posts.add(self.post_1)
        post, created = self.post_tag_1.posts.update_or_create(
            pk=self.post_1.pk, defaults={}
        )
        self.assertFalse(created)
        self.assertEqual(post, self.post_1)
        # Sharing one component must not pick the wrong target.
        post, created = self.post_tag_1.posts.update_or_create(
            tenant_id=self.tenant_2.pk, id=self.post_1.id, defaults={}
        )
        self.assertTrue(created)

    # -- relation declared on a composite primary key model --

    def test_relation_from_composite_model(self):
        self.user_1.tags.add(self.tag_1)
        self.assertEqual(list(self.user_1.tags.all()), [self.tag_1])
        self.assertCountEqual(
            self.tag_1.tagged_users.values_list("pk", flat=True), [self.user_1.pk]
        )

    def test_composite_to_composite(self):
        role_1 = Role.objects.create(tenant=self.tenant_1, code="admin")
        role_2 = Role.objects.create(tenant=self.tenant_2, code="admin")
        # A many-to-many relation between two composite-key models stores
        # every component on both sides.
        self.user_1.roles.add(role_1, role_2)
        self.assertCountEqual(
            self.user_1.roles.values_list("pk", flat=True), [role_1.pk, role_2.pk]
        )
        self.assertCountEqual(
            role_1.users.values_list("pk", flat=True), [self.user_1.pk]
        )
        # The same role code in a different tenant is a distinct relation.
        self.user_other_tenant.roles.add(role_2)
        self.assertEqual(
            self.user_1.roles.filter(pk=role_2.pk).count(), 1
        )
        self.assertEqual(
            role_2.users.filter(pk=self.user_other_tenant.pk).count(), 1
        )

    def test_composite_to_composite_remove_isolation(self):
        role_1 = Role.objects.create(tenant=self.tenant_1, code="admin")
        role_2 = Role.objects.create(tenant=self.tenant_2, code="admin")
        self.user_1.roles.add(role_1, role_2)
        self.user_other_tenant.roles.add(role_1)
        self.user_1.roles.remove((self.tenant_1.pk, "admin"))
        self.assertEqual(
            list(self.user_1.roles.values_list("pk", flat=True)), [role_2.pk]
        )
        # Unrelated link still exists.
        self.assertEqual(
            role_1.users.filter(pk=self.user_other_tenant.pk).count(), 1
        )

    def test_self_referential_composite(self):
        self.user_1.followers.add(self.user_2)
        self.assertEqual(
            list(self.user_1.followers.values_list("pk", flat=True)),
            [self.user_2.pk],
        )
        self.assertEqual(
            list(self.user_2.following.values_list("pk", flat=True)),
            [self.user_1.pk],
        )
        self.assertEqual(self.user_2.followers.count(), 0)
        self.user_1.followers.remove(self.user_2)
        self.assertEqual(self.user_1.followers.count(), 0)

    def test_self_referential_partial_key_isolation(self):
        self.user_1.followers.add(self.user_other_tenant)
        self.user_other_tenant.followers.add(self.user_1)
        through = User.followers.through
        self.assertEqual(
            through.objects.filter(
                from_user_tenant_id=self.tenant_1.pk, from_user_id=self.user_1.id
            ).count(),
            1,
        )
        self.assertEqual(
            through.objects.filter(
                to_user_tenant_id=self.tenant_2.pk,
                to_user_id=self.user_other_tenant.id,
            ).count(),
            1,
        )

    # -- prefetching --

    def test_prefetch_forward(self):
        self.post_tag_1.posts.add(self.post_1, self.post_2)
        post_tag = PostTag.objects.prefetch_related("posts").get(pk=self.post_tag_1.pk)
        self.assertCountEqual(
            [p.pk for p in post_tag.posts.all()], [self.post_1.pk, self.post_2.pk]
        )

    def test_prefetch_reverse(self):
        self.post_tag_1.posts.add(self.post_1)
        post_tag_2 = PostTag.objects.create()
        post_tag_2.posts.add(self.post_1)
        posts = list(
            Post.objects.prefetch_related("post_tags").filter(pk=self.post_1.pk)
        )
        self.assertCountEqual(
            [t.pk for t in posts[0].post_tags.all()],
            [self.post_tag_1.pk, post_tag_2.pk],
        )

    def test_prefetch_from_composite_side(self):
        self.user_1.tags.add(self.tag_1, self.tag_2)
        user = User.objects.prefetch_related("tags").get(pk=self.user_1.pk)
        self.assertCountEqual(
            [t.pk for t in user.tags.all()], [self.tag_1.pk, self.tag_2.pk]
        )
        tag = Tag.objects.prefetch_related("tagged_users").get(pk=self.tag_1.pk)
        self.assertEqual([u.pk for u in tag.tagged_users.all()], [self.user_1.pk])

    # -- prefetch cache invalidation --

    def test_prefetch_cache_dropped_after_write(self):
        self.post_tag_1.posts.add(self.post_1)
        list(self.post_tag_1.posts.all())  # populate prefetch cache
        self.post_tag_1.posts.add(self.post_2)
        self.assertCountEqual(
            list(self.post_tag_1.posts.values_list("pk", flat=True)),
            [self.post_1.pk, self.post_2.pk],
        )

    def test_prefetch_cache_preserved_after_failed_write(self):
        from django.db import transaction

        self.post_tag_1.posts.add(self.post_1)
        list(self.post_tag_1.posts.all())
        unsaved = Post(tenant=self.tenant_1)
        # An explicit savepoint isolates the failure because the manager
        # performs its work with savepoint=False.
        with self.assertRaises(ValueError):
            with transaction.atomic():
                self.post_tag_1.posts.add(unsaved)
        # The prefetch cache was not invalidated by the failed write.
        self.assertEqual(list(self.post_tag_1.posts.all()), [self.post_1])

    # -- atomic failure semantics --

    def test_add_unsaved_object_fails_atomically(self):
        from django.db import transaction

        self.post_tag_1.posts.add(self.post_1)
        unsaved = Post(tenant=self.tenant_1)
        with self.assertRaises(ValueError):
            with transaction.atomic():
                self.post_tag_1.posts.add(self.post_2, unsaved)
        # Neither the valid nor the invalid object changed the relation.
        self.assertEqual(list(self.post_tag_1.posts.all()), [self.post_1])

    def test_add_wrong_model_type(self):
        with self.assertRaises(TypeError):
            self.post_tag_1.posts.add(self.tag_1)

    def test_add_invalid_component_type(self):
        from django.db import transaction

        with self.assertRaises((TypeError, ValueError)):
            with transaction.atomic():
                self.post_tag_1.posts.add(("not-a-tenant-id", self.post_1.id))
        self.assertEqual(self.post_tag_1.posts.count(), 0)

    def test_add_wrong_tuple_length(self):
        with self.assertRaises(ValueError):
            self.post_tag_1.posts.add((self.post_1.pk[0],))
        with self.assertRaises(ValueError):
            self.post_tag_1.posts.add((1, 2, 3))

    def test_add_nonexistent_target(self):
        from django.db import transaction

        with self.assertRaises(ValueError):
            with transaction.atomic():
                self.post_tag_1.posts.add((self.tenant_1.pk, 999999))
        self.assertEqual(self.post_tag_1.posts.count(), 0)

    # -- deletions --

    def test_delete_target_removes_only_matching_through_rows(self):
        self.post_tag_1.posts.add(self.post_1, self.post_2)
        post_tag_2 = PostTag.objects.create()
        post_tag_2.posts.add(self.post_2)
        Post.objects.get(pk=self.post_2.pk).delete()
        self.assertEqual(list(self.post_tag_1.posts.all()), [self.post_1])
        self.assertEqual(post_tag_2.posts.count(), 0)
        # The other target survives.
        self.assertTrue(Post.objects.filter(pk=self.post_1.pk).exists())

    def test_delete_source_removes_only_its_rows(self):
        self.post_tag_1.posts.add(self.post_1)
        post_tag_2 = PostTag.objects.create()
        post_tag_2.posts.add(self.post_1)
        PostTag.objects.get(pk=self.post_tag_1.pk).delete()
        self.assertEqual(
            PostTag.posts.through.objects.filter(
                posttag_id=post_tag_2.pk
            ).count(),
            1,
        )
        self.assertTrue(Post.objects.filter(pk=self.post_1.pk).exists())

    def test_delete_composite_source(self):
        self.user_1.followers.add(self.user_2)
        User.objects.get(pk=self.user_1.pk).delete()
        through = User.followers.through
        self.assertFalse(
            through.objects.filter(
                from_user_tenant_id=self.tenant_1.pk,
                from_user_id=self.user_1.id,
            ).exists()
        )
        self.assertTrue(User.objects.filter(pk=self.user_2.pk).exists())

    # -- count / exists short-circuits --

    def test_count_and_exists(self):
        self.post_tag_1.posts.add(self.post_1)
        self.assertEqual(self.post_tag_1.posts.count(), 1)
        self.assertIs(self.post_tag_1.posts.exists(), True)
        self.post_tag_1.posts.clear()
        self.assertEqual(self.post_tag_1.posts.count(), 0)
        self.assertIs(self.post_tag_1.posts.exists(), False)

    # -- unusual key values round-trip --

    def test_special_key_values_round_trip(self):
        values = [
            "",
            "None",
            "@",
            "\\",
            "/",
            ",",
            "ナイス",
            "a,b/c\\d@None",
            "  ",
        ]
        team = Team.objects.create(name="team")
        for value in values:
            Country.objects.create(region="r", code=value)
        team.countries.add(*[("r", value) for value in values])
        self.assertEqual(team.countries.count(), len(values))
        self.assertCountEqual(
            team.countries.values_list("pk", flat=True),
            [("r", value) for value in values],
        )
        for value in values:
            country = Country.objects.get(pk=("r", value))
            self.assertEqual(list(country.teams.all()), [team])
        # Removing by the full key works for each unusual value.
        for value in values:
            team.countries.remove(("r", value))
        self.assertEqual(team.countries.count(), 0)

    def test_text_key_forms_do_not_collide(self):
        team = Team.objects.create(name="team")
        # The string representations of distinct keys must never be merged.
        keys = [("a", ""), ("a,", ""), ("", "a")]
        for key in keys:
            Country.objects.create(region=key[0], code=key[1])
        team.countries.add(*keys)
        self.assertEqual(team.countries.count(), 3)
        self.assertCountEqual(
            team.countries.values_list("pk", flat=True), keys
        )
