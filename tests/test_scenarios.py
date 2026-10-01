"""服务层场景测试：去重、配额并发、政策时效、处置承担、事件链与预警。"""

import threading
import unittest

from src import domain
from src.db import Database
from src.services import GuardService

ORG, REG, TOUR, OPS = "org-token", "reg-token", "tour-token", "op-token"
STAFF = "staff-token"
HOTEL, FOOD, MALL = "mer-hotel-token", "mer-food-token", "mer-mall-token"
NOW = "2026-10-03T22:30:00+08:00"


class ServiceFixture(unittest.TestCase):
    def setUp(self):
        self.db = Database(":memory:")
        self.svc = GuardService(self.db, clock=lambda: NOW)
        self.svc.register_event(ORG, {
            "event_id": "e1", "name": "测试联赛",
            "starts_at": "2026-10-03T15:00:00+08:00",
            "ends_at": "2026-10-03T22:00:00+08:00",
            "expected_attendance": 12000,
        })
        for mid, name, sector in [
            ("m-hotel", "酒店", "lodging"),
            ("m-food", "餐饮", "dining"),
            ("m-mall", "第二现场", "culture_tourism"),
        ]:
            self.svc.register_merchant(ORG,
                {"merchant_id": mid, "name": name, "sector": sector})
        self.svc.publish_benefit(ORG, {
            "benefit_id": "bf1", "event_id": "e1", "code": "FAN1",
            "title": "礼包", "quota": 100,
            "valid_from": "2026-10-03T12:00:00+08:00",
            "valid_to": "2026-10-03T23:30:00+08:00",
        })

    def tearDown(self):
        self.db.close()

    def commit(self, token, cid, kind, promised=0, spec=None, vf=None, vt=None):
        return self.svc.publish_commitment(token, {
            "commitment_id": cid, "event_id": "e1", "kind": kind,
            "spec": spec or {}, "promised_total": promised,
            "valid_from": vf or "2026-10-03T12:00:00+08:00",
            "valid_to": vt or "2026-10-03T23:30:00+08:00",
        })

    def order(self, token, ref, amount=10.0, channel="app", cid=None,
              dedup=None, mid=None, extra=None):
        payload = {"event_id": "e1", "channel": channel,
                   "channel_ref": ref, "amount": amount}
        if mid:
            payload["merchant_id"] = mid
        if cid:
            payload["commitment_id"] = cid
        if dedup:
            payload["dedup_key"] = dedup
        if extra:
            payload.update(extra)
        return self.svc.create_order(token, payload)

    def complain(self, token, merchant, ref, channel="hotline",
                 dedup=None, category="other", order_id=None):
        payload = {"event_id": "e1", "merchant_id": merchant,
                   "channel": channel, "channel_ref": ref,
                   "category": category}
        if dedup:
            payload["dedup_key"] = dedup
        if order_id:
            payload["order_id"] = order_id
        return self.svc.ingest_complaint(token, payload)


class RegistrationTest(ServiceFixture):
    def test_duplicate_event_rejected(self):
        with self.assertRaises(domain.Conflict):
            self.svc.register_event(ORG, {
                "event_id": "e1", "name": "重复",
                "starts_at": "2026-10-03T15:00:00+08:00",
                "ends_at": "2026-10-03T22:00:00+08:00"})

    def test_staff_cannot_register(self):
        with self.assertRaises(domain.PermissionDenied):
            self.svc.register_event(STAFF, {
                "event_id": "e2", "name": "x",
                "starts_at": "2026-10-03T15:00:00+08:00",
                "ends_at": "2026-10-03T22:00:00+08:00"})

    def test_merchant_cannot_publish_for_other_merchant(self):
        with self.assertRaises(domain.PermissionDenied):
            self.svc.publish_commitment(FOOD, {
                "commitment_id": "cx", "event_id": "e1",
                "merchant_id": "m-hotel", "kind": "price",
                "spec": {"price": 1},
                "valid_from": "2026-10-03T12:00:00+08:00",
                "valid_to": "2026-10-03T23:30:00+08:00"})


