# ADR-REUSE-001：验收 A 采用轻量原生合同服务

- 状态：Accepted（仅限验收 A；网关工作流见 ADR-TRANSPARENT-GATEWAY-003）
- 日期：2026-07-24
- 决策范围：PVI Capability Service 验收 A

> 范围说明（2026-07-31）：本 ADR 对验收 A 的“只读、无凭证、非调用链”边界继续有效。
> 新增的透明聚合网关是独立 P0 工作流，不嵌入 Capability Service；该部分由
> [`ADR-TRANSPARENT-GATEWAY-003`](ADR-TRANSPARENT-GATEWAY-003.md) 决定。

## 背景

PVI 验收 A 必须提供版本化 Endpoint/Capability Profile、Evidence/Probe 引用、只读 HTTP 查询和确定性的 requirements resolver。它不得持有模型凭证、调用供应商或进入原平台模型调用链。

## 决策

使用 Python、FastAPI、Pydantic、jsonschema、PyYAML 和版本化 JSON/YAML 文件实现。FastAPI 仅位于 API 层；冻结合同以 `contracts/schemas/` 和 `contracts/openapi/` 为权威交付物。

不嵌入 LiteLLM、Portkey、Bifrost 或其他 LLM Gateway，不实现 Runtime adapter、路由、回退、预算或在线 Probe。

## 复用边界

- FastAPI：HTTP 与 OpenAPI。
- Pydantic：输入输出 DTO，不作为跨仓库共享类型。
- jsonschema：Draft 2020-12 启动校验。
- PyYAML：只用 `safe_load` 读取受版本控制的 YAML。
- pytest/httpx：离线 ASGI 测试。
- 所有消费者只通过 `/v1` HTTP 合同接入。

## 许可证与维护

选用组件均为宽松开源许可证。依赖限定兼容大版本，并通过 CI、离线验收和依赖审计维护。发布构建不得在服务启动时下载数据或依赖。

## 后果

正面：

- 边界小，启动和离线验收简单；
- 不接触模型凭证和供应商网络；
- 能直接验证 JSON Schema 与 OpenAPI；
- 将网关的路由语义与能力事实分离。

代价：

- 不直接复用网关模型目录；
- 没有自动 Probe、路由或实时供应商发现；
- 档案更新通过受审查的数据变更完成。

## 风险与缓解

- Schema 与 Pydantic 漂移：contract tests 同时验证数据、API 和固化 OpenAPI。
- 文件重复或引用悬空：启动时建立索引并拒绝重复 ID、无效 Evidence/Endpoint 引用。
- 档案被误当供应商事实：fixtures 使用明显占位身份，`advertised/observed/effective` 严格分离。
- 框架锁定：Domain/Application 不依赖 FastAPI；消费者不导入 Python 内部类型。

## 退出方案

未来可以用数据库替换文件仓库、用其他 HTTP 框架替换 FastAPI，或在独立验收 B 模块后接网关；只要保持冻结 Schema/OpenAPI 和 A/B 无运行依赖，就不需要改变消费者合同。

## 合同影响

无。该决策只选择实现技术，不修改 PVI-00 冻结 v1 合同。
