from django.contrib.contenttypes.models import ContentType
from django.db import connection
from django.test import TestCase
from django.test.utils import CaptureQueriesContext

from .models import (
    Answer,
    GFKCompositeAttachment,
    GFKCompositeTarget,
    Question,
)


def _row(source):
    return (
        GFKCompositeAttachment.objects.filter(pk=source.pk)
        .values("content_type_id", "object_id")
        .get()
    )


class GFKCompositeRelationManagerTestCase(TestCase):
    @classmethod
    def setUpTestData(cls):
        # Two targets sharing exactly one primary key component (code=1).
        cls.target_a = GFKCompositeTarget.objects.create(code=1, tenant="1")
        cls.target_b = GFKCompositeTarget.objects.create(code=1, tenant="2")
        # A third target sharing the other component (tenant="1") with a.
        cls.target_c = GFKCompositeTarget.objects.create(code=2, tenant="1")
        cls.source_a = GFKCompositeAttachment.objects.create(
            text="a", content_object=cls.target_a
        )
        cls.source_b = GFKCompositeAttachment.objects.create(
            text="b", content_object=cls.target_b
        )
        cls.content_type = ContentType.objects.get_for_model(GFKCompositeTarget)
        cls.other_content_type = ContentType.objects.get_for_model(Question)

    def _get(self, target):
        return GFKCompositeTarget.objects.get(pk=target.pk)

    # -- add --------------------------------------------------------------

    def test_add_points_source_at_target(self):
        source = GFKCompositeAttachment.objects.create(text="free")
        self.target_a.attachments.add(source)
        self.assertEqual(
            _row(source),
            {"content_type_id": self.content_type.pk, "object_id": '[1, "1"]'},
        )
        self.assertCountEqual(self.target_a.attachments.all(), [self.source_a, source])
        # Forward relation, fresh manager read and prefetch all agree.
        source.refresh_from_db()
        self.assertEqual(source.content_object, self.target_a)
        self.assertCountEqual(
            self._get(self.target_a).attachments.all(), [self.source_a, source]
        )
        prefetched = GFKCompositeTarget.objects.prefetch_related("attachments")
        result = {t.pk: list(t.attachments.all()) for t in prefetched}
        self.assertCountEqual(result[(1, "1")], [self.source_a, source])
        self.assertEqual(result[(1, "2")], [self.source_b])

    def test_add_is_idempotent(self):
        self.target_a.attachments.add(self.source_a)
        # Re-adding a source already related performs no write.
        with CaptureQueriesContext(connection) as captured:
            self.target_a.attachments.add(self.source_a)
        self.assertFalse(
            any(
                query["sql"]
                .lstrip("(")
                .upper()
                .startswith(("INSERT", "UPDATE", "DELETE"))
                for query in captured.captured_queries
            )
        )
        self.assertEqual(
            _row(self.source_a),
            {"content_type_id": self.content_type.pk, "object_id": '[1, "1"]'},
        )
        self.assertEqual(list(self.target_a.attachments.all()), [self.source_a])
        # The shared-component sibling is untouched.
        self.assertEqual(list(self.target_b.attachments.all()), [self.source_b])

    def test_add_with_bulk_false_saves_unsaved_source(self):
        source = GFKCompositeAttachment(text="unsaved")
        self.target_a.attachments.add(source, bulk=False)
        self.assertFalse(source._state.adding)
        source.refresh_from_db()
        self.assertEqual(source.content_object, self.target_a)
        self.assertCountEqual(self.target_a.attachments.all(), [self.source_a, source])

    def test_add_reassigns_source_of_same_content_type(self):
        # Standard generic relation semantics: adding a source related to
        # another object of the same content type reassigns it.
        self.target_a.attachments.add(self.source_b)
        self.assertEqual(
            _row(self.source_b),
            {"content_type_id": self.content_type.pk, "object_id": '[1, "1"]'},
        )
        self.assertEqual(list(self.target_b.attachments.all()), [])
        self.assertCountEqual(
            self.target_a.attachments.all(), [self.source_a, self.source_b]
        )

    def test_add_does_not_mutate_failed_sources_in_memory(self):
        good = GFKCompositeAttachment.objects.create(text="good")
        unsaved = GFKCompositeAttachment(text="unsaved")
        with self.assertRaises(ValueError):
            self.target_a.attachments.add(good, unsaved)
        self.assertIsNone(good.content_type_id)
        self.assertIsNone(good.object_id)
        # And the failed batch left nothing in the database.
        self.assertEqual(_row(good), {"content_type_id": None, "object_id": None})
        self.assertEqual(list(self.target_a.attachments.all()), [self.source_a])
        self.assertEqual(list(self.target_b.attachments.all()), [self.source_b])

    # -- remove -----------------------------------------------------------

    def test_remove_deletes_only_sources_with_full_key_match(self):
        self.target_a.attachments.remove(self.source_b)
        # source_b points at (1, "2"); sharing code=1 must not match a.
        self.assertTrue(
            GFKCompositeAttachment.objects.filter(pk=self.source_b.pk).exists()
        )
        self.assertEqual(
            _row(self.source_b),
            {"content_type_id": self.content_type.pk, "object_id": '[1, "2"]'},
        )
        # Removing the source actually related deletes it.
        self.target_a.attachments.remove(self.source_a)
        self.assertFalse(
            GFKCompositeAttachment.objects.filter(pk=self.source_a.pk).exists()
        )
        self.assertEqual(list(self.target_a.attachments.all()), [])
        self.assertEqual(list(self.target_b.attachments.all()), [self.source_b])

    def test_remove_nonexistent_source_is_a_noop(self):
        deleted = GFKCompositeAttachment.objects.create(
            text="gone", content_object=self.target_a
        )
        deleted.delete()
        # No error removing an already deleted source.
        self.target_a.attachments.remove(deleted)
        # A source that was never related is ignored as well.
        stranger = GFKCompositeAttachment.objects.create(text="stranger")
        self.target_a.attachments.remove(stranger)
        self.assertTrue(GFKCompositeAttachment.objects.filter(pk=stranger.pk).exists())

    def test_remove_failure_is_atomic(self):
        own = GFKCompositeAttachment.objects.create(
            text="own", content_object=self.target_a
        )
        wrong_ct = GFKCompositeAttachment.objects.create(text="wrong")
        GFKCompositeAttachment.objects.filter(pk=wrong_ct.pk).update(
            content_type=self.other_content_type, object_id="1"
        )
        with self.assertRaises(ValueError):
            self.target_a.attachments.remove(own, wrong_ct)
        # Neither the valid nor the invalid source was deleted.
        self.assertTrue(GFKCompositeAttachment.objects.filter(pk=own.pk).exists())
        self.assertEqual(
            _row(wrong_ct),
            {"content_type_id": self.other_content_type.pk, "object_id": "1"},
        )
        self.assertEqual(list(self.target_b.attachments.all()), [self.source_b])

    # -- clear ------------------------------------------------------------

    def test_clear_only_deletes_current_targets_sources(self):
        extra = GFKCompositeAttachment.objects.create(
            text="extra", content_object=self.target_a
        )
        # Invalid references and other content types coexist in the table.
        stray = GFKCompositeAttachment.objects.create(text="stray")
        GFKCompositeAttachment.objects.filter(pk=stray.pk).update(
            content_type=self.other_content_type, object_id="9"
        )
        broken = GFKCompositeAttachment.objects.create(text="broken")
        GFKCompositeAttachment.objects.filter(pk=broken.pk).update(
            content_type=self.content_type, object_id="{not json"
        )
        self.target_a.attachments.clear()
        self.assertEqual(list(self.target_a.attachments.all()), [])
        for source in (self.source_b, stray, broken):
            self.assertTrue(
                GFKCompositeAttachment.objects.filter(pk=source.pk).exists()
            )
        self.assertFalse(GFKCompositeAttachment.objects.filter(pk=extra.pk).exists())

    # -- set --------------------------------------------------------------

    def test_set_replaces_relation_independent_of_order(self):
        keep = GFKCompositeAttachment.objects.create(
            text="keep", content_object=self.target_a
        )
        gone = GFKCompositeAttachment.objects.create(
            text="gone", content_object=self.target_a
        )
        fresh = GFKCompositeAttachment.objects.create(text="fresh")
        # Input order must not matter; pass them "wrong way around".
        self.target_a.attachments.set([fresh, keep])
        keep.refresh_from_db()
        fresh.refresh_from_db()
        self.assertCountEqual(self.target_a.attachments.all(), [keep, fresh])
        self.assertEqual(keep.object_id, '[1, "1"]')
        self.assertEqual(fresh.object_id, '[1, "1"]')
        self.assertFalse(GFKCompositeAttachment.objects.filter(pk=gone.pk).exists())
        # The sibling sharing code=1 is unaffected.
        self.assertEqual(list(self.target_b.attachments.all()), [self.source_b])

    def test_set_clear_true(self):
        keep = GFKCompositeAttachment.objects.create(
            text="keep", content_object=self.target_a
        )
        fresh = GFKCompositeAttachment.objects.create(text="fresh")
        self.target_a.attachments.set([fresh, keep], clear=True)
        self.assertCountEqual(self.target_a.attachments.all(), [keep, fresh])
        self.assertFalse(
            GFKCompositeAttachment.objects.filter(pk=self.source_a.pk).exists()
        )
        self.assertEqual(list(self.target_b.attachments.all()), [self.source_b])

    def test_set_empty_removes_everything(self):
        self.target_a.attachments.set([])
        self.assertEqual(list(self.target_a.attachments.all()), [])
        self.assertFalse(
            GFKCompositeAttachment.objects.filter(pk=self.source_a.pk).exists()
        )
        self.assertEqual(list(self.target_b.attachments.all()), [self.source_b])

    def test_set_failure_leaves_relation_unchanged(self):
        good = GFKCompositeAttachment.objects.create(text="good")
        for clear in (False, True):
            with self.subTest(clear=clear):
                # Re-create the pre-existing relation for each subtest.
                own = GFKCompositeAttachment.objects.create(
                    text="own-%s" % clear, content_object=self.target_a
                )
                before = {
                    s.pk: (s.content_type_id, s.object_id)
                    for s in GFKCompositeAttachment.objects.all()
                }
                wrong_ct = GFKCompositeAttachment.objects.create(
                    text="wrong-%s" % clear
                )
                GFKCompositeAttachment.objects.filter(pk=wrong_ct.pk).update(
                    content_type=self.other_content_type, object_id="1"
                )
                with self.assertRaises(ValueError):
                    self.target_a.attachments.set([good, wrong_ct], clear=clear)
                after = {
                    s.pk: (s.content_type_id, s.object_id)
                    for s in GFKCompositeAttachment.objects.all()
                }
                before[wrong_ct.pk] = (self.other_content_type.pk, "1")
                self.assertEqual(after, before)
                # The relation that existed before the call is intact.
                self.assertIn(own, list(self.target_a.attachments.all()))
                self.assertEqual(
                    _row(good),
                    {"content_type_id": None, "object_id": None},
                )

    def test_set_with_unsaved_source_fails_before_clear(self):
        unsaved = GFKCompositeAttachment(text="unsaved")
        with self.assertRaises(ValueError):
            self.target_a.attachments.set([unsaved], clear=True)
        # clear=True must not have deleted anything.
        self.assertTrue(
            GFKCompositeAttachment.objects.filter(pk=self.source_a.pk).exists()
        )
        self.assertEqual(list(self.target_a.attachments.all()), [self.source_a])

    def test_remove_with_unsaved_source_fails_without_deleting(self):
        own = GFKCompositeAttachment.objects.create(
            text="own", content_object=self.target_a
        )
        unsaved = GFKCompositeAttachment(text="unsaved")
        with self.assertRaisesMessage(ValueError, "isn't saved"):
            self.target_a.attachments.remove(own, unsaved)
        # The saved source in the same batch was not deleted.
        self.assertTrue(GFKCompositeAttachment.objects.filter(pk=own.pk).exists())
        self.assertEqual(list(self.target_b.attachments.all()), [self.source_b])

    def test_set_with_malformed_reference_fails_before_writes(self):
        good = GFKCompositeAttachment.objects.create(text="good")
        broken = GFKCompositeAttachment.objects.create(text="broken")
        GFKCompositeAttachment.objects.filter(pk=broken.pk).update(
            content_type=self.content_type, object_id="[1]"
        )
        for bulk in (True, False):
            with self.subTest(bulk=bulk):
                with self.assertRaises(ValueError):
                    self.target_a.attachments.set([good, broken], bulk=bulk)
                self.assertEqual(
                    _row(good),
                    {"content_type_id": None, "object_id": None},
                )
                self.assertEqual(_row(broken)["object_id"], "[1]")
                # The pre-existing relation survived.
                self.assertEqual(list(self.target_a.attachments.all()), [self.source_a])

    def test_set_bulk_false(self):
        fresh = GFKCompositeAttachment(text="fresh")
        keep = GFKCompositeAttachment.objects.create(
            text="keep", content_object=self.target_a
        )
        self.target_a.attachments.set([keep, fresh], bulk=False)
        self.assertFalse(fresh._state.adding)
        self.assertCountEqual(self.target_a.attachments.all(), [keep, fresh])
        self.assertFalse(
            GFKCompositeAttachment.objects.filter(pk=self.source_a.pk).exists()
        )
        fresh.refresh_from_db()
        self.assertEqual(fresh.content_object, self.target_a)

    def test_failed_add_preserves_source_fields(self):
        source = GFKCompositeAttachment.objects.create(text="untouched")
        wrong_ct = GFKCompositeAttachment.objects.create(text="wrong")
        GFKCompositeAttachment.objects.filter(pk=wrong_ct.pk).update(
            content_type=self.other_content_type, object_id="1"
        )
        with self.assertRaises(ValueError):
            self.target_a.attachments.add(source, wrong_ct)
        source.refresh_from_db()
        wrong_ct.refresh_from_db()
        self.assertEqual(source.text, "untouched")
        self.assertEqual(wrong_ct.text, "wrong")
        self.assertIsNone(source.object_id)

    # -- invalid sources never get written --------------------------------

    def test_add_rejects_unsaved_source_in_bulk_mode(self):
        unsaved = GFKCompositeAttachment(text="unsaved")
        with self.assertRaisesMessage(ValueError, "isn't saved"):
            self.target_a.attachments.add(unsaved)

    def test_add_rejects_wrong_instance_type(self):
        with self.assertRaises(TypeError):
            self.target_a.attachments.add(Question.objects.create(text="nope"))

    def test_add_rejects_different_content_type(self):
        source = GFKCompositeAttachment.objects.create(text="other-ct")
        GFKCompositeAttachment.objects.filter(pk=source.pk).update(
            content_type=self.other_content_type, object_id="1"
        )
        with self.assertRaisesMessage(ValueError, "different content type"):
            self.target_a.attachments.add(source)
        self.assertEqual(
            _row(source),
            {"content_type_id": self.other_content_type.pk, "object_id": "1"},
        )

    def test_add_rejects_malformed_references(self):
        cases = {
            "bad-json": "{not json",
            "not-an-array": '{"code": 1, "tenant": "1"}',
            "too-few": "[1]",
            "too-many": '[1, "1", 3]',
            "bad-component": '["oops", "1"]',
            "type-confused": "[1, 1]",
        }
        for label, reference in cases.items():
            with self.subTest(label=label):
                source = GFKCompositeAttachment.objects.create(text=label)
                GFKCompositeAttachment.objects.filter(pk=source.pk).update(
                    content_type=self.content_type, object_id=reference
                )
                with self.assertRaises(ValueError):
                    self.target_a.attachments.add(source)
                self.assertEqual(_row(source)["object_id"], reference)

    def test_remove_rejects_malformed_references_without_touching_them(self):
        cases = {
            "bad-json": "{not json",
            "not-an-array": '{"code": 1, "tenant": "1"}',
            "too-few": "[1]",
            "too-many": '[1, "1", 3]',
            "bad-component": '["oops", "1"]',
            "type-confused": "[1, 1]",
        }
        own = GFKCompositeAttachment.objects.create(
            text="own", content_object=self.target_a
        )
        for label, reference in cases.items():
            with self.subTest(label=label):
                source = GFKCompositeAttachment.objects.create(text=label)
                GFKCompositeAttachment.objects.filter(pk=source.pk).update(
                    content_type=self.content_type, object_id=reference
                )
                with self.assertRaises(ValueError):
                    self.target_a.attachments.remove(own, source)
                self.assertEqual(_row(source)["object_id"], reference)
                self.assertTrue(
                    GFKCompositeAttachment.objects.filter(pk=own.pk).exists()
                )

    def test_well_formed_reference_to_other_target_is_not_removed(self):
        # [1, null] is a well-formed reference that can match no target.
        dangling = GFKCompositeAttachment.objects.create(text="null-component")
        GFKCompositeAttachment.objects.filter(pk=dangling.pk).update(
            content_type=self.content_type, object_id="[1, null]"
        )
        self.target_a.attachments.remove(dangling)
        self.assertEqual(_row(dangling)["object_id"], "[1, null]")

    # -- target must exist -------------------------------------------------

    def test_unsaved_target_cannot_manage_sources(self):
        target = GFKCompositeTarget(code=5, tenant="z")
        source = GFKCompositeAttachment.objects.create(text="s")
        operations = {
            "add": lambda: target.attachments.add(source),
            "set": lambda: target.attachments.set([source]),
            "clear": target.attachments.clear,
            "create": lambda: target.attachments.create(text="c"),
        }
        for name, operation in operations.items():
            with self.subTest(operation=name):
                with self.assertRaisesMessage(ValueError, "primary key value"):
                    operation()
        self.assertEqual(_row(source), {"content_type_id": None, "object_id": None})

    def test_deleted_target_operations_fail_without_writes(self):
        target = GFKCompositeTarget.objects.create(code=7, tenant="x")
        source = GFKCompositeAttachment.objects.create(text="x", content_object=target)
        target_pk = target.pk
        # A second in-memory copy fetched before the deletion keeps its key
        # components but no longer corresponds to a database row.
        stale = GFKCompositeTarget.objects.get(pk=target_pk)
        target.delete()
        self.assertFalse(GFKCompositeTarget.objects.filter(pk=target_pk).exists())
        # The stale Python instance must not be able to create dangling
        # references.
        free = GFKCompositeAttachment.objects.create(text="free")
        with self.assertRaises(GFKCompositeTarget.DoesNotExist):
            stale.attachments.add(free)
        with self.assertRaises(GFKCompositeTarget.DoesNotExist):
            stale.attachments.set([free])
        with self.assertRaises(GFKCompositeTarget.DoesNotExist):
            stale.attachments.clear()
        with self.assertRaises(GFKCompositeTarget.DoesNotExist):
            stale.attachments.create(text="created")
        # The source that was cascaded on delete is gone, nothing else moved.
        self.assertFalse(GFKCompositeAttachment.objects.filter(pk=source.pk).exists())
        self.assertEqual(_row(free), {"content_type_id": None, "object_id": None})
        self.assertEqual(list(self.target_b.attachments.all()), [self.source_b])

    # -- special components ------------------------------------------------

    def test_special_components_survive_add_remove_and_set(self):
        special = [
            "",
            "None",
            "literal@at",
            "a/b",
            "a,b",
            "a\\b",
            'quote "x"',
            "café ★ → 日本語",
        ]
        targets = {}
        for label in special:
            targets[label] = GFKCompositeTarget.objects.create(code=30, tenant=label)
            # A sibling that shares the code component must never be matched.
            GFKCompositeTarget.objects.create(code=30, tenant=label + "-sibling")
        sources = {
            label: GFKCompositeAttachment.objects.create(text=label)
            for label in special
        }
        # Add every source through its target's manager.
        for label in special:
            targets[label].attachments.add(sources[label])
        # Cross-check through direct access, a fresh query and prefetch.
        for label in special:
            with self.subTest(label=label):
                sources[label].refresh_from_db()
                self.assertEqual(sources[label].content_object, targets[label])
                self.assertEqual(
                    list(targets[label].attachments.all()), [sources[label]]
                )
        prefetched = {
            target.pk: list(target.attachments.all())
            for target in GFKCompositeTarget.objects.filter(
                code=30, tenant__in=special
            ).prefetch_related("attachments")
        }
        for label in special:
            self.assertEqual(prefetched[(30, label)], [sources[label]])

        # Replace the relation of the empty-string and "None" targets; the
        # encoded references must keep the two labels distinct even though
        # the sources are reassigned across targets.
        empty = targets[""]
        none = targets["None"]
        replacement = GFKCompositeAttachment.objects.create(text="replacement")
        empty.attachments.set([sources["None"]])
        none.attachments.set([replacement])
        self.assertEqual(list(empty.attachments.all()), [sources["None"]])
        self.assertEqual(list(none.attachments.all()), [replacement])
        # The empty-string target's old source was deleted by the replacement.
        self.assertFalse(
            GFKCompositeAttachment.objects.filter(pk=sources[""].pk).exists()
        )
        self.assertEqual(
            GFKCompositeAttachment.objects.get(pk=sources["None"].pk).object_id,
            '[30, ""]',
        )
        self.assertEqual(
            GFKCompositeAttachment.objects.get(pk=replacement.pk).object_id,
            '[30, "None"]',
        )
        # Removing through the wrong target (shared code=30) changes nothing.
        none.attachments.remove(sources["None"])
        self.assertTrue(
            GFKCompositeAttachment.objects.filter(pk=sources["None"].pk).exists()
        )
        # Removing through the owning target deletes precisely that source.
        empty.attachments.remove(sources["None"])
        self.assertFalse(
            GFKCompositeAttachment.objects.filter(pk=sources["None"].pk).exists()
        )
        self.assertTrue(
            GFKCompositeAttachment.objects.filter(pk=replacement.pk).exists()
        )

    # -- cascading ---------------------------------------------------------

    def test_delete_target_cascades_only_to_its_sources(self):
        shared = GFKCompositeAttachment.objects.create(text="shared-code")
        GFKCompositeAttachment.objects.filter(pk=shared.pk).update(
            content_type=self.content_type, object_id='[1, "2"]'
        )
        self.target_a.delete()
        self.assertTrue(GFKCompositeTarget.objects.filter(pk=self.target_b.pk).exists())
        self.assertTrue(GFKCompositeTarget.objects.filter(pk=self.target_c.pk).exists())
        self.assertFalse(
            GFKCompositeAttachment.objects.filter(pk=self.source_a.pk).exists()
        )
        # Both sources pointing at target_b survive, including the one that
        # shares the code=1 component with the deleted target.
        for source in (self.source_b, shared):
            self.assertTrue(
                GFKCompositeAttachment.objects.filter(pk=source.pk).exists()
            )

    # -- cache consistency -------------------------------------------------

    def test_direct_prefetch_and_requery_agree_after_mutations(self):
        first = GFKCompositeAttachment.objects.create(text="first")
        second = GFKCompositeAttachment.objects.create(text="second")
        manager = self.target_a.attachments
        manager.add(first)
        self.assertCountEqual(manager.all(), [self.source_a, first])
        manager.set([first, second])
        self.assertCountEqual(manager.all(), [first, second])
        manager.remove(second)
        self.assertEqual(list(manager.all()), [first])
        # Fresh instance, prefetch and forward GFK access all agree.
        fresh = self._get(self.target_a)
        self.assertEqual(list(fresh.attachments.all()), [first])
        prefetched = next(
            iter(
                GFKCompositeTarget.objects.filter(pk=self.target_a.pk).prefetch_related(
                    "attachments"
                )
            )
        )
        self.assertEqual(list(prefetched.attachments.all()), [first])
        first.refresh_from_db()
        self.assertEqual(first.content_object, fresh)


