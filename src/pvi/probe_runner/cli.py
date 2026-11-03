from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from dotenv import load_dotenv

from pvi.config import PROJECT_ROOT
from pvi.probe_runner.client import (
    ProbeDependencyError,
    create_openai_compatible_client,
)
from pvi.probe_runner.config import PROVIDERS, ProbeConfigurationError, ProbeSettings
from pvi.probe_runner.evidence import write_probe_bundle
from pvi.probe_runner.gate import NetworkAuthorizationError, require_network_authorization
from pvi.probe_runner.qualification import qualify_profile
from pvi.probe_runner.runner import PROBE_ALIASES, OnlineProbeRunner
from pvian_gateway.config import GatewaySettings
from pvian_gateway.profiles import ProfileError, load_profiles
from pvian_gateway.state import QualificationStateError, QualificationStateStore


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="pvi-probe",
        description="Run explicitly authorized online PVI capability probes.",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    check = subparsers.add_parser(
        "check",
        help="Validate local configuration without sending a network request.",
    )
    check.add_argument("--provider", choices=sorted(PROVIDERS), default="yunwu")

    run = subparsers.add_parser(
        "run",
        help="Send selected synthetic probes and write redacted candidate evidence.",
    )
    run.add_argument("--provider", choices=sorted(PROVIDERS), default="yunwu")
    run.add_argument(
        "--probe",
        action="append",
        choices=[*sorted(PROBE_ALIASES), "all"],
        default=[],
        help="Repeat to run multiple probes; defaults to text.",
    )
    run.add_argument(
        "--output-dir",
        type=Path,
        default=PROJECT_ROOT / "artifacts" / "probes",
    )
    run.add_argument("--allow-network", action="store_true")
    run.add_argument("--confirm-cost", action="store_true")

    qualify = subparsers.add_parser(
        "qualify",
        help=(
            "Discover profile-declared models and run controlled qualification. "
            "Writes only redacted local qualification state."
        ),
    )
    qualify.add_argument(
        "--profile-id",
        action="append",
        default=[],
        help="Repeat to qualify selected provider profile IDs; default is all active profiles.",
    )
    qualify.add_argument(
        "--due",
        action="store_true",
        help="Skip routes with a still-valid qualification; recheck expired/degraded routes.",
    )
    qualify.add_argument("--allow-network", action="store_true")
    qualify.add_argument("--confirm-cost", action="store_true")

    repair = subparsers.add_parser(
        "repair-state",
        help=(
            "Offline, network-free repair of known historical qualification-state "
            "compatibility issues. Writes a backup; rejects unknown damage."
        ),
    )
    repair.add_argument(
        "--state-file",
        type=Path,
        default=None,
        help="Path to qualification-state.json; defaults to the gateway configured path.",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    load_dotenv(PROJECT_ROOT / ".env", override=False)
    args = build_parser().parse_args(argv)
    try:
        if args.command == "check":
            settings = ProbeSettings.from_env(args.provider)
            print(json.dumps(settings.safe_summary(), ensure_ascii=False, indent=2))
            print("Configuration valid; no network request was sent.")
            return 0

        if args.command == "qualify":
            require_network_authorization(
                allow_network=args.allow_network,
                confirm_cost=args.confirm_cost,
            )
            gateway_settings = GatewaySettings().resolved()
            profiles = load_profiles(gateway_settings.profile_dir)
            selected = _select_profiles(profiles, args.profile_id)
            state = QualificationStateStore(gateway_settings.state_file)
            results = []
            for profile in selected:
                results.extend(qualify_profile(profile, state, due_only=args.due))
            print(
                json.dumps(
                    {
                        "mode": "due" if args.due else "selected",
                        "results": [item.safe_summary() for item in results],
                        "state_file": str(gateway_settings.state_file),
                        "raw_content_persisted": False,
                    },
                    ensure_ascii=False,
                    indent=2,
                )
            )
            return (
                0
                if results and all(item.qualification_status == "enabled" for item in results)
                else 2
            )

        if args.command == "repair-state":
            gateway_settings = GatewaySettings().resolved()
            state_file = args.state_file or gateway_settings.state_file
            store = QualificationStateStore(state_file)
            result = store.repair_state()
            print(
                json.dumps(
                    {
                        "state_file": str(state_file),
                        **result,
                        "raw_content_persisted": False,
                    },
                    ensure_ascii=False,
                    indent=2,
                )
            )
            return 0

        require_network_authorization(
            allow_network=args.allow_network,
            confirm_cost=args.confirm_cost,
        )
        settings = ProbeSettings.from_env(args.provider)
        probe_names = _resolve_probe_names(args.probe)
        client = create_openai_compatible_client(settings)
        observations = OnlineProbeRunner(client, settings).run(probe_names)
        session_dir = write_probe_bundle(
            output_root=args.output_dir,
            settings=settings,
            observations=observations,
        )
        summary = {
            "session_dir": str(session_dir),
            "results": [
                {
                    "probe_id": item.probe_id,
                    "verdict": item.verdict,
                    "reported_model": item.reported_model,
                    "latency_ms": item.latency_ms,
                }
                for item in observations
            ],
            "promotion_status": "manual_review_required",
        }
        print(json.dumps(summary, ensure_ascii=False, indent=2))
        return 0 if all(item.verdict == "observed_supported" for item in observations) else 2
    except (
        NetworkAuthorizationError,
        ProbeConfigurationError,
        ProbeDependencyError,
        ProfileError,
        QualificationStateError,
    ) as exc:
        print(f"pvi-probe: {exc}", file=sys.stderr)
        return 1


def _resolve_probe_names(values: list[str]) -> list[str]:
    if not values:
        return ["text"]
    if "all" in values:
        return ["text", "tool", "stream"]
    return list(dict.fromkeys(values))


def _select_profiles(profiles, requested_ids: list[str]):
    if not requested_ids:
        return [profile for _, profile in sorted(profiles.items()) if profile.lifecycle == "active"]
    unknown = sorted(set(requested_ids).difference(profiles))
    if unknown:
        raise ProfileError("unknown provider profile IDs: " + ", ".join(unknown))
    return [profiles[profile_id] for profile_id in requested_ids]


if __name__ == "__main__":
    raise SystemExit(main())
