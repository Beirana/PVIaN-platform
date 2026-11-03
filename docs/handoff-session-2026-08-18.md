# PVIaN 会话交接文档 — 2026-08-18

> 面向新会话/新接手者。上一会话完成了 GLM 暂停、AiGoCode 四模型接入、MiMo Token Plan
> 接入、自部署 local-qwen 接入、五 alias 统一晋升 5200、以及 5239 专属网关的搭建。
> 本文档是唯一需要的入口，配合两份技能（`.agents/skills/pvian-gateway-ops` 与
> `.agents/skills/pvian-model-onboarding`）阅读。

## 0. 新会话必读（按顺序）

1. `.agents/skills/pvian-gateway-ops/SKILL.md` —— 运维边界（5200 是唯一生产实例）
2. `.agents/skills/pvian-model-onboarding/SKILL.md` + `docs/model-extension-runbook.md` —— 接模型的合同
3. `CHANGELOG.md` Unreleased 段 —— 本会话全部变更的编年记录
4. 本文其余部分

铁律：任何 live 调用需 `--allow-network --confirm-cost` 双旗标 + 用户对该批次的明确授权；
密钥值永不打印/落盘/提交；动 5200（重启/晋升/state）必须用户单独授权。

## 1. 当前运行中的服务（双网关并行）

| 实例 | 地址 | PID 文件 | 用途 | alias 数 |
|---|---|---|---|---|
| 生产网关 | `http://127.0.0.1:5200/v1` | `var/gateway/gateway.pid` | 全部云 provider | 18 |
| local-qwen 专属网关 | `http://127.0.0.1:5239/v1` | `var/gateway-local-qwen/gateway.pid` | 自部署 Qwen 专用 | 1 |

两者共用同一把 client key（`PVI_GATEWAY_CLIENT_KEY_INTERNAL_PRIMARY`，查看方式：
`python scripts/gateway_service.py client-key show`）。消费者只需改 base_url。

- 5200 运维：`python scripts/gateway_service.py status|start|stop|restart`
- 5239 运维：`python scripts/gateway_local_qwen_service.py status|start|stop|restart`
  （需 `PYTHONPATH=src`；用 `.venv/Scripts/python.exe`）
- 5239 的 state/账本/日志全在 `var/gateway-local-qwen/`，与生产完全隔离
- 5239 可用性依赖 SSH 隧道（127.0.0.1:18000）；隧道断则网关健康但上游失败，恢复即自愈

## 2. API Key 获取指南（重点）

**所有密钥只存在于 `.env`（已 gitignore）。以下只记「去哪里拿 + 填到哪个变量」。**
状态为「已设置」的不要动；为空的是暂停/未启用项。

