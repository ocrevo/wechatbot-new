"""Serial single-conversation processor with durable send intent."""
import time
from .consultation import ConsultationState, apply_patch


class PausedError(RuntimeError):
    pass


def process_batch(store, adapter, extractor, events, *, reply_composer=None, turn_analyzer=None, reply_selector=None, timing_callback=None):
    if not store.is_initialized():
        store.pause("尚未建立历史基线，禁止处理消息。")
        raise PausedError(store.paused_reason())
    if store.pending():
        store.pause("存在未确认发送，请人工核对；程序不会自动重发。")
    if store.paused_reason():
        raise PausedError(store.paused_reason())
    fresh = []
    ids = set()
    for event in events:
        if event.event_id not in ids and not store.seen(event.event_id):
            fresh.append(event)
            ids.add(event.event_id)
    if not fresh:
        return False
    if any(e.direction == "unknown" for e in fresh):
        store.pause("发现无法确定发送方向的消息，停止自动回复。")
        raise PausedError(store.paused_reason())
    incoming = [e for e in fresh if e.direction == "incoming" and e.is_text]
    ignored = [e for e in fresh if e not in incoming]
    store.mark_seen([e.event_id for e in ignored])
    if not incoming:
        store.set_watermark(max(e.sort_seq for e in fresh))
        return False
    started = time.perf_counter()
    model_seconds = send_seconds = 0.0
    try:
        saved = store.load_state()
        state = ConsultationState.from_dict(saved) if saved else ConsultationState()
        adapter.assert_account()
        parent_message = "\n".join(e.text for e in incoming)
        model_started = time.perf_counter()
        analysis = turn_analyzer(state, store.last_question(), parent_message) if turn_analyzer else None
        patch = analysis.patch if turn_analyzer else extractor.extract(state, store.last_question(), parent_message)
        new_state, reply = apply_patch(state, patch)
        needs_reply = reply is not None
        if needs_reply and reply_selector is not None:
            reply = reply_selector(analysis, state, new_state, reply)
        elif needs_reply and reply_composer is not None:
            # A new consultation must not give the composer the old child's fields.
            fresh_start = patch.start_new_consultation if hasattr(patch, "start_new_consultation") else patch.get("start_new_consultation", False)
            context = ConsultationState(introduced=state.introduced) if fresh_start else state
            previous_question = "" if fresh_start else store.last_question()
            reply = reply_composer(context, new_state, previous_question, parent_message, reply)
        model_seconds = time.perf_counter() - model_started
        if needs_reply:
            from .consultation import ExtractionError
            if not isinstance(reply, str) or not reply.strip() or len(reply) > 1500 or any(c in reply for c in "\r\n\x00"):
                raise ExtractionError("reply must be non-empty single-line text within the sending limit")
        intent = store.prepare([e.event_id for e in incoming], new_state.to_dict(), reply or store.last_question(), reply)
        if intent:
            send_started = time.perf_counter()
            confirmed = adapter.send(reply)
            store.confirm(intent, confirmed)
            send_seconds = time.perf_counter() - send_started
        store.set_watermark(max(e.sort_seq for e in fresh))
        if timing_callback:
            timings = {"model_seconds": round(model_seconds, 3), "send_seconds": round(send_seconds, 3), "processing_seconds": round(time.perf_counter() - started, 3)}
            created = [getattr(e, "created_at", 0) for e in incoming if getattr(e, "created_at", 0)]
            if created:
                timings["incoming_to_finished_seconds"] = round(max(0.0, time.time() - min(created)), 3)
            try:
                timing_callback(timings)
            except Exception:
                pass  # Non-essential timing output must not alter send confirmation.
        return bool(intent)
    except Exception as exc:
        # Never log the API response, key or parent message.
        from .wechat import WeChatError
        from .consultation import ExtractionError
        detail = ("：" + str(exc)) if isinstance(exc, (WeChatError, ExtractionError)) else ""
        store.pause("处理失败（%s）%s，请人工检查；没有自动重发。" % (type(exc).__name__, detail))
        raise PausedError(store.paused_reason()) from None
