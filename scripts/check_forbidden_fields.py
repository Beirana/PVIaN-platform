from __future__ import annotations

import ast
import re

from pvi.config import PROJECT_ROOT

SENSITIVE_PATTERNS = {
    "authorization header": re.compile(r"(?i)authorization\s*[:=]\s*(bearer|basic)\s+\S+"),
    "private key": re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----"),
    "credential field": re.compile(
        r'(?i)["\'](?:api[_-]?key|access[_-]?token|client[_-]?secret)["\']\s*:'
    ),
}
DISALLOWED_OUTBOUND_IMPORTS = {
    "requests",
    "httpx",
    "urllib",
    "socket",
    "aiohttp",
    "openai",
}
AUTHORIZED_OUTBOUND_ROOTS = {
    PROJECT_ROOT / "src" / "pvi" / "probe_runner",
    PROJECT_ROOT / "src" / "pvian_probe",
    PROJECT_ROOT / "src" / "pvian_gateway",
}


def iter_files():
    for relative in (
        "src",
        "data",
        "fixtures",
        "config",
        "evidence",
        "reports",
        "scripts",
    ):
        root = PROJECT_ROOT / relative
        for path in root.rglob("*"):
            if path.is_file() and path.suffix.lower() in {".py", ".json", ".yaml", ".yml"}:
                yield path


def main() -> None:
    findings: list[str] = []
    for path in iter_files():
        text = path.read_text(encoding="utf-8")
        for label, pattern in SENSITIVE_PATTERNS.items():
            if pattern.search(text):
                findings.append(f"{path.relative_to(PROJECT_ROOT)}: {label}")
    for path in (PROJECT_ROOT / "src").rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            names: list[str] = []
            if isinstance(node, ast.Import):
                names = [alias.name.split(".")[0] for alias in node.names]
            elif isinstance(node, ast.ImportFrom) and node.module:
                names = [node.module.split(".")[0]]
            for name in names:
                if name in DISALLOWED_OUTBOUND_IMPORTS and not any(
                    root == path or root in path.parents for root in AUTHORIZED_OUTBOUND_ROOTS
                ):
                    findings.append(f"{path.relative_to(PROJECT_ROOT)}: outbound import {name}")
    if findings:
        raise SystemExit("forbidden content found:\n" + "\n".join(sorted(findings)))
    print("sensitive-content and unauthorized-outbound-import scans passed")


if __name__ == "__main__":
    main()
