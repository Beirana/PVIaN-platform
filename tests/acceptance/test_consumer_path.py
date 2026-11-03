from __future__ import annotations

import runpy


def test_pvi_unavailable_does_not_replace_existing_model_path(project_root):
    module = runpy.run_path(str(project_root / "examples" / "platform-pvi-client.py"))
    CapabilityCheck = module["CapabilityCheck"]
    run_with_check = module["run_with_advisory_capability_check"]
    calls = []

    class UnavailableClient:
        def resolve(self, query):
            return CapabilityCheck("unavailable", None, "ConnectionError")

    def existing_model_call():
        calls.append("called")
        return {"result": "existing-path-result"}

    result, check = run_with_check(
        existing_model_call,
        UnavailableClient(),
        {"model": "fixture-model-alpha"},
    )
    assert calls == ["called"]
    assert result == {"result": "existing-path-result"}
    assert check.status == "unavailable"


def test_fixture_directories_a1_to_a6_exist(project_root):
    fixture_root = project_root / "fixtures" / "capability"
    ids = {path.name.split("-", 1)[0].upper() for path in fixture_root.iterdir()}
    assert ids == {"A1", "A2", "A3", "A4", "A5", "A6"}
