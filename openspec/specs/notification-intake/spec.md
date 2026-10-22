# notification-intake Specification

## Purpose
定义业务系统提交外部 HTTP 通知的对外契约：谁能提交、什么样的请求会被受理，以及"受理成功"对调用方意味着什么。

## Requirements

### Requirement: 受理成功即已持久化
系统 MUST 只在通知已经持久化之后才返回受理成功（202 和通知 ID）。如果持久化失败，系统 MUST 返回 503，不得返回成功。调用方收到成功之后，这条通知的送达责任就转到本系统。

#### Scenario: 正常受理
- **WHEN** 已认证的调用方提交一个合法的通知
- **THEN** 系统返回 202，响应中带有通知 ID，并且该通知已经以"待投递"状态持久化

#### Scenario: 受理后进程立即崩溃
- **WHEN** 调用方收到 202 之后，服务进程立刻被强制终止，然后重启
- **THEN** 用该通知 ID 能在存储中找到这条通知，状态为"待投递"，内容与提交时一致

#### Scenario: 持久化失败
- **WHEN** 存储写入失败或超时
- **THEN** 系统返回 503，并且不会留下一条"已受理"的通知

### Requirement: 调用方认证
系统 MUST 要求每个提交请求携带一个已配置的调用方 token，并记录这条通知是哪个调用方提交的。token 缺失或无效时，系统 MUST 返回 401，并且不受理该请求。

#### Scenario: 缺少 token
- **WHEN** 提交请求没有携带认证信息
- **THEN** 系统返回 401，不会持久化任何通知

#### Scenario: token 无效
- **WHEN** 提交请求携带一个未配置的 token
- **THEN** 系统返回 401

#### Scenario: 记录提交方
- **WHEN** 调用方 `crm-service` 用自己的 token 成功提交
- **THEN** 持久化的通知记录中，调用方一栏为 `crm-service`

### Requirement: 请求字段校验
系统 MUST 校验以下字段，任何一项不合法都返回 422，并且不受理：
- url：必填，scheme 只能是 http 或 https，并且必须带 host。
- method：必填，只能是 POST、PUT、PATCH、DELETE、GET 之一。
- headers：可选，必须是字符串到字符串的映射，并且不能包含 Host、Content-Length、Transfer-Encoding、Connection。
- body：可选，必须是文本，UTF-8 编码后不超过 1MB。
- expires_at：可选，必须带时区，并且晚于当前时间。

#### Scenario: 不支持的 scheme
- **WHEN** 提交的 url 是 `ftp://example.com/x`
- **THEN** 系统返回 422

#### Scenario: 不支持的 method
- **WHEN** 提交的 method 是 `TRACE`
- **THEN** 系统返回 422

#### Scenario: body 超过上限
- **WHEN** 提交的 body 经 UTF-8 编码后超过 1MB
- **THEN** 系统返回 422

#### Scenario: 携带禁止的 Header
- **WHEN** 提交的 headers 中包含 `Host`
- **THEN** 系统返回 422

#### Scenario: 过期时间已过
- **WHEN** 提交的 expires_at 早于当前时间
- **THEN** 系统返回 422

#### Scenario: 过期时间不带时区
- **WHEN** 提交的 expires_at 为 `2026-10-23T10:00:00`，没有时区
- **THEN** 系统返回 422

### Requirement: 拒绝内网与元数据目标地址
系统 MUST 拒绝目标地址属于私网、回环、链路本地（含云元数据地址 169.254.169.254）、组播、保留或未指定地址的请求，返回 422。目标是域名时，只要解析结果中有任意一个地址命中上述类别，就必须拒绝。如果域名解析失败，或在 2 秒内没有返回结果，系统 MUST 照常受理，由投递阶段再次校验。

#### Scenario: 回环地址
- **WHEN** 提交的 url 是 `http://127.0.0.1:8080/hook`
- **THEN** 系统返回 422

#### Scenario: 云元数据地址
- **WHEN** 提交的 url 是 `http://169.254.169.254/latest/meta-data`
- **THEN** 系统返回 422

#### Scenario: 私网 IPv6 与 IPv4-mapped 地址
- **WHEN** 提交的 url 的 host 是 `[::1]` 或 `[::ffff:10.0.0.1]`
- **THEN** 系统返回 422

#### Scenario: 域名解析到内网
- **WHEN** 提交的 url 的域名有一个解析结果是 `10.0.0.5`
- **THEN** 系统返回 422

#### Scenario: 域名解析失败
- **WHEN** 提交的 url 的域名在受理时无法解析
- **THEN** 系统照常受理，返回 202

#### Scenario: 域名解析超时
- **WHEN** 提交的 url 的域名在受理时解析超过 2 秒仍未返回
- **THEN** 系统在约 2 秒时照常受理，返回 202

#### Scenario: 公网地址
- **WHEN** 提交的 url 的域名只解析到公网地址
- **THEN** 系统照常受理，返回 202

### Requirement: 幂等提交
系统 MUST 要求每个提交请求携带幂等 key，缺少时返回 422。同一调用方用同一个 key 再次提交时，系统 MUST NOT 新建通知：如果请求内容（url、method、headers、body、expires_at）与首次提交相同，返回 200 和首次的通知 ID；如果内容不同，返回 409。不同调用方的 key 互不影响。在并发提交下，这些规则同样成立。

#### Scenario: 缺少幂等 key
- **WHEN** 提交请求没有携带幂等 key
- **THEN** 系统返回 422

#### Scenario: 相同内容重复提交
- **WHEN** 调用方用同一个 key 和相同的内容提交了两次
- **THEN** 第二次返回 200，带回首次的通知 ID，存储中只有一条通知

#### Scenario: 同一 key、内容不同
- **WHEN** 调用方用同一个 key、不同的 body 再次提交
- **THEN** 系统返回 409，原来那条通知不变

#### Scenario: 不同调用方使用同一个 key
- **WHEN** 调用方 A 和调用方 B 各自用 key `evt-1` 提交
- **THEN** 两次都返回 202，生成两条不同的通知

#### Scenario: 并发重复提交
- **WHEN** 同一调用方用同一个 key 和相同的内容同时发出多个提交请求
- **THEN** 存储中只有一条通知，所有成功的响应带回的都是同一个通知 ID
