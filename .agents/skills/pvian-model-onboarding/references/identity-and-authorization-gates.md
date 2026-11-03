# Identity and authorization gates

## Requested/reported identity

Accept exact equality by default. On mismatch:

1. Stop route promotion and publication.
2. Preserve requested ID, reported ID, provider, region, profile/version, endpoint, request-ID hash, finish, usage, and evidence digest.
3. Present the mismatch and impact to the user.
4. Wait for explicit acceptance.
5. Create one versioned mapping only when evidence proves the same vendor and model behind a documented aggregator namespace.
6. Bind the confirmation record to requested ID, reported ID, provider, region, profile ID/version, evidence digest, `mapping_policy_id`, mapping version, confirmation time, accepted decision, and a fresh live-authorization revision.
7. Test the exact accepted pair and reject all nearby forms.

Reject cross-vendor, fuzzy, substring, generic prefix/suffix stripping, case folding, and wildcard mappings. Never implement a universal normalization helper.

Changing a mapping invalidates earlier live authorization. Recalculate the plan and obtain new authorization.

## Authorization gates

Keep catalog GET, provider completion, gateway start/deploy, gateway completion smoke, scientific run, and commit/push permissions distinct. Do not infer one from another.

Before billable calls, present exact GET/completion counts by model and probe. Require both live flags. Missing either flag must execute zero completion calls. Set retries to zero, total attempts to one, and fallback to null.

## Failure gates

Stop globally on authentication or rate limiting. Stop the affected model on transport failure, missing usage/request-ID evidence, identity rejection, invalid terminal finish, or output exhaustion. Do not retry or substitute another model.

An explicit capability-specific 4xx may establish `observed_unsupported`; continue only to independent probes allowed by the frozen plan.

## Isolated gateway

Keep the existing service unchanged. Build a new-version active overlay with independent state, ledger, log, and PID on an unused loopback port. Validate bindings before spawn.

Statically reject references to legacy `scripts/launch_gateway.py`, `taskkill`, port-wide killing, and broad command-line-pattern killing. On failure, terminate only the exact process object/PID created by the current run.
