# ADR-GATEWAY-CLIENT-CONTRACT-004：受鉴权的 public alias facade

- 状态：Accepted
- 日期：2026-07-31
- 范围：PVIaN 透明网关的下游消费者合同；不改变 Capability Service 或验收 B Runtime 的边界。

## 决策

1. 网关除 `/healthz` 与 `/readyz` 外的所有 API 默认要求
   `Authorization: Bearer <gateway-client-key>`。provider API Key 继续只存在于网关机器，绝不由
   下游持有或经响应/日志/证据回显。
2. client key registry 只版本化 `key_id`、状态和**环境变量名**；密钥值不进 Git。网关启动时只保留
   SHA-256 digest，并以 constant-time 比较验证 bearer。多个 `active` key 可重叠轮换；`revoked`
   key 在其本机环境变量仍存在时明确返回 403。
3. `config/public-model-aliases.json` 是唯一 public alias registry。每个 alias 一对一冻结
   `profile_id + profile_version + endpoint_id + route_id + exact upstream model`。`/v1/models` 只列出
   当前资格 `enabled`、未过期且完整绑定仍匹配的 alias。
4. 公共调用只允许 `model=<public alias>`，无需 `X-PVIAN-Route-ID`。裸 upstream model 若对应多个
   alias，返回 `409 model_is_ambiguous`；不自动选路、fallback、负载均衡或按健康度替换。
5. 保留 `X-PVIAN-Route-ID + exact model` 路径作为内部诊断基准，但它同样要求 Bearer，不能成为
   localhost 鉴权绕过入口。
6. alias facade 只将请求 JSON 的**顶层** `model` 改为绑定的精确 upstream model，并在响应头披露
   `X-PVIAN-Public-Model`、`X-PVIAN-Route-ID` 和 `X-PVIAN-Alias-Facade`。这一层保证字段级语义保真，
   不承诺请求 JSON 原始字节保真；显式 route 路径仍保持请求 body 字节透明。响应 HTTP/SSE body 不改写。

## 后果与边界

- 普通消息、SSE、reasoning、usage、tool call ID/arguments、assistant replay 与 tool-result replay
  的字段必须经过 alias/route/直连差分测试；上游 429/5xx 和 gzip 继续原样透传。
- key registry 改动属于安全配置变更，应先添加新 `active` key、部署其环境变量、验证，再将旧 key
  标记为 `revoked`；不打印、提交或通过命令行参数传递实际密钥。
- 本决策不引入业务权限模型、租户隔离、自动路由、SLA、费用优化、工具执行或 Turn/SSE Runtime。
  公网/跨信任域部署仍需 TLS、反向代理、网络隔离、日志脱敏和独立安全审核。
