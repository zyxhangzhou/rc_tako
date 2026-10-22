# Design

## Context

- 本 change 建立在 change A 之上（已归档：`openspec/changes/archive/2026-10-22-intake-persistence/`）。
  - 通知以 `pending` 状态存在 SQLite（WAL，`synchronous=FULL`）中。
  - 所有 SQL 都集中在 `store.py`。
  - schema 版本记在 `PRAGMA user_version`，当前为 1。
- 动机见 proposal.md 的 Why 一节，需求见 specs/。
- 本 change 要兑现的规则（编号沿用 change-plan v1 的 Background）：
  - 2：收到 2xx 才标记已送达。
  - 3：用带期限的占用取出通知。
  - 4：重试计划持久化。
  - 5：结果不明时按失败处理。
  - 6：每次投递都带同一个通知 ID。

## Goals / Non-Goals

**Goals:**
- 规则 2–6 都要有对应的代码机制，并有测试证明。规则 3 和规则 5 的证据，要求是"在请求发出之后杀掉 worker，重启后会重新投递"。
- 失败分类和终态判定可以在单元测试里确定性地验证。为此时钟和 DNS 解析都通过注入传入。
- worker 取任务的逻辑集中在一个存储函数里，给 C 留出"按目标分组"的扩展点。

**Non-Goals:**
- 按目标隔离并发、查询接口、重放、指标，这些都留给 C。
- 熔断和主动探活。
- 多个 worker 进程同时运行的正确性。占用机制本身不排斥多进程，但 MVP 只验证单 worker 进程。
- 按供应商定制成功判定，比如解析 body 里的业务错误码。

## Decisions

### D1 worker 作为独立进程运行（候选人拍板）

- **选择**：新增入口 `python -m notifier.worker`，和 API 是两个进程，共用同一个 SQLite 文件。
- **理由**：
  - 故障隔离：worker 崩溃或卡住，不影响受理；反过来也一样。
  - 可以只对 worker 做 SIGKILL 测试。
  - 两边可以各自重启。
  - WAL 模式允许一个进程写的同时，另一个进程并发读；写和写之间由 `busy_timeout` 串行化。
- **替代方案**：在 API 进程里起后台线程。好处是只需一条命令；坏处是 API 崩溃时投递也一起停，而且 uvicorn 开多个 worker 时会各起一份投递循环。
- **接受的局限**：运行时需要两条命令，README 里写明。

### D2 并发模型：固定大小线程池 + 同步 httpx（候选人拍板）

- **选择**：
  - 主线程循环做三件事：按线程池里的空闲名额批量占用通知；把占到的通知提交给 `ThreadPoolExecutor`（大小默认 16，可配）；如果没有到期的通知，就休眠 poll 间隔（默认 1s）。
  - 每个投递线程各自打开数据库连接，共用同一个 `httpx.Client`，它是线程安全的。
- **理由**：和 A 的同步 sqlite3 写法保持一致，代码和测试都更简单。
- **替代方案**：asyncio 加 `httpx.AsyncClient`。并发上限更高，但数据库调用要包一层 `to_thread`，同步和异步混在一起。
- **接受的局限**：慢下游会占住线程，最坏情况是 16 个线程全被同一个慢下游占满。按目标隔离由 C 解决；在 C 完成之前，这是已知风险。

### D3 状态机与 schema v2

- **状态**：
  - `pending`：待投递，包括等待下一次重试的通知。
  - `delivering`：已被占用。
  - `delivered`：已送达，终态。
  - `failed`：失败，终态。具体原因记在 `failure_reason` 里，取值为 `permanent_error`、`retries_exhausted`、`expired`、`blocked_target`。
- **v2 迁移**：
  - 在 `notifications` 表追加以下字段：`next_attempt_at`、`lease_token`、`lease_until`、`attempt_count`、`failure_reason`、`finished_at`。
  - 已有的行把 `next_attempt_at` 回填为 0，即立即到期。新受理的通知则写入受理时间。
  - 新建索引 `(status, next_attempt_at)`。
  - 新建 `delivery_attempts` 表，字段为：`notification_id`、`attempt_no`、`started_at`、`duration_ms`、`outcome`、`status_code`、`error`、`response_excerpt`。
- **调度用的时间字段一律存整数毫秒**（UTC epoch）。原因：A 里时间存的是 ISO 文本，而 `isoformat()` 在微秒为 0 时会省略小数部分，导致同一秒内的两个时间按字符串比较时排错序。已有的展示类字段（`created_at`、`expires_at`）保持文本格式不变。

### D4 带期限的占用与 fencing（规则 3）

- **占用**：用一条 `UPDATE … WHERE id IN (SELECT … LIMIT k) RETURNING *` 完成，条件是以下两者之一：
  - `status='pending' AND next_attempt_at <= now`；
  - `status='delivering' AND lease_until <= now`，即占用已经过期。

  占用时写入一个新的随机 `lease_token`，把 `lease_until` 设为 now 加占用期限（默认 60s，远大于 10s 的请求超时），并把 `attempt_count` 加 1。
