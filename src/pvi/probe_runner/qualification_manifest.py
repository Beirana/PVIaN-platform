from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from decimal import Decimal
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator

from pvi.probe_runner.qualification import qualification_completion_call_count
from pvian_gateway.profiles import ProviderProfile


class QualificationManifestError(ValueError):
    """The offline qualification authorization contract is invalid or drifted."""


def load_qualification_manifest(
    path: Path,
    schema_path: Path,
    *,
    profile: ProviderProfile | None = None,
    alias_registry: Any = None,
) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        schema = json.loads(schema_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise QualificationManifestError("cannot read qualification manifest") from exc
    validate_qualification_manifest(value, schema, profile=profile, alias_registry=alias_registry)
    return value


def validate_qualification_manifest(
    value: object,
    schema: Mapping[str, Any],
    *,
    profile: ProviderProfile | None = None,
    alias_registry: Any = None,
) -> None:
    errors = sorted(
        Draft202012Validator(schema).iter_errors(value), key=lambda item: list(item.path)
    )
    if errors:
        raise QualificationManifestError("qualification manifest schema validation failed")
    assert isinstance(value, dict)
    expected_digest = qualification_manifest_digest(value)
    if value["manifest_digest"] != expected_digest:
        raise QualificationManifestError("qualification manifest digest mismatch")
    identity = value["identity"]
    if identity["requested_model"] == "qwen3-vl-plus" and identity["model_comparability"] != (
        "interface_compatibility_only"
    ):
        raise QualificationManifestError("qwen3-vl-plus is not a homogeneous text candidate")
    plan = value["probe_plan"]
    expected_calls = planned_completion_calls(plan["probes"], plan["request_variant_ids"])
    if plan["completion_calls"] != expected_calls or plan["max_completion_calls"] != expected_calls:
        raise QualificationManifestError("qualification completion call budget mismatch")
    if not _parameters_are_safe(value["request"]["parameters"]):
        raise QualificationManifestError("qualification request parameters are unsafe")
    _validate_parameter_relations(value["request"])
    required_stops = {
        "catalog_exact_model_absent",
        "requested_reported_model_mismatch",
        "unexpected_parameter_rejection",
        "missing_required_usage",
        "missing_required_finish_reason",
        "transport_or_provider_error",
        "cost_cap_would_be_exceeded",
        "manifest_or_profile_drift",
    }
    if set(value["stop_conditions"]) != required_stops:
        raise QualificationManifestError("qualification stop conditions are incomplete")
    readiness = value["readiness"]
    if (readiness["status"] == "blocked") != bool(readiness["blockers"]):
        raise QualificationManifestError("qualification readiness blockers are inconsistent")
    input_rate = Decimal(value["pricing"]["input_per_1m"])
    cached_rate_value = value["pricing"]["cached_input_per_1m"]
    # Unknown is not zero. Qualification does not rely on cache hits, so the
    # normal input rate is the conservative authorization rate in that case.
    cached_rate = input_rate if cached_rate_value is None else Decimal(cached_rate_value)
    maximum_input_rate = max(input_rate, cached_rate)
    expected_cost = (
        Decimal(plan["input_token_cap_total"]) * maximum_input_rate
        + Decimal(plan["output_token_cap_total"]) * Decimal(value["pricing"]["output_per_1m"])
    ) / Decimal(1_000_000)
    if Decimal(value["pricing"]["maximum_cost"]) != expected_cost:
        raise QualificationManifestError("qualification maximum cost mismatch")
    if profile is not None:
        _validate_profile_binding(value, profile, alias_registry)


def qualification_manifest_digest(value: Mapping[str, Any]) -> str:
    without_digest = {name: item for name, item in value.items() if name != "manifest_digest"}
    encoded = json.dumps(
        without_digest, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def planned_completion_calls(probes: list[str], request_variant_ids: list[str]) -> int:
    probe_set = set(probes)
    count = int(bool(probe_set & {"message", "usage"}))
    count += int("stream" in probe_set)
    count += len(request_variant_ids) if "thinking" in probe_set else 0
    count += 2 if probe_set & {"native_tool_call", "assistant_replay", "tool_result_replay"} else 0
    if count < 1:
        raise QualificationManifestError("qualification plan has no completion calls")
    if "thinking" in probe_set and not request_variant_ids:
        raise QualificationManifestError("thinking probe requires a frozen request variant")
    if "thinking" not in probe_set and request_variant_ids:
        raise QualificationManifestError("request variants are only valid for thinking probes")
    return count


def _validate_profile_binding(
    value: dict[str, Any], profile: ProviderProfile, alias_registry: Any = None
) -> None:
    identity = value["identity"]
    if (identity["profile_id"], identity["profile_version"]) != (
        profile.profile_id,
        profile.profile_version,
    ):
        raise QualificationManifestError("qualification manifest profile binding mismatch")
    if not profile.declares_upstream_model(identity["requested_model"]):
        raise QualificationManifestError("qualification manifest model is not profile-declared")
    if identity["provider"] != profile.provider_id.removeprefix("provider-"):
        raise QualificationManifestError("qualification manifest provider binding mismatch")
    if identity["region"] != profile.region:
        raise QualificationManifestError("qualification manifest region binding mismatch")
    expected_routes = {
        f"{endpoint.route_id_prefix}:{identity['requested_model']}"
        for endpoint in profile.endpoints
        if endpoint.qualification_enabled
    }
    if identity["route_id"] not in expected_routes:
        raise QualificationManifestError("qualification manifest route binding mismatch")
    if identity["public_alias"] is not None:
        if alias_registry is None:
            raise QualificationManifestError("public alias binding was not independently verified")
        alias = alias_registry.get(identity["public_alias"])
        if alias is None or (alias.route_id, alias.upstream_model) != (
            identity["route_id"],
            identity["requested_model"],
        ):
            raise QualificationManifestError("qualification manifest public alias binding mismatch")
    if set(value["probe_plan"]["probes"]) != set(profile.policy.required_probes):
        raise QualificationManifestError("qualification manifest probes differ from profile freeze")
    expected_variants = (
        [item.get("variant_id") for item in profile.request_variants]
        if "thinking" in profile.policy.required_probes
        else []
    )
    if value["probe_plan"]["request_variant_ids"] != expected_variants:
        raise QualificationManifestError(
            "qualification manifest variants differ from profile freeze"
        )
    if value["probe_plan"]["completion_calls"] != qualification_completion_call_count(profile):
        raise QualificationManifestError("qualification manifest call budget differs from profile")


def _parameters_are_safe(parameters: list[dict[str, Any]]) -> bool:
    forbidden = {
        "authorization",
        "api_key",
        "apikey",
        "credential",
        "messages",
        "prompt",
        "input",
        "private_reasoning",
        "reasoning_content",
        "raw_provider_body",
    }
    names: set[str] = set()
    for parameter in parameters:
        name = parameter["name"].lower()
        if name in names or any(part in forbidden for part in name.replace("-", ".").split(".")):
            return False
        names.add(name)
        if parameter["request_state"] != "sent" and parameter["value"] is not None:
            return False
    return True


def _validate_parameter_relations(request: dict[str, Any]) -> None:
    parameters = {item["name"]: item for item in request["parameters"]}
    output_name = request["max_output_parameter"]
    output = parameters.get(output_name)
    if (
        output is None
        or output["request_state"] != "sent"
        or output["value"] != request["max_output_tokens"]
    ):
        raise QualificationManifestError("max output parameter freeze is inconsistent")
    other_output = "max_completion_tokens" if output_name == "max_tokens" else "max_tokens"
    if other_output in parameters:
        raise QualificationManifestError("multiple max output parameters are forbidden")
    seed = parameters.get("seed")
    support = request["seed_support"]
    if support in {"observed_supported", "official_documented"}:
        valid = (
            seed is not None
            and seed["request_state"] == "sent"
            and isinstance(seed["value"], int)
            and not isinstance(seed["value"], bool)
        )
    elif support == "unsupported":
        valid = seed == {"name": "seed", "request_state": "unsupported", "value": None}
    else:
        valid = seed == {"name": "seed", "request_state": "omitted", "value": None}
    if not valid:
        raise QualificationManifestError("seed support and request freeze are inconsistent")
