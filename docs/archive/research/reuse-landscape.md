# PVI 验收 A 复用调研

> **历史调研，非当前选型或实施计划。** 当前决策以 ADR、`PROJECT_STRUCTURE.md` 与 `ROADMAP.md` 为准。

> 调研日期：2026-07-24
> 范围：只读 Capability Service；不包含供应商 Runtime、路由或在线 Probe。
>
> 状态说明（2026-07-31）：这是验收 A 的历史复用调研，保留其结论与证据。对于新增的
> 透明聚合网关 P0 工作流，它不再是选型结论；请以
> [`ADR-TRANSPARENT-GATEWAY-003`](../../../decisions/ADR-TRANSPARENT-GATEWAY-003.md) 和
> 后续保真 POC 为准。

## 结论

本期不嵌入 LiteLLM、Portkey Gateway 或 Bifrost。它们解决的核心问题是多供应商调用、路由、重试、计费和协议转换，而验收 A 只加载版本化档案、校验合同并返回确定性的能力解析结果。嵌入网关会扩大凭证、网络、升级和供应链攻击面，也会模糊“能力档案面不进入模型调用链”的边界。

本期复用以下通用组件：

| 组件 | 用途 | 许可证 | 替换边界 |
|---|---|---|---|
| FastAPI | HTTP 路由与 OpenAPI 生成 | MIT | API 层，可替换而不改变冻结 JSON 合同 |
| Pydantic | 请求/响应运行时校验 | MIT | Python Domain DTO，可由 dataclass 等替换 |
| jsonschema | Draft 2020-12 数据校验 | MIT | Infrastructure 校验器 |
| PyYAML | Probe Catalog 与 OpenAPI YAML | MIT | 文件解析器；只使用安全加载 |
| pytest/httpx | 离线测试与 ASGI contract test | MIT/BSD-3-Clause | 仅开发和验收 |

## 候选网关

### LiteLLM

- 能力：多供应商统一调用、代理、路由、预算和模型目录。
- PVI 适配价值：未来验收 B 或已有网关端点的 adapter/证据来源。
- 本期不采用原因：验收 A 不调用模型；引入会增加大量无用依赖和供应链风险。
- 维护策略：若未来采用，必须锁定哈希、进行 SBOM/漏洞扫描，并单独评估发布渠道安全。
- 退出方案：PVI 合同绑定 `endpoint_id + api_surface`，不绑定 LiteLLM 内部类型。

### Portkey AI Gateway

- 能力：统一 API、路由、重试、回退、限流、预算和 guardrails。
- 许可证：官方开源 gateway 仓库标示 MIT。
- PVI 适配价值：可作为 `broker_gateway` 或 `self_hosted_gateway` 登记和探测。
- 本期不采用原因：同样超出只读能力档案范围；其路由/回退还会使模型身份和协议保真证据更复杂。
- 退出方案：只通过外部 HTTP 端点和 EvidenceReference 观察，不导入其内部模型。

### Bifrost

- 能力：高性能多供应商 AI Gateway。
- PVI 适配价值：未来可以按独立 endpoint/profile 登记。
- 本期不采用原因：Go 网关运行时与 Python 只读档案服务无直接复用收益。
- 退出方案：保持供应商无关合同和显式 endpoint identity。

## 统一维度评估

| 维度 | 本期判断 |
|---|---|
| 许可证与商业限制 | 只使用宽松许可证的通用组件；发布前生成依赖清单 |
| 维护与升级 | Python 依赖限定兼容大版本；CI 校验锁定环境 |
| 供应商/API Surface | 本期不连接供应商 |
| 流式/工具/reasoning/usage | 只作为档案字段，不经过网关翻译 |
| 本地模型 | 只登记 profile，不启动推理 |
| 原始证据 | 保存脱敏引用或摘要，不建设证据仓库 |
| 自定义 adapter | 本期无 adapter |
| 认证/限流/预算 | 内网部署前提；生产鉴权属于后续部署 ADR |
| 离线部署 | 数据与验收均不需要外部网络 |
| Canonical contract | 不实现验收 B canonical contract |
| 退出方案 | 冻结 JSON Schema/OpenAPI 与框架实现解耦 |

## 参考来源

- FastAPI 官方文档：https://fastapi.tiangolo.com/
- Pydantic JSON Schema 文档：https://docs.pydantic.dev/latest/concepts/json_schema/
- JSON Schema Draft 2020-12：https://json-schema.org/draft/2020-12
- LiteLLM 官方仓库：https://github.com/BerriAI/litellm
- Portkey Gateway 官方仓库：https://github.com/Portkey-AI/gateway
- Bifrost 官方仓库：https://github.com/maximhq/bifrost

## 重新评估触发条件

只有在验收 B 立项、目标端点已选择，或现有消费者明确要求复用某网关时，才重新评估嵌入/后接方案，并补充许可证、版本、协议保真、供应链和退出测试。