- **写回结果**：用 `UPDATE … WHERE id=? AND lease_token=?`。如果影响行数为 0，说明自己的占用已经过期，通知被别人重新占用了，这时丢弃本次结果，只记日志。尝试记录照常写入，因为请求确实发出过。
- **为什么在占用时就给 attempt_count 加 1**：这样即使在请求发出后崩溃，这次尝试也被计入。`X-Notification-Attempt` 用的就是这个计数。
- **替代方案**：
  - worker 启动时把所有 `delivering` 重置为 `pending`。这只在单 worker 进程时成立，而且没有 fencing。
  - 不用 lease 用行锁。SQLite 没有行锁。

### D5 发送与结果判定（规则 2、5、6）

- **发送前检查**，按以下顺序：
  1. 时间检查：`now ≥ 有效截止时间` 时不发送，直接进入失败终态，原因按哪个期限先到来决定（见 D6）。
  2. 地址校验（D7）。
- **请求设置**：
  - httpx 设为 `follow_redirects=False`、`trust_env=False`（不读取环境变量里的代理配置）。超时 10s，可配。
  - 请求 Header 是提交时的 headers，再加上 `X-Notification-Id` 和 `X-Notification-Attempt`。如果提交的 headers 里已经有同名 Header，以系统的值为准。
- **响应读取**：用流式方式读取，最多读 1KB 作为摘录，读完即关闭连接，避免把大响应整个下载下来。
- **结果分类**：

| 结果 | 分类 |
|---|---|
| 2xx | `delivered` |
| 3xx；除 408、429 以外的 4xx | `failed/permanent_error`：重试不会有帮助，3xx 通常意味着 URL 配置错了 |
| 5xx、408、429；超时、连接错误、DNS 解析失败；其他传输异常 | 临时失败，进入重试 |

- 结果不明（超时、请求发出后连接中断）一律按临时失败处理，**可能重复送达**。下游需要依靠 `X-Notification-Id` 去重。

### D6 退避与终态（规则 4）

- **退避间隔**：`delay = min(10s × 2^(n-1), 1h)`，再叠加 ±20% 的随机抖动，其中 n 是已失败的次数。
- **Retry-After**：如果响应带了 `Retry-After`（秒数或 HTTP-date），就用它代替退避间隔，取值限制在 [1s, 1h] 之间。
- **有效截止时间**（候选人拍板）：`有效截止时间 = min(受理时间 + 最大重试时长, expires_at)`，两个期限哪个先到，就以哪个为准。失败原因按先到的那个记录：重试期限先到记 `retries_exhausted`，`expires_at` 先到记 `expired`；两者同时到，记 `expired`。
- **下一次重试时间**：失败后按正常规则计算，`candidate = now + delay`。
  - 若 `candidate < 有效截止时间`：`next = candidate`。
  - 否则（已经来不及再试一次）：`next = 有效截止时间`。到点被取出时，发送前检查会把它置为失败终态，不会再发请求。
  - 不为"最后再试一次"做任何特殊处理：既不把重试时间提前，也不压到截止时间那一刻。
- **状态更新的延迟**：进入终态的时间点，最多比截止时间晚一个 poll 周期加上取任务的延迟，这一点可以接受。
- **未采纳的 AI 建议**：
  - AI 最初的 design 写的是"如果 `expires_at` 早于 `next`，失败当时就直接判为已过期"。这样会在通知还没过期时就把它标成已过期，也和 spec 冲突，已被候选人指出并否决。
  - AI 随后提出"过期前把重试提前，作为最后一次尝试"，并配套把重试时间压到截止时间那一刻。候选人认为不值得为此增加特殊规则，否决。被否决的方案里还有一个连带问题：压到截止时间的那次尝试，被取出时已晚于截止时间，发送前检查总会把它判为重试耗尽，实际上永远发不出去。
- **持久化**：重试计划只存在数据库的 `next_attempt_at` 字段里，不用内存中的定时器。重启后从这个字段继续，退避进度不会被重置。
- **可配置项**：首次间隔、最大单次间隔、最大重试时长，测试里会把它们调成很小的值。

### D7 投递侧地址校验与 DNS rebinding（候选人拍板）

- **选择**：每次投递前，复用 `netguard.check_target` 重新解析并校验目标地址。
  - 命中内网等被禁止的地址：判为 `failed/blocked_target`，不发送。
  - 解析失败或超时：按临时失败处理，进入重试。
- **接受的局限（DNS rebinding 的残余窗口）**：从"校验完成"到"httpx 自己再解析一次并建立连接"之间，有一个毫秒级的窗口，期间 DNS 仍可能被改成内网地址。要利用这个窗口，攻击者必须同时满足两个条件：是内网中的可信调用方，并且控制目标域名的 DNS。
- **替代方案**：自定义 transport，直接连接已经校验过的 IP，同时把 Host 和 SNI 设为原域名。这能彻底消除窗口，但要自己处理 TLS SNI 和证书校验，测试成本明显更高。列为演进项。
- **测试如何绕过防护**：生产代码里**不加**"允许内网"之类的开关。测试在进程内把一个宽松的校验函数注入 worker；子进程测试使用测试目录下的专用入口脚本（`tests/worker_entry.py`），在脚本里完成同样的注入。

