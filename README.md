# API 通知投递服务

业务系统把要发送的外部 HTTP 请求（URL / Method / Header / Body）提交给本服务，由本服务负责可靠地投递到目标地址；业务系统不需要等待结果。题目见 [docs/assignment.md](docs/assignment.md)。

> 三个 change 都已完成：受理与持久化（A）、投递引擎（B）、隔离与运维（C）。拆分计划见 [mydoc/plans/notification-delivery/](mydoc/plans/notification-delivery/)，AI 建议的处理记录见 [docs/ai-review.md](docs/ai-review.md)。

## 问题理解

题目要的本质上是一个"可靠的出站 HTTP 投递"服务。业务系统只负责说清楚"发什么、发到哪"，然后就可以离开。真正的难点在于下游不可控：它可能很慢，可能长时间宕机，也可能重复处理同一条通知。

系统边界：本服务**透传**业务方提交的完整请求，不理解各个供应商的报文格式。这样做的代价是，凭证散落在各个业务方手里，带时效的签名在重试时会失效，"返回 200 但 body 里是业务错误"的情况也识别不出来。这些都作为已知局限接受。完整的边界清单见 [plan v1](mydoc/plans/notification-delivery/notification-delivery-v1.md) 的 Out of scope 一节。

## 整体架构

```
业务系统 ── POST /notifications ──▶ API 进程（FastAPI）
                                    │ 认证 → 字段校验 → 地址防护 → 幂等去重
                                    │ 事务 COMMIT 之后才返回 202
                                    ▼
                         SQLite（WAL，synchronous=FULL）
                         notifications ／ delivery_attempts
                                    ▲
                                    │ claim_due：取出到期的、以及占用已过期的通知，按 host 限流
                                    │ 写回结果时凭 lease_token 做 fencing
                         worker 进程（线程池，默认 16）
                                    │ 发送前：先检查截止时间，再做严格地址校验
                                    ▼
                         外部供应商 API（请求带 X-Notification-Id 和 X-Notification-Attempt）

运维接口都在 API 进程中：GET /notifications/{id}、POST /notifications/{id}/replay、GET /metrics
```

通知状态机：

```
pending ──取出（获得占用）──▶ delivering ──2xx──▶ delivered
   ▲                            │
   └── 临时失败（按退避排期） ◀──┤
                                ├── 永久失败 / 到达截止时间 / 目标被禁止 ──▶ failed
                                └── 占用过期（worker 崩溃或卡住）：被重新取出
failed ──重放（失败原因不是 expired）──▶ pending
```

| 模块 | 职责 |
|---|---|
| `app.py` | HTTP 入口：提交、查询、重放、指标 |
| `models.py` | 提交请求的字段校验和请求指纹 |
| `netguard.py` | 目标地址防护，DNS 解析带超时 |
| `store.py` | 全部 SQL：schema 迁移、占用、带 fencing 的写回、重放、统计 |
| `policy.py` | 纯函数：结果分类、退避间隔、截止时间 |
| `delivery.py` | 单次投递：发送前检查 → 发送 → 分类 → 写回 |
| `worker.py` | 投递循环、优雅退出、进程入口 |
| `views.py` | 查询结果的 JSON（含 Header 脱敏）和 Prometheus 指标文本 |

## 可靠性承诺

> 通知一旦受理，本服务会持续投递，直到出现以下三种结果之一：确认成功（2xx）；判定为永久失败（3xx，或 408、429 以外的 4xx）；到达截止时间。截止时间取"受理后 24 小时（可配置）"和调用方给的 `expires_at` 中较早的那个。后两种结果进入失败终态，并记录原因。失败终态的通知可以查询，也可以人工重放（原因为已过期的除外）。

投递语义是**至少一次**：每次投递都带 `X-Notification-Id`（同一条通知的所有尝试都相同）和 `X-Notification-Attempt`（从 1 开始），下游可以据此去重。本服务不承诺 exactly-once，也不保证顺序。

