from __future__ import annotations

FROZEN_PATHS = {
    "/v1/endpoints",
    "/v1/endpoints/{endpoint_id}",
    "/v1/capability-profiles",
    "/v1/capability-profiles/{profile_id}",
    "/v1/capability-queries:resolve",
    "/v1/probes",
    "/v1/probe-runs/{run_id}",
    "/v1/evidence/{evidence_id}",
}


def test_health_and_readiness(client):
    assert client.get("/healthz").json() == {"status": "ok"}
    ready = client.get("/readyz")
    assert ready.status_code == 200
    assert ready.json()["data_version"].startswith("sha256:")


def test_list_endpoints_has_stable_pagination(client):
    response = client.get("/v1/endpoints", params={"limit": 2, "offset": 1})
    assert response.status_code == 200
    payload = response.json()
    assert payload["total"] == 14
    assert payload["limit"] == 2
    assert payload["offset"] == 1
    assert len(payload["items"]) == 2


def test_all_detail_routes(client):
    assert client.get("/v1/endpoints/endpoint-official-alpha").status_code == 200
    assert client.get("/v1/capability-profiles/cap-official-alpha").status_code == 200
    assert client.get("/v1/probe-runs/probe-run-official-tool").status_code == 200
    assert client.get("/v1/evidence/evidence-official-tool-doc").status_code == 200
    probes = client.get("/v1/probes").json()
    assert probes["total"] == 23


def test_resolve_api_returns_full_candidate(client):
    response = client.post(
        "/v1/capability-queries:resolve",
        json={
            "model": "fixture-model-alpha",
            "endpoint_id": "endpoint-official-alpha",
            "requirements": {
                "tool_calling.representation": "native_structured",
                "reasoning.effective": "supported",
            },
        },
    )
    assert response.status_code == 200
    candidate = response.json()["candidates"][0]
    assert candidate["profile"]["profile_id"] == "cap-official-alpha"
    assert candidate["satisfied"][0]["requirement"] == "tool_calling.representation"
    assert candidate["unknown"][0]["requirement"] == "reasoning.effective"


def test_not_found_is_structured_and_has_request_id(client):
    response = client.get(
        "/v1/capability-profiles/missing",
        headers={"x-request-id": "req-test"},
    )
    assert response.status_code == 404
    assert response.headers["x-request-id"] == "req-test"
    assert response.json() == {
        "error": {
            "code": "profile_not_found",
            "message": "Capability profile was not found.",
            "details": {},
            "request_id": "req-test",
        }
    }


def test_invalid_query_has_structured_validation_error(client):
    response = client.post(
        "/v1/capability-queries:resolve",
        json={"endpoint_id": "endpoint-official-alpha"},
        headers={"x-request-id": "req-invalid"},
    )
    assert response.status_code == 422
    payload = response.json()["error"]
    assert payload["code"] == "request_validation_failed"
    assert payload["request_id"] == "req-invalid"
    assert payload["details"]["errors"]


def test_openapi_contains_only_frozen_v1_business_paths(client):
    document = client.get("/openapi.json").json()
    assert document["openapi"].startswith("3.1.")
    business_paths = {path for path in document["paths"] if path.startswith("/v1/")}
    assert business_paths == FROZEN_PATHS
