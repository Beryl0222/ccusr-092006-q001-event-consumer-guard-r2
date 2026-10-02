"""存储层业务规则测试：幂等、并发、政策时效、事件链。"""

import threading
import unittest

import support  # noqa: F401

from guard.db import GuardError, GuardStore

VF = "2026-10-03T12:00:00+08:00"
VT = "2026-10-05T12:00:00+08:00"
DURING = "2026-10-04T20:00:00+08:00"
FUTURE = "2026-11-01T20:00:00+08:00"


def make_world(store: GuardStore):
    ev = store.create_event("周末联赛", "org-1", 6000, "organizer:org-1")
    ses = store.add_session(ev["id"], "2026-10-04T19:30:00+08:00",
                            "2026-10-04T21:30:00+08:00", "市体育中心", "organizer:org-1")
    hotel = store.register_merchant("加价酒店", "LODGING", "天河区", "mer:hotel")
    rest = store.register_merchant("排队餐厅", "DINING", "天河区", "mer:rest")
    room = store.publish_commitment(hotel["id"], ev["id"], "大床房", 30000, VF, VT, 3, "mer:hotel")
    meal = store.publish_commitment(rest["id"], ev["id"], "双人套餐", 19900, VF, VT, 50, "mer:rest")
    ben = store.create_benefit(ev["id"], "FAN100", "球迷满减券", 20, VF, VT, "organizer:org-1")
    return ev, ses, hotel, rest, room, meal, ben


class StoreFixture(unittest.TestCase):
    def setUp(self):
        self.store = GuardStore(":memory:")

    def tearDown(self):
        self.store.close()