class PriceSpikeAlertTest(ServiceFixture):
    def test_price_surge_within_validity_alerts(self):
        self.commit(HOTEL, "cp1", "price", spec={"price": 399})
        self.commit(HOTEL, "cp2", "price", spec={"price": 699},
                    vf="2026-10-03T22:05:00+08:00")
        alerts = self.svc.list_alerts("e1", "price_spike")
        self.assertEqual(len(alerts), 1)
        self.assertEqual(alerts[0]["severity"], "critical")
        self.assertAlmostEqual(alerts[0]["evidence"]["new_price"], 699)
        # 旧承诺被标记为被取代
        old = self.svc.get_commitment("cp1")
        self.assertEqual(old["active"], 0)
        self.assertEqual(old["superseded_by"], "cp2")

    def test_small_increase_no_alert(self):
        self.commit(HOTEL, "cp1", "price", spec={"price": 399})
        self.commit(HOTEL, "cp2", "price", spec={"price": 410},
                    vf="2026-10-03T22:05:00+08:00")
        self.assertEqual(self.svc.list_alerts("e1", "price_spike"), [])


class ClusteredRejectionTest(ServiceFixture):
    def test_three_rejections_triggers_alert(self):
        self.commit(FOOD, "cf1", "package", promised=10)
        for i in range(3):
            o = self.order(FOOD, f"r{i}", cid="cf1")
            self.svc.reject_order(FOOD, o["order_id"], "备货不足")
        alerts = self.svc.list_alerts("e1", "clustered_rejection")
        self.assertEqual(len(alerts), 1)
        self.assertEqual(alerts[0]["evidence"]["count"], 3)

    def test_two_rejections_no_alert(self):
        self.commit(FOOD, "cf1", "package", promised=10)
        for i in range(2):
            o = self.order(FOOD, f"r{i}", cid="cf1")
            self.svc.reject_order(FOOD, o["order_id"], "备货不足")
        self.assertEqual(self.svc.list_alerts("e1", "clustered_rejection"), [])


class RedemptionQuotaTest(ServiceFixture):
    def _pkg(self):
        self.commit(MALL, "cm1", "package", promised=5)

    def test_within_quota_ok(self):
        self._pkg()
        o = self.order(MALL, "o1", cid="cm1")
        r = self.svc.redeem(MALL, {"order_id": o["order_id"],
                                   "commitment_id": "cm1", "qty": 5,
                                   "channel": "app"})
        self.assertEqual(r["qty"], 5)
        self.assertEqual(self.svc.get_commitment("cm1")["redeemed_total"], 5)

    def test_over_quota_rejected(self):
        self._pkg()
        o = self.order(MALL, "o1", cid="cm1")
        self.svc.redeem(MALL, {"order_id": o["order_id"],
                               "commitment_id": "cm1", "qty": 5,
                               "channel": "app"})
        o2 = self.order(MALL, "o2", cid="cm1")
        with self.assertRaises(domain.Conflict) as ctx:
            self.svc.redeem(MALL, {"order_id": o2["order_id"],
                                   "commitment_id": "cm1", "qty": 1,
                                   "channel": "app"})
        self.assertEqual(ctx.exception.code, "quota_exceeded")

    def test_concurrent_redemption_never_exceeds_promised(self):
        self._pkg()
        orders = [self.order(MALL, f"c{i}", cid="cm1") for i in range(20)]
        results = {"ok": 0, "fail": 0}
        lock = threading.Lock()

        def worker(o):
            try:
                self.svc.redeem(MALL, {"order_id": o["order_id"],
                                       "commitment_id": "cm1", "qty": 1,
                                       "channel": "app"})
                with lock:
                    results["ok"] += 1
            except domain.Conflict:
                with lock:
                    results["fail"] += 1

        threads = [threading.Thread(target=worker, args=(o,)) for o in orders]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(results["ok"], 5)
        self.assertEqual(results["fail"], 15)
        self.assertEqual(self.svc.get_commitment("cm1")["redeemed_total"], 5)

    def test_redeem_replay_is_idempotent(self):
        self._pkg()
        o = self.order(MALL, "o1", cid="cm1")
        first = self.svc.redeem(MALL, {"order_id": o["order_id"],
                                       "commitment_id": "cm1", "qty": 2,
                                       "channel": "app"})
        replay = self.svc.redeem(MALL, {"order_id": o["order_id"],
                                        "commitment_id": "cm1", "qty": 2,
                                        "channel": "hotline"})
        self.assertTrue(replay.get("replayed"))
        self.assertEqual(first["redemption_id"], replay["redemption_id"])
        self.assertEqual(self.svc.get_commitment("cm1")["redeemed_total"], 2)

    def test_suspended_merchant_blocks_order_and_redeem(self):
        self._pkg()
        o = self.order(MALL, "o1", cid="cm1")
        self.svc.suspend_merchant(TOUR, "m-mall", "提前关闭")
        with self.assertRaises(domain.StateError):
            self.svc.redeem(MALL, {"order_id": o["order_id"],
                                   "commitment_id": "cm1", "qty": 1,
                                   "channel": "app"})
        self.svc.unsuspend_merchant(TOUR, "m-mall")
        # 复业后在有效期内可核销
        self.svc.redeem(MALL, {"order_id": o["order_id"],
                               "commitment_id": "cm1", "qty": 1,
                               "channel": "app"})


