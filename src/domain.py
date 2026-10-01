"""赛事消费异常联防——领域术语、角色权限与纯业务规则。

本模块不依赖数据库与网络，所有规则都以纯函数/数据类表达，
方便在测试中独立验证，也便于服务层（services.py）复用。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Iterable


# ---------------------------------------------------------------------------
# 角色与权限
# ---------------------------------------------------------------------------

class Role(StrEnum):
    """系统中的操作角色。令牌与角色的绑定见 db.seed_tokens。"""

    ORGANIZER = "organizer"          # 主办方：登记赛程/客流/官方权益
    MERCHANT = "merchant"            # 商户（住宿/餐饮/零售/文旅）
    MARKET_REGULATOR = "market_regulator"  # 市场监管
    CULTURE_TOURISM = "culture_tourism"    # 文旅
    EVENT_OPERATOR = "event_operator"      # 赛事运营
    STAFF = "duty_staff"             # 值班人员：只读还原与核对
    VIEWER = "viewer"                # 兜底只读角色


# 每个角色可以执行的动作集合。权限校验失败返回 403。
PERMISSIONS: dict[str, frozenset[str]] = {
    Role.ORGANIZER: frozenset({
        "event.register", "benefit.publish",
        "event.reschedule", "event.cancel",
        "merchant.register", "complaint.ingest",
    }),
    Role.MERCHANT: frozenset({
        "commitment.publish",
        "order.create", "order.reject",
        "redemption.redeem", "redemption.reverse",
        "appeal.submit", "complaint.ingest",
    }),
    Role.MARKET_REGULATOR: frozenset({
        "complaint.ingest", "complaint.handle", "complaint.close",
        "merchant.suspend", "merchant.unsuspend",
        "investigation.record",
        "refund.approve", "compensation.approve",
        "appeal.rule",
    }),
    Role.CULTURE_TOURISM: frozenset({
        "complaint.ingest", "complaint.handle",
        "merchant.suspend", "merchant.unsuspend",  # 文旅类场所（第二现场等）
        "refund.approve", "compensation.approve",
        "investigation.record",
        "appeal.rule",
    }),
    Role.EVENT_OPERATOR: frozenset({
        "complaint.ingest", "complaint.handle",
        "event.reschedule", "event.cancel",
        "investigation.record",
    }),
    Role.STAFF: frozenset({"complaint.ingest"}),   # 值班台可代为录入
    Role.VIEWER: frozenset(),
}


def can(role: str, action: str) -> bool:
    return action in PERMISSIONS.get(role, frozenset())


# ---------------------------------------------------------------------------
# 枚举与常量
# ---------------------------------------------------------------------------

class EventStatus(StrEnum):
    SCHEDULED = "scheduled"
    RESCHEDULED = "rescheduled"
    CANCELLED = "cancelled"
    FINISHED = "finished"


class MerchantSector(StrEnum):
    LODGING = "lodging"      # 住宿
    DINING = "dining"        # 餐饮
    RETAIL = "retail"        # 零售
    CULTURE_TOURISM = "culture_tourism"  # 文旅（含商圈第二现场）


class CommitmentKind(StrEnum):
    PRICE = "price"                  # 带有效期的价格承诺（禁止临时加价）
    PACKAGE = "package"              # 套餐：可核销，带承诺量
    BENEFIT = "benefit"              # 官方权益承接：可核销，带承诺量
    VENUE_HOURS = "venue_hours"      # 第二现场等开放时段承诺（提前关闭即违约）


class OrderStatus(StrEnum):
    CREATED = "created"
    REJECTED = "rejected"
    REDEEMED = "redeemed"
    REVERSED = "reversed"
    REFUNDED = "refunded"


class ComplaintStatus(StrEnum):
    OPEN = "open"
    INVESTIGATING = "investigating"
    APPEALING = "appealing"
    CLOSED = "closed"


class DispositionKind(StrEnum):
    """处置类型；cost_bearer（最终承担方）由 decide_bearer 统一裁决。"""

    REFUND = "refund"
    COMPENSATION = "compensation"


class Bearer(StrEnum):
    """处置成本的最终承担方。"""

    MERCHANT = "merchant"      # 商户违约（加价/拒单/无法核销/提前关闭）
    ORGANIZER = "organizer"    # 主办方原因（改期/取消导致权益失效）
    FORCE_MAJEURE = "force_majeure"  # 极端气象等不可抗力：走联合保障金
    JOINT_FUND = "joint_fund"  # 联合保障金兜底
    NONE = "none"              # 申诉成立、撤销处置等，无人承担


class AlertType(StrEnum):
    PRICE_SPIKE = "price_spike"                 # 价格突变
    CLUSTERED_REJECTION = "clustered_rejection"  # 集中拒单
    BENEFIT_INVALIDATION = "benefit_invalidation"  # 权益失效


class WeatherLevel(StrEnum):
    NORMAL = "normal"
    EXTREME = "extreme"   # 极端气象：改期/取消按不可抗力处理


# 预警阈值（集中拒单）。
REJECTION_WINDOW_MINUTES = 60
REJECTION_THRESHOLD = 3
# 价格突变：同赛事同商户同类目，短时间内涨幅阈值。
PRICE_SPIKE_RATIO = 0.5  # 50%


# ---------------------------------------------------------------------------
# 异常
# ---------------------------------------------------------------------------

class GuardError(Exception):
    """业务错误基类，code 用于映射 HTTP 状态与错误体。"""

    http_status = 400

    def __init__(self, message: str, *, code: str = "bad_request", status: int | None = None):
        super().__init__(message)
        self.code = code
        if status is not None:
            self.http_status = status


class AuthError(GuardError):
    def __init__(self, message: str = "未授权：缺少或无效的令牌"):
        super().__init__(message, code="unauthorized", status=401)


class PermissionDenied(GuardError):
    def __init__(self, role: str, action: str):
        super().__init__(f"角色 {role} 无权执行 {action}", code="forbidden", status=403)
        self.role = role
        self.action = action


class NotFound(GuardError):
    def __init__(self, resource: str, key: str):
        super().__init__(f"{resource} 不存在: {key}", code="not_found", status=404)


class Conflict(GuardError):
    def __init__(self, message: str, *, code: str = "conflict"):
        super().__init__(message, code=code, status=409)


class StateError(GuardError):
    def __init__(self, message: str):
        super().__init__(message, code="state_invalid", status=422)


# ---------------------------------------------------------------------------
# 纯业务规则
# ---------------------------------------------------------------------------

def require_permission(role: str, action: str) -> None:
    if not can(role, action):
        raise PermissionDenied(role, action)


@dataclass(frozen=True)
class PolicyDecision:
    """政策版本适用裁决。"""

    policy_id: str
    version: int
    reason: str


def applicable_policy(policies: Iterable[dict], at_iso: str) -> PolicyDecision:
    """政策变更只影响生效后的交易。

    policies: 每项含 policy_id/version/effective_at；按 effective_at 排序，
    交易时间 at_iso 时，适用“最后一个 effective_at <= 交易时间”的版本；
    之前的交易不受新版本影响（时点原则）。
    """
    ordered = sorted(policies, key=lambda p: p["effective_at"])
    chosen: dict | None = None
    for p in ordered:
        if p["effective_at"] <= at_iso:
            chosen = p
        else:
            break
    if chosen is None:
        raise StateError("交易时间早于任何已发布政策，无法适用")
    return PolicyDecision(
        policy_id=chosen["policy_id"],
        version=chosen["version"],
        reason=f"交易发生于 {at_iso}，适用 {chosen['effective_at']} 生效的第 {chosen['version']} 版政策",
    )


def decide_bearer(*, root_cause: str, weather_level: str = WeatherLevel.NORMAL) -> Bearer:
    """裁决每笔处置最终由谁承担。

    - merchant_violation 系列：商户承担。
    - event_reschedule / event_cancel 且伴随极端气象：不可抗力→联合保障金。
    - event_reschedule / event_cancel 非气象原因：主办方承担。
    - appeal_upheld / withdrawn：无人承担（撤销）。
    """
    merchant_causes = {
        "price_surge", "package_unredeemable", "venue_early_close",
        "clustered_rejection", "merchant_suspended",
    }
    organizer_causes = {"event_reschedule", "event_cancel"}
    no_cost_causes = {"appeal_upheld", "withdrawn", "duplicate"}

    if root_cause in merchant_causes:
        return Bearer.MERCHANT
    if root_cause in organizer_causes:
        if weather_level == WeatherLevel.EXTREME:
            return Bearer.FORCE_MAJEURE
        return Bearer.ORGANIZER
    if root_cause in no_cost_causes:
        return Bearer.NONE
    # 未知根因默认进入联合保障金，避免无人认领。
    return Bearer.JOINT_FUND


def redeemable_kinds() -> frozenset[str]:
    return frozenset({CommitmentKind.PACKAGE, CommitmentKind.BENEFIT})


@dataclass
class PriceWindow:
    """带有效期的价格/履约承诺。"""

    valid_from: str
    valid_to: str

    def covers(self, at_iso: str) -> bool:
        return self.valid_from <= at_iso <= self.valid_to


@dataclass
class AlertContext:
    """生成可解释预警时的上下文快照。"""

    alert_type: str
    event_id: str
    subject_id: str
    severity: str            # info / warning / critical
    title: str
    explanation: str
    evidence: dict = field(default_factory=dict)
    related: list[str] = field(default_factory=list)  # 承诺/订单/事件链 id


def explain_price_spike(*, event_id: str, merchant_id: str, sector: str,
                        old_price: float, new_price: float,
                        commitment_id: str, observed_at: str) -> AlertContext:
    ratio = (new_price - old_price) / old_price if old_price else 0.0
    return AlertContext(
        alert_type=AlertType.PRICE_SPIKE,
        event_id=event_id,
        subject_id=merchant_id,
        severity="critical" if ratio >= PRICE_SPIKE_RATIO else "warning",
        title=f"{sector} 商户 {merchant_id} 价格突变 {(ratio * 100):.1f}%",
        explanation=(
            f"商户在承诺有效期内将价格由 {old_price:.2f} 上调至 {new_price:.2f}，"
            f"涨幅 {ratio * 100:.1f}%，突破带有效期价格承诺（{commitment_id}），"
            "疑似散场临时加价。"
        ),
        evidence={
            "old_price": old_price, "new_price": new_price,
            "increase_ratio": round(ratio, 4),
            "threshold_ratio": PRICE_SPIKE_RATIO,
            "observed_at": observed_at, "sector": sector,
        },
        related=[commitment_id],
    )


def explain_clustered_rejection(*, event_id: str, merchant_id: str,
                                order_refs: list[str], window_minutes: int,
                                observed_at: str) -> AlertContext:
    return AlertContext(
        alert_type=AlertType.CLUSTERED_REJECTION,
        event_id=event_id,
        subject_id=merchant_id,
        severity="critical",
        title=f"商户 {merchant_id} 在 {window_minutes} 分钟内集中拒单 {len(order_refs)} 笔",
        explanation=(
            f"同一商户在 {window_minutes} 分钟滑动窗口内拒单 {len(order_refs)} 笔"
            f"（阈值 {REJECTION_THRESHOLD}），疑似无正当理由集中拒履约，"
            "需市场监管介入调查。"
        ),
        evidence={
            "count": len(order_refs), "orders": order_refs,
            "window_minutes": window_minutes,
            "threshold": REJECTION_THRESHOLD, "observed_at": observed_at,
        },
        related=order_refs,
    )


def explain_benefit_invalidation(*, event_id: str, benefit_code: str,
                                 reason: str, weather_level: str,
                                 valid_from: str, valid_to: str,
                                 unredeemed: int) -> AlertContext:
    severity = "critical" if unredeemed > 0 else "info"
    cause = "极端气象不可抗力" if weather_level == WeatherLevel.EXTREME else "赛事安排变更"
    return AlertContext(
        alert_type=AlertType.BENEFIT_INVALIDATION,
        event_id=event_id,
        subject_id=benefit_code,
        severity=severity,
        title=f"官方权益 {benefit_code} 因{cause}失效，尚存 {unredeemed} 份未核销",
        explanation=(
            f"权益 {benefit_code}（有效期 {valid_from} ~ {valid_to}）因{reason}失效；"
            f"仍有 {unredeemed} 份未核销，需按承担规则处置："
            f"{'极端气象→联合保障金' if weather_level == WeatherLevel.EXTREME else '主办方承担'}。"
        ),
        evidence={
            "benefit_code": benefit_code, "reason": reason,
            "weather_level": weather_level,
            "valid_from": valid_from, "valid_to": valid_to,
            "unredeemed_count": unredeemed,
        },
    )
