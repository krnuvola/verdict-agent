"""Authentication, trace, error, and route-shell tests."""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient

from gba_agent.api.main import app
from gba_agent.config import get_settings


@pytest.fixture
def api_client(monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    monkeypatch.setenv("SERVICE_TOKEN", "unit-test-token")
    monkeypatch.setenv(
        "DATABASE_URL", "postgresql+psycopg://user:pass@localhost:5433/agentdb"
    )
    monkeypatch.setenv(
        "AGENT_USER_AGENT", "GuardedLegalAgent/1.0 (contact: test@example.com)"
    )
    monkeypatch.setenv("LLM_PROVIDER", "openai_compatible")
    monkeypatch.setenv("LLM_MODE", "replay")
    monkeypatch.setenv("LLM_MODEL_PLANNER", "planner-test")
    monkeypatch.setenv("LLM_MODEL_JUDGE", "judge-test")
    get_settings.cache_clear()
    client = TestClient(app)
    yield client
    client.close()
    get_settings.cache_clear()


def headers(**overrides: str) -> dict[str, str]:
    value = {
        "Authorization": "Bearer unit-test-token",
        "X-Contract-Version": "1.0.0",
    }
    value.update(overrides)
    return value


def test_health_is_public_and_returns_shared_shape(api_client: TestClient) -> None:
    response = api_client.get("/health")

    assert response.status_code == 200
    assert response.json() == {
        "status": "ok",
        "service": "agent-api",
        "version": "0.1.0",
        "contract_version": "1.0.0",
    }
    assert response.headers["X-Request-Id"]
    assert response.headers["X-Contract-Version"] == "1.0.0"


@pytest.mark.parametrize(
    "authorization",
    [None, "Basic unit-test-token", "Bearer wrong-token"],
)
def test_protected_openapi_requires_correct_bearer(
    api_client: TestClient, authorization: str | None
) -> None:
    request_headers = {"X-Contract-Version": "1.0.0"}
    if authorization is not None:
        request_headers["Authorization"] = authorization

    response = api_client.get("/openapi.json", headers=request_headers)

    assert response.status_code == 401
    assert response.json()["error"]["code"] == "UNAUTHORIZED"
    assert response.json()["error"]["request_id"] == response.headers["X-Request-Id"]


def test_contract_major_mismatch_returns_exact_envelope(api_client: TestClient) -> None:
    response = api_client.get("/openapi.json", headers=headers(**{"X-Contract-Version": "2.0.0"}))

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "CONTRACT_VERSION_MISMATCH"
    assert set(response.json()["error"]) == {"code", "message", "details", "request_id"}


def test_contract_minor_compatibility_and_openapi_auth(api_client: TestClient) -> None:
    response = api_client.get("/openapi.json", headers=headers(**{"X-Contract-Version": "1.7.4"}))

    assert response.status_code == 200
    document = response.json()
    assert document["info"]["title"] == "Guarded Browsing Agent"
    assert "/orch/v1/runs" in document["paths"]
    assert "/guards/v1/verify/claims" in document["paths"]


def test_trace_headers_are_returned_and_request_id_is_validated(api_client: TestClient) -> None:
    response = api_client.get(
        "/openapi.json",
        headers=headers(
            **{
                "X-Request-Id": "01JDS7K3PZJ4C6S9W2H3G5T8VA",
                "X-Run-Id": "run_01JDS7K3PZJ4C6S9W2H3G5T8VA",
                "X-Matter-Id": "mtr_01JDS7K3PZJ4C6S9W2H3G5T8VA",
            }
        ),
    )

    assert response.status_code == 200
    assert response.headers["X-Request-Id"] == "01JDS7K3PZJ4C6S9W2H3G5T8VA"
    assert response.headers["X-Run-Id"] == "run_01JDS7K3PZJ4C6S9W2H3G5T8VA"
    assert response.headers["X-Matter-Id"] == "mtr_01JDS7K3PZJ4C6S9W2H3G5T8VA"

    invalid = api_client.get("/openapi.json", headers=headers(**{"X-Request-Id": "bad"}))
    assert invalid.status_code == 422
    assert invalid.json()["error"]["code"] == "VALIDATION_ERROR"


def test_request_validation_uses_error_envelope(api_client: TestClient) -> None:
    response = api_client.post("/orch/v1/runs", headers=headers(), json={})

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "VALIDATION_ERROR"
    assert response.json()["error"]["request_id"] == response.headers["X-Request-Id"]


def test_create_run_rejects_an_invalid_matter(
    api_client: TestClient,
) -> None:
    response = api_client.post(
        "/orch/v1/runs",
        headers=headers(),
        json={"matter": {}},
    )

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "VALIDATION_ERROR"


def test_guard_claim_limit_uses_shared_error_code(api_client: TestClient) -> None:
    response = api_client.post(
        "/guards/v1/verify/claims",
        headers=headers(),
        json={"matter": {}, "claims": [{} for _ in range(51)]},
    )

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "TOO_MANY_CLAIMS"
