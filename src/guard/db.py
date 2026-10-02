"""SQLite 存储层：schema、连接管理与领域写操作。

设计要点
--------
* 单连接 + ``threading.RLock`` 保护写事务；WAL 模式提升读并发，
  ``BEGIN IMMEDIATE`` 语义由锁串行化保证，核销计数天然不会超卖。
* ``ledger`` 为只追加事件链，触发器禁止 UPDATE/DELETE。
* 订单在创建时固化 ``policy_version``，后续处置沿用该版本，
  政策变更不溯及既往。
"""

from __future__ import annotations

import json
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .engine import (
    bearer_for,
    evaluate_early_closure,
    evaluate_mass_rejection,
    evaluate_price_spike,
    minutes_between,
)

# ---------------------------------------------------------------------------
# 常量
# ---------------------------------------------------------------------------

DEPARTMENTS = ("MARKET_REGULATION", "TOURISM", "EVENT_OPERATION")

EVENT_STATUSES = ("SCHEDULED", "RESCHEDULED", "CANCELLED", "FINISHED")
MERCHANT_CATEGORIES = ("LODGING", "DINING", "RETAIL", "TOURISM")

# 投诉类别 -> 默认处置部门（也可被政策规则覆盖）
CATEGORY_DEPT = {
    "PRICE_GOUGE": "MARKET_REGULATION",
    "FAKE_PACKAGE": "MARKET_REGULATION",
    "REDEMPTION_FAIL": "MARKET_REGULATION",
    "REFUND_DISPUTE": "MARKET_REGULATION",
    "EARLY_CLOSURE": "TOURISM",
    "SECOND_VENUE_CLOSED": "TOURISM",
    "BENEFIT_ISSUE": "EVENT_OPERATION",
    "EVENT_CANCELLED": "EVENT_OPERATION",
    "OTHER": "MARKET_REGULATION",
}

SCHEMA = """
CREATE TABLE IF NOT EXISTS sporting_events (
    id               TEXT PRIMARY KEY,
    name             TEXT NOT NULL,
    organizer_id     TEXT NOT NULL,
    expected_traffic INTEGER NOT NULL DEFAULT 0,
    status           TEXT NOT NULL DEFAULT 'SCHEDULED',
    created_at       TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS event_sessions (
    id             TEXT PRIMARY KEY,
    event_id       TEXT NOT NULL REFERENCES sporting_events(id),
    session_no     INTEGER NOT NULL,
    kickoff_at     TEXT NOT NULL,
    ends_at        TEXT NOT NULL,
    venue          TEXT NOT NULL,
    weather_status TEXT NOT NULL DEFAULT 'NORMAL',
    status         TEXT NOT NULL DEFAULT 'SCHEDULED',
    created_at     TEXT NOT NULL,
    UNIQUE(event_id, session_no)
);

CREATE TABLE IF NOT EXISTS merchants (
    id               TEXT PRIMARY KEY,
    name             TEXT NOT NULL,
    category         TEXT NOT NULL,
    district         TEXT NOT NULL,
    status           TEXT NOT NULL DEFAULT 'ACTIVE',
    suspended_reason TEXT,
    suspended_at     TEXT,
    created_at       TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS policies (
    version        INTEGER PRIMARY KEY,
    effective_from TEXT NOT NULL,
    effective_to   TEXT,
    note           TEXT,
    rules          TEXT NOT NULL,
    created_at     TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS commitments (
    id                 TEXT PRIMARY KEY,
    merchant_id        TEXT NOT NULL REFERENCES merchants(id),
    event_id           TEXT NOT NULL REFERENCES sporting_events(id),
    session_id         TEXT REFERENCES event_sessions(id),
    sku                TEXT NOT NULL,
    price_cents        INTEGER NOT NULL CHECK (price_cents >= 0),
    currency           TEXT NOT NULL DEFAULT 'CNY',
    valid_from         TEXT NOT NULL,
    valid_to           TEXT NOT NULL,
    fulfill_qty        INTEGER NOT NULL CHECK (fulfill_qty >= 0),
    redeemed_qty       INTEGER NOT NULL DEFAULT 0 CHECK (redeemed_qty >= 0),
    status             TEXT NOT NULL DEFAULT 'ACTIVE',
    revoked_reason     TEXT,
    policy_version     INTEGER NOT NULL,
    created_at         TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS price_quotes (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    commitment_id TEXT NOT NULL REFERENCES commitments(id),
    price_cents   INTEGER NOT NULL,
    quoted_at     TEXT NOT NULL,
    source        TEXT NOT NULL DEFAULT 'MERCHANT_REPORT'
);

CREATE TABLE IF NOT EXISTS official_benefits (
    id                 TEXT PRIMARY KEY,
    event_id           TEXT NOT NULL REFERENCES sporting_events(id),
    code               TEXT NOT NULL,
    title              TEXT NOT NULL,
    total_qty          INTEGER NOT NULL CHECK (total_qty >= 0),
    redeemed_qty       INTEGER NOT NULL DEFAULT 0,
    valid_from         TEXT NOT NULL,
    valid_to           TEXT NOT NULL,
    status             TEXT NOT NULL DEFAULT 'ACTIVE',
    invalidated_reason TEXT,
    created_at         TEXT
);

CREATE TABLE IF NOT EXISTS orders (
    id                TEXT PRIMARY KEY,
    event_id          TEXT NOT NULL REFERENCES sporting_events(id),
    session_id        TEXT REFERENCES event_sessions(id),
    merchant_id       TEXT NOT NULL REFERENCES merchants(id),
    commitment_id     TEXT REFERENCES commitments(id),
    order_channel     TEXT NOT NULL,
    base_amount_cents INTEGER NOT NULL DEFAULT 0,
    final_amount_cents INTEGER NOT NULL CHECK (final_amount_cents >= 0),
    currency          TEXT NOT NULL DEFAULT 'CNY',
    status            TEXT NOT NULL DEFAULT 'BOOKED',
    reject_reason     TEXT,
    policy_version    INTEGER NOT NULL,
    idem_key          TEXT UNIQUE,
    created_at        TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS redemptions (
    id            TEXT PRIMARY KEY,
    commitment_id TEXT REFERENCES commitments(id),
    benefit_id    TEXT REFERENCES official_benefits(id),
    order_id      TEXT REFERENCES orders(id),
    code          TEXT,
    channel       TEXT NOT NULL,
    result        TEXT NOT NULL,
    reason        TEXT,
    idem_key      TEXT UNIQUE,
    created_at    TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS complaints (
    id           TEXT PRIMARY KEY,
    event_id     TEXT NOT NULL REFERENCES sporting_events(id),
    order_id     TEXT REFERENCES orders(id),
    channel      TEXT NOT NULL,
    business_key TEXT NOT NULL,
    reporter_ref TEXT,
    category     TEXT NOT NULL,
    reason       TEXT NOT NULL,
    status       TEXT NOT NULL DEFAULT 'OPEN',
    assigned_dept TEXT NOT NULL,
    idem_key     TEXT UNIQUE,
    dedup_of     TEXT REFERENCES complaints(id),
    resolution   TEXT,
    created_at   TEXT NOT NULL,
    resolved_at  TEXT
);
CREATE INDEX IF NOT EXISTS idx_complaints_bkey ON complaints(business_key);
CREATE INDEX IF NOT EXISTS idx_complaints_event ON complaints(event_id);

CREATE TABLE IF NOT EXISTS complaint_handlings (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    complaint_id TEXT NOT NULL REFERENCES complaints(id),
    action       TEXT NOT NULL,
    from_dept    TEXT,
    to_dept      TEXT,
    note         TEXT,
    acted_by     TEXT NOT NULL,
    acted_at     TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS remedies (
    id             TEXT PRIMARY KEY,
    complaint_id   TEXT REFERENCES complaints(id),
    order_id       TEXT REFERENCES orders(id),
    event_id       TEXT NOT NULL REFERENCES sporting_events(id),
    kind           TEXT NOT NULL CHECK (kind IN ('REFUND','COMPENSATION','FEE_WAIVER')),
    amount_cents   INTEGER NOT NULL CHECK (amount_cents >= 0),
    currency       TEXT NOT NULL DEFAULT 'CNY',
    status         TEXT NOT NULL DEFAULT 'PROPOSED',
    bearer_type    TEXT NOT NULL,
    bearer_id      TEXT NOT NULL,
    bearer_reason  TEXT NOT NULL,
    policy_version INTEGER NOT NULL,
    category       TEXT NOT NULL,
    idem_key       TEXT UNIQUE,
    proposed_by    TEXT NOT NULL,
    approved_by    TEXT,
    created_at     TEXT NOT NULL,
    approved_at    TEXT,
    paid_at        TEXT
);
CREATE INDEX IF NOT EXISTS idx_remedies_order ON remedies(order_id, kind);

CREATE TABLE IF NOT EXISTS alerts (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id         TEXT NOT NULL REFERENCES sporting_events(id),
    type             TEXT NOT NULL,
    subject          TEXT NOT NULL,
    severity         TEXT NOT NULL DEFAULT 'WARN',
    reason           TEXT NOT NULL,
    evidence         TEXT NOT NULL,
    status           TEXT NOT NULL DEFAULT 'OPEN',
    created_at       TEXT NOT NULL,
    acknowledged_at  TEXT,
    acknowledged_by  TEXT
);
CREATE INDEX IF NOT EXISTS idx_alerts_event ON alerts(event_id);

CREATE TABLE IF NOT EXISTS ledger (
    seq            INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id       TEXT,
    aggregate_type TEXT NOT NULL,
    aggregate_id   TEXT NOT NULL,
    event_type     TEXT NOT NULL,
    payload        TEXT NOT NULL,
    actor          TEXT NOT NULL,
    policy_version INTEGER,
    causation_id   INTEGER,
    occurred_at    TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_ledger_agg ON ledger(aggregate_type, aggregate_id, seq);
CREATE INDEX IF NOT EXISTS idx_ledger_event ON ledger(event_id, seq);

CREATE TRIGGER IF NOT EXISTS ledger_no_update BEFORE UPDATE ON ledger
BEGIN
    SELECT RAISE(ABORT, 'ledger is append-only');
END;
CREATE TRIGGER IF NOT EXISTS ledger_no_delete BEFORE DELETE ON ledger
BEGIN
    SELECT RAISE(ABORT, 'ledger is append-only');
END;

CREATE TABLE IF NOT EXISTS idempotency (
    scope         TEXT NOT NULL,
    idem_key      TEXT NOT NULL,
    request_hash  TEXT NOT NULL,
    response_code INTEGER NOT NULL,
    response_body TEXT NOT NULL,
    created_at    TEXT NOT NULL,
    PRIMARY KEY (scope, idem_key)
);
"""

