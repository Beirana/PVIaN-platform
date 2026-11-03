from __future__ import annotations

from pvi.config import Settings
from pvi.infrastructure.repository import RepositoryLoader


def main() -> None:
    settings = Settings().resolved()
    repository = RepositoryLoader(
        settings.data_dir,
        settings.contract_dir,
        validate=True,
    ).load()
    print(
        "validated "
        f"{len(repository.endpoints)} endpoints, "
        f"{len(repository.profiles)} profiles, "
        f"{len(repository.evidence)} evidence references, "
        f"{len(repository.probe_catalog.probes)} probes, "
        f"{len(repository.probe_runs)} probe runs"
    )
    print(repository.data_version)


if __name__ == "__main__":
    main()
