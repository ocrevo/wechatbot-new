"""Durable deduplication and an outbox that never retries uncertain sends."""
import json
from pathlib import Path
import sqlite3
import time
from uuid import uuid4


class StoreError(RuntimeError):
    pass


class Store:
    def __init__(self, path, binding):
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(str(path))
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA synchronous=FULL")
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY,value TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS seen(event_id TEXT PRIMARY KEY);
            CREATE TABLE IF NOT EXISTS outbox(id TEXT PRIMARY KEY,text TEXT NOT NULL,
                status TEXT NOT NULL,created REAL NOT NULL,confirmed_event_id TEXT);
        """)
        current = self._get("binding")
        if current is not None and current != binding:
            self.close()
            raise StoreError("记录绑定的账号或好友与当前配置不一致。")
        with self.db:
            self._set("binding", binding)

    def _get(self, key, default=None):
        row = self.db.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return row[0] if row else default

    def _set(self, key, value):
        self.db.execute("INSERT OR REPLACE INTO meta VALUES(?,?)", (key, str(value)))

    def close(self):
        self.db.close()

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()

    def load_state(self):
        value = self._get("state")
        return json.loads(value) if value else None

    def last_question(self):
        return self._get("last_question", "")

    def seen(self, event_id):
        return self.db.execute("SELECT 1 FROM seen WHERE event_id=?", (event_id,)).fetchone() is not None

    def mark_seen(self, events):
        with self.db:
            self.db.executemany("INSERT OR IGNORE INTO seen VALUES(?)", [(e,) for e in events])

    def watermark(self):
        return int(self._get("watermark", 0))

    def set_watermark(self, value):
        with self.db:
            self._set("watermark", max(self.watermark(), int(value)))

    def is_initialized(self):
        return self._get("initialized") == "1"

    def initialize(self, events):
        events = list(events)
        if self.is_initialized():
            raise StoreError("历史基线已建立。")
        with self.db:
            self.db.executemany("INSERT OR IGNORE INTO seen VALUES(?)", [(e.event_id,) for e in events])
            self._set("watermark", max((e.sort_seq for e in events), default=0))
            self._set("initialized", "1")

    def prepare(self, event_ids, state, last_question, reply):
        if not event_ids or len(set(event_ids)) != len(event_ids):
            raise StoreError("消息批次为空或包含重复事件。")
        encoded = json.dumps(state, ensure_ascii=False)
        with self.db:
            if self.paused_reason() or self.pending():
                raise StoreError("会话已暂停或存在未确认发送。")
            if any(self.seen(e) for e in event_ids):
                raise StoreError("已处理消息不能再次生成回复。")
            self.db.executemany("INSERT INTO seen VALUES(?)", [(e,) for e in event_ids])
            self._set("state", encoded)
            self._set("last_question", last_question)
            intent = None
            if reply:
                intent = uuid4().hex
                self.db.execute("INSERT INTO outbox VALUES(?,?,?, ?,NULL)", (intent, reply, "pending", time.time()))
        return intent

    def confirm(self, intent, event_id):
        if not isinstance(event_id, str) or not event_id.strip():
            raise StoreError("发送确认必须包含非空的消息身份。")
        with self.db:
            updated = self.db.execute("UPDATE outbox SET status='confirmed',confirmed_event_id=? WHERE id=? AND status='pending'", (event_id, intent))
            if updated.rowcount != 1:
                raise StoreError("待确认发送不存在或已经确认。")
            self.db.execute("INSERT OR IGNORE INTO seen VALUES(?)", (event_id,))

    def pending(self):
        rows = self.db.execute("SELECT id,created FROM outbox WHERE status='pending' ORDER BY created").fetchall()
        return [{"id": row[0], "created": row[1]} for row in rows]

    def pause(self, reason):
        with self.db:
            self._set("paused", reason)

    def paused_reason(self):
        return self._get("paused")

    def resume_after_review(self):
        """Explicit operator action; uncertain outgoing messages can never resume."""
        with self.db:
            if self.pending():
                raise StoreError("存在未确认发送，禁止解除暂停或自动重发；须人工核对。")
            self.db.execute("DELETE FROM meta WHERE key='paused'")
