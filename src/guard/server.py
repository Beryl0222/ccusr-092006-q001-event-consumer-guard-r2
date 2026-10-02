"""HTTP API（标准库 http.server）、RBAC 与幂等重放。

鉴权约定（无外部依赖，便于独立运行；生产应替换为网关注入）：
    X-Actor-Id:   人员账号
    X-Actor-Role: ORGANIZER | MERCHANT | OFFICER
幂等约定：
    对写接口加 ``Idempotency-Key``，同键重放返回首次结果，不重复执行；
    同键不同请求体返回 422，防止键复用造成误入账。
"""

from __future__ import annotations

import hashlib
import json
import re
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Callable

from .db import DEPARTMENTS, GuardError, GuardStore

ROLES = ("ORGANIZER", "MERCHANT", "OFFICER")


class ApiError(Exception):
    def __init__(self, status: int, code: str, message: str):
        super().__init__(message)
        self.status = status
        self.code = code


# ---------------------------------------------------------------------------
# 处理器
# ---------------------------------------------------------------------------


class GuardHandler(BaseHTTPRequestHandler):
    server_version = "EventGuard/0.2"
    store: GuardStore  # 由 GuardHTTPServer 注入

    # 路由在模块末尾的 build_routes 中装配：(method, regex, allowed_roles, idem_scope|None, fn)
    routes: list[tuple[str, re.Pattern[str], tuple[str, ...] | None, str | None, Callable]] = []

    # ------------------------------------------------------------ 基础工具
    def log_message(self, fmt: str, *args: Any) -> None:  # 精简访问日志
        if getattr(self.server, "quiet", False):
            return
        super().log_message(fmt, *args)

    @property
    def store_(self) -> GuardStore:
        return self.server.store  # type: ignore[attr-defined]

    def _read_json(self) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length") or 0)
        if length == 0:
            return {}
        raw = self.rfile.read(length)
        try:
            data = json.loads(raw.decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise ApiError(400, "BAD_JSON", f"请求体不是合法 JSON：{exc}") from exc
        if not isinstance(data, dict):
            raise ApiError(400, "BAD_JSON", "请求体必须是 JSON 对象")
        return data

    def require(self, body: dict[str, Any], *keys: str) -> list[Any]:
        out = []
        for k in keys:
            if k not in body or body[k] in (None, ""):
                raise ApiError(400, "MISSING_FIELD", f"缺少必填字段：{k}")
            out.append(body[k])
        return out

    def opt(self, body: dict[str, Any], key: str, default: Any = None) -> Any:
        v = body.get(key)
        return default if v in (None, "") else v

    def _send(self, status: int, payload: Any, extra_headers: dict[str, str] | None = None) -> None:
        data = json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        for k, v in (extra_headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(data)

    def _auth(self, allowed_roles: tuple[str, ...] | None) -> tuple[str, str, str | None]:
        actor = self.headers.get("X-Actor-Id")
        role = self.headers.get("X-Actor-Role")
        if not actor or not role:
            raise ApiError(401, "UNAUTHENTICATED", "缺少 X-Actor-Id / X-Actor-Role 请求头")
        if role not in ROLES:
            raise ApiError(403, "FORBIDDEN_ROLE", f"角色必须为 {ROLES}")
        if allowed_roles is not None and role not in allowed_roles:
            raise ApiError(403, "FORBIDDEN", f"该接口仅限 {allowed_roles}")
        dept = None
        if role == "OFFICER" and allowed_roles and "OFFICER" in allowed_roles:
            # 仅在该路由允许执法人员行使处置权限时强制声明部门
            dept = self.headers.get("X-Actor-Dept")
            if dept not in DEPARTMENTS:
                raise ApiError(403, "FORBIDDEN_DEPT",
                               f"执法人员必须通过 X-Actor-Dept 指定部门：{DEPARTMENTS}")
        return actor, role, dept

    # ------------------------------------------------------------ 入口
    def _dispatch(self, method: str) -> None:
        try:
            path = self.path.split("?", 1)[0]
            for m, pattern, allowed_roles, idem_scope, fn in self.routes:
                if m != method:
                    continue
                match = pattern.fullmatch(path)
                if not match:
                    continue
                actor, role, dept = "", "", None
                if allowed_roles != PUBLIC:
                    actor, role, dept = self._auth(allowed_roles)
                actor_tag = f"{role.lower()}:{actor}" if actor else "anonymous"
                body = self._read_json() if method == "POST" else {}
                query = self._query()
                # 幂等重放
                idem_key = self.headers.get("Idempotency-Key")
                if idem_scope and idem_key:
                    body_hash = hashlib.sha256(
                        json.dumps(body, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
                    saved = self.store_.lookup_idem(f"{idem_scope}", idem_key)
                    if saved is not None:
                        if saved["request_hash"] != body_hash:
                            raise ApiError(422, "IDEMPOTENCY_CONFLICT",
                                           "同一 Idempotency-Key 对应不同的请求体")
                        self._send(saved["response_code"], json.loads(saved["response_body"]),
                                   {"Idempotent-Replay": "true"})
                        return
                ctx = Ctx(store=self.store_, body=body, query=query, actor=actor_tag,
                          params=match.groupdict(), handler=self, dept=dept)
                status, result = fn(ctx)
                if idem_scope and idem_key:
                    self.store_.store_idem(
                        idem_scope, idem_key,
                        hashlib.sha256(json.dumps(body, sort_keys=True, ensure_ascii=False).encode()
                                       ).hexdigest(),
                        status, json.dumps(result, ensure_ascii=False))
                self._send(status, result)
                return
            self._send(404, {"error": {"code": "NOT_FOUND", "message": f"{method} {path} 不存在"}})
        except ApiError as e:
            self._send(e.status, {"error": {"code": e.code, "message": str(e)}})
        except GuardError as e:
            self._send(e.status, {"error": {"code": e.code, "message": str(e)}})
        except Exception as exc:  # noqa: BLE001 - 统一兜底为 500 JSON
            self._send(500, {"error": {"code": "INTERNAL", "message": f"{type(exc).__name__}: {exc}"}})

    def _query(self) -> dict[str, str]:
        if "?" not in self.path:
            return {}
        from urllib.parse import parse_qs
        return {k: v[-1] for k, v in parse_qs(self.path.split("?", 1)[1]).items()}

    def do_GET(self) -> None:
        self._dispatch("GET")

    def do_POST(self) -> None:
        self._dispatch("POST")


class Ctx:
    """单次请求上下文。"""

    def __init__(self, *, store: GuardStore, body: dict, query: dict, actor: str,
                 params: dict, handler: GuardHandler, dept: str | None = None):
        self.store = store
        self.body = body
        self.query = query
        self.actor = actor
        self.dept = dept
        self.p = params
        self.h = handler

    def need(self, *keys: str) -> list[Any]:
        return self.h.require(self.body, *keys)

    def opt(self, key: str, default: Any = None) -> Any:
        return self.h.opt(self.body, key, default)

    def require_dept(self, *depts: str) -> None:
        """执法人员只能在本部门权限内处置；主办方等非执法角色不受部门限制。"""
        if self.dept is None:
            return
        if self.dept not in depts:
            raise ApiError(403, "OUT_OF_JURISDICTION",
                           f"该操作仅限部门 {depts}，当前为 {self.dept}")


# ---------------------------------------------------------------------------
# 业务端点
# ---------------------------------------------------------------------------

R = re.compile

ALL: tuple[str, ...] | None = None  # 任意已认证角色
PUBLIC: tuple[str, ...] = ("__PUBLIC__",)  # 免鉴权（仅健康检查）
ORG = ("ORGANIZER",)
OFF = ("OFFICER",)
MER = ("MERCHANT",)
ORG_OFF = ("ORGANIZER", "OFFICER")


# ---- 赛事 / 赛程 / 权益（主办方） -----------------------------------------

def create_event(c: Ctx):
    name, traffic = c.h.require(c.body, "name", "expected_traffic")
    return 201, c.store.create_event(name, c.actor, int(traffic), c.actor)


def get_event(c: Ctx):
    return 200, c.store.get_event(c.p["event_id"])


def add_session(c: Ctx):
    kickoff, ends, venue = c.h.require(c.body, "kickoff_at", "ends_at", "venue")
    return 201, c.store.add_session(c.p["event_id"], kickoff, ends, venue, c.actor)


def list_sessions(c: Ctx):
    return 200, c.store.list_event_sessions(c.p["event_id"])


def flag_weather(c: Ctx):
    (weather,) = c.need("weather_status")
    c.require_dept("EVENT_OPERATION")
    return 200, c.store.flag_weather(c.p["session_id"], weather, c.actor)


def reschedule(c: Ctx):
    new_k, new_e, reason = c.h.require(c.body, "new_kickoff_at", "new_ends_at", "reason")
    c.require_dept("EVENT_OPERATION")
    return 200, c.store.reschedule_session(c.p["session_id"], new_k, new_e, reason, c.actor)


def cancel_session(c: Ctx):
    (reason,) = c.need("reason")
    extreme = bool(c.opt("extreme_weather", False))
    c.require_dept("EVENT_OPERATION")
    return 200, c.store.cancel_session(c.p["session_id"], reason, extreme, c.actor)


def create_benefit(c: Ctx):
    code, title, qty, vf, vt = c.h.require(
        c.body, "code", "title", "total_qty", "valid_from", "valid_to")
    return 201, c.store.create_benefit(c.p["event_id"], code, title, int(qty), vf, vt, c.actor)


def get_benefit(c: Ctx):
    return 200, c.store.get_benefit(c.p["benefit_id"])


def invalidate_benefit(c: Ctx):
    (reason,) = c.need("reason")
    c.require_dept("EVENT_OPERATION")
    return 200, c.store.invalidate_benefit(c.p["benefit_id"], reason, c.actor)


# ---- 商户 -----------------------------------------------------------------

def register_merchant(c: Ctx):
    name, category, district = c.h.require(c.body, "name", "category", "district")
    return 201, c.store.register_merchant(name, category, district, c.actor)


def get_merchant(c: Ctx):
    return 200, c.store.get_merchant(c.p["merchant_id"])


def suspend_merchant(c: Ctx):
    (reason,) = c.need("reason")
    c.require_dept("MARKET_REGULATION")
    return 200, c.store.suspend_merchant(c.p["merchant_id"], reason, c.actor,
                                        officer_dept=c.dept)


def resume_merchant(c: Ctx):
    return 200, c.store.resume_merchant(c.p["merchant_id"], c.actor)


# ---- 承诺 / 报价 ----------------------------------------------------------

def publish_commitment(c: Ctx):
    sku, price, vf, vt, qty = c.h.require(
        c.body, "sku", "price_cents", "valid_from", "valid_to", "fulfill_qty")
    return 201, c.store.publish_commitment(
        c.p["merchant_id"], c.opt("event_id") or c.need("event_id")[0], sku, int(price),
        vf, vt, int(qty), c.actor, session_id=c.opt("session_id"))


def get_commitment(c: Ctx):
    return 200, c.store.get_commitment(c.p["commitment_id"])


def report_price(c: Ctx):
    (price,) = c.need("price_cents")
    return 200, c.store.report_price(c.p["commitment_id"], int(price), c.actor,
                                    source=c.opt("source", "MERCHANT_REPORT"),
                                    at=c.opt("quoted_at"))


def revoke_commitment(c: Ctx):
    (reason,) = c.need("reason")
    c.require_dept("MARKET_REGULATION")
    return 200, c.store.revoke_commitment(c.p["commitment_id"], reason, c.actor,
                                         officer_dept=c.dept)


# ---- 订单 / 核销 ----------------------------------------------------------

def create_order(c: Ctx):
    event_id, merchant_id, channel, amount = c.h.require(
        c.body, "event_id", "merchant_id", "order_channel", "final_amount_cents")
    return 201, c.store.create_order(
        event_id, merchant_id, channel, int(amount), c.actor,
        commitment_id=c.opt("commitment_id"), session_id=c.opt("session_id"),
        idem_key=c.h.headers.get("Idempotency-Key"))


def get_order(c: Ctx):
    return 200, c.store.get_order(c.p["order_id"])


def reject_order(c: Ctx):
    (reason,) = c.need("reason")
    return 200, c.store.reject_order(c.p["order_id"], reason, c.actor)


def fulfill_order(c: Ctx):
    return 200, c.store.fulfill_order(c.p["order_id"], c.actor)


def redeem(c: Ctx):
    channel = c.opt("channel", "API")
    return 201, c.store.redeem(
        actor=c.actor, channel=channel,
        commitment_id=c.opt("commitment_id"), benefit_id=c.opt("benefit_id"),
        order_id=c.opt("order_id"), code=c.opt("code"),
        idem_key=c.h.headers.get("Idempotency-Key"))


# ---- 提前关闭 -------------------------------------------------------------

def report_early_closure(c: Ctx):
    venue, sched, actual = c.h.require(
        c.body, "venue", "scheduled_close_at", "actual_close_at")
    c.require_dept("TOURISM")
    return 201, c.store.report_early_closure(
        c.p["event_id"], c.opt("session_id"), venue, sched, actual, c.actor)


# ---- 投诉 -----------------------------------------------------------------

def open_complaint(c: Ctx):
    event_id, category, reason, channel, bkey = c.h.require(
        c.body, "event_id", "category", "reason", "channel", "business_key")
    return 201, c.store.open_complaint(
        event_id, category, reason, channel, bkey, c.actor,
        reporter_ref=c.opt("reporter_ref"), order_id=c.opt("order_id"),
        idem_key=c.h.headers.get("Idempotency-Key"))


def list_complaints(c: Ctx):
    return 200, c.store.list_complaints(event_id=c.query.get("event_id"),
                                        status=c.query.get("status"))


def get_complaint(c: Ctx):
    return 200, c.store.get_complaint(c.p["complaint_id"])


def handle_complaint(c: Ctx):
    (action,) = c.need("action")
    return 200, c.store.handle_complaint(
        c.p["complaint_id"], action, c.actor,
        to_dept=c.opt("to_dept"), note=c.opt("note"),
        officer_dept=c.dept)


# ---- 处置（退款 / 补偿） ---------------------------------------------------

def propose_remedy(c: Ctx):
    kind, amount = c.h.require(c.body, "kind", "amount_cents")
    return 201, c.store.propose_remedy(
        actor=c.actor, kind=kind, amount_cents=int(amount),
        complaint_id=c.opt("complaint_id"), order_id=c.opt("order_id"),
        category=c.opt("category"), idem_key=c.h.headers.get("Idempotency-Key"),
        officer_dept=c.dept)


def get_remedy(c: Ctx):
    return 200, c.store.get_remedy(c.p["remedy_id"])


def approve_remedy(c: Ctx):
    return 200, c.store.approve_remedy(c.p["remedy_id"], c.actor)


def pay_remedy(c: Ctx):
    return 200, c.store.pay_remedy(c.p["remedy_id"], c.actor,
                                  idem_key=c.h.headers.get("Idempotency-Key"))


def reverse_remedy(c: Ctx):
    return 200, c.store.reverse_remedy(c.p["remedy_id"], c.actor)


def trace_remedy(c: Ctx):
    return 200, c.store.remedy_trace(c.p["remedy_id"])


# ---- 预警 / 政策 / 读模型 --------------------------------------------------

def list_alerts(c: Ctx):
    return 200, c.store.list_alerts(event_id=c.query.get("event_id"),
                                    status=c.query.get("status"))


def get_alert(c: Ctx):
    return 200, c.store.get_alert(int(c.p["alert_id"]))


def ack_alert(c: Ctx):
    return 200, c.store.acknowledge_alert(int(c.p["alert_id"]), c.actor)


def close_alert(c: Ctx):
    return 200, c.store.close_alert(int(c.p["alert_id"]), c.actor)


def create_policy(c: Ctx):
    effective_from, rules = c.h.require(c.body, "effective_from", "rules")
    if not isinstance(rules, dict):
        raise ApiError(400, "BAD_RULES", "rules 必须是对象")
    return 201, c.store.create_policy(effective_from, rules,
                                      c.opt("note", ""), c.actor)


def list_policies(c: Ctx):
    return 200, c.store.list_policies()


def event_impact(c: Ctx):
    return 200, c.store.event_impact(c.p["event_id"])


def event_timeline(c: Ctx):
    return 200, c.store.event_timeline(c.p["event_id"])


def ledger_lookup(c: Ctx):
    return 200, c.store.ledger_for(c.p["aggregate_type"], c.p["aggregate_id"])


def health(c: Ctx):
    return 200, {"status": "ok"}


# ---------------------------------------------------------------------------
# 路由表
# ---------------------------------------------------------------------------

def build_routes() -> list[tuple[str, re.Pattern[str], tuple[str, ...] | None, str | None, Callable]]:
    EID = r"/api/events/(?P<event_id>[A-Za-z0-9_\-]+)"
    SID = r"/api/sessions/(?P<session_id>[A-Za-z0-9_\-]+)"
    MID = r"/api/merchants/(?P<merchant_id>[A-Za-z0-9_\-]+)"
    CID = r"/api/commitments/(?P<commitment_id>[A-Za-z0-9_\-]+)"
    BID = r"/api/benefits/(?P<benefit_id>[A-Za-z0-9_\-]+)"
    OID = r"/api/orders/(?P<order_id>[A-Za-z0-9_\-]+)"
    CPL = r"/api/complaints/(?P<complaint_id>[A-Za-z0-9_\-]+)"
    RID = r"/api/remedies/(?P<remedy_id>[A-Za-z0-9_\-]+)"
    AID = r"/api/alerts/(?P<alert_id>\d+)"

    spec = [
        # 健康检查（免鉴权）
        ("GET", R(r"/healthz"), PUBLIC, None, health),
        # 赛事 / 赛程
        ("POST", R(r"/api/events"), ORG, None, create_event),
        ("GET", R(EID + r"$"), ALL, None, get_event),
        ("POST", R(EID + r"/sessions$"), ORG, None, add_session),
        ("GET", R(EID + r"/sessions$"), ALL, None, list_sessions),
        ("GET", R(EID + r"/impact$"), ALL, None, event_impact),
        ("GET", R(EID + r"/timeline$"), ALL, None, event_timeline),
        ("POST", R(EID + r"/benefits$"), ORG, None, create_benefit),
        ("POST", R(EID + r"/early-closures$"), ORG_OFF, None, report_early_closure),
        ("POST", R(SID + r"/weather$"), ORG_OFF, None, flag_weather),
        ("POST", R(SID + r"/reschedule$"), ORG_OFF, None, reschedule),
        ("POST", R(SID + r"/cancel$"), ORG_OFF, None, cancel_session),
        # 商户
        ("POST", R(r"/api/merchants$"), ORG_OFF, None, register_merchant),
        ("GET", R(MID + r"$"), ALL, None, get_merchant),
        ("POST", R(MID + r"/suspend$"), OFF, None, suspend_merchant),
        ("POST", R(MID + r"/resume$"), OFF, None, resume_merchant),
        ("POST", R(MID + r"/commitments$"), MER, None, publish_commitment),
        # 承诺
        ("GET", R(CID + r"$"), ALL, None, get_commitment),
        ("POST", R(CID + r"/prices$"), MER, None, report_price),
        ("POST", R(CID + r"/revoke$"), MER + OFF, None, revoke_commitment),
        # 权益
        ("GET", R(BID + r"$"), ALL, None, get_benefit),
        ("POST", R(BID + r"/invalidate$"), ORG_OFF, None, invalidate_benefit),
        # 订单 / 核销
        ("POST", R(r"/api/orders$"), ALL, "order", create_order),
        ("GET", R(OID + r"$"), ALL, None, get_order),
        ("POST", R(OID + r"/reject$"), MER, None, reject_order),
        ("POST", R(OID + r"/fulfill$"), MER + ORG_OFF, None, fulfill_order),
        ("POST", R(r"/api/redemptions$"), ALL, "redemption", redeem),
        # 投诉
        ("POST", R(r"/api/complaints$"), ALL, "complaint", open_complaint),
        ("GET", R(r"/api/complaints$"), ALL, None, list_complaints),
        ("GET", R(CPL + r"$"), ALL, None, get_complaint),
        ("POST", R(CPL + r"/handle$"), ORG_OFF, None, handle_complaint),
        # 处置
        ("POST", R(r"/api/remedies$"), OFF, "remedy", propose_remedy),
        ("GET", R(r"/api/remedies$"), ALL, None, lambda c: (
            404, {"error": {"code": "NOT_FOUND", "message": "请按 id 查询单笔处置"}})),
        ("GET", R(RID + r"$"), ALL, None, get_remedy),
        ("GET", R(RID + r"/trace$"), ALL, None, trace_remedy),
        ("POST", R(RID + r"/approve$"), OFF, None, approve_remedy),
        ("POST", R(RID + r"/pay$"), OFF, "remedy-pay", pay_remedy),
        ("POST", R(RID + r"/reverse$"), OFF, None, reverse_remedy),
        # 预警 / 政策 / 账本
        ("GET", R(r"/api/alerts$"), ALL, None, list_alerts),
        ("GET", R(AID + r"$"), ALL, None, get_alert),
        ("POST", R(AID + r"/ack$"), ORG_OFF, None, ack_alert),
        ("POST", R(AID + r"/close$"), ORG_OFF, None, close_alert),
        ("POST", R(r"/api/policies$"), OFF, None, create_policy),
        ("GET", R(r"/api/policies$"), ALL, None, list_policies),
        ("GET", R(r"/api/ledger/(?P<aggregate_type>[a-z_]+)/(?P<aggregate_id>[A-Za-z0-9_\-]+)$"),
         ALL, None, ledger_lookup),
    ]
    return spec


GuardHandler.routes = build_routes()


# ---------------------------------------------------------------------------
# Server
# ---------------------------------------------------------------------------


class GuardHTTPServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, addr: tuple[str, int], store: GuardStore, quiet: bool = False):
        self.store = store
        self.quiet = quiet
        super().__init__(addr, GuardHandler)


def serve(host: str = "127.0.0.1", port: int = 8080, db_path: str = ":memory:",
          quiet: bool = False) -> GuardHTTPServer:
    store = GuardStore(db_path)
    httpd = GuardHTTPServer((host, port), store, quiet=quiet)
    return httpd
