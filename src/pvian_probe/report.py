from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from pvian_probe.config import EndpointSettings, ModelTarget
from pvian_probe.probes import CAPABILITIES, ProbeObservation

STATUS_LABELS = {
    "observed_supported": "✅ observed",
    "not_observed": "◻ not observed",
    "inconclusive": "⚠ inconclusive",
}


def write_report_bundle(
    *,
    repository_root: Path,
    settings: EndpointSettings,
    targets: list[ModelTarget],
    observations: list[ProbeObservation],
    catalog_total: int,
) -> dict[str, Path]:
    generated_at = datetime.now(UTC)
    session_id = generated_at.strftime("yunwu-%Y%m%dT%H%M%SZ")
    evidence_dir = repository_root / "evidence" / session_id
    reports_dir = repository_root / "reports"
    evidence_dir.mkdir(parents=True, exist_ok=False)
    reports_dir.mkdir(parents=True, exist_ok=True)

    endpoint = settings.safe_summary()
    endpoint.pop("key_configured", None)
    evidence = {
        "schema_version": "pvian.vendor-matrix-evidence/v1",
        "session_id": session_id,
        "generated_at": _timestamp(generated_at),
        "endpoint": endpoint,
        "model_catalog_total": catalog_total,
        "contains_credentials": False,
        "contains_raw_prompts": False,
        "contains_raw_responses": False,
        "tool_execution_count": 0,
        "observations": [item.to_dict() for item in observations],
    }
    evidence_path = evidence_dir / "observations.json"
    _write_json(evidence_path, evidence)
    evidence_sha256 = hashlib.sha256(evidence_path.read_bytes()).hexdigest()

    matrix = _build_matrix(
        generated_at=generated_at,
        endpoint=endpoint,
        targets=targets,
        observations=observations,
        evidence_path=evidence_path.relative_to(repository_root),
        evidence_sha256=evidence_sha256,
        catalog_total=catalog_total,
    )
    json_path = reports_dir / "yunwu-4x3-capability-matrix.json"
    markdown_path = reports_dir / "yunwu-4x3-capability-matrix.md"
    _write_json(json_path, matrix)
    _write_text(markdown_path, _render_markdown(matrix))
    return {
        "evidence": evidence_path,
        "json_report": json_path,
        "markdown_report": markdown_path,
    }


def _build_matrix(
    *,
    generated_at: datetime,
    endpoint: dict[str, Any],
    targets: list[ModelTarget],
    observations: list[ProbeObservation],
    evidence_path: Path,
    evidence_sha256: str,
    catalog_total: int,
) -> dict[str, Any]:
    index = {(item.vendor, item.capability): item.to_dict() for item in observations}
    return {
        "schema_version": "pvian.vendor-capability-matrix/v1",
        "title": ("Provider Vendor Ingress & Normalization Platform Yunwu 4x3 Capability Matrix"),
        "generated_at": _timestamp(generated_at),
        "acceptance_point": "AP-01-yunwu-provider-capability-reference",
        "endpoint": endpoint,
        "model_catalog_total": catalog_total,
        "thinking_definition": (
            "Native API reasoning exposure only: separate reasoning/thinking field "
            "or non-zero reasoning token accounting. Answer correctness and ordinary "
            "visible explanation do not count."
        ),
        "status_definitions": {
            "observed_supported": "Positive synthetic observation in this run.",
            "not_observed": "Request completed but the target signal was not observed.",
            "inconclusive": "The request or result shape did not support a conclusion.",
        },
        "targets": [
            {
                "vendor": target.vendor,
                "requested_model": target.model,
                "results": {
                    capability: index[(target.vendor, capability)] for capability in CAPABILITIES
                },
            }
            for target in targets
        ],
        "evidence": {
            "path": evidence_path.as_posix(),
            "sha256": evidence_sha256,
            "redaction_profile": "metadata-only/v1",
            "manual_review_required": True,
        },
        "limitations": [
            "Results apply only to the exact model IDs, Yunwu gateway, configuration, and time.",
            "A gateway-reported model ID does not prove upstream model identity.",
            (
                "No business tool was executed; tool calling validates structured call "
                "generation only."
            ),
            "A single negative or absent signal is not a permanent vendor-wide unsupported claim.",
            "No raw prompt, raw response, credential, or sensitive header is retained.",
        ],
    }


def _render_markdown(matrix: dict[str, Any]) -> str:
    lines = [
        "# Yunwu 4×3 Capability Matrix",
        "",
        ("**验收参考点：** `" + matrix["acceptance_point"] + "`"),
        "",
        f"**生成时间：** `{matrix['generated_at']}`",
        "",
        "**端点类型：** `broker_gateway`",
        "",
        "**API：** `OpenAI-compatible Chat Completions`",
        "",
        "## 能力矩阵",
        "",
        "| Vendor / exact model | Message | Thinking exposure | Native tool call |",
        "|---|---:|---:|---:|",
    ]
    for target in matrix["targets"]:
        results = target["results"]
        lines.append(
            "| "
            + f"**{target['vendor']}** / `{target['requested_model']}`"
            + " | "
            + _cell(results["message"])
            + " | "
            + _cell(results["thinking"], thinking=True)
            + " | "
            + _cell(results["tool_calling"])
            + " |"
        )
    lines.extend(
        [
            "",
            "## 判定口径",
            "",
            "- `✅ observed`：本次合成请求观察到目标信号。",
            "- `◻ not observed`：请求完成，但未观察到目标信号；不等同厂商永久不支持。",
            "- `⚠ inconclusive`：请求错误或返回形状不足以得出结论。",
            "- Thinking 只统计独立 reasoning/thinking 字段或 reasoning token；普通解释文字不计。",
            "- Tool Calling 只验证结构化调用生成，不执行工具。",
            "",
            "## 证据与限制",
            "",
            f"- Evidence：`{matrix['evidence']['path']}`",
            f"- SHA-256：`{matrix['evidence']['sha256']}`",
            "- 证据仅包含元数据、摘要、usage、延迟和脱敏错误。",
            "- 结果只适用于表中的精确模型 ID、Yunwu 网关和本次测试时间。",
            "- 网关返回的模型名不能单独证明真实上游模型身份或协议完全保真。",
            "",
        ]
    )
    return "\n".join(lines)


def _cell(observation: dict[str, Any], *, thinking: bool = False) -> str:
    label = STATUS_LABELS[observation["status"]]
    detail = f"{observation['latency_ms']} ms"
    if thinking and observation["status"] == "observed_supported":
        exposure = observation["details"].get("reasoning_exposure")
        detail = f"{exposure}; {detail}"
    if observation.get("error"):
        detail = f"{observation['error']['type']}; {detail}"
    return f"{label}<br><sub>{detail}</sub>"


def _write_json(path: Path, value: dict[str, Any]) -> None:
    _write_text(path, json.dumps(value, ensure_ascii=False, indent=2) + "\n")


def _write_text(path: Path, value: str) -> None:
    path.write_bytes(value.encode("utf-8"))


def _timestamp(value: datetime) -> str:
    return value.isoformat().replace("+00:00", "Z")