# 基线政策：长期有效，新政策生效后自动被截断
DEFAULT_RULES: dict[str, Any] = {
    "price_spike_ratio": 0.20,
    "price_spike_window_hours": 72,
    "mass_rejection_count": 5,
    "mass_rejection_window_minutes": 60,
    "mass_rejection_ratio": 0.50,
    "early_closure_grace_minutes": 30,
    # 各类投诉下退款/补偿的最终承担方
    "bearer_map": {
        "PRICE_GOUGE":         {"REFUND": ["MERCHANT", "self"], "COMPENSATION": ["MERCHANT", "self"]},
        "FAKE_PACKAGE":        {"REFUND": ["MERCHANT", "self"], "COMPENSATION": ["MERCHANT", "self"]},
        "REDEMPTION_FAIL":     {"REFUND": ["MERCHANT", "self"], "COMPENSATION": ["MERCHANT", "self"]},
        "REFUND_DISPUTE":      {"REFUND": ["MERCHANT", "self"], "COMPENSATION": ["MERCHANT", "self"]},
        "EARLY_CLOSURE":       {"REFUND": ["MERCHANT", "self"], "COMPENSATION": ["GOVERNMENT_FUND", "district-relief"]},
        "SECOND_VENUE_CLOSED": {"REFUND": ["MERCHANT", "self"], "COMPENSATION": ["GOVERNMENT_FUND", "district-relief"]},
        "BENEFIT_ISSUE":       {"REFUND": ["ORGANIZER", "benefit-reserve"], "COMPENSATION": ["ORGANIZER", "benefit-reserve"]},
        "EVENT_CANCELLED":     {"REFUND": ["ORGANIZER", "event-risk-reserve"], "COMPENSATION": ["ORGANIZER", "event-risk-reserve"]},
        "OTHER":               {"REFUND": ["MERCHANT", "self"], "COMPENSATION": ["GOVERNMENT_FUND", "district-relief"]},
    },
}


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def new_id(prefix: str) -> str:
    import uuid

    return f"{prefix}_{uuid.uuid4().hex[:12]}"


class GuardError(Exception):
    """业务错误：code 供 API 层映射为 4xx。"""

    def __init__(self, code: str, message: str, status: int = 400):
        super().__init__(message)
        self.code = code
        self.status = status


