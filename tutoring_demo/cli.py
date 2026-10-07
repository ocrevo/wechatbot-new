"""Explicit stages: check, keys bootstrap, receive probe, model check, run."""
import argparse
from contextlib import contextmanager
from hashlib import sha256
import json
import os
from pathlib import Path
import sys
import time

from .config import ConfigError, ROOT, RUNTIME, configure_runtime, load_config, load_api_key


def emit(status, **fields):
    print(json.dumps({"status": status, **fields}, ensure_ascii=False), flush=True)


@contextmanager
def process_lock():
    """Only one receiving/sending process may hold the runtime at a time."""
    import msvcrt
    path = RUNTIME / "demo.lock"
    with path.open("a+b") as handle:
        if path.stat().st_size == 0:
            handle.write(b"0")
            handle.flush()
        handle.seek(0)
        try:
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        except OSError:
            raise ConfigError("已有 Demo 进程运行，不能重复启动。") from None
        try:
            yield
        finally:
            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)


def _report_path():
    return RUNTIME / "stages.json"


def record_stage(name, binding, **fields):
    path = _report_path()
    report = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    report[name] = {"binding": binding, "passed": True, "time": time.time(), **fields}
    temp = path.with_suffix(".tmp")
    temp.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(temp, path)


def target_fingerprint(username):
    return sha256(username.encode("utf-8")).hexdigest()


def require_stages(binding, username=None):
    path = _report_path()
    report = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    for stage in ("receive", "model"):
        data = report.get(stage, {})
        if data.get("passed") is not True or data.get("binding") != binding:
            raise ConfigError("请先通过当前账号/好友的 probe 和 model-check，再启动自动回复。")
        if stage == "receive" and username is not None and data.get("target") != target_fingerprint(username):
            raise ConfigError("备注对应的好友身份已改变，需要重新校准；旧咨询不能转给新好友。")


def adapter_for(config, allow_key_extraction=False):
    # Imported after config validation and runtime setup, never by bot import.
    from .wechat import WeChatAdapter
    adapter = WeChatAdapter(config)
    adapter.open(allow_key_extraction=allow_key_extraction)
    return adapter


def probe(config, seconds):
    adapter = adapter_for(config)
    baseline = adapter.baseline()
    seen = {e.event_id for e in baseline}
    watermark = max((e.sort_seq for e in baseline), default=0)
    emit("probe_ready", friend=config.test_friend, seconds=seconds, sends_enabled=False)
    counts = {"incoming": 0, "outgoing": 0, "unknown": 0}
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        adapter.assert_account()
        events = adapter.poll(watermark)
        for event in events:
            if event.event_id in seen:
                continue
            seen.add(event.event_id)
            counts[event.direction] += 1
            emit("received", direction=event.direction, is_text=event.is_text)
        if events:
            watermark = max(watermark, max(e.sort_seq for e in events))
        if counts["incoming"] and counts["outgoing"] and not counts["unknown"]:
            break
        time.sleep(1)
    passed = counts["incoming"] > 0 and counts["outgoing"] > 0 and counts["unknown"] == 0
    emit("probe_result", passed=passed, **counts)
    if passed:
        record_stage("receive", config.binding, target=target_fingerprint(adapter.target_username), **counts)
    return 0 if passed else 2


def model_check(config):
    from .consultation import ConsultationState, apply_patch
    from .deepseek import DeepSeekExtractor, choose_reply
    extractor = DeepSeekExtractor(load_api_key())
    state = ConsultationState()
    question = ""
    for message in ("你好，想了解补习", "初二，想补数学和英语", "数学80分", "英语也是80分"):
        before = state
        analysis = extractor.analyze_turn(state, question, message)
        state, required = apply_patch(state, analysis.patch)
        reply = choose_reply(analysis, before, state, required)
        question = reply or question
        emit("fictional_model_turn", reply=reply)
    if state.grade != "初二" or state.subjects != ["数学", "英语"] or not state.completed:
        raise ConfigError("模型没有正确提取虚构咨询字段，禁止开始真实收发。")
    if any("80" not in (state.scores.get(s) or "") for s in state.subjects):
        raise ConfigError("模型没有正确映射各科成绩。")
    record_stage("model", config.binding, model="deepseek-flash", mode="single_call")
    emit("model_passed", real_parent_data_used=False)
    return 0


