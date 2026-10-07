"""单账号、单私聊的受控数据库适配器；导入本模块不会操作微信。"""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import time
from typing import Callable, Literal
import xml.etree.ElementTree as ET


class WeChatError(RuntimeError):
    pass


class ConfigurationError(WeChatError):
    pass


class SendUnconfirmedError(WeChatError):
    """可能已发送，禁止自动重发；须人工核对聊天。"""


@dataclass(frozen=True)
class MessageEvent:
    event_id: str
    sort_seq: int
    direction: Literal["incoming", "outgoing", "unknown"]
    text: str
    is_text: bool
    created_at: int


@dataclass(frozen=True)
class LiveAccount:
    pid: int
    create_time: float


@dataclass(frozen=True)
class BoundWindow:
    """只记录已确认窗口；不初始化 OCR、布局校准或 GUI 混合驱动。"""
    pid: int
    main_hwnd: int


_ROOT = Path(__file__).resolve().parents[1]
_SYSTEM_USERS = {"filehelper", "weixin", "fmessage", "medianote", "newsapp", "floatbottle",
                 "qqmail", "qmessage", "tmessage", "weibo", "notification_messages"}


def inventory_accounts(db_dir: Path | str) -> list[dict]:
    """只列目录，不打开数据库、不猜测当前登录账号。"""
    root = Path(db_dir).expanduser().resolve()
    if not root.is_dir():
        raise ConfigurationError("数据库父目录不存在。")
    return [{"account": p.name, "path": str(p), "db_storage": str(p / "db_storage")}
            for p in sorted(root.iterdir(), key=lambda p: p.name)
            if p.is_dir() and (p / "db_storage").is_dir()]


def _path_key(path: Path | str) -> str:
    return os.path.normcase(os.path.abspath(str(path)))


def _account_from_open_file(path: str) -> str | None:
    # Windows 分隔符也可在离线测试中解析；不读取文件正文。
    normalized = path.replace("\\", "/")
    parts = normalized.split("/")
    for i, component in enumerate(parts):
        if component.casefold() == "db_storage" and i > 0:
            return "/".join(parts[:i])
    return None


def prove_live_account(account_dir: Path, pinned: LiveAccount | None = None,
                       *, process_iter=None) -> LiveAccount:
    """以进程实际打开的数据库句柄证明账号；没有正向证据就停止发送。"""
    if process_iter is None:
        import psutil
        process_iter = psutil.process_iter
    expected = _path_key(account_dir)
    matches = []
    opaque = False
    for proc in process_iter(["pid", "name"]):
        try:
            name = proc.info.get("name") or proc.name()
        except Exception:
            continue
        if str(name).casefold() not in {"weixin.exe", "wechat.exe"}:
            continue
        try:
            accounts = {_path_key(p) for f in proc.open_files()
                        if (p := _account_from_open_file(f.path)) is not None}
            stamp = LiveAccount(int(proc.pid), float(proc.create_time()))
        except Exception:
            opaque = True
            continue
        if accounts - {expected}:
            raise ConfigurationError("检测到微信进程打开了其他账号数据库，暂停发送。")
        if expected in accounts:
            matches.append(stamp)
    if opaque or len(matches) != 1:
        raise ConfigurationError("不能用微信进程的已打开数据库句柄确认唯一登录账号，暂停发送。")
    if pinned is not None and matches[0] != pinned:
        raise ConfigurationError("微信进程已重启或切换，暂停发送并重新建立基线。")
    return matches[0]


