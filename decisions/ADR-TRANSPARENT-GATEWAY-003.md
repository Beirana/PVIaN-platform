# ADR-TRANSPARENT-GATEWAY-003：以保真优先的透明聚合网关作为独立 P0 工作流

- 状态：Accepted（第一阶段 MVP 已实现；持续资格复核与下游接入待部署）
- 日期：2026-07-31
- 决策范围：验收 A 之外的内部模型调用基础设施；不改变验收 A 的完成定义

## 背景

后续开发任务、其他实验目录以及 `misalignment-platform` 需要通过同一内部入口调用多个
供应商或联合网关，首先覆盖 Yunwu 与阿里云百炼，后续可能覆盖其他联合网关、OpenRouter
及官方直连端点。目标不是把不同厂商的语义压缩成最低公共集，而是减少各业务仓库重复
适配，同时保留每个精确端点所暴露的 reasoning、SSE、native tool call、tool-result replay、
usage 与错误语义。

验收 A 的 Capability Service 仍是只读能力档案服务；它不能被误写为新的调用网关，
也不能被放进网关的请求关键路径。已有受控在线 Probe 只能产生时间、端点和配置特定的
观察证据，不能构成 SLA、上游模型身份或长期稳定性的证明。

## 决策

1. 建设一个**自维护的透明聚合网关**，作为独立组件和新的 P0 工作流；它不属于验收 A，
   也不宣称验收 B Turn/SSE Runtime 已实现。
2. 第一阶段只接纳通过保真门禁的 OpenAI-compatible 精确端点。消费者使用显式的
   `route_id` 选择供应商、base URL、path、transport 和精确模型；禁止按价格、健康度、
   能力表或模型别名进行自动选路。
3. 对已通过门禁的透明路径，网关必须原样转发请求 body，并原样透传上游 HTTP/SSE body；
   它不得重写 model、messages、tools、tool-call id、arguments、reasoning 字段、usage、
   finish reason 或错误正文。鉴权 header 仅在网关内部按 route 注入，绝不向下游回显。
   当前 MVP 固定上游 `Accept-Encoding: identity`；若上游仍压缩 body，必须保留
   `Content-Encoding`，而不透传失效的 `Content-Length/Transfer-Encoding`。
4. 网关默认没有自动 fallback、负载均衡、SDK 隐式重试、工具执行或 tool-call 后重放。
   上游可观察错误保持上游语义；仅允许在请求尚未可见且有单独 ADR 的情况下讨论重试。
5. Capability Service 继续保存受审查的端点/能力/证据结论；网关只引用它的端点身份和
   证据摘要，运行请求不得依赖它在线可用。模型选择与 thinking 参数属于版本化 provider
   profile，不属于 `.env`。
6. 任何非 OpenAI-compatible 协议（例如原生 DashScope 或 Anthropic）必须以显式 adapter
   单独立项，并先通过同样的逐字段差分保真测试；不能因“统一接口”而默认转换。
7. Portkey passthrough、LiteLLM 与 Bifrost 是可比较的 POC 候选，不是第一阶段的运行时
   依赖。若某候选不能保留所需字段、流事件顺序或重放语义，即使其运维功能更丰富也不得
   成为默认数据面。OpenRouter 是可登记的上游 broker，不是本项目网关的替代物。

## 保真与证据门禁

每条 `provider + base_url + path + transport + requested_model + 参数摘要` 组合必须先进行
直连与经网关的配对测试。至少比较：

- 普通非流式响应、SSE 事件顺序与结束语义；
- `requested_model`、`reported_model`、reasoning 字段及 reasoning token；
- native tool call 的结构、call id 与增量 arguments；
- assistant 原样回放、tool-result replay 后继续响应及 replay 后 reasoning；
- usage、状态码、协议错误分类和可见 header 的契约。

证据只保留脱敏元数据、字段存在性、类型、长度/哈希、延迟、usage、状态和配置摘要。
不得保存 API Key、Authorization、原始 prompt、原始 response、完整 reasoning 或上游错误正文。
单次、burst 或 soak 结果都必须带观测时间窗和样本数；它们是运行观测，不得被表述为 SLA。

## 备选方案与取舍

| 方案 | 决策 |
|---|---|
| 各业务仓库直接适配所有供应商 | 不采用；重复实现会造成凭证、协议和证据口径漂移。 |
| 自维护透明网关 | 首选；可以把保真约束、route identity 与脱敏证据作为本项目合同。 |
| Portkey passthrough | 作为对照 POC；官方文档描述其 passthrough 不转换请求/响应，但仍须以本项目差分测试验证。 |
| LiteLLM / Bifrost | 作为对照 POC 或未来显式 adapter；其统一/路由能力不能替代保真验证。 |
| Kong / Envoy | 留待多团队、Kubernetes 或成熟治理需求出现后评估。 |

## 后果与边界

- 新组件将为开发任务和后续实验提供一个统一、显式路由的内部入口；`misalignment-platform`
  的接入必须在其仓库中另行实施与验收。
- 现有 `pvi.probe_runner` 和 `pvian_probe` 不再各自扩展为第三套 provider runner；后续应
  收敛为 profile-driven qualification runner，复用既有 reasoning/SSE 解析与脱敏规则。
- 当前 Yunwu 4×3 报告保留为历史观察，不作为路由或长期稳定性决策的输入。
- 这项决策**不会**解除验收 B 尚未实现的事实，也不会把自动路由、自动 failover、成本优化
  或业务工具执行纳入第一阶段。第一阶段包含受控的“到期/五连败后复核”CLI 入口；由外部
  scheduler 每 24 小时调用属于部署配置，不在 CI 或服务启动时自动联网。

## 退出与复核条件

网关通过显式 HTTP 合同与 `route_id` 工作，不向消费者泄露内部实现类型；因此可替换为
另一透明实现。若 POC 显示任何所需字段或 SSE 语义不能保真，应关闭该路径并让消费者
继续使用直连，而不是用“兼容”掩盖损失。引入自动路由、重试、健康探测或持久化监控前，
必须新增 ADR，并定义时间窗、成本上限、隐私边界和失败后的人工处置规则。周期复核的默认
有效期为 24 小时、连续失败阈值为 5；费用上限暂不定义，只记录可获得的 usage 计数。

## 与既有决策的关系

- `ADR-REUSE-001` 对验收 A 仍有效；其“不要嵌入网关”结论在本 ADR 的独立网关工作流内
  被取代。
- `ADR-ONLINE-PROBE-001` 的显式授权、离线禁用、脱敏与人工审核规则继续适用。
- `ADR-UNIFIED-REPOSITORY-002` 的单一主仓库与两个既有 Probe 的收敛方向继续适用。

## 参考

- [Portkey passthrough documentation](https://portkey.ai/docs/integrations/llms/passthrough)
- [LiteLLM documentation](https://docs.litellm.ai/)
- [Bifrost overview](https://docs.getbifrost.ai/overview)
- [OpenRouter provider routing](https://openrouter.ai/docs/guides/routing/provider-selection)
