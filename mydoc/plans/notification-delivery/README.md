# API 通知投递服务

- Current: [notification-delivery-v1.md](notification-delivery-v1.md)
- Language: zh
- Requirement: 实现一个内部服务，接收业务系统提交的外部 HTTP 通知请求，并尽可能可靠地投递到目标地址（题目见 docs/assignment.md）

## 版本链

| Version | Date | Session | Trigger | Summary |
|---|---|---|---|---|
| v1 | 2026-10-22 | edba609d-5f50-4748-b206-3de02b21dfa6 | initial | 首版拆分：A 受理与持久化、B 投递引擎、C 隔离与运维，外加交付收尾 |
