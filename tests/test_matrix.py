from __future__ import annotations

import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from pvian_probe.cli import main as matrix_cli_main
from pvian_probe.config import (
    ConfigurationError,
    EndpointSettings,
    ModelTarget,
)
from pvian_probe.probes import MatrixProbeRunner, _collect_stream
from pvian_probe.report import write_report_bundle

TEST_SECRET = "test-secret-never-write"


class FakeCompletions:
    def create(self, **request):
        usage = SimpleNamespace(
            prompt_tokens=8,
            completion_tokens=4,
            total_tokens=12,
            completion_tokens_details=SimpleNamespace(reasoning_tokens=2),
        )
        if request.get("tools"):
            message = SimpleNamespace(
                content=None,
                tool_calls=[
                    SimpleNamespace(
                        function=SimpleNamespace(
                            name="pvi_matrix_probe",
                            arguments='{"nonce":"pvian-matrix-nonce"}',
                        )
                    )
                ],
                model_extra={},
            )
            finish_reason = "tool_calls"
        elif "Repeat exactly eight times" in request["messages"][0]["content"]:
            message = SimpleNamespace(
                content="26",
                tool_calls=None,
                model_extra={"reasoning_content": "synthetic reasoning"},
            )
            finish_reason = "stop"
        else:
            message = SimpleNamespace(
                content="PVI_MESSAGE_OK",
                tool_calls=None,
                model_extra={},
            )
            finish_reason = "stop"
        return SimpleNamespace(
            model=request["model"] + "-reported",
            choices=[
                SimpleNamespace(
                    message=message,
                    finish_reason=finish_reason,
                )
            ],
            usage=usage,
        )


class FakeClient:
    def __init__(self) -> None:
        self.chat = SimpleNamespace(completions=FakeCompletions())


def settings() -> EndpointSettings:
    return EndpointSettings(
        base_url="https://yunwu.ai/v1",
        api_key=TEST_SECRET,
    )


def targets() -> list[ModelTarget]:
    return [
        ModelTarget(
            vendor="openai",
            model="synthetic-openai",
            token_limit_parameter="max_completion_tokens",
            reasoning_effort="low",
        ),
        ModelTarget(
            vendor="deepseek",
            model="synthetic-deepseek",
            token_limit_parameter="max_tokens",
            reasoning_effort=None,
        ),
        ModelTarget(
            vendor="grok",
            model="synthetic-grok",
            token_limit_parameter="max_tokens",
            reasoning_effort=None,
        ),
        ModelTarget(
            vendor="glm",
            model="synthetic-glm",
            token_limit_parameter="max_tokens",
            reasoning_effort=None,
            stream=False,
        ),
    ]


def test_endpoint_settings_require_yunwu_https() -> None:
    configured = EndpointSettings.from_env(
        {
            "PVI_PROBE_YUNWU_API_KEY": TEST_SECRET,
            "PVI_PROBE_YUNWU_BASE_URL": "https://yunwu.ai/v1/",
        }
    )
    assert configured.base_url == "https://yunwu.ai/v1"
    assert "api_key" not in configured.safe_summary()

    with pytest.raises(ConfigurationError):
        EndpointSettings.from_env(
            {
                "PVI_PROBE_YUNWU_API_KEY": TEST_SECRET,
                "PVI_PROBE_YUNWU_BASE_URL": "https://example.com/v1",
            }
        )


def test_matrix_cli_uses_shared_offline_network_gate(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setenv("PVI_NETWORK_DISABLED", "1")

    assert matrix_cli_main(["--allow-network", "--confirm-cost"]) == 1
    assert "network probes are disabled" in capsys.readouterr().err


def test_matrix_runs_exactly_four_by_three_without_tool_execution() -> None:
    observations = MatrixProbeRunner(FakeClient(), settings()).run(targets())

    assert len(observations) == 12
    assert all(item.status == "observed_supported" for item in observations)
    tool_results = [item for item in observations if item.capability == "tool_calling"]
    assert all(item.details["tool_executed"] is False for item in tool_results)
    assert TEST_SECRET not in json.dumps([item.to_dict() for item in observations])


def test_streaming_response_is_normalized_without_executing_tools() -> None:
    chunks = [
        SimpleNamespace(
            model="synthetic-stream",
            usage=None,
            choices=[
                SimpleNamespace(
                    finish_reason=None,
                    delta=SimpleNamespace(
                        content=None,
                        tool_calls=[
                            SimpleNamespace(
                                index=0,
                                id="call_",
                                function=SimpleNamespace(
                                    name="pvi_matrix_",
                                    arguments='{"nonce":"pvian-',
                                ),
                            )
                        ],
                        model_extra={"reasoning_content": "step "},
                    ),
                )
            ],
        ),
        SimpleNamespace(
            model="synthetic-stream",
            usage=SimpleNamespace(
                prompt_tokens=4,
                completion_tokens=3,
                total_tokens=7,
                completion_tokens_details=None,
            ),
            choices=[
                SimpleNamespace(
                    finish_reason="tool_calls",
                    delta=SimpleNamespace(
                        content=None,
                        tool_calls=[
                            SimpleNamespace(
                                index=0,
                                id="1",
                                function=SimpleNamespace(
                                    name="probe",
                                    arguments='matrix-nonce"}',
                                ),
                            )
                        ],
                        model_extra={"reasoning_content": "two"},
                    ),
                )
            ],
        ),
    ]

    response = _collect_stream(iter(chunks))

    assert response.model == "synthetic-stream"
    assert response.choices[0].finish_reason == "tool_calls"
    call = response.choices[0].message.tool_calls[0]
    assert call.function.name == "pvi_matrix_probe"
    assert call.function.arguments == '{"nonce":"pvian-matrix-nonce"}'
    assert response.choices[0].message.model_extra["reasoning_content"] == "step two"


def test_report_bundle_contains_no_secret_or_raw_payload(tmp_path: Path) -> None:
    configured = settings()
    observations = MatrixProbeRunner(FakeClient(), configured).run(targets())
    paths = write_report_bundle(
        repository_root=tmp_path,
        settings=configured,
        targets=targets(),
        observations=observations,
        catalog_total=4,
    )

    all_text = "\n".join(
        path.read_text(encoding="utf-8") for path in tmp_path.rglob("*") if path.is_file()
    )
    assert TEST_SECRET not in all_text
    assert "synthetic reasoning" not in all_text
    assert "Repeat exactly eight times" not in all_text
    assert "contains_credentials" in all_text
    assert paths["json_report"].is_file()
    assert paths["markdown_report"].is_file()
    assert all(b"\r\n" not in path.read_bytes() for path in paths.values())

    report = json.loads(paths["json_report"].read_text(encoding="utf-8"))
    assert (
        report["evidence"]["sha256"] == hashlib.sha256(paths["evidence"].read_bytes()).hexdigest()
    )
