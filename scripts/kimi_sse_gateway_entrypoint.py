"""Explicit entrypoint for the isolated Kimi SSE aggregation gateway."""

from __future__ import annotations

import sys

from pvian_gateway.kimi_sse_adapter import install

install()

from pvian_gateway.main import main  # noqa: E402

if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
