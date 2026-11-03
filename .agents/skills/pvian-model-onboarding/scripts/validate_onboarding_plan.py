from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Any

CAPABILITY_KEYS = (
    "message_usage",
    "stream",
    "thinking",
    "tool_initial",
    "tool_replay",
    "structured_output",
)
FORBIDDEN_KEY = re.compile(
    r"(^|_)(api_?key|bearer|secret|password|raw_?body|prompt|output_?text|reasoning_?content)($|_)",
    re.IGNORECASE,
)
SHA256 = re.compile(r"sha256:[0-9a-f]{64}")
UTC_TIMESTAMP = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z")
RUNBOOK_VERSION = "pvian-model-extension-runbook/v1"


class PlanError(ValueError):
    pass


def _fail(message: str) -> None:
    raise PlanError(message)


def _exact_keys(value: dict[str, Any], expected: set[str], where: str) -> None:
    if set(value) != expected:
        _fail(f"{where} fields are not exact")


def _nonempty(value: Any, where: str) -> str:
    if not isinstance(value, str) or not value.strip():
        _fail(f"{where} must be a non-empty string")
    return value


def _positive_int(value: Any, where: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < 1:
        _fail(f"{where} must be a positive integer")
    return value


def _reject_sensitive_keys(value: Any, path: str = "plan") -> None:
    if isinstance(value, dict):
        for key, item in value.items():
            if (
                not isinstance(key, str)
                or key.casefold() == "authorization"
                or FORBIDDEN_KEY.search(key)
            ):
                _fail(f"{path} contains a forbidden field name")
            _reject_sensitive_keys(item, f"{path}.{key}")
    elif isinstance(value, list):
        for index, item in enumerate(value):
            _reject_sensitive_keys(item, f"{path}[{index}]")


def validate_plan(plan: Any) -> dict[str, int]:
    if not isinstance(plan, dict):
        _fail("plan root must be an object")
    _reject_sensitive_keys(plan)
    expected = {
        "schema_version",
        "runbook_version",
        "runbook_sha256",
        "models",
        "catalog_gets",
        "provider_completion_calls_max",
        "gateway_completion_calls_max",
        "scientific_completion_calls",
        "total_completion_calls_max",
        "retry_total_attempts",
        "fallback",
        "required_live_flags",
        "cost_accounting_required",
        "runtime_cost_cap",
        "runtime_token_cap",
        "evidence_classes",
        "identity_mappings",
        "implementation_files",
        "proof_gates",
        "live_authorization_id",
    }
    _exact_keys(plan, expected, "plan")
    if plan.get("schema_version") != "pvian.model-onboarding-plan/v1":
        _fail("unsupported plan schema_version")
    if plan.get("runbook_version") != RUNBOOK_VERSION:
        _fail("unsupported Runbook version")
    if not isinstance(plan.get("runbook_sha256"), str) or not SHA256.fullmatch(
        plan["runbook_sha256"]
    ):
        _fail("runbook_sha256 must be a canonical digest")
    if plan.get("retry_total_attempts") != 1 or plan.get("fallback") is not None:
        _fail("live requests must use one total attempt and null fallback")
    if plan.get("required_live_flags") != ["allow_network", "confirm_cost"]:
        _fail("both exact live flags are required")
    if (
        plan.get("cost_accounting_required") is not True
        or plan.get("runtime_cost_cap") is not None
        or plan.get("runtime_token_cap") is not None
    ):
        _fail("cost accounting is required and runtime cost/token caps must be null")
    if plan.get("scientific_completion_calls") != 0:
        _fail("model onboarding cannot plan scientific calls")
    if plan.get("proof_gates") != {
        "schema_validation": True,
        "focused_tests": True,
        "binding_tests": True,
        "wire_evidence_after_live": True,
    }:
        _fail("schema, tests, bindings, and wire evidence must all remain gated")
    classes = plan.get("evidence_classes")
    if classes != {
        "offline": True,
        "official_catalog": True,
        "provider_probe": True,
        "forwarder_smoke": True,
        "deployed_gateway_smoke": True,
        "scientific_run": False,
    }:
        _fail("all evidence classes must remain separated")
    models = plan.get("models")
    if not isinstance(models, list) or not models:
        _fail("models must be a non-empty list")
    model_fields = {
        "requested_model",
        "provider",
        "region",
        "profile_id",
        "profile_version",
        "route_id",
        "public_alias",
        "capability_calls",
        "completion_calls_max",
        "vl_text_only",
    }
    seen_models: set[str] = set()
    seen_aliases: set[str] = set()
    provider_total = 0
    for index, model in enumerate(models):
        if not isinstance(model, dict):
            _fail(f"models[{index}] must be an object")
        _exact_keys(model, model_fields, f"models[{index}]")
        requested = _nonempty(model.get("requested_model"), f"models[{index}].requested_model")
        if requested in seen_models:
            _fail("requested models must be unique")
        seen_models.add(requested)
        for name in ("provider", "region", "profile_id", "profile_version", "route_id"):
            _nonempty(model.get(name), f"models[{index}].{name}")
        alias = _nonempty(model.get("public_alias"), f"models[{index}].public_alias")
        if alias in seen_aliases or not alias.startswith("pvian/") or not alias.endswith(requested):
            _fail("public aliases must be unique exact PVIaN bindings")
        seen_aliases.add(alias)
        if not model["route_id"].endswith(":" + requested):
            _fail("route_id must preserve the exact requested model")
        calls = model.get("capability_calls")
        if not isinstance(calls, dict) or set(calls) != set(CAPABILITY_KEYS):
            _fail("capability_calls fields are not exact")
        for name in CAPABILITY_KEYS:
            _positive_int(calls.get(name), f"models[{index}].capability_calls.{name}")
        for name in ("message_usage", "stream", "tool_initial", "tool_replay", "structured_output"):
            if calls[name] != 1:
                _fail(f"{name} must reserve exactly one completion")
        calculated = sum(calls.values())
        if model.get("completion_calls_max") != calculated:
            _fail("per-model completion_calls_max does not match capability calls")
        if not isinstance(model.get("vl_text_only"), bool):
            _fail("vl_text_only must be boolean")
        if "vl" in requested.lower() and model["vl_text_only"] is not True:
            _fail("vision-language targets must freeze text-only scope")
        provider_total += calculated
    if plan.get("catalog_gets") != 1:
        _fail("catalog_gets must be exactly one")
    if plan.get("provider_completion_calls_max") != provider_total:
        _fail("provider completion total does not match model plans")
    if plan.get("gateway_completion_calls_max") != len(models):
        _fail("gateway smoke must reserve exactly one call per model")
    if plan.get("total_completion_calls_max") != provider_total + len(models):
        _fail("total completion count is inconsistent")
    mappings = plan.get("identity_mappings")
    if not isinstance(mappings, list):
        _fail("identity_mappings must be a list")
    mapping_fields = {
        "mapping_id",
        "mapping_version",
        "mapping_policy_id",
        "match_mode",
        "requested_model",
        "reported_model",
        "provider",
        "reported_provider",
        "region",
        "profile_id",
        "profile_version",
        "evidence_sha256",
        "user_confirmation_id",
        "confirmed_at",
        "decision",
        "authorization_revision",
    }
    seen_mapping_ids: set[str] = set()
    seen_mapping_pairs: set[tuple[str, str]] = set()
    for index, mapping in enumerate(mappings):
        if not isinstance(mapping, dict):
            _fail(f"identity_mappings[{index}] must be an object")
        _exact_keys(mapping, mapping_fields, f"identity_mappings[{index}]")
        for name in mapping_fields - {"evidence_sha256"}:
            _nonempty(mapping.get(name), f"identity_mappings[{index}].{name}")
        if mapping["mapping_id"] in seen_mapping_ids:
            _fail("identity mapping IDs must be unique")
        seen_mapping_ids.add(mapping["mapping_id"])
        mapping_pair = (mapping["requested_model"], mapping["reported_model"])
        if mapping_pair in seen_mapping_pairs:
            _fail("identity mapping pairs must be unique")
        seen_mapping_pairs.add(mapping_pair)
        if mapping["match_mode"] != "exact_pair":
            _fail("identity mappings must use exact_pair matching")
        if mapping["requested_model"] not in seen_models:
            _fail("identity mapping requested model is outside the plan")
        if mapping["requested_model"] == mapping["reported_model"]:
            _fail("exact identity must not create a mapping")
        if mapping["requested_model"].casefold() == mapping["reported_model"].casefold():
            _fail("case-only identity mappings are forbidden")
        if any(char in mapping["reported_model"] for char in "*?"):
            _fail("wildcard identity mappings are forbidden")
        planned_model = next(
            model for model in models if model["requested_model"] == mapping["requested_model"]
        )
        if mapping["provider"] != planned_model["provider"]:
            _fail("identity mapping provider must match the planned provider")
        if mapping["reported_provider"] != mapping["provider"]:
            _fail("cross-provider identity mappings are forbidden")
        if mapping["region"] != planned_model["region"]:
            _fail("identity mapping region must match the planned region")
        if mapping["profile_version"] != planned_model["profile_version"]:
            _fail("identity mapping profile version must match the planned profile")
        if mapping["profile_id"] != planned_model["profile_id"]:
            _fail("identity mapping profile ID must match the planned profile")
        if not SHA256.fullmatch(str(mapping.get("evidence_sha256"))):
            _fail("identity mapping evidence digest is invalid")
        if mapping["decision"] != "accepted":
            _fail("identity mapping decision must be accepted")
        if not UTC_TIMESTAMP.fullmatch(mapping["confirmed_at"]):
            _fail("identity mapping confirmed_at must be a canonical UTC timestamp")
    files = plan.get("implementation_files")
    if not isinstance(files, list) or not files:
        _fail("implementation_files must be a non-empty list")
    for index, item in enumerate(files):
        path = Path(_nonempty(item, f"implementation_files[{index}]"))
        if (
            path.is_absolute()
            or ".." in path.parts
            or path.as_posix() == "scripts/launch_gateway.py"
        ):
            _fail("implementation_files must be safe repository-relative paths")
    authorization = plan.get("live_authorization_id")
    if authorization is not None and (not isinstance(authorization, str) or not authorization):
        _fail("live_authorization_id must be null or a non-empty identifier")
    if mappings and authorization is None:
        _fail("identity mappings require a fresh live authorization revision")
    if mappings and any(mapping["authorization_revision"] != authorization for mapping in mappings):
        _fail("mapping authorization revision must match the current live authorization")
    return {
        "models": len(models),
        "catalog_gets": 1,
        "provider_completion_calls_max": provider_total,
        "gateway_completion_calls_max": len(models),
        "total_completion_calls_max": provider_total + len(models),
    }


def permitted_completion_calls(
    plan: Any, *, allow_network: bool, confirm_cost: bool
) -> int:
    """Return the deterministic completion budget; this function never performs I/O."""
    summary = validate_plan(plan)
    if not (allow_network and confirm_cost) or plan["live_authorization_id"] is None:
        return 0
    return summary["total_completion_calls_max"]


def load_and_validate(path: Path) -> dict[str, int]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise PlanError("cannot read onboarding plan JSON") from exc
    return validate_plan(value)


def main() -> int:
    parser = argparse.ArgumentParser(description="Validate a PVIaN model onboarding live plan.")
    parser.add_argument("plan", type=Path)
    arguments = parser.parse_args()
    try:
        summary = load_and_validate(arguments.plan)
    except PlanError as exc:
        print(json.dumps({"status": "invalid", "reason": str(exc)}, separators=(",", ":")))
        return 2
    print(json.dumps({"status": "valid", **summary}, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    sys.exit(main())
