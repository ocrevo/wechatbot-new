from dataclasses import dataclass
from pathlib import Path
import tempfile
import unittest

from tutoring_demo.consultation import ConsultationState
from tutoring_demo.storage import Store, StoreError


@dataclass
class Event:
    event_id: str
    sort_seq: int


class StorageTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "state.sqlite3"
        self.store = Store(self.path, "account:test-friend")

    def tearDown(self):
        self.store.close()
        self.temp.cleanup()

    def reopen(self):
        self.store.close()
        self.store = Store(self.path, "account:test-friend")

    def test_explicit_resume_without_pending_retains_seen_and_state(self):
        state = ConsultationState(grade="初二").to_dict()
        self.store.prepare(["already-done"], state, "科目？", None)
        self.store.pause("模型提取失败")
        self.store.resume_after_review()
        self.assertIsNone(self.store.paused_reason())
        self.assertTrue(self.store.seen("already-done"))
        self.assertEqual(self.store.load_state(), state)

    def test_resume_cannot_clear_uncertain_send(self):
        self.store.prepare(["incoming"], ConsultationState().to_dict(), "年级？", "年级？")
        self.store.pause("发送未确认")
        with self.assertRaises(StoreError):
            self.store.resume_after_review()
        self.assertTrue(self.store.paused_reason())
        self.assertEqual(len(self.store.pending()), 1)

    def test_baseline_records_all_events_and_highest_watermark(self):
        self.store.initialize([Event("old-a", 20), Event("old-b", 20), Event("old-c", 18)])
        self.assertTrue(self.store.is_initialized())
        self.assertEqual(self.store.watermark(), 20)
        self.reopen()
        self.assertTrue(self.store.seen("old-a"))
        self.assertTrue(self.store.seen("old-b"))
        self.assertTrue(self.store.seen("old-c"))
        with self.assertRaises(StoreError):
            self.store.initialize([])

    def test_baseline_accepts_single_pass_iterable(self):
        self.store.initialize(iter([Event("old-a", 20), Event("old-b", 22)]))
        self.assertEqual(self.store.watermark(), 22)

    def test_empty_baseline_and_monotonic_watermark(self):
        self.store.initialize([])
        self.assertEqual(self.store.watermark(), 0)
        self.store.set_watermark(10)
        self.store.set_watermark(9)
        self.assertEqual(self.store.watermark(), 10)

    def test_seen_uses_event_identity_and_persists(self):
        self.store.mark_seen(["event-a", "event-a", "event-b"])
        self.reopen()
        self.assertTrue(self.store.seen("event-a"))
        self.assertTrue(self.store.seen("event-b"))
        self.assertFalse(self.store.seen("event-c"))

    def test_prepare_durably_records_state_before_delivery(self):
        state = ConsultationState(grade="初二", subjects=["数学"], introduced=True).to_dict()
        intent = self.store.prepare(["incoming-a"], state, "数学成绩？", "数学成绩？")
        self.assertTrue(intent)
        self.reopen()
        self.assertTrue(self.store.seen("incoming-a"))
        self.assertEqual(self.store.load_state(), state)
        self.assertEqual(self.store.last_question(), "数学成绩？")
        self.assertEqual(self.store.pending()[0]["id"], intent)

    def test_no_reply_commits_state_and_seen_without_outbox(self):
        state = ConsultationState().to_dict()
        self.assertIsNone(self.store.prepare(["incoming-a"], state, "previous question", None))
        self.assertTrue(self.store.seen("incoming-a"))
        self.assertEqual(self.store.pending(), [])
        self.assertEqual(self.store.last_question(), "previous question")

    def test_duplicate_or_empty_batch_does_not_commit_state(self):
        self.store.mark_seen(["seen"])
        for events in ([], ["duplicate", "duplicate"], ["seen", "new"]):
            with self.subTest(events=events):
                with self.assertRaises(StoreError):
                    self.store.prepare(events, {"grade": "bad"}, "bad", "bad")
        self.assertIsNone(self.store.load_state())
        self.assertFalse(self.store.seen("new"))
        self.assertFalse(self.store.seen("duplicate"))
        self.assertEqual(self.store.pending(), [])

    def test_pending_or_paused_prevents_new_intent(self):
        self.store.prepare(["one"], ConsultationState().to_dict(), "question", "question")
        with self.assertRaises(StoreError):
            self.store.prepare(["two"], ConsultationState().to_dict(), "other", "other")
        self.assertFalse(self.store.seen("two"))
        self.store.pause("check uncertain send")
        self.reopen()
        self.assertEqual(self.store.paused_reason(), "check uncertain send")
        with self.assertRaises(StoreError):
            self.store.prepare(["two"], ConsultationState().to_dict(), "other", None)

    def test_confirm_records_receipt_and_cannot_confirm_again(self):
        intent = self.store.prepare(["incoming"], ConsultationState().to_dict(), "question", "question")
        self.store.confirm(intent, "own-message")
        self.assertEqual(self.store.pending(), [])
        self.assertTrue(self.store.seen("own-message"))
        with self.assertRaises(StoreError):
            self.store.confirm(intent, "another-receipt")
        self.assertFalse(self.store.seen("another-receipt"))
        with self.assertRaises(StoreError):
            self.store.confirm("missing-intent", "not-a-receipt")

    def test_confirm_rejects_missing_receipt_without_clearing_pending(self):
        intent = self.store.prepare(["incoming"], ConsultationState().to_dict(), "question", "question")
        for invalid in (None, "", "   "):
            with self.subTest(receipt=invalid):
                with self.assertRaises(StoreError):
                    self.store.confirm(intent, invalid)
                self.assertEqual(len(self.store.pending()), 1)

    def test_different_account_or_friend_binding_is_rejected(self):
        self.store.prepare(["incoming"], ConsultationState().to_dict(), "question", None)
        with self.assertRaises(StoreError):
            Store(self.path, "different-account:test-friend")
        with self.assertRaises(StoreError):
            Store(self.path, "account:different-friend")
        self.reopen()
        self.assertTrue(self.store.seen("incoming"))


if __name__ == "__main__":
    unittest.main()
