# 当前规划与迭代计划

> 2026-08-03 MVP note: local, versioned token-price estimates are recorded only
> after a provider response body completes. They are not budgets, invoices, or
> a consumer-facing pricing API.

> 当前权威计划。基线是 **v0.5.1 内部版**；本文件取代初期实施计划和 P0 迁移登记作为日常规划入口。
> 历史计划保留在 [docs/archive/INDEX.md](docs/archive/INDEX.md)，不应被当作当前完成状态或工作指令。

## 版本策略

- 近期只在 **0.5.x** 演进：补 provider、资格和保真合同，修复已发现错误，并完成下游薄客户端接入。
- 只有出现新的、已批准的外部合同（例如非 OpenAI-compatible adapter 或公开 API 语义变更）才考虑 0.6。
- 只有多个实验平台长期稳定地使用同一消费者合同后，才评估 1.0；当前不为版本号预先扩张范围。

## 0.5.x 工作流

| 工作项 | 当前状态 | 完成标准 | 依赖与不做事项 |
|---|---|---|---|
| Provider 扩充 | Yunwu 与百炼 OpenAI-compatible profiles/aliases 已实现；其他联合网关、OpenRouter、官方直连尚未接入 | 每个精确 endpoint + model 具备版本化 profile、离线合同、显式 qualification 和唯一 alias | 需要用户提供非敏感端点身份与网关机环境变量；不猜测模型 ID，不静默 fallback；非兼容协议先 ADR/adapter |
| Qualification 与稳定性观察 | 24h 有效期、五连败提前复核、脱敏 state 和一次短 burst 观察已实现 | 单次 smoke、低量 burst 与可选受控 soak 均输出样本数、时间窗、分类失败、延迟与恢复摘要 | 必须显式网络/成本授权；不把短测写成 SLA，不在 CI/启动/普通调用时自动付费复核 |
| Thinking、工具、replay、SSE 保真 | alias/route 的离线合同和 DeepSeek V4 Pro 时间窗观察已具备 | 新 provider 对 non-stream、SSE `[DONE]`、reasoning/usage、native tool、assistant replay、tool-result replay 做字段级差分结论 | 不跨 provider 规范化 reasoning 字段，不保存原始内容；联合 thinking + tool 必须按该 profile 的已观察请求形态验证 |
| Provider/model 能力表积累 | 现有 profile 与 qualification 已记录部分 endpoint/model 事实，但还没有覆盖多种 API surface 的统一能力表单 | 每个稳定绑定身份都有版本化能力表和独立观测元数据，区分 advertised/observed/effective 并引用脱敏证据 | 不按模型名称复制能力；不把同一模型在不同供应商、区域或 API surface 的结论合并；未知保持未知 |
| 协议方言与适配层规划 | 当前网关仅透明保留 OpenAI-compatible Chat Completions 语义，尚未统一不同 reasoning/tool/event 方言 | 在能力表覆盖足够真实组合后，定义可逆、可追溯的内部能力词汇与薄 adapter；无法无损映射的字段必须保留原始方言和明确能力缺口 | 0.5.x 先积累合同与证据，不提前建设“大一统”运行时；Responses API、Anthropic Messages 等新 surface 需独立 ADR、adapter 和验收 |
| 网关可靠性修复 | Bearer、gzip、状态跨进程锁已修复并冻结 | 发现的鉴权、协议、状态或保真缺陷有回归测试、发布说明和必要 ADR | 不以健康度触发自动切换、重试或负载均衡 |
| Probe 收敛 | profile-driven qualification 已可用；两个 legacy probe 仍保留 | 复用现有 reasoning/SSE 脱敏解析，将常用矩阵沉为 preset，且不增加第三套通用 runner | legacy Yunwu 单模型与 4×3 报告保留为历史/对照证据，不覆盖原记录 |
| 下游薄客户端 | 网关消费者合同和样例已提供；`misalignment-platform` 尚未修改 | 在目标业务仓库实现 Base URL + gateway key + public alias 客户端、mock 与回归；Capability Service 的 advisory 路径按需独立接入 | 不在本仓库未经授权修改下游平台；不把 provider key 下发给业务项目 |
| R11 / 实验平台接入 | 尚未在目标仓库实施 | 按各仓库的独立 manifest、权限和验收接入上述薄客户端 | 不把 provider 资格观察当成实验结果或运行许可 |

## 能力表与协议方言收敛路线

不同供应商即使暴露相同模型名，也可能在请求参数、响应字段、流式事件和工具回放上表现不同。PVIaN 后续
不能只维护“模型支持 thinking/tool”这样的扁平布尔表，而应将稳定绑定身份与观测元数据分开：

```text
stable binding:
endpoint operator/provider + declared upstream provenance
+ region/base URL policy + endpoint/path/transport
+ profile ID/version + requested model + model revision/deployment
+ relevant configuration digest + API surface/version + request dialect version

observation metadata:
reported model + observed_at + runner revision + evidence/config digest
```

中转/聚合网关的 endpoint operator 与其声明的上游模型来源必须分开；后者无法核实时保持
`declared_unverified` 或 `unknown`，不能从返回的 model 名称推断。

第一阶段只积累版本化能力表和证据，不改写网关响应。每张能力表至少覆盖：

