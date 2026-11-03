from __future__ import annotations

import json

import yaml

from pvi.config import PROJECT_ROOT
from pvi.main import create_app


def main() -> None:
    path = PROJECT_ROOT / "contracts" / "openapi" / "capability-v1.openapi.yaml"
    with path.open(encoding="utf-8") as handle:
        frozen = yaml.safe_load(handle)
    generated = create_app().openapi()
    if frozen != generated:
        raise SystemExit(
            "frozen OpenAPI differs from implementation; run scripts/export_openapi.py "
            "and review the contract change"
        )
    required_paths = {
        "/v1/endpoints",
        "/v1/endpoints/{endpoint_id}",
        "/v1/capability-profiles",
        "/v1/capability-profiles/{profile_id}",
        "/v1/capability-queries:resolve",
        "/v1/probes",
        "/v1/probe-runs/{run_id}",
        "/v1/evidence/{evidence_id}",
    }
    missing = required_paths - frozen.get("paths", {}).keys()
    if missing:
        raise SystemExit(f"OpenAPI missing frozen paths: {sorted(missing)}")
    json.dumps(frozen)
    print("OpenAPI 3.1 document matches implementation and contains all frozen paths")


if __name__ == "__main__":
    main()
