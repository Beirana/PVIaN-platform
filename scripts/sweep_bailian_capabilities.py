from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path

from dotenv import load_dotenv
from openai import OpenAI

from pvi.probe_runner.capability_sweep import (
    TARGET_MODELS,
    SweepError,
    run_capability_sweep,
    validate_sweep_artifact,
)
from pvian_gateway.accounting import load_pricing
from pvian_gateway.profiles import ProviderProfile, load_profiles

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SCHEMA_PATH = PROJECT_ROOT / "contracts" / "schemas" / "capability-sweep.schema.json"
PROFILE_IDS = (
    "provider-bailian-cn-beijing-qwen3-8-max",
    "provider-bailian-cn-beijing-kimi-k3",
    "provider-bailian-cn-beijing-glm-5-2",
    "provider-bailian-cn-beijing-qwen3-7-plus",
    "provider-bailian-cn-beijing-qwen3-vl-plus",
    "provider-bailian-cn-beijing",
    "provider-bailian-cn-beijing-minimax-m3",
    "provider-bailian-cn-beijing-xiaomi-mimo-v2-5-pro",
)


def _arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Run the bounded 8-model/58-call Bailian capability sweep."
    )
    parser.add_argument("--allow-network", action="store_true")
    parser.add_argument("--confirm-cost", action="store_true")
    parser.add_argument("--output-path", type=Path, required=True)
    parser.add_argument("--supersedes", type=Path)
    return parser.parse_args()


def _profiles() -> dict[str, ProviderProfile]:
    loaded = load_profiles(PROJECT_ROOT / "config" / "provider-profiles")
    return {
        model: loaded[profile_id]
        for model, profile_id in zip(TARGET_MODELS, PROFILE_IDS, strict=True)
    }


def _evidence_paths(output_path: Path) -> tuple[Path, Path]:
    return (
        output_path.with_name(output_path.name + ".pending.json"),
        output_path.with_name(output_path.name + ".rejected.json"),
    )


def _sha256_file(path: Path) -> str:
    if not path.is_file():
        raise FileNotFoundError("superseded evidence must be an existing regular file")
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return "sha256:" + digest.hexdigest()


def _persist_artifact(artifact: dict[str, object], output_path: Path) -> tuple[str, Path]:
    """Persist before validation so a safe failed artifact is never lost.

    Exclusive creation protects the target, pending, and rejected paths from
    overwrite. On this supported Windows host, ``Path.rename`` is atomic and
    fails rather than replacing an existing destination.
    """

    output_path = output_path.resolve()
    pending_path, rejected_path = _evidence_paths(output_path)
    if any(path.exists() for path in (output_path, pending_path, rejected_path)):
        raise FileExistsError("refusing to overwrite existing evidence path")
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with pending_path.open("x", encoding="utf-8") as handle:
        json.dump(artifact, handle, ensure_ascii=False, indent=2, sort_keys=True)
        handle.write("\n")
    try:
        validate_sweep_artifact(artifact, SCHEMA_PATH)
    except SweepError:
        if rejected_path.exists():
            raise FileExistsError(
                "refusing to overwrite existing rejected evidence"
            ) from None
        pending_path.rename(rejected_path)
        return "artifact_rejected", rejected_path
    if output_path.exists():
        raise FileExistsError("refusing to overwrite existing validated evidence")
    pending_path.rename(output_path)
    return "artifact_written", output_path


def main() -> int:
    arguments = _arguments()
    if not arguments.allow_network or not arguments.confirm_cost:
        raise SystemExit("live sweep requires --allow-network and --confirm-cost")
    output_path = arguments.output_path.resolve()
    pending_path, rejected_path = _evidence_paths(output_path)
    if any(path.exists() for path in (output_path, pending_path, rejected_path)):
        raise SystemExit("refusing to overwrite an existing evidence path")
    try:
        supersedes_sha256 = (
            _sha256_file(arguments.supersedes.resolve())
            if arguments.supersedes is not None
            else None
        )
    except FileNotFoundError as exc:
        raise SystemExit(str(exc)) from None

    load_dotenv(PROJECT_ROOT / ".env", override=False)
    profiles = _profiles()
    first = next(iter(profiles.values()))
    endpoint_definition = next(
        item for item in first.endpoints if item.qualification_enabled
    )
    endpoint = first.resolve_endpoint(endpoint_definition)
    timeout = max(
        profile.policy.probe_request_policy.timeout_seconds
        if profile.policy.probe_request_policy is not None
        else 60
        for profile in profiles.values()
    )
    client = OpenAI(
        base_url=endpoint.base_url,
        api_key=first.credential(),
        timeout=timeout,
        max_retries=0,
    )
    artifact = run_capability_sweep(
        client=client,
        profiles_by_model=profiles,
        quotes=load_pricing(PROJECT_ROOT / "config" / "pricing"),
        supersedes_sha256=supersedes_sha256,
        sweep_revision="v2",
    )
    try:
        persistence_status, persisted_path = _persist_artifact(artifact, output_path)
    except FileExistsError as exc:
        raise SystemExit(str(exc)) from None
    print(
        json.dumps(
            {
                "status": persistence_status,
                "output_path": str(persisted_path),
                "catalog_gets": artifact["catalog_gets"],
                "completion_calls_attempted": artifact["completion_calls_attempted"],
                "completion_call_budget": artifact["completion_call_budget"],
                "global_stop": artifact["global_stop"],
            },
            separators=(",", ":"),
        )
    )
    return (
        0
        if persistence_status == "artifact_written" and artifact["global_stop"] is None
        else 2
    )


if __name__ == "__main__":
    raise SystemExit(main())