class DedupTest(ServiceFixture):
    def test_same_order_from_two_channels_dedup_by_key(self):
        a = self.order(HOTEL, "ref1", channel="app", dedup="order-key-1")
        b = self.order(HOTEL, "ref2", channel="hotline", dedup="order-key-1")
        self.assertEqual(a["order_id"], b["order_id"])
        with self.db.transaction() as con:
            n = con.execute("SELECT COUNT(*) c FROM orders").fetchone()["c"]
        self.assertEqual(n, 1)

    def test_same_channel_ref_replays(self):
        a = self.order(HOTEL, "same-ref", channel="app")
        b = self.order(HOTEL, "same-ref", channel="app")
        self.assertEqual(a["order_id"], b["order_id"])

    def test_complaint_replay_across_channels(self):
        a = self.complain(REG, "m-hotel", "HL-1", dedup="case-1",
                          category="price")
        b = self.complain(STAFF, "m-hotel", "MP-2", channel="miniapp",
                          dedup="case-1", category="price")
        self.assertTrue(b.get("replayed"))
        self.assertEqual(a["complaint_id"], b["complaint_id"])
        self.assertEqual(b["dedup_hit"], "dedup_key")


class DispositionTest(ServiceFixture):
    def _settled_complaint(self):
        c = self.complain(REG, "m-hotel", "HL-9", category="price")
        self.svc.record_investigation(REG, c["complaint_id"],
                                      "涨价证据确凿", root_cause="price_surge")
        return c

    def test_policy_version_by_transaction_time(self):
        c = self._settled_complaint()
        old = self.svc.settle_disposition(
            REG, c["complaint_id"], "compensation", 100,
            root_cause="price_surge",
            transaction_at="2026-08-20T12:00:00+08:00")
        self.assertEqual(old["policy_version"], 1)
        refund = self.svc.settle_disposition(
            REG, c["complaint_id"], "refund", 200,
            root_cause="price_surge",
            transaction_at="2026-10-03T20:00:00+08:00")
        self.assertEqual(refund["policy_version"], 2)

    def test_duplicate_compensation_blocked_and_replayed(self):
        c = self._settled_complaint()
        first = self.svc.settle_disposition(
            REG, c["complaint_id"], "compensation", 300,
            root_cause="price_surge")
        again = self.svc.settle_disposition(
            REG, c["complaint_id"], "compensation", 9999,
            root_cause="price_surge")
        self.assertTrue(again.get("replayed"))
        self.assertEqual(again["disposition_id"], first["disposition_id"])
        self.assertEqual(again["amount"], 300)
        # 库里该投诉该类型只有一笔
        n = self.db.scalar(
            "SELECT COUNT(*) FROM dispositions WHERE complaint_id=?"
            " AND kind='compensation' AND status='settled'",
            (c["complaint_id"],))
        self.assertEqual(n, 1)

    def test_bearer_force_majeure_on_extreme_reschedule(self):
        # 赛事因极端气象改期后，相关投诉的气象快照为 extreme
        self.svc.reschedule_event(
            OPS, "e1", "2026-10-06T15:00:00+08:00",
            "2026-10-06T22:00:00+08:00", "台风", weather_level="extreme")
        c = self.complain(REG, "m-hotel", "HL-W", category="benefit_lost")
        self.svc.record_investigation(REG, c["complaint_id"],
                                      "权益因改期失效",
                                      root_cause="event_reschedule")
        d = self.svc.settle_disposition(
            REG, c["complaint_id"], "compensation", 50,
            root_cause="event_reschedule")
        self.assertEqual(d["bearer"], domain.Bearer.FORCE_MAJEURE)

    def test_appeal_upheld_reverses_disposition(self):
        c = self._settled_complaint()
        d = self.svc.settle_disposition(
            REG, c["complaint_id"], "refund", 88,
            root_cause="package_unredeemable")
        self.assertEqual(d["bearer"], domain.Bearer.MERCHANT)
        # 商户必须先被记录为投诉对象所属商户，申诉权限才通过
        appeal = self.svc.submit_appeal(HOTEL, c["complaint_id"],
                                        "系误判")
        ruling = self.svc.rule_appeal(TOUR, appeal["appeal_id"], uphold=True,
                                      note="证据不足")
        self.assertEqual(ruling["status"], "upheld")
        self.assertIn(d["disposition_id"], ruling["reversed_dispositions"])
        after = self.svc.get_disposition(d["disposition_id"])
        self.assertEqual(after["status"], "reversed")
        self.assertEqual(after["bearer"], domain.Bearer.NONE)
        rec = self.svc.reconcile_bearer(d["disposition_id"])
        self.assertTrue(rec["consistent"])

    def test_tourism_cannot_rule_market_only_complaint(self):
        # 文旅有 appeal.rule 权限；但主办方没有处置权限
        c = self._settled_complaint()
        with self.assertRaises(domain.PermissionDenied):
            self.svc.settle_disposition(ORG, c["complaint_id"],
                                        "refund", 10, root_cause="price_surge")