| 环境变量（.env） | 状态 | 获取途径 | 用于 |
|---|---|---|---|
| `PVI_PROBE_BAILIAN_API_KEY` | 已设置 | [bailian.console.aliyun.com](https://bailian.console.aliyun.com) → API-KEY 页面 | 百炼按量 9 模型 |
| `PVI_PROBE_DEEPSEEK_API_KEY` | 已设置 | [platform.deepseek.com](https://platform.deepseek.com) → API Keys | DeepSeek 官方 ×2 |
| `PVI_PROBE_MOONSHOT_API_KEY` | 已设置 | [platform.kimi.com](https://platform.kimi.com) 控制台 → API Key | kimi-k3 官方 |
| `PVI_PROBE_OPENROUTER_API_KEY` | 已设置 | [openrouter.ai/keys](https://openrouter.ai/keys) | OpenRouter 免费层 |
| `PVI_PROBE_AIGOCODE_CODEX_1.2_API_KEY` | 已设置 | [www.aigocode.com](https://www.aigocode.com) 控制台 → 令牌页，选 OpenAI/Codex 分组 | gpt-5.6-sol |
| `PVI_PROBE_AIGOCODE_CLAUDE_3.9_API_KEY` | 已设置 | 同上，选 Claude 分组 | claude-sonnet-5 |
| `PVI_PROBE_AIGOCODE_GROK_2.5_API_KEY` | 已设置 | 同上，选 Grok 分组 | grok-4.6 |
| `PVI_PROBE_AIGOCODE_GEMINI_1_API_KEY` | 已设置 | 同上，选 Gemini 分组 | gemini-3.7-flash |
| `PVI_PROBE_MIMO_TOKEN_PLAN_API_KEY` | 已设置 | [platform.xiaomimimo.com](https://platform.xiaomimimo.com) → 控制台 → 订阅/Plan 管理 → Token Plan 页获取专属 `tp-` Key（**订阅到期重购后 Key 会变，需重配**） | mimo-v2.5-pro 订阅 |
| `PVI_PROBE_LOCAL_QWEN_API_KEY` | 固定值 `dummy` | **非密钥**：部署契约规定任意非空串，勿改 | 自部署 vLLM |
| `PVI_PROBE_BIGMODEL_CODING_API_KEY` | **空（暂停项）** | 若恢复：[bigmodel.cn](https://bigmodel.cn) → 个人中心 → 编程套餐 → 套餐概览 → 新建 Key（注意：套餐 Key ≠ 普通智谱 Key） | GLM Coding Plan（未上线） |

### AiGoCode 令牌要点（中转站特例）

- **每把 Key 只能访问对应厂商**（2026-08-18 只读实测：四把 Key 目录零交叉）。
  profile 按 model 绑定对应变量，绝不跨厂商用 Key。
- Base URL 固定 `https://api.aigocode.com/v1`（`PVI_PROBE_AIGOCODE_OPENAI_BASE_URL`）。
- 站点 WAF 按 UA 拦 python-urllib（Cloudflare 1010）；OpenAI-SDK 与 python-httpx UA 放行
  ——网关与探针现有栈无需处理，但若换 HTTP 库需重验。
- 站点价目在控制台登录墙后 → 无 pricing 文件，台账成本估算为 unknown（设计如此，非缺档）。

### 密钥安全规则

- 只写进 `.env`；`.env.example` 只有变量名和注释；测试用非密钥哨兵。
- 轮换 client key：`python scripts/gateway_service.py client-key rotate`（流程见 ops skill）。
- 任何时候不得把值打进命令行参数、日志、报告、证据或提交。

## 3. Provider / alias 全景（2026-08-18 晋升后）

生产 5200 的 18 个可调用 alias：

| 前缀 | 模型 | 说明 |
|---|---|---|
| bailian-cn-beijing ×9 | qwen3.8-max、kimi/kimi-k3、glm-5.2、qwen3.7-plus、qwen3-vl-plus、MiniMax-M3、xiaomi/mimo-v2.5-pro、deepseek-v4-pro/flash | 按量付费老批次 |
| deepseek-official ×2 | deepseek-v4-pro/flash | 官方直连 |
| moonshot-official ×1 | kimi-k3 | 官方直连 |
| openrouter ×1 | dots-studio/dots-3-note-preview:free | 免费层 |
| aigocode ×4 | gpt-5.6-sol、claude-sonnet-5、gemini-3.7-flash、grok-4.6 | 中转站（v0.1.1 放宽：enablement 仅要求 message+usage，见 §5） |
| mimo-token-plan ×1 | mimo-v2.5-pro | 订阅制（ToS 灰区已由操作员知情接受） |
| local-qwen ×1（在 5239） | qwen3.8-27b-fp8 | 自部署 vLLM，overlay 独立实例 |

TTL：aigocode/mimo/local-qwen 各路由 enabled 至 **2026-08-25**；Bailian 由每日 04:00
任务例行刷新。

## 4. 悬而未决事项（按优先级）

1. **requalify 预算（08-25 前必须处理）**：aigocode/mimo 未加入
   `config/requalification-budgets.json`，每日任务不刷新它们，过期 alias 会消失。
   二选一：手动跑
   `PVI_GATEWAY_STATE_FILE=var/gateway/qualification-state-flash-0731.json python -m pvi.probe_runner.cli qualify --profile-id <id> --allow-network --confirm-cost`
   （需用户授权探针次数），或请用户批准预算条目。local-qwen 的 5239 overlay state
   同理（重qualify 后需重建 overlay 并重启 5239）。
2. **5239 开机自启未注册**：本机注册计划任务需提权，定义文件已备好
   `deploy/pvian-localqwen-at-logon.xml`，等用户在提权命令行执行一次：
   `schtasks /Create /TN "PVIaN-LocalQwen-Gateway-AtLogon" /XML deploy\pvian-localqwen-at-logon.xml /F`
3. **全部工作未提交**：working tree 有大量改动（本会话五个批次），等用户确认后按
   CHANGELOG 的分批次 commit。
4. **GLM Coding Plan 暂停**：离线 candidate 档案保留（profile/endpoint/cap/evidence/
   token-limits/隐藏 alias），恢复需用户提供套餐 Key + 决定是否接受「限编码工具」ToS。
5. **可选**：local-qwen 是否将来并入 5200（独立授权窗口）；AiGoCode 台账若想要真实
   成本估算，需用户从控制台导出价目建 pricing 文件，或立项「cost_in_usd_ticks 透传」。

## 5. 关键技术事实（避免新会话踩坑）

- **推理透传方言（已定论）**：OpenAI Chat Completions 协议无推理槽位；AiGoCode 的
  `/v1/responses` 是不透推理的转译垫片（3 探针定性）；Claude thinking 块与 Gemini
  thought summaries 均为原生 API 特性。**可见推理的只有 grok（reasoning_content）、
  mimo（reasoning_content）、local-qwen（`message.reasoning`，注意字段名不同）、
  qwen3.8/kimi/glm 系（enable_thinking 方言）**。aigocode 的 sol/sonnet/gemini 三个
  profile 因此做过 v0.1.1 放宽（审计链：profile 字段+版本、alias 同步、CHANGELOG、
  cap 0.2.1 notes、git 历史）。
- **订阅制/自部署无逐 token 牌价** → 不建 pricing 文件，ledger 估算 unknown
  （`pricing_missing`），这是诚实表示；勿伪造 0 价。
- **回环 http 例外（本会话唯一网关代码变更）**：`profiles.py` 与
  `pvi/probe_runner/config.py` 允许 127.0.0.1/::1/localhost 用 http+显式端口；
  外部 host 一律 https/443。有单测锁定（test_gateway_profiles_and_state.py）。
- **qualify 不支持 --state-file**：会写 `.env` 里 `PVI_GATEWAY_STATE_FILE` 指向的文件。
  隔离资质探针必须用环境变量覆盖该路径（`PVI_GATEWAY_STATE_FILE=<隔离文件>`，
  load_dotenv(override=False) 不覆盖已存在的进程变量）。
- **账本路径由 state 文件目录派生**（`state.py:27` `with_name("usage-attempt-ledger.json")`），
  独立 state 目录天然获得独立账本。
- **AiGoCode 模型代际**：站点目录是 gpt-5.6/opus-5/3.7-flash 时代，故意与 OpenRouter
  目录身份不同，禁止跨渠道映射同名模型。
- 一次瞬时现象备忘：5200 在承载真实请求时，外部 status 探测可能瞬时超时（2026-08-18
  出现一次，直接连测 3/3 恢复 200），先复核再报警。

## 6. 文件与目录地图

| 路径 | 内容 |
|---|---|
| `var/gateway/` | 生产 state（qualification-state-flash-0731.json）、账本、PID；晋升前备份 `*.pre-aigocode-mimo-20260818.bak` |
| `var/gateway-local-qwen/` | 5239 专属：overlay profiles/aliases/state/账本/日志/PID |
| `.codex_tmp/aigocode-live-20260818/` | aigocode 资质探针隔离 state 与日志 |
| `.codex_tmp/mimo-live-20260818/` | mimo 同上 |
| `.codex_tmp/local-qwen-live-20260818/` | local-qwen 同上 |
| `data/evidence-artifacts/gateway-forwarder-smoke-*-20260818.json` | 四条 forwarder smoke 证据 |
| `scripts/gateway_local_qwen_service.py` | 5239 服务管理（新） |
| `scripts/gateway_isolated_{aigocode,mimo,local_qwen}.py` | 隔离 smoke 批脚本（新） |
| `scripts/smoke_provider_forwarder.py` | forwarder smoke（新增 5 个 provider-set） |
| `deploy/pvian-localqwen-at-logon.xml` | 5239 自启任务定义（待用户导入） |
| `docs/reviews/` + `CHANGELOG.md` | 既有交付记录 + 本会话编年 |

## 7. 常用命令速查

```powershell
# 双网关状态
python scripts/gateway_service.py status
python scripts/gateway_local_qwen_service.py status   # 需 PYTHONPATH=src + venv python

# 生产重qualify（到期路由；预算内）
python scripts/gateway_service.py requalify --dry-run
python scripts/gateway_service.py requalify

# 离线测试
$env:PVI_NETWORK_DISABLED='1'; python -m pytest tests/unit tests/contract -q
python scripts/validate_data.py

# 隔离资质探针（示例：勿写生产 state）
$env:PVI_GATEWAY_STATE_FILE='<隔离路径>'
python -m pvi.probe_runner.cli qualify --profile-id <exact-id> --allow-network --confirm-cost
```
