from dataclasses import dataclass
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock
from types import SimpleNamespace

from tutoring_demo.consultation import ConsultationState, ExtractionError
from tutoring_demo.runner import PausedError, process_batch
from tutoring_demo.storage import Store


@dataclass
class Event:
    event_id: str
    sort_seq: int
    direction: str = "incoming"
    text: str = "你好"
    is_text: bool = True


def empty_patch(**changes):
    patch = {"grade": None, "subjects": None, "scores": {}, "clarification": None, "off_topic": False}
    patch.update(changes)
    return patch


class FakeExtractor:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def extract(self, state, last_question, message):
        self.calls.append((state.to_dict(), last_question, message))
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response


class FakeAdapter:
    def __init__(self, on_send=None, account_error=None, send_error=None):
        self.on_send = on_send
        self.account_error = account_error
        self.send_error = send_error
        self.account_checks = 0
        self.sent = []

    def assert_account(self):
        self.account_checks += 1
        if self.account_error:
            raise self.account_error

    def send(self, text):
        if self.on_send:
            self.on_send(text)
        self.sent.append(text)
        if self.send_error:
            raise self.send_error
        return "receipt-" + str(len(self.sent))


class RunnerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "state.sqlite3"
        self.store = Store(self.path, "account:test-friend")
        self.store.initialize([])

    def tearDown(self):
        self.store.close()
        self.temp.cleanup()

    def reopen(self):
        self.store.close()
        self.store = Store(self.path, "account:test-friend")

    def test_old_baseline_messages_never_reach_extractor(self):
        self.store.mark_seen(["old"])
        self.store.set_watermark(10)
        extractor, adapter = FakeExtractor([]), FakeAdapter()
        self.assertFalse(process_batch(self.store, adapter, extractor, [Event("old", 10)]))
        self.assertEqual(extractor.calls, [])
        self.assertEqual(adapter.sent, [])

    def test_missing_baseline_pauses_before_extraction_or_send(self):
        with Store(Path(self.temp.name) / "uninitialized.sqlite3", "account:test-friend") as store:
            extractor, adapter = FakeExtractor([]), FakeAdapter()
            with self.assertRaises(PausedError):
                process_batch(store, adapter, extractor, [Event("possibly-historical", 10)])
            self.assertTrue(store.paused_reason())
            self.assertEqual(extractor.calls, [])
            self.assertEqual(adapter.sent, [])
            self.assertFalse(store.seen("possibly-historical"))

    def test_same_sequence_and_same_text_with_distinct_identity_are_processed(self):
        extractor = FakeExtractor([
            empty_patch(grade="初二", subjects=["数学", "英语"]),
            empty_patch(scores={"数学": "80"}),
            empty_patch(scores={"英语": "80"}),
        ])
        adapter = FakeAdapter()
        process_batch(self.store, adapter, extractor, [Event("start", 5, text="初二，数学英语")])
        process_batch(self.store, adapter, extractor, [Event("math", 10, text="80")])
        process_batch(self.store, adapter, extractor, [Event("english", 10, text="80")])
        self.assertEqual([call[2] for call in extractor.calls], ["初二，数学英语", "80", "80"])
        self.assertEqual(self.store.load_state()["scores"], {"数学": "80", "英语": "80"})
        self.assertTrue(self.store.load_state()["completed"])
        self.assertEqual(len(adapter.sent), 3)

    def test_own_echo_and_nontext_are_seen_without_ai(self):
        extractor, adapter = FakeExtractor([]), FakeAdapter()
        events = [Event("own", 10, "outgoing", "自动回复"), Event("photo", 11, "incoming", "", False)]
        self.assertFalse(process_batch(self.store, adapter, extractor, events))
        self.assertEqual(extractor.calls, [])
        self.assertEqual(adapter.sent, [])
        self.assertTrue(self.store.seen("own"))
        self.assertTrue(self.store.seen("photo"))
        self.assertEqual(self.store.watermark(), 11)

    def test_unknown_direction_pauses_before_ai_or_send(self):
        extractor, adapter = FakeExtractor([]), FakeAdapter()
        with self.assertRaises(PausedError):
            process_batch(self.store, adapter, extractor, [Event("unknown", 10, "unknown")])
        self.assertTrue(self.store.paused_reason())
        self.assertEqual(extractor.calls, [])
        self.assertEqual(adapter.sent, [])
        self.assertFalse(self.store.seen("unknown"))

    def test_prepare_is_committed_before_adapter_send(self):
        def assert_durable_before_send(text):
            self.assertTrue(self.store.seen("incoming"))
            self.assertEqual(self.store.load_state()["grade"], "初二")
            self.assertEqual(len(self.store.pending()), 1)
            self.assertEqual(self.store.last_question(), text)

        adapter = FakeAdapter(on_send=assert_durable_before_send)
        extractor = FakeExtractor([empty_patch(grade="初二")])
        self.assertTrue(process_batch(self.store, adapter, extractor, [Event("incoming", 10, text="初二")]))
        self.assertEqual(self.store.pending(), [])
        self.assertTrue(self.store.seen("receipt-1"))

    def test_empty_or_invalid_extraction_never_sends_or_commits_incoming(self):
        for extraction in (None, {}, "", empty_patch(extra="send"), ExtractionError("empty JSON")):
            with self.subTest(extraction=extraction):
                # Separate durable storage for each failure; pauses are intentional and permanent.
                with Store(Path(self.temp.name) / (str(id(extraction)) + ".sqlite3"), "account:test-friend") as store:
                    store.initialize([])
                    adapter = FakeAdapter()
                    extractor = FakeExtractor([extraction])
                    with self.assertRaises(PausedError):
                        process_batch(store, adapter, extractor, [Event("incoming", 10)])
                    self.assertEqual(adapter.sent, [])
                    self.assertEqual(store.pending(), [])
                    self.assertFalse(store.seen("incoming"))
                    self.assertIsNone(store.load_state())
                    self.assertTrue(store.paused_reason())

    def test_uncertain_send_persists_pending_and_restart_never_retries(self):
        extractor = FakeExtractor([empty_patch(grade="初二")])
        adapter = FakeAdapter(send_error=RuntimeError("delivery could not be confirmed"))
        with self.assertRaises(PausedError):
            process_batch(self.store, adapter, extractor, [Event("incoming", 10)])
        self.assertEqual(len(adapter.sent), 1)
        self.assertEqual(len(self.store.pending()), 1)
        self.assertTrue(self.store.seen("incoming"))
        self.reopen()
        restarted_adapter, restarted_extractor = FakeAdapter(), FakeExtractor([])
        with self.assertRaises(PausedError):
            process_batch(self.store, restarted_adapter, restarted_extractor, [Event("incoming", 10), Event("new", 11)])
        self.assertEqual(restarted_adapter.sent, [])
        self.assertEqual(restarted_extractor.calls, [])
        self.assertFalse(self.store.seen("new"))

    def test_crash_after_prepare_before_send_also_pauses_on_restart(self):
        self.store.prepare(["incoming"], ConsultationState().to_dict(), "question", "question")
        self.assertIsNone(self.store.paused_reason())
        self.reopen()
        adapter, extractor = FakeAdapter(), FakeExtractor([])
        with self.assertRaises(PausedError):
            process_batch(self.store, adapter, extractor, [])
        self.assertEqual(adapter.sent, [])
        self.assertEqual(extractor.calls, [])

    def test_completed_thanks_is_committed_without_another_send(self):
        extractor = FakeExtractor([
            empty_patch(grade="初二", subjects=["数学"], scores={"数学": "80"}),
            empty_patch(),
        ])
        adapter = FakeAdapter()
        process_batch(self.store, adapter, extractor, [Event("intake", 10, text="初二数学80")])
        summary = self.store.last_question()
        self.assertFalse(process_batch(self.store, adapter, extractor, [Event("thanks", 11, text="谢谢")]))
        self.assertEqual(len(adapter.sent), 1)
        self.assertTrue(self.store.seen("thanks"))
        self.assertEqual(self.store.last_question(), summary)
        self.assertEqual(self.store.pending(), [])

    def test_composed_reply_is_durably_recorded_and_sent(self):
        extractor, adapter = FakeExtractor([empty_patch(grade="四年级")]), FakeAdapter()
        natural = "好的，四年级。您想重点补哪几科呢？"
        composer = Mock(return_value=natural)
        self.assertTrue(process_batch(self.store, adapter, extractor, [Event("grade", 10, text="四年级")], reply_composer=composer))
        before, after, previous, message, required = composer.call_args.args
        self.assertIsNone(before.grade)
        self.assertEqual(after.grade, "四年级")
        self.assertEqual(message, "四年级")
        self.assertIn("科", required)
        self.assertEqual(adapter.sent, [natural])
        self.assertEqual(self.store.last_question(), natural)
        self.assertEqual(self.store.pending(), [])

    def test_composer_failure_pauses_without_pending_send_or_committed_message(self):
        extractor, adapter = FakeExtractor([empty_patch(grade="四年级")]), FakeAdapter()
        composer = Mock(side_effect=ExtractionError("DeepSeek reply request failed"))
        with self.assertRaises(PausedError):
            process_batch(self.store, adapter, extractor, [Event("grade", 10, text="四年级")], reply_composer=composer)
        self.assertEqual(adapter.sent, [])
        self.assertEqual(self.store.pending(), [])
        self.assertFalse(self.store.seen("grade"))
        self.assertIsNone(self.store.load_state())
        self.assertTrue(self.store.paused_reason())

    def test_completed_thanks_and_outgoing_echo_never_call_composer(self):
        self.store.prepare(["intake"], ConsultationState(grade="初二", subjects=["数学"], scores={"数学": "80"}, completed=True).to_dict(), "summary", None)
        extractor, adapter = FakeExtractor([empty_patch()]), FakeAdapter()
        composer = Mock()
        self.assertFalse(process_batch(self.store, adapter, extractor, [Event("thanks", 11, text="谢谢老师"), Event("echo", 12, "outgoing")], reply_composer=composer))
        composer.assert_not_called()
        self.assertEqual(adapter.sent, [])

    def test_unusable_composed_message_pauses_before_durable_send_intent(self):
        for i, invalid in enumerate(("好的。\n孩子几年级？", "", None, "a" * 1501)):
            with self.subTest(reply=type(invalid).__name__), Store(Path(self.temp.name) / f"reply-{i}.sqlite3", "account:test-friend") as store:
                store.initialize([])
                extractor, adapter = FakeExtractor([empty_patch(grade="四年级")]), FakeAdapter()
                with self.assertRaises(PausedError):
                    process_batch(store, adapter, extractor, [Event("grade", 10, text="四年级")], reply_composer=Mock(return_value=invalid))
                self.assertEqual(adapter.sent, [])
                self.assertEqual(store.pending(), [])
                self.assertFalse(store.seen("grade"))

    def test_restart_composer_cannot_access_old_child_fields_or_question(self):
        old = ConsultationState(grade="四年级", subjects=["数学", "英语"], scores={"数学": "65", "英语": "70"}, completed=True, introduced=True)
        self.store.prepare(["old-intake"], old.to_dict(), "旧孩子数学65英语70的总结", None)
        extractor, adapter = FakeExtractor([empty_patch(start_new_consultation=True)]), FakeAdapter()
        composer = Mock(return_value="您好，可以的。孩子现在读几年级呢？")
        process_batch(self.store, adapter, extractor, [Event("restart", 10, text="你好，想了解补习")], reply_composer=composer)
        before, after, previous, _, _ = composer.call_args.args
        self.assertIsNone(before.grade)
        self.assertEqual(before.subjects, [])
        self.assertEqual(before.scores, {})
        self.assertTrue(before.introduced)
        self.assertEqual(previous, "")
        self.assertIsNone(after.grade)

    def test_single_call_analysis_sends_selected_reply_without_legacy_api_calls(self):
        analysis = SimpleNamespace(patch=empty_patch(grade="四年级"), reply_candidate="好的，四年级。您想补习哪一科呢？")
        analyze, select = Mock(return_value=analysis), Mock(return_value=analysis.reply_candidate)
        extractor, adapter, timings = FakeExtractor([]), FakeAdapter(), []
        process_batch(self.store, adapter, extractor, [Event("grade", 10, text="四年级")], turn_analyzer=analyze, reply_selector=select, timing_callback=timings.append)
        analyze.assert_called_once()
        select.assert_called_once()
        self.assertEqual(extractor.calls, [])
        self.assertEqual(adapter.sent, [analysis.reply_candidate])
        self.assertEqual(self.store.last_question(), analysis.reply_candidate)
        self.assertFalse(self.store.pending())
        self.assertEqual(set(timings[0]), {"model_seconds", "send_seconds", "processing_seconds"})

    def test_single_call_thanks_ignores_candidate_even_if_it_suggests_sending(self):
        self.store.prepare(["intake"], ConsultationState(grade="初二", subjects=["数学"], scores={"数学": "80"}, completed=True).to_dict(), "summary", None)
        analyzer = Mock(return_value=SimpleNamespace(patch=empty_patch(), reply_candidate="再发总结"))
        selector = Mock(return_value="再发总结")
        adapter, extractor = FakeAdapter(), FakeExtractor([])
        self.assertFalse(process_batch(self.store, adapter, extractor, [Event("thanks", 11, text="谢谢老师")], turn_analyzer=analyzer, reply_selector=selector))
        selector.assert_not_called()
        self.assertEqual(adapter.sent, [])

    def test_analysis_failure_never_falls_back_to_another_model_call(self):
        extractor, adapter = FakeExtractor([]), FakeAdapter()
        with self.assertRaises(PausedError):
            process_batch(self.store, adapter, extractor, [Event("grade", 10, text="四年级")], turn_analyzer=Mock(return_value=None))
        self.assertEqual(extractor.calls, [])
        self.assertEqual(adapter.sent, [])
        self.assertEqual(self.store.pending(), [])

    def test_duplicates_within_batch_and_after_restart_are_processed_once(self):
        extractor, adapter = FakeExtractor([empty_patch(grade="初二")]), FakeAdapter()
        event = Event("incoming", 10, text="初二")
        process_batch(self.store, adapter, extractor, [event, event, Event("incoming", 10, text="初二")])
        self.assertEqual(len(extractor.calls), 1)
        self.assertEqual(extractor.calls[0][2], "初二")
        self.assertEqual(len(adapter.sent), 1)
        self.reopen()
        restarted_adapter, restarted_extractor = FakeAdapter(), FakeExtractor([])
        self.assertFalse(process_batch(self.store, restarted_adapter, restarted_extractor, [event]))
        self.assertEqual(restarted_extractor.calls, [])
        self.assertEqual(restarted_adapter.sent, [])

    def test_changed_account_pauses_before_extraction_or_send(self):
        adapter = FakeAdapter(account_error=RuntimeError("account changed"))
        extractor = FakeExtractor([])
        with self.assertRaises(PausedError):
            process_batch(self.store, adapter, extractor, [Event("incoming", 10)])
        self.assertEqual(extractor.calls, [])
        self.assertEqual(adapter.sent, [])
        self.assertTrue(self.store.paused_reason())

    def test_corrupt_saved_state_pauses_instead_of_escaping_unhandled(self):
        with self.store.db:
            self.store._set("state", "{broken json")
        adapter, extractor = FakeAdapter(), FakeExtractor([])
        with self.assertRaises(PausedError):
            process_batch(self.store, adapter, extractor, [Event("incoming", 10)])
        self.assertTrue(self.store.paused_reason())
        self.assertEqual(adapter.sent, [])
        self.assertEqual(extractor.calls, [])


if __name__ == "__main__":
    unittest.main()
