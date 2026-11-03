from __future__ import annotations

import copy
import hashlib
import json
import sys
import tempfile
import unittest
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
SKILL_DIR = SCRIPT_DIR.parent
FIXTURES = SKILL_DIR / "fixtures"
sys.path.insert(0, str(SCRIPT_DIR))

from offline_preflight import PreflightError, preflight  # noqa: E402
from validate_onboarding_plan import (  # noqa: E402
    PlanError,
    permitted_completion_calls,
    validate_plan,
)


def fixture(name: str) -> dict[str, object]:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def authorized(plan: dict[str, object]) -> dict[str, object]:
    result = copy.deepcopy(plan)
    result["live_authorization_id"] = "authorization-revision-1"
    return result


class PlanValidatorTests(unittest.TestCase):
    def test_valid_plan_has_exact_call_counts_and_no_runtime_cap(self) -> None:
        plan = fixture("valid-plan.json")
        self.assertEqual(
            validate_plan(plan),
            {
                "models": 1,
                "catalog_gets": 1,
                "provider_completion_calls_max": 7,
                "gateway_completion_calls_max": 1,
                "total_completion_calls_max": 8,
            },
        )
        self.assertIsNone(plan["runtime_cost_cap"])
        self.assertIsNone(plan["runtime_token_cap"])

    def test_missing_either_live_flag_permits_zero_calls(self) -> None:
        plan = authorized(fixture("valid-plan.json"))
        self.assertEqual(
            permitted_completion_calls(plan, allow_network=False, confirm_cost=True), 0
        )
        self.assertEqual(
            permitted_completion_calls(plan, allow_network=True, confirm_cost=False), 0
        )
        self.assertEqual(
            permitted_completion_calls(plan, allow_network=False, confirm_cost=False), 0
        )
        self.assertEqual(
            permitted_completion_calls(plan, allow_network=True, confirm_cost=True), 8
        )
        with self.assertRaises(PlanError):
            validate_plan(fixture("invalid-missing-live-flag.json"))

    def test_sensitive_sentinel_is_rejected_without_echo(self) -> None:
        sentinel = "NON_SECRET_SENTINEL_DO_NOT_ECHO"
        plan = fixture("valid-plan.json")
        plan["api_key"] = sentinel
        with self.assertRaises(PlanError) as caught:
            validate_plan(plan)
        self.assertNotIn(sentinel, str(caught.exception))

    def test_generic_mapping_is_rejected(self) -> None:
        with self.assertRaisesRegex(PlanError, "exact_pair"):
            validate_plan(fixture("invalid-generic-strip.json"))

    def test_exact_versioned_mapping_binds_all_identity_fields(self) -> None:
        plan = authorized(fixture("valid-plan.json"))
        model = plan["models"][0]
        model["requested_model"] = "xiaomi/mimo-v2.5-pro"
        model["profile_id"] = "bailian-cn-beijing-xiaomi-mimo-v2.5-pro"
        model["route_id"] = "bailian-cn-beijing:xiaomi/mimo-v2.5-pro"
        model["public_alias"] = "pvian/bailian-cn-beijing/xiaomi/mimo-v2.5-pro"
        model["vl_text_only"] = False
        plan["identity_mappings"] = [
            {
                "mapping_id": "mimo-exact-pair",
                "mapping_version": "1",
                "mapping_policy_id": "bailian-mimo-exact-v1",
                "match_mode": "exact_pair",
                "requested_model": "xiaomi/mimo-v2.5-pro",
                "reported_model": "mimo-v2.5-pro",
                "provider": "bailian",
                "reported_provider": "bailian",
                "region": "cn-beijing",
                "profile_id": "bailian-cn-beijing-xiaomi-mimo-v2.5-pro",
                "profile_version": "2026-08-09.candidate.1",
                "evidence_sha256": "sha256:" + "b" * 64,
                "user_confirmation_id": "decision-1",
                "confirmed_at": "2026-08-09T10:00:00Z",
                "decision": "accepted",
                "authorization_revision": "authorization-revision-1",
            }
        ]
        validate_plan(plan)

        cross_provider = copy.deepcopy(plan)
        cross_provider["identity_mappings"][0]["reported_provider"] = "xiaomi-direct"
        with self.assertRaisesRegex(PlanError, "cross-provider"):
            validate_plan(cross_provider)

        stale_authorization = copy.deepcopy(plan)
        stale_authorization["identity_mappings"][0]["authorization_revision"] = "old"
        with self.assertRaisesRegex(PlanError, "authorization revision"):
            validate_plan(stale_authorization)

        case_only = copy.deepcopy(plan)
        case_only["identity_mappings"][0]["reported_model"] = "XIAOMI/MIMO-V2.5-PRO"
        with self.assertRaisesRegex(PlanError, "case-only"):
            validate_plan(case_only)

    def test_vl_scope_and_proof_gates_are_mandatory(self) -> None:
        plan = fixture("valid-plan.json")
        plan["models"][0]["vl_text_only"] = False
        with self.assertRaisesRegex(PlanError, "text-only"):
            validate_plan(plan)
        plan = fixture("valid-plan.json")
        plan["proof_gates"]["wire_evidence_after_live"] = False
        with self.assertRaisesRegex(PlanError, "wire evidence"):
            validate_plan(plan)


