# Design

## Context

- 本 change 建立在 A 和 B 之上，两者都已归档。
- 当前状态：
  - schema 版本为 v2。
  - `claim_due` 只按到期时间取任务，不区分目标。
  - 截止时间由 `policy.deadline(created_at, expires_at)` 计算。
  - API 只有一个端点 `POST /notifications`。
- 动机见 proposal.md 的 Why 一节，需求见 specs/。

## Goals / Non-Goals

**Goals:**
- 一个下游持续超时时，其他下游的投递延迟不受影响，并用测试证明。
- 进入失败终态的通知可以查询、可以重放，重放后仍然遵守至少一次语义，并且沿用同一个通知 ID。
- 指标在 API 和 worker 两个进程之间保持一致，重启后不会归零。

**Non-Goals:**
- 熔断。按 host 限流只限制并发数，不会主动暂停对某个 host 的投递。
- 管理员角色、跨调用方查询、批量重放。
- 列表和搜索接口。
- 对接告警系统。只提供指标，告警规则由使用方自己配置。

## Decisions

### D1 按 host 限流在取任务时进行（候选人拍板）

- **选择**：
  - schema v3 新增 `target_host` 列。新通知在受理时，用 `urlsplit(url).hostname` 转成小写后写入；已有的数据在迁移时用 Python 解析 url 回填。
  - `claim_due` 改为一条带 CTE 的查询：
    1. `inflight`：统计每个 host 当前"投递中、且占用还没过期"的通知数。
    2. `due`：取出所有可以投递的通知（与 B 相同），用 `ROW_NUMBER() OVER (PARTITION BY target_host ORDER BY next_attempt_at, id)` 给每个 host 内的通知编号。
    3. 只保留编号不超过 `上限 - 该 host 当前投递中数量` 的行，再按 `next_attempt_at` 排序，取前 `limit` 条，然后执行 `UPDATE … RETURNING` 完成占用。
  - 每个 host 的上限默认 4，通过 `NOTIFIER_WORKER_PER_HOST_LIMIT` 配置，并且必须小于等于 `concurrency`。
- **理由**：状态保存在存储里，worker 重启后限流依然有效，也不排斥多个 worker 进程同时运行。按默认值，一个下游挂掉时最多占住 4 个线程，其余 12 个线程照常投递其他下游。
- **替代方案**：在 worker 内存里给每个 host 一个信号量。问题是任务取到之后要排队等名额，等的过程中占用期限一直在消耗，可能被别人重复取走。要避免，就得"取到后放回"，逻辑反而更复杂。
- **隔离粒度**：只按 hostname，不区分端口。同一个供应商用不同端口的情况很少见，而按 hostname 粒度更粗，对下游的保护也更强。
- **接受的局限**：每次轮询时，`due` 子查询要扫描当前所有可投递的通知。某个 host 积压了大量已到期的通知时，扫描成本会随积压量线性增长。在 MVP 的量级下可以接受。演进方向：先按 host 聚合再取任务，或者迁移到 PG。

### D2 重试窗口起点与重放（候选人拍板）

- schema v3 新增 `retry_window_start` 列，存整数毫秒。新通知写入受理时间；已有的数据回填为 `created_at`。
- `policy.deadline(window_start, expires_at, cfg)`：有效截止时间改为 `min(window_start + 最大重试时长, expires_at)`，其余规则与 B 相同。
- **重放**：执行一条条件 UPDATE：
  ```
  UPDATE … SET status='pending', failure_reason=NULL, finished_at=NULL,
               next_attempt_at=now, retry_window_start=now
  WHERE id=? AND caller=? AND status='failed' AND failure_reason<>'expired'
    AND (expires_at IS NULL OR expires_at > now)
  ```
  这条语句既做检查也做修改，原子完成，两个并发的重放请求不会都成功。如果影响行数为 0，再读一次这条通知，判断该返回 404 还是 409。
- **沿用的内容**：通知 ID 和 `attempt_count` 都不重置，所以 `X-Notification-Attempt` 会接着之前的编号继续递增。下游看到的是同一条通知又来了，可以据此去重。
- **不允许重放的情况**：
  - 原因为已过期，或者已经超过了 `expires_at`。业务给的期限是硬约束，重放不能绕过它。
  - 已送达。重复送达没有意义。
  - 尚未进入终态。这时系统还在负责投递，不需要人工介入。
- **替代方案**：
  - 不重置重试窗口：重放后，截止时间往往早已过去，通知一被取出就立即再次失败，重放等于没做。
  - 允许重放已过期的通知：会绕过业务约束。

### D3 查询接口与 Header 脱敏（候选人拍板）

