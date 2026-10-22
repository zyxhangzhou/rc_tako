# Spec Delta

## Purpose

保存已受理的通知，让它们在进程重启后仍然完整可用，为后续的投递提供可靠的数据来源。

## ADDED Requirements

### Requirement: 完整保存投递所需内容
系统 MUST 为每条已受理的通知保存投递所需的全部内容：url、method、headers、body、expires_at，以及通知 ID、提交方、幂等 key 和受理时间。保存的内容 MUST 与提交时一致，不能改写。

#### Scenario: 内容原样保存
- **WHEN** 一条带有自定义 headers 和 JSON body 的通知被受理
- **THEN** 按通知 ID 读出的 url、method、headers、body、expires_at 与提交时逐项一致

### Requirement: 新受理通知的初始状态
系统 MUST 把新受理的通知置为"待投递"状态。

#### Scenario: 初始状态
- **WHEN** 一条通知被受理
- **THEN** 它在存储中的状态是"待投递"

### Requirement: 重启后数据不丢失
系统 MUST 保证：已返回受理成功的通知，在服务进程正常退出或被强制终止后，重启时仍然存在，内容和状态都不变。这一保证的前提是存储介质本身没有损坏。

#### Scenario: 强制终止后重启
- **WHEN** 已受理若干条通知后，服务进程被 SIGKILL 终止，然后重新启动
- **THEN** 这些通知全部存在，状态仍是"待投递"，内容不变

#### Scenario: 首次启动
- **WHEN** 服务在一个没有数据文件的环境中启动
- **THEN** 系统自动初始化存储，并且能够正常受理通知
