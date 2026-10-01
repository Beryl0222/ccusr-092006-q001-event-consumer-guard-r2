"""event_consumer_guard 领域资料：跨部门统一术语与最小事件契约。

联防后端（src/domain.py、src/services.py）中的事件链动作、预警类型
均应使用这里登记的术语，保证业务、运营与研发在同一套词汇下协作。
"""

from __future__ import annotations

# 业务主体生命周期事件。
EVENT_KINDS = [
    'EVENT_PUBLISHED',        # 主办方登记赛程/客流/权益
    'EVENT_RESCHEDULED',      # 改期（含极端气象）
    'EVENT_CANCELLED',        # 取消
    'MERCHANT_REGISTERED',    # 商户建档
    'MERCHANT_SUSPENDED',     # 停业整顿
    'MERCHANT_COMMITMENT',    # 带有效期的价格/履约承诺发布
    'ORDER_CREATED',          # 多渠道下单
    'ORDER_REJECTED',         # 商户拒单
    'BENEFIT_REDEEMED',       # 套餐/权益核销
    'REDEMPTION_REVERSED',    # 核销撤销（配额归还）
    'COMPLAINT_OPENED',       # 投诉受理（多渠道）
    'INVESTIGATION_RECORDED', # 调查记录
    'APPEAL_RULED',           # 申诉裁定
    'REMEDY_SETTLED',         # 退款/补偿处置生效
    'REMEDY_REVERSED',        # 处置冲正（申诉成立等）
    'ALERT_RAISED',           # 预警生成
]

# 可解释预警类型。
ALERT_KINDS = ['PRICE_SPIKE', 'CLUSTERED_REJECTION', 'BENEFIT_INVALIDATION']

# 处置成本最终承担方。
BEARER_KINDS = ['merchant', 'organizer', 'force_majeure', 'joint_fund', 'none']

REQUIRED_FIELDS = ("event_id", "kind", "occurred_at", "subject_id", "payload")


def validate_event(record: dict) -> list[str]:
    """检查交换事件是否具备可联调的最小字段。"""
    problems = [name for name in REQUIRED_FIELDS if name not in record]
    if record.get("kind") not in EVENT_KINDS:
        problems.append("kind")
    return problems
