"""用临时 SQLite 和假 UIA 验证边界；不访问真实微信、密钥或进程。"""
from contextlib import closing
import hashlib
from pathlib import Path
import re
import sqlite3
from types import SimpleNamespace
import tempfile
import unittest
from unittest.mock import patch
import builtins

from tutoring_demo.wechat import (ConfigurationError, LiveAccount, SendUnconfirmedError,
                                 WeChatAdapter, WeChatError, inventory_accounts,
                                 prove_live_account, prove_pinned_identity,
                                 _find_exact_chat_input, _new_bound_uia)


ROOT = Path(__file__).resolve().parents[1]
SELF = "wxid_teacher"
FRIEND = "wxid_parent"
NAME = "补习demo测试家长"
TABLE = "Msg_" + hashlib.md5(FRIEND.encode()).hexdigest()


class FakeDB:
    def __init__(self, **kwargs):
        self.kwargs = kwargs
        self.account = kwargs["account"]
        self.account_dir = str(Path(kwargs["db_dir"]) / self.account)
        self.wxid = re.sub(r"_\w{4}$", "", self.account)
        self._db_files = [(str(p.relative_to(Path(self.account_dir) / "db_storage")), str(p), 0)
                          for p in (Path(self.account_dir) / "db_storage").rglob("*.db")]

    def _message_dbs(self):
        return sorted(rel for rel, _, _ in self._db_files if "message_" in rel)

    def _open(self, rel):
        conn = sqlite3.connect(Path(self.account_dir) / "db_storage" / rel)
        conn.row_factory = sqlite3.Row
        return conn


class FakeClock:
    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now

    def sleep(self, amount):
        self.now += amount


class FakeControl:
    AutomationId = "MainView.chat_input_field"
    ClassName = "mmui::ChatInputField"
    ControlTypeName = "EditControl"

    def __init__(self, uia):
        self.uia = uia

    def SendKeys(self, keys, **kwargs):
        self.uia.keys.append(keys)
        if keys == "{Enter}":
            if self.uia.on_enter:
                self.uia.on_enter(self.uia.value)
            self.uia.value = ""


class FakeUIA:
    def __init__(self, on_enter=None):
        self.chat = NAME
        self.value = ""
        self.keys = []
        self.on_enter = on_enter
        self.after_paste = None
        self.control = FakeControl(self)

    def current_chat(self):
        return self.chat

    def _chat_input(self):
        return self.control

    def _read_edit_value(self, control):
        return self.value

    def _paste_into(self, control, text, clear=False):
        self.value = text
        if self.after_paste:
            self.after_paste(self)


class FakeGUI:
    pid = 123

    def __init__(self, uia, **kwargs):
        self.uia = uia
        self.main_hwnd = kwargs.get("hwnd", 321)

    def _get_uia(self):
        return self.uia


class AdapterTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix=".test_wechat_", dir=ROOT)
        self.base = Path(self.tmp.name)
        self.account = "wxid_teacher_abcd"
        self.storage = self.base / "source" / self.account / "db_storage"
        (self.storage / "contact").mkdir(parents=True)
        (self.storage / "message").mkdir()
        self.contact_file = self.storage / "contact" / "contact.db"
        with closing(sqlite3.connect(self.contact_file)) as conn, conn:
            conn.execute("CREATE TABLE contact(username TEXT, nick_name TEXT, remark TEXT)")
            conn.executemany("INSERT INTO contact VALUES(?,?,?)",
                             [(SELF, "测试老师", ""), (FRIEND, "家长", NAME),
                              ("other@chatroom", "群聊", "")])
        self.shards = []
        self.add_shard([(2, FRIEND), (12, SELF)])
        self.cfg = SimpleNamespace(db_dir=self.base / "source", account=self.account,
                                   workdir=self.base / "work", keys_file=self.base / "keys.json",
                                   test_friend=NAME, poll_limit=50, poll_overlap_seq=0)

    def tearDown(self):
        self.tmp.cleanup()

    def add_shard(self, mappings):
        path = self.storage / "message" / ("message_%d.db" % len(self.shards))
        with closing(sqlite3.connect(path)) as conn, conn:
            conn.execute("CREATE TABLE Name2Id(user_name TEXT)")
            conn.executemany("INSERT INTO Name2Id(rowid,user_name) VALUES(?,?)", mappings)
            conn.execute(f'CREATE TABLE "{TABLE}"(local_id INTEGER PRIMARY KEY, local_type INT, '
                         "real_sender_id INT, create_time INT, message_content TEXT, source TEXT, "
                         "status INT, compress_content BLOB, sort_seq INT)")
        self.shards.append(path)
        return len(self.shards) - 1

    def insert(self, local_id, seq, text="80", sender=2, shard=0, local_type=1,
               source="", status=0):
        with closing(sqlite3.connect(self.shards[shard])) as conn, conn:
            conn.execute(f'INSERT INTO "{TABLE}" VALUES(?,?,?,?,?,?,?,?,?)',
                         (local_id, local_type, sender, 1700000000 + seq, text,
                          source, status, None, seq))

    def adapter(self, **kwargs):
        return WeChatAdapter(self.cfg, db_factory=FakeDB, **kwargs).open()

    def send_adapter(self, uia):
        clock = FakeClock()
        return self.adapter(gui_factory=lambda **kw: FakeGUI(uia, **kw),
                            live_account_check=lambda directory, pinned: LiveAccount(123, 1.0),
                            window_finder=lambda pid: 321, clock=clock, sleep=clock.sleep)

    def test_constructor_explicit_account_and_no_extraction(self):
        adapter = self.adapter()
        self.assertTrue(adapter._db.kwargs["strict_account"])
        self.assertFalse(adapter._db.kwargs["allow_key_extraction"])
        self.assertEqual(adapter._db.kwargs["keys_file"], str(self.cfg.keys_file.resolve()))
        self.assertIsNone(adapter._gui)

    def test_inventory_only_direct_source_directories(self):
        self.assertEqual([x["account"] for x in inventory_accounts(self.cfg.db_dir)], [self.account])

    def test_repeated_text_and_same_seq_are_distinct(self):
        self.insert(1, 100)
        self.insert(2, 100)
        events = self.adapter().poll(100)
        self.assertEqual([e.direction for e in events], ["incoming", "incoming"])
        self.assertEqual(len({e.event_id for e in events}), 2)

    def test_boundary_arrival_after_baseline_is_returned(self):
        self.insert(1, 100)
        adapter = self.adapter()
        baseline = adapter.baseline()
        self.insert(2, 100)
        arrivals = [e for e in adapter.poll(100) if e.event_id not in {b.event_id for b in baseline}]
        self.assertEqual(len(arrivals), 1)

    def test_cross_shard_ids_and_sender_namespaces(self):
        second = self.add_shard([(2, SELF), (12, FRIEND)])
        self.insert(1, 100, sender=2)
        self.insert(1, 100, sender=2, shard=second)
        events = self.adapter().poll(100)
        self.assertEqual({e.direction for e in events}, {"incoming", "outgoing"})
        self.assertEqual(len({e.event_id for e in events}), 2)

    def test_unknown_numeric_sender_and_status_fail_closed(self):
        self.insert(1, 100, sender=99, status=2)
        self.assertEqual(self.adapter().poll(100)[0].direction, "unknown")

    def test_sender_id_two_without_map_is_unknown(self):
        with closing(sqlite3.connect(self.shards[0])) as conn, conn:
            conn.execute("DELETE FROM Name2Id")
        self.insert(1, 100, sender=2)
        self.assertEqual(self.adapter().poll(100)[0].direction, "unknown")

    def test_xml_evidence_and_contradiction(self):
        self.insert(1, 100, sender=99, source=f"<msgsource><fromusername>{SELF}</fromusername></msgsource>")
        self.insert(2, 101, sender=2, source=f"<msgsource><fromusername>{SELF}</fromusername></msgsource>")
        events = self.adapter().poll(100)
        self.assertEqual([e.direction for e in events], ["outgoing", "unknown"])

    def test_user_message_xml_is_not_sender_evidence(self):
        self.insert(1, 100, sender=99, text=f"<fromusername>{SELF}</fromusername>")
        self.assertEqual(self.adapter().poll(100)[0].direction, "unknown")

    def test_non_text_ignored_and_empty_unknown(self):
        self.insert(1, 100, text="[图片]", local_type=3)
        self.insert(2, 101, text="")
        self.assertFalse(any(e.is_text for e in self.adapter().poll(100)))

    def test_ambiguous_exact_friend_rejected(self):
        with closing(sqlite3.connect(self.contact_file)) as conn, conn:
            conn.execute("INSERT INTO contact VALUES(?,?,?)", ("wxid_other", "other", NAME))
        with self.assertRaises(ConfigurationError):
            self.adapter()

    def test_username_match_still_requires_unique_display(self):
        self.cfg.test_friend = FRIEND
        with closing(sqlite3.connect(self.contact_file)) as conn, conn:
            conn.execute("INSERT INTO contact VALUES(?,?,?)", ("wxid_other", "other", NAME))
        with self.assertRaises(ConfigurationError):
            self.adapter()

    def test_self_and_group_rejected(self):
        for requested in (SELF, "other@chatroom"):
            self.cfg.test_friend = requested
            with self.assertRaises(ConfigurationError):
                self.adapter()

    def test_fuzzy_friend_is_rejected(self):
        self.cfg.test_friend = "补习demo"
        with self.assertRaises(ConfigurationError):
            self.adapter()

    def test_poll_backlog_fails_instead_of_dropping(self):
        self.cfg.poll_limit = 2
        for i in range(3):
            self.insert(i + 1, 100)
        with self.assertRaises(WeChatError):
            self.adapter().poll(100)
        with self.assertRaises(WeChatError):
            self.adapter().baseline()

    def test_schema_failure_is_not_silent(self):
        with closing(sqlite3.connect(self.shards[0])) as conn, conn:
            conn.execute(f'DROP TABLE "{TABLE}"')
            conn.execute(f'CREATE TABLE "{TABLE}"(local_id INT)')
        with self.assertRaises(WeChatError):
            self.adapter().baseline()

    def test_db_account_change_stops(self):
        adapter = self.adapter()
        adapter._db.account = "different"
        with self.assertRaises(ConfigurationError):
            adapter.poll(0)

    def test_send_confirmed_only_new_outgoing_exact_event(self):
        self.insert(1, 100, text="请问孩子几年级？", sender=12)
        uia = FakeUIA(lambda text: self.insert(2, 100, text=text, sender=12))
        adapter = self.send_adapter(uia)
        event_id = adapter.send("请问孩子几年级？")
        self.assertIn(event_id, {e.event_id for e in adapter.poll(100)})
        self.assertEqual(uia.keys, ["{Enter}"])

    def test_old_matching_event_does_not_confirm_and_no_resend(self):
        self.insert(1, 100, text="请问孩子几年级？", sender=12)
        uia = FakeUIA()
        adapter = self.send_adapter(uia)
        with self.assertRaises(SendUnconfirmedError):
            adapter.send("请问孩子几年级？")
        with self.assertRaises(SendUnconfirmedError):
            adapter.send("请问孩子几年级？")
        self.assertEqual(uia.keys, ["{Enter}"])

    def test_incoming_same_text_cannot_confirm(self):
        uia = FakeUIA(lambda text: self.insert(1, 100, text=text, sender=2))
        adapter = self.send_adapter(uia)
        with self.assertRaises(SendUnconfirmedError):
            adapter.send("请问孩子几年级？")
        self.assertEqual(uia.keys, ["{Enter}"])

    def test_wrong_chat_no_action(self):
        uia = FakeUIA()
        uia.chat = NAME + "另一个"
        with self.assertRaises(ConfigurationError):
            self.send_adapter(uia).send("请问孩子几年级？")
        self.assertEqual(uia.keys, [])
        self.assertEqual(uia.value, "")

    def test_chat_change_after_paste_no_enter(self):
        uia = FakeUIA()
        uia.after_paste = lambda ui: setattr(ui, "chat", "其他人")
        with self.assertRaises(ConfigurationError):
            self.send_adapter(uia).send("请问孩子几年级？")
        self.assertEqual(uia.keys, [])

    def test_exact_content_readback_required(self):
        uia = FakeUIA()
        uia.after_paste = lambda ui: setattr(ui, "value", "请问孩子几 年级？")
        with self.assertRaises(ConfigurationError):
            self.send_adapter(uia).send("请问孩子几年级？")
        self.assertEqual(uia.keys, [])

    def test_existing_or_unreadable_draft_stops(self):
        for value in ("老师的草稿", None):
            uia = FakeUIA()
            uia.value = value
            with self.assertRaises(ConfigurationError):
                self.send_adapter(uia).send("请问孩子几年级？")
            self.assertEqual(uia.keys, [])

    def test_structural_fallback_edit_cannot_send(self):
        uia = FakeUIA()
        uia.control.AutomationId = "profile_remark"
        with self.assertRaises(ConfigurationError):
            self.send_adapter(uia).send("请问孩子几年级？")
        self.assertEqual(uia.keys, [])

    def test_foreground_change_stops_before_enter(self):
        uia = FakeUIA()

        def changed():
            raise ConfigurationError("前台窗口已改变")

        uia.assert_foreground = changed
        with self.assertRaises(ConfigurationError):
            self.send_adapter(uia).send("请问孩子几年级？")
        self.assertEqual(uia.keys, [])

    def test_real_send_path_uses_bound_window_and_uia_without_gui_import(self):
        uia = FakeUIA(lambda text: self.insert(1, 100, text=text, sender=12))
        uia.ensure_window = lambda: True
        clock = FakeClock()
        adapter = self.adapter(live_account_check=lambda directory, pinned: LiveAccount(123, 1.0),
                               window_finder=lambda pid: 321, clock=clock, sleep=clock.sleep)
        actual_import = builtins.__import__

        def guard_import(name, *args, **kwargs):
            if name == "wechatauto.guia":
                raise AssertionError("真实发送路径不得导入 GUI 混合驱动")
            return actual_import(name, *args, **kwargs)

        with patch("builtins.__import__", side_effect=guard_import), \
                patch("tutoring_demo.wechat._new_bound_uia", return_value=uia) as create_uia:
            confirmed = adapter.send("请问孩子几年级？")
        create_uia.assert_called_once_with(321, 123, adapter._assert_pinned,
                                          assert_write_live=adapter._assert_live)
        self.assertEqual(adapter._gui.pid, 123)
        self.assertEqual(adapter._gui.main_hwnd, 321)
        self.assertEqual(type(adapter._gui).__name__, "BoundWindow")
        self.assertTrue(confirmed)
        self.assertEqual(uia.keys, ["{Enter}"])

    def test_readiness_checks_without_paste_or_enter(self):
        uia = FakeUIA()

        def forbid_paste(*args, **kwargs):
            raise AssertionError("就绪检查不得粘贴文本")

        uia._paste_into = forbid_paste
        adapter = self.send_adapter(uia)
        self.assertTrue(adapter.assert_send_ready())
        self.assertEqual(uia.keys, [])
        self.assertEqual(uia.value, "")

    def test_readiness_rejects_wrong_chat_draft_and_unreadable_input(self):
        for chat, value in (("其他好友", ""), (NAME, "老师草稿"), (NAME, None)):
            with self.subTest(chat=chat, value=value):
                uia = FakeUIA()
                uia.chat, uia.value = chat, value
                with self.assertRaises(ConfigurationError):
                    self.send_adapter(uia).assert_send_ready()
                self.assertEqual(uia.keys, [])
                self.assertEqual(uia.value, value)

    def traced_adapter(self, uia, trace, *, fail_full_at=None, cheap_check=None, sleep=None):
        full_count = 0
        clock = FakeClock()

        def full(directory, pinned):
            nonlocal full_count
            full_count += 1
            trace.append("full")
            if full_count == fail_full_at:
                raise ConfigurationError("当前唯一账号无法证明")
            return LiveAccount(123, 1.0)

        def cheap(pinned, hwnd):
            trace.append("cheap")
            if cheap_check:
                return cheap_check(pinned, hwnd)
            return pinned

        adapter = self.adapter(gui_factory=lambda **kw: FakeGUI(uia, **kw),
                               live_account_check=full, pinned_identity_check=cheap,
                               window_finder=lambda pid: 321, clock=clock,
                               sleep=(lambda amount: sleep(clock, amount)) if sleep else clock.sleep)
        old_paste = uia._paste_into

        def paste(*args, **kwargs):
            trace.append("paste")
            return old_paste(*args, **kwargs)

        uia._paste_into = paste
        return adapter

    def test_fresh_proofs_at_paste_enter_receipt_and_not_empty_wait_rounds(self):
        trace = []
        entered_text = []
        uia = FakeUIA(lambda text: (trace.append("enter"), entered_text.append(text)))

        def wait(clock, amount):
            clock.sleep(amount)
            trace.append("wait")
            if clock.now >= 0.5 and entered_text:
                self.insert(1, 100, text=entered_text.pop(), sender=12)

        adapter = self.traced_adapter(uia, trace, sleep=wait)
        confirmed = adapter.send("请问孩子几年级？")
        self.assertTrue(confirmed)
        self.assertEqual(trace.count("full"), 4)  # 初始化 + 粘贴 + Enter + 接受新回执。
        paste_at, enter_at = trace.index("paste"), trace.index("enter")
        self.assertIn("full", trace[:paste_at])
        self.assertEqual([x for x in trace[paste_at + 1:enter_at] if x == "full"], ["full"])
        self.assertEqual([x for x in trace[enter_at + 1:] if x == "full"], ["full"])
        self.assertEqual(trace[-1], "full")
        self.assertEqual(trace.count("wait"), 2)
        self.assertEqual(uia.keys, ["{Enter}"])

    def test_warm_send_keeps_only_three_fresh_write_receipt_proofs(self):
        trace = []
        counter = 0

        def enter(text):
            nonlocal counter
            counter += 1
            trace.append("enter")
            self.insert(counter, 100 + counter, text=text, sender=12)

        uia = FakeUIA(enter)
        adapter = self.traced_adapter(uia, trace)
        self.assertTrue(adapter.assert_send_ready())
        self.assertEqual(trace.count("full"), 1)
        for message in ("请问孩子几年级？", "希望补习哪科？"):
            previous = trace.count("full")
            self.assertTrue(adapter.send(message))
            self.assertEqual(trace.count("full") - previous, 3)
        self.assertEqual(uia.keys, ["{Enter}", "{Enter}"])

    def test_unique_account_failure_at_each_write_or_receipt_boundary(self):
        for failure_at in (2, 3, 4):
            with self.subTest(boundary=failure_at):
                trace = []
                local_id = 10 + failure_at
                uia = FakeUIA(lambda text: (trace.append("enter"), self.insert(local_id, 100 + local_id, text=text, sender=12)))
                adapter = self.traced_adapter(uia, trace, fail_full_at=failure_at)
                error = SendUnconfirmedError if failure_at == 4 else ConfigurationError
                with self.assertRaises(error):
                    adapter.send("请问孩子几年级？")
                self.assertEqual(uia.keys, ["{Enter}"] if failure_at == 4 else [])
                if failure_at == 2:
                    self.assertNotIn("paste", trace)
                if failure_at == 4:
                    with self.assertRaises(SendUnconfirmedError):
                        adapter.send("请问孩子几年级？")
                    self.assertEqual(uia.keys, ["{Enter}"])

    def test_pinned_identity_change_after_paste_stops_before_enter(self):
        trace = []
        changed = False

        def cheap(pinned, hwnd):
            if changed:
                return LiveAccount(123, 2.0)
            return pinned

        def switch(ui):
            nonlocal changed
            changed = True

        uia = FakeUIA()
        uia.after_paste = switch
        adapter = self.traced_adapter(uia, trace, cheap_check=cheap)
        with self.assertRaises(ConfigurationError):
            adapter.send("请问孩子几年级？")
        self.assertEqual(uia.keys, [])
        self.assertEqual(trace.count("full"), 2)

    def test_pre_enter_full_scan_cannot_hide_chat_change(self):
        trace = []
        uia = FakeUIA()
        adapter = self.traced_adapter(uia, trace)
        old_full = adapter._live_check
        count = 0

        def full(directory, pinned):
            nonlocal count
            count += 1
            result = old_full(directory, pinned)
            if count == 3:
                uia.chat = "扫描期间切换的其他好友"
            return result

        adapter._live_check = full
        with self.assertRaises(ConfigurationError):
            adapter.send("请问孩子几年级？")
        self.assertEqual(uia.keys, [])

    def test_pre_paste_full_scan_cannot_hide_chat_or_draft_change(self):
        for changed_field, changed_value in (("chat", "扫描期间切换的其他好友"), ("value", "老师刚写的草稿")):
            with self.subTest(changed_field=changed_field):
                trace = []
                uia = FakeUIA()
                adapter = self.traced_adapter(uia, trace)
                old_full = adapter._live_check
                count = 0

                def full(directory, pinned):
                    nonlocal count
                    count += 1
                    result = old_full(directory, pinned)
                    if count == 2:
                        setattr(uia, changed_field, changed_value)
                    return result

                adapter._live_check = full
                with self.assertRaises(ConfigurationError):
                    adapter.send("请问孩子几年级？")
                self.assertNotIn("paste", trace)
                self.assertEqual(uia.keys, [])

    def test_empty_receipt_round_pinned_change_pauses_without_another_enter(self):
        trace = []
        entered = False

        def enter(text):
            nonlocal entered
            entered = True
            trace.append("enter")

        def cheap(pinned, hwnd):
            if entered:
                raise ConfigurationError("进程已经重启")
            return pinned

        uia = FakeUIA(enter)
        adapter = self.traced_adapter(uia, trace, cheap_check=cheap)
        with self.assertRaises(SendUnconfirmedError):
            adapter.send("请问孩子几年级？")
        with self.assertRaises(SendUnconfirmedError):
            adapter.send("请问孩子几年级？")
        self.assertEqual(uia.keys, ["{Enter}"])
        self.assertEqual(trace.count("full"), 3)


