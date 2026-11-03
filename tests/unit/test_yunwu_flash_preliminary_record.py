from __future__ import annotations


def test_yunwu_flash_preliminary_failure_is_registered_without_alias(
    repository, project_root
) -> None:
    profile = repository.profiles["cap-yunwu-deepseek-v4-flash"]

    assert profile.subject.model_requested == "deepseek-v4-flash"
    assert profile.subject.model_reported is None
    assert profile.assurance.operational_assurance.value == "failed"
    for capability in ("messages", "streaming", "reasoning", "tool_calling", "usage_context"):
        assert profile.model_api_capabilities[capability]["effective"] == "inconclusive"

    run = repository.probe_runs[
        "probe-run-yunwu-deepseek-v4-flash-preliminary-20260803"
    ]
    assert run.verdict == "inconclusive"

    aliases = (project_root / "config" / "public-model-aliases.json").read_text(
        encoding="utf-8"
    )
    assert "pvian/yunwu/deepseek-v4-flash" not in aliases
