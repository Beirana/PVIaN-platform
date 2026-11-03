"""Minimal PVIaN public-alias consumer; keep its key in local environment only."""

from __future__ import annotations

import os

from openai import OpenAI


def main() -> None:
    gateway_root = os.environ["PVIAN_GATEWAY_BASE_URL"].rstrip("/")
    client = OpenAI(
        base_url=f"{gateway_root}/v1",
        api_key=os.environ["PVIAN_CLIENT_KEY"],
        max_retries=0,
    )
    completion = client.chat.completions.create(
        model=os.environ["PVIAN_MODEL"],
        messages=[{"role": "user", "content": "Return a short synthetic acknowledgement."}],
    )
    print(completion.model)


if __name__ == "__main__":
    main()