class SinglePKGenericRelationManagerCompatibilityTestCase(TestCase):
    """Single-field primary key generic relations keep their old behavior."""

    @classmethod
    def setUpTestData(cls):
        cls.question = Question.objects.create(text="q")
        cls.other = Question.objects.create(text="other")

    def test_add_remove_clear_set(self):
        first = Answer.objects.create(text="first", question=self.question)
        second = Answer.objects.create(text="second", question=self.other)
        self.question.answer_set.add(second)
        self.assertCountEqual(self.question.answer_set.all(), [first, second])
        # Re-adding is harmless.
        self.question.answer_set.add(second)
        # Cross removal (answer belongs to another question) is a no-op.
        self.other.answer_set.remove(second)
        self.assertTrue(Answer.objects.filter(pk=second.pk).exists())
        # set() replaces.
        third = Answer.objects.create(text="third", question=self.other)
        self.question.answer_set.set([third])
        self.assertEqual(list(self.question.answer_set.all()), [third])
        self.assertFalse(Answer.objects.filter(pk=first.pk).exists())
        # clear() deletes only this question's answers.
        self.question.answer_set.clear()
        self.assertEqual(list(self.question.answer_set.all()), [])

    def test_bulk_add_still_single_update_for_saved_sources(self):
        answers = [
            Answer.objects.create(text="t1", question=self.question),
            Answer.objects.create(text="t2", question=self.question),
        ]
        bacon = Question.objects.create(text="bacon")
        with self.assertNumQueries(1):
            bacon.answer_set.add(*answers)

    def test_unsaved_source_failure_leaves_good_untouched(self):
        good = Answer.objects.create(text="good", question=self.question)
        unsaved = Answer(text="unsaved")
        with self.assertRaises(ValueError):
            self.question.answer_set.add(good, unsaved)
        row = (
            Answer.objects.filter(pk=good.pk)
            .values("content_type_id", "object_id")
            .get()
        )
        self.assertEqual(row["object_id"], self.question.pk)


