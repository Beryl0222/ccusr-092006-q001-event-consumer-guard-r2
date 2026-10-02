# 赛事消费异常联防后端

一套可独立运行的赛事消费异常联防服务：主办方登记赛程、预计客流与官方权益；住宿、餐饮、零售、文旅商户发布带有效期的价格与履约承诺；市场监管、文旅、赛事运营人员在各自权限内处理投诉、停业、极端气象改期与活动取消。系统对**价格突变、集中拒单、权益失效、第二现场提前关闭**生成可解释预警，保留调查、申诉、退款、补偿的完整事件链，支持按一场赛事还原消费影响，并逐笔核对处置最终由谁承担。

仅依赖 **Python 3.11+ 标准库**（`http.server` + SQLite WAL），无需数据库服务或第三方包。

## 快速开始

```bash
# 独立运行（默认 127.0.0.1:8080，数据落 data/guard.db），免安装：
python3 run_server.py
# 或指定参数
python3 run_server.py --host 0.0.0.0 --port 8080 --db data/guard.db
# 以模块方式运行（未 pip install 时需指定源码路径）
PYTHONPATH=src python3 -m guard --port 8080

# 测试
python3 -m unittest discover -s tests
```

## 目录

- `src/guard/db.py`：SQLite schema、领域写操作（单连接 + RLock 串行化写事务，WAL 读并发）。
- `src/guard/engine.py`：纯函数规则引擎——政策时效、预警阈值评估、承担方推导。
- `src/guard/server.py`：HTTP API、RBAC/部门管辖、`Idempotency-Key` 重放。
- `src/guard/__main__.py` / `run_server.py`：启动入口。
- `tests/`：规则引擎、存储层（含 30 线程并发核销/赔付）、HTTP 端到端全流程。
- `src/event_consumer_guard.py`：早期领域术语表（事件名与最小字段），保持兼容。

## 鉴权模型

无外部依赖的请求头约定（生产由网关替换）：

| 请求头 | 取值 |
| --- | --- |
| `X-Actor-Id` | 人员账号 |
| `X-Actor-Role` | `ORGANIZER`（主办方）/ `MERCHANT`（商户）/ `OFFICER`（执法人员） |
| `X-Actor-Dept` | 执法人员必填：`MARKET_REGULATION` / `TOURISM` / `EVENT_OPERATION` |
| `Idempotency-Key` | 写请求幂等键；同键重放返回首次结果，同键不同体返回 422 |

部门管辖：停业、强制撤销承诺仅市监；第二现场登记、文旅类案件处置归文旅；气象改期/取消、权益失效归赛事运营；案件只能由当前归属部门处置，跨部门先 `TRANSFER`。

## 核心规则如何落地

| 诉求 | 实现 |
| --- | --- |
| 同一订单/投诉多渠道重传不重复赔付 | 订单、核销、投诉、处置均有唯一幂等键；投诉另按 `business_key` 业务去重；处置按 **(订单, 类型)** 唯一，重复发起返回 `409 DUPLICATE_REMEDY` |
| 政策变更只影响生效后的交易 | 订单创建时固化 `policy_version`，处置沿用该版本解析阈值与承担方；新版政策发布时自动截断上一版生效区间 |
| 并发核销不得突破承诺量 | 核销在写锁内“判定 + 计数”原子完成；超量返回 `SOLD_OUT` 且不增加计数（有 30 线程压测） |
| 价格突变预警 | 承诺有效期内相邻报价涨幅超政策阈值（默认 20%）即告警，证据含原价、新价、涨幅、阈值、有效期 |
| 集中拒单预警 | 滚动窗口（默认 60 分钟）内拒单数（默认 ≥5）与占比（默认 ≥50%）同时越线 |
| 权益失效可解释 | 场次取消批量失效官方权益，载荷记录 `causation`（如 SESSION_CANCELLED）并告警；失效/过期后核销拒绝 |
| 第二现场提前关闭 | 提前超过宽限分钟数（默认 30）才告警，宽限内只在事件链留痕 |
| 完整事件链与承担方 | `ledger` 表只追加（触发器禁止 UPDATE/DELETE）；`GET /api/remedies/{id}/trace` 返回提议→核准→支付链与最终承担方及解释 |
| 按赛事还原消费影响 | `GET /api/events/{id}/impact` 汇总场次、承诺/核销、订单、投诉、赔付（按承担方分类）、预警 |

