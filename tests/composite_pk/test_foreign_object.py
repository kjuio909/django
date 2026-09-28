from django.db import models
from django.db.models import Q
from django.test import TestCase
from django.test.utils import isolate_apps

from .models import (
    Book,
    BookHold,
    Member,
    MemberBooking,
    Tenant,
)


class ForeignObjectToCompositePKTests(TestCase):
    """
    A ForeignObject spanning the full composite primary key of a target must
    match on every component, so targets that share only one component cannot
    be mistaken for each other.
    """

    @classmethod
    def setUpTestData(cls):
        cls.tenant_1 = Tenant.objects.create()
        cls.tenant_2 = Tenant.objects.create()
        # Targets sharing the second component but not the first.
        cls.member_a = Member.objects.create(
            tenant=cls.tenant_1, id=7, name="a"
        )
        cls.member_b = Member.objects.create(
            tenant=cls.tenant_2, id=7, name="b"
        )
        # Target sharing the first component but not the second.
        cls.member_c = Member.objects.create(
            tenant=cls.tenant_1, id=8, name="c"
        )
        cls.booking_a = MemberBooking.objects.create(
            tenant_id=cls.tenant_1.id, member=cls.member_a
        )
        cls.booking_b = MemberBooking.objects.create(
            tenant_id=cls.tenant_2.id, member=cls.member_b
        )
        cls.booking_c = MemberBooking.objects.create(
            tenant_id=cls.tenant_1.id, member=cls.member_c
        )
        # Text component, shared across tenants.
        cls.book_a = Book.objects.create(
            tenant=cls.tenant_1, isbn="X", title="a"
        )
        cls.book_b = Book.objects.create(
            tenant=cls.tenant_2, isbn="X", title="b"
        )
        cls.hold_a = BookHold.objects.create(
            tenant_id=cls.tenant_1.id, book=cls.book_a
        )
        cls.hold_b = BookHold.objects.create(
            tenant_id=cls.tenant_2.id, book=cls.book_b
        )

    def _reload(self, obj):
        return obj.__class__.objects.get(pk=obj.pk)

    def test_forward_access_matches_full_key(self):
        booking_a = self._reload(self.booking_a)
        booking_b = self._reload(self.booking_b)
        self.assertEqual(booking_a.member, self.member_a)
        self.assertEqual(booking_b.member, self.member_b)
        # Shared "id=7" component alone must not bind to the other tenant's
        # member.
        self.assertNotEqual(booking_a.member, self.member_b)
        self.assertNotEqual(booking_b.member, self.member_a)

    def test_forward_access_text_component(self):
        self.assertEqual(self._reload(self.hold_a).book, self.book_a)
        self.assertEqual(self._reload(self.hold_b).book, self.book_b)

    def test_reverse_set_matches_full_key(self):
        self.assertEqual(list(self.member_a.bookings.all()), [self.booking_a])
        self.assertEqual(list(self.member_b.bookings.all()), [self.booking_b])
        self.assertEqual(list(self.member_c.bookings.all()), [self.booking_c])
        self.assertEqual(self.member_a.bookings.count(), 1)
        self.assertEqual(self.member_b.bookings.count(), 1)
        self.assertEqual(
            list(self.book_a.holds.all()),
            [self.hold_a],
        )
        self.assertEqual(
            list(self.book_b.holds.all()),
            [self.hold_b],
        )

    def test_filter_and_count(self):
        self.assertEqual(
            list(MemberBooking.objects.filter(member=self.member_a)),
            [self.booking_a],
        )
        self.assertEqual(
            list(MemberBooking.objects.filter(member=self.member_b)),
            [self.booking_b],
        )
        self.assertEqual(
            MemberBooking.objects.filter(member=self.member_a).count(),
            1,
        )

    def test_filter_by_tuple(self):
        self.assertEqual(
            list(MemberBooking.objects.filter(member=(self.tenant_1.id, 7))),
            [self.booking_a],
        )
        self.assertEqual(
            list(MemberBooking.objects.filter(member=(self.tenant_2.id, 7))),
            [self.booking_b],
        )

    def test_filter_by_shared_component(self):
        # Filtering on the shared component spans tenants, but adding the
        # tenant component constrains the same rows.
        self.assertCountEqual(
            MemberBooking.objects.filter(member__id=7),
            [self.booking_a, self.booking_b],
        )
        self.assertCountEqual(
            MemberBooking.objects.filter(
                member__id=7, tenant_id=self.tenant_1.id
            ),
            [self.booking_a],
        )

    def test_filter_in_collection_of_tuples(self):
        self.assertCountEqual(
            MemberBooking.objects.filter(
                member__in=[(self.tenant_1.id, 7), (self.tenant_2.id, 7)]
            ),
            [self.booking_a, self.booking_b],
        )

    def test_multiple_conditions_constrain_same_row(self):
        # A source row cannot point at two targets at once.
        self.assertEqual(
            list(
                MemberBooking.objects.filter(
                    Q(member=self.member_a) & Q(member=self.member_b)
                )
            ),
            [],
        )
        self.assertEqual(
            list(
                MemberBooking.objects.filter(
                    member=self.member_a, tenant_id=self.tenant_2.id
                )
            ),
            [],
        )

    def test_repeated_evaluation_and_slicing(self):
        queryset = MemberBooking.objects.filter(member=self.member_a).order_by(
            "id"
        )
        first = list(queryset)
        self.assertEqual(list(queryset), first)
        self.assertEqual(list(queryset), first)
        self.assertEqual(list(queryset[:10]), first)
        self.assertEqual(list(queryset[:10][:5]), first)

    def test_ordering_by_relation(self):
        self.assertEqual(
            list(
                MemberBooking.objects.filter(
                    member__in=[self.member_a, self.member_b]
                ).order_by("member")
            ),
            [self.booking_a, self.booking_b],
        )
        self.assertEqual(
            list(
                MemberBooking.objects.filter(
                    member__in=[self.member_a, self.member_b]
                ).order_by("-member")
            ),
            [self.booking_b, self.booking_a],
        )

    def test_select_related(self):
        booking = MemberBooking.objects.select_related("member").get(
            pk=self.booking_a.pk
        )
        with self.assertNumQueries(0):
            self.assertEqual(booking.member, self.member_a)

    def test_prefetch_related(self):
        bookings = list(
            MemberBooking.objects.filter(
                pk__in=[self.booking_a.pk, self.booking_b.pk]
            ).prefetch_related("member")
        )
        with self.assertNumQueries(0):
            self.assertEqual(
                {booking.member for booking in bookings},
                {self.member_a, self.member_b},
            )

    def test_prefetch_reverse_set(self):
        members = list(
            Member.objects.filter(
                pk__in=[self.member_a.pk, self.member_b.pk]
            ).prefetch_related("bookings")
        )
        with self.assertNumQueries(0):
            result = {
                member.pk: list(member.bookings.all()) for member in members
            }
        self.assertEqual(result[self.member_a.pk], [self.booking_a])
        self.assertEqual(result[self.member_b.pk], [self.booking_b])

    def test_missing_target(self):
        ghost = MemberBooking.objects.create(
            tenant_id=self.tenant_1.id, member_id=999
        )
        with self.assertRaises(Member.DoesNotExist):
            self._reload(ghost).member
        # Queries and reverse sets do not fabricate the missing target.
        self.assertEqual(
            list(
                MemberBooking.objects.filter(
                    member=(self.tenant_1.id, ghost.member_id)
                )
            ),
            [ghost],
        )
        self.assertEqual(
            Member.objects.filter(
                pk=(self.tenant_1.id, ghost.member_id)
            ).count(),
            0,
        )
        self.assertEqual(list(self.member_a.bookings.all()), [self.booking_a])

    def test_missing_target_unknown_tenant(self):
        ghost = MemberBooking.objects.create(tenant_id=999, member_id=999)
        with self.assertRaises(Member.DoesNotExist):
            self._reload(ghost).member
        # The failed read must not have written anything.
        reloaded = self._reload(ghost)
        self.assertEqual((reloaded.tenant_id, reloaded.member_id), (999, 999))

    def test_partial_component_mismatch(self):
        # Same tenant as member_a, but an id no member has.
        ghost = MemberBooking.objects.create(
            tenant_id=self.tenant_1.id, member_id=4242
        )
        with self.assertRaises(Member.DoesNotExist):
            self._reload(ghost).member
        self.assertEqual(list(self.member_a.bookings.all()), [self.booking_a])

    def test_rebind_second_component(self):
        booking = self._reload(self.booking_a)
        booking.member_id = self.member_c.id
        booking.save()
        reloaded = self._reload(booking)
        self.assertEqual(reloaded.member, self.member_c)
        self.assertEqual(list(self.member_a.bookings.all()), [])
        self.assertCountEqual(
            list(self.member_c.bookings.all()),
            [self.booking_c, reloaded],
        )

    def test_rebind_first_component(self):
        # Change the shared-component discriminator as well.
        booking = self._reload(self.booking_a)
        booking.tenant_id = self.tenant_2.id
        booking.member_id = self.member_b.id
        booking.save()
        reloaded = self._reload(booking)
        self.assertEqual(reloaded.member, self.member_b)
        self.assertEqual(list(self.member_a.bookings.all()), [])
        self.assertCountEqual(
            list(self.member_b.bookings.all()),
            [self.booking_b, reloaded],
        )

    def test_rebind_does_not_touch_neighbors(self):
        booking = self._reload(self.booking_a)
        booking.tenant_id = self.tenant_2.id
        booking.member_id = 7
        booking.save()
        # The source pointing at the other target is unchanged.
        self.assertEqual(
            self._reload(self.booking_b).member,
            self.member_b,
        )

    def test_deleting_target_does_not_cascade(self):
        Member.objects.get(pk=self.member_c.pk).delete()
        # The ForeignObject establishes no database foreign key, so the
        # source row survives.
        self.assertTrue(
            MemberBooking.objects.filter(pk=self.booking_c.pk).exists()
        )
        with self.assertRaises(Member.DoesNotExist):
            self._reload(self.booking_c).member
        # Targets sharing a component still resolve.
        self.assertEqual(
            self._reload(self.booking_b).member,
            self.member_b,
        )

    def test_deleting_target_with_shared_component(self):
        # Deleting the (tenant_1, X) book must not affect the (tenant_2, X)
        # book nor its hold.
        Book.objects.get(pk=self.book_a.pk).delete()
        self.assertTrue(BookHold.objects.filter(pk=self.hold_a.pk).exists())
        with self.assertRaises(Book.DoesNotExist):
            self._reload(self.hold_a).book
        self.assertEqual(self._reload(self.hold_b).book, self.book_b)

    def test_null_component(self):
        booking = MemberBooking.objects.create(
            tenant_id=self.tenant_1.id, member_id=None
        )
        self.assertIsNone(self._reload(booking).member)
        # A partially null key never appears on any reverse set.
        self.assertEqual(list(self.member_a.bookings.all()), [self.booking_a])
        self.assertEqual(list(self.member_c.bookings.all()), [self.booking_c])

    def test_null_text_component(self):
        hold = BookHold.objects.create(tenant_id=self.tenant_1.id, isbn=None)
        self.assertIsNone(self._reload(hold).book)

    def test_special_strings_round_trip(self):
        for isbn in ("", "  ", "héllo/1", "x'y\"z"):
            with self.subTest(isbn=isbn):
                book = Book.objects.create(
                    tenant=self.tenant_1, isbn=isbn, title=isbn
                )
                hold = BookHold.objects.create(
                    tenant_id=self.tenant_1.id, book=book
                )
                self.assertEqual(self._reload(hold).book, book)
                self.assertEqual(list(book.holds.all()), [hold])

    def test_empty_string_distinct_from_null(self):
        book = Book.objects.create(tenant=self.tenant_1, isbn="")
        empty_hold = BookHold.objects.create(
            tenant_id=self.tenant_1.id, isbn=""
        )
        null_hold = BookHold.objects.create(
            tenant_id=self.tenant_1.id, isbn=None
        )
        self.assertEqual(self._reload(empty_hold).book, book)
        self.assertIsNone(self._reload(null_hold).book)
        self.assertEqual(list(book.holds.all()), [empty_hold])

    def test_empty_string_stays_distinct_across_tenants(self):
        book_1 = Book.objects.create(tenant=self.tenant_1, isbn="")
        book_2 = Book.objects.create(tenant=self.tenant_2, isbn="")
        hold_1 = BookHold.objects.create(
            tenant_id=self.tenant_1.id, book=book_1
        )
        hold_2 = BookHold.objects.create(
            tenant_id=self.tenant_2.id, book=book_2
        )
        self.assertEqual(self._reload(hold_1).book, book_1)
        self.assertEqual(self._reload(hold_2).book, book_2)
        self.assertEqual(list(book_1.holds.all()), [hold_1])
        self.assertEqual(list(book_2.holds.all()), [hold_2])

    def test_assignment_sets_all_columns(self):
        booking = MemberBooking(tenant_id=-1, member_id=-1)
        booking.member = self.member_b
        self.assertEqual(
            (booking.tenant_id, booking.member_id),
            (self.tenant_2.id, self.member_b.id),
        )

    def test_create_without_target(self):
        # A source with a combination that does not match any target can still
        # be created and updated on its own.
        booking = MemberBooking.objects.create(
            tenant_id=self.tenant_1.id, member_id=555
        )
        booking.note = "updated"
        booking.save()
        self.assertEqual(self._reload(booking).note, "updated")


class ForeignObjectToCompositePKCheckTests(TestCase):
    @isolate_apps("composite_pk")
    def test_checks_accept_full_composite_key_relation(self):
        class TenantTarget(models.Model):
            pass

        class Target(models.Model):
            pk = models.CompositePrimaryKey("tenant_id", "code")
            tenant = models.ForeignKey(TenantTarget, models.CASCADE)
            code = models.CharField(max_length=20)

        class Source(models.Model):
            tenant_id = models.IntegerField()
            code = models.CharField(max_length=20, null=True)
            target = models.ForeignObject(
                Target,
                on_delete=models.DO_NOTHING,
                from_fields=("tenant_id", "code"),
                to_fields=("tenant_id", "code"),
            )

        self.assertEqual(Target.check(databases=[]), [])
        self.assertEqual(Source.check(databases=[]), [])
