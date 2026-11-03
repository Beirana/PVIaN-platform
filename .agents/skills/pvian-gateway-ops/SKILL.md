---
name: pvian-gateway-ops
description: Operate the PVIaN transparent gateway as a long-running local service — start/stop/restart, check health and TTL, requalify expiring routes, manage the stable client key, archive the usage ledger, and troubleshoot 401/502/409/multi-instance failures. Use when the gateway is down or unhealthy, when aliases disappear, when a consumer needs the client key, when logs or state need inspection, or when restarting after a reboot or crash. Also use BEFORE touching gateway config, profiles, state files, or Task Scheduler tasks.
---

# PVIaN Gateway Operations

The gateway is a family of local long-running services. The production instance
is `127.0.0.1:5200` (auto-starts at user logon via Windows Task Scheduler);
`127.0.0.1:5245` is a dedicated instance for the aliyun-token-plan routes
(manual lifecycle, operator-protected: renew its state without touching the
process). They are **not** cloud services — they run on this machine and must
never be exposed publicly.

## Ground rules (read first)

1. **Never bypass the launcher.** The only correct way to start the gateway is
   `scripts/launch_gateway.py` (directly or via `gateway_service.py`). Do not
   run `pvian-gateway.exe` or `uvicorn` manually — the launcher does provider-key
   preflight and an end-to-end self-check that catches 401/502 before consumers
   see them.
2. **Provider keys stay in `.env`.** Never print, log, commit, or forward
   `PVI_PROBE_BAILIAN_API_KEY` or any provider credential. The client key
   (`PVI_GATEWAY_CLIENT_KEY_INTERNAL_PRIMARY`) is also secret but is meant to be
   shared with downstream consumers on this machine only.
3. **One production instance per port.** Port 5200 is the only production
   gateway; 5245 is the dedicated aliyun-token-plan instance (started manually
   with env pointing at the overlay in `.codex_tmp/gateway-5245-aliyun-token-plan/`;
   never restart it casually — renew it in place, see Requalification). Do not
   start a second listener on either port. Isolated smoke tests must use a
   different port (see pvian-model-onboarding skill). The local-qwen family
   (5239/5240/5242 + SSH tunnels 18000-18003) is currently stopped but fully
   recoverable: start the tunnel, then `scripts/gateway_local_qwen*_service.py start`.
4. **Qualification state is shared.** The gateway and the requalifier read/write
   the same file (`var/gateway/qualification-state-flash-0731.json`). The path is
   pinned in `.env` as `PVI_GATEWAY_STATE_FILE` — do not change it without
   updating `.env`, `.env.example`, and the launcher default in lockstep.
5. **Changing `validity_hours` requires requalification.** If you change TTL in
   profiles or `profiles.py`, you MUST requalify before restarting the gateway,
   otherwise the binding validator (`state.py:1069`) will crash on startup. See
   "Change TTL" below.

## Quick reference — the one command

```bash
python scripts/gateway_service.py status
```

This single command shows: process state (PID), `/healthz`, `/readyz`,
`/v1/models` callable aliases, per-route TTL remaining, client-key presence, and
usage-ledger size. **Run this first** whenever you need to know if the gateway is
healthy.

## Daily operations

### Check if the gateway is running

```bash
python scripts/gateway_service.py status
```

Exit code 0 = running, 1 = not running. The output includes health endpoints,
callable aliases, TTL hours remaining, and the ledger size.

### Start the gateway

```bash
python scripts/gateway_service.py start
```

Starts via the launcher (preflight + self-check). If already running, reports the
existing PID and exits 0. The started process is detached — it survives terminal
closure.

For auto-start at logon, the Task Scheduler task `PVIaN-Gateway-AtLogon` handles
this automatically (see "Task Scheduler" below).

### Stop the gateway

```bash
python scripts/gateway_service.py stop
```

Kills the process tree (launcher + uvicorn worker). Clears the PID file.

### Restart

