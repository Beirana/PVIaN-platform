"""Dedicated local-qwen gateway #3 on 127.0.0.1:5242 (twin of the 5239 service).

S-branch instance pointed at the THIRD self-deployed vLLM clone (qwen-3) via
the SSH tunnel 127.0.0.1:18003. Independent overlay root / state / ledger /
log / PID (var/gateway-local-qwen-3); shares only the controlled client-key
registry. Same model identity as 5239/5240 (qwen3.8-27b-fp8 cloned from the
same relaxed-template image).
"""
from __future__ import annotations

import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(Path(__file__).resolve().parent))

import os  # noqa: E402

import gateway_isolated_local_qwen as base  # noqa: E402

base.PORT = 5242
base.OVERLAY_VERSION_SUFFIX = "-gateway5242.1"
os.environ["PVI_PROBE_LOCAL_QWEN_OPENAI_BASE_URL"] = "http://127.0.0.1:18003/v1"

import gateway_local_qwen_service as svc  # noqa: E402
from gateway_local_qwen_service import main as _base_main  # noqa: E402

svc.PORT = base.PORT
svc.ROOT = PROJECT_ROOT / "var" / "gateway-local-qwen-3"
svc.PID_FILE = svc.ROOT / "gateway.pid"

if __name__ == "__main__":
    raise SystemExit(_base_main())
