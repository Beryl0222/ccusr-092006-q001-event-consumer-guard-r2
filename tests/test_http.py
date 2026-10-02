"""HTTP 端到端测试：真实端口 + http.client，覆盖 RBAC、幂等重放与联防全流程。"""

import http.client
import json
import threading
import unittest

import support  # noqa: F401

from guard.db import GuardStore
from guard.server import GuardHTTPServer

VF = "2026-10-03T12:00:00+08:00"
VT = "2026-10-05T12:00:00+08:00"
DURING = "2026-10-04T20:00:00+08:00"


class ApiFixture(unittest.TestCase):
    def setUp(self):
        self.store = GuardStore(":memory:")
        self.httpd = GuardHTTPServer(("127.0.0.1", 0), self.store, quiet=True)
        self.port = self.httpd.server_address[1]
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.httpd.shutdown()
        self.httpd.server_close()
        self.thread.join(timeout=3)
        self.store.close()

    def call(self, method: str, path: str, body=None, *, actor="u1", role="OFFICER",
             dept=None, idem_key=None, expect=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        headers = {"Content-Type": "application/json"}
        if actor is not None:
            headers["X-Actor-Id"] = actor
            headers["X-Actor-Role"] = role
        if dept:
            headers["X-Actor-Dept"] = dept
        if idem_key:
            headers["Idempotency-Key"] = idem_key
        payload = json.dumps(body, ensure_ascii=False).encode("utf-8") if body is not None else None
        conn.request(method, path, body=payload, headers=headers)
        resp = conn.getresponse()
        raw = resp.read().decode("utf-8")
        conn.close()
        data = json.loads(raw) if raw else {}
        if expect is not None:
            self.assertEqual(resp.status, expect, f"{method} {path} -> {resp.status}: {data}")
        return resp.status, dict(resp.getheaders()), data


class AuthTest(ApiFixture):
    def test_health_is_public(self):
        status, _, data = self.call("GET", "/healthz", actor=None, role=None)
        self.assertEqual(status, 200)

    def test_missing_identity_rejected(self):
        status, _, data = self.call("GET", "/api/policies", actor=None, role=None, expect=401)
        self.assertEqual(data["error"]["code"], "UNAUTHENTICATED")

    def test_role_boundary(self):
        body = {"name": "联赛", "expected_traffic": 100}
        self.call("POST", "/api/events", body, actor="m", role="MERCHANT", expect=403)
        status, _, ev = self.call("POST", "/api/events", body, actor="o", role="ORGANIZER",
                                  expect=201)
        self.assertEqual(ev["status"], "SCHEDULED")

    def test_officer_must_declare_department(self):
        self.call("POST", "/api/merchants",
                  {"name": "店", "category": "DINING", "district": "天河区"},
                  actor="t", role="OFFICER", expect=403)

    def test_department_jurisdiction_over_http(self):
        _, _, mer = self.call("POST", "/api/merchants",
                              {"name": "酒店", "category": "LODGING", "district": "天河区"},
                              actor="o", role="ORGANIZER", expect=201)
        # 文旅部门无权执行停业
        self.call("POST", f"/api/merchants/{mer['id']}/suspend", {"reason": "x"},
                  dept="TOURISM", expect=403)
        # 市监可以
        self.call("POST", f"/api/merchants/{mer['id']}/suspend", {"reason": "临时加价"},
                  dept="MARKET_REGULATION", expect=200)


class IdempotencyHttpTest(ApiFixture):
    def _world(self):
        _, _, ev = self.call("POST", "/api/events",
                             {"name": "联赛", "expected_traffic": 100},
                             actor="o", role="ORGANIZER", expect=201)
        _, _, mer = self.call("POST", "/api/merchants",
                              {"name": "店", "category": "DINING", "district": "d"},
                              actor="o", role="ORGANIZER", expect=201)
        return ev, mer

    def test_same_key_replays_first_response(self):
        ev, mer = self._world()
        body = {"event_id": ev["id"], "merchant_id": mer["id"], "order_channel": "APP",
                "final_amount_cents": 19900}
        s1, h1, o1 = self.call("POST", "/api/orders", body, idem_key="ORD-1", expect=201)
        s2, h2, o2 = self.call("POST", "/api/orders", body, idem_key="ORD-1", expect=201)
        self.assertEqual(o1["id"], o2["id"])
        self.assertEqual(h2.get("Idempotent-Replay"), "true")
        self.assertIsNone(h1.get("Idempotent-Replay"))

    def test_same_key_different_body_conflicts(self):
        ev, mer = self._world()
        body1 = {"event_id": ev["id"], "merchant_id": mer["id"], "order_channel": "APP",
                 "final_amount_cents": 19900}
        body2 = dict(body1, final_amount_cents=1)
        self.call("POST", "/api/orders", body1, idem_key="ORD-2", expect=201)
        status, _, data = self.call("POST", "/api/orders", body2, idem_key="ORD-2", expect=422)
        self.assertEqual(data["error"]["code"], "IDEMPOTENCY_CONFLICT")

    def test_concurrent_same_key_single_order(self):
        ev, mer = self._world()
        body = {"event_id": ev["id"], "merchant_id": mer["id"], "order_channel": "APP",
                "final_amount_cents": 19900}
        results = []
        errors = []

        def worker():
            try:
                status, _, data = self.call("POST", "/api/orders", body, idem_key="ORD-3")
                results.append((status, data.get("id")))
            except Exception as e:  # noqa: BLE001
                errors.append(e)

        threads = [threading.Thread(target=worker) for _ in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(errors, [])
        self.assertTrue(all(s == 201 for s, _ in results))
        self.assertEqual(len({oid for _, oid in results}), 1)


class FullScenarioHttpTest(ApiFixture):
    def test_weekend_league_incident_end_to_end(self):
        # 1. 主办方登记赛程 / 客流 / 权益
        _, _, ev = self.call("POST", "/api/events",
                             {"name": "周末城市德比", "expected_traffic": 12000},
                             actor="org", role="ORGANIZER", expect=201)
        _, _, ses = self.call("POST", f"/api/events/{ev['id']}/sessions",
                              {"kickoff_at": "2026-10-04T19:30:00+08:00",
                               "ends_at": "2026-10-04T21:30:00+08:00",
                               "venue": "市体育中心"},
                              actor="org", role="ORGANIZER", expect=201)
        _, _, ben = self.call("POST", f"/api/events/{ev['id']}/benefits",
                              {"code": "FAN50", "title": "球迷消费券", "total_qty": 100,
                               "valid_from": VF, "valid_to": VT},
                              actor="org", role="ORGANIZER", expect=201)

        # 2. 商户发布带有效期的价格与履约承诺
        _, _, hotel = self.call("POST", "/api/merchants",
                                {"name": "球场旁酒店", "category": "LODGING",
                                 "district": "天河区"},
                                actor="org", role="ORGANIZER", expect=201)
        _, _, room = self.call("POST", f"/api/merchants/{hotel['id']}/commitments",
                               {"event_id": ev["id"], "session_id": ses["id"], "sku": "大床房",
                                "price_cents": 30000, "valid_from": VF, "valid_to": VT,
                                "fulfill_qty": 20},
                               actor="hotel", role="MERCHANT", expect=201)

        # 3. 散场后临时加价 -> 可解释价格预警
        self.call("POST", f"/api/commitments/{room['id']}/prices",
                  {"price_cents": 48000, "quoted_at": DURING},
                  actor="hotel", role="MERCHANT", expect=200)
        _, _, alerts = self.call("GET", f"/api/alerts?event_id={ev['id']}")
        spike = next(a for a in alerts if a["type"] == "PRICE_SPIKE")
        self.assertEqual(spike["evidence"]["new_cents"], 48000)
        self.call("POST", f"/api/alerts/{spike['id']}/ack", dept="MARKET_REGULATION", expect=200)

        # 4. 消费者多渠道投诉同一订单 -> 只立一案
        order_body = {"event_id": ev["id"], "merchant_id": hotel["id"],
                      "order_channel": "MEITUAN", "final_amount_cents": 48000,
                      "commitment_id": room["id"], "session_id": ses["id"]}
        _, _, order = self.call("POST", "/api/orders", order_body, idem_key="O-1", expect=201)
        cbody = {"event_id": ev["id"], "order_id": order["id"], "category": "PRICE_GOUGE",
                 "reason": "散场后到店被要求补差价", "channel": "12345",
                 "business_key": f"case:{order['id']}"}
        _, _, c1 = self.call("POST", "/api/complaints", cbody, expect=201)
        cbody2 = dict(cbody, channel="WECHAT", reason="微信端重复投诉")
        _, _, c2 = self.call("POST", "/api/complaints", cbody2, expect=201)
        self.assertEqual(c1["id"], c2["id"])
        self.assertTrue(c2["duplicate"])

        # 5. 跨部门推诿被管辖规则拦住，再走转交
        self.call("POST", f"/api/complaints/{c1['id']}/handle",
                  {"action": "INVESTIGATE"}, dept="TOURISM", expect=403)
        self.call("POST", f"/api/complaints/{c1['id']}/handle",
                  {"action": "INVESTIGATE"}, dept="MARKET_REGULATION", expect=200)

        # 6. 市监发起退款，重复发起被拒；核准 -> 支付
        _, _, rem = self.call("POST", "/api/remedies",
                              {"complaint_id": c1["id"], "kind": "REFUND",
                               "amount_cents": 18000},
                              dept="MARKET_REGULATION", idem_key="R-1", expect=201)
        self.call("POST", "/api/remedies",
                  {"complaint_id": c1["id"], "kind": "REFUND", "amount_cents": 18000},
                  dept="MARKET_REGULATION", expect=409)
        self.call("POST", f"/api/remedies/{rem['id']}/approve",
                  dept="MARKET_REGULATION", expect=200)
        self.call("POST", f"/api/remedies/{rem['id']}/pay",
                  dept="MARKET_REGULATION", idem_key="PAY-1", expect=200)
        # 支付重放
        _, h, paid2 = self.call("POST", f"/api/remedies/{rem['id']}/pay",
                                dept="MARKET_REGULATION", idem_key="PAY-1", expect=200)
        self.assertEqual(paid2["status"], "PAID")

        # 7. 接口核对最终承担方与完整事件链
        _, _, trace = self.call("GET", f"/api/remedies/{rem['id']}/trace", expect=200)
        self.assertEqual(trace["final_bearer"]["type"], "MERCHANT")
        self.assertEqual(trace["final_bearer"]["id"], hotel["id"])
        chain = [x["event_type"] for x in trace["event_chain"]]
        self.assertEqual(chain, ["REMEDY_PROPOSED", "REMEDY_APPROVED", "REMEDY_SETTLED"])

        # 8. 极端气象改期 / 取消：权益失效并告警
        self.call("POST", f"/api/sessions/{ses['id']}/weather",
                  {"weather_status": "EXTREME_WEATHER"},
                  actor="ops", role="ORGANIZER", expect=200)
        _, _, cancel = self.call("POST", f"/api/sessions/{ses['id']}/cancel",
                                 {"reason": "台风红色预警", "extreme_weather": True},
                                 actor="ops", role="ORGANIZER", expect=200)
        self.assertEqual(cancel["status"], "CANCELLED")
        _, _, ben2 = self.call("GET", f"/api/benefits/{ben['id']}")
        self.assertEqual(ben2["status"], "INVALIDATED")

        # 9. 第二现场提前关闭（文旅登记，超宽限 -> 预警）
        _, _, ec = self.call("POST", f"/api/events/{ev['id']}/early-closures",
                             {"session_id": ses["id"], "venue": "天河路商圈第二现场",
                              "scheduled_close_at": "2026-10-04T23:00:00+08:00",
                              "actual_close_at": "2026-10-04T20:30:00+08:00"},
                             dept="TOURISM", expect=201)
        self.assertEqual(ec["alert"]["type"], "EARLY_CLOSURE")

        # 10. 值班人员按赛事还原消费影响
        _, _, impact = self.call("GET", f"/api/events/{ev['id']}/impact", expect=200)
        self.assertEqual(impact["complaints"]["total"], 1)
        self.assertEqual(impact["remedies"]["paid_cents"], 18000)
        self.assertEqual(impact["remedies"]["paid_by_bearer_type"]["MERCHANT"], 18000)
        self.assertIn("BENEFIT_INVALIDATION", impact["alerts"]["by_type"])
        self.assertIn("EARLY_CLOSURE", impact["alerts"]["by_type"])

        _, _, timeline = self.call("GET", f"/api/events/{ev['id']}/timeline", expect=200)
        self.assertGreater(len(timeline), 15)
        types = {x["event_type"] for x in timeline}
        self.assertIn("SESSION_CANCELLED", types)
        self.assertIn("BENEFIT_INVALIDATED", types)


if __name__ == "__main__":
    unittest.main()
