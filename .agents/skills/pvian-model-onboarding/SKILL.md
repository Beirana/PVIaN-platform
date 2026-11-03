---
name: pvian-model-onboarding
description: Safely onboard or extend provider models in PVIaN, including exact catalog/profile/alias bindings, pricing and capability profiles, qualification evidence, bounded live probes, and isolated gateway smoke tests. Use when adding a model or provider route, updating model capabilities or prices, qualifying an alias, investigating requested/reported model identity differences, or preparing a new gateway instance without disturbing an existing service.
---

# PVIaN Model Onboarding

## Establish scope

1. Read every applicable `AGENTS.md`, repository governance file, and `docs/model-extension-runbook.md` before editing. Stop if the Runbook is missing or its required version does not match the task.
2. Record `git status --short`, inspect overlapping dirty files, and treat existing changes as user-owned. Never reset, overwrite, delete, stage, or commit them without authorization.
3. Separate offline, official/catalog, provider qualification, local forwarder smoke, deployed gateway smoke, and scientific execution evidence. Never convert one evidence class into another.
4. Keep credentials in controlled environment/process sources. Never print, persist, diff, fixture, or forward them downstream. Use a non-secret sentinel to test redaction paths.
5. Read [references/contracts-and-evidence.md](references/contracts-and-evidence.md) before changing profiles, pricing, capabilities, evidence, or state.
6. Read [references/identity-and-authorization-gates.md](references/identity-and-authorization-gates.md) before catalog access, live requests, identity mappings, service starts, or promotion.

## Audit and plan offline

1. Bind each requested model to one exact catalog ID, provider profile/version, endpoint, region, route, and public alias. Never substitute a preview, nearby version, typo, or token-plan model.
2. Add exact profiles, price documents, capability profiles, alias candidates, schemas, and tests. Also record each model in `config/model-token-limits.json`: advertised max output (incl. reasoning tokens), context window, max thinking-chain, thinking controls, structured `thinking_default` (state + default effort/budget), and a `recommended_defaults` request recipe (send/omit/required_non_reasoning) — see `docs/adr/0003-model-default-thinking-profile.md`. Preserve candidate lifecycle until promotion gates pass.
3. Represent unknown prices/capabilities as unknown or null. Never invent zero prices or inherit capabilities from related models.
4. Keep cost/token estimation and per-model accounting. Default both runtime hard caps to null; use exact authorized completion counts as the live boundary. Introduce a runtime token/cost hard stop only under a new plan revision after the user explicitly authorizes that cap.
5. Qualify vision-language models with text-only messages unless the user authorizes a frozen multimodal design. Never infer multimodal qualification from text evidence.
6. Create a JSON live plan and run:

   ```powershell
   python .agents/skills/pvian-model-onboarding/scripts/validate_onboarding_plan.py PLAN.json
   python .agents/skills/pvian-model-onboarding/scripts/offline_preflight.py --repo-root . --plan PLAN.json
   ```

7. Run schemas, focused unit/contract tests, lint, and `git diff --check`. Verify real behavior; file existence is never proof.

## Freeze the capability plan

Test every model independently: message plus usage, stream plus terminal usage, every frozen thinking variant, native tool call followed by replay, and structured output.

`anthropic_native_messages` endpoints are the exception path (ADR 0004): they
qualify via `scripts/qualify_anthropic_surface.py` — message+usage, forced
tool_use with native dict input, and tool_result replay; identity is proven on
the wire (there is no catalog GET on that surface) and only status-only
capability summaries are persisted. The OpenAI qualifier skips this transport,
and daily renewal for such routes runs through the anthropic pass of
`scripts/requalify_due.py`. Never translate between Anthropic and OpenAI wire
formats inside the gateway.

Use one model and one exact route per request. Set SDK retries to zero, total attempts to one, and fallback to null. Persist only requested/reported model, versioned identity decision, route/profile/version, safe request-ID hashes, finish/error classification, usage, estimated cost, attempt count, and validation booleans. Never persist prompt/output text, tool arguments, private reasoning, or raw provider bodies.

Show the user exact catalog GETs, maximum completion calls by model/probe, frozen parameters, and stop conditions. Wait for explicit authorization before billable calls.

## Execute live gates

1. Require both `--allow-network` and `--confirm-cost`; missing either must produce zero completion calls.
2. Discover the catalog once where planned and stop if an exact model is absent.
3. Keep provider artifacts separate from gateway smoke.
4. Stop on authentication, rate limit, transport, identity, usage, request-ID, or finish evidence failures. Never retry with altered parameters or another model.
5. Treat explicit capability-parameter rejection as capability evidence, not permission to fall back.
6. Reconcile identity only through the identity gate. Mapping changes require a new authorization; never reuse prior live consent.
7. Report usage and cost by model. Never truncate an authorized workload because of an arbitrary runtime token/cost ceiling.

## Promote and smoke in isolation

1. Compile active overlay profiles with new versions; never mutate candidate sources solely to start a test service.
2. Generate state through `QualificationStateStore.record_qualification`, then validate profile and alias bindings.
3. Use an unused loopback port and independent profile/alias/state/ledger/log/PID paths. Reuse the controlled client-key registry. Never restart/reconfigure the existing gateway.
4. Never invoke the legacy `scripts/launch_gateway.py`, `taskkill`, or port/pattern-wide process termination.
5. Start the child without a visible Windows console and retain its exact process handle/PID.
6. After dual authorization, call each alias once with no retry/fallback. Persist only schema-validated metadata.
7. Stop only the exact child PID on failure. Keep the isolated service running only after every smoke succeeds.

## Deliver evidence

Separate offline tests, official/catalog evidence, provider-probe evidence, local-forwarder evidence, deployed-gateway evidence, scientific evidence, and unresolved gates. State what is implemented, qualified, callable, and still awaiting authorization or deployment.

