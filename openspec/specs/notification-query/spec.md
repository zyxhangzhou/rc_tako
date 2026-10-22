# notification-query Specification

## Purpose
让提交方能自助查询某条通知的投递进度和每次尝试的结果，用于排障，同时避免在查询结果中泄露请求里的凭证类 Header。

## Requirements

### Requirement: 查询单条通知
系统 MUST 让已认证的调用方按通知 ID 查询自己提交的通知，返回内容包括：状态、失败原因、尝试次数、受理时间、过期时间、结束时间；处于待投递状态时，还包括下一次重试时间。此外 MUST 返回原始请求（url、method、headers、body）和按时间顺序排列的投递尝试记录。

#### Scenario: 查询已送达的通知
- **WHEN** 提交方查询一条先收到 500、再收到 200 的通知
- **THEN** 返回 200，状态为"已送达"，尝试记录有 2 条，按顺序分别带有状态码 500 和 200

#### Scenario: 查询等待重试的通知
- **WHEN** 提交方查询一条正在等待重试的通知
- **THEN** 返回内容中包含下一次重试时间

#### Scenario: 查询失败的通知
- **WHEN** 提交方查询一条因目标返回 400 而失败的通知
- **THEN** 返回内容中状态为"失败"，失败原因为永久失败

### Requirement: 查询的访问控制
系统 MUST 要求查询请求携带有效的调用方 token，缺失或无效时返回 401。通知不存在，或者属于其他调用方时，系统 MUST 一律返回 404，不暴露该通知是否存在。

#### Scenario: 查询他人的通知
- **WHEN** 调用方 B 查询调用方 A 提交的通知
- **THEN** 返回 404

#### Scenario: 查询不存在的通知
- **WHEN** 调用方查询一个不存在的通知 ID
- **THEN** 返回 404

#### Scenario: 未认证查询
- **WHEN** 查询请求没有携带 token
- **THEN** 返回 401

### Requirement: 查询结果中的 Header 脱敏
查询结果中的请求 headers，MUST 只对白名单内的 Header 显示原值；白名单为 Content-Type、Accept、User-Agent、Content-Encoding、Accept-Language，比较时不区分大小写。其余 Header MUST 保留名称，值替换为 `***`。脱敏只作用于查询结果，MUST NOT 影响实际投递时发送的内容。

#### Scenario: 凭证 Header 被脱敏
- **WHEN** 提交时 headers 为 `{"Content-Type": "application/json", "X-Api-Key": "secret", "authorization": "Bearer t"}`
- **THEN** 查询结果中 Content-Type 为原值，X-Api-Key 和 authorization 的值都是 `***`

#### Scenario: 投递不受脱敏影响
- **WHEN** 一条带有 X-Api-Key 的通知被投递
- **THEN** 下游收到的 X-Api-Key 是原值
