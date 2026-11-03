# 网关基础设施值班交接手册

> 本文档面向**接手网关基础设施侧工作**的人(无论是不是我/同一个会话)。
> 它回答三个问题:**我该做什么**、**怎么确认网关是健康的**、**下游分支报错时我该怎么排查**。
>
> 相关文档:[`gateway-standard-runbook.md`](gateway-standard-runbook.md)(消费者调用流程)、
> [`gateway-operator-guide.md`](gateway-operator-guide.md)(provider/qualification 深度运维)。
> 本文不重复它们,只补"基础设施值班"这个视角。

## 1. 角色边界

| 角色 | 职责 | 不做什么 |
|---|---|---|
| **基础设施侧(本手册读者)** | 用 launcher 起网关、保证 key/超时/state 正确、出问题时排查网关↔上游链路、把 base url + client key 交给下游 | 不发实验请求、不碰实验侧代码、不改实验侧 state |
| **实验侧(S 分支 / P 分支等)** | 拿到 base url + client key 后发请求、看报错;碰到 401/502/超时不自己猜根因,把请求 id + 时间戳 + 错误码报给基础设施侧 | 不自己起网关、不直接读 `.env` / provider key、不动网关进程 |

关键:网关**只有一个**,由基础设施侧统一拉起。下游分支**不要各自起网关**——这是本次多个故障的根源。

## 2. 唯一正确的启动方式:`scripts/launch_gateway.py`

**不要手动 `pvian-gateway.exe` 或 `uvicorn` 起网关。** 手动启动踩过两次坑(见 §4)。永远用 launcher:

```bash
# 标准启动(前台运行,占 5200,Ctrl+C 停)
python scripts/launch_gateway.py

# 自检后立即关闭(验证用,不留进程)
python scripts/launch_gateway.py --no-keep

# 指定端口/state
python scripts/launch_gateway.py --port 5200 \
  --state-file var/gateway/qualification-state-flash-0731.json
```

launcher 做了四件手动启动做不到的事:
1. **`load_dotenv(override=True)`** —— 强制 `.env` 的 provider key 覆盖 shell 里的空/旧值(修复 §4.2 的 401 根因)。
2. **启动前直连上游预检** —— 用真实 provider key 打一发 chat/completions,200 才继续(现有 launcher 只查 alias 可见性,查不出 key 失效)。
3. **走 `pvian-gateway.exe` 正确入口** —— 确保 `main()` 的 `load_dotenv` 执行。
4. **启动后端到端自检** —— 通过网关发一发,200 才宣布 ready。

启动成功后,launcher 会打印三样东西(见 §3),交给下游分支。

## 3. 每次启动后,必须交接给下游的信息

launcher 成功会打印:
```
GATEWAY READY AND VERIFIED
  base url : http://127.0.0.1:5200
  client key (give this to the consumer): <稳定 key,来自 .env 的 PVI_GATEWAY_CLIENT_KEY_INTERNAL_PRIMARY>
  alias    : pvian/bailian-cn-beijing/deepseek-v4-flash-0731
```

把这三行**原样**发给下游分支。他们要设的环境变量:
```
PVIAN_GATEWAY_BASE_URL=http://127.0.0.1:5200
PVIAN_CLIENT_KEY=<上面的 client key>
PVIAN_MODEL=pvian/bailian-cn-beijing/deepseek-v4-flash-0731
```

**注意**:client key 现在是**稳定的**,持久化在 `.env` 的 `PVI_GATEWAY_CLIENT_KEY_INTERNAL_PRIMARY`(由 launcher 复用,不再每次重启随机生成)。网关重启后 key 不变,下游不必跟着换。这一设计决定的背景见
[`adr/0002-stable-client-key-via-env-reuse.md`](adr/0002-stable-client-key-via-env-reuse.md)。要查看当前 key:

```bash
python scripts/gateway_service.py client-key show
```

(任务 B 之前 key 确实是每次启动随机生成的一次性 key,现已过时。)

## 4. 已知故障与根因(本次调试踩过的坑)

### 4.1 502 upstream_transport_error(其实是超时,不是上游挂了)

**症状**:某条高推理负载 call 8/8 失败,同批次别的全过。错误码 `upstream_transport_error`。
**根因**:网关上游超时(原默认 60s)比该 call 的实际生成时间(143-200s)短,`httpx.ReadTimeout` 被归成 transport error 返回 502。**名字有误导——上游没挂,是网关到点掐了连接。**
**已修复**:`config.py` 默认 `upstream_timeout_seconds` 改为 300s。判别证据:`.tmp_gateway_diag/issue_report_s_sb_002_judgment_addendum.md`。
**识别特征**:失败耗时 ≈ 超时阈值(60s 或 300s)的整数倍边界。