def prove_pinned_identity(pinned: LiveAccount, hwnd: int | None = None,
                          *, process_factory=None, window_pid=None) -> LiveAccount:
    """只检查已绑定进程/窗口身份，不枚举句柄；不能替代写入边界的账号证明。"""
    if process_factory is None:
        import psutil
        process_factory = psutil.Process
    try:
        proc = process_factory(pinned.pid)
        if not proc.is_running() or float(proc.create_time()) != pinned.create_time:
            raise ConfigurationError("微信进程已重启或退出，暂停发送并重新建立基线。")
        if hwnd is not None:
            if window_pid is None:
                import ctypes
                from ctypes import wintypes
                u = ctypes.windll.user32
                u.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
                u.GetWindowThreadProcessId.restype = wintypes.DWORD
                value = wintypes.DWORD()
                u.GetWindowThreadProcessId(hwnd, ctypes.byref(value))
                owner = value.value
            else:
                owner = window_pid(hwnd)
            if int(owner) != pinned.pid:
                raise ConfigurationError("绑定微信窗口已失效或属于其他进程，暂停发送。")
    except ConfigurationError:
        raise
    except Exception as exc:
        raise ConfigurationError("无法核验已绑定微信进程/窗口，暂停发送。") from exc
    return pinned


def _find_exact_chat_input(win):
    """在绑定窗口内立即查精确输入锚点；不等待旧短 ID，不保存跨聊天控件。"""
    if win is None:
        return None
    stack = [(win, 0)]
    visited = 0
    while stack:
        control, depth = stack.pop()
        visited += 1
        if visited > 2000:
            raise ConfigurationError("绑定窗口控件树过大，不能安全定位唯一输入框。")
        if (str(getattr(control, "ControlTypeName", "") or "") == "EditControl"
                and str(getattr(control, "ClassName", "") or "") == "mmui::ChatInputField"
                and "chat_input_field" in str(getattr(control, "AutomationId", "") or "").split(".")):
            return control
        if depth < 40:
            # 控件不可读时传播异常，不能跳过后随意使用另一个编辑框。
            stack.extend((child, depth + 1) for child in reversed(control.GetChildren()))
    return None


def _window_for_pid(pid: int) -> int:
    """仅定位已确认进程的可见微信主窗，不接受其他进程的窗口。"""
    import ctypes
    from ctypes import wintypes
    u = ctypes.windll.user32
    u.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
    u.GetWindowThreadProcessId.restype = wintypes.DWORD
    u.IsWindowVisible.argtypes = [wintypes.HWND]
    u.IsWindowVisible.restype = wintypes.BOOL
    u.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
    u.GetClassNameW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
    candidates = []
    callback_type = ctypes.WINFUNCTYPE(ctypes.c_bool, wintypes.HWND, wintypes.LPARAM)

    def visit(hwnd, _):
        process_id = wintypes.DWORD()
        u.GetWindowThreadProcessId(hwnd, ctypes.byref(process_id))
        if process_id.value != pid or not u.IsWindowVisible(hwnd):
            return True
        title = ctypes.create_unicode_buffer(256)
        cls = ctypes.create_unicode_buffer(256)
        u.GetWindowTextW(hwnd, title, 256)
        u.GetClassNameW(hwnd, cls, 256)
        if title.value in {"微信", "WeChat", "Weixin"} and "QWindow" in cls.value:
            candidates.append(int(hwnd))
        return True

    callback = callback_type(visit)
    u.EnumWindows(callback, 0)
    if len(candidates) != 1:
        raise ConfigurationError("已确认进程没有唯一可见微信主窗口，请关闭弹窗并显示主窗口。")
    return candidates[0]


