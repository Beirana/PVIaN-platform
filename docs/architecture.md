# Architecture

> 这是当前实现的专题架构说明。快速接管、目录责任和请求流先读根目录
> [PROJECT_STRUCTURE.md](../PROJECT_STRUCTURE.md)；当前计划见 [ROADMAP.md](../ROADMAP.md)。

## 边界

PVI Capability Service 是验收 A 的独立组件：

```text
消费者 ──HTTP /v1──> API ──> Application Resolver ──> File Repository
                                                     ├─ data/
                                                     └─ contracts/schemas/

消费者 ──原 SDK/网关/adapter──────────────────────────> 模型端点
```

PVI Capability Service 不在模型调用路径，不持有模型凭证，不自动选择或调用端点。验收 B
Runtime 没有在本仓库实现。透明聚合网关已实现为独立进程：它持有 provider 独立
环境变量、读取脱敏资格状态，并将受 Bearer 鉴权的 public alias 显式绑定到一个精确端点。它不能被误写为
Capability Service 的现有功能，边界见
[`ADR-TRANSPARENT-GATEWAY-003`](../decisions/ADR-TRANSPARENT-GATEWAY-003.md) 与
[`ADR-GATEWAY-CLIENT-CONTRACT-004`](../decisions/ADR-GATEWAY-CLIENT-CONTRACT-004.md)。

```text
当前已实现（仅 localhost / 受控内网）
下游 ──Gateway Bearer Key + public model alias──> PVIaN Gateway ──provider API Key──> 精确上游端点
                                │
                                └──资格状态 / 能力摘要（本地脱敏文件 + 跨进程锁文件）

内部诊断基准（同样需要 Bearer）
下游 ──Gateway Bearer Key + X-PVIAN-Route-ID + exact model──> PVIaN Gateway
```

因此下游不配置 Yunwu/百炼等 provider API Key，只配置网关 Base URL、gateway client key 和唯一
public alias；网关仍不能假定已可对公网或多租户使用。

网关请求路径与外部 `pvi-probe qualify --due` 会共享同一脱敏 qualification state。每个读或
读改写操作都用相邻 `.lock` 文件实现 Windows/POSIX 跨进程互斥，并通过原子 `os.replace` 发布新 JSON；
锁只覆盖本地状态临界区，绝不覆盖 provider 网络请求。部署时 state JSON 与 lock 文件必须位于同一可写、
支持本机文件锁的卷；不支持共享文件锁的多节点存储不属于当前部署合同。

## 分层

- `domain`：冻结枚举与输入输出 DTO；不依赖 FastAPI 或文件系统。
- `application`：确定性筛选和 requirements 分类；无 I/O。
- `infrastructure`：安全加载 JSON/YAML、Draft 2020-12 校验、索引和引用完整性。
- `api`：HTTP 路由、分页和结构化错误。
- `contracts`：对外 Schema/OpenAPI，是跨实现边界的权威合同。

## 启动不变量

- 每个对象满足对应 JSON Schema；
- ID 唯一；
- Capability Profile 引用存在的 Endpoint；
- Profile 的 endpoint kind 与 Endpoint 一致；
- Evidence 与 Probe Run 引用不悬空；
- 数据版本由全部规范化数据的 SHA-256 确定。

任何不变量失败时服务拒绝启动。

## Resolver

1. 只按精确 `profile_id/model/endpoint_id/profile_version` 筛选；
2. 不模糊匹配或推断模型身份；
3. 优先使用能力对象的 `effective`；
4. `unknown/not_tested/inconclusive/字段缺失` 进入 unknown；
5. `unsupported/not_applicable/failed/明确冲突` 进入 unsatisfied；
6. 精确匹配进入 satisfied；
7. 保留同一能力对象的 Evidence IDs；
8. 候选按 endpoint/profile/version 确定性排序。

## 安全

核心 Capability Service 源码没有出站 HTTP 客户端。受控在线 Probe 是独立模块，只有在
显式网络与成本授权下才可访问 provider；外部资料、模型响应和真实用户数据不会由核心
服务获取。fixture locator 采用不透明 `fixture://`，不得写入本机敏感绝对路径、Token 或 Header。

## 扩展

文件仓库可以在保持 HTTP 合同的条件下替换为数据库。未来 Runtime 必须是独立边界，并且请求期间不得依赖 Capability Service 在线。透明网关的请求路径同样不得依赖 Capability Service 在线。
