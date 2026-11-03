from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request

from pvi.application.resolver import resolve
from pvi.domain.models import CapabilityQuery
from pvi.infrastructure.repository import Repository

router = APIRouter()


def get_repository(request: Request) -> Repository:
    return request.app.state.repository


RepoDependency = Annotated[Repository, Depends(get_repository)]


def _page(items: list[Any], limit: int, offset: int) -> dict[str, Any]:
    return {
        "items": items[offset : offset + limit],
        "total": len(items),
        "limit": limit,
        "offset": offset,
    }


@router.get("/healthz", include_in_schema=False)
def healthz() -> dict[str, str]:
    return {"status": "ok"}


@router.get("/readyz", include_in_schema=False)
def readyz(repository: RepoDependency) -> dict[str, str]:
    return {"status": "ready", "data_version": repository.data_version}


@router.get("/v1/endpoints")
def list_endpoints(
    repository: RepoDependency,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> dict[str, Any]:
    items = [repository.endpoints[key] for key in sorted(repository.endpoints)]
    return _page(items, limit, offset)


@router.get("/v1/endpoints/{endpoint_id}")
def get_endpoint(endpoint_id: str, repository: RepoDependency):
    item = repository.endpoints.get(endpoint_id)
    if item is None:
        raise HTTPException(status_code=404, detail=("endpoint_not_found", "Endpoint not found."))
    return item


@router.get("/v1/capability-profiles")
def list_profiles(
    repository: RepoDependency,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> dict[str, Any]:
    items = [repository.profiles[key] for key in sorted(repository.profiles)]
    return _page(items, limit, offset)


@router.get("/v1/capability-profiles/{profile_id}")
def get_profile(profile_id: str, repository: RepoDependency):
    item = repository.profiles.get(profile_id)
    if item is None:
        raise HTTPException(
            status_code=404,
            detail=("profile_not_found", "Capability profile was not found."),
        )
    return item


@router.post("/v1/capability-queries:resolve")
def resolve_capabilities(query: CapabilityQuery, repository: RepoDependency):
    return resolve(query, repository)


@router.get("/v1/probes")
def list_probes(
    repository: RepoDependency,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> dict[str, Any]:
    items = sorted(repository.probe_catalog.probes, key=lambda item: item.probe_id)
    return _page(items, limit, offset)


@router.get("/v1/probe-runs/{run_id}")
def get_probe_run(run_id: str, repository: RepoDependency):
    item = repository.probe_runs.get(run_id)
    if item is None:
        raise HTTPException(status_code=404, detail=("probe_run_not_found", "Probe run not found."))
    return item


@router.get("/v1/evidence/{evidence_id}")
def get_evidence(evidence_id: str, repository: RepoDependency):
    item = repository.evidence.get(evidence_id)
    if item is None:
        raise HTTPException(status_code=404, detail=("evidence_not_found", "Evidence not found."))
    return item
