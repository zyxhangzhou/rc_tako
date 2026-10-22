# Design

## Context

- 仓库目前没有代码，技术栈为 Python 3.12。动机见 proposal.md 的 Why 一节，需求见 specs/。
- 本服务的承诺来自 change-plan v1（mydoc/plans/notification-delivery/）的 Background 一节。本 change 只负责规则 1：先落盘，再返回"已受理"。
- 本 change 选定的存储，会被 B（投递引擎）直接用来实现带期限的占用和持久化的重试时间。

## Goals / Non-Goals

**Goals:**
- "受理成功"必须对应一次已经持久化的事务提交，由代码机制和杀进程测试共同证明。
- 幂等去重在并发提交下也成立。
- 存储访问收拢在一个模块里，B 扩展状态、将来迁移到 PG，都只改这一层。

**Non-Goals:**
- 投递、占用、重试时间字段，以及除"待投递"以外的状态，都留给 B。
- 多实例部署和水平扩展。
- 数据迁移框架：MVP 只用"建表 + 按版本号追加 DDL"的最小机制。

## Decisions

### D1 存储：SQLite（候选人拍板）

- **选择**：SQLite 文件库，使用 Python 标准库 `sqlite3`。每个连接都设置 `journal_mode=WAL`、`synchronous=FULL` 和 `busy_timeout`。
- **理由**：
  - `synchronous=FULL` 下，事务提交返回之前日志已经 fsync 到磁盘。所以"COMMIT 返回之后才回 202"能直接对应到代码机制上。
  - 唯一约束可以直接实现幂等。
  - B 需要的"条件 UPDATE 抢占"和"按 next_attempt_at 排序查询"，SQLite 都能自然支持。
  - 零运维，评审 clone 之后就能跑。
- **替代方案**：
  - PostgreSQL：有 `SKIP LOCKED`，适合多实例。代价是需要 docker-compose，运行门槛更高，而 MVP 的量级用不上。这是演进方向。
  - 消息队列加数据库：延迟重试不好做，还需要两套存储并处理它们之间的一致性。AI 也不推荐。
- **接受的局限**：
  - 只能单机、单写者，无法多实例水平扩展。
  - 磁盘损坏会丢数据（见 Risks）。
  - 迁移到 PG 时，部分 SQL 需要改写，比如 upsert 和时间函数。

### D2 Web 框架：FastAPI + uvicorn（候选人拍板）

- **选择**：FastAPI，用 pydantic 声明请求模型，用 uvicorn 运行。端点写成同步 `def`，由 FastAPI 放进线程池执行，这样阻塞的 `sqlite3` 调用和 DNS 解析不会卡住事件循环。
- **理由**：校验是声明式的；测试有 TestClient；B 的 HTTP 客户端可以和 httpx 保持一致。
- **替代方案**：标准库 `http.server`，零依赖，但路由、校验和错误响应都要手写。
- **维护成本**：两个主流依赖，在 `pyproject.toml` 里锁定版本。

### D3 幂等规则（候选人拍板）

- `idempotency_key` 必填，作用域是"调用方 + key"，对应唯一约束 `UNIQUE(caller, idempotency_key)`。
- 请求指纹：对 `url, method, headers, body, expires_at` 做规范化的 JSON 序列化（键排序），再取 SHA-256，存在 `request_hash` 字段里。
- 处理流程：
  1. 先尝试 INSERT。
  2. 如果触发唯一约束冲突，就读出已有的那行，比较 `request_hash`：
     - 相同：返回 `200` 和已有的通知 ID。
     - 不同：返回 `409`。
  - 并发提交同一个 key 时，正确性由数据库的唯一约束保证，不依赖应用层先查后写。
- **理由**：
  - key 必填：业务方会在没拿到回执时重试，这是"至少一次"的前提。如果 key 可选，这个前提就会被悄悄打破。
  - 内容不同返回 409：这种情况说明调用方有 bug，静默吞掉会掩盖问题。
- **替代方案**：
  - key 可选：接入门槛更低，但会产生重复通知。
  - 内容不同时返回旧 ID：对调用方更宽松，但会掩盖 bug。
- **接受的局限**：去重窗口是永久的，没有清理机制，数据只增不减。

### D4 受理侧地址防护（候选人拍板 DNS 失败的处理）

