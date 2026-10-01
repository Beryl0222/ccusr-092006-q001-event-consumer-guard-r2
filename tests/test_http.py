"""HTTP API 集成测试：鉴权、幂等键重放、并发核销走 HTTP。"""

import json
import threading
import unittest
import urllib.error
import urllib.request
from http.client import RemoteDisconnected

from src.api import make_server

ORG, MALL, TOUR = "org-token", "mer-mall-token", "tour-token"


class HttpFixture(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.httpd = make_server("127.0.0.1", 0, ":memory:")
        cls.port = cls.httpd.server_address[1]
        cls.thread = threading.Thread(target=cls.httpd.serve_forever,
                                     daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.httpd.db.close()
        cls.thread.join(timeout=3)

    def call(self, method, path, body=None, token=None,
             idem_key=None, expect_status=None):
        url = f"http://127.0.0.1:{self.port}{path}"
        data = json.dumps(body).encode() if body is not None else None
        headers = {"Content-Type": "application/json"}
        if token:
            headers["Authorization"] = f"Bearer {token}"
        if idem_key:
            headers["Idempotency-Key"] = idem_key
        req = urllib.request.Request(url, data=data, headers=headers,
                                     method=method)
        try:
            with urllib.request.urlopen(req) as resp:
                payload = json.loads(resp.read().decode())
                status = resp.status
                replay = resp.headers.get("Idempotent-Replayed")
        except urllib.error.HTTPError as exc:
            payload = json.loads(exc.read().decode())
            status = exc.code
            replay = exc.headers.get("Idempotent-Replayed")
        if expect_status is not None:
            self.assertEqual(status, expect_status, payload)
        return status, payload, replay

    def setUp(self):
        # 每个用例使用独立赛事，互不干扰；商户在整个服务实例内只注册一次。
        self.eid = f"e-{self._testMethodName}"
        self.call("POST", "/v1/events", {
            "event_id": self.eid, "name": self._testMethodName,
            "starts_at": "2026-10-03T15:00:00+08:00",
            "ends_at": "2026-10-03T22:00:00+08:00",
            "expected_attendance": 100}, token=ORG, expect_status=201)
        try:
            self.call("POST", "/v1/merchants", {
                "merchant_id": "m-mall", "name": "第二现场",
                "sector": "culture_tourism"}, token=ORG, expect_status=201)
        except AssertionError:
            pass  # 已被前一个用例注册

    def _ensure_mall(self):
        return  # setUp 已确保


class AuthAndIdempotencyTest(HttpFixture):
    def test_healthz_open(self):
        status, body, _ = self.call("GET", "/healthz")
        self.assertEqual(status, 200)

    def test_missing_token_401(self):
        status, body, _ = self.call("GET", "/v1/policies", expect_status=401)
        self.assertEqual(body["error"]["code"], "unauthorized")

    def test_bad_token_401(self):
        self.call("GET", "/v1/policies", token="nope", expect_status=401)

    def test_forbidden_403(self):
        status, body, _ = self.call(
            "POST", "/v1/merchants/m-mall/suspend",
            {"reason": "x"}, token=ORG, expect_status=403)
        self.assertEqual(body["error"]["code"], "forbidden")

    def test_idempotency_key_replays_response(self):
        # 首次请求登记新赛事，同键重放必须返回同一响应（即便请求体已变）。
        key = f"idem-{self.eid}"
        fresh = f"{self.eid}-fresh"
        s1, b1, r1 = self.call("POST", "/v1/events", {
            "event_id": fresh, "name": "fresh",
            "starts_at": "2026-10-03T15:00:00+08:00",
            "ends_at": "2026-10-03T22:00:00+08:00"}, token=ORG, idem_key=key,
            expect_status=201)
        self.assertIsNone(r1)
        s2, b2, r2 = self.call("POST", "/v1/events", {
            "event_id": f"{fresh}-changed", "name": "changed",
            "starts_at": "2026-10-03T15:00:00+08:00",
            "ends_at": "2026-10-03T22:00:00+08:00"}, token=ORG, idem_key=key,
            expect_status=201)
        self.assertEqual(r2, "true")
        self.assertEqual(b1["event_id"], b2["event_id"])

    def test_not_found_json(self):
        status, body, _ = self.call("GET", "/v1/no-such/path", token=TOUR)
        self.assertEqual(status, 404)


class ConcurrentHttpRedeemTest(HttpFixture):
    def test_concurrent_http_redeem_respects_quota(self):
        self._ensure_mall()
        self.call("POST", "/v1/commitments", {
            "commitment_id": f"cm-{self.eid}", "event_id": self.eid,
            "kind": "package", "spec": {"name": "套餐"},
            "promised_total": 3,
            "valid_from": "2026-10-01T00:00:00+08:00",
            "valid_to": "2026-12-31T00:00:00+08:00"}, token=MALL,
            expect_status=201)
        order_ids = []
        for i in range(10):
            _, body, _ = self.call("POST", "/v1/orders", {
                "event_id": self.eid, "channel": "app",
                "channel_ref": f"{self.eid}-{i}", "amount": 50,
                "commitment_id": f"cm-{self.eid}"}, token=MALL,
                expect_status=201)
            order_ids.append(body["order_id"])

        outcomes = {"ok": [], "fail": []}
        lock = threading.Lock()

        def redeem(oid):
            status, body, _ = self.call("POST", "/v1/redemptions", {
                "order_id": oid, "commitment_id": f"cm-{self.eid}",
                "qty": 1, "channel": "app"}, token=MALL)
            with lock:
                (outcomes["ok"] if status == 201 else outcomes["fail"]).append(oid)

        threads = [threading.Thread(target=redeem, args=(o,)) for o in order_ids]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(len(outcomes["ok"]), 3)
        self.assertEqual(len(outcomes["fail"]), 7)
        _, com, _ = self.call("GET", f"/v1/commitments/cm-{self.eid}", token=MALL)
        self.assertEqual(com["redeemed_total"], 3)


if __name__ == "__main__":
    unittest.main()
