from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class FrozenModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class EndpointKind(StrEnum):
    OFFICIAL_DIRECT = "official_direct"
    MANAGED_CLOUD = "managed_cloud"
    BROKER_GATEWAY = "broker_gateway"
    SELF_HOSTED_GATEWAY = "self_hosted_gateway"
    LOCAL_OPEN_WEIGHTS = "local_open_weights"
    LOCAL_OPAQUE_RUNTIME = "local_opaque_runtime"
    UNKNOWN = "unknown"


class AssuranceStatus(StrEnum):
    VERIFIED = "verified"
    GOVERNED = "governed"
    DECLARED_UNVERIFIED = "declared_unverified"
    UNKNOWN = "unknown"
    FAILED = "failed"


class ProtocolFidelity(StrEnum):
    NATIVE = "native"
    DOCUMENTED_TRANSLATION = "documented_translation"
    OBSERVED_TRANSLATION = "observed_translation"
    LOSSY_TRANSLATION = "lossy_translation"
    UNKNOWN = "unknown"


class CapabilityStatus(StrEnum):
    SUPPORTED = "supported"
    UNSUPPORTED = "unsupported"
    UNKNOWN = "unknown"
    NOT_TESTED = "not_tested"
    NOT_APPLICABLE = "not_applicable"
    INCONCLUSIVE = "inconclusive"


class Assurance(FrozenModel):
    identity_assurance: AssuranceStatus
    model_provenance_assurance: AssuranceStatus
    protocol_fidelity_assurance: AssuranceStatus
    data_handling_assurance: AssuranceStatus = AssuranceStatus.UNKNOWN
    operational_assurance: AssuranceStatus = AssuranceStatus.UNKNOWN
    protocol_fidelity: ProtocolFidelity
    usage_notes: list[str] = Field(default_factory=list)


class Subject(FrozenModel):
    endpoint_id: str = Field(min_length=1)
    endpoint_kind: EndpointKind
    provider_id: str = Field(min_length=1)
    api_surface: str = Field(min_length=1)
    api_version: str = Field(min_length=1)
    model_requested: str = Field(min_length=1)
    model_reported: str | None
    model_revision: str = Field(min_length=1)
    deployment_id: str | None
    region: str | None
    relevant_configuration_digest: str | None
    observed_at: datetime


class EndpointProfile(FrozenModel):
    schema_version: Literal["pvi.endpoint-profile/v1"]
    profile_version: str = Field(min_length=1)
    endpoint_id: str = Field(min_length=1)
    endpoint_kind: EndpointKind
    provider_id: str = Field(min_length=1)
    api_surface: str = Field(min_length=1)
    api_version: str = Field(min_length=1)
    model_requested: str = Field(min_length=1)
    model_reported: str | None
    model_revision: str = Field(min_length=1)
    deployment_id: str | None
    region: str | None
    relevant_configuration_digest: str | None
    observed_at: datetime
    assurance: Assurance


class CapabilityProfile(FrozenModel):
    schema_version: Literal["pvi.capability-profile/v1"]
    profile_id: str = Field(min_length=1)
    profile_version: str = Field(min_length=1)
    subject: Subject
    assurance: Assurance
    model_api_capabilities: dict[str, Any]
    endpoint_capabilities: dict[str, Any]
    runtime_interop: dict[str, Any]
    probe_summary: dict[str, Any]

    @model_validator(mode="after")
    def require_capability_domains(self) -> CapabilityProfile:
        required_model = {
            "messages",
            "multimodal",
            "structured_output",
            "tool_calling",
            "reasoning",
            "usage_context",
            "streaming",
            "sampling_observability",
            "builtin_tools",
        }
        required_endpoint = {
            "stream_transport",
            "usage_reporting",
            "routing_transparency",
            "fallback_behavior",
            "raw_evidence_availability",
            "model_disclosure",
        }
        missing_model = required_model - self.model_api_capabilities.keys()
        missing_endpoint = required_endpoint - self.endpoint_capabilities.keys()
        if missing_model or missing_endpoint:
            raise ValueError(
                "missing capability domains: "
                f"model={sorted(missing_model)}, endpoint={sorted(missing_endpoint)}"
            )
        return self


class EvidenceReference(FrozenModel):
    schema_version: Literal["pvi.evidence-reference/v1"]
    evidence_id: str = Field(min_length=1)
    source_kind: Literal[
        "official_document",
        "provider_native",
        "managed_cloud",
        "broker_gateway",
        "local_runtime",
        "host_bridge",
        "host_hook",
        "session_export",
        "operator_note",
        "unknown",
    ]
    host_profile_id: str | None
    capture_surface: Literal[
        "provider_wire",
        "gateway_wire",
        "host_normalized_event",
        "host_session_record",
        "hook_callback",
        "exported_transcript",
        "operator_observation",
        "unknown",
    ]
    sha256: str | None
    media_type: str
    redaction_profile: str
    locator: str | None
    captured_at: datetime
    retention_class: Literal["ephemeral", "standard", "restricted", "metadata_only"]


class ProbeDefinition(FrozenModel):
    probe_id: str
    probe_version: str
    description: str
    capability_path: str
    offline_only: bool = True


class ProbeCatalog(FrozenModel):
    schema_version: Literal["pvi.probe-catalog/v1"]
    probes: list[ProbeDefinition]


class ProbeRun(FrozenModel):
    schema_version: Literal["pvi.probe-run/v1"]
    probe_run_id: str
    probe_id: str
    probe_version: str
    subject_profile_id: str
    input_digest: str | None
    configuration_digest: str | None
    environment_digest: str | None
    started_at: datetime
    completed_at: datetime
    verdict: Literal[
        "observed_supported",
        "observed_unsupported",
        "inconclusive",
        "not_applicable",
        "aborted",
    ]
    evidence_ids: list[str]
    normalization_error_ids: list[str]


class CapabilityQuery(FrozenModel):
    profile_id: str | None = None
    model: str | None = None
    endpoint_id: str | None = None
    profile_version: str | None = None
    requirements: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def require_selector(self) -> CapabilityQuery:
        if not self.profile_id and not self.model:
            raise ValueError("profile_id or model is required")
        return self


class ResolutionItem(FrozenModel):
    requirement: str
    expected: Any
    actual: Any
    reason: str | None = None
    evidence_ids: list[str] = Field(default_factory=list)


class ResolutionCandidate(FrozenModel):
    profile_id: str
    profile_version: str
    endpoint_id: str
    satisfied: list[ResolutionItem]
    unsatisfied: list[ResolutionItem]
    unknown: list[ResolutionItem]
    profile: CapabilityProfile


class CapabilityResolution(FrozenModel):
    schema_version: Literal["pvi.capability-resolution/v1"]
    query_id: str
    data_version: str
    resolved_at: datetime | None
    candidates: list[ResolutionCandidate]


class Page(BaseModel):
    model_config = ConfigDict(extra="forbid")

    items: list[Any]
    total: int
    limit: int
    offset: int
