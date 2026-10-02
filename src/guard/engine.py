"""纯函数规则引擎：政策时效、可解释预警、承担方推导。

本模块不触碰数据库，便于单独测试；所有阈值均来自当刻生效政策的
``rules`` JSON，阈值调整不需要改代码。
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

# ---------------------------------------------------------------------------
# 时间工具（统一 ISO-8601；允许无时区写法，按 UTC 处理）
# ---------------------------------------------------------------------------


def parse_ts(value: str) -> datetime:
    dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=datetime.timezone.utc)
    return dt


def minutes_between(start: str, end: str) -> int:
    return int((parse_ts(end) - parse_ts(start)).total_seconds() // 60)


# ---------------------------------------------------------------------------
# 预警 1：价格突变（临时加价）
# ---------------------------------------------------------------------------


def evaluate_price_spike(previous_cents: int, new_cents: int, rules: dict[str, Any],
                         *, commitment_valid_from: str, commitment_valid_to: str,
                         quoted_at: str | None = None) -> dict[str, Any] | None:
    """对比最近一次报价；超过政策阈值则返回可解释结论。

    降价不预警；证据包含阈值、涨幅、承诺有效期，便于值班人员解释。
    """
    if previous_cents <= 0 or new_cents < previous_cents or new_cents == previous_cents:
        return None
    ratio = (new_cents - previous_cents) / previous_cents
    threshold = float(rules.get("price_spike_ratio", 0.20))
    if ratio <= threshold:
        return None
    if quoted_at is not None and not (commitment_valid_from <= quoted_at <= commitment_valid_to):
        return None
    severe = ratio >= threshold * 2
    return {
        "severity": "HIGH" if severe else "WARN",
        "reason": (f"报价在承诺有效期内由 {previous_cents} 分上调至 {new_cents} 分，"
                   f"涨幅 {ratio:.1%}，超过政策阈值 {threshold:.0%}"),
        "evidence": {
            "previous_cents": previous_cents,
            "new_cents": new_cents,
            "increase_ratio": round(ratio, 4),
            "threshold_ratio": threshold,
            "valid_from": commitment_valid_from,
            "valid_to": commitment_valid_to,
        },
    }


# ---------------------------------------------------------------------------
# 预警 2：集中拒单
# ---------------------------------------------------------------------------


def evaluate_mass_rejection(orders: list[dict[str, Any]], rules: dict[str, Any],
                            now_iso: str) -> dict[str, Any] | None:
    """滚动窗口内拒单数量与占比同时越线即预警。"""
    window_min = int(rules.get("mass_rejection_window_minutes", 60))
    min_count = int(rules.get("mass_rejection_count", 5))
    min_ratio = float(rules.get("mass_rejection_ratio", 0.50))
    now = parse_ts(now_iso)
    cutoff = now - timedelta(minutes=window_min)
    recent = [o for o in orders if parse_ts(o["created_at"]) >= cutoff]
    rejected = [o for o in recent if o["status"] == "REJECTED"]
    total = len(recent)
    if total == 0:
        return None
    ratio = len(rejected) / total
    if len(rejected) < min_count or ratio < min_ratio:
        return None
    return {
        "severity": "HIGH",
        "reason": (f"近 {window_min} 分钟内拒单 {len(rejected)} 笔、占比 {ratio:.0%}，"
                   f"达到集中拒单阈值（≥{min_count} 笔且 ≥{min_ratio:.0%}）"),
        "evidence": {
            "window_minutes": window_min,
            "recent_orders": total,
            "rejected_orders": len(rejected),
            "rejection_ratio": round(ratio, 4),
            "threshold_count": min_count,
            "threshold_ratio": min_ratio,
            "order_ids": [o["id"] for o in rejected][:20],
        },
    }


# ---------------------------------------------------------------------------
# 预警 3：商圈第二现场提前关闭
# ---------------------------------------------------------------------------


def evaluate_early_closure(scheduled_close_at: str, actual_close_at: str,
                           rules: dict[str, Any]) -> dict[str, Any] | None:
    """提前关闭超过政策宽限分钟数才预警；宽限内只记录不告警。"""
    grace = int(rules.get("early_closure_grace_minutes", 30))
    early = minutes_between(actual_close_at, scheduled_close_at)
    if early <= grace:
        return None
    return {
        "severity": "HIGH" if early >= 120 else "WARN",
        "reason": (f"实际关闭时间 {actual_close_at} 早于公示关闭时间 {scheduled_close_at} "
                   f"{early} 分钟，超过 {grace} 分钟宽限"),
        "evidence": {
            "scheduled_close_at": scheduled_close_at,
            "actual_close_at": actual_close_at,
            "early_minutes": early,
            "grace_minutes": grace,
        },
    }


# ---------------------------------------------------------------------------
# 处置最终承担方推导
# ---------------------------------------------------------------------------


def bearer_for(category: str, kind: str, rules: dict[str, Any],
               merchant_id: str | None = None) -> tuple[str, str, str]:
    """返回 ``(bearer_type, bearer_id, 解释)``。

    政策 ``bearer_map[category][kind]`` 形如 ``["MERCHANT", "self"]``：
    - MERCHANT + "self" 会解析为具体商户 id，赔付可对追到店；
    - GOVERNMENT_FUND / ORGANIZER 搭配专户名称（如 district-relief）。
    """
    bearer_map = rules.get("bearer_map", {})
    mapping = bearer_map.get(category) or bearer_map.get("OTHER") or {}
    spec = mapping.get(kind) or mapping.get("REFUND")
    if not spec:
        spec = ["GOVERNMENT_FUND", "district-relief"]
    btype, target = spec[0], spec[1]
    if btype == "MERCHANT" and target == "self":
        bid = merchant_id or "unknown-merchant"
        reason = f"政策规定 {category} 类 {kind} 由涉事商户自行承担（{bid}）"
    elif btype == "ORGANIZER":
        bid = target
        reason = f"政策规定 {category} 类 {kind} 由主办方风险准备金 {target} 承担"
    else:
        bid = target
        reason = f"政策规定 {category} 类 {kind} 由财政/商圈纾困专户 {target} 承担"
    return btype, bid, reason


# ---------------------------------------------------------------------------
# 政策时效：交易时点决定适用版本（不溯及既往）
# ---------------------------------------------------------------------------


def resolve_policy_version(policies: list[dict[str, Any]], at_iso: str) -> int | None:
    """给定全部政策（含 effective_from/effective_to），返回某时刻适用版本。"""
    at = parse_ts(at_iso)
    candidates = []
    for p in policies:
        start = parse_ts(p["effective_from"])
        end = parse_ts(p["effective_to"]) if p.get("effective_to") else None
        if start <= at and (end is None or at < end):
            candidates.append((int(p["version"]), p))
    if not candidates:
        return None
    return max(candidates, key=lambda x: x[0])[0]