def _new_bound_uia(hwnd: int, pid: int, assert_live: Callable, *, engine_class=None,
                   assert_write_live: Callable | None = None):
    """收窄上游 UIA 的全局窗口搜索；不自动登录或启动其他微信实例。"""
    if engine_class is None:
        from wechatauto.uia_driver import WeChatUIA
        engine_class = WeChatUIA

    class BoundUIA(engine_class):
        def _wechat_hwnds(self, diag=None):
            assert_live()
            if self._pid_from_hwnd(hwnd) != pid:
                raise ConfigurationError("绑定窗口的微信进程已改变。")
            if diag is not None:
                diag.update(win32=True, matched=1, kept=1, dropped_no_dll=0)
            return [hwnd]

        def _wake_accessibility(self):
            # 上游会为全部微信窗口写 gate 并设置全局读屏标志；此处只操作绑定窗口。
            self._wechat_hwnds()
            # gate 也是写入边界，只在实际激活时重新做完整账号证明。
            (assert_write_live or assert_live)()
            self._wechat_hwnds()
            ok = self._hot_activate_accessibility(hwnd)
            if ok:
                self._win = None
            return ok

        def _chat_input(self, win=None):
            self._wechat_hwnds()
            return _find_exact_chat_input(win if win is not None else self._win)

        def assert_foreground(self):
            import ctypes
            from ctypes import wintypes
            u = ctypes.windll.user32
            u.GetForegroundWindow.restype = wintypes.HWND
            u.GetAncestor.argtypes = [wintypes.HWND, wintypes.UINT]
            u.GetAncestor.restype = wintypes.HWND
            foreground = int(u.GetForegroundWindow())
            root = int(u.GetAncestor(foreground, 2)) if foreground else 0
            if root != hwnd or self._pid_from_hwnd(foreground) != pid:
                raise ConfigurationError("前台窗口已改变，未按发送键，请人工检查草稿。")

        def ensure_window(self, wake=True, timeout=None):
            self._wechat_hwnds()
            win = self._find_main()
            if win is None and wake:
                if not self.ensure_materialized(timeout=min(float(timeout or 6.0), 6.0)):
                    return False
                win = self._find_main()
            if win is None:
                return False
            self._win = win
            self._activate(win)
            return True

        def _login_window(self):
            return None

        def _auto_login(self):
            return False

        def wake(self):
            raise ConfigurationError("受控 demo 不自动登录或拉起微信。")

    return BoundUIA(timeout=6.0)


