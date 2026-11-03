# 历史资料索引

这里保存有审计、发布或研究价值，但**不是当前架构、当前规划或当前消费者合同权威来源**的材料。
当前接管请优先阅读根目录的 [PROJECT_STRUCTURE.md](../../PROJECT_STRUCTURE.md) 与
[ROADMAP.md](../../ROADMAP.md)。现行细节以 [架构说明](../architecture.md)、ADR、
[v0.5.0 冻结说明](../releases/v0.5.0-internal.md)、运维和消费者说明为准。

| 类别 | 材料 | 归档原因 |
|---|---|---|
| foundation | [PVI-00 正式交接文档](foundation/PVI-00-供应商能力信任调用与规范化平台-正式交接文档.md) | 冻结设计背景，含尚未实施的验收 B 与早期方案，不说明 v0.5 当前状态 |
| plans | [初期实施方案](plans/IMPLEMENTATION_PLAN.md) | PVI-001～014 与初期仓库建设指令已被实际实现和 ROADMAP 取代 |
| transition | [透明网关 P0 迁移登记](transition/gateway-priority-and-migration-register.md) | P0 过渡记录；其后续事项已收敛到当前 ROADMAP |
| reviews | [执行报告](reviews/execution-report.md)、[技术审核](reviews/technical-review.md)、[独立接手审核](reviews/independent-handoff-review.md) | 验收 A 阶段快照与 blocked 结论，保留审计价值但不作为当前总状态 |
| research | [复用调研](research/reuse-landscape.md) | 早期候选调研；实际选择以 ADR 和后续保真证据为准 |
| releases | [v0.4.0rc1 测试冻结说明](releases/v0.4.0rc1-internal.md) | rc1 中间测试态（无 Bearer/alias），已被 v0.5.0 完全取代 |

发布冻结说明与 ADR 继续保留在原位置：它们是可追溯决定和版本证据，不应因瘦身而丢失。v0.4.0rc1 因其描述的
无鉴权状态已被 v0.5.0 取代而移入归档；v0.5.0 及之后的冻结说明仍在 `docs/releases/`。所有在线观察仍仅代表
精确端点、模型、配置和时间窗，不能作为 SLA。