```bash
python scripts/gateway_service.py restart
```

Stop + 2s wait + start. Use after editing `.env`, profiles, or gateway code. Note:
editing `config/pricing/*.json` or `config/model-token-limits.json` (data only) hot-reloads
on next pricing-snapshot query — **no restart needed**; only code or profile changes need a restart.

### Get the client key for a consumer

```bash
python scripts/gateway_service.py client-key show
```

Prints the stable key from `.env`. Give this to downstream projects as
`PVIAN_CLIENT_KEY` (or `Authorization: Bearer <key>`). The key is stable across
restarts — consumers do not need to update it unless you rotate (see below).

## API surfaces (production 5200)

- **OpenAI-compatible**: `POST /v1/chat/completions`, `GET /v1/models` — 18
  callable aliases (`pvian/<provider>/<model>`).
- **Anthropic-native passthrough** (ADR 0004, zero protocol translation):
  `POST /v1/messages` and `POST /v1/messages/count_tokens` for
  `anthropic_native_messages` routes (currently
  `pvian/aigocode-anthropic/claude-sonnet-5` — full Claude tool loops and
  thinking blocks). Consumers (Claude Code / CCSwitch / Anthropic SDK) set:
  `ANTHROPIC_BASE_URL=http://127.0.0.1:5200` and
  `ANTHROPIC_AUTH_TOKEN=<client key>` (AUTH_TOKEN, not API_KEY — the gateway
  authenticates `Authorization: Bearer`). An OpenAI-surface alias sent to
  `/v1/messages` (or the reverse) fails closed with 409.
  Note: `count_tokens` transparently returns the station's upstream 404 — the
  aigocode Anthropic face does not implement that endpoint; Claude Code
  tolerates it.

### Rotate the client key

```bash
python scripts/gateway_service.py client-key rotate
```

Generates a new key, writes it to `.env`, and prompts you to restart. After
restart, old key stops working immediately. Downstream consumers must update.

For zero-downtime rotation (old key keeps working during transition), add a
second active entry to `config/gateway-client-keys.json` pointing to a different
env var (e.g. `PVI_GATEWAY_CLIENT_KEY_INTERNAL_SECONDARY`), set both values in
`.env`, restart, let consumers switch, then revoke the old one.

## Requalification (keeping aliases alive)

Aliases stay callable while their qualification TTL is valid (**7 days**, 168h).
The daily Task Scheduler job (`PVIaN-Requalify-Daily`, 04:00) runs
`scripts/requalify_due.py`, which is **three passes**:

1. **Production pass** — `pvi-probe qualify --due` against the production state.
2. **5245 overlay pass** — `scripts/requalify_aliyun_5245.py`: qualifies into a
   temp state, rewrites profile versions to the overlay suffix, atomically
   swaps the overlay state file. The running 5245 process picks it up on the
   next request — **never restart or stop that process for renewal**.
3. **Anthropic pass** — `scripts/qualify_anthropic_surface.py` renews
   `anthropic_native_messages` routes (the OpenAI qualifier skips that
   transport) by writing directly into the production state.

Renewal semantics (ADR 0001 as amended 2026-08-20): a route is **due once its
remaining TTL enters the final 24 hours** (or it is expired/degraded), and a
successful pass refreshes it to a fresh 168h — so aliases renew *before* they
disappear. Every due route must have a frozen budget entry in
`config/requalification-budgets.json` (two classes: priced CNY ceilings, and
null-cost subscription/relay entries where the frozen call count IS the
budget); a missing entry aborts the whole run fail-closed.

### Check what is due

```bash
python scripts/gateway_service.py requalify --dry-run
```

Shows due routes (24h window included) without making any upstream calls.

### Run requalification manually

```bash
python scripts/gateway_service.py requalify
```

Runs the same three-pass scheduler with dual live flags. Cost is bounded by the
frozen budgets.