class ScheduleAndBenefitTest(ServiceFixture):
    def test_extreme_reschedule_invalidates_benefit_with_alert(self):
        out = self.svc.reschedule_event(
            OPS, "e1", "2026-10-06T15:00:00+08:00",
            "2026-10-06T22:00:00+08:00", "暴雨红色预警", weather_level="extreme")
        self.assertEqual(out["status"], "rescheduled")
        self.assertEqual(len(out["benefit_alerts"]), 1)
        bf = self.svc.get_benefit("bf1")
        self.assertEqual(bf["status"], "invalidated")
        alert = self.svc.list_alerts("e1", "benefit_invalidation")[0]
        self.assertEqual(alert["evidence"]["unredeemed_count"], 100)

    def test_cancel_then_reschedule_rejected(self):
        self.svc.cancel_event(OPS, "e1", "因故取消")
        with self.assertRaises(domain.StateError):
            self.svc.reschedule_event(
                OPS, "e1", "2026-10-06T15:00:00+08:00",
                "2026-10-06T22:00:00+08:00", "x")


class ChainAndReportTest(ServiceFixture):
    def test_full_chain_and_impact_report(self):
        self.commit(HOTEL, "cp1", "price", spec={"price": 399})
        self.commit(HOTEL, "cp2", "price", spec={"price": 699},
                    vf="2026-10-03T22:05:00+08:00")
        o = self.order(HOTEL, "o1", amount=699, extra={"surcharge_flag": True})
        c = self.complain(REG, "m-hotel", "HL-1", category="price",
                          order_id=o["order_id"])
        self.svc.record_investigation(REG, c["complaint_id"], "加价",
                                      root_cause="price_surge")
        d = self.svc.settle_disposition(
            REG, c["complaint_id"], "refund", 300,
            root_cause="price_surge")

        chain = self.svc.chain_of("complaint", c["complaint_id"])
        actions = [x["action"] for x in chain]
        self.assertIn("complaint.opened", actions)
        self.assertIn("investigation.recorded", actions)

        timeline = self.svc.event_timeline("e1")
        self.assertTrue(any(t["action"] == "alert.price_spike" for t in timeline))

        report = self.svc.event_impact_report("e1")
        self.assertEqual(report["orders"]["surcharge_flags"], 1)
        self.assertEqual(report["dispositions_by_bearer"]["merchant"]
                         ["settled_amount"], 300.0)

        rec = self.svc.reconcile_bearer(d["disposition_id"])
        self.assertTrue(rec["consistent"])
        self.assertEqual(rec["recorded_bearer"], "merchant")
        self.assertEqual(rec["applied_policy"]["version"], 2)


if __name__ == "__main__":
    unittest.main()