| 规则 | 机制 | 证据 | 状态 |
|---|---|---|---|
| 1. 先落盘，再返回"已受理" | SQLite 开 WAL、`synchronous=FULL`，事务 COMMIT 后才返回 202；写入失败返回 503 | `tests/test_crash_recovery.py`：SIGKILL 后重启，数据不丢；`tests/test_api.py`：注入故障和锁超时时都返回 503 | ✅ A |
| 2. 收到 2xx 后才标记为已送达 | `deliver_one` 只在分类结果为成功时调用 `mark_delivered` | `tests/test_delivery.py`、`tests/test_worker.py`：先 500 后 200 的场景 | ✅ B |
| 3. 用带期限的占用取出通知 | 一条 `UPDATE … RETURNING` 同时取出到期的通知和占用已过期的通知；写回时凭 `lease_token` 做 fencing，旧占用的结果会被丢弃 | `tests/test_store_delivery.py`：占用过期、fencing；`tests/test_worker_process.py`：worker 发出请求后被 SIGKILL，重启后重投。把"占用过期可重取"这条逻辑去掉后，这个测试会失败 | ✅ B |
| 4. 重试计划持久化 | 下一次重试时间存在 `next_attempt_at`（整数毫秒）里，不使用内存定时器 | `tests/test_worker.py::test_restart_does_not_retry_early` | ✅ B |
| 5. 结果不明时按失败处理 | 超时、连接错误、请求发出后崩溃，都会进入重试 | `tests/test_delivery.py::test_processed_but_timed_out_is_redelivered_with_same_id` | ✅ B |
| 6. 每次投递带同一个通知 ID | 系统写入的 Header 会覆盖提交时的同名项 | 同上，外加 SIGKILL 测试：两次请求的 ID 相同，attempt 分别为 1 和 2 | ✅ B |
| 隔离：一个下游挂掉不拖累其他下游 | 取任务时按 host 限制并发，默认每个 host 4 条；限流状态存在库里，重启后仍然生效 | `tests/test_isolation.py`：X 卡死时，Y 在 3s 内全部送达。对照组不按 host 限流，Y 会被堵住 | ✅ C |
| 终态可查询、可重放、可观测 | `GET /notifications/{id}`、`POST …/replay`、`GET /metrics` | `tests/test_ops_api.py`、`tests/test_ops_store.py`。并发重放时只有一个能成功 | ✅ C |

**失败分类**：

| 结果 | 处理 |
|---|---|
| 2xx | 已送达 |
| 3xx（不跟随重定向）；408、429 以外的 4xx | 永久失败 |
| 5xx、408、429、超时、连接错误、DNS 解析失败 | 指数退避后重试：首次 10s，之后每次翻倍，上限 1 小时，带 ±20% 抖动；响应带 `Retry-After` 时按它安排 |

**截止时间**：每次发送前都会检查，到期就不再发送，失败原因按先到的那个期限记录（重试耗尽或已过期）。如果下一次重试已经来不及，就在截止时间把通知标记为失败，不再发请求。我们不会为了"最后再试一次"去提前重试。

**前提**：
- 存储介质本身不损坏（MVP 是单机部署）。
- 业务方在没收到受理回执时，会用同一个幂等 key 重试。
- SIGKILL 测试只能证明进程崩溃时不丢数据；掉电时不丢数据依赖 fsync，没有实测过。

## 关键决策

各条决策的来源如下：
- **截止时间**：采用候选人自己提出的规则。
- **指标**：由 AI 自主决定，候选人验收通过。
- **其余各条**：由 AI 给出推荐方案和替代方案，候选人拍板。

