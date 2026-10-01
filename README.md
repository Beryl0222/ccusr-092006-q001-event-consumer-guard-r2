# 赛事消费异常联防后端（event-consumer-guard）

一套**仅依赖 Python 3.11 标准库、可独立运行**的赛事消费异常联防后端。
面向周末联赛散场后的三类高发问题——酒店临时加价、套餐无法核销、
商圈第二现场提前关闭——把主办方、住宿/餐饮/零售/文旅商户、市场监管、
文旅与赛事运营纳入同一套联防流程，杜绝跨部门推诿。

## 它保证了什么

| 业务要求 | 实现机制 |
| --- | --- |
| 主办方先登记赛程、预计客流与官方权益 | `POST /v1/events`、`POST /v1/benefits`（总量+有效期） |
| 商户发布带有效期的价格与履约承诺 | `POST /v1/commitments`：`price/package/benefit/venue_hours`，均带 `valid_from/valid_to` |
| 同一订单/投诉多渠道重传不重复赔付 | `(channel, channel_ref)` 唯一键 + `dedup_key` 唯一索引；处置 `(complaint_id, kind)` 的 **settled 唯一索引**，重传返回原单 |
| 政策变更只影响生效后的交易 | 处置按**交易时点**适用政策版本（种子含 v1@2026-01-01、v2@2026-09-01） |
| 并发核销不得突破商户承诺量 | `BEGIN IMMEDIATE` + 条件更新 `WHERE redeemed_total+?<=promised_total` 原子扣减 |
| 价格突变 / 集中拒单 / 权益失效可解释预警 | 自动生成含标题、解释、证据快照与关联单号的预警（60 分钟 ≥3 拒单、涨幅 ≥50%、权益失效含未核销存量） |
| 极端气象改期/取消 | 赛事运营或主办方操作，权益批量失效并预警；处置承担方走不可抗力规则 |
| 调查→申诉→退款→补偿完整事件链 | 每个动作写 `event_chain`（含因果序号），可按订单/投诉/赛事还原 |
| 每笔处置最终由谁承担可核对 | 根因+气象快照裁决 `merchant/organizer/force_majeure/joint_fund/none`；`GET /v1/dispositions/{id}/reconcile` 反查政策版本与溯源链 |
| 各角色只在权限内操作 | Bearer 令牌 + 角色权限矩阵（403），停业商户被冻结接单/核销 |

## 目录

- `src/domain.py`：角色权限、承担方裁决、政策时点适用、预警解释（纯规则）。
- `src/db.py`：SQLite schema、种子令牌与政策、原子事务。
- `src/services.py`：全部业务用例与事件链。
- `src/api.py` / `src/__main__.py`：HTTP API 与启动入口。
- `src/demo.py`：不启网络的全链路业务走查。
- `data/sample.json`：脱敏交换样例；`tests/`：43 个测试。

## 快速开始

```bash
# 1) 看完整业务故事（散场加价→预警→投诉→处置→申诉→极端气象改期→对账）
python3 -m src.demo

# 2) 启动 HTTP 服务（自动建库 data/guard.db）
python3 -m src --host 127.0.0.1 --port 8080
#   环境变量：ECG_HTTP_HOST / ECG_HTTP_PORT / ECG_DB_PATH

# 3) 跑测试
python3 -m unittest discover -s tests
```

健康检查无需令牌：`GET /healthz`；其余接口需
`Authorization: Bearer <token>`，写接口建议带 `Idempotency-Key`
（同键重放首次响应，响应头带 `Idempotent-Replayed: true`）。

### 种子令牌（仅演示/测试，生产请轮换）

| 令牌 | 角色 | 令牌 | 角色 |
| --- | --- | --- | --- |
| `org-token` | 主办方 | `reg-token` | 市场监管 |
| `tour-token` | 文旅 | `op-token` | 赛事运营 |
| `staff-token` | 值班人员（只读+代录） | `mer-hotel/food/mall-token` | 三家演示商户 |

## API 一览（前缀 `/v1`）

- 赛事：`POST /events`；`POST /events/{id}/reschedule|cancel`；
  `GET /events/{id}`、`/events/{id}/impact`、`/events/{id}/timeline`
- 商户：`POST /merchants`、`POST /merchants/{id}/suspend|unsuspend`、`GET /merchants/{id}`
- 权益/承诺：`POST /benefits`、`POST /commitments`、`GET /benefits/{id}`、`GET /commitments/{id}`
- 订单/核销：`POST /orders`、`POST /orders/{id}/reject`、`POST /redemptions`、
  `POST /redemptions/{id}/reverse`、`GET /orders/{id}`
- 投诉处置：`POST /complaints`、`POST /complaints/{id}/investigations|appeals|refunds|compensations|close`、
  `POST /appeals/{id}/ruling`、`GET /complaints/{id}`
- 预警：`GET /alerts?event_id=&type=`、`POST /alerts/{id}/acknowledge`
- 事件链/对账：`GET /chain/{scope}/{subject_ref}`、
  `GET /dispositions/{id}/reconcile`、`GET /policies`

### 最小调用示例

```bash
curl -s localhost:8080/v1/events -H "Authorization: Bearer org-token" \
  -H 'Content-Type: application/json' -H 'Idempotency-Key: reg-1' -d '{
  "event_id":"league-final-2026","name":"城市周末联赛·决赛",
  "starts_at":"2026-10-03T15:00:00+08:00","ends_at":"2026-10-03T22:00:00+08:00",
  "expected_attendance":18000}'
```

退款/补偿请求体：`{"amount": 300, "root_cause": "price_surge",
"transaction_at": "2026-08-15T20:00:00+08:00"}`。
不传 `transaction_at` 时默认投诉创建时点；政策版本按该时点裁决，
**不因政策修订而溯及既往**。

### 承担方裁决规则

`price_surge / package_unredeemable / venue_early_close / clustered_rejection`
→ 商户；`event_reschedule / event_cancel` 常规天气 → 主办方，
极端气象（`weather_level=extreme`）→ 不可抗力（联合保障金）；
申诉成立 → 原处置冲正、承担方改 `none`。

## 设计说明

- 所有时间统一归一化为东八区 ISO 8601 秒精度，可直接字典序比较。
- 写操作在单事务内完成「状态变更 + 事件链」，异常整体回滚。
- SQLite 单连接 + 进程锁串行化（`BEGIN IMMEDIATE`），
  HTTP 多线程下核销等原子性由数据库条件更新保证；
  需要水平扩展时可把 Database 替换为同接口的 Postgres 实现。
- 本系统不含真实个人信息；消费者标识以 `consumer_ref` 脱敏传入。