- `GET /notifications/{id}` 使用和提交接口相同的 Bearer 认证。如果通知不存在，或者属于其他调用方，**一律返回 404**，不暴露它是否存在。
- **响应内容**：
  - 基本信息：`id`、`status`、`failure_reason`、`attempt_count`、`created_at`、`expires_at`、`finished_at`。
  - `next_attempt_at`：仅在待投递时返回，格式为 ISO 时间。
  - `request`：包括 `url`、`method`、`headers`、`body`。
  - `attempts[]`：每次尝试的 `attempt_no`、`started_at`、`duration_ms`、`outcome`、`status_code`、`error`、`response_excerpt`。
- **Header 脱敏用白名单**：`Content-Type`、`Accept`、`User-Agent`、`Content-Encoding`、`Accept-Language` 这几个显示原值，比较时不区分大小写；其余 Header 保留名字，值替换为 `***`。
  - 理由：各家供应商放密钥的 Header 名五花八门，白名单能做到默认安全。
  - 替代方案：按关键词做黑名单。能保留更多排障信息，但只要 Header 名起得不规范，密钥就会泄露。
- **接受的局限**：`url` 的查询串、`body` 和响应摘录都会原样返回给提交方本人，不做脱敏。阶段 1 已经决定 MVP 只对 Header 脱敏。如果 url 里带了 `?token=…` 这类参数，同样会原样返回。

### D4 指标（AI 自主决定）

- `GET /metrics` 由 API 进程提供，输出 Prometheus 文本格式（`text/plain; version=0.0.4`），代码手写，不引入 `prometheus_client`。
- 每次被抓取时，从存储中统计以下指标：

| 指标 | 类型 | 含义 |
|---|---|---|
| `notifier_notifications{status}` | gauge | 各状态的通知数 |
| `notifier_failed_notifications{reason}` | gauge | 各失败原因的通知数 |
| `notifier_backlog_due` | gauge | 已到期、尚未取走的待投递通知数 |
| `notifier_oldest_due_age_seconds` | gauge | 最早到期的待投递通知已经等了多久；没有时为 0 |
| `notifier_delivery_attempts_total{outcome}` | counter | 各结果分类的累计尝试次数 |

- **理由**：
  - 计数来自存储，所以 API 和 worker 两个进程之间不用共享内存，重启也不会归零。
  - `notifier_backlog_due` 和 `notifier_oldest_due_age_seconds` 能直接反映"下游长期不可用"或"worker 停了"这两类问题，是最值得配告警的指标。
- **替代方案**：使用 `prometheus_client`，在进程内维护计数器。问题是 API 和 worker 各有一份计数器，需要聚合，而且重启后会归零。
- **不按 host 输出指标**：host 的取值个数没有上限，作为标签可能让指标数量失控。
- **不做认证**：指标里只有计数，没有通知内容。

### D5 其他实现细节（AI 自主决定）

- **重放的响应**：成功时返回 `202 {"id", "status": "pending"}`。
- **API 内部结构**：三个新端点和提交接口放在同一个 `create_app` 里，共用同一个认证依赖。
- **schema 迁移机制**：迁移项改为既可以是一段 SQL，也可以是一个 Python 函数。v3 需要用 Python 解析 url 来回填 `target_host`。迁移仍然在 `BEGIN IMMEDIATE` 事务中执行。

## Risks / Trade-offs

- [某个 host 大量积压时，取任务的扫描变慢] → 见 D1。可以通过 `notifier_backlog_due` 观察。
- [查询结果包含 body 和 url 查询串中的敏感信息] → 只返回给提交方本人；已作为已知局限写明。
- [重放导致重复送达] → 沿用同一个通知 ID，下游可以据此去重。这是人工操作，操作者需要自行判断是否重放。
- [`/metrics` 每次被抓取都要执行几条 COUNT 查询] → 各条查询都有索引可用。抓取间隔通常在 15 秒以上，在 MVP 的量级下可以接受。
- [按 host 限流可能让同一 host 的通知排队变慢] → 这是有意为之。换来的是对下游的保护和对其他下游的隔离。

## 本 change 不保证的事项

- 不提供熔断，也不主动探活下游。
- 不对 body、url 查询串和响应摘录做脱敏。
- 重放不会绕过 `expires_at`。
- 不提供跨调用方的管理视图。

## Migration Plan

API 或 worker 启动时，通过 `init_schema` 把 schema 从 v2 升级到 v3。升级内容：新增两个字段；用 Python 回填 `target_host`，`retry_window_start` 回填为 `created_at`；为 `(target_host, status)` 建索引。

升级后，已有的待投递通知立即受到按 host 限流的约束。
