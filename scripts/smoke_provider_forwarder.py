from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
from pathlib import Path
from typing import Any

import httpx
from dotenv import load_dotenv

from pvi.probe_runner.qualification import _base_request_parameters, _probe_policy
from pvian_gateway.accounting import UsageCollector, estimate, load_pricing, select_quote
from pvian_gateway.forwarder import TransparentForwarder
from pvian_gateway.profiles import ProviderProfile, load_profiles

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SMOKE_PROMPT = "Reply with exactly: PVI_GATEWAY_SMOKE_OK"

PROVIDER_SETS: dict[str, dict[str, Any]] = {
    "moonshot-official": {
        "provider": "moonshot-official",
        "region": "cn",
        "credential_var": "PVI_PROBE_MOONSHOT_API_KEY",
        "profile_ids": ["provider-moonshot-official-kimi-k3"],
        "models": ["kimi-k3"],
        "artifact": PROJECT_ROOT
        / "data"
        / "evidence-artifacts"
        / "gateway-forwarder-smoke-moonshot-official-20260817.json",
    },
    "openrouter-free": {
        "provider": "openrouter",
        "region": "global",
        "credential_var": "PVI_PROBE_OPENROUTER_API_KEY",
        "profile_ids": ["provider-openrouter-dots-3-note-free"],
        "models": ["dots-studio/dots-3-note-preview:free"],
        "artifact": PROJECT_ROOT
        / "data"
        / "evidence-artifacts"
        / "gateway-forwarder-smoke-openrouter-free-20260817.json",
    },
    "aigocode-grok": {
        "provider": "aigocode",
        "region": "global",
        "credential_var": "PVI_PROBE_AIGOCODE_GROK_2.5_API_KEY",
        "profile_ids": ["provider-aigocode-grok-4-6"],
        "models": ["grok-4.6"],
        "artifact": PROJECT_ROOT
        / "data"
        / "evidence-artifacts"
        / "gateway-forwarder-smoke-aigocode-grok-20260818.json",
    },
    "aigocode-sol": {
        "provider": "aigocode",
        "region": "global",
        "credential_var": "PVI_PROBE_AIGOCODE_CODEX_1.2_API_KEY",
        "profile_ids": ["provider-aigocode-gpt-5-6-sol"],
        "models": ["gpt-5.6-sol"],
        "artifact": PROJECT_ROOT
        / "data"
        / "evidence-artifacts"
        / "gateway-forwarder-smoke-aigocode-sol-20260818.json",
    },
    "aigocode-sonnet": {
        "provider": "aigocode",
        "region": "global",
        "credential_var": "PVI_PROBE_AIGOCODE_CLAUDE_3.9_API_KEY",
        "profile_ids": ["provider-aigocode-claude-sonnet-5"],
        "models": ["claude-sonnet-5"],
        "artifact": PROJECT_ROOT
        / "data"
        / "evidence-artifacts"
        / "gateway-forwarder-smoke-aigocode-sonnet-20260818.json",
    },
    "aigocode-gemini": {
        "provider": "aigocode",
        "region": "global",
        "credential_var": "PVI_PROBE_AIGOCODE_GEMINI_1_API_KEY",
        "profile_ids": ["provider-aigocode-gemini-3-7-flash"],
        "models": ["gemini-3.7-flash"],
        "artifact": PROJECT_ROOT
        / "data"
        / "evidence-artifacts"
        / "gateway-forwarder-smoke-aigocode-gemini-20260818.json",
    },
    "local-qwen": {
        "provider": "local-qwen",
        "region": "local",
        "credential_var": "PVI_PROBE_LOCAL_QWEN_API_KEY",
        "profile_ids": ["provider-local-qwen-qwen3-8-27b-fp8"],
        "models": ["qwen3.8-27b-fp8"],
        "artifact": PROJECT_ROOT
        / "data"
        / "evidence-artifacts"
        / "gateway-forwarder-smoke-local-qwen-20260818.json",
    },
    "mimo-token-plan": {
        "provider": "mimo-token-plan",
        "region": "cn",
        "credential_var": "PVI_PROBE_MIMO_TOKEN_PLAN_API_KEY",
        "profile_ids": ["provider-mimo-token-plan-mimo-v2-5-pro"],
        "models": ["mimo-v2.5-pro"],
        "artifact": PROJECT_ROOT
        / "data"
        / "evidence-artifacts"
        / "gateway-forwarder-smoke-mimo-token-plan-20260818.json",
    },
    "bigmodel-coding": {
        "provider": "bigmodel-coding",
        "region": "cn",
        "credential_var": "PVI_PROBE_BIGMODEL_CODING_API_KEY",
        "profile_ids": ["provider-bigmodel-coding-glm-5-3"],
        "models": ["glm-5.3"],
        "artifact": PROJECT_ROOT
        / "data"
        / "evidence-artifacts"
        / "gateway-forwarder-smoke-bigmodel-coding-glm-5-3.json",
    },
    "aliyun-token-plan": {
        "provider": "aliyun-token-plan",
        "region": "cn-beijing",
        "credential_var": "PVI_PROBE_ALIYUN_TOKEN_PLAN_API_KEY",
        "profile_ids": [
            "provider-aliyun-token-plan-qwen3-8-max",
            "provider-aliyun-token-plan-qwen3-7-max",
            "provider-aliyun-token-plan-qwen3-6-flash",
        ],
        "models": [
            "qwen3.8-max",
            "qwen3.7-max",
            "qwen3.6-flash",
        ],
        "artifact": PROJECT_ROOT
        / "data"
        / "evidence-artifacts"
        / "gateway-forwarder-smoke-aliyun-token-plan-20260819.json",
    },
}