class GFKCompositeRelationManagerAsyncTestCase(TestCase):
    @classmethod
    def setUpTestData(cls):
        # Creating the target and a related source synchronously warms the
        # ContentType cache the manager needs when constructed inside the
        # event loop.
        cls.target = GFKCompositeTarget.objects.create(code=4, tenant="z")
        cls.source = GFKCompositeAttachment.objects.create(
            text="s", content_object=cls.target
        )

    async def test_aadd_aset_aclear_acreate(self):
        await self.target.attachments.aadd(self.source)
        self.assertEqual(
            [s async for s in self.target.attachments.all()],
            [self.source],
        )
        # Re-adding is idempotent.
        await self.target.attachments.aadd(self.source)
        self.assertEqual(await self.target.attachments.acount(), 1)
        other = await GFKCompositeAttachment.objects.acreate(text="o")
        await self.target.attachments.aset([other], clear=True)
        self.assertEqual(await self.target.attachments.acount(), 1)
        self.assertFalse(
            await GFKCompositeAttachment.objects.filter(pk=self.source.pk).aexists()
        )
        created = await self.target.attachments.acreate(text="c")
        self.assertEqual(await self.target.attachments.acount(), 2)
        await self.target.attachments.aclear()
        self.assertEqual(await self.target.attachments.acount(), 0)
        self.assertFalse(
            await GFKCompositeAttachment.objects.filter(pk=created.pk).aexists()
        )