class OfflinePreflightTests(unittest.TestCase):
    def make_repo(self, root: Path, implementation_text: str = "SAFE = True\n") -> Path:
        plan = fixture("valid-plan.json")
        for relative in (
            "config/provider-profiles",
            "config/pricing",
            "data/capability-profiles",
            "contracts/schemas",
            "docs",
            "src",
        ):
            (root / relative).mkdir(parents=True, exist_ok=True)
        (root / "config/public-model-aliases.json").write_text("{}\n", encoding="utf-8")
        runbook = root / "docs/model-extension-runbook.md"
        runbook.write_text(
            "# Model extension Runbook\n"
            "contract: pvian-model-extension-runbook/v1\n",
            encoding="utf-8",
        )
        plan["runbook_sha256"] = "sha256:" + hashlib.sha256(runbook.read_bytes()).hexdigest()
        (root / "src/safe_gateway.py").write_text(implementation_text, encoding="utf-8")
        (root / "AGENTS.md").write_text("# Test governance\n", encoding="utf-8")
        plan_path = root / "plan.json"
        plan_path.write_text(json.dumps(plan), encoding="utf-8")
        return plan_path

    def test_preflight_is_offline_and_finds_governance(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            result = preflight(root, self.make_repo(root))
            self.assertEqual(result["network_calls"], 0)
            self.assertEqual(result["secrets_read"], 0)
            self.assertEqual(result["agents_files"], ["AGENTS.md"])

    def test_runbook_mismatch_stops(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            plan_path = self.make_repo(root)
            (root / "docs/model-extension-runbook.md").write_text("changed\n", encoding="utf-8")
            with self.assertRaisesRegex(PreflightError, "Runbook"):
                preflight(root, plan_path)

    def test_runbook_version_mismatch_stops(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            plan_path = self.make_repo(root)
            plan = json.loads(plan_path.read_text(encoding="utf-8"))
            plan["runbook_version"] = "pvian-model-extension-runbook/v0"
            plan_path.write_text(json.dumps(plan), encoding="utf-8")
            with self.assertRaisesRegex((PlanError, PreflightError), "Runbook"):
                preflight(root, plan_path)

    def test_file_existence_and_legacy_process_logic_are_not_accepted_as_proof(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            plan_path = self.make_repo(root, "taskkill /pid 123\n")
            with self.assertRaisesRegex(PreflightError, "forbidden"):
                preflight(root, plan_path)
            (root / "src/safe_gateway.py").unlink()
            with self.assertRaisesRegex(PreflightError, "does not exist"):
                preflight(root, plan_path)

    def test_port_or_pattern_wide_kills_are_rejected_but_exact_pid_is_allowed(self) -> None:
        unsafe = (
            "Get-NetTCPConnection -LocalPort 5220 | Stop-Process\n"
            "Stop-Process -Name pvian-gateway\n"
        )
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with self.assertRaisesRegex(PreflightError, "forbidden"):
                preflight(root, self.make_repo(root, unsafe))
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            result = preflight(
                root,
                self.make_repo(root, "Stop-Process -Id $exactChildPid\n"),
            )
            self.assertEqual(result["status"], "ready_offline")

    def test_validators_contain_no_network_or_environment_access(self) -> None:
        sources = "\n".join(
            (SCRIPT_DIR / name).read_text(encoding="utf-8")
            for name in ("validate_onboarding_plan.py", "offline_preflight.py")
        ).lower()
        for forbidden in (
            "import requests",
            "from requests",
            "import urllib",
            "from urllib",
            "import socket",
            "from socket",
            "os.environ",
            "dotenv",
        ):
            self.assertNotIn(forbidden, sources)


if __name__ == "__main__":
    unittest.main(verbosity=2)
