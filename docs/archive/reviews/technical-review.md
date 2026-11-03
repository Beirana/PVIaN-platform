# 技术审核记录

> **历史审核快照，非当前权威。** 它保留验收 A 阶段的审计背景；当前冻结请看 v0.5.0 release review。

- 日期：2026-07-24
- 范围：PVI Capability Service 验收 A
- 状态：passed（本机 Docker daemon 未运行；GitHub Actions 镜像构建已验证）

检查项：

- [x] Schema、Pydantic、OpenAPI、fixtures 一致
- [x] 模型、端点、运行器能力未混淆
- [x] 未按平台名称赋信任
- [x] unknown/not_tested/inconclusive 未升级
- [x] 敏感信息与出站网络扫描通过
- [x] 未编造真实供应商语义
- [x] PVI 未替换消费者模型调用路径
- [x] 故意注入无效 profile 时启动校验失败

证据：

- `acceptance/run-offline.ps1`：通过；
- Ruff：通过；
- 数据/引用校验：3 endpoints、3 profiles、3 evidence、19 probes、1 probe run；
- OpenAPI 3.1 固化一致性：通过；
- pytest：22 passed；
- 敏感内容与 PVI 源码禁止出站 import 扫描：通过；
- 本机 Docker build：未执行成功，原因是 Docker Desktop Service 停止；
- GitHub Actions Docker build：通过；
- GitHub Actions offline-acceptance：通过。
