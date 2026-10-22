# Spec Delta

## MODIFIED Requirements

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