- 用 `urllib.parse` 解析 URL，scheme 只允许 `http` 和 `https`，并且必须带 host。
- host 是 IP 字面量时，直接判定。host 是域名时，用 `getaddrinfo` 解析，**任意一个**解析结果命中拒绝规则，就拒绝整个请求。
- 拒绝规则基于 `ipaddress` 模块：`is_private`、`is_loopback`、`is_link_local`（覆盖 `169.254.169.254`）、`is_multicast`、`is_reserved`、`is_unspecified`。IPv4-mapped 的 IPv6 地址会先还原成 IPv4 再判定。
- **DNS 解析失败时照常受理**，由投递侧（B）再次校验。理由：临时的 DNS 故障不应该变成业务失败。IP 字面量的内网地址不受影响，仍然立即拒绝。
- **替代方案**：解析失败时返回 503，让调用方重试。受理时把关更严，但临时故障会被推回给业务方。
- **不保证**：受理时的校验挡不住 DNS rebinding，也就是"受理时解析到公网，投递时解析到内网"。这要靠 B 在投递时校验，是否需要严格绑定"校验 IP"和"连接 IP"，在 B 的 design 里评估。

### D5 其他实现细节（AI 自主决定）

- **认证**：使用 `Authorization: Bearer <token>`。token 和调用方名称的对应关系从配置文件读取，路径由环境变量指定。token 缺失或无效时返回 `401`。
- **校验**：
  - method 白名单：`POST`、`PUT`、`PATCH`、`DELETE`、`GET`。
  - body 只接受文本（字符串），UTF-8 编码后不超过 1MB。
  - headers 是字符串到字符串的映射，并拒绝 `Host`、`Content-Length`、`Transfer-Encoding`、`Connection` 这几个 hop-by-hop 或由客户端库负责的 Header。
  - `expires_at` 可选，必须带时区，并且必须晚于当前时间。
  - 校验失败返回 `422`。
- **响应**：新受理返回 `202 {id}`；重复提交返回 `200 {id}`；持久化失败（如磁盘满或锁超时）返回 `503`，不会返回成功。
- **ID**：用 UUID4 字符串，在应用侧生成。
- **数据表**：`notifications` 表的字段是 `id`、`caller`、`idempotency_key`、`request_hash`、`url`、`method`、`headers_json`、`body`、`expires_at`、`status`（本 change 中恒为 `pending`）、`created_at`。B 会通过版本化的 DDL 追加投递相关的字段。
- **连接**：每个请求单独打开一个连接，用完关闭。这样避免了跨线程共享连接；SQLite 打开连接的开销对 MVP 的量级可以忽略。

## Risks / Trade-offs

- [测试证据的覆盖范围有限] SIGKILL 测试只能证明"进程崩溃不丢数据"：进程被杀后，已写入的数据还在操作系统页缓存里，所以即使 `synchronous=OFF`，这个测试也会通过。"掉电不丢数据"依赖的是 `synchronous=FULL` 下的 fsync，目前的证据只有 PRAGMA 取值的单元测试。真正的掉电测试需要虚拟机或故障注入文件系统，MVP 不做。
- [SQLite 文件或磁盘损坏，导致已受理的数据丢失] → 作为前提写明（单机 MVP）。演进时换成 PG 并配置主备。
- [单写者锁争用，高并发下受理会变慢或超时] → 设置 `busy_timeout`，超时返回 503，调用方会带着幂等 key 重试，不会产生重复。量级假设是峰值几百 QPS，这在 SQLite 单写者的能力范围内。
- [去重数据永久保留，表持续增长] → MVP 接受这一点。演进时按 `created_at` 归档或清理，同时缩短去重窗口。
- [DNS rebinding 绕过受理侧校验] → 由 B 在投递时校验兜底。
- [调用方 token 明文存放在配置文件里] → MVP 接受，部署时由文件权限保护。演进时接入密钥管理。
- [不限制调用方的提交速率] → 内网调用方是受信任的，MVP 不做。

## 本 change 不保证的事项

- 不保证投递，本 change 只负责受理。
- 不防御存储介质本身的损坏。
- 不防御 DNS rebinding。
- 不检测"同一个业务事件、却换了一个幂等 key"导致的重复，key 由调用方负责生成。

## Migration Plan

这是新项目，不涉及迁移。启动时自动建表，并记录 schema 版本号，供 B 追加 DDL 时使用。