def _arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Run one bounded, content-free forwarder smoke per model for a provider set."
    )
    parser.add_argument("--provider-set", required=True, choices=sorted(PROVIDER_SETS))
    parser.add_argument("--allow-network", action="store_true")
    parser.add_argument("--confirm-cost", action="store_true")
    return parser.parse_args()


def _require_live_authorization(arguments: argparse.Namespace) -> None:
    if not arguments.allow_network or not arguments.confirm_cost:
        raise SystemExit("live smoke requires --allow-network and --confirm-cost")


def _safe_request_id(headers: dict[str, str]) -> str | None:
    for name in ("x-request-id", "request-id"):
        value = next((item for key, item in headers.items() if key.lower() == name), None)
        if isinstance(value, str) and value and len(value) <= 1024:
            return "sha256:" + hashlib.sha256(f"{name}:{value}".encode()).hexdigest()
    return None


def _failure_category(status_code: int) -> str | None:
    if 200 <= status_code < 300:
        return None
    if status_code == 429:
        return "rate_limited"
    if status_code in {408, 504}:
        return "timeout"
    if 500 <= status_code < 600:
        return "upstream_5xx"
    return "upstream_protocol_or_auth"


def _request_body(profile: ProviderProfile, model: str) -> bytes:
    policy = _probe_policy(profile)
    variant = profile.request_variants[0]
    overrides = variant.get("request_overrides")
    if not isinstance(overrides, dict):
        overrides = {}
    value = {
        "model": model,
        "messages": [{"role": "user", "content": SMOKE_PROMPT}],
        "stream": False,
        **_base_request_parameters(policy, "message"),
        **overrides,
    }
    return json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


async def _discard(response: Any) -> None:
    async for _ in response.body:
        pass


async def _smoke_one(
    forwarder: TransparentForwarder,
    profile: ProviderProfile,
    model: str,
    quotes: tuple[Any, ...],
    provider: str,
    region: str,
) -> dict[str, Any]:
    endpoint = profile.resolve_endpoint(profile.endpoints[0])
    result: dict[str, Any] = {
        "profile_id": profile.profile_id,
        "profile_version": profile.profile_version,
        "route_id": endpoint.route_id(model),
        "requested_model": model,
        "reported_model": None,
        "status": "attempted",
        "http_status": None,
        "provider_request_id_sha256": None,
        "finish_reason": None,
        "usage": None,
        "cost": {"status": "unknown", "reason": "usage_unknown"},
        "error_category": None,
    }
    try:
        response = await forwarder.forward(
            endpoint,
            profile.credential(),
            _request_body(profile, model),
            {"content-type": "application/json", "accept": "application/json"},
        )
        result["http_status"] = response.status_code
        result["provider_request_id_sha256"] = _safe_request_id(response.headers)
        category = _failure_category(response.status_code)
        if category is not None:
            result["status"] = "provider_error"
            result["error_category"] = category
            await _discard(response)
            await response.close()
            return result
        collector = UsageCollector(response.headers.get("content-type"))
        async for chunk in response.body:
            collector.feed(chunk)
        await response.close()
        usage = collector.final_usage()
        result["reported_model"] = collector.reported_model
        result["finish_reason"] = collector.finish_reason
        result["usage"] = usage.as_dict() if usage is not None else None
        quote = select_quote(quotes, provider, region, model)
        result["cost"] = estimate(usage, quote)
        if collector.reported_model != model:
            result["status"] = "model_mismatch"
            result["error_category"] = "requested_reported_model_mismatch"
        elif usage is None:
            result["status"] = "usage_unknown"
            result["error_category"] = "missing_required_usage"
        elif collector.finish_reason is None:
            result["status"] = "finish_reason_unknown"
            result["error_category"] = "missing_required_finish_reason"
        elif collector.finish_reason == "length":
            result["status"] = "observed_truncated"
            result["error_category"] = "output_limit_reached"
        else:
            result["status"] = "observed_supported"
        return result
    except httpx.TimeoutException:
        result["status"] = "transport_error"
        result["error_category"] = "timeout"
        return result
    except httpx.TransportError:
        result["status"] = "transport_error"
        result["error_category"] = "transport_error"
        return result


