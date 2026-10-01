"""端到端演示：不启网络，直接用内存库走查联防全链路。

运行：python -m src.demo

场景：城市周末联赛散场后，酒店临时加价、商户集中拒单、
商圈第二现场提前关闭导致套餐无法核销；随后极端气象迫使赛事改期、
官方权益失效；最后完成调查、申诉、退款补偿与承担方核对。
"""

from __future__ import annotations

import json

from . import domain
from .db import Database
from .services import GuardService

ORG, REG, TOUR, OPS = "org-token", "reg-token", "tour-token", "op-token"
HOTEL, FOOD, MALL = "mer-hotel-token", "mer-food-token", "mer-mall-token"
EVENT = "league-final-2026"


def pp(title: str, payload) -> None:
    print(f"\n-- {title}")
    print(json.dumps(payload, ensure_ascii=False, indent=2, default=str))


def main() -> None:
    # 固定在散场后的 10-03 22:30，保证“带有效期的承诺/拒单窗口/政策时点”可复现。
    fixed = "2026-10-03T22:30:00+08:00"
    svc = GuardService(Database(":memory:"), clock=lambda: fixed)

    print("== 1. 主办方登记赛程、预计客流与官方权益 ==")
    svc.register_event(ORG, {
        "event_id": EVENT, "name": "城市周末联赛·决赛",
        "venue": "市体育中心", "starts_at": "2026-10-03T15:00:00+08:00",
        "ends_at": "2026-10-03T22:00:00+08:00", "expected_attendance": 18000,
    })
    svc.publish_benefit(ORG, {
        "benefit_id": "bf-1", "event_id": EVENT, "code": "FAN2026",
        "title": "球迷观赛礼包", "quota": 500,
        "valid_from": "2026-10-03T12:00:00+08:00",
        "valid_to": "2026-10-03T23:30:00+08:00",
    })

    print("== 2. 住宿/餐饮/文旅商户发布带有效期的价格与履约承诺 ==")
    for mid, name, sector in [
        ("m-hotel", "东站快捷酒店", "lodging"),
        ("m-food", "看台小吃集合店", "dining"),
        ("m-mall", "中心商圈第二现场", "culture_tourism"),
    ]:
        svc.register_merchant(ORG,
                              {"merchant_id": mid, "name": name, "sector": sector})
    svc.publish_commitment(HOTEL, {
        "commitment_id": "c-hotel-price", "event_id": EVENT,
        "kind": "price", "spec": {"price": 399.0, "room_type": "标准间"},
        "valid_from": "2026-10-01T00:00:00+08:00",
        "valid_to": "2026-10-04T12:00:00+08:00",
    })
    svc.publish_commitment(FOOD, {
        "commitment_id": "c-food-pkg", "event_id": EVENT,
        "kind": "package", "spec": {"name": "观赛小吃套餐"},
        "promised_total": 100,
        "valid_from": "2026-10-03T12:00:00+08:00",
        "valid_to": "2026-10-03T23:00:00+08:00",
    })
    svc.publish_commitment(MALL, {
        "commitment_id": "c-mall-pkg", "event_id": EVENT,
        "kind": "package", "spec": {"name": "第二现场核销套餐"},
        "promised_total": 50,
        "valid_from": "2026-10-03T12:00:00+08:00",
        "valid_to": "2026-10-04T02:00:00+08:00",
    })
    svc.publish_commitment(MALL, {
        "commitment_id": "c-mall-hours", "event_id": EVENT,
        "kind": "venue_hours",
        "spec": {"open_from": "2026-10-03T18:00", "open_to": "2026-10-04T01:00"},
        "valid_from": "2026-10-03T18:00:00+08:00",
        "valid_to": "2026-10-04T01:00:00+08:00",
    })

    print("== 3. 散场后酒店在承诺有效期内把 399 临时上调到 699 → 价格突变预警 ==")
    svc.publish_commitment(HOTEL, {
        "commitment_id": "c-hotel-price-2", "event_id": EVENT,
        "kind": "price", "spec": {"price": 699.0, "room_type": "标准间"},
        "valid_from": "2026-10-03T22:05:00+08:00",
        "valid_to": "2026-10-04T12:00:00+08:00",
    })

    print("== 4. 餐饮商户 1 小时内集中拒单 3 笔 → 集中拒单预警 ==")
    rejected_orders = []
    for i in range(3):
        o = svc.create_order(FOOD, {
            "event_id": EVENT, "channel": "miniapp",
            "channel_ref": f"food-order-{i}", "amount": 45.0,
            "commitment_id": "c-food-pkg",
        })
        svc.reject_order(FOOD, o["order_id"], "称食材不足")
        rejected_orders.append(o["order_id"])

    print("== 5. 第二现场提前关闭：文旅停业，随后套餐核销失败（无法核销）==")
    mall_order = svc.create_order(MALL, {
        "event_id": EVENT, "channel": "onsite", "channel_ref": "mall-order-1",
        "amount": 88.0, "commitment_id": "c-mall-pkg",
    })
    svc.suspend_merchant(TOUR, "m-mall", "第二现场未到承诺时段提前关闭")
    try:
        svc.redeem(MALL, {"order_id": mall_order["order_id"],
                          "commitment_id": "c-mall-pkg", "channel": "onsite"})
    except domain.StateError as exc:
        print(f"   核销被阻止：{exc}")

    print("== 6. 并发核销不得突破承诺量：套餐总量 2，第三份必被拒 ==")
    svc.unsuspend_merchant(TOUR, "m-mall", "核实完毕，准予复业")
    svc.db.execute(
        "UPDATE commitments SET promised_total=2 WHERE commitment_id='c-mall-pkg'")
    o1 = svc.create_order(MALL, {"event_id": EVENT, "channel": "onsite",
                                 "channel_ref": "mall-order-2", "amount": 88.0,
                                 "commitment_id": "c-mall-pkg"})
    o2 = svc.create_order(MALL, {"event_id": EVENT, "channel": "hotline",
                                 "channel_ref": "mall-order-3", "amount": 88.0,
                                 "commitment_id": "c-mall-pkg"})
    svc.redeem(MALL, {"order_id": o1["order_id"],
                      "commitment_id": "c-mall-pkg", "qty": 2, "channel": "onsite"})
    try:
        svc.redeem(MALL, {"order_id": o2["order_id"],
                          "commitment_id": "c-mall-pkg", "qty": 1, "channel": "hotline"})
    except domain.Conflict as exc:
        print(f"   超限核销被阻止：{exc}")
    # 同一订单换渠道重传核销 → 返回原记录，不重复占用
    replay = svc.redeem(MALL, {"order_id": o1["order_id"],
                               "commitment_id": "c-mall-pkg", "qty": 2,
                               "channel": "miniapp"})
    print(f"   重传核销返回原记录：replayed={replay.get('replayed')}")

    print("== 7. 多渠道投诉；同一投诉从热线/小程序重传不重复建单 ==")
    hotel_order = svc.create_order(HOTEL, {
        "event_id": EVENT, "channel": "platform", "channel_ref": "hotel-order-1",
        "amount": 699.0, "surcharge_flag": 1,
    })
    c1 = svc.ingest_complaint(REG, {
        "event_id": EVENT, "merchant_id": "m-hotel",
        "order_id": hotel_order["order_id"],
        "channel": "hotline", "channel_ref": "HL-20261003-001",
        "dedup_key": "case-hotel-surge", "category": "hotel_price_surge",
    })
    again = svc.ingest_complaint("staff-token", {
        "event_id": EVENT, "merchant_id": "m-hotel",
        "channel": "miniapp", "channel_ref": "MP-7788",
        "dedup_key": "case-hotel-surge", "category": "hotel_price_surge",
    })
    print(f"   首次 {c1['complaint_id']} / 重传 replayed={again.get('replayed')}"
          f" 命中={again.get('dedup_hit')}")
    c2 = svc.ingest_complaint(REG, {
        "event_id": EVENT, "merchant_id": "m-food",
        "channel": "onsite", "channel_ref": "OS-002",
        "category": "clustered_rejection",
    })
    c3 = svc.ingest_complaint(TOUR, {
        "event_id": EVENT, "merchant_id": "m-mall",
        "order_id": mall_order["order_id"],
        "channel": "hotline", "channel_ref": "HL-20261003-003",
        "category": "package_unredeemable",
    })

    print("== 8. 调查、退款与补偿；政策按交易时点适用，重复赔付被唯一约束拦截 ==")
    svc.record_investigation(REG, c1["complaint_id"],
                             "酒店在承诺有效期内涨价 75%，订房记录与新价单已固定",
                             root_cause="price_surge")
    # 交易发生在 2026-08（旧版政策期间）→ 仍适用 v1，政策变更不溯及既往
    d_refund = svc.settle_disposition(
        REG, c1["complaint_id"], "refund", 300.0,
        root_cause="price_surge", transaction_at="2026-08-15T20:00:00+08:00")
    # 交易发生在 2026-10 → 适用 v2
    d_comp = svc.settle_disposition(
        REG, c1["complaint_id"], "compensation", 450.0,
        root_cause="price_surge", transaction_at="2026-10-03T22:30:00+08:00")
    dup = svc.settle_disposition(REG, c1["complaint_id"], "compensation", 999.0,
                                 root_cause="price_surge")
    print(f"   退款适用政策 v{d_refund['policy_version']}，"
          f"补偿适用 v{d_comp['policy_version']}，承担方={d_comp['bearer']}")
    print(f"   补偿重传返回原单 replayed={dup.get('replayed')}，金额未变={dup['amount']}")

    svc.record_investigation(REG, c2["complaint_id"],
                             "60 分钟内拒单 3 笔，超出合理备货边界",
                             root_cause="clustered_rejection")
    svc.settle_disposition(REG, c2["complaint_id"], "compensation", 135.0,
                           root_cause="clustered_rejection")

    svc.record_investigation(TOUR, c3["complaint_id"],
                             "第二现场提前关闭，停业期间套餐无法核销",
                             root_cause="package_unredeemable")
    d_mall = svc.settle_disposition(TOUR, c3["complaint_id"], "refund", 88.0,
                                    root_cause="package_unredeemable")

    print("== 9. 商户申诉：第二现场提前关闭系文旅临时管控，申诉成立 → 冲正，承担方改 none ==")
    appeal = svc.submit_appeal(MALL, c3["complaint_id"],
                               "接到文旅临时管控通知才闭店，非擅自提前关闭")
    svc.rule_appeal(TOUR, appeal["appeal_id"], uphold=True,
                    note="情况属实，非商户过错")

    print("== 10. 极端气象迫使赛事改期 → 官方权益失效预警（含未核销存量）==")
    svc.reschedule_event(
        OPS, EVENT, "2026-10-05T15:00:00+08:00", "2026-10-05T22:00:00+08:00",
        "台风过境，应急管理部门建议延期", weather_level="extreme")

    print("== 11. 值班人员按一场赛事还原消费影响 ==")
    pp("赛事影响汇总", svc.event_impact_report(EVENT))
    pp("预警清单", svc.list_alerts(EVENT))
    pp(f"承担方核对：{d_comp['disposition_id']}",
       svc.reconcile_bearer(d_comp["disposition_id"]))
    pp(f"投诉 {c1['complaint_id']} 完整事件链",
       svc.chain_of("complaint", c1["complaint_id"]))
    svc.close_complaint(REG, c1["complaint_id"], "退款补偿到账，消费者无异议")
    print("\n演示完成。HTTP 方式运行：python -m src --port 8080")


if __name__ == "__main__":
    main()