承担方由政策 `bearer_map` 推导，例如：临时加价退款→涉事商户本人；第二现场提前关闭补偿→`GOVERNMENT_FUND:district-relief`；赛事取消退款→`ORGANIZER:event-risk-reserve`。

## API 一览

赛事与权益（主办方）
- `POST /api/events`、`GET /api/events/{id}`
- `POST /api/events/{id}/sessions`、`GET /api/events/{id}/sessions`
- `POST /api/sessions/{id}/weather`（极端气象标记）
- `POST /api/sessions/{id}/reschedule`、`POST /api/sessions/{id}/cancel`
- `POST /api/events/{id}/benefits`、`GET /api/benefits/{id}`、`POST /api/benefits/{id}/invalidate`
- `POST /api/events/{id}/early-closures`（文旅登记第二现场提前关闭）
- `GET /api/events/{id}/impact`、`GET /api/events/{id}/timeline`

商户与承诺
- `POST /api/merchants`、`GET /api/merchants/{id}`
- `POST /api/merchants/{id}/suspend`（市监）、`POST /api/merchants/{id}/resume`
- `POST /api/merchants/{id}/commitments`、`GET /api/commitments/{id}`
- `POST /api/commitments/{id}/prices`（报价；可附 `quoted_at`）
- `POST /api/commitments/{id}/revoke`

订单与核销
- `POST /api/orders`、`GET /api/orders/{id}`、`POST /api/orders/{id}/reject`、`POST /api/orders/{id}/fulfill`
- `POST /api/redemptions`（`commitment_id` 或 `benefit_id` 二选一）

投诉与处置
- `POST /api/complaints`、`GET /api/complaints`、`GET /api/complaints/{id}`
- `POST /api/complaints/{id}/handle`：`INVESTIGATE` / `TRANSFER` / `RESOLVE` / `DISMISS` / `APPEAL` / `REOPEN`
- `POST /api/remedies`、`GET /api/remedies/{id}`、`POST /api/remedies/{id}/approve|pay|reverse`
- `GET /api/remedies/{id}/trace`

预警、政策与审计
- `GET /api/alerts`、`GET /api/alerts/{id}`、`POST /api/alerts/{id}/ack|close`
- `GET/POST /api/policies`
- `GET /api/ledger/{aggregate_type}/{aggregate_id}`
- `GET /healthz`（免鉴权）

### 最小调用示例

```bash
curl -s -XPOST localhost:8080/api/events \
  -H 'X-Actor-Id: org1' -H 'X-Actor-Role: ORGANIZER' -H 'Content-Type: application/json' \
  -d '{"name":"周末城市德比","expected_traffic":12000}'
```

## 政策版本

服务首次启动写入 v1 基线政策（1999 起长期有效）。发布新版本：

```json
POST /api/policies
{
  "effective_from": "2026-10-05T00:00:00+08:00",
  "note": "加价补偿改由财政纾困专户承担，价格阈值收紧到5%",
  "rules": {
    "price_spike_ratio": 0.05,
    "mass_rejection_count": 5,
    "mass_rejection_window_minutes": 60,
    "mass_rejection_ratio": 0.50,
    "early_closure_grace_minutes": 30,
    "bearer_map": { "PRICE_GOUGE": {
        "REFUND": ["MERCHANT", "self"],
        "COMPENSATION": ["GOVERNMENT_FUND", "district-relief"] } }
  }
}
```

`effective_from` 之前的订单永远适用旧版本。

## 错误模型

```json
{ "error": { "code": "DUPLICATE_REMEDY", "message": "订单 ord_xxx 的 REFUND 已存在（PAID：rem_xxx），不得重复赔付" } }
```

常见码：`UNAUTHENTICATED(401)`、`FORBIDDEN/OUT_OF_JURISDICTION(403)`、`IDEMPOTENCY_CONFLICT(422)`、
`DUPLICATE_REMEDY(409)`、`SOLD_OUT`、`OUTSIDE_VALID_WINDOW`、`BENEFIT_INVALIDATED`、
`COMMITMENT_REVOKED`、`MERCHANT_SUSPENDED`、`COMPLAINT_NOT_INVESTIGATING`、`NO_POLICY`。
