"""Dedicated local-qwen gateway #2 on 127.0.0.1:5240 (twin of the 5239 service).

Same overlay pattern as scripts/gateway_local_qwen_service.py, but pointed at
the SECOND self-deployed vLLM clone server via the SSH tunnel 127.0.0.1:18001.
Independent overlay root / state / ledger / log / PID (var/gateway-local-qwen-2);
shares only the controlled client-key registry, exactly like 5239. The model
identity is identical to 5239's route (qwen3.8-27b-fp8 cloned from the same
image), so the same isolated qualification source builds the overlay.
"""
from __future__ import annotations

import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(Path(__file__).resolve().parent))

import gateway_isolated_local_qwen as base  # noqa: E402
from gateway_local_qwen_service import main as _base_main  # noqa: E402

# Twin overrides BEFORE any overlay/env use: port 5240, root var/gateway-local-qwen-2,
# and the upstream tunnel 18001 (wins over .env because _child_env prefers os.environ).
base.PORT = 5240
base.OVERLAY_VERSION_SUFFIX = "-gateway5240.1"
import os  # noqa: E402

os.environ["PVI_PROBE_LOCAL_QWEN_OPENAI_BASE_URL"] = "http://127.0.0.1:18001/v1"

import gateway_local_qwen_service as svc  # noqa: E402

svc.PORT = base.PORT
svc.ROOT = PROJECT_ROOT / "var" / "gateway-local-qwen-2"
svc.PID_FILE = svc.ROOT / "gateway.pid"

if __name__ == "__main__":
    raise SystemExit(_base_main())
