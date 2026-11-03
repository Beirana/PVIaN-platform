from __future__ import annotations

from pvi.infrastructure.repository import RepositoryLoader


def test_flash_qualification_records_match_bailian_only_alias_admission(project_root) -> None:
    repository = RepositoryLoader(
        project_root / "data", project_root / "contracts", validate=True
    ).load()

    yunwu = repository.profiles["cap-yunwu-deepseek-v4-flash-0731"]
    bailian = repository.profiles["cap-bailian-deepseek-v4-flash-0731"]

    assert yunwu.subject.model_requested == "deepseek-v4-flash-0731"
    assert yunwu.subject.model_reported is None
    assert yunwu.model_api_capabilities["messages"]["effective"] == "unknown"

    assert bailian.subject.model_requested == "deepseek-v4-flash-0731"
    assert bailian.subject.model_reported == "deepseek-v4-flash-0731"
    for capability in ("messages", "streaming", "reasoning", "tool_calling", "usage_context"):
        assert bailian.model_api_capabilities[capability]["effective"] == "supported"

    assert "probe-run-yunwu-deepseek-v4-flash-0731-catalog-20260803" in repository.probe_runs
    assert (
        "probe-run-bailian-deepseek-v4-flash-0731-qualification-20260803"
        in repository.probe_runs
    )

    aliases = (project_root / "config" / "public-model-aliases.json").read_text(encoding="utf-8")
    assert "pvian/bailian-cn-beijing/deepseek-v4-flash-0731" in aliases
    assert "pvian/yunwu/deepseek-v4-flash-0731" not in aliases
