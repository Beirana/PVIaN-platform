# Contracts and evidence

## Evidence classes

Keep all six classes independent:

| Class | Purpose | May prove |
|---|---|---|
| Offline | Schemas, static bindings, tests, lint | Internal consistency without network evidence |
| Official/catalog | Official documentation, price tables, authenticated model catalog | Advertised model IDs, availability, documented parameters, list prices |
| Provider probe | Direct exact upstream call | Parameter acceptance, capabilities, identity, provider usage |
| Forwarder smoke | Local forwarding layer before gateway deployment | Forwarding protocol and safe evidence capture |
| Deployed gateway smoke | Public alias call through an isolated gateway | Alias/state/profile binding and gateway forwarding |
| Scientific run | Frozen experimental design | Experimental observations only |

Never promote a claim using another class. Never count a probe or smoke as a scientific replicate.

## Exact binding

Bind `requested catalog ID -> provider profile ID/version -> endpoint/region -> route -> public alias -> qualification-state record`. Preserve case and slashes. Keep preview, token-plan, dedicated-endpoint, and standard-endpoint models distinct.

Require `docs/model-extension-runbook.md` and the task's expected Runbook version before implementation. Stop on absence or mismatch.

## Pricing and capability

- Record currency, unit, capture/effective time, source, model, region, and tier boundaries.
- Store unknown cache rates as null, never zero.
- Account observed usage/cost by model without enforcing an arbitrary runtime token/spending cap. Use exact authorized completion counts as the default hard boundary; require a separately revised, explicitly authorized plan for any token/cost hard stop.
- Separate advertised, observed, and effective capability states.
- Probe message, usage, stream, thinking variants, native tool call, replay, and structured output independently.
- Label vision-language evidence as text-only unless a multimodal plan is separately frozen.

## Safe evidence allowlist

Allow only schema/evidence version, class, timestamp, bounded call counts, requested/reported identity decision, profile/route/endpoint/region/alias, safe request-ID hashes, HTTP/finish/error classification, normalized usage/cost, validation booleans, and attempt count.

Reject prompts, outputs, tool arguments/results, private reasoning, raw bodies, broad raw headers, authorization values, keys, and provider exception text. Test persistence with a non-secret sentinel and assert that it never appears in artifacts, logs, stdout, stderr, or diffs.

## Proof order

1. Validate schemas and exact bindings.
2. Run deterministic offline preflight and plan validation.
3. Run focused unit/contract tests, lint, and diff checks.
4. Obtain live authorization.
5. Run provider probes.
6. Resolve identity gates.
7. Build and validate an isolated overlay/state.
8. Run gateway smoke on a new port.
9. Validate wire results and artifacts.

File existence is never proof. Require schema validity, tests, binding checks, and wire evidence for each applicable claim.