**Important:** requalification writes to the state file(s), and the gateways
read those files on every request (no cache, no restart needed). A successfully
requalified alias is callable immediately. Requalification does NOT require the
gateway process to be running — only the state files and upstream reachability.

### Full requalification (not --due)

If you changed `validity_hours` or need to requalize everything:

```bash
python -m pvi.probe_runner.cli qualify \
  --profile-id provider-bailian-cn-beijing \
  --profile-id provider-bailian-cn-beepseek-v4-flash-0731 \
  --allow-network --confirm-cost
```

**Never use `--due` after changing `validity_hours`** — `callable_record` does not
check validity_hours consistency, so `--due` will skip still-valid routes and
leave stale records that crash the gateway on restart.

## Troubleshooting

### Gateway not responding / connection refused

1. `python scripts/gateway_service.py status` — is it running?
2. If not: `python scripts/gateway_service.py start`
3. If start fails, check the launcher output:
   ```bash
   python scripts/launch_gateway.py --no-keep
   ```
   This runs start + verify + stop in the foreground. Errors are printed.

### 401 `invalid_client_api_key` (provider key)

The provider key in `.env` is rejected by the upstream. This is usually an
environment issue, not a bad key:

1. Check `.env` has `PVI_PROBE_BAILIAN_API_KEY` set (non-empty).
2. The launcher uses `load_dotenv(override=True)` — make sure no shell env var
   shadows it with an empty value.
3. Test the key directly:
   ```bash
   python scripts/launch_gateway.py --no-keep
   ```
   The "step 2/4: verify provider key reaches upstream" will tell you if the key
   works.

### 502 `upstream_transport_error`

Usually a timeout (the gateway default is 300s) or network/TUN/proxy issue. See
`docs/gateway-infra-runbook.md §4.1` and `§5.3`.

### 409 `alias_not_callable` / aliases missing from /v1/models

The route's qualification expired or was degraded:

1. `python scripts/gateway_service.py status` — check TTL remaining.
2. `python scripts/gateway_service.py requalify` — requalify expired routes.
3. If the route was degraded by 5 consecutive failures, requalification resets it.

### Multiple gateway instances (port conflict)

Only one instance should listen on 5200. If `netstat` shows two:

```bash
netstat -ano | grep ':5200\b' | grep LISTENING
```

Kill all but one, or kill all and restart cleanly:
```bash
python scripts/gateway_service.py stop
python scripts/gateway_service.py start
```

### Gateway crashed and did not auto-restart

Task Scheduler's `RestartOnFailure` is **not a reliable watchdog**. If the
gateway process died and did not come back:

```bash
# Option A: via the service manager
python scripts/gateway_service.py start

# Option B: trigger the scheduled task directly
powershell -Command "Start-ScheduledTask -TaskName 'PVIaN-Gateway-AtLogon'"
```

## Task Scheduler tasks

Two tasks are registered (verify with `powershell -Command "Get-ScheduledTask -TaskName 'PVIaN-*'"`):

| Task | Trigger | What it does |
|---|---|---|
| `PVIaN-Gateway-AtLogon` | At log on (60s delay) | Runs `launch_gateway.py` via pythonw. Auto-starts the gateway when you log in. |
| `PVIaN-Requalify-Daily` | Daily 04:00 | Runs `requalify_due.py`. Requalifies expiring routes. |

### Disable before maintenance

```bash
powershell -Command "Disable-ScheduledTask -TaskName 'PVIaN-Gateway-AtLogon'"
# ... do your maintenance ...
powershell -Command "Enable-ScheduledTask -TaskName 'PVIaN-Gateway-AtLogon'"
```

This prevents the watchdog from restarting the gateway while you are changing
config.

### After a machine reboot

The gateway auto-starts when you log in (60s delay for network warmup). If it
did not:

```bash
python scripts/gateway_service.py status    # check
python scripts/gateway_service.py start     # manual start
```

## Ledger management