### 4.2 401 invalid_client_api_key(provider key 没对上)

**症状**:大批量(如 16/18)call 撞 401,熔断器没跳(consecutive_failures=0)。错误码 `invalid_client_api_key`。
**根因**:网关读 provider key 用 `os.environ.get("PVI_PROBE_BAILIAN_API_KEY")`(`profiles.py:121`),而 `main.py:524` 的 `load_dotenv(..., override=False)` 有个坑——**shell 里即使是空字符串,`.env` 真值也不会覆盖**。如果起网关的 shell 该变量是空/旧值,网关就用错 key 打上游 → 401。
**关键**:`.env` 里的 key 本身通常是有效的(用 §5.2 的直连预检验证)。问题在"网关进程启动时环境没对上",不在 key 本身。
**已修复**:launcher 用 `override=True` + 启动前预检。
**识别特征**:大批量 401 且熔断器不跳(401 不计入 consecutive_failures)。

### 4.3 多个网关实例并存(端口冲突 / 指向错误实例)

**症状**:下游报错时好时坏,或连到的网关行为不一致。
**根因**:不同分支/会话各自起了网关,占不同端口或抢同一端口。下游可能连到 key 没对上的那个。
**处理**:基础设施侧只维护**一个**官方网关实例。§5.4 的排查命令能列出所有监听端口。

## 5. 故障排查 Playbook(下游报错时按这个走)

### 5.1 第一步:看错误码,分流

| 错误码 | 指向 | 下一步 |
|---|---|---|
| 401 `invalid_client_api_key` | provider key 没对上(§4.2) | §5.2 预检 key;若 key 有效则重启网关(launcher) |
| 502 `upstream_transport_error` 且耗时≈超时 | 超时(§4.1) | 确认 `PVI_GATEWAY_UPSTREAM_TIMEOUT_SECONDS`;若已 300 还失败,看 §5.3 |
| 502 `upstream_transport_error` 且秒级失败 | 连接层(代理/key/上游拒连) | §5.2 + §5.3 |
| 409 `alias_not_callable` / `route_not_callable` | route degraded/过期 | 查 state(§5.5),重置 consecutive_failures |
| 连接被拒 / 端口不通 | 网关没起或端口错 | §5.4 确认实例,用 launcher 重启 |

### 5.2 直连上游预检 provider key(最常用)

绕过网关,直接用 `.env` 的 key 打上游。**这一步能一锤定音区分"key 坏了"还是"网关没加载到 key":**
```bash
.venv/Scripts/python.exe -c "
import json, time, urllib.request, re
from pathlib import Path
e = Path('.env').read_text(encoding='utf-8')
key = re.search(r'^PVI_PROBE_BAILIAN_API_KEY\s*=\s*(.+)\$', e, re.M).group(1).strip().strip('\"')
base = re.search(r'^PVI_PROBE_BAILIAN_OPENAI_BASE_URL\s*=\s*(.+)\$', e, re.M).group(1).strip().strip('\"').rstrip('/')
body = json.dumps({'model':'deepseek-v4-flash-0731','messages':[{'role':'user','content':'reply OK'}],'max_tokens':16,'enable_thinking':False}).encode()
req = urllib.request.Request(base+'/chat/completions', data=body, headers={'Authorization':'Bearer '+key,'Content-Type':'application/json'})
opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))  # 不走代理,直连 fake-IP/TUN
t=time.perf_counter()
try:
    with opener.open(req, timeout=60) as r: print('UPSTREAM OK', r.status, round(time.perf_counter()-t,2),'s')
except urllib.error.HTTPError as ex: print('UPSTREAM HTTP', ex.code, ex.read()[:200])
except Exception as ex: print('UPSTREAM FAIL', type(ex).__name__, str(ex)[:200])
"
```
- **200** → key 有效,问题在网关没加载到它 → 用 launcher 重启。
- **401/403** → key 真的失效了 → 需要换 key(联系 provider,不在本手册范围)。
- **超时/连接拒绝** → 网络层(TUN/代理/上游不可达)→ §5.3。

### 5.3 检查网络层(fake-IP / TUN / 代理)

DNS 解析到 `198.18.x.x` 是 **正常的**(TUN 模式工作的标志,不是故障)。直连该 IP 应能在 ~0.2s 内拿到 HTTP 响应(404/401 都算"通了"):
```bash
.venv/Scripts/python.exe -c "
import socket, urllib.request
h='llm-example.cn-beijing.maas.aliyuncs.com'
print('DNS ->', set(i[4][0] for i in socket.getaddrinfo(h,443,type=socket.SOCK_STREAM)))
opener=urllib.request.build_opener(urllib.request.ProxyHandler({}))
try:
    with opener.open('https://'+h+'/', timeout=5) as r: print('direct GET / ->', r.status)
except urllib.error.HTTPError as e: print('direct GET / -> HTTP', e.code, '(reachable)')
except Exception as e: print('direct FAIL', type(e).__name__, str(e)[:100])
"
```
若直连超时/拒绝 → TUN/代理层问题,需要重启代理或检查系统网络(不在网关代码范围)。