class WeChatAdapter:
    def __init__(self, config, *, db_factory=None, gui_factory=None,
                 live_account_check: Callable = prove_live_account,
                 pinned_identity_check: Callable | None = None,
                 window_finder: Callable = _window_for_pid,
                 clock: Callable = time.monotonic, sleep: Callable = time.sleep):
        self.config = config
        self._db_factory = db_factory
        self._gui_factory = gui_factory
        self._live_check = live_account_check
        # 旧测试只注入完整 checker 时沿用它，避免访问真实进程。生产默认走廉价检查。
        self._pinned_check = pinned_identity_check or (
            prove_pinned_identity if live_account_check is prove_live_account
            else lambda pinned, hwnd: live_account_check(self.account_dir, pinned))
        self._window_finder = window_finder
        self._clock, self._sleep = clock, sleep
        self._db = None
        self._gui = None
        self._uia = None
        self._live = None
        self._send_uncertain = False
        self.target_username = ""
        self.target_display_name = ""
        self.self_username = ""
        self.account_dir = Path(config.db_dir).resolve() / config.account
        self._limit = max(1, min(5000, int(getattr(config, "poll_limit", 500))))
        self._overlap = max(0, int(getattr(config, "poll_overlap_seq", 0)))

    def open(self, allow_key_extraction: bool = False):
        if self._db is not None:
            raise ConfigurationError("适配器已打开；请创建新实例重新配置。")
        account = self.config.account
        if not isinstance(account, str) or account in {"", ".", ".."} or any(c in account for c in "/\\:"):
            raise ConfigurationError("必须填写完整且唯一的账号目录名。")
        if not (self.account_dir / "db_storage").is_dir():
            raise ConfigurationError("指定账号没有 db_storage 目录。")
        for destination in (Path(self.config.workdir), Path(self.config.keys_file)):
            if not destination.resolve().is_relative_to(_ROOT):
                raise ConfigurationError("密钥和解密缓存必须保存在项目目录内。")
        factory = self._db_factory
        if factory is None:
            # SDK 的 GUI 实例只在 send() 创建；这里仅构造明确绑定的数据库对象。
            from wechatauto.db import WeChatDB
            from wechatauto.param import WxParam
            WxParam.ENABLE_FILE_LOGGER = False
            factory = WeChatDB
        try:
            self._db = factory(db_dir=str(Path(self.config.db_dir).resolve()), account=account,
                               workdir=str(Path(self.config.workdir).resolve()),
                               keys_file=str(Path(self.config.keys_file).resolve()),
                               strict_account=True, allow_key_extraction=bool(allow_key_extraction))
            self.self_username = str(self._db.wxid)
            self.assert_account()
            self._bind_target()
        except WeChatError:
            self._db = None
            raise
        except Exception as exc:
            self._db = None
            raise WeChatError("无法打开指定账号数据库；检查缓存密钥和数据路径。") from exc
        return self

    def assert_account(self):
        if self._db is None:
            raise ConfigurationError("适配器未打开。")
        if (self._db.account != self.config.account
                or _path_key(self._db.account_dir) != _path_key(self.account_dir)
                or str(self._db.wxid) != self.self_username):
            raise ConfigurationError("数据库账号绑定发生变化，已停止。")

    def _contacts(self) -> list[dict]:
        found = {}
        for rel, path, _ in self._db._db_files:
            if Path(str(path).replace("\\", "/")).name != "contact.db":
                continue
            conn = self._db._open(rel)
            try:
                conn.row_factory = sqlite3.Row
                rows = conn.execute("SELECT username, nick_name, remark FROM contact").fetchall()
                for row in rows:
                    item = dict(row)
                    username = str(item["username"] or "")
                    if username in found and found[username] != item:
                        raise ConfigurationError("联系人分片资料不一致，无法唯一绑定好友。")
                    found[username] = item
            finally:
                conn.close()
        if not found:
            raise ConfigurationError("没有可用的联系人数据库。")
        return list(found.values())

    def _bind_target(self):
        contacts = self._contacts()
        requested = str(self.config.test_friend)
        matches = [c for c in contacts if requested in {str(c.get(k) or "")
                                                       for k in ("username", "nick_name", "remark")}]
        if len(matches) != 1:
            raise ConfigurationError("测试好友必须精确匹配唯一联系人；请使用唯一备注或 username。")
        target = matches[0]
        username = str(target["username"] or "")
        if (not username or username == self.self_username or "@" in username
                or username.startswith("gh_") or username in _SYSTEM_USERS):
            raise ConfigurationError("测试目标必须是单个个人好友，不能是自己、群聊或系统账号。")
        display = str(target.get("remark") or target.get("nick_name") or "")
        if not display or display != display.strip():
            raise ConfigurationError("好友需要不含首尾空白的唯一昵称或备注。")
        shown = [c for c in contacts
                 if str(c.get("remark") or c.get("nick_name") or "") == display]
        if len(shown) != 1:
            raise ConfigurationError("测试好友的显示名与其他联系人重复；请设置唯一备注。")
        if self.target_username and (username != self.target_username or display != self.target_display_name):
            raise ConfigurationError("测试好友身份或备注已改变，暂停发送。")
        self.target_username, self.target_display_name = username, display

    @staticmethod
    def _sender_index(conn) -> dict[int, str | None]:
        result = {}
        names = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        for table in ("Name2Id", "SenderName2Id"):
            if table not in names:
                continue
            columns = {r[1] for r in conn.execute(f'PRAGMA table_info("{table}")')}
            name_col = next((c for c in ("user_name", "username") if c in columns), None)
            if name_col is None:
                continue
            for sender_id, username in conn.execute(f'SELECT rowid, "{name_col}" FROM "{table}"'):
                username = str(username or "")
                if not username:
                    continue
                sid = int(sender_id)
                if sid in result and result[sid] != username:
                    result[sid] = None
                else:
                    result[sid] = username
        return result

    @staticmethod
    def _xml_senders(source) -> set[str]:
        if isinstance(source, bytes):
            try:
                source = source.decode("utf-8")
            except UnicodeDecodeError:
                return set()
        if not isinstance(source, str) or not source.lstrip().startswith("<") or len(source) > 100_000:
            return set()
        try:
            root = ET.fromstring(source)
        except ET.ParseError:
            return set()
        senders = set()
        for node in root.iter():
            if node.tag.rsplit("}", 1)[-1].casefold() in {"fromusername", "from_username"} and node.text:
                senders.add(node.text.strip())
            for name, value in node.attrib.items():
                if name.rsplit("}", 1)[-1].casefold() in {"fromusername", "from_username"} and value:
                    senders.add(value)
        return senders

    def _direction(self, row, sender_index) -> str:
        evidence = self._xml_senders(row["source"])
        sid = row["real_sender_id"]
        if sid is not None:
            mapped = sender_index.get(int(sid))
            if mapped:
                evidence.add(mapped)
            elif int(sid) in sender_index:
                return "unknown"
        # 数字 sender_id == 2 / status 数值不单独证明自己，必须有用户名证据。
        if evidence == {self.self_username}:
            return "outgoing"
        if evidence == {self.target_username}:
            return "incoming"
        return "unknown"

    def _content(self, row) -> tuple[str, bool]:
        is_text = int(row["local_type"] or 0) == 1
        value = row["message_content"]
        if not value and row["compress_content"]:
            value = row["compress_content"]
        if isinstance(value, bytes):
            try:
                value = value.decode("utf-8")
            except UnicodeDecodeError:
                decoder = getattr(self._db, "_friendly_content", None)
                value = decoder(value, "文本") if decoder and is_text else ""
                if not value or value == "[文本]":
                    is_text = False
        if not isinstance(value, str) or "\x00" in value:
            return "", False
        return value, bool(is_text and value)

    def _read(self, since_seq: int | None) -> list[MessageEvent]:
        self.assert_account()
        events = []
        ordering = {}
        table = "Msg_" + hashlib.md5(self.target_username.encode()).hexdigest()
        required = {"local_id", "local_type", "real_sender_id", "create_time", "message_content", "sort_seq"}
        optional = {"source", "status", "compress_content"}
        for rel in sorted(self._db._message_dbs()):
            conn = self._db._open(rel)
            try:
                conn.row_factory = sqlite3.Row
                if conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone() is None:
                    continue
                columns = {r[1] for r in conn.execute(f'PRAGMA table_info("{table}")')}
                if not required.issubset(columns):
                    raise WeChatError("消息分片结构不受支持，停止而非跳过该分片。")
                selected = [f'"{c}"' for c in sorted(required)]
                selected += [f'"{c}"' if c in columns else f'NULL AS "{c}"' for c in sorted(optional)]
                where = "" if since_seq is None else "WHERE sort_seq >= ?"
                params = () if since_seq is None else (max(0, int(since_seq) - self._overlap),)
                order = "DESC" if since_seq is None else "ASC"
                rows = conn.execute(f'SELECT {", ".join(selected)} FROM "{table}" {where} '
                                    f'ORDER BY sort_seq {order}, local_id ASC LIMIT ?',
                                    (*params, self._limit + 1)).fetchall()
                if since_seq is not None and len(rows) > self._limit:
                    raise WeChatError("目标聊天积压或同序号消息超过上限，须人工重新建立基线。")
                if since_seq is None and len(rows) > self._limit and rows[-1]["sort_seq"] == rows[0]["sort_seq"]:
                    raise WeChatError("最新序号包含过多消息，无法安全建立完整基线。")
                sender_index = self._sender_index(conn)
                for row in rows:
                    identity = [self.config.account, self.target_username, str(rel).replace("\\", "/"),
                                table, int(row["local_id"])]
                    event_id = hashlib.sha256(json.dumps(identity, ensure_ascii=False,
                                                          separators=(",", ":")).encode()).hexdigest()
                    text, is_text = self._content(row)
                    ordering[event_id] = (int(row["sort_seq"]), str(rel).replace("\\", "/"), int(row["local_id"]))
                    events.append(MessageEvent(event_id, int(row["sort_seq"]),
                                               self._direction(row, sender_index), text, is_text,
                                               int(row["create_time"] or 0)))
            except sqlite3.DatabaseError as exc:
                raise WeChatError("目标消息分片读取失败，已停止。") from exc
            finally:
                conn.close()
        events.sort(key=lambda e: ordering[e.event_id])
        if since_seq is not None and len(events) > self._limit:
            raise WeChatError("跨分片目标消息超过轮询上限，已停止。")
        if since_seq is None and len(events) > self._limit:
            if sum(e.sort_seq == events[-1].sort_seq for e in events) > self._limit:
                raise WeChatError("最新序号包含过多消息，无法安全建立完整基线。")
            events = events[-self._limit:]
        return events

    def baseline(self) -> list[MessageEvent]:
        return self._read(None)

    def poll(self, since_seq: int) -> list[MessageEvent]:
        # >= 保留序号边界，持久化 event_id 去重，不能按文本或 > 水位去重。
        return self._read(int(since_seq))

    def _assert_live(self):
        self.assert_account()
        checked = self._live_check(self.account_dir, self._live)
        if not isinstance(checked, LiveAccount):
            raise ConfigurationError("当前微信账号未得到正向证明。")
        if self._live is not None and checked != self._live:
            raise ConfigurationError("微信进程身份改变，暂停发送并重新建立基线。")
        self._live = checked
        if self._gui is not None and int(self._gui.pid) != self._live.pid:
            raise ConfigurationError("微信窗口与已确认账号不属于同一进程。")

    def _assert_pinned(self):
        self.assert_account()
        if self._live is None:
            self._assert_live()
            return
        raw_hwnd = getattr(self._gui, "main_hwnd", None) if self._gui is not None else None
        if self._gui is not None and raw_hwnd is None and self._gui_factory is None:
            raise ConfigurationError("绑定微信窗口身份缺失，暂停发送。")
        hwnd = int(raw_hwnd) if raw_hwnd is not None else None
        checked = self._pinned_check(self._live, hwnd)
        if checked is not None and checked != self._live:
            raise ConfigurationError("微信进程或窗口身份改变，暂停发送。")
        if self._gui is not None and int(self._gui.pid) != self._live.pid:
            raise ConfigurationError("微信窗口与已确认账号不属于同一进程。")

    def _current_uia(self):
        self._assert_pinned()
        if self._gui is None:
            hwnd = self._window_finder(self._live.pid)
            if self._gui_factory is None:
                self._gui = BoundWindow(pid=self._live.pid, main_hwnd=hwnd)
            else:
                self._gui = self._gui_factory(hwnd=hwnd, calibrate=False)
                self._gui._cached_db = self._db
        self._assert_pinned()
        if self._uia is None:
            if self._gui_factory is None:
                # GUI._get_uia 不尊重 main_hwnd，不能用于此处的账号隔离。
                self._uia = _new_bound_uia(int(self._gui.main_hwnd), self._live.pid, self._assert_pinned,
                                           assert_write_live=self._assert_live)
                if not self._uia.ensure_window():
                    self._uia = None
            else:
                self._uia = self._gui._get_uia()
        uia = self._uia
        if uia is None:
            raise ConfigurationError("微信没有可核验的 UIA 输入框，暂停发送。")
        if uia.current_chat() != self.target_display_name:
            raise ConfigurationError("请手动打开唯一测试好友会话；当前会话身份不匹配。")
        return uia

    @staticmethod
    def _checked_input(uia):
        control = uia._chat_input()
        if control is None:
            raise ConfigurationError("找不到测试好友输入框，未发送。")
        aid = str(getattr(control, "AutomationId", "") or "")
        if ("chat_input_field" not in aid.split(".")
                or str(getattr(control, "ClassName", "") or "") != "mmui::ChatInputField"
                or str(getattr(control, "ControlTypeName", "") or "") != "EditControl"):
            raise ConfigurationError("输入框没有已支持的精确 UIA 锚点，未发送。")
        return control

    def assert_send_ready(self) -> bool:
        """只核验发送条件；可物化绑定 UIA，但不粘贴文本、不按 Enter。"""
        self._bind_target()
        self._assert_live()
        uia = self._current_uia()
        control = self._checked_input(uia)
        if uia._read_edit_value(control) != "":
            raise ConfigurationError("输入框有草稿或无法回读，请先清空并人工核对。")
        if hasattr(uia, "assert_foreground"):
            uia.assert_foreground()
        return True

    def send(self, text: str) -> str:
        if self._send_uncertain:
            raise SendUnconfirmedError("上次发送未确认，须人工核对；本实例禁止再次发送。")
        if not isinstance(text, str) or not text.strip() or len(text) > 1500 or any(c in text for c in "\r\n\x00"):
            raise ConfigurationError("demo 只允许单行、非空、1500字以内的纯文本。")
        self._bind_target()
        uia = self._current_uia()
        before = self.baseline()
        known = {e.event_id for e in before}
        seq = max((e.sort_seq for e in before), default=0)
        control = self._checked_input(uia)
        # 禁止覆盖老师未发送的草稿；读不到也停止。
        if uia._read_edit_value(control) != "":
            raise ConfigurationError("输入框有草稿或无法回读，请先清空并人工核对。")
        if uia.current_chat() != self.target_display_name:
            raise ConfigurationError("输入前会话发生变化，未发送。")
        self._assert_live()
        self._assert_pinned()
        # 粘贴前的句柄扫描也可能耗时，重新核验目标和空草稿，不复用扫描前控件。
        if uia.current_chat() != self.target_display_name:
            raise ConfigurationError("账号核验期间会话发生变化，未粘贴。")
        control = self._checked_input(uia)
        if uia._read_edit_value(control) != "":
            raise ConfigurationError("账号核验期间输入框出现草稿或无法回读，未粘贴。")
        if hasattr(uia, "assert_foreground"):
            uia.assert_foreground()
        uia._paste_into(control, text, clear=False)
        if uia._read_edit_value(control) != text:
            raise ConfigurationError("粘贴内容无法精确回读；未按发送键，请人工检查草稿。")
        self._assert_pinned()
        self._bind_target()
        if uia.current_chat() != self.target_display_name:
            raise ConfigurationError("发送前会话发生变化；未按发送键，请人工检查草稿。")
        current = self._checked_input(uia)
        if uia._read_edit_value(current) != text:
            raise ConfigurationError("发送前输入框内容发生变化，未发送。")
        if hasattr(uia, "assert_foreground"):
            uia.assert_foreground()
        self._assert_live()
        # 完整句柄扫描可能耗时；之后仍廉价重读会话、内容及前台再执行唯一 Enter。
        if uia.current_chat() != self.target_display_name:
            raise ConfigurationError("账号核验期间会话发生变化，未发送。")
        current = self._checked_input(uia)
        if uia._read_edit_value(current) != text:
            raise ConfigurationError("账号核验期间输入框内容发生变化，未发送。")
        if hasattr(uia, "assert_foreground"):
            uia.assert_foreground()
        # Enter 调用一旦开始，其失败也可能已发送，不能回退或重试。
        self._send_uncertain = True
        try:
            current.SendKeys("{Enter}", waitTime=0.05)
        except Exception as exc:
            raise SendUnconfirmedError("发送键结果不明，禁止自动重发，请人工检查。") from exc
        deadline = self._clock() + 8.0
        while self._clock() < deadline:
            try:
                self._assert_pinned()
                candidates = [e for e in self.poll(seq) if e.event_id not in known
                              and e.direction == "outgoing" and e.is_text and e.text == text]
            except Exception as exc:
                raise SendUnconfirmedError("发送后无法继续核验账号或数据库，禁止自动重发。") from exc
            if len(candidates) == 1:
                try:
                    self._assert_live()
                except Exception as exc:
                    raise SendUnconfirmedError("匹配到回执但无法确认当前唯一账号，禁止自动重发。") from exc
                self._send_uncertain = False
                return candidates[0].event_id
            if len(candidates) > 1:
                raise SendUnconfirmedError("出现多个同文发送事件，无法唯一确认；禁止自动重发。")
            self._sleep(0.25)
        raise SendUnconfirmedError("8秒内未找到本账号在唯一测试好友会话中的新同文发送事件；请人工核对。")


def begin(config, *, allow_key_extraction: bool = False) -> WeChatAdapter:
    return WeChatAdapter(config).open(allow_key_extraction=allow_key_extraction)
