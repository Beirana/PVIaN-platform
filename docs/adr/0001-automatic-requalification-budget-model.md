# ADR 0001: Automatic Requalification Budget Model

Date: 2026-08-10
Status: Accepted
Amended: 2026-08-20 (renewal window + unpriced budget class; see bottom section)
Workorder: WORKORDER-long-running-gateway.md (task D, decision D4)

## Context

The PVIaN gateway exposes model aliases only while their qualification is valid
(state.py `callable_record`). Qualification TTL was originally 24h, causing the
2026-08-10 S3 outage when all aliases expired simultaneously. The long-running-
gateway workorder extends TTL to 7 days (task A) and adds a daily automatic
requalification scheduler at 04:00 (task D).

Automatic requalification means the system makes **paid upstream API calls
without per-invocation human authorization**. This relaxes the existing
principle in `docs/model-extension-runbook.md §7`, which requires "every paid
qualification call to be explicitly authorized."

## Decision

Adopt a **per-model frozen-budget** authorization model:

1. **At model onboarding**, the maximum completion-call count is calculated
   using `qualification_completion_call_count` (message+usage 1 + stream 1 +
   thinking per variant + tool/replay family 2). This count is multiplied by
   the then-current list price to yield a frozen maximum cost per requalification
   run.

2. **The frozen budget is recorded** in `config/requalification-budgets.json`
   (committed, non-sensitive) after the user confirms it once. After
   confirmation, automatic requalification for that model is **permanently
   authorized up to that frozen budget** — no per-run re-authorization.

3. **No global monthly cap.** The user explicitly chose per-model budgets over
   a global ceiling (D4). Rationale: global caps create false coupling between
   unrelated models and risk blocking a cheap-but-important model because an
   expensive one exhausted the shared quota.

4. **Budgets are never auto-increased.** If an upstream price change causes
   actual cost to exceed the frozen budget, requalification for that model
   **stops and alerts**. A human must re-evaluate and manually update the
   frozen budget. This is the fail-closed guard against silent cost drift.

## Consequences

- **Positive**: Daily requalification runs unattended; aliases stay alive
  across weeks without manual intervention. The 2026-08-10 outage class is
  eliminated.
- **Positive**: Each model's cost ceiling is auditable from a single committed
  file; no hidden runtime state controls spending.
- **Negative**: An upstream price hike silently stops requalification until a
  human intervenes. Mitigated by the alerting in `requalify_due.py` (2-day
  debounce) and the 7-day TTL buffer (a price-hike-induced stoppage does not
  cause immediate alias expiry).
- **Negative**: Adding a new model requires a one-time budget calculation and
  confirmation step before automatic requalification covers it. This is
  acceptable — model onboarding is infrequent.

## Alternatives considered

- **Global monthly cap**: rejected (D4). Creates coupling between models and
  risks blocking unrelated services.
- **Per-run authorization**: rejected. Would require daily human action,
  defeating the purpose of automation and reintroducing the outage risk.
- **Unlimited auto-requalification**: rejected. Violates the §7 cost-control
  principle and creates unbounded financial exposure.

## Amendment 2026-08-20: 24h renewal window and unpriced budget class

Operator directive: automatic renewal only, no manual requalification rounds.

1. **Renewal window (behavioral change)**: `due_records`/`renewal_due`
   (`src/pvian_gateway/state.py`) now treat a route as due once its remaining
   validity enters the final **24 hours** (`RENEWAL_WINDOW_HOURS = 24`), in
   addition to the previous expired/degraded conditions. The daily 04:00
   checkpoint therefore refreshes a due route to a fresh 168h TTL *before*
   expiry, eliminating the up-to-24h post-expiry outage window of the old
   expired-only semantics. `callable_record` is unchanged: aliases stay
   callable until actual expiry, so the gateway request path is untouched.
   The qualifier's `--due` skip now consults `renewal_due` instead of
   callable-record presence.
2. **Unpriced budget class**: subscription (Credits) and relay providers have
   no public per-token list price, so their budget entries freeze
   `max_completion_calls` only with `frozen_max_cost_cny: null` and a
   `pricing_basis.note`. No CNY figure is invented for Credits or
   console-walled relay pricing. Membership gating in `requalify_due.py` is
   unchanged (fail-closed on any due route without an entry), which remains
   the load-bearing control.
3. **Isolated overlay renewal**: the same daily checkpoint renews the
   dedicated 5245 gateway by qualifying into a temporary state and atomically
   swapping the overlay state file; the running process is never restarted or
   stopped (operator constraint).
