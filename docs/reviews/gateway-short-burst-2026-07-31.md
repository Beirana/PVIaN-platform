# 透明网关真实短 Burst 观察（2026-07-31）

> 观察范围：修复 gzip 传输合同后的临时本机网关；每个显式 route 5 次请求、无重试、
> 普通 httpx 默认 `Accept-Encoding`、`reasoning_effort=high`。
> 此记录不包含 API Key、原始 prompt/response/reasoning 或远端错误正文。

## 结果摘要

| route_id | HTTP 结果 | 可解码 200 | 429 / 5xx / transport | 延迟 min / median / max | 当前短窗判断 |
|---|---:|---:|---:|---:|---|
| `yunwu:deepseek-v4-pro` | 3/5 HTTP 200 | 3/3 | 2 / 0 / 0 | 2850 / 5234 / 16249 ms | 观察到真实 rate limit；成功响应的 model/content/reasoning 均通过。 |
| `bailian-cn-beijing:deepseek-v4-pro` | 5/5 HTTP 200 | 5/5 | 0 / 0 / 0 | 1838 / 1868 / 1938 ms | 当前短窗稳定；model/content/reasoning 均通过。 |

gzip 修复已通过真实普通 httpx 调用验证：调用方未显式设置 `Accept-Encoding: identity`，所有
HTTP 200 响应仍可正常 JSON 解码。临时网关实例已停止。

## 解释与操作边界

- 这是时间特定、每 route 仅 5 个样本的观察；不是可用性承诺、SLA 或长期性能结论。
- Yunwu 的两次 429 是真实 `rate_limited` 观测，不是“模型不支持”；后续成功使连续失败计数
  回到 0，`operational_status` 维持 `nominal`，符合五连败才提前复核的策略。
- 当前人工选择的真实运行首选为 `bailian-cn-beijing:deepseek-v4-pro`；
  `yunwu:deepseek-v4-pro` 保留为显式备用/对照 route。网关不得据此自动 fallback、负载均衡或
  根据健康度替换上游。
- 下次周期资格复核与后续 burst 必须继续记录观测时间、配置摘要、样本数、状态/错误分类、
  延迟与脱敏 usage；不得保存原始内容。
