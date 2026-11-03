from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from pvi.config import PROJECT_ROOT, Settings
from pvi.infrastructure.repository import RepositoryLoader
from pvi.main import create_app


@pytest.fixture(scope="session")
def project_root() -> Path:
    return PROJECT_ROOT


@pytest.fixture(scope="session")
def repository():
    return RepositoryLoader(
        PROJECT_ROOT / "data",
        PROJECT_ROOT / "contracts",
        validate=True,
    ).load()


@pytest.fixture()
def client():
    settings = Settings(
        data_dir=PROJECT_ROOT / "data",
        contract_dir=PROJECT_ROOT / "contracts",
    )
    with TestClient(create_app(settings)) as test_client:
        yield test_client