| 决策 | 选择 | 主要替代方案与不选的理由 |
|---|---|---|
| 存储 | SQLite（标准库） | PostgreSQL：多实例友好，但要额外部署，MVP 的量级用不上，作为演进方向。消息队列：延迟重试不好做，而且查询和幂等仍然需要一个数据库 |
| Web 框架 | FastAPI + uvicorn | 标准库 `http.server`：零依赖，但校验、路由、错误处理都要手写 |
| 幂等 | key 必填；同一 key 内容不同时返回 409 | key 可选：重试时会产生重复。静默返回旧 ID：会掩盖调用方的 bug |
| 地址防护 | 拒绝内网、回环、链路本地和元数据地址。受理时 DNS 解析失败或超过 2s 照常放行；每次投递前用严格模式重新校验 | 完整的目标白名单：每加一个目标都要登记，MVP 不做 |
| worker 进程模型 | 独立进程，和 API 共用同一个 SQLite 文件 | API 进程内的后台线程：API 崩溃时投递会一起停，uvicorn 多进程部署时还会起多份投递循环 |
| 并发模型 | 固定大小线程池（默认 16）+ 同步 httpx | asyncio：并发上限更高，但会同步、异步混用，测试也更复杂 |
| DNS rebinding | 投递前校验，接受残余窗口 | 严格绑定已校验的 IP：要自己处理 SNI 和证书，成本高，作为演进项 |
| 按目标隔离 | 取任务时用 SQL 按 host 限流 | worker 内存信号量：任务排队等名额时，占用期限也在消耗 |
| 重放 | 重置重试窗口；沿用同一个通知 ID 和尝试序号；已过期的通知拒绝重放 | 不重置窗口：重放后会立刻再次失败。允许重放已过期的通知：会绕过业务约束 |
| 查询脱敏 | Header 白名单，其余值显示为 `***` | 关键词黑名单：Header 名不规范时密钥会泄露 |
| 指标 | 抓取时从库里统计，手写 Prometheus 文本格式 | `prometheus_client`：两个进程各有一份计数器，重启后会归零 |
| 截止时间 | min(重试期限, `expires_at`)，到期就不再发送，不做"最后再试一次" | AI 最初方案会在通知还没过期时就把它标成已过期，被候选人发现后否决；随后提出的"提前补一次尝试"也被否决，理由是规则更简单 |

每项决策的完整理由见 `openspec/changes/archive/*/design.md`，AI 建议的采纳和否决记录见 [docs/ai-review.md](docs/ai-review.md)。

## 已知局限

- **重复送达**：超时，或请求发出后 worker 崩溃，都会触发重投。下游需要用 `X-Notification-Id` 去重。这是"至少一次"语义的固有代价。
- **只限流，不熔断**：一个持续超时的下游会一直占着它那 4 个名额，系统不会主动暂停对它的投递。某个 host 积压很多时，取任务的扫描成本会随积压量增长。
- **隔离按 hostname 区分**：`localhost` 和 `127.0.0.1`、或同一后端的两个域名，会被当作两个 host，各自享有一份并发名额。
- **重放没有持久化的审计记录**：谁在何时重放只记在日志里。
- **查询结果只对 Header 脱敏**：url 的查询串、body 和响应摘录都原样返回给提交方本人。
- **DNS rebinding 的残余窗口**：在"投递前校验"和"httpx 建立连接"之间，有一个毫秒级的窗口。要利用它，攻击者必须既是内网的可信调用方，又能控制目标域名的 DNS。
- **返回 200 但 body 里是业务错误**：这种情况会被当作成功。这是透传边界带来的局限。
- **只支持单 worker 进程**：占用机制本身不排斥多进程，但只验证过单 worker 的情况。
- 尝试记录和去重数据会一直增长，没有清理机制。响应摘录（前 1KB）可能包含敏感信息。

## 设计说明（逐条回答题目问题）

本节按题目"设计说明要求"的三组问题作答。为区分观点来源，凡是候选人的判断，都附上 [docs/ai-review.md](docs/ai-review.md) 中记录的原话和编号。

### 1. 系统边界

**本系统负责解决的问题：**

| 问题 | 怎么解决 |
|---|---|
| 送达责任从什么时候起由本系统承担 | 通知先落盘，再返回 202；没拿到回执时，业务方带同一个幂等 key 重试 |
| 下游失败时怎么办 | 先区分永久失败和临时失败；临时失败按指数退避重试，并遵守 `Retry-After`；在截止时间内持续投递 |
| 重试必然带来的重复 | 承诺至少一次；每次投递都带稳定的 `X-Notification-Id`，供下游去重 |
| 一个下游慢或宕机，会不会拖垮其他下游 | 按目标 host 限制同时投递的数量 |
| 最终失败之后怎么办 | 失败终态的通知可查询、可重放，运行状况通过指标观测 |
| 本服务被当成 SSRF 跳板 | 受理时和投递前都校验目标地址，禁止内网和元数据地址（候选人拍板的"最低限度"防护，S-11） |

**明确不解决的问题，以及原因：**

