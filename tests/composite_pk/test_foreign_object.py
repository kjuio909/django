from django.test import TestCase

from .models import Reservation, Tenant, Venue


class CompositePKForeignObjectTests(TestCase):
    """
    Persistable ForeignObject relations spanning a composite primary key
    always match on the full key, not on a shared component.
    """

    @classmethod
    def setUpTestData(cls):
        cls.tenant_1 = Tenant.objects.create()
        cls.tenant_2 = Tenant.objects.create()
        cls.tenant_3 = Tenant.objects.create()
        # Venues sharing exactly one component of the composite key.
        cls.venue_1a = Venue.objects.create(tenant=cls.tenant_1, code="a", name="1a")
        cls.venue_1b = Venue.objects.create(tenant=cls.tenant_1, code="b", name="1b")
        cls.venue_2a = Venue.objects.create(tenant=cls.tenant_2, code="a", name="2a")
        cls.reservation_1a = Reservation.objects.create(venue=cls.venue_1a)
        cls.reservation_1b = Reservation.objects.create(venue=cls.venue_1b)
        cls.reservation_2a = Reservation.objects.create(venue=cls.venue_2a)

    def test_local_values_mapped_in_declaration_order(self):
        self.assertEqual(
            (
                self.reservation_1a.venue_tenant_id,
                self.reservation_1a.venue_code,
            ),
            (self.tenant_1.id, "a"),
        )
        self.assertEqual(
            (
                self.reservation_2a.venue_tenant_id,
                self.reservation_2a.venue_code,
            ),
            (self.tenant_2.id, "a"),
        )

    def test_forward_access_matches_full_key(self):
        for reservation, venue in (
            (self.reservation_1a, self.venue_1a),
            (self.reservation_1b, self.venue_1b),
            (self.reservation_2a, self.venue_2a),
        ):
            with self.subTest(reservation=reservation.pk):
                self.assertEqual(
                    Reservation.objects.get(pk=reservation.pk).venue, venue
                )

    def test_reverse_set_matches_full_key(self):
        self.assertSequenceEqual(
            self.venue_1a.reservations.order_by("pk"),
            [self.reservation_1a],
        )
        # Sharing tenant_id with venue_1a must not put this reservation in
        # venue_1b's set.
        self.assertSequenceEqual(
            self.venue_1b.reservations.order_by("pk"),
            [self.reservation_1b],
        )
        # Sharing code with venue_1a must not put this reservation in
        # venue_2a's set.
        self.assertSequenceEqual(
            self.venue_2a.reservations.order_by("pk"),
            [self.reservation_2a],
        )

    def test_filter_count_order_select_related(self):
        for venue, reservation in (
            (self.venue_1a, self.reservation_1a),
            (self.venue_1b, self.reservation_1b),
            (self.venue_2a, self.reservation_2a),
        ):
            with self.subTest(venue=venue.pk):
                self.assertEqual(Reservation.objects.filter(venue=venue).count(), 1)
                self.assertSequenceEqual(
                    Reservation.objects.filter(venue=venue).order_by("pk"),
                    [reservation],
                )
                fetched = Reservation.objects.select_related("venue").get(
                    pk=reservation.pk
                )
                self.assertEqual(fetched.venue, venue)

    def test_filter_by_component_requires_existing_target(self):
        # A dangling reservation that shares the 'a' code but has no matching
        # venue must not be returned by a filter on that shared component.
        dangling = Reservation.objects.create(
            venue_tenant_id=self.tenant_3.id, venue_code="a"
        )
        reservations = Reservation.objects.filter(venue__code="a").order_by("pk")
        self.assertEqual(set(reservations), {self.reservation_1a, self.reservation_2a})
        self.assertNotIn(dangling, reservations)
        self.assertEqual(
            Reservation.objects.filter(venue__tenant_id=self.tenant_3.id).count(),
            0,
        )

    def test_multiple_conditions_constrain_same_row(self):
        # Two conditions in one query hit the same join alias, so a value
        # combining components from different venues matches nothing.
        self.assertSequenceEqual(
            Reservation.objects.filter(
                venue__tenant_id=self.tenant_1.id, venue__code="a"
            ).order_by("pk"),
            [self.reservation_1a],
        )
        self.assertFalse(
            Reservation.objects.filter(
                venue__tenant_id=self.tenant_2.id, venue__code="b"
            ).exists()
        )
        self.assertFalse(
            Reservation.objects.filter(venue=self.venue_1a)
            .exclude(venue=self.venue_1a)
            .exists()
        )

    def test_repeated_evaluation_and_slicing(self):
        queryset = Reservation.objects.filter(venue__code="a").order_by("pk")
        self.assertEqual(list(queryset), list(queryset))
        full = list(queryset)
        self.assertEqual(list(queryset[:1]) + list(queryset[1:]), full)
        self.assertEqual(list(queryset[::1]), full)

    def test_rebind_when_local_component_changes(self):
        reservation = Reservation.objects.get(pk=self.reservation_1a.pk)
        reservation.venue = self.venue_2a
        reservation.save()

        reservation = Reservation.objects.get(pk=reservation.pk)
        self.assertEqual(reservation.venue, self.venue_2a)
        # The old target no longer claims the reservation, and the neighbour
        # that shared a component is untouched.
        self.assertFalse(self.venue_1a.reservations.exists())
        self.assertSequenceEqual(
            self.venue_2a.reservations.order_by("pk"),
            [reservation, self.reservation_2a],
        )
        self.assertSequenceEqual(
            self.venue_1b.reservations.order_by("pk"),
            [self.reservation_1b],
        )

    def test_missing_target_direct_access_raises(self):
        reservation = Reservation.objects.create(
            venue_tenant_id=self.tenant_1.id, venue_code="missing"
        )
        with self.assertRaises(Venue.DoesNotExist):
            reservation.venue

    def test_partial_component_mismatch_raises(self):
        # (tenant_2, 'b') combines components that each exist on some venue
        # but no venue has that full key.
        reservation = Reservation.objects.create(
            venue_tenant_id=self.tenant_2.id, venue_code="b"
        )
        with self.assertRaises(Venue.DoesNotExist):
            reservation.venue

    def test_missing_target_not_in_queries_or_reverse_set(self):
        reservation = Reservation.objects.create(
            venue_tenant_id=self.tenant_1.id, venue_code="missing"
        )
        self.assertFalse(Venue.objects.filter(reservations__pk=reservation.pk).exists())
        self.assertFalse(Reservation.objects.filter(venue__code="missing").exists())
        self.assertEqual(Reservation.objects.filter(venue__code="missing").count(), 0)

    def test_failed_access_does_not_write(self):
        reservation = Reservation.objects.create(
            venue_tenant_id=self.tenant_1.id, venue_code="missing"
        )
        before = list(
            Reservation.objects.order_by("pk").values_list(
                "venue_tenant_id", "venue_code"
            )
        )
        with self.assertRaises(Venue.DoesNotExist):
            reservation.venue
        after = list(
            Reservation.objects.order_by("pk").values_list(
                "venue_tenant_id", "venue_code"
            )
        )
        self.assertEqual(before, after)
        # Other reservations are unaffected.
        self.assertEqual(
            Reservation.objects.get(pk=self.reservation_1a.pk).venue,
            self.venue_1a,
        )

    def test_create_and_update_source_with_missing_target(self):
        reservation = Reservation.objects.create(
            venue_tenant_id=self.tenant_3.id, venue_code="nope"
        )
        reservation.venue_code = "still-nope"
        reservation.save()
        self.assertEqual(
            Reservation.objects.get(pk=reservation.pk).venue_code,
            "still-nope",
        )

    def test_null_components(self):
        reservation = Reservation.objects.create()
        self.assertIsNone(reservation.venue)
        self.assertIsNone(Reservation.objects.get(pk=reservation.pk).venue)
        self.assertSequenceEqual(
            Reservation.objects.filter(venue__isnull=True).order_by("pk"),
            [reservation],
        )

    def test_empty_string_component(self):
        venue = Venue.objects.create(tenant=self.tenant_3, code="")
        reservation = Reservation.objects.create(
            venue_tenant_id=self.tenant_3.id, venue_code=""
        )
        self.assertEqual(reservation.venue, venue)
        self.assertEqual(Reservation.objects.get(pk=reservation.pk).venue, venue)
        # An empty string is not NULL and must not collide with it.
        self.assertEqual(Reservation.objects.filter(venue__isnull=True).count(), 0)

    def test_component_type_conversion_round_trip(self):
        # Each local field applies its own type rules on save:
        # SmallIntegerField converts a numeric string, CharField stringifies.
        venue = Venue.objects.create(tenant=self.tenant_1, code="97")
        reservation = Reservation.objects.create(
            venue_tenant_id=str(self.tenant_1.id), venue_code=97
        )
        reservation = Reservation.objects.get(pk=reservation.pk)
        self.assertEqual(reservation.venue_tenant_id, self.tenant_1.id)
        self.assertIsInstance(reservation.venue_tenant_id, int)
        self.assertEqual(reservation.venue_code, "97")
        self.assertEqual(reservation.venue, venue)

    def test_delete_target_does_not_cascade(self):
        self.venue_1a.delete()
        # The reservation survives because ForeignObject creates no database
        # foreign key and on_delete is DO_NOTHING.
        reservation = Reservation.objects.get(pk=self.reservation_1a.pk)
        with self.assertRaises(Venue.DoesNotExist):
            reservation.venue
        # Targets sharing a component with the deleted venue still resolve.
        self.assertEqual(
            Reservation.objects.get(pk=self.reservation_2a.pk).venue,
            self.venue_2a,
        )
        self.assertEqual(
            Reservation.objects.get(pk=self.reservation_1b.pk).venue,
            self.venue_1b,
        )
        # No reservation moved or got deleted.
        self.assertEqual(Reservation.objects.count(), 3)

    def test_ordering_by_relation_component(self):
        dangling = Reservation.objects.create(
            venue_tenant_id=self.tenant_3.id, venue_code="zzz"
        )
        # The dangling row sorts first on SQLite (NULL joined component);
        # bound rows follow in target-column order and never cross keys.
        reservations = list(Reservation.objects.order_by("venue__code", "pk"))
        self.assertEqual(reservations[0], dangling)
        self.assertEqual(
            [r.venue_code for r in reservations[1:]],
            ["a", "a", "b"],
        )
        self.assertEqual(
            [r.venue for r in reservations[1:]],
            [self.venue_1a, self.venue_2a, self.venue_1b],
        )

    def test_prefetch_forward_and_reverse(self):
        reservations = list(
            Reservation.objects.filter(
                pk__in=[
                    self.reservation_1a.pk,
                    self.reservation_2a.pk,
                ]
            )
            .select_related("venue")
            .order_by("pk")
        )
        self.assertEqual(
            [r.venue for r in reservations],
            [self.venue_1a, self.venue_2a],
        )
        venues = list(
            Venue.objects.filter(
                pk__in=[self.venue_1a.pk, self.venue_2a.pk]
            ).prefetch_related("reservations")
        )
        self.assertEqual(
            [list(v.reservations.all()) for v in venues],
            [[self.reservation_1a], [self.reservation_2a]],
        )
