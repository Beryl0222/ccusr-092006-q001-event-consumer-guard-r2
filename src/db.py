"""SQLite 存储层：schema 迁移、种子数据与原子操作。

仅依赖标准库。所有写操作在进程内串行化（_WRITE_LOCK），
核销配额使用条件 UPDATE 原子扣减，保证并发不突破商户承诺量。
"""

from __future__ import annotations

import json
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any, Iterable

CST = timezone(timedelta(hours=8))

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS api_tokens (
    token TEXT PRIMARY KEY,
    role TEXT NOT NULL,
    subject_id TEXT,
    label TEXT
);

CREATE TABLE IF NOT EXISTS policies (
    policy_id TEXT NOT NULL,
    version INTEGER NOT NULL,
    title TEXT NOT NULL,
    content_json TEXT NOT NULL,
    effective_at TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY (policy_id, version)
);

CREATE TABLE IF NOT EXISTS events (
    event_id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    organizer_id TEXT NOT NULL,
    sector TEXT,
    venue TEXT,
    starts_at TEXT NOT NULL,
    ends_at TEXT NOT NULL,
    expected_attendance INTEGER NOT NULL DEFAULT 0,
    status TEXT NOT NULL DEFAULT 'scheduled',
    weather_level TEXT NOT NULL DEFAULT 'normal',
    detail_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS event_schedule_history (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id TEXT NOT NULL,
    change_type TEXT NOT NULL,           -- reschedule / cancel / weather
    old_starts_at TEXT,
    new_starts_at TEXT,
    reason TEXT,
    weather_level TEXT NOT NULL DEFAULT 'normal',
    actor_role TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS merchants (
    merchant_id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    sector TEXT NOT NULL,
    contact TEXT,
    status TEXT NOT NULL DEFAULT 'active',   -- active / suspended
    suspend_reason TEXT,
    suspended_by TEXT,
    suspended_at TEXT,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS benefits (
    benefit_id TEXT PRIMARY KEY,
    event_id TEXT NOT NULL,
    code TEXT NOT NULL UNIQUE,
    title TEXT NOT NULL,
    quota INTEGER NOT NULL,
    valid_from TEXT NOT NULL,
    valid_to TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'active',  -- active / invalidated
    invalidated_reason TEXT,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS commitments (
    commitment_id TEXT PRIMARY KEY,
    event_id TEXT NOT NULL,
    merchant_id TEXT NOT NULL,
    kind TEXT NOT NULL,                    -- price / package / benefit / venue_hours
    spec_json TEXT NOT NULL DEFAULT '{}',
    valid_from TEXT NOT NULL,
    valid_to TEXT NOT NULL,
    promised_total INTEGER NOT NULL DEFAULT 0,  -- 0 表示不计量（如纯价格/时段承诺）
    redeemed_total INTEGER NOT NULL DEFAULT 0,
    active INTEGER NOT NULL DEFAULT 1,
    superseded_by TEXT,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS orders (
    order_id TEXT PRIMARY KEY,
    event_id TEXT NOT NULL,
    merchant_id TEXT NOT NULL,
    commitment_id TEXT,
    channel TEXT NOT NULL,
    channel_ref TEXT NOT NULL,
    dedup_key TEXT,                       -- 跨渠道同一订单的去重键
    consumer_ref TEXT,                    -- 脱敏后的消费者标识
    amount REAL NOT NULL,
    currency TEXT NOT NULL DEFAULT 'CNY',
    status TEXT NOT NULL DEFAULT 'created',
    rejected_reason TEXT,
    surcharge_flag INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    decided_at TEXT,
    UNIQUE (channel, channel_ref)
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_orders_dedup
    ON orders(dedup_key) WHERE dedup_key IS NOT NULL;

CREATE TABLE IF NOT EXISTS redemptions (
    redemption_id TEXT PRIMARY KEY,
    order_id TEXT NOT NULL,
    commitment_id TEXT NOT NULL,
    event_id TEXT NOT NULL,
    qty INTEGER NOT NULL CHECK (qty > 0),
    status TEXT NOT NULL DEFAULT 'redeemed',
    channel TEXT NOT NULL,
    created_at TEXT NOT NULL,
    reversed_at TEXT
);

CREATE TABLE IF NOT EXISTS complaints (
    complaint_id TEXT PRIMARY KEY,
    event_id TEXT NOT NULL,
    order_id TEXT,
    merchant_id TEXT NOT NULL,
    consumer_ref TEXT,
    channel TEXT NOT NULL,
    channel_ref TEXT NOT NULL,
    dedup_key TEXT,
    category TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'open',
    root_cause TEXT,
    weather_snapshot TEXT NOT NULL DEFAULT 'normal',
    handler_role TEXT,
    duplicate_of TEXT,                    -- 指向首次投诉
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE (channel, channel_ref)
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_complaints_dedup
    ON complaints(dedup_key) WHERE dedup_key IS NOT NULL;

CREATE TABLE IF NOT EXISTS investigations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    complaint_id TEXT NOT NULL,
    note TEXT NOT NULL,
    evidence_json TEXT NOT NULL DEFAULT '{}',
    actor_role TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS appeals (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    complaint_id TEXT NOT NULL,
    reason TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',  -- pending / upheld / rejected
    evidence_json TEXT NOT NULL DEFAULT '{}',
    ruled_by TEXT,
    ruling_note TEXT,
    created_at TEXT NOT NULL,
    ruled_at TEXT
);

CREATE TABLE IF NOT EXISTS dispositions (
    disposition_id TEXT PRIMARY KEY,
    complaint_id TEXT NOT NULL,
    kind TEXT NOT NULL,                   -- refund / compensation
    amount REAL NOT NULL CHECK (amount >= 0),
    currency TEXT NOT NULL DEFAULT 'CNY',
    root_cause TEXT NOT NULL,
    bearer TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'settled', -- settled / reversed
    policy_id TEXT NOT NULL,
    policy_version INTEGER NOT NULL,
    created_at TEXT NOT NULL,
    reversed_at TEXT,
    reversal_reason TEXT
);

-- 同一投诉同一处置类型只允许一笔有效（settled）赔付，从根上杜绝重复赔付。
CREATE UNIQUE INDEX IF NOT EXISTS idx_dispositions_once
    ON dispositions(complaint_id, kind) WHERE status = 'settled';

CREATE TABLE IF NOT EXISTS event_chain (
    seq INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id TEXT,                        -- 赛事 id（可空，便于登记前异常）
    scope TEXT NOT NULL,                  -- order / complaint / event / merchant ...
    subject_ref TEXT NOT NULL,            -- 订单/投诉/承诺等业务 id
    action TEXT NOT NULL,
    actor_role TEXT,
    detail_json TEXT NOT NULL DEFAULT '{}',
    causation_seq INTEGER,                -- 前序事件（构成链条）
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS alerts (
    alert_id TEXT PRIMARY KEY,
    event_id TEXT NOT NULL,
    subject_id TEXT NOT NULL,
    type TEXT NOT NULL,
    severity TEXT NOT NULL,
    title TEXT NOT NULL,
    explanation TEXT NOT NULL,
    evidence_json TEXT NOT NULL DEFAULT '{}',
    related_json TEXT NOT NULL DEFAULT '[]',
    status TEXT NOT NULL DEFAULT 'open',  -- open / acknowledged / resolved
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS idempotency_keys (
    idem_key TEXT PRIMARY KEY,
    method TEXT NOT NULL,
    path TEXT NOT NULL,
    response_status INTEGER NOT NULL,
    response_body TEXT NOT NULL,
    created_at TEXT NOT NULL
);
"""

# 演示用固定令牌；生产应由部署方通过环境变量/密钥管理轮换。
SEED_TOKENS = [
    ("org-token", "organizer", "org-001", "主办方"),
    ("reg-token", "market_regulator", "reg-001", "市场监管值班号"),
    ("tour-token", "culture_tourism", "tour-001", "文旅值班号"),
    ("op-token", "event_operator", "ops-001", "赛事运营值班号"),
    ("staff-token", "duty_staff", "staff-001", "商务部门值班人员"),
    # 演示商户令牌（subject_id 即商户号），生产由商户自助注册后签发。
    ("mer-hotel-token", "merchant", "m-hotel", "东站快捷酒店"),
    ("mer-food-token", "merchant", "m-food", "看台小吃集合店"),
    ("mer-mall-token", "merchant", "m-mall", "中心商圈第二现场"),
]


def now_iso() -> str:
    return datetime.now(CST).isoformat(timespec="seconds")


def to_iso(value: str) -> str:
    """把任意 ISO 8601 时间归一化到东八区可比较格式。"""
    dt = datetime.fromisoformat(value)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=CST)
    return dt.astimezone(CST).isoformat(timespec="seconds")


class Database:
    def __init__(self, path: str | Path = ":memory:"):
        self.path = str(path)
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(
            self.path, check_same_thread=False, isolation_level=None
        )
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA foreign_keys=ON")
        self._write_lock = threading.RLock()
        self._init_schema()
        self._seed()

    # -- 基础 --------------------------------------------------------------

    def _init_schema(self) -> None:
        self._conn.executescript(SCHEMA)

    def _seed(self) -> None:
        with self.transaction():
            for token, role, subject, label in SEED_TOKENS:
                self.execute(
                    "INSERT OR IGNORE INTO api_tokens(token, role, subject_id, label)"
                    " VALUES (?,?,?,?)",
                    (token, role, subject, label),
                )
            seeded = self.scalar("SELECT value FROM meta WHERE key='policy_seeded'")
            if not seeded:
                ts = now_iso()
                self.execute(
                    "INSERT INTO policies(policy_id, version, title, content_json,"
                    " effective_at, created_at) VALUES (?,?,?,?,?,?)",
                    ("guard-default", 1, "赛事消费联防基础政策",
                     json.dumps({"compensation_multiplier": 1.0,
                                 "note": "首版：违约退一赔一基线"}, ensure_ascii=False),
                     "2026-01-01T00:00:00+08:00", ts),
                )
                self.execute(
                    "INSERT INTO policies(policy_id, version, title, content_json,"
                    " effective_at, created_at) VALUES (?,?,?,?,?,?)",
                    ("guard-default", 2, "赛事消费联防基础政策（秋季修订）",
                     json.dumps({"compensation_multiplier": 1.5,
                                 "note": "2026-09-01 起：恶意加价退一赔一点五"}, ensure_ascii=False),
                     "2026-09-01T00:00:00+08:00", ts),
                )
                self.execute(
                    "INSERT OR IGNORE INTO meta(key, value) VALUES ('policy_seeded','1')"
                )

    @contextmanager
    def transaction(self):
        # BEGIN IMMEDIATE 立即获取写锁，配合条件 UPDATE 实现原子扣减。
        self._write_lock.acquire()
        try:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                yield self
                self._conn.execute("COMMIT")
            except Exception:
                self._conn.execute("ROLLBACK")
                raise
        finally:
            self._write_lock.release()

    def execute(self, sql: str, params: Iterable[Any] = ()):
        # 单一连接在多线程（HTTP ThreadingHTTPServer）间共享，
        # 用可重入锁串行化全部访问；RLock 保证事务内嵌套调用不死锁。
        with self._write_lock:
            return self._conn.execute(sql, tuple(params))

    def executemany(self, sql: str, seq: Iterable[Iterable[Any]]):
        with self._write_lock:
            return self._conn.executemany(sql, [tuple(p) for p in seq])

    def query(self, sql: str, params: Iterable[Any] = ()) -> list[sqlite3.Row]:
        with self._write_lock:
            return list(self._conn.execute(sql, tuple(params)).fetchall())

    def query_one(self, sql: str, params: Iterable[Any] = ()) -> sqlite3.Row | None:
        with self._write_lock:
            return self._conn.execute(sql, tuple(params)).fetchone()

    def scalar(self, sql: str, params: Iterable[Any] = ()):
        with self._write_lock:
            row = self._conn.execute(sql, tuple(params)).fetchone()
        return row[0] if row else None

    def close(self) -> None:
        self._conn.close()