| 不解决 | 原因 | 谁决定 |
|---|---|---|
| 理解各供应商的报文格式，做模板转换 | 本服务透传完整的 HTTP 请求，不与任何供应商耦合；新接入一个供应商不需要改动本服务 | AI 建议，候选人确认 |
| 凭证托管、动态签名、OAuth 刷新 | 一旦要做，系统就必须理解供应商，边界会整体改变。代价是带时效的签名在重试时会失效 | 候选人："同意第10点，不纳入本次的MVP实现中"（S-10） |
| 投递顺序 | 保序会让一条失败的通知阻塞同组后续所有通知。对业务的影响已逐个场景评估（见 ai-review"不保证顺序对业务的影响"）；需要保序的业务方自己携带版本号 | 候选人："我倾向于本次 MVP 不保证投递顺序, 但可以记录一下对业务产生的影响"（S-16） |
| exactly-once | 下游是否已经处理，我们无法确认，所以做不到；改为透传通知 ID，由下游去重 | AI 建议，候选人确认 |
| 按供应商定制成功判定（比如解析 body 里的业务错误码） | 与透传边界冲突。"200 + 业务错误码"会被当作成功，作为已知局限接受 | AI 建议，候选人确认 |
| 把投递结果回调给业务方 | 题目明确说业务系统不关心返回值 | 题目要求 |
| 业务侧的事务一致性（outbox） | 从"业务库提交成功"到"调用本服务"之间如果出了问题，属于业务方的责任 | AI 建议，候选人确认 |
| 熔断、多实例部署、管理 UI、批量提交 | MVP 的量级用不上；设计上没有堵死这些方向，见下面的演进方向 | AI 建议，候选人确认 |

### 2. 可靠性与失败处理

**投递语义：至少一次**，承诺的完整表述见上文"可靠性承诺"。承诺包含的每一条规则，都在上文表格中对应到了代码机制和测试证据；其中必须满足的前提也一并写明（存储介质不损坏、业务方会重试）。

**外部系统失败、长期不可用时的处理策略。** 下面以"某个下游宕机 30 小时"为例，按时间顺序说明：

1. **失败分类**：连接失败、超时、5xx 属于临时失败，会进入重试。如果返回的是 400 这类永久失败，则立即进入终态，不会白白重试 24 小时。
2. **退避重试**：重试间隔为 10s、20s、40s……逐次翻倍，最长 1 小时，并带有 ±20% 的随机抖动，避免大量通知在同一时刻重试。前约 0.7 小时内尝试 9 次，之后约每小时 1 次，24 小时内一共约 33 次（按默认参数实算）。重试计划存放在库里，worker 重启后不会被重置。
3. **隔离**：这个下游最多同时占用 4 个投递名额（每个 host 的上限），其余下游照常投递。对照测试证明，如果不按 host 限流，其他下游会被堵住。
4. **可观测**：`notifier_backlog_due` 和 `notifier_oldest_due_age_seconds` 会持续上升，`notifier_delivery_attempts_total{outcome="temporary"}` 会快速增长。这三个指标适合配置告警。
5. **终态**：到达截止时间后，通知被标记为 `failed`，失败原因为 `retries_exhausted`；如果业务给的 `expires_at` 先到，原因为 `expired`。到期之后不再发送任何请求。
6. **恢复**：下游恢复后，运维或业务方对 `retries_exhausted` 的通知逐条重放。重放会重新计算重试窗口，并沿用原来的通知 ID，下游可以据此去重。`expired` 的通知不能重放，因为业务给的期限是硬约束。

在这个场景里，当前版本有一个短板：只能逐条重放，不能按 host 或时间范围批量重放。下游长时间宕机后需要补投时，操作成本较高，已列入下面的演进方向。

### 3. 取舍与演进

**AI 方案中被候选人判定为"过度"、因而没有采纳的设计：**

| 编号 | AI 的方案 | 候选人的判断 | 最终做法 |
|---|---|---|---|
| P-1 | 首版把需求拆成 7 个 change：受理、投递、失败分类、地址防护、隔离、运维、文档 | "考虑到当前为MVP的实现，change划分是否可以精简一点" | 精简为 3 个 change，按"能独立验证的承诺"划分；文档收尾不走 OpenSpec |
| B-6 | 业务过期之前，把重试提前，"最后再试一次"；同时把最后一次重试压缩到截止时间那一刻 | "我选简单方案，不需要为了‘最后再试一次’增加特殊规则……作为MVP, 规则更为简单友好" | 截止时间 = min(重试期限, `expires_at`)。每次发送前检查，到期就不再发送；下一次重试已经来不及时，等到截止时间直接标记失败。顺带一提，被否决的这个方案本身还藏着一个 bug：压缩到截止时间的那一次重试，取出时已经晚于截止时间，永远发不出去 |