def run(config, seconds, once):
    from .deepseek import DeepSeekExtractor, choose_reply
    from .runner import process_batch, PausedError
    from .storage import Store
    require_stages(config.binding)
    # Fail early on missing key, before DB or GUI initialization.
    extractor = DeepSeekExtractor(load_api_key())
    state_path = RUNTIME / "consultation.sqlite3"
    adapter = adapter_for(config)
    require_stages(config.binding, adapter.target_username)
    # Persist the resolved username, so assigning the same remark to another
    # person cannot carry state or pending messages across a restart.
    with Store(state_path, config.binding + ":" + target_fingerprint(adapter.target_username)) as store:
        if store.pending():
            store.pause("上次发送未确认，请人工核对；不会自动补发。")
        if store.paused_reason():
            raise PausedError(store.paused_reason())
        # Validate the computer conversation before calling the model or making
        # a pending send, so a blank chat pane cannot poison a fresh session.
        adapter.assert_send_ready()
        if not store.is_initialized():
            store.initialize(adapter.baseline())
        emit("run_ready", friend=config.test_friend, once=once)
        deadline = time.monotonic() + seconds if seconds else float("inf")
        while time.monotonic() < deadline:
            adapter.assert_account()
            events = adapter.poll(store.watermark())
            if any(e.direction == "incoming" and e.is_text and not store.seen(e.event_id) for e in events):
                # Merge consecutive parent messages before generating one question.
                time.sleep(0.3)
                events = adapter.poll(store.watermark())
            sent = process_batch(store, adapter, extractor, events, turn_analyzer=extractor.analyze_turn, reply_selector=choose_reply, timing_callback=lambda timings: emit("turn_timing", **timings))
            if sent:
                emit("reply_confirmed")
                if once:
                    record_stage("single_send", config.binding)
                    return 0
            time.sleep(0.25)
    return 0


def main(argv=None):
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    parser = argparse.ArgumentParser(description="个人微信：单好友补习咨询 Demo")
    sub = parser.add_subparsers(dest="command")
    sub.add_parser("check", help="检查本地配置和依赖，不连接微信")
    accounts = sub.add_parser("accounts", help="仅列出数据库父目录下的账号文件夹")
    accounts.add_argument("db_dir")
    sub.add_parser("bootstrap-keys", help="显式提取指定账号的数据库密钥，不发送")
    receive = sub.add_parser("probe", help="只接收；期间请人工一收一发校准")
    receive.add_argument("--seconds", type=int, default=60)
    sub.add_parser("model-check", help="只用虚构消息测试真实 DeepSeek，不连接微信")
    sub.add_parser("resume", help="人工核对后清除暂停；有未确认发送时拒绝，不发送")
    live = sub.add_parser("run", help="通过前置测试后，启动指定好友自动咨询")
    live.add_argument("--once", action="store_true", help="第一条自动回复确认后退出")
    live.add_argument("--seconds", type=int, default=0, help="0 表示持续运行，Ctrl+C 停止")
    args = parser.parse_args(argv)
    command = args.command or "check"
    try:
        configure_runtime()
        if command == "accounts":
            from .wechat import inventory_accounts
            emit("accounts", accounts=inventory_accounts(Path(args.db_dir)))
            return 0
        config = load_config()
        if command == "check":
            import importlib.metadata
            import importlib.util
            for package, expected in (("openai", "1.84.0"), ("wechatauto-replica", "1.2.5.1")):
                if importlib.metadata.version(package) != expected:
                    raise ConfigError("依赖版本与固定版本不一致，请安装 requirements-demo.txt。")
            spec = importlib.util.find_spec("wechatauto")
            expected_sdk = ROOT / "vendor" / "wechatauto-replica" / "wechatauto" / "__init__.py"
            if spec is None or Path(spec.origin).resolve() != expected_sdk.resolve():
                raise ConfigError("SDK 必须来自本仓库 vendor 的修补版本，请安装 requirements-demo.txt。")
            load_api_key()
            if any(not (ROOT / "prompts" / name).is_file() for name in ("teacher_demo.md", "teacher_reply.md", "teacher_turn.md")):
                raise ConfigError("咨询提示词缺失。")
            emit("config_ready", friend=config.test_friend, wechat_connected=False)
            return 0
        if hasattr(args, "seconds") and args.seconds < 0:
            raise ConfigError("seconds 不能为负数。")
        with process_lock():
            if command == "bootstrap-keys":
                adapter_for(config, allow_key_extraction=True)
                emit("keys_ready", sends_enabled=False)
                return 0
            if command == "probe":
                return probe(config, args.seconds)
            if command == "model-check":
                return model_check(config)
            if command == "resume":
                from .storage import Store
                adapter = adapter_for(config)
                binding = config.binding + ":" + target_fingerprint(adapter.target_username)
                with Store(RUNTIME / "consultation.sqlite3", binding) as store:
                    if store.pending():
                        raise ConfigError("存在未确认发送，禁止解除暂停；请人工核对真实聊天。")
                    store.resume_after_review()
                emit("pause_cleared", sends_enabled=False)
                return 0
            return run(config, args.seconds, args.once)
    except KeyboardInterrupt:
        emit("stopped")
        return 130
    except Exception as exc:
        # Only known local configuration/pause errors have user-facing details.
        from .runner import PausedError
        from .wechat import WeChatError
        safe_message = str(exc) if isinstance(exc, (ConfigError, PausedError, WeChatError)) else "阶段失败；请查看本地配置和验证记录。"
        emit("failed", error_type=type(exc).__name__, message=safe_message)
        return 2
