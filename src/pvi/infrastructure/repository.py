from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, TypeVar

import yaml
from jsonschema import Draft202012Validator
from pydantic import BaseModel

from pvi.domain.models import (
    CapabilityProfile,
    EndpointProfile,
    EvidenceReference,
    ProbeCatalog,
    ProbeRun,
)

ModelT = TypeVar("ModelT", bound=BaseModel)


class DataValidationError(RuntimeError):
    """Raised when versioned PVI data does not satisfy the frozen contracts."""


@dataclass(frozen=True)
class Repository:
    endpoints: dict[str, EndpointProfile]
    profiles: dict[str, CapabilityProfile]
    evidence: dict[str, EvidenceReference]
    probe_catalog: ProbeCatalog
    probe_runs: dict[str, ProbeRun]
    data_version: str


class RepositoryLoader:
    def __init__(self, data_dir: Path, contract_dir: Path, validate: bool = True) -> None:
        self.data_dir = data_dir
        self.schema_dir = contract_dir / "schemas"
        self.validate = validate

    def load(self) -> Repository:
        endpoints = self._load_directory(
            self.data_dir / "endpoints",
            "endpoint-profile.schema.json",
            EndpointProfile,
            "endpoint_id",
        )
        profiles = self._load_directory(
            self.data_dir / "capability-profiles",
            "capability-profile.schema.json",
            CapabilityProfile,
            "profile_id",
        )
        evidence = self._load_directory(
            self.data_dir / "evidence",
            "evidence-reference.schema.json",
            EvidenceReference,
            "evidence_id",
        )
        probe_runs = self._load_directory(
            self.data_dir / "probe-runs",
            "probe-run.schema.json",
            ProbeRun,
            "probe_run_id",
        )
        catalog_data = self._read_yaml(self.data_dir / "catalog" / "probe-catalog.yaml")
        self._validate(catalog_data, "probe-catalog.schema.json", "probe-catalog.yaml")
        catalog = ProbeCatalog.model_validate(catalog_data)
        self._validate_references(endpoints, profiles, evidence, catalog, probe_runs)
        digest_payload = {
            "endpoints": [item.model_dump(mode="json") for item in endpoints.values()],
            "profiles": [item.model_dump(mode="json") for item in profiles.values()],
            "evidence": [item.model_dump(mode="json") for item in evidence.values()],
            "probe_catalog": catalog.model_dump(mode="json"),
            "probe_runs": [item.model_dump(mode="json") for item in probe_runs.values()],
        }
        encoded = json.dumps(
            digest_payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode()
        return Repository(
            endpoints=endpoints,
            profiles=profiles,
            evidence=evidence,
            probe_catalog=catalog,
            probe_runs=probe_runs,
            data_version=f"sha256:{hashlib.sha256(encoded).hexdigest()}",
        )

    def _load_directory(
        self,
        directory: Path,
        schema_name: str,
        model_type: type[ModelT],
        id_field: str,
    ) -> dict[str, ModelT]:
        if not directory.is_dir():
            raise DataValidationError(f"required data directory not found: {directory}")
        result: dict[str, ModelT] = {}
        for path in sorted(directory.glob("*.json")):
            data = self._read_json(path)
            self._validate(data, schema_name, path.name)
            try:
                item = model_type.model_validate(data)
            except Exception as exc:
                raise DataValidationError(
                    f"{path}: runtime model validation failed: {exc}"
                ) from exc
            item_id = getattr(item, id_field)
            if item_id in result:
                raise DataValidationError(f"duplicate {id_field}: {item_id}")
            result[item_id] = item
        return result

    def _validate(self, instance: dict[str, Any], schema_name: str, source_name: str) -> None:
        if not self.validate:
            return
        schema = self._read_json(self.schema_dir / schema_name)
        validator = Draft202012Validator(schema)
        errors = sorted(validator.iter_errors(instance), key=lambda item: list(item.path))
        if errors:
            detail = "; ".join(
                f"{'/'.join(map(str, error.path)) or '<root>'}: {error.message}" for error in errors
            )
            raise DataValidationError(f"{source_name} failed {schema_name}: {detail}")

    @staticmethod
    def _read_json(path: Path) -> dict[str, Any]:
        try:
            with path.open(encoding="utf-8") as handle:
                value = json.load(handle)
        except (OSError, json.JSONDecodeError) as exc:
            raise DataValidationError(f"cannot read JSON {path}: {exc}") from exc
        if not isinstance(value, dict):
            raise DataValidationError(f"{path} must contain a JSON object")
        return value

    @staticmethod
    def _read_yaml(path: Path) -> dict[str, Any]:
        try:
            with path.open(encoding="utf-8") as handle:
                value = yaml.safe_load(handle)
        except (OSError, yaml.YAMLError) as exc:
            raise DataValidationError(f"cannot read YAML {path}: {exc}") from exc
        if not isinstance(value, dict):
            raise DataValidationError(f"{path} must contain a mapping")
        return value

    @staticmethod
    def _collect_evidence_ids(value: Any) -> set[str]:
        found: set[str] = set()
        if isinstance(value, dict):
            for key, child in value.items():
                if key == "evidence_ids" and isinstance(child, list):
                    found.update(str(item) for item in child)
                else:
                    found.update(RepositoryLoader._collect_evidence_ids(child))
        elif isinstance(value, list):
            for child in value:
                found.update(RepositoryLoader._collect_evidence_ids(child))
        return found

    def _validate_references(
        self,
        endpoints: dict[str, EndpointProfile],
        profiles: dict[str, CapabilityProfile],
        evidence: dict[str, EvidenceReference],
        catalog: ProbeCatalog,
        probe_runs: dict[str, ProbeRun],
    ) -> None:
        probe_ids = {item.probe_id for item in catalog.probes}
        for profile in profiles.values():
            endpoint = endpoints.get(profile.subject.endpoint_id)
            if endpoint is None:
                raise DataValidationError(
                    f"profile {profile.profile_id} references unknown endpoint "
                    f"{profile.subject.endpoint_id}"
                )
            if endpoint.endpoint_kind != profile.subject.endpoint_kind:
                raise DataValidationError(
                    f"profile {profile.profile_id} endpoint_kind differs from endpoint"
                )
            evidence_ids = self._collect_evidence_ids(profile.model_dump(mode="json"))
            missing = evidence_ids - evidence.keys()
            if missing:
                raise DataValidationError(
                    f"profile {profile.profile_id} references unknown evidence: {sorted(missing)}"
                )
        for run in probe_runs.values():
            if run.subject_profile_id not in profiles:
                raise DataValidationError(
                    f"probe run {run.probe_run_id} references unknown profile"
                )
            if run.probe_id not in probe_ids:
                raise DataValidationError(f"probe run {run.probe_run_id} references unknown probe")
            missing = set(run.evidence_ids) - evidence.keys()
            if missing:
                raise DataValidationError(
                    f"probe run {run.probe_run_id} references unknown evidence: {sorted(missing)}"
                )
