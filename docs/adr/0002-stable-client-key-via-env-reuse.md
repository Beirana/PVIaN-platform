# ADR 0002: Stable Client Key via .env Reuse

Date: 2026-08-10
Status: Accepted
Workorder: WORKORDER-long-running-gateway.md (task B, decision D2)

## Context

The PVIaN gateway authenticates downstream consumers with a Bearer client key.
The gateway already had a complete stable-key architecture:
`config/gateway-client-keys.json` (registry of key IDs + status + env var names),
`.env` (stores the actual key value, gitignored), and `auth.py` (reads the key
from the environment variable, computes a SHA256 digest, supports active/revoked
multi-key rotation).

However, `scripts/launch_gateway.py` **ignored this architecture** — it called
`generate_client_key()` (secrets.token_urlsafe(48)) on every launch and injected
the random key into the gateway subprocess environment, overriding the `.env`
value. This made the client key change on every gateway restart, forcing
downstream consumers to update their credentials each time.

The workorder's initial proposal (task B v1) was to build a new SQLite-backed
key store. The self-review discovered this was redundant — the existing
architecture already solved the problem; only the launcher was broken.

## Decision

**Fix the launcher to reuse the .env key** (solution X), not build new persistence.

1. `load_env_with_override` in `launch_gateway.py` now loads
   `PVI_GATEWAY_CLIENT_KEY_INTERNAL_PRIMARY` from `.env` (alongside the existing
   provider vars).

2. `main()` reads the key from `os.environ` (populated by step 1) instead of
   generating a random one. It falls back to a one-time generated key only when
   `.env` has no usable value (< 32 bytes), with a warning.

3. `auth.py` is unchanged — it already reads from the environment variable.

4. Rotation is done by editing `.env` + restarting, or by using the
   `gateway-client-keys.json` registry's active/revoked mechanism for smooth
   transitions (two active entries during transition, then revoke the old one).

## Consequences

- **Positive**: Zero new persistence code. The existing `.env` + registry +
  auth.py stack is sufficient and already security-reviewed.
- **Positive**: `.env` is already gitignored and stays on the local machine —
  no SQLite database file that could be accidentally backed up or synced.
- **Positive**: Key digest lives only in gateway memory (`auth.py:77`), never
  in logs.
- **Negative**: Key rotation requires a gateway restart (no hot-swap). This is
  acceptable — rotation is infrequent and the `.bat`/`gateway_service.py`
  restart path is now a single command.

## Alternatives considered

- **SQLite key store** (original workorder proposal): rejected. Redundant with
  the existing architecture. A SQLite file introduces a new backup/sync
  exposure surface for what is fundamentally a single local secret. More code,
  more complexity, no benefit.
- **Windows DPAPI encryption of .env**: deferred. Would add defense-in-depth
  but is out of scope for this workorder. The .env is already local-only and
  gitignored.