class GuardStore:
    def __init__(self, path: str | Path = ":memory:"):
        self.path = str(path)
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(self.path, check_same_thread=False, isolation_level=None)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA foreign_keys=ON")
        self._lock = threading.RLock()
        with self._lock:
            self._conn.executescript(SCHEMA)
            self._seed_default_policy()

    # ------------------------------------------------------------------ utils
    def _seed_default_policy(self) -> None:
        row = self._conn.execute("SELECT COUNT(*) AS n FROM policies").fetchone()
        if row["n"] == 0:
            self._conn.execute(
                "INSERT INTO policies(version, effective_from, effective_to, note, rules, created_at) "
                "VALUES (1, '1999-01-01T00:00:00+00:00', NULL, '基线政策', ?, ?)",
                (json.dumps(DEFAULT_RULES, ensure_ascii=False), utcnow()),
            )

    def append(
        self,
        aggregate_type: str,
        aggregate_id: str,
        event_type: str,
        payload: dict[str, Any],
        actor: str,
        event_id: str | None = None,
        policy_version: int | None = None,
        causation_id: int | None = None,
    ) -> int:
        cur = self._conn.execute(
            "INSERT INTO ledger(event_id, aggregate_type, aggregate_id, event_type, payload, actor, "
            "policy_version, causation_id, occurred_at) VALUES (?,?,?,?,?,?,?,?,?)",
            (
                event_id,
                aggregate_type,
                aggregate_id,
                event_type,
                json.dumps(payload, ensure_ascii=False),
                actor,
                policy_version,
                causation_id,
                payload.get("at") or utcnow(),
            ),
        )
        return int(cur.lastrowid)

    def _one(self, sql: str, params: tuple = ()) -> sqlite3.Row:
        row = self._conn.execute(sql, params).fetchone()
        if row is None:
            raise GuardError("NOT_FOUND", "资源不存在", 404)
        return row

    # ------------------------------------------------------------ policies
    def active_policy(self, at: str | None = None) -> sqlite3.Row:
        at = at or utcnow()
        row = self._conn.execute(
            "SELECT * FROM policies WHERE effective_from <= ? "
            "AND (effective_to IS NULL OR effective_to > ?) ORDER BY version DESC LIMIT 1",
            (at, at),
        ).fetchone()
        if row is None:
            raise GuardError("NO_POLICY", f"{at} 时刻无生效政策", 409)
        return row

    def create_policy(self, effective_from: str, rules: dict, note: str, actor: str) -> dict:
        """发布新版本政策；自动截断上一版的生效区间。"""
        with self._lock:
            prev = self._conn.execute(
                "SELECT version FROM policies WHERE effective_from <= ? AND "
                "(effective_to IS NULL OR effective_to > ?) ORDER BY version DESC LIMIT 1",
                (effective_from, effective_from),
            ).fetchone()
            new_version = int(self._conn.execute("SELECT COALESCE(MAX(version),0)+1 AS v FROM policies").fetchone()["v"])
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                if prev is not None:
                    self._conn.execute(
                        "UPDATE policies SET effective_to = ? WHERE version = ? AND effective_to IS NULL",
                        (effective_from, prev["version"]),
                    )
                self._conn.execute(
                    "INSERT INTO policies(version, effective_from, effective_to, note, rules, created_at) "
                    "VALUES (?,?,NULL,?,?,?)",
                    (new_version, effective_from, note, json.dumps(rules, ensure_ascii=False), utcnow()),
                )
                self.append(
                    "policy", str(new_version), "POLICY_PUBLISHED",
                    {"version": new_version, "effective_from": effective_from, "note": note, "rules": rules,
                     "at": utcnow()}, actor,
                )
                self._conn.execute("COMMIT")
            except Exception:
                self._conn.execute("ROLLBACK")
                raise
            return self.get_policy(new_version)

    def get_policy(self, version: int) -> dict:
        row = self._one("SELECT * FROM policies WHERE version = ?", (version,))
        return _policy_dict(row)

    def list_policies(self) -> list[dict]:
        rows = self._conn.execute("SELECT * FROM policies ORDER BY version").fetchall()
        return [_policy_dict(r) for r in rows]

    # ------------------------------------------------------------ events
    def create_event(self, name: str, organizer_id: str, expected_traffic: int, actor: str) -> dict:
        with self._lock:
            eid = new_id("evt")
            at = utcnow()
            self._conn.execute(
                "INSERT INTO sporting_events(id,name,organizer_id,expected_traffic,status,created_at) "
                "VALUES (?,?,?,?, 'SCHEDULED', ?)",
                (eid, name, organizer_id, expected_traffic, at),
            )
            self.append("event", eid, "EVENT_PUBLISHED",
                        {"event_id": eid, "name": name, "organizer_id": organizer_id,
                         "expected_traffic": expected_traffic, "at": at}, actor, event_id=eid)
            return self.get_event(eid)

    def add_session(self, event_id: str, kickoff_at: str, ends_at: str, venue: str, actor: str) -> dict:
        with self._lock:
            self._one("SELECT id FROM sporting_events WHERE id=?", (event_id,))
            if not (kickoff_at < ends_at):
                raise GuardError("BAD_TIME", "开赛时间必须早于散场时间")
            no = int(self._conn.execute(
                "SELECT COALESCE(MAX(session_no),0)+1 AS n FROM event_sessions WHERE event_id=?", (event_id,)
            ).fetchone()["n"])
            sid = new_id("ses")
            self._conn.execute(
                "INSERT INTO event_sessions(id,event_id,session_no,kickoff_at,ends_at,venue,status,created_at) "
                "VALUES (?,?,?,?,?,?, 'SCHEDULED', ?)",
                (sid, event_id, no, kickoff_at, ends_at, venue, utcnow()),
            )
            self.append("session", sid, "SESSION_SCHEDULED",
                        {"session_id": sid, "event_id": event_id, "session_no": no,
                         "kickoff_at": kickoff_at, "ends_at": ends_at, "venue": venue, "at": utcnow()},
                        actor, event_id=event_id)
            return self.get_session(sid)

    def _set_session_status(self, session_id: str, status: str, weather: str | None,
                            reason: str, event_type: str, actor: str,
                            new_kickoff: str | None = None, new_ends: str | None = None) -> dict:
        row = self._one("SELECT * FROM event_sessions WHERE id=?", (session_id,))
        self._conn.execute(
            "UPDATE event_sessions SET status=?, weather_status=COALESCE(?, weather_status), "
            "kickoff_at=COALESCE(?, kickoff_at), ends_at=COALESCE(?, ends_at) WHERE id=?",
            (status, weather, new_kickoff, new_ends, session_id),
        )
        payload = {"session_id": session_id, "event_id": row["event_id"], "status": status,
                   "reason": reason, "at": utcnow()}
        if new_kickoff:
            payload["new_kickoff_at"] = new_kickoff
            payload["new_ends_at"] = new_ends
        self.append("session", session_id, event_type, payload, actor, event_id=row["event_id"])
        return self.get_session(session_id)

    def flag_weather(self, session_id: str, weather_status: str, actor: str) -> dict:
        with self._lock:
            if weather_status not in ("NORMAL", "EXTREME_WEATHER"):
                raise GuardError("BAD_WEATHER", "weather_status 仅支持 NORMAL/EXTREME_WEATHER")
            row = self._one("SELECT * FROM event_sessions WHERE id=?", (session_id,))
            self._conn.execute("UPDATE event_sessions SET weather_status=? WHERE id=?", (weather_status, session_id))
            self.append("session", session_id, "WEATHER_FLAGGED",
                        {"session_id": session_id, "event_id": row["event_id"],
                         "weather_status": weather_status, "at": utcnow()}, actor, event_id=row["event_id"])
            return self.get_session(session_id)

    def reschedule_session(self, session_id: str, new_kickoff_at: str, new_ends_at: str,
                           reason: str, actor: str) -> dict:
        with self._lock:
            if not (new_kickoff_at < new_ends_at):
                raise GuardError("BAD_TIME", "改期后的开赛时间必须早于散场时间")
            return self._set_session_status(
                session_id, "RESCHEDULED", None, reason, "SESSION_RESCHEDULED", actor,
                new_kickoff=new_kickoff_at, new_ends=new_ends_at)

    def cancel_session(self, session_id: str, reason: str, extreme_weather: bool, actor: str) -> dict:
        with self._lock:
            result = self._set_session_status(
                session_id, "CANCELLED",
                "EXTREME_WEATHER" if extreme_weather else None,
                reason, "SESSION_CANCELLED", actor)
            # 赛事取消导致官方权益批量失效（可解释、可审计）
            benefits = self._conn.execute(
                "SELECT * FROM official_benefits WHERE event_id=? AND status='ACTIVE'",
                (result["event_id"],),
            ).fetchall()
            for b in benefits:
                self._invalidate_benefit_row(b, f"场次取消：{reason}", actor, causation="SESSION_CANCELLED")
            return result

    def get_event(self, event_id: str) -> dict:
        row = self._one("SELECT * FROM sporting_events WHERE id=?", (event_id,))
        return dict(row)

    def get_session(self, session_id: str) -> dict:
        return dict(self._one("SELECT * FROM event_sessions WHERE id=?", (session_id,)))

    def list_event_sessions(self, event_id: str) -> list[dict]:
        return [dict(r) for r in self._conn.execute(
            "SELECT * FROM event_sessions WHERE event_id=? ORDER BY session_no", (event_id,)).fetchall()]

    # ------------------------------------------------------------ merchants
    def register_merchant(self, name: str, category: str, district: str, actor: str) -> dict:
        with self._lock:
            if category not in MERCHANT_CATEGORIES:
                raise GuardError("BAD_CATEGORY", f"category 必须为 {MERCHANT_CATEGORIES}")
            mid = new_id("mer")
            self._conn.execute(
                "INSERT INTO merchants(id,name,category,district,status,created_at) VALUES (?,?,?,?,'ACTIVE',?)",
                (mid, name, category, district, utcnow()),
            )
            self.append("merchant", mid, "MERCHANT_REGISTERED",
                        {"merchant_id": mid, "name": name, "category": category,
                         "district": district, "at": utcnow()}, actor)
            return self.get_merchant(mid)

    def suspend_merchant(self, merchant_id: str, reason: str, actor: str,
                         officer_dept: str | None = None) -> dict:
        with self._lock:
            if officer_dept is not None and officer_dept != "MARKET_REGULATION":
                raise GuardError("OUT_OF_JURISDICTION", "停业处理仅限市场监管部门", 403)
            self._one("SELECT id FROM merchants WHERE id=?", (merchant_id,))
            self._conn.execute(
                "UPDATE merchants SET status='SUSPENDED', suspended_reason=?, suspended_at=? WHERE id=?",
                (reason, utcnow(), merchant_id),
            )
            self.append("merchant", merchant_id, "MERCHANT_SUSPENDED",
                        {"merchant_id": merchant_id, "reason": reason, "at": utcnow()}, actor)
            return self.get_merchant(merchant_id)

    def resume_merchant(self, merchant_id: str, actor: str) -> dict:
        with self._lock:
            self._one("SELECT id FROM merchants WHERE id=?", (merchant_id,))
            self._conn.execute(
                "UPDATE merchants SET status='ACTIVE', suspended_reason=NULL, suspended_at=NULL WHERE id=?",
                (merchant_id,),
            )
            self.append("merchant", merchant_id, "MERCHANT_RESUMED",
                        {"merchant_id": merchant_id, "at": utcnow()}, actor)
            return self.get_merchant(merchant_id)

    def get_merchant(self, merchant_id: str) -> dict:
        return dict(self._one("SELECT * FROM merchants WHERE id=?", (merchant_id,)))

    # ------------------------------------------------------------ commitments
    def publish_commitment(self, merchant_id: str, event_id: str, sku: str, price_cents: int,
                           valid_from: str, valid_to: str, fulfill_qty: int,
                           actor: str, session_id: str | None = None) -> dict:
        with self._lock:
            m = self._one("SELECT * FROM merchants WHERE id=?", (merchant_id,))
            if m["status"] != "ACTIVE":
                raise GuardError("MERCHANT_SUSPENDED", "商户已停业，不能发布承诺")
            self._one("SELECT id FROM sporting_events WHERE id=?", (event_id,))
            if not (valid_from < valid_to) or price_cents < 0 or fulfill_qty < 0:
                raise GuardError("BAD_COMMITMENT", "有效期/价格/承诺量不合法")
            policy = self.active_policy()
            cid = new_id("com")
            at = utcnow()
            self._conn.execute(
                "INSERT INTO commitments(id,merchant_id,event_id,session_id,sku,price_cents,currency,"
                "valid_from,valid_to,fulfill_qty,redeemed_qty,status,policy_version,created_at) "
                "VALUES (?,?,?,?,?,?, 'CNY', ?,?,?, 0, 'ACTIVE', ?,?)",
                (cid, merchant_id, event_id, session_id, sku, price_cents,
                 valid_from, valid_to, fulfill_qty, policy["version"], at),
            )
            self._conn.execute(
                "INSERT INTO price_quotes(commitment_id, price_cents, quoted_at, source) VALUES (?,?,?,?)",
                (cid, price_cents, at, "INITIAL"),
            )
            self.append("commitment", cid, "MERCHANT_COMMITMENT",
                        {"commitment_id": cid, "merchant_id": merchant_id, "event_id": event_id,
                         "sku": sku, "price_cents": price_cents, "valid_from": valid_from,
                         "valid_to": valid_to, "fulfill_qty": fulfill_qty,
                         "policy_version": policy["version"], "at": at},
                        actor, event_id=event_id)
            return self.get_commitment(cid)

    def report_price(self, commitment_id: str, price_cents: int, actor: str,
                     source: str = "MERCHANT_REPORT", at: str | None = None) -> dict:
        with self._lock:
            com = self._one("SELECT * FROM commitments WHERE id=?", (commitment_id,))
            at = at or utcnow()
            prev = self._conn.execute(
                "SELECT price_cents FROM price_quotes WHERE commitment_id=? ORDER BY id DESC LIMIT 1",
                (commitment_id,),
            ).fetchone()
            self._conn.execute(
                "INSERT INTO price_quotes(commitment_id,price_cents,quoted_at,source) VALUES (?,?,?,?)",
                (commitment_id, price_cents, at, source),
            )
            self.append("commitment", commitment_id, "PRICE_QUOTED",
                        {"commitment_id": commitment_id, "price_cents": price_cents,
                         "previous_cents": prev["price_cents"] if prev else None, "at": at},
                        actor, event_id=com["event_id"])
            alert = None
            if prev is not None and com["valid_from"] <= at <= com["valid_to"]:
                rules = json.loads(self.active_policy(at)["rules"])
                finding = evaluate_price_spike(
                    prev["price_cents"], price_cents, rules,
                    commitment_valid_from=com["valid_from"], commitment_valid_to=com["valid_to"])
                if finding is not None:
                    alert = self._raise_alert(
                        com["event_id"], "PRICE_SPIKE", f"commitment:{commitment_id}",
                        finding["severity"], finding["reason"], finding["evidence"], actor)
                    # 报价超出承诺价即构成临时加价，承诺价仍以首次发布为准，不更新 commitments.price_cents
            out = self.get_commitment(commitment_id)
            out["latest_quote_cents"] = price_cents
            out["alert"] = alert
            return out

    def revoke_commitment(self, commitment_id: str, reason: str, actor: str,
                          officer_dept: str | None = None) -> dict:
        with self._lock:
            if officer_dept is not None and officer_dept != "MARKET_REGULATION":
                raise GuardError("OUT_OF_JURISDICTION", "强制撤销承诺仅限市场监管部门", 403)
            com = self._one("SELECT * FROM commitments WHERE id=?", (commitment_id,))
            self._conn.execute(
                "UPDATE commitments SET status='REVOKED', revoked_reason=? WHERE id=?",
                (reason, commitment_id),
            )
            self.append("commitment", commitment_id, "COMMITMENT_REVOKED",
                        {"commitment_id": commitment_id, "reason": reason, "at": utcnow()},
                        actor, event_id=com["event_id"])
            evidence = {"commitment_id": commitment_id, "merchant_id": com["merchant_id"],
                        "sku": com["sku"], "reason": reason}
            self._raise_alert(com["event_id"], "COMMITMENT_REVOKED",
                              f"commitment:{commitment_id}", "HIGH",
                              f"商户承诺 {com['sku']} 在有效期内被撤销/失效：{reason}",
                              evidence, actor)
            return self.get_commitment(commitment_id)

    def get_commitment(self, commitment_id: str) -> dict:
        row = self._one("SELECT * FROM commitments WHERE id=?", (commitment_id,))
        out = dict(row)
        out["remaining_qty"] = out["fulfill_qty"] - out["redeemed_qty"]
        return out

    # ------------------------------------------------------------ benefits
    def create_benefit(self, event_id: str, code: str, title: str, total_qty: int,
                       valid_from: str, valid_to: str, actor: str) -> dict:
        with self._lock:
            self._one("SELECT id FROM sporting_events WHERE id=?", (event_id,))
            if not (valid_from < valid_to) or total_qty < 0:
                raise GuardError("BAD_BENEFIT", "权益有效期或总量不合法")
            bid = new_id("ben")
            self._conn.execute(
                "INSERT INTO official_benefits(id,event_id,code,title,total_qty,redeemed_qty,"
                "valid_from,valid_to,status,created_at) VALUES (?,?,?,?,?,0,?,?,'ACTIVE',?)",
                (bid, event_id, code, title, total_qty, valid_from, valid_to, utcnow()),
            )
            self.append("benefit", bid, "BENEFIT_PUBLISHED",
                        {"benefit_id": bid, "event_id": event_id, "code": code, "title": title,
                         "total_qty": total_qty, "valid_from": valid_from, "valid_to": valid_to,
                         "at": utcnow()}, actor, event_id=event_id)
            return self.get_benefit(bid)

    def _invalidate_benefit_row(self, brow: sqlite3.Row, reason: str, actor: str,
                                causation: str | None = None) -> dict:
        self._conn.execute(
            "UPDATE official_benefits SET status='INVALIDATED', invalidated_reason=? WHERE id=?",
            (reason, brow["id"]),
        )
        self.append("benefit", brow["id"], "BENEFIT_INVALIDATED",
                    {"benefit_id": brow["id"], "event_id": brow["event_id"], "code": brow["code"],
                     "reason": reason, "causation": causation, "at": utcnow()},
                    actor, event_id=brow["event_id"])
        self._raise_alert(brow["event_id"], "BENEFIT_INVALIDATION", f"benefit:{brow['id']}",
                          "HIGH", f"官方权益 {brow['code']}（{brow['title']}）已失效：{reason}",
                          {"benefit_id": brow["id"], "code": brow["code"], "reason": reason,
                           "causation": causation}, actor)
        return self.get_benefit(brow["id"])

    def invalidate_benefit(self, benefit_id: str, reason: str, actor: str) -> dict:
        with self._lock:
            brow = self._one("SELECT * FROM official_benefits WHERE id=?", (benefit_id,))
            if brow["status"] == "INVALIDATED":
                raise GuardError("ALREADY_INVALID", "权益已处于失效状态", 409)
            return self._invalidate_benefit_row(brow, reason, actor)

    def get_benefit(self, benefit_id: str) -> dict:
        row = self._one("SELECT * FROM official_benefits WHERE id=?", (benefit_id,))
        out = dict(row)
        out["remaining_qty"] = out["total_qty"] - out["redeemed_qty"]
        return out

    # ------------------------------------------------------------ orders
    def create_order(self, event_id: str, merchant_id: str, order_channel: str,
                     final_amount_cents: int, actor: str,
                     commitment_id: str | None = None, session_id: str | None = None,
                     idem_key: str | None = None, at: str | None = None) -> dict:
        with self._lock:
            existing = self._idem_row("orders", idem_key)
            if existing is not None:
                return self.get_order(existing)
            self._one("SELECT id FROM sporting_events WHERE id=?", (event_id,))
            m = self._one("SELECT * FROM merchants WHERE id=?", (merchant_id,))
            if m["status"] != "ACTIVE":
                raise GuardError("MERCHANT_SUSPENDED", "商户已停业，不能接单")
            at = at or utcnow()
            policy = self.active_policy(at)
            base = final_amount_cents
            if commitment_id is not None:
                com = self._one("SELECT * FROM commitments WHERE id=?", (commitment_id,))
                if com["merchant_id"] != merchant_id or com["event_id"] != event_id:
                    raise GuardError("COMMITMENT_MISMATCH", "承诺与商户/赛事不一致")
                base = com["price_cents"]
            oid = new_id("ord")
            try:
                self._conn.execute(
                    "INSERT INTO orders(id,event_id,session_id,merchant_id,commitment_id,order_channel,"
                    "base_amount_cents,final_amount_cents,currency,status,policy_version,idem_key,created_at) "
                    "VALUES (?,?,?,?,?,?,?,?,'CNY','BOOKED',?,?,?)",
                    (oid, event_id, session_id, merchant_id, commitment_id, order_channel,
                     base, final_amount_cents, policy["version"], idem_key, at),
                )
            except sqlite3.IntegrityError:
                # 并发同键：另一线程已插入，按重放处理，不重复建单
                existing_id = self._idem_row("orders", idem_key)
                if existing_id:
                    return self.get_order(existing_id)
                raise
            self.append("order", oid, "ORDER_PLACED",
                        {"order_id": oid, "event_id": event_id, "merchant_id": merchant_id,
                         "commitment_id": commitment_id, "channel": order_channel,
                         "final_amount_cents": final_amount_cents,
                         "policy_version": policy["version"], "at": at},
                        actor, event_id=event_id, policy_version=policy["version"])
            return self.get_order(oid)

    def _idem_row(self, table: str, idem_key: str | None) -> str | None:
        if not idem_key:
            return None
        row = self._conn.execute(
            f"SELECT id FROM {table} WHERE idem_key=?", (idem_key,)
        ).fetchone()
        return row["id"] if row else None

    def reject_order(self, order_id: str, reason: str, actor: str) -> dict:
        with self._lock:
            order = self._one("SELECT * FROM orders WHERE id=?", (order_id,))
            if order["status"] != "BOOKED":
                raise GuardError("ORDER_NOT_OPEN", f"订单当前状态 {order['status']} 不可拒单", 409)
            self._conn.execute(
                "UPDATE orders SET status='REJECTED', reject_reason=? WHERE id=?", (reason, order_id))
            self.append("order", order_id, "ORDER_REJECTED",
                        {"order_id": order_id, "merchant_id": order["merchant_id"],
                         "event_id": order["event_id"], "reason": reason, "at": utcnow()},
                        actor, event_id=order["event_id"])
            self._check_mass_rejection(order["event_id"], order["merchant_id"], actor)
            return self.get_order(order_id)

    def fulfill_order(self, order_id: str, actor: str) -> dict:
        with self._lock:
            order = self._one("SELECT * FROM orders WHERE id=?", (order_id,))
            if order["status"] not in ("BOOKED",):
                raise GuardError("ORDER_NOT_OPEN", f"订单当前状态 {order['status']} 不可核销履约", 409)
            self._conn.execute("UPDATE orders SET status='FULFILLED' WHERE id=?", (order_id,))
            self.append("order", order_id, "ORDER_FULFILLED",
                        {"order_id": order_id, "at": utcnow()}, actor, event_id=order["event_id"])
            return self.get_order(order_id)

    def _check_mass_rejection(self, event_id: str, merchant_id: str, actor: str) -> dict | None:
        rules = json.loads(self.active_policy()["rules"])
        rows = self._conn.execute(
            "SELECT id, status, created_at FROM orders WHERE event_id=? AND merchant_id=? "
            "ORDER BY created_at DESC LIMIT 200",
            (event_id, merchant_id),
        ).fetchall()
        finding = evaluate_mass_rejection([dict(r) for r in rows], rules, utcnow())
        if finding is None:
            return None
        # 同一主体存在未关闭的同类预警时不重复告警
        dup = self._conn.execute(
            "SELECT id FROM alerts WHERE event_id=? AND type='MASS_REJECTION' "
            "AND subject=? AND status='OPEN'",
            (event_id, f"merchant:{merchant_id}"),
        ).fetchone()
        if dup is not None:
            return None
        return self._raise_alert(event_id, "MASS_REJECTION", f"merchant:{merchant_id}",
                                 finding["severity"], finding["reason"], finding["evidence"], actor)

    def get_order(self, order_id: str) -> dict:
        return dict(self._one("SELECT * FROM orders WHERE id=?", (order_id,)))

    # ------------------------------------------------------------ redemption
    def redeem(self, *, actor: str, channel: str, commitment_id: str | None = None,
               benefit_id: str | None = None, order_id: str | None = None,
               code: str | None = None, idem_key: str | None = None,
               at: str | None = None) -> dict:
        """并发安全核销：超量、失效、过期均拒绝，且不增加计数。"""
        if not (commitment_id or benefit_id):
            raise GuardError("BAD_REDEMPTION", "必须指定 commitment_id 或 benefit_id")
        with self._lock:
            if idem_key:
                row = self._conn.execute(
                    "SELECT * FROM redemptions WHERE idem_key=?", (idem_key,)).fetchone()
                if row is not None:
                    return dict(row) | {"replayed": True}
            at = at or utcnow()
            target_type = "commitment" if commitment_id else "benefit"
            target_id = commitment_id or benefit_id
            result, reason, event_id = self._check_redeemable(
                target_type, target_id, at, order_id)
            rid = new_id("red")
            self._conn.execute(
                "INSERT INTO redemptions(id,commitment_id,benefit_id,order_id,code,channel,result,reason,"
                "idem_key,created_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
                (rid, commitment_id, benefit_id, order_id, code, channel, result, reason, idem_key, at),
            )
            if result == "SUCCESS":
                if commitment_id:
                    self._conn.execute(
                        "UPDATE commitments SET redeemed_qty = redeemed_qty + 1 WHERE id=?",
                        (commitment_id,))
                else:
                    self._conn.execute(
                        "UPDATE official_benefits SET redeemed_qty = redeemed_qty + 1 WHERE id=?",
                        (benefit_id,))
            self.append(target_type, target_id,
                        "BENEFIT_REDEEMED" if result == "SUCCESS" else "REDEMPTION_REJECTED",
                        {"redemption_id": rid, f"{target_type}_id": target_id, "order_id": order_id,
                         "code": code, "channel": channel, "result": result, "reason": reason, "at": at},
                        actor, event_id=event_id)
            return self.get_redemption(rid) | {"replayed": False}

    def _check_redeemable(self, kind: str, target_id: str, at: str,
                          order_id: str | None) -> tuple[str, str | None, str | None]:
        if kind == "commitment":
            row = self._one("SELECT * FROM commitments WHERE id=?", (target_id,))
            eid = row["event_id"]
            if row["status"] != "ACTIVE":
                return "REJECTED", "COMMITMENT_REVOKED", eid
            if at < row["valid_from"] or at > row["valid_to"]:
                return "REJECTED", "OUTSIDE_VALID_WINDOW", eid
            if row["redeemed_qty"] >= row["fulfill_qty"]:
                return "REJECTED", "SOLD_OUT", eid
        else:
            row = self._one("SELECT * FROM official_benefits WHERE id=?", (target_id,))
            eid = row["event_id"]
            if row["status"] != "ACTIVE":
                return "REJECTED", "BENEFIT_INVALIDATED", eid
            if at < row["valid_from"] or at > row["valid_to"]:
                return "REJECTED", "OUTSIDE_VALID_WINDOW", eid
            if row["redeemed_qty"] >= row["total_qty"]:
                return "REJECTED", "SOLD_OUT", eid
        if order_id is not None:
            self._one("SELECT id FROM orders WHERE id=?", (order_id,))
        return "SUCCESS", None, eid

    def get_redemption(self, redemption_id: str) -> dict:
        return dict(self._one("SELECT * FROM redemptions WHERE id=?", (redemption_id,)))

    # ------------------------------------------------------------ early closure
    def report_early_closure(self, event_id: str, session_id: str | None, venue_name: str,
                             scheduled_close_at: str, actual_close_at: str, actor: str) -> dict:
        with self._lock:
            self._one("SELECT id FROM sporting_events WHERE id=?", (event_id,))
            rules = json.loads(self.active_policy()["rules"])
            finding = evaluate_early_closure(scheduled_close_at, actual_close_at, rules)
            early_minutes = minutes_between(actual_close_at, scheduled_close_at)
            rid_seq = self.append("event", event_id, "EARLY_CLOSURE_REPORTED",
                                  {"event_id": event_id, "session_id": session_id, "venue": venue_name,
                                   "scheduled_close_at": scheduled_close_at,
                                   "actual_close_at": actual_close_at,
                                   "early_minutes": early_minutes,
                                   "within_grace": finding is None, "at": utcnow()},
                                  actor, event_id=event_id)
            alert = None
            if finding is not None:
                evidence = dict(finding["evidence"])
                evidence.update({"session_id": session_id, "venue": venue_name,
                                 "ledger_seq": rid_seq})
                alert = self._raise_alert(event_id, "EARLY_CLOSURE", f"venue:{venue_name}",
                                          finding["severity"], finding["reason"], evidence, actor)
            return {"early_minutes": early_minutes, "within_grace": finding is None, "alert": alert}

    # ------------------------------------------------------------ alerts
    def _raise_alert(self, event_id: str, type_: str, subject: str, severity: str,
                     reason: str, evidence: dict, actor: str) -> dict:
        cur = self._conn.execute(
            "INSERT INTO alerts(event_id,type,subject,severity,reason,evidence,status,created_at) "
            "VALUES (?,?,?,?,?,?,'OPEN',?)",
            (event_id, type_, subject, severity, reason,
             json.dumps(evidence, ensure_ascii=False), utcnow()),
        )
        alert_id = int(cur.lastrowid)
        self.append("alert", str(alert_id), "ALERT_RAISED",
                    {"alert_id": alert_id, "event_id": event_id, "type": type_, "subject": subject,
                     "severity": severity, "reason": reason, "evidence": evidence, "at": utcnow()},
                    actor, event_id=event_id)
        return self.get_alert(alert_id)

    def acknowledge_alert(self, alert_id: int, actor: str) -> dict:
        with self._lock:
            self._one("SELECT id FROM alerts WHERE id=?", (alert_id,))
            self._conn.execute(
                "UPDATE alerts SET status='ACK', acknowledged_at=?, acknowledged_by=? WHERE id=?",
                (utcnow(), actor, alert_id))
            self.append("alert", str(alert_id), "ALERT_ACKNOWLEDGED",
                        {"alert_id": alert_id, "at": utcnow()}, actor)
            return self.get_alert(alert_id)

    def close_alert(self, alert_id: int, actor: str) -> dict:
        with self._lock:
            self._one("SELECT id FROM alerts WHERE id=?", (alert_id,))
            self._conn.execute("UPDATE alerts SET status='CLOSED' WHERE id=?", (alert_id,))
            self.append("alert", str(alert_id), "ALERT_CLOSED",
                        {"alert_id": alert_id, "at": utcnow()}, actor)
            return self.get_alert(alert_id)

    def get_alert(self, alert_id: int) -> dict:
        row = self._one("SELECT * FROM alerts WHERE id=?", (alert_id,))
        out = dict(row)
        out["evidence"] = json.loads(out["evidence"])
        return out

    def list_alerts(self, event_id: str | None = None, status: str | None = None) -> list[dict]:
        sql, params = "SELECT * FROM alerts", []
        where = []
        if event_id:
            where.append("event_id = ?"); params.append(event_id)
        if status:
            where.append("status = ?"); params.append(status)
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += " ORDER BY id DESC"
        return [self.get_alert(r["id"]) for r in self._conn.execute(sql, params).fetchall()]

    # ------------------------------------------------------------ complaints
    def open_complaint(self, event_id: str, category: str, reason: str, channel: str,
                       business_key: str, actor: str, reporter_ref: str | None = None,
                       order_id: str | None = None, idem_key: str | None = None) -> dict:
        with self._lock:
            if idem_key:
                row = self._conn.execute(
                    "SELECT * FROM complaints WHERE idem_key=?", (idem_key,)).fetchone()
                if row is not None:
                    # 同键重传不另立案件
                    return dict(row) | {"replayed": True, "duplicate": True}
            if category not in CATEGORY_DEPT:
                raise GuardError("BAD_CATEGORY", f"category 必须为 {sorted(CATEGORY_DEPT)}")
            # 多渠道重传统一去重：同一业务键只立一案
            dup = self._conn.execute(
                "SELECT * FROM complaints WHERE business_key=? ORDER BY created_at LIMIT 1",
                (business_key,),
            ).fetchone()
            if dup is not None:
                self.append("complaint", dup["id"], "COMPLAINT_DEDUP_HIT",
                            {"complaint_id": dup["id"], "incoming_channel": channel,
                             "business_key": business_key, "at": utcnow()}, actor,
                            event_id=event_id)
                return dict(dup) | {"replayed": False, "duplicate": True}
            self._one("SELECT id FROM sporting_events WHERE id=?", (event_id,))
            if order_id is not None:
                self._one("SELECT id FROM orders WHERE id=?", (order_id,))
            cid = new_id("cpl")
            dept = CATEGORY_DEPT[category]
            at = utcnow()
            self._conn.execute(
                "INSERT INTO complaints(id,event_id,order_id,channel,business_key,reporter_ref,category,"
                "reason,status,assigned_dept,idem_key,created_at) "
                "VALUES (?,?,?,?,?,?,?,?,'OPEN',?,?,?)",
                (cid, event_id, order_id, channel, business_key, reporter_ref, category,
                 reason, dept, idem_key, at),
            )
            self.append("complaint", cid, "COMPLAINT_OPENED",
                        {"complaint_id": cid, "event_id": event_id, "order_id": order_id,
                         "channel": channel, "business_key": business_key, "category": category,
                         "reason": reason, "assigned_dept": dept, "at": at},
                        actor, event_id=event_id)
            return self.get_complaint(cid) | {"replayed": False, "duplicate": False}

    def handle_complaint(self, complaint_id: str, action: str, actor: str,
                         to_dept: str | None = None, note: str | None = None,
                         officer_dept: str | None = None) -> dict:
        with self._lock:
            c = self._one("SELECT * FROM complaints WHERE id=?", (complaint_id,))
            if officer_dept is not None and officer_dept != c["assigned_dept"]:
                raise GuardError(
                    "OUT_OF_JURISDICTION",
                    f"案件 {complaint_id} 当前归属 {c['assigned_dept']}，"
                    f"需先转交或由该部门处置（当前 {officer_dept}）", 403)
            from_dept = c["assigned_dept"]
            new_status, new_dept = c["status"], from_dept
            if action == "INVESTIGATE":
                new_status = "INVESTIGATING"
            elif action == "TRANSFER":
                if to_dept not in DEPARTMENTS:
                    raise GuardError("BAD_DEPT", f"to_dept 必须为 {DEPARTMENTS}")
                new_dept = to_dept
                new_status = "INVESTIGATING" if c["status"] == "OPEN" else c["status"]
            elif action == "APPEAL":
                if c["status"] not in ("RESOLVED", "DISMISSED"):
                    raise GuardError("NOT_RESOLVED", "仅已结案件可申诉", 409)
                new_status = "APPEALED"
            elif action == "RESOLVE":
                if not note:
                    raise GuardError("RESOLUTION_REQUIRED", "结案必须填写 resolution 说明")
                new_status = "RESOLVED"
            elif action == "DISMISS":
                new_status = "DISMISSED"
            elif action == "REOPEN":
                if c["status"] not in ("RESOLVED", "DISMISSED"):
                    raise GuardError("NOT_CLOSED", "仅已结案件可重开", 409)
                new_status = "INVESTIGATING"
            else:
                raise GuardError("BAD_ACTION", "不支持的处置动作")
            self._conn.execute(
                "UPDATE complaints SET status=?, assigned_dept=?, "
                "resolved_at=CASE WHEN ? IN ('RESOLVED','DISMISSED') THEN ? ELSE resolved_at END, "
                "resolution=CASE WHEN ?='RESOLVED' THEN ? ELSE resolution END WHERE id=?",
                (new_status, new_dept, new_status, utcnow(), new_status, note, complaint_id),
            )
            self._conn.execute(
                "INSERT INTO complaint_handlings(complaint_id,action,from_dept,to_dept,note,acted_by,acted_at) "
                "VALUES (?,?,?,?,?,?,?)",
                (complaint_id, action, from_dept, to_dept if action == "TRANSFER" else None,
                 note, actor, utcnow()),
            )
            self.append("complaint", complaint_id, "COMPLAINT_HANDLED",
                        {"complaint_id": complaint_id, "action": action, "from_dept": from_dept,
                         "to_dept": to_dept, "note": note, "new_status": new_status, "at": utcnow()},
                        actor, event_id=c["event_id"])
            return self.get_complaint(complaint_id)

    def get_complaint(self, complaint_id: str) -> dict:
        c = dict(self._one("SELECT * FROM complaints WHERE id=?", (complaint_id,)))
        c["handlings"] = [dict(r) for r in self._conn.execute(
            "SELECT action,from_dept,to_dept,note,acted_by,acted_at FROM complaint_handlings "
            "WHERE complaint_id=? ORDER BY id", (complaint_id,)).fetchall()]
        c["remedies"] = [dict(r) for r in self._conn.execute(
            "SELECT id,kind,amount_cents,status,bearer_type,bearer_id,bearer_reason,policy_version "
            "FROM remedies WHERE complaint_id=? ORDER BY created_at", (complaint_id,)).fetchall()]
        return c

    def list_complaints(self, event_id: str | None = None, status: str | None = None) -> list[dict]:
        sql, params = "SELECT id FROM complaints", []
        where = []
        if event_id:
            where.append("event_id=?"); params.append(event_id)
        if status:
            where.append("status=?"); params.append(status)
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += " ORDER BY created_at"
        return [self.get_complaint(r["id"]) for r in self._conn.execute(sql, params).fetchall()]

    # ------------------------------------------------------------ remedies
    def propose_remedy(self, *, actor: str, kind: str, amount_cents: int,
                       complaint_id: str | None = None, order_id: str | None = None,
                       category: str | None = None, idem_key: str | None = None,
                       officer_dept: str | None = None) -> dict:
        with self._lock:
            if idem_key:
                row = self._conn.execute("SELECT * FROM remedies WHERE idem_key=?", (idem_key,)).fetchone()
                if row is not None:
                    return dict(row) | {"replayed": True}
            if kind not in ("REFUND", "COMPENSATION", "FEE_WAIVER"):
                raise GuardError("BAD_KIND", "kind 必须为 REFUND/COMPENSATION/FEE_WAIVER")
            if amount_cents < 0:
                raise GuardError("BAD_AMOUNT", "金额不能为负")
            complaint = None
            if complaint_id is not None:
                complaint = self._one("SELECT * FROM complaints WHERE id=?", (complaint_id,))
                if complaint["status"] not in ("INVESTIGATING", "APPEALED"):
                    raise GuardError("COMPLAINT_NOT_INVESTIGATING",
                                     f"案件状态 {complaint['status']}，须先受理调查", 409)
                if officer_dept is not None and officer_dept != complaint["assigned_dept"]:
                    raise GuardError(
                        "OUT_OF_JURISDICTION",
                        f"案件归属 {complaint['assigned_dept']}，当前部门 {officer_dept} 不得发起处置", 403)
            if order_id is None:
                if complaint is None:
                    raise GuardError("BAD_REMEDY", "处置必须关联订单或投诉")
                order_id = complaint["order_id"]
            if order_id is None:
                raise GuardError("BAD_REMEDY", "处置必须能定位到订单")
            order = self._one("SELECT * FROM orders WHERE id=?", (order_id,))
            cat = category or (complaint["category"] if complaint else "OTHER")
            # 同一订单同一处置类型已存在有效（已提议/已核准/已支付）记录 => 拒绝重复赔付
            existing = self._conn.execute(
                "SELECT * FROM remedies WHERE order_id=? AND kind=? AND status != 'REVERSED' "
                "ORDER BY created_at LIMIT 1",
                (order_id, kind),
            ).fetchone()
            if existing is not None:
                raise GuardError(
                    "DUPLICATE_REMEDY",
                    f"订单 {order_id} 的 {kind} 已存在（{existing['status']}：{existing['id']}），不得重复赔付",
                    409)
            rules = json.loads(
                self._conn.execute("SELECT rules FROM policies WHERE version=?",
                                   (order["policy_version"],)).fetchone()["rules"])
            bearer_type, bearer_id, reason = bearer_for(cat, kind, rules, merchant_id=order["merchant_id"])
            rid = new_id("rem")
            at = utcnow()
            self._conn.execute(
                "INSERT INTO remedies(id,complaint_id,order_id,event_id,kind,amount_cents,currency,status,"
                "bearer_type,bearer_id,bearer_reason,policy_version,category,idem_key,proposed_by,"
                "created_at) VALUES (?,?,?,?,?,?,'CNY','PROPOSED',?,?,?,?,?,?,?,?)",
                (rid, complaint_id, order_id, order["event_id"], kind, amount_cents,
                 bearer_type, bearer_id, reason, order["policy_version"], cat, idem_key, actor, at),
            )
            self.append("remedy", rid, "REMEDY_PROPOSED",
                        {"remedy_id": rid, "complaint_id": complaint_id, "order_id": order_id,
                         "event_id": order["event_id"], "kind": kind, "amount_cents": amount_cents,
                         "bearer_type": bearer_type, "bearer_id": bearer_id, "bearer_reason": reason,
                         "policy_version": order["policy_version"], "category": cat, "at": at},
                        actor, event_id=order["event_id"], policy_version=order["policy_version"])
            return self.get_remedy(rid) | {"replayed": False}

    def _transition_remedy(self, remedy_id: str, target: str, event_type: str, actor: str) -> dict:
        r = self._one("SELECT * FROM remedies WHERE id=?", (remedy_id,))
        allowed = {"APPROVED": ("PROPOSED",), "PAID": ("APPROVED",),
                   "REVERSED": ("PAID",)}[target]
        if r["status"] not in allowed:
            raise GuardError("BAD_TRANSITION", f"处置状态 {r['status']} 不能流转为 {target}", 409)
        if target == "APPROVED":
            self._conn.execute(
                "UPDATE remedies SET status='APPROVED', approved_by=?, approved_at=? WHERE id=?",
                (actor, utcnow(), remedy_id))
        elif target == "PAID":
            self._conn.execute(
                "UPDATE remedies SET status='PAID', paid_at=? WHERE id=?", (utcnow(), remedy_id))
        else:
            self._conn.execute("UPDATE remedies SET status='REVERSED' WHERE id=?", (remedy_id,))
        self.append("remedy", remedy_id, event_type,
                    {"remedy_id": remedy_id, "from_status": r["status"], "to_status": target,
                     "bearer_type": r["bearer_type"], "bearer_id": r["bearer_id"], "at": utcnow()},
                    actor, event_id=r["event_id"])
        return self.get_remedy(remedy_id)

    def approve_remedy(self, remedy_id: str, actor: str) -> dict:
        with self._lock:
            return self._transition_remedy(remedy_id, "APPROVED", "REMEDY_APPROVED", actor)

    def pay_remedy(self, remedy_id: str, actor: str, idem_key: str | None = None) -> dict:
        with self._lock:
            if idem_key:
                row = self._conn.execute(
                    "SELECT * FROM remedies WHERE id=? AND status='PAID'", (remedy_id,)).fetchone()
                if row is not None:
                    return dict(row) | {"replayed": True}
            return self._transition_remedy(remedy_id, "PAID", "REMEDY_SETTLED",
                                           actor) | {"replayed": False}

    def reverse_remedy(self, remedy_id: str, actor: str) -> dict:
        with self._lock:
            return self._transition_remedy(remedy_id, "REVERSED", "REMEDY_REVERSED", actor)

    def get_remedy(self, remedy_id: str) -> dict:
        return dict(self._one("SELECT * FROM remedies WHERE id=?", (remedy_id,)))

    def remedy_trace(self, remedy_id: str) -> dict:
        """按处置还原完整事件链，明确最终承担方。"""
        with self._lock:
            r = self.get_remedy(remedy_id)
            chain = [dict(x) for x in self._conn.execute(
                "SELECT seq,aggregate_type,aggregate_id,event_type,payload,actor,policy_version,occurred_at "
                "FROM ledger WHERE aggregate_type='remedy' AND aggregate_id=? ORDER BY seq",
                (remedy_id,)).fetchall()]
            for x in chain:
                x["payload"] = json.loads(x["payload"])
            complaint = self.get_complaint(r["complaint_id"]) if r["complaint_id"] else None
            order = self.get_order(r["order_id"]) if r["order_id"] else None
            return {
                "remedy": r,
                "final_bearer": {"type": r["bearer_type"], "id": r["bearer_id"],
                                 "reason": r["bearer_reason"], "policy_version": r["policy_version"]},
                "complaint": complaint,
                "order": order,
                "event_chain": chain,
            }

    # ------------------------------------------------------------ read models
    def ledger_for(self, aggregate_type: str, aggregate_id: str) -> list[dict]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT seq,event_id,aggregate_type,aggregate_id,event_type,payload,actor,"
                "policy_version,causation_id,occurred_at FROM ledger "
                "WHERE aggregate_type=? AND aggregate_id=? ORDER BY seq",
                (aggregate_type, aggregate_id),
            ).fetchall()
            out = []
            for r in rows:
                d = dict(r)
                d["payload"] = json.loads(d["payload"])
                out.append(d)
            return out

    def event_timeline(self, event_id: str) -> list[dict]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT seq,aggregate_type,aggregate_id,event_type,payload,actor,occurred_at "
                "FROM ledger WHERE event_id=? ORDER BY seq", (event_id,)).fetchall()
            out = []
            for r in rows:
                d = dict(r)
                d["payload"] = json.loads(d["payload"])
                out.append(d)
            return out

    def event_impact(self, event_id: str) -> dict:
        with self._lock:
            self._one("SELECT id FROM sporting_events WHERE id=?", (event_id,))
            sessions = self.list_event_sessions(event_id)
            commitments = [dict(r) for r in self._conn.execute(
                "SELECT id,status,fulfill_qty,redeemed_qty,price_cents FROM commitments "
                "WHERE event_id=?", (event_id,)).fetchall()]
            benefits = [dict(r) for r in self._conn.execute(
                "SELECT id,status,total_qty,redeemed_qty FROM official_benefits WHERE event_id=?",
                (event_id,)).fetchall()]
            orders = [dict(r) for r in self._conn.execute(
                "SELECT status, final_amount_cents FROM orders WHERE event_id=?", (event_id,)).fetchall()]
            complaints = [dict(r) for r in self._conn.execute(
                "SELECT status, category FROM complaints WHERE event_id=?", (event_id,)).fetchall()]
            remedies = [dict(r) for r in self._conn.execute(
                "SELECT status,kind,amount_cents,bearer_type FROM remedies WHERE event_id=?",
                (event_id,)).fetchall()]
            alerts = [dict(r) for r in self._conn.execute(
                "SELECT type,severity,status FROM alerts WHERE event_id=?", (event_id,)).fetchall()]
            merchants = [dict(r) for r in self._conn.execute(
                "SELECT DISTINCT m.id, m.status, m.category FROM merchants m "
                "JOIN commitments c ON c.merchant_id=m.id WHERE c.event_id=?", (event_id,)).fetchall()]

            def bucket(rows: list[dict], key: str) -> dict:
                out: dict[str, int] = {}
                for r in rows:
                    out[r[key]] = out.get(r[key], 0) + 1
                return out

            paid = [r for r in remedies if r["status"] == "PAID"]
            return {
                "event_id": event_id,
                "sessions": sessions,
                "merchants": {"total": len(merchants), "by_category": bucket(merchants, "category"),
                              "suspended": sum(1 for m in merchants if m["status"] == "SUSPENDED")},
                "commitments": {
                    "total": len(commitments),
                    "active": sum(1 for c in commitments if c["status"] == "ACTIVE"),
                    "revoked": sum(1 for c in commitments if c["status"] == "REVOKED"),
                    "promised_qty": sum(c["fulfill_qty"] for c in commitments),
                    "redeemed_qty": sum(c["redeemed_qty"] for c in commitments),
                },
                "benefits": {
                    "total": len(benefits),
                    "invalidated": sum(1 for b in benefits if b["status"] == "INVALIDATED"),
                    "total_qty": sum(b["total_qty"] for b in benefits),
                    "redeemed_qty": sum(b["redeemed_qty"] for b in benefits),
                },
                "orders": {"total": len(orders), "by_status": bucket(orders, "status"),
                           "gmv_cents": sum(o["final_amount_cents"] for o in orders)},
                "complaints": {"total": len(complaints),
                               "by_status": bucket(complaints, "status"),
                               "by_category": bucket(complaints, "category")},
                "remedies": {"total": len(remedies), "by_status": bucket(remedies, "status"),
                             "paid_cents": sum(r["amount_cents"] for r in paid),
                             "paid_by_bearer_type": {
                                 k: sum(x["amount_cents"] for x in paid if x["bearer_type"] == k)
                                 for k in {x["bearer_type"] for x in paid}}},
                "alerts": {"total": len(alerts), "by_type": bucket(alerts, "type"),
                           "open": sum(1 for a in alerts if a["status"] == "OPEN"),
                           "by_severity": bucket(alerts, "severity")},
                "timeline_entries": self._conn.execute(
                    "SELECT COUNT(*) AS n FROM ledger WHERE event_id=?", (event_id,)).fetchone()["n"],
            }

    # ------------------------------------------------------------ idempotency
    def lookup_idem(self, scope: str, idem_key: str) -> sqlite3.Row | None:
        with self._lock:
            return self._conn.execute(
                "SELECT * FROM idempotency WHERE scope=? AND idem_key=?", (scope, idem_key)).fetchone()

    def store_idem(self, scope: str, idem_key: str, request_hash: str,
                   response_code: int, response_body: str) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT OR IGNORE INTO idempotency(scope,idem_key,request_hash,response_code,response_body,created_at) "
                "VALUES (?,?,?,?,?,?)",
                (scope, idem_key, request_hash, response_code, response_body, utcnow()))

    def close(self) -> None:
        self._conn.close()


def _policy_dict(row: sqlite3.Row) -> dict:
    out = dict(row)
    out["rules"] = json.loads(out["rules"])
    return out
