from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from pvi.probe_runner.client import (
    ProbeDependencyError,
    create_openai_compatible_client,
)
from pvi.probe_runner.gate import (
    NetworkAuthorizationError,
    require_network_authorization,
)
from pvian_probe.config import ConfigurationError, EndpointSettings, load_model_targets
from pvian_probe.probes import MatrixProbeRunner
from pvian_probe.report import write_report_bundle

PROJECT_ROOT = Path(__file__).resolve().parents[2]


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="pvian-probe",
        description="Run the controlled Yunwu 4x3 provider capability matrix.",
    )
    parser.add_argument(
        "--models",
        type=Path,
        default=PROJECT_ROOT / "config" / "models.json",
    )
    parser.add_argument("--allow-network", action="store_true")
    parser.add_argument("--confirm-cost", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        require_network_authorization(
            allow_network=args.allow_network,
            confirm_cost=args.confirm_cost,
        )
        settings = EndpointSettings.from_env()
        targets = load_model_targets(args.models)
        client = create_openai_compatible_client(settings)
        catalog = client.models.list()
        catalog_total = len(catalog.data)
        available_ids = {item.id for item in catalog.data}
        missing = [target.model for target in targets if target.model not in available_ids]
        if missing:
            raise ConfigurationError(
                "configured models are absent from the live catalog: " + ", ".join(missing)
            )
        observations = MatrixProbeRunner(client, settings).run(targets)
        paths = write_report_bundle(
            repository_root=PROJECT_ROOT,
            settings=settings,
            targets=targets,
            observations=observations,
            catalog_total=catalog_total,
        )
        result = {
            "requests_attempted": len(observations),
            "results": {
                status: sum(item.status == status for item in observations)
                for status in (
                    "observed_supported",
                    "not_observed",
                    "inconclusive",
                )
            },
            "files": {name: str(path) for name, path in paths.items()},
            "manual_review_required": True,
        }
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    except (
        ConfigurationError,
        NetworkAuthorizationError,
        ProbeDependencyError,
    ) as exc:
        print(f"pvian-probe: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