The usage-attempt-ledger (`var/gateway/usage-attempt-ledger.json`) is append-only
and grows over time (currently ~7.5 MB). Archive it quarterly or when it exceeds
~50 MB:

```bash
python scripts/gateway_service.py ledger size      # check size
python scripts/gateway_service.py ledger archive   # archive + fresh empty
```

This is safe to run while the gateway is running (the ledger uses a cross-process
lock).

## Change TTL (validity_hours)

This is a **dangerous operation** — wrong order crashes the gateway on restart.
Read `docs/adr/` and the workorder before attempting.

1. Edit `src/pvian_gateway/profiles.py` (default value + loader fallback).
2. Edit every `config/provider-profiles/*.json` `enablement.validity_hours`.
3. Update test fixtures that hardcode the old value.
4. Run offline tests: `PVI_NETWORK_DISABLED=1 python -m pytest`
5. **Stop the gateway.**
6. Requalify all active profiles (full, NOT `--due`):
   ```bash
   python -m pvi.probe_runner.cli qualify --profile-id <id> --allow-network --confirm-cost
   ```
7. Restart the gateway and verify: `python scripts/gateway_service.py status`

Never restart between steps 1-6 — the intermediate state (profile changed, state
not yet requalified) will crash the binding validator.

## Key file locations

| File | Purpose |
|---|---|
| `.env` | Provider keys + gateway client key + config (gitignored) |
| `var/gateway/qualification-state-flash-0731.json` | Qualification state (the gateway and requalifier share this) |
| `var/gateway/usage-attempt-ledger.json` | Per-request usage ledger (append-only) |
| `var/gateway/gateway.pid` | PID file from gateway_service.py start |
| `var/gateway/requalify-consecutive-failures.json` | Alert debounce counter |
| `.codex_tmp/gateway-5245-aliyun-token-plan/` | 5245 dedicated instance overlay (profiles/aliases/state/ledger; env-pinned, do not delete) |
| `scripts/requalify_aliyun_5245.py` | 5245 in-place state renewal (atomic swap, no process touch) |
| `scripts/qualify_anthropic_surface.py` | Anthropic wire-format qualification probes |
| `config/requalification-budgets.json` | Frozen per-route renewal budgets (priced CNY + null-cost call-count classes) |
| `config/gateway-client-keys.json` | Client key registry (key IDs + env var names, not values) |
| `config/public-model-aliases.json` | Public alias registry |
| `config/provider-profiles/*.json` | Provider qualification profiles |
| `config/pricing/*.json` | Versioned list-price tables (cost accounting only; hot-reload) |
| `config/model-token-limits.json` | Advertised max output/context/thinking-chain + default reasoning behavior + `recommended_defaults` request recipe; injected into pricing-snapshot `output_limits` (hot-reload; see `adr/0003`) |
| `deploy/pvian-gateway-at-logon.xml` | Task Scheduler task definition (for re-import) |

## Further reading

- `docs/gateway-infra-runbook.md` — full troubleshooting playbook (§4-5), 
  restart procedure (§6), long-running service details (§7), handoff checklist (§8)
- `docs/gateway-standard-runbook.md` — standard operations and PowerShell templates
- `docs/adr/0001-automatic-requalification-budget-model.md` — why auto-requalify is budget-gated (+2026-08-20 amendment: 24h renewal window, unpriced budget class, overlay renewal)
- `docs/adr/0002-stable-client-key-via-env-reuse.md` — why the key is in .env not SQLite
- `docs/adr/0003-model-default-thinking-profile.md` — how default thinking behavior &
  `recommended_defaults` are recorded in token-limits data
- `docs/adr/0004-anthropic-native-passthrough-surface.md` — why the Claude surface is
  transparent passthrough, not protocol translation
- `docs/gateway-pricing-snapshots.md` — pricing + `output_limits` (token ceilings, default
  thinking, recommended request params) endpoint; both pricing & token-limits hot-reload
- `WORKORDER-long-running-gateway.md` — the full implementation workorder (archived)