### 5.4 确认有几个网关实例在跑

```bash
# 所有候选端口
netstat -ano | grep LISTENING | grep -E ":(5200|5213|5214|5215|5216)\s"
```
只该有一个(官方实例)。多出来的,联系对应分支停掉,或基础设施侧统一到一个端口。

### 5.5 查/重置 qualification state

```bash
# 查 route 当前状态
.venv/Scripts/python.exe -c "
import json
d=json.load(open('var/gateway/qualification-state-flash-0731.json',encoding='utf-8'))
r=d['records']['bailian-cn-beijing:deepseek-v4-flash-0731']
print({k:r.get(k) for k in ['operational_status','qualification_status','consecutive_failures','consecutive_failure_threshold','last_failure_category','valid_until','revalidation_due']})
"

# 若 degraded(被失败拖累),重置这条 route(consecutive_failures 清零)
.venv/Scripts/python.exe -c "
import json
from pathlib import Path
p=Path('var/gateway/qualification-state-flash-0731.json')
d=json.loads(p.read_text(encoding='utf-8'))
r=d['records']['bailian-cn-beijing:deepseek-v4-flash-0731']
r['consecutive_failures']=0; r['operational_status']='nominal'; r['revalidation_due']=False
r['last_failure_category']=None; r['last_failure_at']=None
p.write_text(json.dumps(d,ensure_ascii=False,indent=2),encoding='utf-8')
print('route reset to nominal')
"
```
重置后要**重启网关**才生效(网关启动时加载 state)。

## 6. 重启网关的标准流程

长期常驻模式下,标准重启命令是:

```bash
python scripts/gateway_service.py restart
```

它内部就是"stop 旧实例 → 用 launcher 起 → 跑启动后自检"一条龙。成功后下游无需重取 client key(key 已稳定,见 §3)。

手动 netstat + taskkill + launcher 的老流程**已废弃**(容易杀错 PID、漏掉 uvicorn worker、忘记跑自检),只有在 `gateway_service.py` 本身不可用时才回退到 §6.1 的手动步骤。

### 6.1 回退:手动 netstat + taskkill(`gateway_service.py` 不可用时)

1. **确认要停的网关 PID**(避免杀错):`netstat -ano | grep :5200` 拿 PID。
2. **停掉旧网关**。若 `Stop-Process`/`taskkill` 报"拒绝访问",说明它由更高权限的会话起的,需要管理员 PowerShell 执行:
   ```powershell
   Stop-Process -Id <PID>,<子进程PID> -Force
   ```
   注意:`pvian-gateway.exe` 会 spawn 独立的 uvicorn worker,可能要杀两个 PID(exe + worker)。
3. **用 launcher 起**:`python scripts/launch_gateway.py --port 5200`。
4. **看 launcher 输出**:四步全绿(预检 200 + 端到端 200)才算成功。
5. **把打印的 base url + client key + alias 交给下游分支**(§3)。

## 7. 长期常驻服务(2026-08-10 工单任务 C/D)

网关现在以**长期常驻服务**方式运行:登录自启、定时复核、稳定 key,下游不必每次找值班人重启。本节是常驻模式的运维速查。

### 7.1 两个 Task Scheduler 任务

| 任务名 | 触发 | 干什么 | 关键参数 |
|---|---|---|---|
| `PVIaN-Gateway-AtLogon` | 用户登录时 | 用 `scripts/gateway_start.bat` → `gateway_service.py start` 拉起网关 | `ExecutionTimeLimit=PT0S`(不限时)、`MultipleInstances=IgnoreNew`(已起就不重复起) |
| `PVIaN-Requalify-Daily` | 每日 04:00 | 跑 `scripts/requalify_due.py`,对到期 route 自动复核续期 | TTL 7 天(168h);预算由 `config/requalification-budgets.json` 冻结,超支即停(预算模型见 [`adr/0001-automatic-requalification-budget-model.md`](adr/0001-automatic-requalification-budget-model.md)) |

查看 / 手动触发任务(管理员 PowerShell):

```powershell
# 看任务状态
Get-ScheduledTask -TaskName 'PVIaN-Gateway-AtLogon','PVIaN-Requalify-Daily' | `
  Select-Object TaskName, State, @{n='LastRun';e={(Get-ScheduledTaskInfo $_).LastRunTime}}

