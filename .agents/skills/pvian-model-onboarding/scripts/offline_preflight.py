from __future__ import annotations

import argparse
import hashlib
import json
import re
import subprocess
import sys
from pathlib import Path

from validate_onboarding_plan import RUNBOOK_VERSION, PlanError, load_and_validate

REQUIRED_PATHS = (
    "docs/model-extension-runbook.md",
    "config/provider-profiles",
    "config/pricing",
    "config/public-model-aliases.json",
    "data/capability-profiles",
    "contracts/schemas",
)
FORBIDDEN_IMPLEMENTATION_TEXT = (
    "scripts/launch_gateway.py",
    "taskkill",
    "strip-prefix",
    "strip_prefix",
    "killall",
    "pkill",
)
FORBIDDEN_IMPLEMENTATION_PATTERNS = (
    re.compile(r"\bstop-process\b[^\r\n]*\s-name\b"),
    re.compile(r"\bget-nettcpconnection\b[^\r\n]*(?:\||;)\s*stop-process\b"),
    re.compile(r"\bcommandline\b[^\r\n]*(?:-like|-match)[^\r\n]*(?:\||;)\s*stop-process\b"),
    re.compile(r"\bfuser\b[^\r\n]*\s-k(?:\s|$)"),
)


class PreflightError(ValueError):
    pass


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return "sha256:" + digest.hexdigest()


def preflight(repo_root: Path, plan_path: Path) -> dict[str, object]:
    root = repo_root.resolve()
    plan_path = plan_path.resolve()
    summary = load_and_validate(plan_path)
    plan = json.loads(plan_path.read_text(encoding="utf-8"))
    missing = [name for name in REQUIRED_PATHS if not (root / name).exists()]
    if missing:
        raise PreflightError("required repository paths are missing")
    runbook = root / "docs" / "model-extension-runbook.md"
    runbook_text = runbook.read_text(encoding="utf-8")
    if plan["runbook_version"] != RUNBOOK_VERSION or RUNBOOK_VERSION not in runbook_text:
        raise PreflightError("Runbook version does not match the plan")
    if _sha256(runbook) != plan["runbook_sha256"]:
        raise PreflightError("Runbook digest does not match the plan")
    agents_files = sorted(
        str(path.relative_to(root)).replace("\\", "/") for path in root.rglob("AGENTS.md")
    )
    for relative in plan["implementation_files"]:
        path = (root / relative).resolve()
        try:
            path.relative_to(root)
        except ValueError as exc:
            raise PreflightError("implementation path escapes repository") from exc
        if not path.is_file():
            raise PreflightError("planned implementation file does not exist")
        text = path.read_text(encoding="utf-8", errors="replace").lower()
        if any(token in text for token in FORBIDDEN_IMPLEMENTATION_TEXT) or any(
            pattern.search(text) for pattern in FORBIDDEN_IMPLEMENTATION_PATTERNS
        ):
            raise PreflightError("planned implementation references forbidden legacy/process logic")
    try:
        result = subprocess.run(
            ["git", "status", "--short", "--untracked-files=all"],
            cwd=root,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=15,
            check=False,
        )
        dirty_paths = [line[3:] for line in result.stdout.splitlines() if len(line) > 3]
    except (OSError, subprocess.SubprocessError):
        dirty_paths = []
    return {
        "status": "ready_offline",
        **summary,
        "agents_files": agents_files,
        "dirty_paths": dirty_paths,
        "network_calls": 0,
        "secrets_read": 0,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Run deterministic offline onboarding preflight.")
    parser.add_argument("--repo-root", type=Path, required=True)
    parser.add_argument("--plan", type=Path, required=True)
    arguments = parser.parse_args()
    try:
        result = preflight(arguments.repo_root, arguments.plan)
    except (PlanError, PreflightError, OSError, json.JSONDecodeError) as exc:
        print(json.dumps({"status": "blocked", "reason": str(exc)}, separators=(",", ":")))
        return 2
    print(json.dumps(result, ensure_ascii=False, sort_keys=True, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    sys.exit(main())