**AI 输出中被识别出缺陷并修正的地方：**

| 编号 | 问题 | 由谁发现 | 处理 |
|---|---|---|---|
| B-5 | design 初稿规定"失败时如果 `expires_at` 早于下一次重试时间，就立即判为已过期"。这会在通知还没过期时就给它打上"已过期"的标签，而且与 spec 矛盾 | 候选人举反例："假如现在 10:00，通知 10:05 过期，但退避算出下一次是 10:10。当前规则会在 10:00 就把尚未过期的任务标记为已过期？" | 否决，改用上面 B-6 那一行中候选人给出的规则 |
| P-2 | change-plan skill 默认把拆分计划当作个人笔记，建议用 git 忽略 | 候选人："本次是 homework，可以提交给评审看到拆分过程" | 计划文件提交进仓库 |
| — | 实现阶段，AI 在自查中发现 API 和 worker 同时冷启动时会触发两个竞态：重复建表，以及切换 WAL 时报 locked | AI 在自查和复现中发现 | 改为在写锁内执行迁移，切换 WAL 时增加重试；补了回归测试，并用变异测试确认它能抓住这个问题（详见 change B 的 design） |

其余 A、B、C 三个阶段的设计决策（存储、框架、幂等规则、进程模型、隔离方式、重放语义、脱敏规则等），候选人都选择了 AI 的推荐项，没有另外给出理由，记录见 ai-review 的 A-1 到 C-4。

**演进方向**：第一版有意留下了以下扩展点：
- 所有 SQL 收拢在 `store.py`；
- 占用和 fencing 机制本身就支持多个 worker 并行；
- 限流状态存放在库里。

| 触发条件 | 演进 |
|---|---|
| 写入量逼近 SQLite 单写者的上限，或需要高可用 | 换成 PostgreSQL。用 `FOR UPDATE SKIP LOCKED` 取任务，部署多个 worker 实例；API 无状态，可以水平扩展。只需改写 `store.py` |
| 吞吐继续增长，需要削峰 | 数据库只保存状态和幂等信息，投递通道改走消息队列（状态库 + outbox → MQ），worker 改为消费者 |
| 下游数量变多，长期故障频繁 | 在按 host 限流的基础上增加熔断和半开探测，并允许按供应商分别配置并发、超时和重试策略 |
| 有供应商要求动态签名或 OAuth | 引入供应商注册表、凭证托管和签名插件。这意味着系统边界从"透传"变为"理解供应商"，属于一次有意识的边界调整 |
| 业务需要保序 | 同一业务 key 的通知串行投递，同一时刻只允许一条在途 |
| 运维成本上升 | 支持按 host 或时间范围批量重放；增加重放审计表、列表和搜索接口；把告警规则模板化 |
| 安全要求提高 | 严格绑定已校验的 IP，消除 DNS rebinding 窗口；敏感字段加密存储；目标地址改为白名单登记制 |
| 数据持续增长 | 按时间归档或清理尝试记录和去重数据，并缩短去重窗口；把 DNS 解析的任务队列改为有界 |

## 本地运行

需要 Python 3.12 及以上版本。

```bash
python3 -m venv .venv
.venv/bin/pip install -e '.[test]'

# 调用方 token 配置：{token: 调用方名称}
echo '{"dev-token": "demo-service"}' > tokens.json

export NOTIFIER_DB_PATH=./notifier.db
export NOTIFIER_TOKENS_FILE=./tokens.json

# 终端 1：受理 API
.venv/bin/uvicorn notifier.app:create_app --factory --port 8000

# 终端 2：投递 worker（只需要 NOTIFIER_DB_PATH）
.venv/bin/notifier-worker
```

worker 的可选配置都是环境变量，下面是默认值：

| 变量 | 默认值 | 说明 |
|---|---|---|
| `NOTIFIER_WORKER_CONCURRENCY` | 16 | 同时投递的数量 |
| `NOTIFIER_WORKER_PER_HOST_LIMIT` | 4 | 同一个目标 host 同时投递的上限，不能超过并发数 |
| `NOTIFIER_WORKER_POLL_INTERVAL_S` | 1 | 没有到期任务时的轮询间隔 |
| `NOTIFIER_WORKER_LEASE_S` | 60 | 占用期限，必须大于请求超时 |
| `NOTIFIER_WORKER_REQUEST_TIMEOUT_S` | 10 | 单次请求的超时时间 |
| `NOTIFIER_WORKER_BACKOFF_BASE_S` / `_CAP_S` | 10 / 3600 | 退避的首次间隔和单次间隔上限 |
| `NOTIFIER_WORKER_MAX_RETRY_DURATION_S` | 86400 | 从受理开始算的最大重试时长 |