async def _run(spec: dict[str, Any]) -> dict[str, Any]:
    profiles = load_profiles(PROJECT_ROOT / "config" / "provider-profiles")
    selected = [profiles[profile_id] for profile_id in spec["profile_ids"]]
    for profile, model in zip(selected, spec["models"], strict=True):
        if not profile.declares_upstream_model(model):
            raise RuntimeError("profile does not declare its exact model")

    first = selected[0]
    endpoint = first.resolve_endpoint(first.endpoints[0])
    credential = first.credential()
    timeout = max(_probe_policy(profile).timeout_seconds for profile in selected)
    async with httpx.AsyncClient(timeout=timeout, follow_redirects=False) as client:
        catalog_response = await client.get(
            endpoint.catalog_url(),
            headers={"authorization": f"Bearer {credential}", "accept": "application/json"},
        )
        catalog_request_id = _safe_request_id(dict(catalog_response.headers))
        try:
            catalog_value = catalog_response.json()
        except (UnicodeDecodeError, json.JSONDecodeError):
            catalog_value = {}
        data = catalog_value.get("data") if isinstance(catalog_value, dict) else None
        catalog_ids = {
            item["id"]
            for item in data or []
            if isinstance(item, dict) and isinstance(item.get("id"), str)
        }
        quotes = load_pricing(PROJECT_ROOT / "config" / "pricing")
        forwarder = TransparentForwarder(client)
        results: list[dict[str, Any]] = []
        for profile, model in zip(selected, spec["models"], strict=True):
            if catalog_response.status_code == 200 and model not in catalog_ids:
                results.append(
                    {
                        "profile_id": profile.profile_id,
                        "profile_version": profile.profile_version,
                        "route_id": endpoint.route_id(model),
                        "requested_model": model,
                        "reported_model": None,
                        "status": "not_discovered",
                        "http_status": None,
                        "provider_request_id_sha256": None,
                        "finish_reason": None,
                        "usage": None,
                        "cost": {"status": "unknown", "reason": "not_called"},
                        "error_category": "catalog_exact_model_absent",
                    }
                )
                continue
            results.append(
                await _smoke_one(
                    forwarder, profile, model, quotes, spec["provider"], spec["region"]
                )
            )
        return {
            "schema_version": "pvian.gateway-forwarder-smoke/v1",
            "evidence_class": "gateway_forwarder_smoke",
            "catalog_gets": 1,
            "catalog_http_status": catalog_response.status_code,
            "catalog_request_id_sha256": catalog_request_id,
            "completion_calls_attempted": sum(
                1 for item in results if item["status"] != "not_discovered"
            ),
            "results": results,
        }


def main() -> int:
    arguments = _arguments()
    _require_live_authorization(arguments)
    load_dotenv(PROJECT_ROOT / ".env", override=False)
    spec = PROVIDER_SETS[arguments.provider_set]
    result = asyncio.run(_run(spec))
    spec["artifact"].write_text(
        json.dumps(result, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n",
        encoding="utf-8",
    )
    print(json.dumps(result, ensure_ascii=False, sort_keys=True, separators=(",", ":")))
    return 0 if all(item["status"] == "observed_supported" for item in result["results"]) else 2


if __name__ == "__main__":
    raise SystemExit(main())