### D8 受理时 DNS 解析超时（候选人拍板）

- **选择**：受理时的 DNS 解析放进一个有界线程池（8 个线程）执行，用 `future.result(timeout=2s)` 等待结果。超时按"解析失败"处理，即照常受理，与 A 的 D4 一致。投递侧使用同样的机制。
- **接受的局限**：`getaddrinfo` 无法取消，超时之后对应的线程会在后台继续运行到解析结束。线程池有界，所以 DNS 全面变慢时，受理请求最多等 2s 就会返回，线程数不会超过 8。但线程池的任务队列是无界的：在高受理速率下 DNS 持续变慢，已经超时的解析任务仍会留在队列里，等着依次执行。MVP 接受这一点，演进方向是改成有界队列，队列满时直接按超时处理。
- **退出延迟**：DNS 线程池的线程不是守护线程。进程退出时要等这些线程结束，所以如果某个 `getaddrinfo` 卡住，SIGTERM 之后的退出会被推迟到系统解析器超时为止。这一点没有测试。

### D9 其他实现细节（AI 自主决定）

- **配置**：worker 只需要 `NOTIFIER_DB_PATH`，不需要 token 文件。为此 `config` 拆出一个只读取数据库路径的加载函数。其余参数都通过 `NOTIFIER_WORKER_*` 环境变量配置，并带有默认值。
- **优雅退出**：收到 SIGTERM 或 SIGINT 后，停止占用新的通知，等正在投递的完成后再退出。被 SIGKILL 时则依靠占用过期，让其他 worker（或重启后的自己）重新取走。
- **时钟**：所有"现在几点"都通过一个可注入的 `clock` 函数获取，测试里用假时钟推进时间。

## Risks / Trade-offs

- [重复送达] 超时、或请求发出后崩溃，都会导致重投 → 每次都带 `X-Notification-Id`，下游可以据此去重。这是"至少一次"的固有代价，在 README 里写明。
- [慢下游占满线程池] 在 C 之前，一个持续超时的下游最多能占住全部 16 个线程，每个占 10s → C 会加按目标的并发上限。
- [DNS rebinding 的残余窗口] 见 D7。
- ["200 + 业务错误码"会被当作成功] 这是透传边界带来的已知局限，阶段 1 已经接受。
- [尝试记录持续增长] 每次尝试一行，最多保存 1KB 的响应摘录，MVP 不做清理 → 演进时按时间归档。
- [响应摘录里可能有敏感信息] 只保存 1KB；C 的查询接口做脱敏的范围只包括 Header → 作为已知局限写明。
- [单写者串行化] API 受理和 worker 写回结果会争用同一把写锁 → 每个事务都很短，再加上 `busy_timeout`。在 MVP 的量级下可以接受，演进时改用 PG。
- [进程被杀可以测，掉电无法实测] 和 A 一样，只能用 SIGKILL 测试来证明。

## 本 change 不保证的事项

- 不保证恰好一次，也不保证顺序。
- 不保证在重试期限之外仍然送达。超出期限的通知进入终态，由 C 提供重放。
- 不识别 body 里的业务错误。
- 不严格防御 DNS rebinding。
- 不验证多个 worker 进程同时运行时的行为。

## Migration Plan

worker 启动和 API 启动时都会执行 `init_schema`，按版本号把 schema 从 v1 迁移到 v2。两个进程可能同时冷启动，所以迁移要先用 `BEGIN IMMEDIATE` 拿到写锁，再在锁内读取版本号，只执行尚未完成的迁移。这个问题是实现阶段发现的：修复前用并发冷启动复现，20 次里失败 17 次，报错 `table notifications already exists`。

修完这个问题后又发现第二个竞态：`PRAGMA journal_mode=WAL` 在切换日志模式时不会走 busy handler，并发冷启动时会直接报 `database is locked`。复现结果是 200 轮里失败 7 次，每轮 6 个线程。由于 WAL 模式会持久化保存在数据库文件中，修复方式是把 WAL 的设置从 `connect()` 挪到 `init_schema`，设置一次即可，遇到 locked 时有限次重试。修复后 200 轮里 0 次失败。这一点改变了 change A design D1 中"每个连接都设置 WAL"的做法：`connect()` 现在只设置连接级的 `synchronous=FULL` 和 `busy_timeout`。回归测试是 `tests/test_store_delivery.py::test_concurrent_cold_start_init_schema`。迁移是追加字段加回填，已经受理的 `pending` 通知会被 worker 直接取走。

不提供回滚到 v1：回退代码之后，新增的字段不会被读取，不影响 v1 的逻辑。