- 消息与内容：支持的 role、content block、多模态形态、上下文与 usage 字段；
- thinking/reasoning：请求开关与强度参数、非流式字段、SSE delta/event 字段、token 摘要、续接条件；
- 工具调用：tool schema、`tool_choice` 取值、native structured/text simulation/provider built-in 等表示方式、
  并行调用、流式 arguments 合并、call ID、assistant replay、tool-result role/字段、继续响应，以及工具由
  provider、consumer/外部 runtime（测试时可由 runner 代演）或无人执行的责任边界；
- API surface：OpenAI Chat Completions、OpenAI Responses API，以及未来经批准的其他 Messages/Events
  surface；分别记录对象结构、事件类型、终止标记、状态码和错误语义；
- 保真与稳定性：gzip/SSE、finish reason、request ID、429/5xx/timeout 分类、短窗成功率与证据时间。

能力表继续保留 `advertised / observed / effective` 三列，每列分别使用
`supported / unsupported / unknown / not_tested / inconclusive` 等状态。供应商文档只能形成 advertised；
只有精确身份下的受控 probe 才能形成 observed；effective 是依据版本化档案、证据与明确策略派生的能力结论。

这三列不等于网关当前可调用性：qualification 是否通过、24 小时有效期、五连败复核和 profile/credential
漂移属于独立的 operational callability 层；某个消费者的 requirements 是否得到满足又是第三层。能力事实、
当前能否调用、特定实验是否允许运行不得互相替代。

第二阶段才根据积累结果设计适配层：

1. 提取跨 provider 的最小内部能力词汇，而不是统一原始 chain-of-thought 文本；
2. 每个 adapter 明确“无损映射、语义映射、不可映射”字段，并保留原始方言证据；
3. Chat Completions 与 Responses API 不互相假定等价；reasoning item、output item、function call/output、
   `previous_response_id` 和流式 event 必须分别验证；
4. 下游可以请求统一能力需求，但最终仍显式选择唯一 alias，不引入自动 provider fallback；
5. 只有能力表覆盖多个真实 provider/model/API surface 且在 R11、misalignment-platform 中稳定使用后，
   才评估将适配合同提升为 0.6 的公开语义。

## 多模型阶段的按需发现与资格化（后续更新项）

> 该项已登记但暂缓实施。当前只有单一目标模型，继续使用显式 profile qualification；当多 provider、多模型使逐模型手工建 profile、升版和加 alias 成为维护瓶颈时再启动。

目标是把人工维护边界收敛到 provider endpoint、凭据、协议方言和资格政策模板；模型级事实由首次使用前的按需预检产生，而不是要求运维人员为中转站目录中的每个模型预先建档：

1. 调用方提交显式 provider、精确模型 ID 和所需能力；基础设施先读取该 endpoint 的实时模型目录。
2. 目录无精确 ID 时记录 `not_discovered`；不得替换为相近名称。
3. 目录存在时运行低量合成资格探针，并区分 `call_failed`、`protocol_mismatch`、`capability_mismatch` 与 `qualified`，避免把“模型不存在”和“模型存在但输出不符合要求”合并为同一种失败。
4. 将目录发现、请求/返回模型身份、实际观测能力、失败分类、配置摘要和 TTL 自动写入 qualification state 与能力表；有效期内复用，到期或发生漂移后再资格化。
5. 首次预检与调用方业务请求、科学实验结果分离；同一 provider/model 的并发首次请求应合并为一次资格任务，避免重复付费探针。
6. `qualified` 只表示该精确绑定在当前时间窗满足声明的最低能力。它不引入跨 provider 自动 fallback、模型名近似替换或未验证的能力继承。

该更新需要独立 ADR，明确动态 model binding/alias 的生命周期、缓存与并发语义、失败返回合同及 Capability Service 的写入接口；在此之前不改变当前网关准入路径。

## 明确不在当前路线中的事项

以下事项均不是 0.5.x 默认工作，不得以“网关”名义偷偷加入：

- 1.0 发布、多租户、公网暴露、业务权限体系；
- 计费、预算、配额、费用优化或账单真实性保证；
- 自动路由、健康度替换、fallback、负载均衡、隐式重试；
- SLA、长期可用性承诺、持续监控替代人工判断；
- 验收 B Canonical Turn/SSE Runtime、业务工具执行或 MCP/Skill 执行。

若确有需求，先新建 ADR，明确身份、字段/流语义、失败处理、隐私、成本、部署与验收条件，再决定版本。

## 每个迭代的完成口径

1. 配置身份精确：provider、base URL、path、transport、endpoint、profile version、route 和 exact model 无歧义。
2. 离线优先：先通过数据、Schema、鉴权、保真与安全扫描；`PVI_NETWORK_DISABLED=1` 时无网络调用。
3. 实时验证显式授权且脱敏：保存结构性结论和时间窗，不保存 key、Authorization、prompt、response、thinking 或远端错误正文。
4. 下游只通过 public alias 显式选择；失败由调用方看见，绝不自动切至另一 provider。
5. 用户可阅读 [PROJECT_STRUCTURE.md](PROJECT_STRUCTURE.md)、本路线图、相关 ADR、运维/消费者文档后复现配置与验收，而不依赖旧交接稿。
