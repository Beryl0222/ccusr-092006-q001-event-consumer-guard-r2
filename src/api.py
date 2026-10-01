"""HTTP API 层（标准库 http.server，无第三方依赖）。

- Bearer 令牌鉴权：Authorization: Bearer <token>
- 写接口支持 Idempotency-Key 请求头：同键重放首次响应
- 错误体统一为 {"error": {"code": ..., "message": ...}}
"""

from __future__ import annotations

import json
import re
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

from . import domain
from .db import Database, now_iso
from .services import GuardService


class GuardHandler(BaseHTTPRequestHandler):
    server_version = "EventConsumerGuard/1.0"
    db: Database = None  # 由 make_server 注入

    # -- 基础工具 ----------------------------------------------------------

    def _send(self, status: int, body: dict | list, *, extra_headers=None) -> None:
        raw = json.dumps(body, ensure_ascii=False, default=str).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        for k, v in (extra_headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(raw)

    def _send_error(self, exc: Exception) -> None:
        if isinstance(exc, domain.GuardError):
            status, code = exc.http_status, exc.code
        else:
            status, code = 500, "internal_error"
        self._send(status, {"error": {"code": code, "message": str(exc)}})

    def _read_json(self) -> dict:
        length = int(self.headers.get("Content-Length") or 0)
        if length == 0:
            return {}
        try:
            data = json.loads(self.rfile.read(length).decode("utf-8"))
        except json.JSONDecodeError as exc:
            raise domain.GuardError(f"请求体不是合法 JSON: {exc}")
        if not isinstance(data, dict):
            raise domain.GuardError("请求体必须是 JSON 对象")
        return data

    def _token(self) -> str | None:
        raw = self.headers.get("Authorization", "")
        if raw.startswith("Bearer "):
            return raw[7:].strip()
        return None

    def log_message(self, fmt, *args):  # 安静一点，异常由统一错误体表达
        if self.server.verbose:  # type: ignore[attr-defined]
            super().log_message(fmt, *args)

    # -- 幂等层 ------------------------------------------------------------

    def _idempotency_lookup(self, key: str, method: str, path: str):
        row = self.db.query_one(
            "SELECT response_status, response_body FROM idempotency_keys"
            " WHERE idem_key=?", (key,),
        )
        if row is None:
            return None
        return row["response_status"], json.loads(row["response_body"])

    def _idempotency_store(self, key: str, method: str, path: str,
                           status: int, body: dict | list) -> None:
        with self.db.transaction() as con:
            con.execute(
                "INSERT OR IGNORE INTO idempotency_keys(idem_key, method, path,"
                " response_status, response_body, created_at) VALUES (?,?,?,?,?,?)",
                (key, method, path, status,
                 json.dumps(body, ensure_ascii=False, default=str), now_iso()),
            )

    # -- 路由 --------------------------------------------------------------

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        path, query = parsed.path, parse_qs(parsed.query)
        svc = GuardService(self.db)
        try:
            if path == "/healthz":
                self._send(200, {"status": "ok"})
                return
            token = self._token()
            svc.authenticate(token)  # 只读接口也需登录
            self._route_get(svc, path, query)
        except Exception as exc:  # noqa: BLE001 - 统一错误出口
            self._send_error(exc)

    def do_POST(self) -> None:
        parsed = urlparse(self.path)
        path = parsed.path
        svc = GuardService(self.db)
        try:
            data = self._read_json()
            token = self._token()
            idem = self.headers.get("Idempotency-Key")
            if idem:
                cached = self._idempotency_lookup(idem, "POST", path)
                if cached is not None:
                    status, body = cached
                    self._send(status, body,
                               extra_headers={"Idempotent-Replayed": "true"})
                    return
            status, body = self._route_post(svc, path, data, token)
            if idem:
                self._idempotency_store(idem, "POST", path, status, body)
            self._send(status, body)
        except Exception as exc:  # noqa: BLE001
            self._send_error(exc)

    # -- GET 路由表 --------------------------------------------------------

    def _route_get(self, svc: GuardService, path: str, query: dict) -> None:
        def q(name: str) -> str | None:
            return query.get(name, [None])[0]

        m = re.fullmatch(r"/v1/events/([^/]+)", path)
        if m:
            self._send(200, svc.get_event(m.group(1))); return
        m = re.fullmatch(r"/v1/events/([^/]+)/impact", path)
        if m:
            self._send(200, svc.event_impact_report(m.group(1))); return
        m = re.fullmatch(r"/v1/events/([^/]+)/timeline", path)
        if m:
            self._send(200, {"timeline": svc.event_timeline(m.group(1))}); return
        m = re.fullmatch(r"/v1/merchants/([^/]+)", path)
        if m:
            self._send(200, svc.get_merchant(m.group(1))); return
        m = re.fullmatch(r"/v1/benefits/([^/]+)", path)
        if m:
            self._send(200, svc.get_benefit(m.group(1))); return
        m = re.fullmatch(r"/v1/commitments/([^/]+)", path)
        if m:
            self._send(200, svc.get_commitment(m.group(1))); return
        m = re.fullmatch(r"/v1/orders/([^/]+)", path)
        if m:
            self._send(200, svc.get_order(m.group(1))); return
        m = re.fullmatch(r"/v1/complaints/([^/]+)", path)
        if m:
            self._send(200, svc.get_complaint(m.group(1))); return
        m = re.fullmatch(r"/v1/dispositions/([^/]+)/reconcile", path)
        if m:
            self._send(200, svc.reconcile_bearer(m.group(1))); return
        m = re.fullmatch(r"/v1/chain/([^/]+)/([^/]+)", path)
        if m:
            self._send(200, {"chain": svc.chain_of(m.group(1), m.group(2))}); return
        if path == "/v1/alerts":
            self._send(200, {"alerts": svc.list_alerts(q("event_id"), q("type"))})
            return
        if path == "/v1/policies":
            self._send(200, {"policies": svc.list_policies()}); return
        self._send(404, {"error": {"code": "not_found", "message": f"无此路径: {path}"}})

    # -- POST 路由表（返回 (status, body)） ---------------------------------

    def _route_post(self, svc: GuardService, path: str, data: dict,
                    token: str | None) -> tuple[int, dict | list]:
        # 赛事
        if path == "/v1/events":
            return 201, svc.register_event(token, data)
        m = re.fullmatch(r"/v1/events/([^/]+)/reschedule", path)
        if m:
            return 200, svc.reschedule_event(
                token, m.group(1), data["new_starts_at"], data["new_ends_at"],
                data.get("reason", ""), data.get("weather_level", "normal"))
        m = re.fullmatch(r"/v1/events/([^/]+)/cancel", path)
        if m:
            return 200, svc.cancel_event(
                token, m.group(1), data.get("reason", ""),
                data.get("weather_level", "normal"))

        # 商户
        if path == "/v1/merchants":
            return 201, svc.register_merchant(token, data)
        m = re.fullmatch(r"/v1/merchants/([^/]+)/suspend", path)
        if m:
            return 200, svc.suspend_merchant(token, m.group(1),
                                             data.get("reason", ""))
        m = re.fullmatch(r"/v1/merchants/([^/]+)/unsuspend", path)
        if m:
            return 200, svc.unsuspend_merchant(token, m.group(1),
                                               data.get("note", ""))

        # 权益与承诺
        if path == "/v1/benefits":
            return 201, svc.publish_benefit(token, data)
        if path == "/v1/commitments":
            return 201, svc.publish_commitment(token, data)

        # 订单与核销
        if path == "/v1/orders":
            return 201, svc.create_order(token, data)
        m = re.fullmatch(r"/v1/orders/([^/]+)/reject", path)
        if m:
            return 200, svc.reject_order(token, m.group(1), data.get("reason", ""))
        if path == "/v1/redemptions":
            return 201, svc.redeem(token, data)
        m = re.fullmatch(r"/v1/redemptions/([^/]+)/reverse", path)
        if m:
            return 200, svc.reverse_redemption(token, m.group(1),
                                               data.get("reason", ""))

        # 投诉、调查、申诉、处置
        if path == "/v1/complaints":
            return 201, svc.ingest_complaint(token, data)
        m = re.fullmatch(r"/v1/complaints/([^/]+)/investigations", path)
        if m:
            return 201, svc.record_investigation(
                token, m.group(1), data.get("note", ""),
                data.get("evidence"), data.get("root_cause"))
        m = re.fullmatch(r"/v1/complaints/([^/]+)/appeals", path)
        if m:
            return 201, svc.submit_appeal(token, m.group(1),
                                          data.get("reason", ""),
                                          data.get("evidence"))
        m = re.fullmatch(r"/v1/complaints/([^/]+)/refunds", path)
        if m:
            return 201, svc.settle_disposition(
                token, m.group(1), domain.DispositionKind.REFUND,
                data["amount"], root_cause=data["root_cause"],
                transaction_at=data.get("transaction_at"))
        m = re.fullmatch(r"/v1/complaints/([^/]+)/compensations", path)
        if m:
            return 201, svc.settle_disposition(
                token, m.group(1), domain.DispositionKind.COMPENSATION,
                data["amount"], root_cause=data["root_cause"],
                transaction_at=data.get("transaction_at"))
        m = re.fullmatch(r"/v1/complaints/([^/]+)/close", path)
        if m:
            return 200, svc.close_complaint(token, m.group(1),
                                            data.get("note", ""))
        m = re.fullmatch(r"/v1/appeals/(\d+)/ruling", path)
        if m:
            return 200, svc.rule_appeal(token, int(m.group(1)),
                                        bool(data["uphold"]),
                                        data.get("note", ""))

        # 预警
        m = re.fullmatch(r"/v1/alerts/([^/]+)/acknowledge", path)
        if m:
            return 200, svc.acknowledge_alert(token, m.group(1),
                                              data.get("note", ""))

        return 404, {"error": {"code": "not_found", "message": f"无此路径: {path}"}}


def make_server(host: str, port: int, db_path: str, *, verbose: bool = False):
    db = Database(db_path)

    class _Handler(GuardHandler):
        pass

    _Handler.db = db
    httpd = ThreadingHTTPServer((host, port), _Handler)
    httpd.verbose = verbose  # type: ignore[attr-defined]
    httpd.db = db            # type: ignore[attr-defined]
    return httpd
