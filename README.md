# API 通知投递服务

业务系统把要发送的外部 HTTP 请求（URL / Method / Header / Body）提交给本服务，由本服务负责可靠地投递到目标地址；业务系统不需要等待结果。题目见 [docs/assignment.md](docs/assignment.md)。

> 本服务目前处于开发中。已完成：受理与持久化（change A）、投递引擎（change B）。未完成：隔离与运维（change C）。拆分计划见 [mydoc/plans/notification-delivery/](mydoc/plans/notification-delivery/)，AI 建议的处理记录见 [docs/ai-review.md](docs/ai-review.md)。

## 问题理解

题目要的本质上是一个"可靠的出站 HTTP 投递"服务。业务系统只负责说清楚"发什么、发到哪"，然后就可以离开。真正的难点在于下游不可控：它可能很慢，可能长时间宕机，也可能重复处理同一条通知。

系统边界：本服务**透传**业务方提交的完整请求，不理解各个供应商的报文格式。这样做的代价是，凭证散落在各个业务方手里，带时效的签名在重试时会失效，"返回 200 但 body 里是业务错误"的情况也识别不出来。这些都作为已知局限接受。完整的边界清单见 [plan v1](mydoc/plans/notification-delivery/notification-delivery-v1.md) 的 Out of scope 一节。

## 可靠性承诺

> 通知一旦受理，本服务会持续投递，直到出现以下三种结果之一：确认成功（2xx）；判定为永久失败（3xx，或 408、429 以外的 4xx）；到达截止时间。截止时间取"受理后 24 小时（可配置）"和调用方给的 `expires_at` 中较早的那个。后两种结果进入失败终态，并记录原因。人工重放由 change C 提供。

投递语义是**至少一次**：每次投递都带 `X-Notification-Id`（同一条通知的所有尝试都相同）和 `X-Notification-Attempt`（从 1 开始），下游可以据此去重。本服务不承诺 exactly-once，也不保证顺序。

| 规则 | 机制 | 证据 | 状态 |
|---|---|---|---|
| 1. 先落盘，再返回"已受理" | SQLite 开 WAL、`synchronous=FULL`，事务 COMMIT 后才返回 202；写入失败返回 503 | `tests/test_crash_recovery.py`：SIGKILL 后重启，数据不丢；`tests/test_api.py`：注入故障和锁超时时都返回 503 | ✅ A |
| 2. 收到 2xx 后才标记为已送达 | `deliver_one` 只在分类结果为成功时调用 `mark_delivered` | `tests/test_delivery.py`、`tests/test_worker.py`：先 500 后 200 的场景 | ✅ B |
| 3. 用带期限的占用取出通知 | 一条 `UPDATE … RETURNING` 同时取出到期的通知和占用已过期的通知；写回时凭 `lease_token` 做 fencing，旧占用的结果会被丢弃 | `tests/test_store_delivery.py`：占用过期、fencing；`tests/test_worker_process.py`：worker 发出请求后被 SIGKILL，重启后重投。把"占用过期可重取"这条逻辑去掉后，这个测试会失败 | ✅ B |
| 4. 重试计划持久化 | 下一次重试时间存在 `next_attempt_at`（整数毫秒）里，不使用内存定时器 | `tests/test_worker.py::test_restart_does_not_retry_early` | ✅ B |
| 5. 结果不明时按失败处理 | 超时、连接错误、请求发出后崩溃，都会进入重试 | `tests/test_delivery.py::test_processed_but_timed_out_is_redelivered_with_same_id` | ✅ B |
| 6. 每次投递带同一个通知 ID | 系统写入的 Header 会覆盖提交时的同名项 | 同上，外加 SIGKILL 测试：两次请求的 ID 相同，attempt 分别为 1 和 2 | ✅ B |

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

## 关键决策（截至 change B）

| 决策 | 选择 | 主要替代方案与不选的理由 |
|---|---|---|
| 存储 | SQLite（标准库） | PostgreSQL：多实例友好，但要额外部署，MVP 的量级用不上，作为演进方向。消息队列：延迟重试不好做，而且查询和幂等仍然需要一个数据库 |
| Web 框架 | FastAPI + uvicorn | 标准库 `http.server`：零依赖，但校验、路由、错误处理都要手写 |
| 幂等 | key 必填；同一 key 内容不同时返回 409 | key 可选：重试时会产生重复。静默返回旧 ID：会掩盖调用方的 bug |
| 地址防护 | 拒绝内网、回环、链路本地和元数据地址。受理时 DNS 解析失败或超过 2s 照常放行；每次投递前用严格模式重新校验 | 完整的目标白名单：每加一个目标都要登记，MVP 不做 |
| worker 进程模型 | 独立进程，和 API 共用同一个 SQLite 文件 | API 进程内的后台线程：API 崩溃时投递会一起停，uvicorn 多进程部署时还会起多份投递循环 |
| 并发模型 | 固定大小线程池（默认 16）+ 同步 httpx | asyncio：并发上限更高，但会同步、异步混用，测试也更复杂 |
| DNS rebinding | 投递前校验，接受残余窗口 | 严格绑定已校验的 IP：要自己处理 SNI 和证书，成本高，作为演进项 |
| 截止时间 | min(重试期限, `expires_at`)，到期就不再发送，不做"最后再试一次" | AI 最初方案会在通知还没过期时就把它标成已过期，被候选人发现后否决；随后提出的"提前补一次尝试"也被否决，理由是规则更简单 |

每项决策的完整理由见 `openspec/changes/archive/*/design.md`，AI 建议的采纳和否决记录见 [docs/ai-review.md](docs/ai-review.md)。

## 已知局限

- **重复送达**：超时，或请求发出后 worker 崩溃，都会触发重投。下游需要用 `X-Notification-Id` 去重。这是"至少一次"语义的固有代价。
- **慢下游会占满线程池**：一个持续超时的下游，最多能占满全部 16 个线程（每次占 10s）。按目标隔离由 change C 解决。
- **DNS rebinding 的残余窗口**：在"投递前校验"和"httpx 建立连接"之间，有一个毫秒级的窗口。要利用它，攻击者必须既是内网的可信调用方，又能控制目标域名的 DNS。
- **返回 200 但 body 里是业务错误**：这种情况会被当作成功。这是透传边界带来的局限。
- **只支持单 worker 进程**：占用机制本身不排斥多进程，但只验证过单 worker 的情况。
- 尝试记录和去重数据会一直增长，没有清理机制。响应摘录（前 1KB）可能包含敏感信息。

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

查看投递结果。查询接口在 change C 才提供，目前先直接查库：

```bash
sqlite3 notifier.db "SELECT status, attempt_count, failure_reason FROM notifications"
sqlite3 notifier.db "SELECT attempt_no, outcome, status_code, error FROM delivery_attempts"
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
