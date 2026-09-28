from django.forms import modelform_factory
from django.test import TestCase

from .models import Board, Label, Member, Tag, Tenant

# Slugs that must remain distinguishable when a composite primary key is
# carried through a single HTML option value: an empty component, the literal
# text "None", the None marker, quoting/separator characters and non-ASCII
# text.
SPECIAL_SLUGS = ["", "None", "@", "\\", "/", ",", '"', "Üé 中", "_40", "_5F"]


class CompositePKModelFormTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.tenant_1 = Tenant.objects.create(id=1, name="tenant 1")
        cls.tenant_2 = Tenant.objects.create(id=2, name="tenant 2")
        # member_1 and member_3 (different tenants) share their second
        # component and must not collapse into one option.
        cls.member_1 = Member.objects.create(tenant=cls.tenant_1, id=1, name="m1")
        cls.member_2 = Member.objects.create(tenant=cls.tenant_1, id=2, name="m2")
        cls.member_3 = Member.objects.create(tenant=cls.tenant_2, id=1, name="m3")
        cls.labels_1 = [
            Label.objects.create(tenant=cls.tenant_1, slug=slug)
            for slug in SPECIAL_SLUGS
        ]
        cls.labels_2 = [
            Label.objects.create(tenant=cls.tenant_2, slug=slug)
            for slug in SPECIAL_SLUGS
        ]
        cls.tag_1 = Tag.objects.create(name="tag 1")
        cls.tag_2 = Tag.objects.create(name="tag 2")
        cls.board = Board.objects.create(name="board")

    def member_labels_form(self, data=None, instance=None):
        form = modelform_factory(Member, fields=["name", "labels"])(
            data=data, instance=instance
        )
        # The form field only offers labels of the member's tenant, as a
        # limit_choices_to/callback would arrange in a real project.
        form.fields["labels"].queryset = Label.objects.filter(
            tenant=instance.tenant if instance is not None else self.tenant_1
        )
        return form

    def encoded_choice_values(self, form):
        return {str(value): label for value, label in form.fields["labels"].choices}

    # Display / encoding -------------------------------------------------

    def test_unbound_choices_carry_full_key_in_declared_order(self):
        self.member_1.labels.add(self.labels_1[2], self.labels_1[3])
        form = self.member_labels_form(instance=self.member_1)
        expected = {
            label._meta.pk.encode_pk(label.pk): str(label) for label in self.labels_1
        }
        self.assertEqual(self.encoded_choice_values(form), expected)
        html = str(form["labels"])
        # Parts appear in declaration order (tenant_id, slug), separated by a
        # quoted comma, and only the current relation is selected. A slug that
        # contains a comma is quoted so it cannot be read as the separator.
        comma_label = next(label for label in self.labels_1 if label.slug == ",")
        self.assertIn(
            'value="%s"' % comma_label._meta.pk.encode_pk(comma_label.pk),
            html,
        )
        for label in (self.labels_1[2], self.labels_1[3]):
            self.assertIn(
                'value="%s" selected' % label._meta.pk.encode_pk(label.pk), html
            )
        for label in self.labels_1[:2]:
            self.assertNotIn(
                'value="%s" selected' % label._meta.pk.encode_pk(label.pk), html
            )

    def test_option_values_distinguish_all_special_components(self):
        form = self.member_labels_form(instance=self.member_1)
        values = list(self.encoded_choice_values(form))
        # Every special slug yields a different option value, and a label from
        # another tenant that shares the slug yields yet another value.
        self.assertEqual(len(values), len(set(values)))
        self.assertEqual(len(values), len(SPECIAL_SLUGS))
        form_other_tenant = self.member_labels_form(instance=self.member_3)
        other_values = set(self.encoded_choice_values(form_other_tenant))
        self.assertTrue(other_values.isdisjoint(values))
        # None ("@" marker) and an empty component stay distinct both from
        # each other and from the literal text "None".
        pk_field = Label._meta.pk
        self.assertEqual(pk_field.encode_pk((1, "")), "1,")
        self.assertEqual(pk_field.encode_pk((1, None)), "1,@")
        self.assertEqual(pk_field.encode_pk((1, "None")), "1,None")
        self.assertEqual(pk_field.encode_pk((1, "@")), "1,_40")
        self.assertEqual(pk_field.encode_pk((1, "a,b")), "1,a_2Cb")

    def test_special_components_survive_display_submit_clean_rerender(self):
        form = self.member_labels_form(instance=self.member_1)
        submitted = [label._meta.pk.encode_pk(label.pk) for label in self.labels_1]
        bound = self.member_labels_form(
            data={"name": "m1", "labels": submitted}, instance=self.member_1
        )
        self.assertTrue(bound.is_valid(), bound.errors)
        cleaned = set(bound.cleaned_data["labels"])
        self.assertEqual(cleaned, set(self.labels_1))
        # Re-rendering the valid bound form keeps the exact same values.
        rendered = str(bound["labels"])
        for value in submitted:
            self.assertIn('value="%s" selected' % value, rendered)
        # Repeated instantiation produces the identical option set.
        rebuilt = self.member_labels_form(instance=self.member_1)
        self.assertEqual(
            sorted(self.encoded_choice_values(form)),
            sorted(self.encoded_choice_values(rebuilt)),
        )

    def test_initial_is_explained_by_full_key(self):
        self.member_1.labels.add(*self.labels_1[:3])
        first = self.member_labels_form(instance=self.member_1)
        second = self.member_labels_form(instance=self.member_1)
        first_html = str(first["labels"])
        second_html = str(second["labels"])
        for label in self.labels_1[:3]:
            value = label._meta.pk.encode_pk(label.pk)
            self.assertIn('value="%s" selected' % value, first_html)
            self.assertIn('value="%s" selected' % value, second_html)

    # Valid saves ---------------------------------------------------------

    def test_valid_save_is_seen_forward_reverse_and_across_relations(self):
        chosen = self.labels_1[::2]
        values = [label._meta.pk.encode_pk(label.pk) for label in chosen]
        form = self.member_labels_form(
            data={"name": "m1", "labels": values}, instance=self.member_1
        )
        self.assertTrue(form.is_valid(), form.errors)
        self.assertEqual(set(form.cleaned_data["labels"]), set(chosen))
        form.save()
        self.member_1.refresh_from_db()

        expected = {label.pk for label in chosen}
        self.assertEqual(
            set(self.member_1.labels.values_list("pk", flat=True)), expected
        )
        self.assertEqual(
            set(
                Label.objects.filter(members=self.member_1).values_list("pk", flat=True)
            ),
            expected,
        )
        self.assertEqual(
            set(
                Member.objects.filter(labels__in=chosen)
                .distinct()
                .values_list("pk", flat=True)
            ),
            {self.member_1.pk},
        )
        # The through table and a prefetched copy explain the same set.
        through_rows = set(
            Member.labels.through.objects.values_list(
                "member_tenant_id", "member_id", "label_tenant_id", "label_slug"
            )
        )
        self.assertEqual(
            through_rows,
            {(1, 1, 1, label.slug) for label in chosen},
        )
        prefetched = Member.objects.prefetch_related("labels").get(pk=self.member_1.pk)
        self.assertEqual({label.pk for label in prefetched.labels.all()}, expected)
        # A form rebuilt against the saved instance explains the saved set
        # with the same full keys.
        saved_form = self.member_labels_form(instance=self.member_1)
        saved_html = str(saved_form["labels"])
        for label in chosen:
            self.assertIn(
                'value="%s" selected' % label._meta.pk.encode_pk(label.pk),
                saved_html,
            )

    def test_repeated_submit_is_idempotent(self):
        chosen = self.labels_1[1:4]
        values = [label._meta.pk.encode_pk(label.pk) for label in chosen]
        for _ in range(2):
            form = self.member_labels_form(
                data={"name": "m1", "labels": values}, instance=self.member_1
            )
            self.assertTrue(form.is_valid(), form.errors)
            form.save()
            self.member_1.refresh_from_db()
        self.assertEqual(
            set(self.member_1.labels.values_list("pk", flat=True)),
            {label.pk for label in chosen},
        )
        self.assertEqual(Member.labels.through.objects.count(), len(chosen))

    def test_save_replaces_only_this_field_and_this_object(self):
        self.member_1.labels.add(*self.labels_1[:3])
        self.member_1.tags.add(self.tag_1)
        self.member_2.labels.add(self.labels_1[4])
        replacement = self.labels_1[5:8]
        values = [label._meta.pk.encode_pk(label.pk) for label in replacement]
        form = self.member_labels_form(
            data={"name": "m1", "labels": values}, instance=self.member_1
        )
        self.assertTrue(form.is_valid(), form.errors)
        form.save()
        self.member_1.refresh_from_db()
        self.member_2.refresh_from_db()
        self.assertEqual(
            set(self.member_1.labels.values_list("pk", flat=True)),
            {label.pk for label in replacement},
        )
        # Other relations of this object and the same relation on another
        # object are untouched.
        self.assertEqual(list(self.member_1.tags.all()), [self.tag_1])
        self.assertEqual(
            set(self.member_2.labels.values_list("pk", flat=True)),
            {self.labels_1[4].pk},
        )

    def test_valid_choice_does_not_affect_target_sharing_a_component(self):
        label_1 = self.labels_1[2]
        label_2 = self.labels_2[2]
        self.member_1.labels.add(label_1)
        self.member_3.labels.add(label_2)
        # Replace member_1's relation with the same slug in tenant 1 only.
        form = self.member_labels_form(
            data={
                "name": "m1",
                "labels": [label_1._meta.pk.encode_pk(label_1.pk)],
            },
            instance=self.member_1,
        )
        self.assertTrue(form.is_valid(), form.errors)
        form.save()
        self.assertEqual(list(self.member_1.labels.all()), [label_1])
        self.assertEqual(list(self.member_3.labels.all()), [label_2])

    # Invalid submissions -------------------------------------------------

    def test_invalid_values_raise_invalid_choice(self):
        self.member_1.labels.add(self.labels_1[0])
        valid = self.labels_1[0]._meta.pk.encode_pk(self.labels_1[0].pk)
        invalid_values = [
            "999,x",  # nonexistent tenant
            "1,ta_6dmp",  # tampered quoting decodes to an absent slug
            "1,_ZZ",  # unknown escape, no matching object
            "1",  # too few components
            "1,2,3",  # too many components
            "1,@,x",  # too many components, None marker included
        ]
        for invalid in invalid_values:
            with self.subTest(invalid=invalid):
                form = self.member_labels_form(
                    data={"name": "m1", "labels": [valid, invalid]},
                    instance=self.member_1,
                )
                self.assertFalse(form.is_valid())
                codes = [error.code for error in form.errors.as_data()["labels"]]
                self.assertEqual(codes, ["invalid_choice"])
                # A failed form never reaches save_m2m(); the relation, the
                # through table and the instance's relation are unchanged.
                self.assertEqual(
                    set(self.member_1.labels.values_list("pk", flat=True)),
                    {self.labels_1[0].pk},
                )
                self.assertEqual(Member.labels.through.objects.count(), 1)
                # The failed bound form keeps explaining itself through the
                # same full key (the valid submitted value stays selected).
                self.assertIn('value="%s" selected' % valid, str(form["labels"]))
                # A fresh unbound form renders the untouched original relation.
                fresh = self.member_labels_form(instance=self.member_1)
                html = str(fresh["labels"])
                self.assertIn('value="%s" selected' % valid, html)

    def test_integer_component_conversion_failure_is_invalid_choice(self):
        # Board.members targets Member(tenant_id, id): the second component
        # must convert to an integer.
        self.board.members.add(self.member_1)
        form = modelform_factory(Board, fields=["name", "members"])(
            data={"name": "board", "members": ["1,not-a-number"]}
        )
        self.assertFalse(form.is_valid())
        self.assertEqual(
            [error.code for error in form.errors.as_data()["members"]],
            ["invalid_choice"],
        )
        self.assertEqual(list(self.board.members.all()), [self.member_1])

    def test_batch_with_one_invalid_value_leaves_no_partial_update(self):
        chosen = self.labels_1[:4]
        self.member_1.labels.add(*chosen)
        # Prefetch the relation before the failed submission; its cache must
        # keep describing the pre-operation set.
        prefetched = Member.objects.prefetch_related("labels").get(pk=self.member_1.pk)
        original = {label.pk for label in prefetched.labels.all()}

        submitted = [label._meta.pk.encode_pk(label.pk) for label in chosen] + [
            "1,no-such-slug"
        ]
        form = self.member_labels_form(
            data={"name": "m1", "labels": submitted}, instance=self.member_1
        )
        self.assertFalse(form.is_valid())
        self.assertEqual(
            [error.code for error in form.errors.as_data()["labels"]],
            ["invalid_choice"],
        )
        # Database state: relation and through table are untouched.
        self.assertEqual(
            set(self.member_1.labels.values_list("pk", flat=True)), original
        )
        self.assertEqual(Member.labels.through.objects.count(), len(chosen))
        # The prefetched cache still answers from memory with the old set.
        with self.assertNumQueries(0):
            self.assertEqual({label.pk for label in prefetched.labels.all()}, original)
        # Re-reading and a freshly rendered form show the original set.
        self.member_1.refresh_from_db()
        self.assertEqual(
            set(self.member_1.labels.values_list("pk", flat=True)), original
        )
        fresh = self.member_labels_form(instance=self.member_1)
        html = str(fresh["labels"])
        for label in chosen:
            self.assertIn(
                'value="%s" selected' % label._meta.pk.encode_pk(label.pk),
                html,
            )

    # Deletion ------------------------------------------------------------

    def test_choices_and_relation_follow_target_deletion(self):
        label = self.labels_1[3]
        stale_value = label._meta.pk.encode_pk(label.pk)
        self.member_1.labels.add(label)
        label.delete()
        # Regenerated choices no longer list the deleted target and the
        # relation disappears with it (existing cascade semantics).
        form = self.member_labels_form(instance=self.member_1)
        self.assertNotIn(stale_value, self.encoded_choice_values(form))
        self.assertFalse(self.member_1.labels.exists())
        # A stale value submitted after deletion yields invalid_choice.
        bound = self.member_labels_form(
            data={"name": "m1", "labels": [stale_value]}, instance=self.member_1
        )
        self.assertFalse(bound.is_valid())
        self.assertEqual(
            [error.code for error in bound.errors.as_data()["labels"]],
            ["invalid_choice"],
        )

    # Single-column primary keys keep working ----------------------------

    def test_single_column_pk_target_remains_compatible(self):
        # Composite source -> single-column target (Member.tags -> Tag).
        form = modelform_factory(Member, fields=["name", "tags"])(
            data={"name": "m1", "tags": [str(self.tag_1.pk), str(self.tag_2.pk)]},
            instance=self.member_1,
        )
        self.assertTrue(form.is_valid(), form.errors)
        self.assertEqual(set(form.cleaned_data["tags"]), {self.tag_1, self.tag_2})
        form.save()
        self.member_1.refresh_from_db()
        self.assertEqual(set(self.member_1.tags.all()), {self.tag_1, self.tag_2})
        # Resubmitting is idempotent.
        form = modelform_factory(Member, fields=["name", "tags"])(
            data={"name": "m1", "tags": [str(self.tag_1.pk)]},
            instance=self.member_1,
        )
        self.assertTrue(form.is_valid(), form.errors)
        form.save()
        self.member_1.refresh_from_db()
        self.assertEqual(list(self.member_1.tags.all()), [self.tag_1])
        # A non-numeric value keeps the legacy invalid_pk_value error.
        form = modelform_factory(Member, fields=["name", "tags"])(
            data={"name": "m1", "tags": ["abc"]}, instance=self.member_1
        )
        self.assertFalse(form.is_valid())
        self.assertEqual(
            [error.code for error in form.errors.as_data()["tags"]],
            ["invalid_pk_value"],
        )
        self.assertEqual(list(self.member_1.tags.all()), [self.tag_1])

    def test_single_column_pk_source_targeting_composite_remains_compatible(
        self,
    ):
        # Single-column source -> composite target (Board.members -> Member).
        unbound = modelform_factory(Board, fields=["name", "members"])(
            instance=self.board
        )
        values = {str(v) for v, _ in unbound.fields["members"].choices}
        self.assertIn("1,1", values)
        self.assertIn("2,1", values)
        bound = modelform_factory(Board, fields=["name", "members"])(
            data={"name": "board", "members": ["1,1", "2,1"]},
            instance=self.board,
        )
        self.assertTrue(bound.is_valid(), bound.errors)
        self.assertEqual(
            set(bound.cleaned_data["members"]), {self.member_1, self.member_3}
        )
        bound.save()
        self.board.refresh_from_db()
        self.assertEqual(set(self.board.members.all()), {self.member_1, self.member_3})
        invalid = modelform_factory(Board, fields=["name", "members"])(
            data={"name": "board", "members": ["1,1", "9,9"]},
            instance=self.board,
        )
        self.assertFalse(invalid.is_valid())
        self.assertEqual(
            [error.code for error in invalid.errors.as_data()["members"]],
            ["invalid_choice"],
        )
        self.assertEqual(set(self.board.members.all()), {self.member_1, self.member_3})