class RedemptionConcurrencyTest(StoreFixture):
    def test_concurrent_redeem_never_exceeds_committed_qty(self):
        ev, ses, hotel, rest, room, meal, ben = make_world(self.store)

        results: list[str] = []
        lock = threading.Lock()

        def worker():
            r = self.store.redeem(actor="fan", channel="APP", commitment_id=room["id"], at=DURING)
            with lock:
                results.append(r["result"])

        threads = [threading.Thread(target=worker) for _ in range(30)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        self.assertEqual(results.count("SUCCESS"), 3)
        self.assertEqual(results.count("REJECTED"), 27)
        self.assertEqual(self.store.get_commitment(room["id"])["redeemed_qty"], 3)

    def test_concurrent_same_idempotency_key_redeems_once(self):
        ev, ses, hotel, rest, room, meal, ben = make_world(self.store)

        def worker(out, idx):
            r = self.store.redeem(actor="fan", channel="APP", commitment_id=meal["id"],
                                  at=DURING, idem_key="SAME-KEY")
            out[idx] = (r["id"], r["result"], r.get("replayed"))

        out: dict = {}
        threads = [threading.Thread(target=worker, args=(out, i)) for i in range(10)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        ids = {v[0] for v in out.values()}
        self.assertEqual(len(ids), 1)  # 只有一条核销记录
        self.assertEqual(self.store.get_commitment(meal["id"])["redeemed_qty"], 1)

    def test_concurrent_order_same_idem_key_creates_one_order(self):
        ev, ses, hotel, rest, room, meal, ben = make_world(self.store)
        out: list[str] = []
        lock = threading.Lock()

        def worker():
            o = self.store.create_order(ev["id"], hotel["id"], "MEITUAN", 30000,
                                        "fan", commitment_id=room["id"], idem_key="ORDER-KEY")
            with lock:
                out.append(o["id"])

        threads = [threading.Thread(target=worker) for _ in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(len(set(out)), 1)

    def test_redeem_rules(self):
        ev, ses, hotel, rest, room, meal, ben = make_world(self.store)
        # 权益过期
        r = self.store.redeem(actor="f", channel="K", benefit_id=ben["id"],
                              at="2026-10-06T00:00:00+08:00")
        self.assertEqual(r["result"], "REJECTED")
        self.assertEqual(r["reason"], "OUTSIDE_VALID_WINDOW")
        # 失效后不可用
        self.store.invalidate_benefit(ben["id"], "系统维护", "organizer:org-1")
        r = self.store.redeem(actor="f", channel="K", benefit_id=ben["id"], at=DURING)
        self.assertEqual(r["reason"], "BENEFIT_INVALIDATED")
        self.assertEqual(self.store.get_benefit(ben["id"])["redeemed_qty"], 0)


class ComplaintDedupAndRemedyTest(StoreFixture):
    def _open_case(self, ev, order, category="PRICE_GOUGE", key_extra=""):
        return self.store.open_complaint(
            ev["id"], category, "散场后酒店临时加价", "12345热线",
            business_key=f"{order['id']}:{category}{key_extra}",
            actor="fan", order_id=order["id"])

    def test_multi_channel_retransmission_dedupes(self):
        ev, ses, hotel, rest, room, meal, ben = make_world(self.store)
        order = self.store.create_order(ev["id"], hotel["id"], "MEITUAN", 45000,
                                        "fan", commitment_id=room["id"])
        c1 = self.store.open_complaint(ev["id"], "PRICE_GOUGE", "加价", "12345",
                                       f"BK:{order['id']}", "fan", order_id=order["id"])
        c2 = self.store.open_complaint(ev["id"], "PRICE_GOUGE", "加价(微信)", "WECHAT",
                                       f"BK:{order['id']}", "fan", order_id=order["id"])
        c3 = self.store.open_complaint(ev["id"], "PRICE_GOUGE", "加价(同键重传)", "WEB",
                                       f"BK:{order['id']}", "fan", order_id=order["id"],
                                       idem_key="CPL-KEY")
        c4 = self.store.open_complaint(ev["id"], "PRICE_GOUGE", "加价(同键重传)", "APP",
                                       f"BK:{order['id']}", "fan", order_id=order["id"],
                                       idem_key="CPL-KEY")
        self.assertEqual({c1["id"], c2["id"], c3["id"], c4["id"]}, {c1["id"]})
        self.assertTrue(c2["duplicate"] and c3["duplicate"] and c4["duplicate"])
        # 存储层同键首次落在业务键去重分支时不产生新行；HTTP 幂等表负责逐字节重放
        self.assertFalse(c3["replayed"])
        # 去重命中也留痕
        chain = self.store.ledger_for("complaint", c1["id"])
        self.assertTrue(any(x["event_type"] == "COMPLAINT_DEDUP_HIT" for x in chain))

    def test_duplicate_remedy_blocked_even_concurrently(self):
        ev, ses, hotel, rest, room, meal, ben = make_world(self.store)
        order = self.store.create_order(ev["id"], hotel["id"], "MEITUAN", 45000,
                                        "fan", commitment_id=room["id"])
        c = self._open_case(ev, order)
        self.store.handle_complaint(c["id"], "INVESTIGATE", "officer:z",
                                    officer_dept="MARKET_REGULATION")

        errors: list[str] = []

        def worker():
            try:
                self.store.propose_remedy(
                    actor="officer:z", kind="REFUND", amount_cents=15000,
                    complaint_id=c["id"], officer_dept="MARKET_REGULATION")
            except GuardError as e:
                errors.append(e.code)

        threads = [threading.Thread(target=worker) for _ in range(6)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        remedies = self.store.get_complaint(c["id"])["remedies"]
        self.assertEqual(len(remedies), 1)
        self.assertEqual(errors.count("DUPLICATE_REMEDY"), 5)

    def test_refund_and_compensation_can_coexist_but_each_once(self):
        ev, ses, hotel, rest, room, meal, ben = make_world(self.store)
        order = self.store.create_order(ev["id"], hotel["id"], "MEITUAN", 45000,
                                        "fan", commitment_id=room["id"])
        c = self._open_case(ev, order)
        self.store.handle_complaint(c["id"], "INVESTIGATE", "officer:z",
                                    officer_dept="MARKET_REGULATION")
        refund = self.store.propose_remedy(
            actor="o", kind="REFUND", amount_cents=15000, complaint_id=c["id"],
            officer_dept="MARKET_REGULATION")
        comp = self.store.propose_remedy(
            actor="o", kind="COMPENSATION", amount_cents=5000, complaint_id=c["id"],
            officer_dept="MARKET_REGULATION")
        self.assertNotEqual(refund["id"], comp["id"])
        self.assertEqual(len(self.store.get_complaint(c["id"])["remedies"]), 2)

    def test_jurisdiction_blocks_wrong_department(self):
        ev, ses, hotel, rest, room, meal, ben = make_world(self.store)
        order = self.store.create_order(ev["id"], hotel["id"], "MEITUAN", 45000,
                                        "fan", commitment_id=room["id"])
        c = self._open_case(ev, order)
        with self.assertRaises(GuardError) as ctx:
            self.store.handle_complaint(c["id"], "INVESTIGATE", "tourism-guy",
                                        officer_dept="TOURISM")
        self.assertEqual(ctx.exception.code, "OUT_OF_JURISDICTION")
        # 转交后可办
        self.store.handle_complaint(c["id"], "INVESTIGATE", "mr-guy",
                                    officer_dept="MARKET_REGULATION")
        self.store.handle_complaint(c["id"], "TRANSFER", "mr-guy",
                                    to_dept="TOURISM", officer_dept="MARKET_REGULATION")
        self.store.handle_complaint(c["id"], "RESOLVE", "tourism-guy",
                                    note="跨部门联合处置完成", officer_dept="TOURISM")
        self.assertEqual(self.store.get_complaint(c["id"])["status"], "RESOLVED")

    def test_appeal_and_reopen_keep_chain(self):
        ev, ses, hotel, rest, room, meal, ben = make_world(self.store)
        order = self.store.create_order(ev["id"], hotel["id"], "MEITUAN", 45000,
                                        "fan", commitment_id=room["id"])
        c = self._open_case(ev, order)
        self.store.handle_complaint(c["id"], "INVESTIGATE", "o", officer_dept="MARKET_REGULATION")
        self.store.handle_complaint(c["id"], "DISMISS", "o", officer_dept="MARKET_REGULATION")
        self.store.handle_complaint(c["id"], "APPEAL", "fan")
        self.assertEqual(self.store.get_complaint(c["id"])["status"], "APPEALED")
        actions = [h["action"] for h in self.store.get_complaint(c["id"])["handlings"]]
        self.assertEqual(actions, ["INVESTIGATE", "DISMISS", "APPEAL"])


class PolicyNoRetroactiveTest(StoreFixture):
    def test_policy_change_only_affects_later_transactions(self):
        ev, ses, hotel, rest, room, meal, ben = make_world(self.store)
        # 旧政策下下单
        old_order = self.store.create_order(ev["id"], rest["id"], "APP", 19900, "fan",
                                            commitment_id=meal["id"],
                                            at="2026-10-04T18:00:00+08:00")
        # 新政策：商户补偿改由财政承担，阈值收紧
        new_rules = {
            "price_spike_ratio": 0.05,
            "mass_rejection_count": 5,
            "mass_rejection_window_minutes": 60,
            "mass_rejection_ratio": 0.50,
            "early_closure_grace_minutes": 30,
            "bearer_map": {
                "PRICE_GOUGE": {"REFUND": ["MERCHANT", "self"],
                                "COMPENSATION": ["GOVERNMENT_FUND", "district-relief"]},
            },
        }
        self.store.create_policy("2026-10-05T00:00:00+08:00", new_rules, "加价补偿改财政",
                                 "officer:boss")
        new_order = self.store.create_order(ev["id"], rest["id"], "APP", 19900, "fan",
                                            commitment_id=meal["id"],
                                            at="2026-10-05T09:00:00+08:00")
        self.assertEqual(old_order["policy_version"], 1)
        self.assertEqual(new_order["policy_version"], 2)

        def settle(order, cat):
            c = self.store.open_complaint(
                ev["id"], cat, "x", "HOTLINE", f"BK2:{order['id']}", "fan",
                order_id=order["id"])
            self.store.handle_complaint(c["id"], "INVESTIGATE", "o",
                                        officer_dept="MARKET_REGULATION")
            rem = self.store.propose_remedy(
                actor="o", kind="COMPENSATION", amount_cents=3000,
                complaint_id=c["id"], officer_dept="MARKET_REGULATION")
            return rem

        old_rem = settle(old_order, "PRICE_GOUGE")
        new_rem = settle(new_order, "PRICE_GOUGE")
        # 旧单按旧政策：商户承担；新单按新政策：财政承担
        self.assertEqual(old_rem["bearer_type"], "MERCHANT")
        self.assertEqual(old_rem["policy_version"], 1)
        self.assertEqual(new_rem["bearer_type"], "GOVERNMENT_FUND")
        self.assertEqual(new_rem["policy_version"], 2)


class AlertTest(StoreFixture):
    def test_price_spike_and_mass_rejection_and_early_closure(self):
        ev, ses, hotel, rest, room, meal, ben = make_world(self.store)
        spike = self.store.report_price(room["id"], 45000, "mer:hotel", at=DURING)
        self.assertEqual(spike["alert"]["type"], "PRICE_SPIKE")
        self.assertIn("evidence", spike["alert"])

        # 6 笔拒单（在 60 分钟窗口内），1 笔成交 -> 触发集中拒单
        for i in range(6):
            o = self.store.create_order(ev["id"], hotel["id"], "APP", 30000, f"fan{i}",
                                        commitment_id=room["id"],
                                        at=f"2026-10-04T20:{i:02d}:00+08:00")
            self.store.reject_order(o["id"], "无房", "mer:hotel")
        self.store.create_order(ev["id"], hotel["id"], "APP", 30000, "fan9",
                                commitment_id=room["id"],
                                at="2026-10-04T20:30:00+08:00")
        types = {a["type"] for a in self.store.list_alerts(ev["id"])}
        self.assertIn("MASS_REJECTION", types)

        rep = self.store.report_early_closure(
            ev["id"], ses["id"], "天河路第二现场", "2026-10-04T23:00:00+08:00",
            "2026-10-04T20:30:00+08:00", "officer:t")
        self.assertFalse(rep["within_grace"])
        self.assertEqual(rep["alert"]["type"], "EARLY_CLOSURE")

        # 宽限内只留痕不告警
        rep2 = self.store.report_early_closure(
            ev["id"], ses["id"], "体育西第二现场", "2026-10-04T23:00:00+08:00",
            "2026-10-04T22:45:00+08:00", "officer:t")
        self.assertTrue(rep2["within_grace"])
        self.assertIsNone(rep2["alert"])


class CancellationTest(StoreFixture):
    def test_cancel_session_invalidates_benefits_and_blocks_further_use(self):
        ev, ses, hotel, rest, room, meal, ben = make_world(self.store)
        # 取消前核销一张
        ok = self.store.redeem(actor="f", channel="APP", benefit_id=ben["id"], at=DURING)
        self.assertEqual(ok["result"], "SUCCESS")
        self.store.cancel_session(ses["id"], "台风红色预警", True, "organizer:org-1")
        self.assertEqual(self.store.get_session(ses["id"])["status"], "CANCELLED")
        self.assertEqual(self.store.get_benefit(ben["id"])["status"], "INVALIDATED")
        again = self.store.redeem(actor="f", channel="APP", benefit_id=ben["id"], at=DURING)
        self.assertEqual(again["result"], "REJECTED")
        alerts = {a["type"] for a in self.store.list_alerts(ev["id"])}
        self.assertIn("BENEFIT_INVALIDATION", alerts)

    def test_reschedule_keeps_commitments_valid(self):
        ev, ses, hotel, rest, room, meal, ben = make_world(self.store)
        self.store.flag_weather(ses["id"], "EXTREME_WEATHER", "officer:e")
        self.store.reschedule_session(ses["id"], "2026-10-06T19:30:00+08:00",
                                      "2026-10-06T21:30:00+08:00", "暴雨改期", "officer:e")
        self.assertEqual(self.store.get_session(ses["id"])["status"], "RESCHEDULED")
        timeline = [x["event_type"] for x in self.store.event_timeline(ev["id"])]
        self.assertIn("WEATHER_FLAGGED", timeline)
        self.assertIn("SESSION_RESCHEDULED", timeline)


class SuspensionAndEventChainTest(StoreFixture):
    def test_suspended_merchant_cannot_commit_or_take_orders(self):
        ev, ses, hotel, rest, room, meal, ben = make_world(self.store)
        self.store.suspend_merchant(hotel["id"], "投诉集中且拒检", "officer:z",
                                    officer_dept="MARKET_REGULATION")
        with self.assertRaises(GuardError) as ctx:
            self.store.publish_commitment(hotel["id"], ev["id"], "x", 100, VF, VT, 1, "mer:hotel")
        self.assertEqual(ctx.exception.code, "MERCHANT_SUSPENDED")
        with self.assertRaises(GuardError):
            self.store.create_order(ev["id"], hotel["id"], "APP", 100, "fan")
        # 恢复后恢复经营
        self.store.resume_merchant(hotel["id"], "officer:z")
        o = self.store.create_order(ev["id"], hotel["id"], "APP", 100, "fan")
        self.assertEqual(o["status"], "BOOKED")

    def test_remedy_trace_names_final_bearer_with_full_chain(self):
        ev, ses, hotel, rest, room, meal, ben = make_world(self.store)
        order = self.store.create_order(ev["id"], hotel["id"], "MEITUAN", 45000, "fan",
                                        commitment_id=room["id"])
        c = self.store.open_complaint(ev["id"], "PRICE_GOUGE", "加价", "TEL",
                                      f"BK3:{order['id']}", "fan", order_id=order["id"])
        self.store.handle_complaint(c["id"], "INVESTIGATE", "o", officer_dept="MARKET_REGULATION")
        rem = self.store.propose_remedy(actor="o", kind="REFUND", amount_cents=15000,
                                        complaint_id=c["id"], officer_dept="MARKET_REGULATION")
        self.store.approve_remedy(rem["id"], "officer:boss")
        paid = self.store.pay_remedy(rem["id"], "treasury")
        trace = self.store.remedy_trace(rem["id"])
        names = [x["event_type"] for x in trace["event_chain"]]
        self.assertEqual(names, ["REMEDY_PROPOSED", "REMEDY_APPROVED", "REMEDY_SETTLED"])
        self.assertEqual(trace["final_bearer"]["type"], "MERCHANT")
        self.assertEqual(trace["final_bearer"]["id"], hotel["id"])
        self.assertEqual(paid["status"], "PAID")

    def test_event_impact_reconstructs_the_whole_match(self):
        ev, ses, hotel, rest, room, meal, ben = make_world(self.store)
        self.store.redeem(actor="f", channel="APP", commitment_id=room["id"], at=DURING)
        self.store.redeem(actor="f", channel="APP", commitment_id=room["id"], at=DURING)
        self.store.report_price(room["id"], 45000, "mer:hotel", at=DURING)
        order = self.store.create_order(ev["id"], hotel["id"], "MEITUAN", 45000, "fan",
                                        commitment_id=room["id"])
        c = self.store.open_complaint(ev["id"], "PRICE_GOUGE", "x", "T", f"BK4:{order['id']}",
                                      "fan", order_id=order["id"])
        self.store.handle_complaint(c["id"], "INVESTIGATE", "o", officer_dept="MARKET_REGULATION")
        rem = self.store.propose_remedy(actor="o", kind="REFUND", amount_cents=15000,
                                        complaint_id=c["id"], officer_dept="MARKET_REGULATION")
        self.store.approve_remedy(rem["id"], "b")
        self.store.pay_remedy(rem["id"], "t")

        impact = self.store.event_impact(ev["id"])
        self.assertEqual(impact["commitments"]["promised_qty"], 53)
        self.assertEqual(impact["commitments"]["redeemed_qty"], 2)
        self.assertEqual(impact["complaints"]["total"], 1)
        self.assertEqual(impact["alerts"]["by_type"]["PRICE_SPIKE"], 1)
        self.assertEqual(impact["remedies"]["paid_cents"], 15000)
        self.assertEqual(impact["remedies"]["paid_by_bearer_type"]["MERCHANT"], 15000)
        self.assertGreater(impact["timeline_entries"], 10)


if __name__ == "__main__":
    unittest.main()