class ProcessProofTests(unittest.TestCase):
    def proc(self, pid=123, paths=(), created=1.0):
        return SimpleNamespace(pid=pid, info={"name": "Weixin.exe"},
                               open_files=lambda: [SimpleNamespace(path=p) for p in paths],
                               create_time=lambda: created)

    def test_exact_open_database_path_proves_unique_account(self):
        account_dir = Path(r"D:\source\wxid_teacher_abcd")
        proc = self.proc(paths=[r"D:\source\wxid_teacher_abcd\db_storage\message\message_0.db"])
        self.assertEqual(prove_live_account(account_dir, process_iter=lambda attrs: [proc]), LiveAccount(123, 1.0))

    def test_no_open_files_is_not_proof(self):
        with self.assertRaises(ConfigurationError):
            prove_live_account(Path(r"D:\source\wxid_teacher_abcd"),
                               process_iter=lambda attrs: [self.proc()])

    def test_other_account_is_rejected(self):
        paths = [r"D:\source\wxid_teacher_abcd\db_storage\message\message_0.db",
                 r"D:\source\wxid_other_abcd\db_storage\message\message_0.db"]
        with self.assertRaises(ConfigurationError):
            prove_live_account(Path(r"D:\source\wxid_teacher_abcd"),
                               process_iter=lambda attrs: [self.proc(paths=paths)])

    def test_restart_and_duplicate_process_fail_closed(self):
        paths = [r"D:\source\wxid_teacher_abcd\db_storage\message\message_0.db"]
        with self.assertRaises(ConfigurationError):
            prove_live_account(Path(r"D:\source\wxid_teacher_abcd"), LiveAccount(123, 0.5),
                               process_iter=lambda attrs: [self.proc(paths=paths)])
        with self.assertRaises(ConfigurationError):
            prove_live_account(Path(r"D:\source\wxid_teacher_abcd"),
                               process_iter=lambda attrs: [self.proc(paths=paths), self.proc(pid=124, paths=paths)])

    def test_cheap_pinned_check_does_not_enumerate_open_files(self):
        proc = SimpleNamespace(is_running=lambda: True, create_time=lambda: 1.0,
                               open_files=lambda: self.fail("廉价检查不得扫描文件句柄"))
        pinned = LiveAccount(123, 1.0)
        self.assertEqual(prove_pinned_identity(pinned, 321, process_factory=lambda pid: proc,
                                              window_pid=lambda hwnd: 123), pinned)

    def test_cheap_pinned_check_rejects_exit_restart_and_window_reassignment(self):
        for running, creation, window_owner in ((False, 1.0, 123), (True, 2.0, 123), (True, 1.0, 456)):
            with self.subTest(running=running, creation=creation, window_owner=window_owner):
                proc = SimpleNamespace(is_running=lambda: running, create_time=lambda: creation)
                with self.assertRaises(ConfigurationError):
                    prove_pinned_identity(LiveAccount(123, 1.0), 321,
                                          process_factory=lambda pid: proc,
                                          window_pid=lambda hwnd: window_owner)


