from __future__ import annotations

from pvi.probe_runner.qualification import _with_overrides


def test_vendor_thinking_extension_is_sent_via_extra_body() -> None:
    assert _with_overrides({"enable_thinking": False}) == {"extra_body": {"enable_thinking": False}}


def test_standard_reasoning_effort_remains_a_top_level_sdk_argument() -> None:
    assert _with_overrides({"reasoning_effort": "high"}) == {"reasoning_effort": "high"}
