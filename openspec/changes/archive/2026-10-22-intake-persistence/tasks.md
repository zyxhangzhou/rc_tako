# Tasks

每组任务都附带该组需要的测试。任务后的方括号注明它对应 spec 中的哪条需求或场景。

## 1. 项目骨架

- [x] 1.1 新建 `pyproject.toml`，锁定 fastapi、uvicorn、pydantic 的版本，测试依赖为 pytest、httpx。包放在 `src/notifier/`，测试放在 `tests/`。用 `python3 -m venv .venv && .venv/bin/pip install -e '.[test]'` 安装成功，并且 `.venv/bin/pytest` 能跑起来（此时 0 个测试）即为完成。
- [x] 1.2 新建 `.gitignore`，忽略 `.venv/`、`__pycache__/`、`*.db*`、本地配置文件。`git status` 中不出现这些文件即为完成。
- [x] 1.3 新建配置模块：从环境变量读取数据库路径和调用方 token 文件路径，token 文件为 JSON 格式，内容是 token 到调用方名称的映射。缺少必需的配置时，启动报错，错误信息要清楚。用单元测试覆盖正常读取和缺少配置两种情况。

## 2. 存储层（notification-store）

- [x] 2.1 实现存储模块：
  - 每个连接都设置 `journal_mode=WAL`、`synchronous=FULL` 和 `busy_timeout`。
  - 启动时自动建表，并记录 schema 版本号。
  - 建立 `notifications` 表（字段见 design D5），以及唯一约束 `UNIQUE(caller, idempotency_key)`。

  测试要验证：首次启动时会自动初始化，PRAGMA 的取值正确。[需求"重启后数据不丢失" / 场景"首次启动"]
- [x] 2.2 实现"插入，或返回已有记录"：
  - INSERT 触发唯一约束冲突时，读出已有的那一行，返回它的 ID 和 `request_hash`，供上层判断是否冲突。
  - 新记录的状态为 `pending`。

  测试要验证：内容原样保存、初始状态正确、重复插入不会产生第二行。[需求"完整保存投递所需内容"、"新受理通知的初始状态"]
- [x] 2.3 实现按 ID 读取通知（供测试和 B 使用），并验证 headers 和 body 能原样往返。

## 3. 校验与地址防护（notification-intake）

- [x] 3.1 用 pydantic 定义提交请求模型：
  - url：scheme 只允许 http/https，并且必须带 host。
  - method：限定在白名单内。
  - headers：不允许出现禁止的 Header。
  - body：UTF-8 编码后不超过 1MB。
  - expires_at：必须带时区，并且晚于当前时间。
  - idempotency_key：必填。

  每个非法场景都要有一个参数化测试，断言返回 422。[需求"请求字段校验"、"幂等提交" / 场景"缺少幂等 key"]
- [x] 3.2 实现目标地址校验：
  - 用 `ipaddress` 判定地址类别，IPv4-mapped 的 IPv6 地址先还原成 IPv4。
  - 域名用 `getaddrinfo` 解析，任意一个解析结果命中禁止类别就拒绝。
  - 解析失败时放行。

  测试时 mock 掉 DNS 解析，覆盖以下场景：回环、元数据地址、`[::1]`、`[::ffff:10.0.0.1]`、域名解析到内网、解析失败、只解析到公网。[需求"拒绝内网与元数据目标地址"，涉及的全部场景]
- [x] 3.3 计算请求指纹：把 `url, method, headers, body, expires_at` 规范化成 JSON（键排序）后取 SHA-256。测试要验证：headers 的键顺序不同，指纹相同；body 不同，指纹不同。

## 4. 提交接口

- [x] 4.1 实现 `Authorization: Bearer` 认证依赖，用 TestClient 验证：缺少 token、token 无效时都返回 401，并且不会写入任何数据。认证通过后，记录的提交方名称正确。[需求"调用方认证"，涉及的全部场景]
- [x] 4.2 实现 `POST /notifications`，用同步端点，执行顺序为：认证 → 校验 → 地址防护 → 计算指纹 → 插入或返回已有记录：
  - 新记录返回 202 和通知 ID。
  - 同一 key、相同指纹返回 200 和原来的 ID。
  - 同一 key、不同指纹返回 409。

  用 TestClient 覆盖这几种情况：正常受理、相同内容重复提交、同一 key 内容不同、不同调用方使用同一个 key。[需求"受理成功即已持久化" / 场景"正常受理"；需求"幂等提交"]
- [x] 4.3 存储写入失败或锁超时时返回 503。测试时通过注入故障，让存储层抛出 `sqlite3.OperationalError`，断言返回 503，并且没有残留记录。[场景"持久化失败"]
- [x] 4.4 并发幂等测试：多个线程用同一个 key 和相同内容同时提交，断言数据库里只有 1 行，并且所有成功响应返回的都是同一个 ID。[场景"并发重复提交"]

## 5. 端到端验证（跨组的系统行为）

- [x] 5.1 SIGKILL 重启测试：
  1. 用子进程启动 uvicorn，指向一个临时数据库。
  2. 提交 N 条通知，每条都拿到 202。
  3. 用 SIGKILL 杀掉子进程，再重新启动。
  4. 按 ID 逐条检查：每条通知都存在，状态是 `pending`，内容与提交时一致。

  对应场景"受理后进程立即崩溃"和"强制终止后重启"。这个测试是规则 1 的测试证据。
- [x] 5.2 在 README 中写明本地运行方式：安装、配置 token 文件、启动服务，以及一个 curl 提交示例。按文档逐条执行，能得到 202 即为完成。
- [x] 5.3 运行 `openspec validate intake-persistence --strict` 和完整的测试套件，两者都要通过，并把实际的输出贴进完成报告。

## Workflow follow-up

- 完成后在 `mydoc/plans/notification-delivery/` 中更新 A 的状态，并在 Status log 中追加一行记录。
- 候选人验收后，执行 archive，并在 plan 中把 A 标记为 `archived`。
