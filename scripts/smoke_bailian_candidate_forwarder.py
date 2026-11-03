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
TARGET_PROFILE_IDS = (
    "provider-bailian-cn-beijing-qwen3-8-max",
    "provider-bailian-cn-beijing-kimi-k3",
    "provider-bailian-cn-beijing-glm-5-2",
    "provider-bailian-cn-beijing-qwen3-7-plus",
    "provider-bailian-cn-beijing-qwen3-vl-plus",
    "provider-bailian-cn-beijing",
    "provider-bailian-cn-beijing-minimax-m3",
    "provider-bailian-cn-beijing-xiaomi-mimo-v2-5-pro",
)
SMOKE_PROMPT = "Reply with exactly: PVI_GATEWAY_SMOKE_OK"


def _arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Run one bounded, content-free Bailian forwarder smoke per candidate model."
    )
    parser.add_argument("--allow-network", action="store_true")
    parser.add_argument("--confirm-cost", action="store_true")
    return parser.parse_args()


def _require_live_authorization(arguments: argparse.Namespace) -> None:
    if not arguments.allow_network or not arguments.confirm_cost:
        raise SystemExit("live smoke requires --allow-network and --confirm-cost")


def _safe_request_id(headers: dict[str, str]) -> str | None:
    for name in ("x-request-id", "request-id", "x-dashscope-request-id", "x-acs-request-id"):
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
) -> dict[str, Any]:
    endpoint_definition = profile.endpoints[0]
    endpoint = profile.resolve_endpoint(endpoint_definition)
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
        quote = select_quote(quotes, "bailian", "cn-beijing", model)
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


async def _run() -> dict[str, Any]:
    profiles = load_profiles(PROJECT_ROOT / "config" / "provider-profiles")
    selected = [profiles[profile_id] for profile_id in TARGET_PROFILE_IDS]
    targets = [(profile, profile.targets[0].logical_model_id) for profile in selected]
    exact_models = [
        "qwen3.8-max",
        "kimi/kimi-k3",
        "glm-5.2",
        "qwen3.7-plus",
        "qwen3-vl-plus",
        "deepseek-v4-pro",
        "MiniMax/MiniMax-M3",
        "xiaomi/mimo-v2.5-pro",
    ]
    for (profile, _), model in zip(targets, exact_models, strict=True):
        if not profile.declares_upstream_model(model):
            raise RuntimeError("candidate profile does not declare its exact model")

    first = selected[0]
    endpoint = first.resolve_endpoint(first.endpoints[0])
    credential = first.credential()
    timeout = max(_probe_policy(profile).timeout_seconds for profile in selected)
    async with httpx.AsyncClient(timeout=timeout, follow_redirects=False) as client:
        catalog_response = await client.get(
            endpoint.base_url.rstrip("/") + "/models",
            headers={"authorization": f"Bearer {credential}", "accept": "application/json"},
        )
        catalog_request_id = _safe_request_id(dict(catalog_response.headers))
        if catalog_response.status_code != 200:
            return {
                "schema_version": "pvian.gateway-forwarder-smoke/v1",
                "evidence_class": "gateway_forwarder_smoke",
                "catalog_gets": 1,
                "catalog_http_status": catalog_response.status_code,
                "catalog_request_id_sha256": catalog_request_id,
                "completion_calls_attempted": 0,
                "results": [],
            }
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
        calls = 0
        for profile, model in zip(selected, exact_models, strict=True):
            if model not in catalog_ids:
                results.append(
                    {
                        "profile_id": profile.profile_id,
                        "profile_version": profile.profile_version,
                        "route_id": f"bailian-cn-beijing:{model}",
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
            calls += 1
            if calls > 8:
                raise RuntimeError("completion call budget exceeded")
            results.append(await _smoke_one(forwarder, profile, model, quotes))
        return {
            "schema_version": "pvian.gateway-forwarder-smoke/v1",
            "evidence_class": "gateway_forwarder_smoke",
            "catalog_gets": 1,
            "catalog_http_status": catalog_response.status_code,
            "catalog_request_id_sha256": catalog_request_id,
            "completion_calls_attempted": calls,
            "results": results,
        }


def main() -> int:
    arguments = _arguments()
    _require_live_authorization(arguments)
    load_dotenv(PROJECT_ROOT / ".env", override=False)
    result = asyncio.run(_run())
    print(json.dumps(result, ensure_ascii=False, sort_keys=True, separators=(",", ":")))
    return 0 if all(item["status"] == "observed_supported" for item in result["results"]) else 2


if __name__ == "__main__":
    raise SystemExit(main())
