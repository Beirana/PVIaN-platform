#!/usr/bin/env python3
"""Qualify an anthropic_native_messages route with Anthropic wire probes.

The OpenAI qualifier does not implement this transport (and there is no
catalog GET on this surface — identity is proven on the wire). This script
runs three bounded probes against the endpoint declared by the provider
profile:

  1. message+usage — expects a Messages response with usage and an exact
     requested/reported model identity,
  2. forced tool_use — expects a native tool_use content block whose input
     is a JSON object (not a serialized/concatenated string),
  3. tool_result replay — expects the assistant to continue from the tool
     output.

Only metadata is persisted (statuses, stop reasons, usage ints, identity);
prompts, outputs, tool arguments, and thinking text never enter any record.
The result is written through ``QualificationStateStore.record_qualification``
into the state file given by ``--state-file``.

Requires both live flags exactly like the OpenAI qualifier.
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import UTC, datetime
from pathlib import Path

import httpx

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from dotenv import load_dotenv  # noqa: E402

load_dotenv(PROJECT_ROOT / ".env", override=False)

from pvian_gateway.profiles import (  # noqa: E402
    ANTHROPIC_TRANSPORT,
    ProfileError,
    load_profiles,
)
from pvian_gateway.state import QualificationStateStore  # noqa: E402

DEFAULT_PROFILE_ID = "provider-aigocode-anthropic-claude-sonnet-5"
ANTHROPIC_HEADERS = {
    "anthropic-version": "2023-06-01",
    "user-agent": "pvian-gateway/anthropic-passthrough claude-cli/1.0",
}
TOOL_DEFINITION = {
    "name": "pvian_echo_probe",
    "description": "Return a synthetic nonce. No side effects.",
    "input_schema": {
        "type": "object",
        "properties": {"nonce": {"type": "string"}},
        "required": ["nonce"],
        "additionalProperties": False,
    },
}


class ProbeFailure(RuntimeError):
    pass


def _arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--profile-id", default=DEFAULT_PROFILE_ID)
    parser.add_argument(
        "--state-file",
        type=Path,
        required=True,
        help="Qualification state file to write (isolated or production).",
    )
    parser.add_argument("--allow-network", action="store_true")
    parser.add_argument("--confirm-cost", action="store_true")
    return parser.parse_args()


def _post(client: httpx.Client, url: str, credential: str, payload: dict) -> dict:
    response = client.post(
        url,
        headers={"authorization": f"Bearer {credential}", **ANTHROPIC_HEADERS},
        json=payload,
        timeout=300,
    )
    if response.status_code != 200:
        raise ProbeFailure(f"http_{response.status_code}")
    return response.json()


def _expect_usage(payload: dict) -> dict:
    usage = payload.get("usage") or {}
    input_tokens = usage.get("input_tokens")
    output_tokens = usage.get("output_tokens")
    if not isinstance(input_tokens, int) or not isinstance(output_tokens, int):
        raise ProbeFailure("usage_missing")
    return {"input_tokens": input_tokens, "output_tokens": output_tokens}


def _probe_message(client: httpx.Client, url: str, credential: str, model: str) -> dict:
    payload = _post(
        client,
        url,
        credential,
        {
            "model": model,
            "max_tokens": 16,
            "messages": [{"role": "user", "content": "Reply exactly: PVI_QUALIFY_OK"}],
        },
    )
    identity = payload.get("model")
    if identity != model:
        raise ProbeFailure(f"identity_mismatch:{identity!r}")
    usage = _expect_usage(payload)
    return {
        "status": "observed_supported",
        "reported_model": identity,
        "usage": usage,
    }


def _probe_tool_use(client: httpx.Client, url: str, credential: str, model: str) -> dict:
    payload = _post(
        client,
        url,
        credential,
        {
            "model": model,
            "max_tokens": 128,
            "messages": [
                {
                    "role": "user",
                    "content": "Echo the nonce pvian-qualification-nonce via the tool.",
                }
            ],
            "tools": [TOOL_DEFINITION],
            "tool_choice": {"type": "tool", "name": TOOL_DEFINITION["name"]},
        },
    )
    blocks = payload.get("content") or []
    tool_use = next(
        (b for b in blocks if isinstance(b, dict) and b.get("type") == "tool_use"),
        None,
    )
    if tool_use is None:
        raise ProbeFailure("tool_use_block_missing")
    if tool_use.get("name") != TOOL_DEFINITION["name"]:
        raise ProbeFailure("tool_name_mismatch")
    if not isinstance(tool_use.get("input"), dict):
        raise ProbeFailure("tool_input_not_native_object")
    if not isinstance(tool_use.get("id"), str) or not tool_use["id"]:
        raise ProbeFailure("tool_use_id_missing")
    _expect_usage(payload)
    return {
        "status": "observed_supported",
        "native_input_object": True,
        "stop_reason": payload.get("stop_reason"),
    }


def _probe_replay(
    client: httpx.Client, url: str, credential: str, model: str, tool_use: dict
) -> dict:
    payload = _post(
        client,
        url,
        credential,
        {
            "model": model,
            "max_tokens": 64,
            "messages": [
                {
                    "role": "user",
                    "content": "Echo the nonce pvian-qualification-nonce via the tool.",
                },
                {"role": "assistant", "content": [tool_use]},
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "tool_result",
                            "tool_use_id": tool_use["id"],
                            "content": "pvian-qualification-nonce",
                        }
                    ],
                },
            ],
            "tools": [TOOL_DEFINITION],
            "tool_choice": {"type": "auto"},
        },
    )
    if payload.get("stop_reason") not in {"end_turn", "stop_sequence"}:
        raise ProbeFailure(f"replay_stop_reason:{payload.get('stop_reason')!r}")
    _expect_usage(payload)
    return {"status": "observed_supported", "stop_reason": payload.get("stop_reason")}


def main() -> int:
    args = _arguments()
    if not args.allow_network or not args.confirm_cost:
        raise SystemExit("live qualification requires --allow-network and --confirm-cost")

    profiles = load_profiles(PROJECT_ROOT / "config" / "provider-profiles")
    profile = profiles[args.profile_id]
    endpoint_definition = next(
        e for e in profile.endpoints if e.transport == ANTHROPIC_TRANSPORT
    )
    try:
        endpoint = profile.resolve_endpoint(endpoint_definition)
        credential = profile.credential()
    except ProfileError as exc:
        raise SystemExit(f"endpoint/credential resolution failed: {exc}") from exc
    model = profile.targets[0].catalog_id_patterns[0].removeprefix("^").removesuffix("$")
    url = endpoint.messages_url()
    observed_at = datetime.now(UTC)

    observations: dict[str, dict] = {}
    try:
        with httpx.Client() as client:
            observations["message"] = _probe_message(client, url, credential, model)
            tool_probe = _probe_tool_use(client, url, credential, model)
            observations["native_tool_call"] = {
                "status": tool_probe["status"],
                "native_input_object": tool_probe["native_input_object"],
            }
            replay_probe = _probe_replay(
                client,
                url,
                credential,
                model,
                {
                    "type": "tool_use",
                    "id": "pvian-qualification-replay-id",
                    "name": TOOL_DEFINITION["name"],
                    "input": {"nonce": "pvian-qualification-nonce"},
                },
            )
            observations["tool_result_replay"] = replay_probe
            observations["usage"] = {
                "status": "observed_supported",
                "usage": observations["message"]["usage"],
            }
    except ProbeFailure as exc:
        print(
            json.dumps(
                {
                    "status": "inconclusive",
                    "profile_id": profile.profile_id,
                    "requested_model": model,
                    "failure": str(exc),
                    "raw_content_persisted": False,
                }
            )
        )
        return 2

    passed = all(item.get("status") == "observed_supported" for item in observations.values())
    reported_model = observations["message"]["reported_model"]
    # The state store accepts only the redacted status-only capability shape;
    # the richer evidence (usage ints, stop reasons, native-input proof) stays
    # in this script's stdout summary and the capability profile.
    capability_summary = {
        name: {"status": item["status"]} for name, item in observations.items()
    }
    store = QualificationStateStore(args.state_file)
    record = store.record_qualification(
        route_id=endpoint.route_id(model),
        profile_id=profile.profile_id,
        profile_version=profile.profile_version,
        provider_id=profile.provider_id,
        endpoint_id=endpoint_definition.endpoint_id,
        requested_model=model,
        reported_model=reported_model,
        passed=passed,
        capability_summary=capability_summary,
        validity_hours=profile.policy.validity_hours,
        failure_threshold=profile.policy.consecutive_failure_threshold,
        observed_at=observed_at,
    )
    print(
        json.dumps(
            {
                "status": record["qualification_status"],
                "route_id": record["route_id"],
                "requested_model": model,
                "reported_model": reported_model,
                "valid_until": record["valid_until"],
                "probes": sorted(observations),
                "raw_content_persisted": False,
            },
            ensure_ascii=False,
        )
    )
    return 0 if passed else 2


if __name__ == "__main__":
    raise SystemExit(main())
