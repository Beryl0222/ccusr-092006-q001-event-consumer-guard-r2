"""联防服务层：全部业务用例。

设计要点：
- 每个写用例在单个 db.transaction() 内完成状态变更 + 事件链记录；
- 跨渠道重传通过 (channel, channel_ref) 唯一键与 dedup_key 唯一索引双重去重；
- 核销用条件 UPDATE ... WHERE redeemed_total + ? <= promised_total 原子扣减；
- 处置（退款/补偿）对 (complaint_id, kind) 有 settled 唯一索引，杜绝重复赔付；
- 政策版本按交易时点适用，政策变更只影响生效后的交易；
- 所有关键动作写入 event_chain，可按订单/投诉/赛事完整还原。
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime
from typing import Any

from . import domain
from .db import Database, now_iso, to_iso


def _uid(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:12]}"


def _row_to_dict(row) -> dict:
    return dict(row) if row is not None else None


def _parse_json_fields(row: dict, fields: tuple[str, ...]) -> dict:
    for f in fields:
        if f in row and isinstance(row[f], str):
            row[f] = json.loads(row[f])
    return row


class GuardService:
    def __init__(self, db: Database, clock=None):
        self.db = db
        # clock: 可注入的无参时钟（返回东八区 ISO 字符串），默认真实时钟；
        # 测试注入固定时钟以复现“有效期/政策时点/拒单窗口”等时间规则。
        self._clock = clock or now_iso

    def _now(self) -> str:
        return self._clock()

    # ======================================================================
    # 鉴权
    # ======================================================================

    def authenticate(self, token: str | None) -> dict:
        if not token:
            raise domain.AuthError()
        row = self.db.query_one(
            "SELECT token, role, subject_id, label FROM api_tokens WHERE token=?",
            (token,),
        )
        if row is None:
            raise domain.AuthError()
        return dict(row)

    def _auth(self, token: str | None, action: str) -> dict:
        caller = self.authenticate(token)
        domain.require_permission(caller["role"], action)
        return caller

    def _chain(self, con, *, scope: str, subject_ref: str, action: str,
               actor_role: str | None, detail: dict | None = None,
               event_id: str | None = None, causation_seq: int | None = None) -> int:
        cur = con.execute(
            "INSERT INTO event_chain(event_id, scope, subject_ref, action, actor_role,"
        " detail_json, causation_seq, created_at) VALUES (?,?,?,?,?,?,?,?)",
            (event_id, scope, subject_ref, action, actor_role,
             json.dumps(detail or {}, ensure_ascii=False), causation_seq, self._now()),
        )
        return cur.lastrowid

    def _get(self, table: str, key_col: str, key: str) -> dict:
        row = self.db.query_one(
            f"SELECT * FROM {table} WHERE {key_col}=?", (key,)
        )
        if row is None:
            raise domain.NotFound(table, key)
        return dict(row)

    # ======================================================================
    # 主办方：赛事登记 / 改期 / 取消
    # ======================================================================

    def register_event(self, token: str, data: dict) -> dict:
        caller = self._auth(token, "event.register")
        required = ("event_id", "name", "starts_at", "ends_at")
        missing = [k for k in required if not data.get(k)]
        if missing:
            raise domain.GuardError(f"缺少必填字段: {', '.join(missing)}")
        starts, ends = to_iso(data["starts_at"]), to_iso(data["ends_at"])
        if ends <= starts:
            raise domain.GuardError("ends_at 必须晚于 starts_at")
        event_id = data["event_id"]
        if self.db.query_one("SELECT 1 FROM events WHERE event_id=?", (event_id,)):
            raise domain.Conflict(f"赛事 {event_id} 已登记", code="event_exists")
        ts = self._now()
        with self.db.transaction() as con:
            con.execute(
                "INSERT INTO events(event_id, name, organizer_id, sector, venue,"
                " starts_at, ends_at, expected_attendance, status, weather_level,"
                " detail_json, created_at, updated_at)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (event_id, data["name"], caller["subject_id"],
                 data.get("sector"), data.get("venue"), starts, ends,
                 int(data.get("expected_attendance", 0)),
                 domain.EventStatus.SCHEDULED, domain.WeatherLevel.NORMAL,
                 json.dumps(data.get("detail", {}), ensure_ascii=False), ts, ts),
            )
            self._chain(con, scope="event", subject_ref=event_id,
                        action="event.registered", actor_role=caller["role"],
                        detail={"expected_attendance": data.get("expected_attendance", 0),
                                "starts_at": starts, "ends_at": ends},
                        event_id=event_id)
        return self.get_event(event_id)

    def get_event(self, event_id: str) -> dict:
        ev = _parse_json_fields(self._get("events", "event_id", event_id), ("detail_json",))
        ev["detail"] = ev.pop("detail_json")
        return ev

    def register_merchant(self, token: str, data: dict) -> dict:
        caller = self._auth(token, "merchant.register")
        for k in ("merchant_id", "name", "sector"):
            if not data.get(k):
                raise domain.GuardError(f"缺少必填字段: {k}")
        if data["sector"] not in [s.value for s in domain.MerchantSector]:
            raise domain.GuardError("sector 取值非法")
        ts = self._now()
        with self.db.transaction() as con:
            con.execute(
                "INSERT INTO merchants(merchant_id, name, sector, contact, status, created_at)"
                " VALUES (?,?,?,?,'active',?)",
                (data["merchant_id"], data["name"], data["sector"],
                 data.get("contact"), ts),
            )
            self._chain(con, scope="merchant", subject_ref=data["merchant_id"],
                        action="merchant.registered", actor_role=caller["role"],
                        detail={"sector": data["sector"], "name": data["name"]})
        return self.get_merchant(data["merchant_id"])

    def get_merchant(self, merchant_id: str) -> dict:
        return self._get("merchants", "merchant_id", merchant_id)

    def publish_benefit(self, token: str, data: dict) -> dict:
        """主办方发布官方权益（总量、有效期）。"""
        caller = self._auth(token, "benefit.publish")
        for k in ("benefit_id", "event_id", "code", "title", "quota",
                  "valid_from", "valid_to"):
            if data.get(k) is None:
                raise domain.GuardError(f"缺少必填字段: {k}")
        event = self._get("events", "event_id", data["event_id"])
        vf, vt = to_iso(data["valid_from"]), to_iso(data["valid_to"])
        if vt <= vf:
            raise domain.GuardError("valid_to 必须晚于 valid_from")
        if int(data["quota"]) <= 0:
            raise domain.GuardError("quota 必须为正整数")
        ts = self._now()
        with self.db.transaction() as con:
            try:
                con.execute(
                    "INSERT INTO benefits(benefit_id, event_id, code, title, quota,"
                    " valid_from, valid_to, status, created_at)"
                    " VALUES (?,?,?,?,?,?,?,'active',?)",
                    (data["benefit_id"], data["event_id"], data["code"], data["title"],
                     int(data["quota"]), vf, vt, ts),
                )
            except Exception as exc:  # 唯一码冲突
                raise domain.Conflict("权益码已存在", code="benefit_exists") from exc
            self._chain(con, scope="benefit", subject_ref=data["benefit_id"],
                        action="benefit.published", actor_role=caller["role"],
                        detail={"code": data["code"], "quota": data["quota"],
                                "valid_from": vf, "valid_to": vt},
                        event_id=event["event_id"])
        return self.get_benefit(data["benefit_id"])

    def get_benefit(self, benefit_id: str) -> dict:
        return self._get("benefits", "benefit_id", benefit_id)

    # ======================================================================
    # 商户：带有效期的价格与履约承诺
    # ======================================================================

    def publish_commitment(self, token: str, data: dict) -> dict:
        """商户发布承诺。kind:
        - price: spec={price}（有效期内禁止加价）
        - package/benefit: spec 含套餐/权益码，promised_total 为承诺量
        - venue_hours: spec={open_from, open_to}（第二现场开放时段）
        """
        caller = self._auth(token, "commitment.publish")
        for k in ("commitment_id", "event_id", "kind", "valid_from", "valid_to"):
            if not data.get(k):
                raise domain.GuardError(f"缺少必填字段: {k}")
        if data["kind"] not in [k.value for k in domain.CommitmentKind]:
            raise domain.GuardError("kind 取值非法")
        merchant_id = data.get("merchant_id") or caller["subject_id"]
        if caller["subject_id"] != merchant_id:
            # 商户角色只能以自己的商户号发布承诺，防止跨户挂承诺。
            raise domain.PermissionDenied(caller["role"], "commitment.publish:other_merchant")
        merchant = self.db.query_one(
            "SELECT * FROM merchants WHERE merchant_id=?", (merchant_id,)
        )
        if merchant is None:
            raise domain.NotFound("merchant", merchant_id)
        event = self._get("events", "event_id", data["event_id"])
        vf, vt = to_iso(data["valid_from"]), to_iso(data["valid_to"])
        if vt <= vf:
            raise domain.GuardError("valid_to 必须晚于 valid_from")
        promised = int(data.get("promised_total", 0))
        if data["kind"] in domain.redeemable_kinds() and promised <= 0:
            raise domain.GuardError("套餐/权益类承诺必须提供正整数 promised_total")
        spec = data.get("spec", {})

        # 价格承诺更新：同一赛事+商户的旧价格承诺标记被取代（supersede），
        # 保留历史以便计算价格突变。
        superseded_seq = None
        ts = self._now()
        with self.db.transaction() as con:
            spike_alert = None
            old_price_row = None
            if data["kind"] == domain.CommitmentKind.PRICE:
                old_price_row = con.execute(
                    "SELECT * FROM commitments WHERE event_id=? AND merchant_id=?"
                    " AND kind='price' AND active=1 ORDER BY created_at DESC LIMIT 1",
                    (data["event_id"], merchant_id),
                ).fetchone()
            try:
                cur = con.execute(
                    "INSERT INTO commitments(commitment_id, event_id, merchant_id, kind,"
                    " spec_json, valid_from, valid_to, promised_total, redeemed_total,"
                    " active, created_at) VALUES (?,?,?,?,?,?,?,?,?,1,?)",
                    (data["commitment_id"], data["event_id"], merchant_id, data["kind"],
                     json.dumps(spec, ensure_ascii=False), vf, vt, promised, 0, ts),
                )
            except Exception as exc:
                raise domain.Conflict("承诺 id 已存在", code="commitment_exists") from exc
            if old_price_row is not None:
                con.execute(
                    "UPDATE commitments SET active=0, superseded_by=? WHERE commitment_id=?",
                    (data["commitment_id"], old_price_row["commitment_id"]),
                )
                old_price = float(json.loads(old_price_row["spec_json"]).get("price", 0))
                new_price = float(spec.get("price", 0))
                ratio = (new_price - old_price) / old_price if old_price else 0.0
                # 仅当旧承诺仍在有效期、且涨幅达到突变阈值时预警；
                # 小幅调价只做版本更替（旧承诺标记 superseded），不打扰值班台。
                if (old_price > 0 and new_price > old_price
                        and old_price_row["valid_to"] >= ts
                        and ratio >= domain.PRICE_SPIKE_RATIO):
                    ctx = domain.explain_price_spike(
                        event_id=data["event_id"], merchant_id=merchant_id,
                        sector=merchant["sector"], old_price=old_price,
                        new_price=new_price,
                        commitment_id=old_price_row["commitment_id"], observed_at=ts,
                    )
                    alert_id = _uid("alert")
                    con.execute(
                        "INSERT INTO alerts(alert_id, event_id, subject_id, type, severity,"
                        " title, explanation, evidence_json, related_json, status, created_at)"
                        " VALUES (?,?,?,?,?,?,?,?,?,'open',?)",
                        (alert_id, data["event_id"], merchant_id, ctx.alert_type,
                         ctx.severity, ctx.title, ctx.explanation,
                         json.dumps(ctx.evidence, ensure_ascii=False),
                         json.dumps(ctx.related, ensure_ascii=False), ts),
                    )
                    spike_alert = alert_id
                    self._chain(con, scope="alert", subject_ref=alert_id,
                                action="alert.price_spike", actor_role="system",
                                detail={"title": ctx.title},
                                event_id=data["event_id"])
            seq = self._chain(
                con, scope="commitment", subject_ref=data["commitment_id"],
                action="commitment.published", actor_role=caller["role"],
                detail={"kind": data["kind"], "promised_total": promised,
                        "supersedes": old_price_row["commitment_id"] if old_price_row else None,
                        "price_alert": spike_alert},
                event_id=event["event_id"],
            )
        out = self.get_commitment(data["commitment_id"])
        return out

    def get_commitment(self, commitment_id: str) -> dict:
        row = self._get("commitments", "commitment_id", commitment_id)
        row["spec"] = json.loads(row.pop("spec_json"))
        return row

    # ======================================================================
    # 订单 / 拒单 / 核销
    # ======================================================================

    def create_order(self, token: str, data: dict) -> dict:
        """多渠道下单。同一订单重传（同 channel+channel_ref，或同 dedup_key）
        返回原订单，不产生第二笔。"""
        caller = self._auth(token, "order.create")
        merchant_id = data.get("merchant_id") or caller["subject_id"]
        for k in ("event_id", "channel", "channel_ref", "amount"):
            if data.get(k) is None:
                raise domain.GuardError(f"缺少必填字段: {k}")
        data = {**data, "merchant_id": merchant_id}
        self._get("events", "event_id", data["event_id"])
        merchant = self._get("merchants", "merchant_id", data["merchant_id"])
        if caller["subject_id"] != data["merchant_id"]:
            raise domain.PermissionDenied(caller["role"], "order.create:other_merchant")
        if merchant["status"] == "suspended":
            raise domain.StateError("商户已被停业整顿，暂不可接单")
        commitment_id = data.get("commitment_id")
        if commitment_id:
            com = self._get("commitments", "commitment_id", commitment_id)
            # 预订（赛前下单）只要求承诺有效存在；有效期在核销/履约时点强制。
            if not com["active"]:
                raise domain.StateError("引用的承诺已失效")
        ts = self._now()
        with self.db.transaction() as con:
            existing = self._find_existing_order(con, data)
            if existing:
                self._chain(con, scope="order", subject_ref=existing["order_id"],
                            action="order.replayed", actor_role=caller["role"],
                            detail={"channel": data["channel"],
                                    "channel_ref": data["channel_ref"],
                                    "dedup": existing["_dedup_hit"]},
                            event_id=data["event_id"])
                return self.get_order(existing["order_id"])
            order_id = _uid("ord")
            try:
                con.execute(
                    "INSERT INTO orders(order_id, event_id, merchant_id, commitment_id,"
                    " channel, channel_ref, dedup_key, consumer_ref, amount, status,"
                    " surcharge_flag, created_at)"
                    " VALUES (?,?,?,?,?,?,?,?,?,'created',?,?)",
                    (order_id, data["event_id"], data["merchant_id"], commitment_id,
                     data["channel"], data["channel_ref"], data.get("dedup_key"),
                     data.get("consumer_ref"), float(data["amount"]),
                     1 if data.get("surcharge_flag") else 0, ts),
                )
            except Exception as exc:
                raise domain.Conflict("订单重复提交", code="duplicate_order") from exc
            self._chain(con, scope="order", subject_ref=order_id,
                        action="order.created", actor_role=caller["role"],
                        detail={"amount": data["amount"], "channel": data["channel"],
                                "commitment_id": commitment_id},
                        event_id=data["event_id"])
        return self.get_order(order_id)

    @staticmethod
    def _find_existing_order(con, data: dict) -> dict | None:
        row = con.execute(
            "SELECT * FROM orders WHERE channel=? AND channel_ref=?",
            (data["channel"], data["channel_ref"]),
        ).fetchone()
        if row:
            d = dict(row); d["_dedup_hit"] = "channel_ref"
            return d
        if data.get("dedup_key"):
            row = con.execute(
                "SELECT * FROM orders WHERE dedup_key=?", (data["dedup_key"],)
            ).fetchone()
            if row:
                d = dict(row); d["_dedup_hit"] = "dedup_key"
                return d
        return None

    def get_order(self, order_id: str) -> dict:
        return self._get("orders", "order_id", order_id)

    def reject_order(self, token: str, order_id: str, reason: str) -> dict:
        """商户拒单；窗口内集中拒单触发可解释预警。"""
        caller = self._auth(token, "order.reject")
        ts = self._now()
        with self.db.transaction() as con:
            row = con.execute("SELECT * FROM orders WHERE order_id=?", (order_id,)).fetchone()
            if row is None:
                raise domain.NotFound("order", order_id)
            order = dict(row)
            if caller["subject_id"] != order["merchant_id"]:
                raise domain.PermissionDenied(caller["role"], "order.reject:other_merchant")
            if order["status"] != domain.OrderStatus.CREATED:
                raise domain.StateError(f"订单状态 {order['status']} 不可拒单")
            con.execute(
                "UPDATE orders SET status='rejected', rejected_reason=?, decided_at=?"
                " WHERE order_id=?",
                (reason, ts, order_id),
            )
            self._chain(con, scope="order", subject_ref=order_id,
                        action="order.rejected", actor_role=caller["role"],
                        detail={"reason": reason}, event_id=order["event_id"],
                        causation_seq=self._last_seq(con, "order", order_id))
            alert_id = self._maybe_rejection_alert(con, order["event_id"],
                                                   order["merchant_id"], ts)
        return self.get_order(order_id)

    def _maybe_rejection_alert(self, con, event_id: str, merchant_id: str, ts: str):
        # 所有时间均为东八区归一化 ISO，按同一偏移做字典序比较即可；
        # 截止时间在 Python 中计算，避免 SQLite 时间函数对 'T'/偏移的处理差异。
        from datetime import timedelta
        cutoff = (datetime.fromisoformat(ts)
                  - timedelta(minutes=domain.REJECTION_WINDOW_MINUTES)
                  ).isoformat(timespec="seconds")
        rows = con.execute(
            "SELECT order_id FROM orders WHERE merchant_id=? AND status='rejected'"
            " AND decided_at >= ?",
            (merchant_id, cutoff),
        ).fetchall()
        if len(rows) >= domain.REJECTION_THRESHOLD:
            refs = [r["order_id"] for r in rows]
            ctx = domain.explain_clustered_rejection(
                event_id=event_id, merchant_id=merchant_id, order_refs=refs,
                window_minutes=domain.REJECTION_WINDOW_MINUTES, observed_at=ts,
            )
            alert_id = _uid("alert")
            con.execute(
                "INSERT INTO alerts(alert_id, event_id, subject_id, type, severity,"
                " title, explanation, evidence_json, related_json, status, created_at)"
                " VALUES (?,?,?,?,?,?,?,?,?,'open',?)",
                (alert_id, event_id, merchant_id, ctx.alert_type, ctx.severity,
                 ctx.title, ctx.explanation,
                 json.dumps(ctx.evidence, ensure_ascii=False),
                 json.dumps(ctx.related, ensure_ascii=False), ts),
            )
            self._chain(con, scope="alert", subject_ref=alert_id,
                        action="alert.clustered_rejection", actor_role="system",
                        detail={"title": ctx.title, "orders": refs},
                        event_id=event_id)
            return alert_id
        return None

    def redeem(self, token: str, data: dict) -> dict:
        """并发核销。条件 UPDATE 原子占用配额，任何并发都不得突破承诺量。

        data: order_id, commitment_id, qty, channel
        """
        caller = self._auth(token, "redemption.redeem")
        qty = int(data.get("qty", 1))
        if qty <= 0:
            raise domain.GuardError("qty 必须为正整数")
        ts = self._now()
        with self.db.transaction() as con:
            order = con.execute(
                "SELECT * FROM orders WHERE order_id=?", (data["order_id"],)
            ).fetchone()
            if order is None:
                raise domain.NotFound("order", data["order_id"])
            order = dict(order)
            com = con.execute(
                "SELECT * FROM commitments WHERE commitment_id=?",
                (data["commitment_id"],),
            ).fetchone()
            if com is None:
                raise domain.NotFound("commitment", data["commitment_id"])
            com = dict(com)
            if caller["subject_id"] != com["merchant_id"]:
                raise domain.PermissionDenied(caller["role"],
                                              "redemption.redeem:other_merchant")
            self._check_redeemable(order, com, ts)

            # 同一订单在同一承诺上的重复核销直接重放原记录（跨渠道重传安全）。
            prior = con.execute(
                "SELECT * FROM redemptions WHERE order_id=? AND commitment_id=?"
                " AND status='redeemed'",
                (data["order_id"], data["commitment_id"]),
            ).fetchone()
            if prior:
                self._chain(con, scope="redemption", subject_ref=prior["redemption_id"],
                            action="redemption.replayed", actor_role=caller["role"],
                            detail={"order_id": data["order_id"]},
                            event_id=com["event_id"])
                out = dict(prior); out["replayed"] = True
                return out

            # 原子条件扣减：只有剩余量足够时才更新成功。
            cur = con.execute(
                "UPDATE commitments SET redeemed_total = redeemed_total + ?"
                " WHERE commitment_id=? AND active=1 AND redeemed_total + ? <= promised_total",
                (qty, data["commitment_id"], qty),
            )
            if cur.rowcount != 1:
                raise domain.Conflict(
                    f"并发核销冲突：承诺 {data['commitment_id']} 剩余量不足 {qty}",
                    code="quota_exceeded",
                )
            redemption_id = _uid("rdp")
            con.execute(
                "INSERT INTO redemptions(redemption_id, order_id, commitment_id,"
                " event_id, qty, status, channel, created_at)"
                " VALUES (?,?,?,?,?,'redeemed',?,?)",
                (redemption_id, data["order_id"], data["commitment_id"],
                 com["event_id"], qty, data.get("channel", order["channel"]), ts),
            )
            con.execute(
                "UPDATE orders SET status='redeemed', decided_at=? WHERE order_id=?",
                (ts, data["order_id"]),
            )
            self._chain(con, scope="redemption", subject_ref=redemption_id,
                        action="redemption.redeemed", actor_role=caller["role"],
                        detail={"order_id": data["order_id"], "qty": qty,
                                "commitment_id": data["commitment_id"]},
                        event_id=com["event_id"])
            out = dict(con.execute(
                "SELECT * FROM redemptions WHERE redemption_id=?", (redemption_id,)
            ).fetchone())
            return out

    @staticmethod
    def _check_redeemable(order: dict, com: dict, ts: str) -> None:
        if com["kind"] not in domain.redeemable_kinds():
            raise domain.StateError(f"承诺类型 {com['kind']} 不可核销")
        if not com["active"]:
            raise domain.StateError("承诺已失效，无法核销（套餐无法核销场景）")
        if not (com["valid_from"] <= ts <= com["valid_to"]):
            raise domain.StateError("承诺不在有效期内，无法核销")
        if order["merchant_id"] != com["merchant_id"]:
            raise domain.StateError("订单与承诺不属于同一商户")
        if order["event_id"] != com["event_id"]:
            raise domain.StateError("订单与承诺不属于同一赛事")
        if order["status"] not in (domain.OrderStatus.CREATED, domain.OrderStatus.REDEEMED):
            raise domain.StateError(f"订单状态 {order['status']} 不可核销")

    def reverse_redemption(self, token: str, redemption_id: str, reason: str) -> dict:
        """撤销核销（改期/取消后），归还承诺配额。"""
        caller = self._auth(token, "redemption.reverse")
        ts = self._now()
        with self.db.transaction() as con:
            r = con.execute("SELECT * FROM redemptions WHERE redemption_id=?",
                            (redemption_id,)).fetchone()
            if r is None:
                raise domain.NotFound("redemption", redemption_id)
            r = dict(r)
            if r["status"] != "redeemed":
                raise domain.StateError("该核销已撤销")
            com = con.execute(
                "SELECT merchant_id FROM commitments WHERE commitment_id=?",
                (r["commitment_id"],),
            ).fetchone()
            if caller["subject_id"] != (com and com["merchant_id"]):
                raise domain.PermissionDenied(caller["role"],
                                              "redemption.reverse:other_merchant")
            con.execute(
                "UPDATE redemptions SET status='reversed', reversed_at=? WHERE redemption_id=?",
                (ts, redemption_id),
            )
            con.execute(
                "UPDATE commitments SET redeemed_total = MAX(redeemed_total - ?, 0)"
                " WHERE commitment_id=?",
                (r["qty"], r["commitment_id"]),
            )
            self._chain(con, scope="redemption", subject_ref=redemption_id,
                        action="redemption.reversed", actor_role=caller["role"],
                        detail={"reason": reason, "qty": r["qty"]},
                        event_id=r["event_id"])
        return dict(self.db.query_one(
            "SELECT * FROM redemptions WHERE redemption_id=?", (redemption_id,)))

    # ======================================================================
    # 投诉（多渠道、重传去重）
    # ======================================================================

    def ingest_complaint(self, token: str, data: dict) -> dict:
        """投诉从热线、小程序、现场台等多渠道进入；同一投诉重传不重复建单、
        更不会重复赔付。任一联防角色均可受理/代录。"""
        caller = self._auth(token, "complaint.ingest")
        for k in ("event_id", "merchant_id", "channel", "channel_ref", "category"):
            if not data.get(k):
                raise domain.GuardError(f"缺少必填字段: {k}")
        self._get("events", "event_id", data["event_id"])
        self._get("merchants", "merchant_id", data["merchant_id"])
        ts = self._now()
        with self.db.transaction() as con:
            existing = con.execute(
                "SELECT * FROM complaints WHERE channel=? AND channel_ref=?",
                (data["channel"], data["channel_ref"]),
            ).fetchone()
            hit = "channel_ref"
            if existing is None and data.get("dedup_key"):
                existing = con.execute(
                    "SELECT * FROM complaints WHERE dedup_key=?",
                    (data["dedup_key"],),
                ).fetchone()
                hit = "dedup_key"
            if existing is not None:
                comp = dict(existing)
                self._chain(con, scope="complaint", subject_ref=comp["complaint_id"],
                            action="complaint.replayed", actor_role=caller["role"],
                            detail={"via": hit}, event_id=comp["event_id"])
                comp["replayed"] = True
                comp["dedup_hit"] = hit
                return comp
            complaint_id = _uid("cpl")
            weather = con.execute(
                "SELECT weather_level FROM events WHERE event_id=?", (data["event_id"],)
            ).fetchone()["weather_level"]
            try:
                con.execute(
                    "INSERT INTO complaints(complaint_id, event_id, order_id, merchant_id,"
                    " consumer_ref, channel, channel_ref, dedup_key, category, status,"
                    " weather_snapshot, created_at, updated_at)"
                    " VALUES (?,?,?,?,?,?,?,?,?,'open',?,?,?)",
                    (complaint_id, data["event_id"], data.get("order_id"),
                     data["merchant_id"], data.get("consumer_ref"),
                     data["channel"], data["channel_ref"], data.get("dedup_key"),
                     data["category"], weather, ts, ts),
                )
            except Exception as exc:
                raise domain.Conflict("投诉重复提交", code="duplicate_complaint") from exc
            self._chain(con, scope="complaint", subject_ref=complaint_id,
                        action="complaint.opened", actor_role=caller["role"],
                        detail={"category": data["category"],
                                "channel": data["channel"]},
                        event_id=data["event_id"])
        return self.get_complaint(complaint_id)

    def get_complaint(self, complaint_id: str) -> dict:
        return self._get("complaints", "complaint_id", complaint_id)

    # ======================================================================
    # 调查 / 申诉
    # ======================================================================

    def record_investigation(self, token: str, complaint_id: str, note: str,
                             evidence: dict | None = None,
                             root_cause: str | None = None) -> dict:
        caller = self._auth(token, "investigation.record")
        comp = self._get("complaints", "complaint_id", complaint_id)
        if comp["status"] == domain.ComplaintStatus.CLOSED:
            raise domain.StateError("投诉已关闭，不可再补充调查")
        ts = self._now()
        with self.db.transaction() as con:
            con.execute(
                "INSERT INTO investigations(complaint_id, note, evidence_json,"
                " actor_role, created_at) VALUES (?,?,?,?,?)",
                (complaint_id, note, json.dumps(evidence or {}, ensure_ascii=False),
                 caller["role"], ts),
            )
            if root_cause:
                con.execute(
                    "UPDATE complaints SET status='investigating', root_cause=?,"
                    " handler_role=?, updated_at=? WHERE complaint_id=?",
                    (root_cause, caller["role"], ts, complaint_id),
                )
            else:
                con.execute(
                    "UPDATE complaints SET status='investigating', handler_role=?,"
                    " updated_at=? WHERE complaint_id=?",
                    (caller["role"], ts, complaint_id),
                )
            self._chain(con, scope="complaint", subject_ref=complaint_id,
                        action="investigation.recorded", actor_role=caller["role"],
                        detail={"note": note, "root_cause": root_cause},
                        event_id=comp["event_id"])
        return self.get_complaint(complaint_id)

    def submit_appeal(self, token: str, complaint_id: str, reason: str,
                      evidence: dict | None = None) -> dict:
        caller = self._auth(token, "appeal.submit")
        comp = self._get("complaints", "complaint_id", complaint_id)
        if caller["subject_id"] != comp["merchant_id"]:
            raise domain.PermissionDenied(caller["role"], "appeal.submit:other_merchant")
        if comp["status"] not in (domain.ComplaintStatus.INVESTIGATING,
                                  domain.ComplaintStatus.APPEALING):
            raise domain.StateError("仅调查中的投诉可申诉")
        ts = self._now()
        with self.db.transaction() as con:
            cur = con.execute(
                "INSERT INTO appeals(complaint_id, reason, status, evidence_json,"
                " created_at) VALUES (?,?,'pending',?,?)",
                (complaint_id, reason, json.dumps(evidence or {}, ensure_ascii=False), ts),
            )
            appeal_id = cur.lastrowid
            con.execute(
                "UPDATE complaints SET status='appealing', updated_at=? WHERE complaint_id=?",
                (ts, complaint_id),
            )
            self._chain(con, scope="appeal", subject_ref=str(appeal_id),
                        action="appeal.submitted", actor_role=caller["role"],
                        detail={"complaint_id": complaint_id, "reason": reason},
                        event_id=comp["event_id"])
        return {"appeal_id": appeal_id, "complaint_id": complaint_id, "status": "pending"}

    def rule_appeal(self, token: str, appeal_id: int, uphold: bool,
                    note: str = "") -> dict:
        """监管/文旅裁定申诉。成立则撤销已生效处置（退款冲正、补偿追回），
        承担方改为 none；不成立则维持。"""
        caller = self._auth(token, "appeal.rule")
        ts = self._now()
        with self.db.transaction() as con:
            ar = con.execute("SELECT * FROM appeals WHERE id=?", (appeal_id,)).fetchone()
            if ar is None:
                raise domain.NotFound("appeal", str(appeal_id))
            if ar["status"] != "pending":
                raise domain.StateError("该申诉已裁定")
            complaint_id = ar["complaint_id"]
            comp = dict(con.execute(
                "SELECT * FROM complaints WHERE complaint_id=?", (complaint_id,)
            ).fetchone())
            new_status = "upheld" if uphold else "rejected"
            con.execute(
                "UPDATE appeals SET status=?, ruled_by=?, ruling_note=?, ruled_at=?"
                " WHERE id=?",
                (new_status, caller["role"], note, ts, appeal_id),
            )
            reversed_dispositions = []
            if uphold:
                con.execute(
                    "UPDATE complaints SET root_cause='appeal_upheld', updated_at=?"
                    " WHERE complaint_id=?",
                    (ts, complaint_id),
                )
                rows = con.execute(
                    "SELECT * FROM dispositions WHERE complaint_id=? AND status='settled'",
                    (complaint_id,),
                ).fetchall()
                for d in rows:
                    con.execute(
                        "UPDATE dispositions SET status='reversed', reversed_at=?,"
                        " reversal_reason='appeal_upheld', bearer='none' WHERE disposition_id=?",
                        (ts, d["disposition_id"]),
                    )
                    reversed_dispositions.append(d["disposition_id"])
                    self._chain(con, scope="disposition",
                                subject_ref=d["disposition_id"],
                                action="disposition.reversed",
                                actor_role=caller["role"],
                                detail={"reason": "申诉成立，原承担撤销",
                                        "previous_bearer": d["bearer"]},
                                event_id=comp["event_id"])
            con.execute(
                "UPDATE complaints SET status='investigating', updated_at=? WHERE complaint_id=?",
                (ts, complaint_id),
            )
            self._chain(con, scope="appeal", subject_ref=str(appeal_id),
                        action="appeal.ruled", actor_role=caller["role"],
                        detail={"uphold": uphold, "note": note,
                                "reversed": reversed_dispositions},
                        event_id=comp["event_id"])
        return {"appeal_id": appeal_id, "status": new_status,
                "reversed_dispositions": reversed_dispositions}

    # ======================================================================
    # 处置：退款 / 补偿（唯一 settled 约束 + 政策时点适用 + 承担裁决）
    # ======================================================================

    def _active_policy_at(self, con, at_iso: str) -> dict:
        rows = con.execute(
            "SELECT policy_id, version, effective_at, content_json FROM policies"
            " WHERE effective_at <= ? ORDER BY effective_at DESC LIMIT 1",
            (at_iso,),
        ).fetchall()
        if not rows:
            raise domain.StateError("没有适用于该交易时点的政策")
        return dict(rows[0])

    def settle_disposition(self, token: str, complaint_id: str, kind: str,
                           amount: float, *, root_cause: str,
                           transaction_at: str | None = None) -> dict:
        """统一处置入口（退款/补偿）。

        - 权限：refund.approve / compensation.approve
        - 幂等：同一投诉同一 kind 仅允许一笔 settled，重传返回既有记录
        - 政策：按交易时点适用版本（默认投诉创建时间），政策变更不溯及既往
        - 承担方：由 root_cause + 气象快照经 decide_bearer 裁决
        """
        action = "refund.approve" if kind == domain.DispositionKind.REFUND \
            else "compensation.approve"
        caller = self._auth(token, action)
        if kind not in (domain.DispositionKind.REFUND, domain.DispositionKind.COMPENSATION):
            raise domain.GuardError("kind 仅支持 refund / compensation")
        if float(amount) < 0:
            raise domain.GuardError("amount 不可为负")
        comp = self._get("complaints", "complaint_id", complaint_id)
        if comp["status"] == domain.ComplaintStatus.CLOSED:
            raise domain.StateError("投诉已关闭")
        ts = self._now()
        tx_at = to_iso(transaction_at) if transaction_at else comp["created_at"]
        with self.db.transaction() as con:
            existing = con.execute(
                "SELECT * FROM dispositions WHERE complaint_id=? AND kind=? AND status='settled'",
                (complaint_id, kind),
            ).fetchone()
            if existing is not None:
                self._chain(con, scope="disposition",
                            subject_ref=existing["disposition_id"],
                            action="disposition.replayed", actor_role=caller["role"],
                            detail={"amount": amount}, event_id=comp["event_id"])
                out = dict(existing); out["replayed"] = True
                return out

            policy = self._active_policy_at(con, tx_at)
            bearer = domain.decide_bearer(
                root_cause=root_cause, weather_level=comp["weather_snapshot"])
            disposition_id = _uid("dsp")
            try:
                con.execute(
                    "INSERT INTO dispositions(disposition_id, complaint_id, kind, amount,"
                    " root_cause, bearer, status, policy_id, policy_version,"
                    " created_at) VALUES (?,?,?,?,?,?,'settled',?,?,?)",
                    (disposition_id, complaint_id, kind, float(amount), root_cause,
                     bearer, policy["policy_id"], policy["version"], ts),
                )
            except Exception as exc:
                raise domain.Conflict("该投诉已有同类型有效赔付，禁止重复赔付",
                                      code="duplicate_disposition") from exc
            con.execute(
                "UPDATE complaints SET root_cause=?, updated_at=? WHERE complaint_id=?",
                (root_cause, ts, complaint_id),
            )
            self._chain(con, scope="disposition", subject_ref=disposition_id,
                        action=f"disposition.{kind}_settled", actor_role=caller["role"],
                        detail={"amount": amount, "bearer": bearer,
                                "policy_id": policy["policy_id"],
                                "policy_version": policy["version"],
                                "root_cause": root_cause,
                                "policy_reason": f"按交易时点 {tx_at} 适用"},
                        event_id=comp["event_id"])
        return self.get_disposition(disposition_id)

    def get_disposition(self, disposition_id: str) -> dict:
        return self._get("dispositions", "disposition_id", disposition_id)

    def close_complaint(self, token: str, complaint_id: str, note: str = "") -> dict:
        caller = self._auth(token, "complaint.close")
        comp = self._get("complaints", "complaint_id", complaint_id)
        if comp["status"] == domain.ComplaintStatus.CLOSED:
            raise domain.StateError("投诉已关闭")
        if comp["status"] == domain.ComplaintStatus.APPEALING:
            raise domain.StateError("申诉裁定前不可关闭")
        ts = self._now()
        with self.db.transaction() as con:
            con.execute(
                "UPDATE complaints SET status='closed', updated_at=? WHERE complaint_id=?",
                (ts, complaint_id),
            )
            self._chain(con, scope="complaint", subject_ref=complaint_id,
                        action="complaint.closed", actor_role=caller["role"],
                        detail={"note": note}, event_id=comp["event_id"])
        return self.get_complaint(complaint_id)

    # ======================================================================
    # 商户停业 / 复业
    # ======================================================================

    def suspend_merchant(self, token: str, merchant_id: str, reason: str) -> dict:
        caller = self._auth(token, "merchant.suspend")
        m = self._get("merchants", "merchant_id", merchant_id)
        if m["status"] == "suspended":
            raise domain.StateError("商户已处于停业状态")
        ts = self._now()
        with self.db.transaction() as con:
            con.execute(
                "UPDATE merchants SET status='suspended', suspend_reason=?,"
                " suspended_by=?, suspended_at=? WHERE merchant_id=?",
                (reason, caller["role"], ts, merchant_id),
            )
            con.execute(
                "UPDATE commitments SET active=0 WHERE merchant_id=?", (merchant_id,)
            )
            self._chain(con, scope="merchant", subject_ref=merchant_id,
                        action="merchant.suspended", actor_role=caller["role"],
                        detail={"reason": reason})
        return self.get_merchant(merchant_id)

    def unsuspend_merchant(self, token: str, merchant_id: str, note: str = "") -> dict:
        caller = self._auth(token, "merchant.unsuspend")
        m = self._get("merchants", "merchant_id", merchant_id)
        if m["status"] != "suspended":
            raise domain.StateError("商户未被停业")
        ts = self._now()
        with self.db.transaction() as con:
            con.execute(
                "UPDATE merchants SET status='active', suspend_reason=NULL,"
                " suspended_by=NULL, suspended_at=NULL WHERE merchant_id=?",
                (merchant_id,),
            )
            # 仅恢复仍在有效期内的承诺，过期承诺不复活。
            con.execute(
                "UPDATE commitments SET active=1 WHERE merchant_id=? AND valid_to>=?",
                (merchant_id, ts),
            )
            self._chain(con, scope="merchant", subject_ref=merchant_id,
                        action="merchant.unsuspended", actor_role=caller["role"],
                        detail={"note": note})
        return self.get_merchant(merchant_id)

    # ======================================================================
    # 极端气象 / 改期 / 取消：权益失效与补偿链
    # ======================================================================

    def reschedule_event(self, token: str, event_id: str, new_starts_at: str,
                         new_ends_at: str, reason: str,
                         weather_level: str = domain.WeatherLevel.NORMAL) -> dict:
        caller = self._auth(token, "event.reschedule")
        ev = self._get("events", "event_id", event_id)
        if ev["status"] == domain.EventStatus.CANCELLED:
            raise domain.StateError("赛事已取消，不可改期")
        new_s, new_e = to_iso(new_starts_at), to_iso(new_ends_at)
        if new_e <= new_s:
            raise domain.GuardError("new_ends_at 必须晚于 new_starts_at")
        ts = self._now()
        with self.db.transaction() as con:
            con.execute(
                "UPDATE events SET starts_at=?, ends_at=?, status='rescheduled',"
                " weather_level=?, updated_at=? WHERE event_id=?",
                (new_s, new_e, weather_level, ts, event_id),
            )
            con.execute(
                "INSERT INTO event_schedule_history(event_id, change_type,"
                " old_starts_at, new_starts_at, reason, weather_level, actor_role,"
                " created_at) VALUES (?,?,?,?,?,?,?,?)",
                (event_id, "reschedule", ev["starts_at"], new_s, reason,
                 weather_level, caller["role"], ts),
            )
            self._chain(con, scope="event", subject_ref=event_id,
                        action="event.rescheduled", actor_role=caller["role"],
                        detail={"old": ev["starts_at"], "new": new_s,
                                "reason": reason, "weather_level": weather_level},
                        event_id=event_id)
            alerts = self._invalidate_event_benefits(
                con, event_id, reason=f"改期：{reason}",
                weather_level=weather_level, actor=caller["role"])
        out = self.get_event(event_id)
        out["benefit_alerts"] = alerts
        return out

    def cancel_event(self, token: str, event_id: str, reason: str,
                     weather_level: str = domain.WeatherLevel.NORMAL) -> dict:
        caller = self._auth(token, "event.cancel")
        ev = self._get("events", "event_id", event_id)
        if ev["status"] == domain.EventStatus.CANCELLED:
            raise domain.StateError("赛事已取消")
        ts = self._now()
        with self.db.transaction() as con:
            con.execute(
                "UPDATE events SET status='cancelled', weather_level=?, updated_at=?"
                " WHERE event_id=?",
                (weather_level, ts, event_id),
            )
            con.execute(
                "INSERT INTO event_schedule_history(event_id, change_type,"
                " old_starts_at, new_starts_at, reason, weather_level, actor_role,"
                " created_at) VALUES (?,?,?,?,?,?,?,?)",
                (event_id, "cancel", ev["starts_at"], None, reason,
                 weather_level, caller["role"], ts),
            )
            self._chain(con, scope="event", subject_ref=event_id,
                        action="event.cancelled", actor_role=caller["role"],
                        detail={"reason": reason, "weather_level": weather_level},
                        event_id=event_id)
            alerts = self._invalidate_event_benefits(
                con, event_id, reason=f"取消：{reason}",
                weather_level=weather_level, actor=caller["role"])
        out = self.get_event(event_id)
        out["benefit_alerts"] = alerts
        return out

    def _invalidate_event_benefits(self, con, event_id: str, *, reason: str,
                                   weather_level: str, actor: str) -> list[str]:
        """赛事改期/取消使官方权益失效，对仍有未核销存量的权益生成预警。"""
        alert_ids: list[str] = []
        benefits = con.execute(
            "SELECT b.*, COALESCE((SELECT SUM(r.qty) FROM redemptions r"
            " JOIN commitments c ON c.commitment_id = r.commitment_id"
            " WHERE r.status='redeemed' AND c.kind='benefit' AND c.event_id=b.event_id"
            " AND json_extract(c.spec_json,'$.benefit_code')=b.code),0) AS used"
            " FROM benefits b WHERE b.event_id=? AND b.status='active'",
            (event_id,),
        ).fetchall()
        ts = self._now()
        for b in benefits:
            b = dict(b)
            con.execute(
                "UPDATE benefits SET status='invalidated', invalidated_reason=?"
                " WHERE benefit_id=?",
                (reason, b["benefit_id"]),
            )
            con.execute(
                "UPDATE commitments SET active=0 WHERE event_id=? AND kind='benefit'"
                " AND json_extract(spec_json,'$.benefit_code')=?",
                (event_id, b["code"]),
            )
            unredeemed = max(int(b["quota"]) - int(b["used"]), 0)
            ctx = domain.explain_benefit_invalidation(
                event_id=event_id, benefit_code=b["code"], reason=reason,
                weather_level=weather_level, valid_from=b["valid_from"],
                valid_to=b["valid_to"], unredeemed=unredeemed,
            )
            alert_id = _uid("alert")
            con.execute(
                "INSERT INTO alerts(alert_id, event_id, subject_id, type, severity,"
                " title, explanation, evidence_json, related_json, status, created_at)"
                " VALUES (?,?,?,?,?,?,?,?,?,'open',?)",
                (alert_id, event_id, b["code"], ctx.alert_type, ctx.severity,
                 ctx.title, ctx.explanation,
                 json.dumps(ctx.evidence, ensure_ascii=False),
                 json.dumps(ctx.related, ensure_ascii=False), ts),
            )
            alert_ids.append(alert_id)
            self._chain(con, scope="benefit", subject_ref=b["benefit_id"],
                        action="benefit.invalidated", actor_role=actor,
                        detail={"reason": reason, "unredeemed": unredeemed,
                                "alert": alert_id}, event_id=event_id)
        return alert_ids

    # ======================================================================
    # 预警处置 / 查询
    # ======================================================================

    def acknowledge_alert(self, token: str, alert_id: str, note: str = "") -> dict:
        caller = self._auth(token, "complaint.handle")
        a = self._get("alerts", "alert_id", alert_id)
        with self.db.transaction() as con:
            con.execute("UPDATE alerts SET status='acknowledged' WHERE alert_id=?",
                        (alert_id,))
            self._chain(con, scope="alert", subject_ref=alert_id,
                        action="alert.acknowledged", actor_role=caller["role"],
                        detail={"note": note}, event_id=a["event_id"])
        return self.get_alert(alert_id)

    def get_alert(self, alert_id: str) -> dict:
        row = self._get("alerts", "alert_id", alert_id)
        row["evidence"] = json.loads(row.pop("evidence_json"))
        row["related"] = json.loads(row.pop("related_json"))
        return row

    def list_alerts(self, event_id: str | None = None,
                    alert_type: str | None = None) -> list[dict]:
        sql, params = "SELECT * FROM alerts WHERE 1=1", []
        if event_id:
            sql += " AND event_id=?"; params.append(event_id)
        if alert_type:
            sql += " AND type=?"; params.append(alert_type)
        sql += " ORDER BY created_at"
        out = []
        for r in self.db.query(sql, params):
            d = dict(r)
            d["evidence"] = json.loads(d.pop("evidence_json"))
            d["related"] = json.loads(d.pop("related_json"))
            out.append(d)
        return out

    # ======================================================================
    # 事件链 / 按赛事还原 / 承担方对账
    # ======================================================================

    def chain_of(self, scope: str, subject_ref: str) -> list[dict]:
        """按订单/投诉/承诺等主体还原完整事件链（含因果序号）。"""
        rows = self.db.query(
            "SELECT seq, event_id, scope, subject_ref, action, actor_role,"
            " detail_json, causation_seq, created_at FROM event_chain"
            " WHERE scope=? AND subject_ref=? ORDER BY seq",
            (scope, subject_ref),
        )
        out = []
        for r in rows:
            d = dict(r)
            d["detail"] = json.loads(d.pop("detail_json"))
            out.append(d)
        return out

    def event_timeline(self, event_id: str) -> list[dict]:
        """一场赛事的完整时间线（跨主体），供值班人员还原消费影响。"""
        rows = self.db.query(
            "SELECT seq, scope, subject_ref, action, actor_role, detail_json,"
            " causation_seq, created_at FROM event_chain WHERE event_id=?"
            " ORDER BY seq", (event_id,),
        )
        out = []
        for r in rows:
            d = dict(r)
            d["detail"] = json.loads(d.pop("detail_json"))
            out.append(d)
        return out

    def event_impact_report(self, event_id: str) -> dict:
        """按一场赛事汇总消费影响：订单、核销、拒单、投诉、处置与承担。"""
        self._get("events", "event_id", event_id)
        totals = self.db.query_one(
            "SELECT COUNT(*) AS orders,"
            " COALESCE(SUM(CASE WHEN status='rejected' THEN 1 ELSE 0 END),0) AS rejected,"
            " COALESCE(SUM(CASE WHEN status='redeemed' THEN 1 ELSE 0 END),0) AS redeemed,"
            " COALESCE(SUM(CASE WHEN surcharge_flag=1 THEN 1 ELSE 0 END),0) AS surcharges,"
            " COALESCE(SUM(amount),0) AS gmv FROM orders WHERE event_id=?",
            (event_id,),
        )
        complaints = self.db.query_one(
            "SELECT COUNT(*) AS n,"
            " COALESCE(SUM(CASE WHEN status='closed' THEN 1 ELSE 0 END),0) AS closed"
            " FROM complaints WHERE event_id=?", (event_id,),
        )
        disposition_rows = self.db.query(
            "SELECT d.bearer AS bearer, d.kind AS kind, d.status AS status,"
            " COUNT(*) AS n, COALESCE(SUM(d.amount),0) AS amount"
            " FROM dispositions d JOIN complaints c ON c.complaint_id=d.complaint_id"
            " WHERE c.event_id=? GROUP BY d.bearer, d.kind, d.status"
            " ORDER BY d.bearer, d.kind",
            (event_id,),
        )
        by_bearer: dict[str, dict] = {}
        for r in disposition_rows:
            slot = by_bearer.setdefault(r["bearer"], {"settled_amount": 0.0,
                                                      "reversed_amount": 0.0,
                                                      "items": []})
            item = {"kind": r["kind"], "status": r["status"],
                    "count": r["n"], "amount": round(r["amount"], 2)}
            slot["items"].append(item)
            if r["status"] == "settled":
                slot["settled_amount"] += r["amount"]
            else:
                slot["reversed_amount"] += r["amount"]
        for slot in by_bearer.values():
            slot["settled_amount"] = round(slot["settled_amount"], 2)
            slot["reversed_amount"] = round(slot["reversed_amount"], 2)
        return {
            "event_id": event_id,
            "orders": {"total": totals["orders"], "rejected": totals["rejected"],
                       "redeemed": totals["redeemed"],
                       "surcharge_flags": totals["surcharges"],
                       "gmv": round(totals["gmv"], 2)},
            "complaints": {"total": complaints["n"], "closed": complaints["closed"],
                           "open": complaints["n"] - complaints["closed"]},
            "dispositions_by_bearer": by_bearer,
            "alerts": [{"alert_id": a["alert_id"], "type": a["type"],
                        "severity": a["severity"], "title": a["title"],
                        "status": a["status"]} for a in self.list_alerts(event_id)],
        }

    def reconcile_bearer(self, disposition_id: str) -> dict:
        """接口核对：某笔处置最终由谁承担，附完整溯源链。"""
        d = self._get("dispositions", "disposition_id", disposition_id)
        comp = self._get("complaints", "complaint_id", d["complaint_id"])
        policy = self.db.query_one(
            "SELECT title, effective_at FROM policies WHERE policy_id=? AND version=?",
            (d["policy_id"], d["policy_version"]),
        )
        expected = domain.decide_bearer(
            root_cause=d["root_cause"], weather_level=comp["weather_snapshot"])
        return {
            "disposition_id": disposition_id,
            "complaint_id": d["complaint_id"],
            "kind": d["kind"],
            "amount": d["amount"],
            "status": d["status"],
            "recorded_bearer": d["bearer"],
            "expected_bearer": expected,
            "consistent": d["bearer"] == expected
            or (d["status"] == "reversed" and d["bearer"] == domain.Bearer.NONE),
            "root_cause": d["root_cause"],
            "weather_snapshot": comp["weather_snapshot"],
            "applied_policy": {"policy_id": d["policy_id"],
                               "version": d["policy_version"],
                               "title": policy["title"],
                               "effective_at": policy["effective_at"]},
            "chain": self.chain_of("disposition", disposition_id),
        }

    # ======================================================================
    # 辅助
    # ======================================================================

    @staticmethod
    def _last_seq(con, scope: str, subject_ref: str) -> int | None:
        row = con.execute(
            "SELECT seq FROM event_chain WHERE scope=? AND subject_ref=?"
            " ORDER BY seq DESC LIMIT 1", (scope, subject_ref),
        ).fetchone()
        return row["seq"] if row else None

    def list_policies(self) -> list[dict]:
        return [dict(r) for r in self.db.query(
            "SELECT policy_id, version, title, effective_at, created_at"
            " FROM policies ORDER BY effective_at")]
