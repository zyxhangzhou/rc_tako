# service-metrics Specification

## Purpose
以 Prometheus 文本格式暴露投递相关的运行指标，让运维能发现下游长期不可用、积压增长或投递进程停止等问题，并在此基础上配置告警。

## Requirements

### Requirement: 投递指标
系统 MUST 在 `GET /metrics` 上以 Prometheus 文本格式输出以下指标：
- 各状态的通知数。
- 各失败原因的通知数。
- 已到期但尚未被取出的待投递通知数（积压量）。
- 最早到期的待投递通知已等待的秒数。
- 各结果分类的累计投递尝试次数。

指标数值 MUST 反映存储中的实际数据，在服务重启后不得归零。

#### Scenario: 状态计数
- **WHEN** 存储中有 2 条已送达、1 条失败（永久失败）、3 条待投递的通知
- **THEN** 指标中，已送达为 2，失败为 1，待投递为 3，永久失败原因计数为 1

#### Scenario: 积压与等待时长
- **WHEN** 有一条待投递通知在 120 秒前已经到期，但还没有被取出
- **THEN** 积压量为 1，最早到期通知的等待秒数约为 120

#### Scenario: 无积压
- **WHEN** 没有已到期的待投递通知
- **THEN** 积压量为 0，等待秒数为 0

#### Scenario: 重启后不归零
- **WHEN** 服务重启
- **THEN** 累计尝试次数等指标与重启前一致

### Requirement: 指标不含通知内容
`GET /metrics` MUST 只包含计数类数据，MUST NOT 包含通知 ID、url、headers、body 等内容，也不以 host 作为标签。该端点不要求认证。

#### Scenario: 未认证访问
- **WHEN** 不带 token 请求 `GET /metrics`
- **THEN** 返回 200，内容中不包含任何 url 或通知 ID