提交一条通知：

```bash
curl -i -X POST http://127.0.0.1:8000/notifications \
  -H 'Authorization: Bearer dev-token' \
  -H 'Content-Type: application/json' \
  -d '{
        "url": "https://httpbin.org/post",
        "method": "POST",
        "headers": {"Content-Type": "application/json"},
        "body": "{\"event\": \"registered\"}",
        "idempotency_key": "evt-001"
      }'
```

查询投递结果（把 `<id>` 换成上面返回的 id）：

```bash
curl -s http://127.0.0.1:8000/notifications/<id> -H 'Authorization: Bearer dev-token'
```

失败与重放的演示：httpbin 的 `/status/400` 一定返回 400，所以这条通知会被判为永久失败。

```bash
curl -s -X POST http://127.0.0.1:8000/notifications \
  -H 'Authorization: Bearer dev-token' -H 'Content-Type: application/json' \
  -d '{"url": "https://httpbin.org/status/400", "method": "POST", "idempotency_key": "evt-002"}'
# 查询：status=failed，failure_reason=permanent_error
curl -s http://127.0.0.1:8000/notifications/<id> -H 'Authorization: Bearer dev-token'
# 重放：返回 202。下游仍然返回 400，所以重放后会再次失败，尝试序号接着往下编
curl -s -X POST http://127.0.0.1:8000/notifications/<id>/replay -H 'Authorization: Bearer dev-token'
# 对已送达的通知重放：返回 409
curl -s -o /dev/null -w '%{http_code}\n' -X POST http://127.0.0.1:8000/notifications/<已送达的 id>/replay -H 'Authorization: Bearer dev-token'
```

指标（不需要认证）：

```bash
curl -s http://127.0.0.1:8000/metrics
```

运行测试：

```bash
.venv/bin/pytest
```

## 提交接口 `POST /notifications`

| 字段 | 必填 | 说明 |
|---|---|---|
| `url` | 是 | 只支持 http 或 https；不能指向内网、回环、链路本地或元数据地址 |
| `method` | 是 | `POST` / `PUT` / `PATCH` / `DELETE` / `GET` |
| `headers` | 否 | 字符串到字符串的映射；不能包含 `Host`、`Content-Length`、`Transfer-Encoding`、`Connection` |
| `body` | 否 | 文本格式，UTF-8 编码后不超过 1MB；JSON 需要先序列化成字符串 |
| `expires_at` | 否 | 带时区的 ISO 8601 时间；过期后不再投递 |
| `idempotency_key` | 是 | 在同一调用方内唯一；没收到回执时，用同一个 key 重试 |

| 响应码 | 含义 |
|---|---|
| `202 {"id"}` | 已受理并持久化，从这时起送达由本服务负责 |
| `200 {"id"}` | 重复提交（同一个 key、同样的内容），返回首次受理时的通知 ID |
| `401` | token 缺失或无效 |
| `409` | 这个 key 已经被用在内容不同的请求上 |
| `422` | 字段校验失败，或目标地址被禁止 |
| `503` | 暂时无法持久化，请用同一个 key 重试 |

## 运维接口

| 接口 | 认证 | 说明 |
|---|---|---|
| `GET /notifications/{id}` | 提交方 token | 返回状态、失败原因、下一次重试时间、原始请求（Header 按白名单脱敏）和每次尝试的记录。他人的通知或不存在的通知都返回 404 |
| `POST /notifications/{id}/replay` | 提交方 token | 把失败终态的通知改回待投递：沿用同一个通知 ID，重试窗口从现在重新计算。原因为已过期、已超过 `expires_at`、已送达，或还没进入终态时，返回 409 |
| `GET /metrics` | 无 | Prometheus 文本格式，包括各状态数量、各失败原因数量、积压量（`notifier_backlog_due`）、最老一条待投递通知的等待秒数、各结果分类的尝试次数 |
