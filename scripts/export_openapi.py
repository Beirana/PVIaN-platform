from __future__ import annotations

import yaml

from pvi.config import PROJECT_ROOT
from pvi.main import create_app


def main() -> None:
    target = PROJECT_ROOT / "contracts" / "openapi" / "capability-v1.openapi.yaml"
    target.parent.mkdir(parents=True, exist_ok=True)
    with target.open("w", encoding="utf-8", newline="\n") as handle:
        yaml.safe_dump(
            create_app().openapi(),
            handle,
            allow_unicode=True,
            sort_keys=False,
        )
    print(f"exported {target}")


if __name__ == "__main__":
    main()
