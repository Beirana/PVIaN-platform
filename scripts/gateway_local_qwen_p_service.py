"""P-branch local-qwen gateway on 127.0.0.1:5239, upstream = qwen-2 clone.

2026-08-18: the ORIGINAL self-deployed server (tunnel 18000) could not be
preserved by the owner; the P branch now runs on the qwen-2 clone via tunnel
127.0.0.1:18002 (identical image, relaxed-template fix verified present).
This wrapper keeps the upstream override durable across restarts; everything
else (port 5239, overlay root var/gateway-local-qwen, state/ledger lineage
with the P smoke usage history) is inherited unchanged from
gateway_local_qwen_service.py.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(Path(__file__).resolve().parent))

os.environ["PVI_PROBE_LOCAL_QWEN_OPENAI_BASE_URL"] = "http://127.0.0.1:18002/v1"

from gateway_local_qwen_service import main  # noqa: E402

if __name__ == "__main__":
    raise SystemExit(main())
