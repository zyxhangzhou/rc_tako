# API 通知投递服务

业务系统把要发送的外部 HTTP 请求（URL / Method / Header / Body）提交给本服务，由本服务负责可靠地投递到目标地址；业务系统不需要等待结果。题目见 [docs/assignment.md](docs/assignment.md)。

> 本服务目前处于开发中。已完成：受理与持久化（change A）。未完成：投递引擎（B）、隔离与运维（C）。拆分计划见 [mydoc/plans/notification-delivery/](mydoc/plans/notification-delivery/)，AI 建议的处理记录见 [docs/ai-review.md](docs/ai-review.md)。

## 问题理解

题目要的本质上是一个"可靠的出站 HTTP 投递"服务。业务系统只负责说清楚"发什么、发到哪"，然后就可以离开。真正的难点在于下游不可控：它可能很慢，可能长时间宕机，也可能重复处理同一条通知。

系统边界：本服务**透传**业务方提交的完整请求，不理解各个供应商的报文格式。这样做的代价是，凭证散落在各个业务方手里，带时效的签名在重试时会失效，"返回 200 但 body 里是业务错误"的情况也识别不出来。这些都作为已知局限接受。完整的边界清单见 [plan v1](mydoc/plans/notification-delivery/notification-delivery-v1.md) 的 Out of scope 一节。

## 可靠性承诺

> 通知一旦受理，本服务会持续投递，直到出现以下三种结果之一：确认成功（2xx）；判定为永久失败（4xx，但 408 和 429 除外）；超出重试期限（约 24 小时，可配置）或已过期。后两种进入终态，有记录可查，并且可以人工重放。

投递语义是**至少一次**：每次重试都带同一个通知 ID，由下游据此去重。本服务不承诺 exactly-once，也不保证顺序。

| 规则 | 机制 | 证据 | 状态 |
|---|---|---|---|
| 1. 先落盘，再返回"已受理" | SQLite 开 WAL，`synchronous=FULL`，事务 COMMIT 之后才返回 202；写入失败返回 503 | `tests/test_crash_recovery.py`：SIGKILL 后重启，数据不丢；`tests/test_api.py`：注入故障和锁超时时返回 503 | ✅ change A |
| 2–6. 收到 2xx 才标记送达；带期限的占用；持久化重试计划；结果不明按失败处理；同一个通知 ID | — | — | change B |

前提：存储介质本身不损坏（MVP 是单机）；业务方在没收到受理回执时，会用同一个幂等 key 重试。SIGKILL 测试只能证明进程崩溃时不丢数据；掉电时不丢数据依赖 fsync，这一点没有经过实测。

## 关键决策（截至 change A）

| 决策 | 选择 | 主要替代方案与不选的理由 |
|---|---|---|
| 存储 | SQLite（标准库） | PostgreSQL：多实例友好，但要额外部署，MVP 的量级用不上，作为演进方向。消息队列：延迟重试不好做，而且查询和幂等仍然需要一个数据库 |
| Web 框架 | FastAPI + uvicorn | 标准库 `http.server`：零依赖，但校验、路由、错误处理都要手写 |
| 幂等 | key 必填；同一 key 内容不同时返回 409 | key 可选：重试时会产生重复。静默返回旧 ID：会掩盖调用方的 bug |
| 地址防护 | 拒绝内网、回环、链路本地和元数据地址；受理时 DNS 解析失败照常放行，投递时再校验 | 完整的目标白名单：每加一个目标都要登记，MVP 不做 |

每项决策的完整理由见 `openspec/changes/archive/*/design.md`，AI 建议的采纳和否决记录见 [docs/ai-review.md](docs/ai-review.md)。

## 本地运行

需要 Python 3.12 及以上版本。

```bash
python3 -m venv .venv
.venv/bin/pip install -e '.[test]'

# 调用方 token 配置：{token: 调用方名称}
echo '{"dev-token": "demo-service"}' > tokens.json

export NOTIFIER_DB_PATH=./notifier.db
export NOTIFIER_TOKENS_FILE=./tokens.json
.venv/bin/uvicorn notifier.app:create_app --factory --port 8000
```

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