class BoundUIATests(unittest.TestCase):
    def node(self, control_type="PaneControl", class_name="mmui::Container", aid="", children=()):
        return SimpleNamespace(ControlTypeName=control_type, ClassName=class_name,
                               AutomationId=aid, GetChildren=lambda: list(children),
                               Exists=lambda *args: self.fail("精确查询不得进入 Exists 的1秒等待"))

    def test_exact_scoped_input_lookup_has_no_exists_wait_or_search_fallback(self):
        search = self.node("EditControl", "mmui::XValidatorTextEdit", "search")
        input_box = self.node("EditControl", "mmui::ChatInputField", "MainView.chat_input_field")
        root = self.node(children=[search, input_box])
        self.assertIs(_find_exact_chat_input(root), input_box)
        self.assertIsNone(_find_exact_chat_input(self.node(children=[search])))

    def test_input_lookup_rejects_similar_aid_or_wrong_class(self):
        for class_name, aid in (("mmui::ChatInputField", "other_chat_input_field"),
                                ("mmui::OtherEdit", "chat_input_field")):
            with self.subTest(class_name=class_name, aid=aid):
                wrong = self.node("EditControl", class_name, aid)
                self.assertIsNone(_find_exact_chat_input(self.node(children=[wrong])))

    def test_bound_input_lookup_does_not_cache_across_chat_controls(self):
        first = self.node("EditControl", "mmui::ChatInputField", "MainView.chat_input_field")
        second = self.node("EditControl", "mmui::ChatInputField", "MainView.chat_input_field")
        children = [first]
        root = SimpleNamespace(ControlTypeName="PaneControl", GetChildren=lambda: list(children))

        class Engine:
            def __init__(self, **kwargs):
                self._win = root

            def _pid_from_hwnd(self, hwnd):
                return 123

        uia = _new_bound_uia(321, 123, lambda: None, engine_class=Engine)
        self.assertIs(uia._chat_input(), first)
        children[:] = [second]
        self.assertIs(uia._chat_input(), second)

    def test_gate_activation_stays_in_bound_window_without_login_or_wake(self):
        actions = []

        class Engine:
            def __init__(self, **kwargs):
                self._win = None
                self.materialized = False

            def _pid_from_hwnd(self, hwnd):
                return 123

            def _find_main(self):
                return object() if self.materialized else None

            def _hot_activate_accessibility(self, hwnd):
                actions.append(("gate", hwnd))
                self.materialized = True
                return True

            def ensure_materialized(self, **kwargs):
                return self._wake_accessibility()

            def _activate(self, win):
                actions.append(("activate",))

        uia = _new_bound_uia(321, 123, lambda: actions.append(("prove",)), engine_class=Engine)
        self.assertTrue(uia.ensure_window())
        self.assertEqual([a for a in actions if a[0] == "gate"], [("gate", 321)])
        self.assertEqual(uia._wechat_hwnds(), [321])
        self.assertFalse(uia._auto_login())
        with self.assertRaises(ConfigurationError):
            uia.wake()

    def test_window_pid_change_rejected_before_gate(self):
        class Engine:
            def __init__(self, **kwargs):
                pass

            def _pid_from_hwnd(self, hwnd):
                return 999

        uia = _new_bound_uia(321, 123, lambda: None, engine_class=Engine)
        with self.assertRaises(ConfigurationError):
            uia.ensure_window()

    def test_gate_rechecks_window_pid_after_full_account_scan(self):
        owner = 123
        gates = []

        class Engine:
            def __init__(self, **kwargs):
                pass

            def _pid_from_hwnd(self, hwnd):
                return owner

            def _hot_activate_accessibility(self, hwnd):
                gates.append(hwnd)
                return True

        def full():
            nonlocal owner
            owner = 456

        uia = _new_bound_uia(321, 123, lambda: None, engine_class=Engine, assert_write_live=full)
        with self.assertRaises(ConfigurationError):
            uia._wake_accessibility()
        self.assertEqual(gates, [])


if __name__ == "__main__":
    unittest.main()