# 手动触发登录自启
Start-ScheduledTask -TaskName 'PVIaN-Gateway-AtLogon'

# 手动触发当日复核
Start-ScheduledTask -TaskName 'PVIaN-Requalify-Daily'
```

### 7.2 日志与运行时文件位置

统一在 `var/gateway/` 下:
- 网关 PID:`gateway.pid`
- qualification state:`qualification-state-flash-0731.json`(统一 state 文件,TTL 7 天)
- usage 台账:`usage-attempt-ledger.json`
- 复核失败计数器:`requalify-consecutive-failures.json`(`requalify_due.py` 维护)
- 冻结预算:`config/requalification-budgets.json`
- 价格表:`config/pricing/*.json`(版本化 list price,只用于核算)
- 模型 token 上限:`config/model-token-limits.json`(advertised 最大输出/上下文/思维链上限 +
  默认推理行为 + `recommended_defaults` 请求配方,注入价格快照端点的 `output_limits`;与价格同
  hot-reload,改完无需重启。设计决策见
  [`adr/0003-model-default-thinking-profile.md`](adr/0003-model-default-thinking-profile.md))

### 7.3 手动运维的标准命令

日常操作全部走 `scripts/gateway_service.py`,不要再手敲 netstat/taskkill:

```bash
python scripts/gateway_service.py start        # 启动(已起则跳过)
python scripts/gateway_service.py stop         # 停止
python scripts/gateway_service.py restart      # 重启(见 §6)
python scripts/gateway_service.py status       # 看健康/TTL/key 状态
python scripts/gateway_service.py client-key show   # 看当前稳定 key(见 §3)
python scripts/gateway_service.py requalify         # 手动跑一次 requalify_due.py
python scripts/gateway_service.py ledger size       # 看 usage 台账大小
python scripts/gateway_service.py ledger archive    # 归档并新开一本台账
```

### 7.4 故障排查补充(常驻模式特有)

- **网关崩溃后不会自动拉起。** Task Scheduler 的 `RestartOnFailure`/restart-on-failure 在 Windows 上不可靠,**两个任务都没配看门狗重启**。发现网关挂了(`status` 报未运行,或下游报连接被拒)→ 手动 `python scripts/gateway_service.py restart`,或 `Start-ScheduledTask -TaskName 'PVIaN-Gateway-AtLogon'`。
- **复核没跑成。** 先看 `Get-ScheduledTaskInfo PVIaN-Requalify-Daily` 的 `LastRunTime`/`LastTaskResult`;再看 `var/gateway/requalify-consecutive-failures.json` 是否被失败拖到熔断;`config/requalification-budgets.json` 是否超支被叫停。
- **route 提前 degraded。** 走 §5.5 重置 consecutive_failures,然后 `gateway_service.py restart` 让网关重新加载 state。
- **本节之外的故障**(401/502/超时/多实例)仍按 §4 根因和 §5 Playbook 排查,与常驻与否无关。

## 8. 给接手者的检查清单

接手时先确认这几件事:
- [ ] `config.py:27` 的 `upstream_timeout_seconds` 是 300.0(不是 60)
- [ ] `scripts/launch_gateway.py` 存在且 smoke 通过(`--no-keep`)
- [ ] `scripts/gateway_service.py` 存在且 `status` 能跑通(常驻模式入口,见 §7)
- [ ] 两个 Task Scheduler 任务 `PVIaN-Gateway-AtLogon` / `PVIaN-Requalify-Daily` 处于 Ready 状态(§7.1)
- [ ] `.env` 的 `PVI_GATEWAY_CLIENT_KEY_INTERNAL_PRIMARY` 存在且 `gateway_service.py client-key show` 能读到(稳定 key,见 §3)
- [ ] 当前只有一个网关实例(§5.4),且是用 launcher / `gateway_service.py` 起的(不是手动 uvicorn)
- [ ] `.env` 的 provider key 直连上游 = 200(§5.2)
- [ ] `GET /v1/gateway/pricing/snapshots` 返回的快照含 `output_limits` 块(确认 token 上限已加载;
  缺失说明网关跑的是旧代码,需 `restart`;数字来源见
  [`reviews/bailian-max-output-token-audit-2026-08-13.md`](reviews/bailian-max-output-token-audit-2026-08-13.md))

任何一项不满足,按对应章节修。

---

*文档建立:2026-08-08。背景:本日 S/P 两个分支先后撞上 401(§4.2)和 502(§4.1),根因都是"网关怎么起的"。本手册把排查经验固化,避免重蹈。诊断证据见 `loss_of_control_fault_graph_experiments/.tmp_gateway_diag/` 下的 issue report + judgment addendum。*
