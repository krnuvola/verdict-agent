"""Contract and scenario checks for local Track B/C mock servers."""

from __future__ import annotations

from datetime import date
from typing import Any

import pytest
from fastapi.testclient import TestClient

from tests.mocks.mock_graph import (
    EDGES,
    NODES,
    applicability,
)
from tests.mocks.mock_graph import (
    app as graph_app,
)
from tests.mocks.mock_workbench import (
    CONSISTENCY_REPORT,
)
from tests.mocks.mock_workbench import (
    app as workbench_app,
)

TOKEN = "mock-contract-token"
HEADERS = {
    "Authorization": f"Bearer {TOKEN}",
    "X-Contract-Version": "1.0.0",
    "X-Request-Id": "01JDS7K3PZJ4C6S9W2H3G5T8VA",
    "X-Run-Id": "run_01JDS7K3PZJ4C6S9W2H3G5T8VA",
    "X-Matter-Id": "mtr_01JDS7K3PZJ4C6S9W2H3G5T8VA",
}


@pytest.fixture(autouse=True)
def service_token(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SERVICE_TOKEN", TOKEN)


def known(value: Any) -> dict[str, Any]:
    return {"value": value, "status": "known"}


def test_fixture_contains_shared_seed_ids_and_deterministic_treatment() -> None:
    required_ids = {
        "Provision:de.dgcl.144b@2025-03-25",
        "Provision:de.dgcl.144c@2025-03-25",
        "Holding:match-group-2024",
        "Holding:mfw-2014",
        "Element:committee-approval",
        "Element:mom-approval",
        "Element:committee-independence",
        "Element:committee-power-to-say-no",
        "Element:committee-power-to-negotiate",
        "Element:committee-free-to-select-advisors",
        "Element:committee-duty-of-care",
        "Element:committee-ab-initio",
        "Element:mom-informed",
        "Element:mom-uncoerced",
        "Element:disinterested-stockholder-approval",
        "Standard:business-judgment",
        "Standard:entire-fairness",
    }

    assert required_ids.issubset(NODES)
    assert len(EDGES) == 1
    assert EDGES[0]["edge_id"] == "edge_94623511e68d5ae7"


def test_graph_mock_health_auth_contract_and_openapi() -> None:
    with TestClient(graph_app) as client:
        health = client.get("/health")
        denied = client.get("/graph/v1/registry", headers={"X-Contract-Version": "1.0.0"})
        openapi = client.get("/openapi.json", headers=HEADERS)

    assert health.status_code == 200
    assert health.json()["contract_version"] == "1.0.0"
    assert denied.status_code == 401
    assert denied.json()["error"]["code"] == "UNAUTHORIZED"
    assert openapi.status_code == 200
    assert "/graph/v1/query/validity" in openapi.json()["paths"]


def test_mock_generated_request_id_matches_error_envelope() -> None:
    with TestClient(graph_app) as client:
        denied = client.get(
            "/graph/v1/registry",
            headers={"X-Contract-Version": "1.0.0"},
        )

    assert denied.status_code == 401
    assert denied.json()["error"]["request_id"] == denied.headers["X-Request-Id"]
    assert denied.headers["X-Run-Id"].startswith("run_")


def test_graph_point_in_time_node_and_edge_filtering() -> None:
    with TestClient(graph_app) as client:
        hidden = client.get(
            "/graph/v1/nodes/Provision%3Ade.dgcl.144b%402025-03-25?as_of=2024-12-01",
            headers=HEADERS,
        )
        visible_holding = client.get(
            "/graph/v1/nodes/Holding%3Amatch-group-2024?as_of=2024-12-01",
            headers=HEADERS,
        )
        old_edges = client.get(
            "/graph/v1/nodes/Provision%3Ade.dgcl.144b%402025-03-25/edges?as_of=2024-12-01",
            headers=HEADERS,
        )

    assert hidden.status_code == 404
    assert hidden.json()["error"]["code"] == "NOT_KNOWN_AS_OF"
    assert visible_holding.status_code == 200
    assert old_edges.status_code == 200
    assert old_edges.json() == {"items": []}


def test_graph_mock_returns_scope_and_unevaluated_validity() -> None:
    with TestClient(graph_app) as client:
        frame = client.post(
            "/graph/v1/query/scope_frame",
            headers=HEADERS,
            json={
                "node_id": "Provision:de.dgcl.144b@2025-03-25",
                "as_of": "2026-11-07",
            },
        )
        validity = client.post(
            "/graph/v1/query/validity",
            headers=HEADERS,
            json={
                "node_id": "Holding:match-group-2024",
                "as_of": "2026-11-07",
            },
        )

    assert frame.status_code == 200
    assert frame.json()["frame"]["transaction_types"] == ["other_controller_deal"]
    assert validity.status_code == 200
    assert validity.json()["treatments"][0]["edge"]["condition"] == EDGES[0]["condition"]


def test_scope_mock_collects_missing_facts_without_short_circuit() -> None:
    frame = NODES["Provision:de.dgcl.144b@2025-03-25"]["scope_frame"]
    facts = {"controller_present": {"status": "unknown"}}
    result, missing, failed_clause = applicability(frame, facts, date(2026, 11, 7))

    assert result is None
    assert missing == ["controller_present", "litigation_commenced_on", "transaction_type"]
    assert failed_clause is None


def test_graph_standard_of_review_s1_s2_s4_s5_shapes() -> None:
    s1 = {
        "transaction_type": known("other_controller_deal"),
        "controller_present": known(True),
        "litigation_commenced_on": known(None),
        "special_committee_formed": known(True),
        "committee_all_disinterested": known(True),
        "committee_power_to_say_no": known(True),
        "committee_power_to_negotiate": known(True),
        "committee_approval_obtained": known(True),
    }
    s2 = {
        "transaction_type": known("other_controller_deal"),
        "controller_present": known(True),
        "litigation_commenced_on": known("2023-11-01"),
    }
    s4 = {
        "transaction_type": known("going_private"),
        "controller_present": known(True),
        "litigation_commenced_on": known(None),
        "committee_all_disinterested": known(True),
        "committee_power_to_say_no": known(True),
        "committee_power_to_negotiate": known(True),
        "committee_approval_obtained": known(True),
        "mom_approval_obtained": {"status": "unknown"},
    }
    s5 = {
        "transaction_type": {"status": "unknown"},
        "controller_present": known(True),
    }

    with TestClient(graph_app) as client:
        results = [
            client.post(
                "/graph/v1/query/standard_of_review",
                headers=HEADERS,
                json={"as_of": "2026-11-07", "facts": facts},
            )
            for facts in (s1, s2, s4, s5)
        ]

    for result in results:
        assert result.status_code == 200
        assert set(result.json()) == {"standard", "conditions"}
        assert set(result.json()["standard"]) == {"value", "safe_harbor", "basis", "readings"}
    assert results[0].json()["standard"]["safe_harbor"] == "true"
    assert results[0].json()["standard"]["basis"] == [
        "Provision:de.dgcl.144b@2025-03-25"
    ]
    assert results[1].json()["standard"]["value"] == "unknown"
    assert any(
        reading["regime"] == "Holding:mfw-2014"
        for reading in results[1].json()["standard"]["readings"]
    )
    assert results[2].json()["standard"]["safe_harbor"] == "unknown"
    assert any(
        "mom_approval_obtained" in reading["requires"]
        for reading in results[2].json()["standard"]["readings"]
    )
    assert results[3].json()["standard"]["value"] == "unknown"
    assert any(
        "transaction_type" in reading["applies_if"]
        for reading in results[3].json()["standard"]["readings"]
    )


def test_graph_search_empty_extract_jobs_gnn_registry_and_register_contracts() -> None:
    doc_id = "sha256:" + "0" * 64
    source = {
        "source_id": "src_01JDS7K3PZJ4C6S9W2H3G5T8VA",
        "url": "https://www.sec.gov/Archives/test",
        "registry_entry_id": "sec_edgar",
        "retrieved_at": "2026-11-07T17:00:00Z",
        "session_id": None,
        "doc_id": doc_id,
        "media_type": "text/plain",
        "byte_length": 0,
        "http_status": 200,
        "fetch_method": "api",
        "retrieved_by": "tests.mock",
        "expected_doc_type": "filing_index",
        "version_label": None,
        "canon_version": None,
    }
    with TestClient(graph_app) as client:
        search = client.post(
            "/graph/v1/search/documents",
            headers=HEADERS,
            json={"query": "fixture", "as_of": "2026-11-07"},
        )
        registered = client.post(
            "/graph/v1/documents/register",
            headers=HEADERS,
            json={"source": source},
        )
        metadata = client.get(
            f"/graph/v1/documents/{doc_id.replace(':', '%3A')}/meta",
            headers=HEADERS,
        )
        text = client.get(
            f"/graph/v1/documents/{doc_id.replace(':', '%3A')}/text",
            headers=HEADERS,
        )
        extract = client.post(
            "/graph/v1/extract/facts",
            headers=HEADERS,
            json={"doc_ids": [doc_id], "as_of": "2026-11-07"},
        )
        job = client.get(
            f"/graph/v1/jobs/{extract.json()['job_id']}",
            headers=HEADERS,
        )
        gnn = client.post(
            "/graph/v1/gnn/score_edges",
            headers=HEADERS,
            json={
                "as_of": "2026-11-07",
                "edges": [
                    {
                        "src": "Provision:de.dgcl.144b@2025-03-25",
                        "dst": "Holding:match-group-2024",
                        "edge_type": "superseded_by_statute",
                    },
                    {"src": "missing", "dst": "target", "edge_type": "cites"},
                ],
            },
        )
        predictions = client.post(
            "/graph/v1/gnn/predict_missing",
            headers=HEADERS,
            json={
                "as_of": "2026-11-07",
                "anchor_node_ids": ["Element:committee-approval"],
            },
        )
        registry = client.get("/graph/v1/registry", headers=HEADERS)

    assert search.json() == {"items": []}
    assert registered.status_code == 201
    assert metadata.json()["node_ids"] == []
    assert text.headers["content-type"].startswith("text/plain")
    assert text.text == ""
    assert job.json()["status"] == "done"
    assert gnn.json()["scores"][0]["score_calibrated"] == 0.9
    assert gnn.json()["scores"][1]["score_calibrated"] == 0.1
    assert predictions.json()["items"]
    assert registry.json()["version"] == 1
    assert all(not entry["terms_verified"] for entry in registry.json()["entries"])


def test_workbench_mock_contract_and_canned_s9_shape() -> None:
    with TestClient(workbench_app) as client:
        created = client.post(
            "/wb/v1/reviews",
            headers=HEADERS,
            json={
                "doc_id": "sha256:" + "a" * 64,
                "playbook_id": "delaware-controller-v1",
                "matter": {},
            },
        )
        review = client.get(
            f"/wb/v1/reviews/{created.json()['review_id']}",
            headers=HEADERS,
        )

    assert created.status_code == 202
    assert set(created.json()) == {"review_id"}
    body = review.json()
    assert body["state"] == "ready"
    assert len(body["edits"]) == 1
    assert body["edits"][0]["op"] == "insert"
    assert set(body["consistency_report"]) == set(CONSISTENCY_REPORT)
    assert set(body["edits"][0]) == {
        "edit_id",
        "document_id",
        "op",
        "clause_path",
        "paragraph_id",
        "start",
        "end",
        "old_text",
        "new_text",
        "rationale",
        "authority_claim_ids",
        "guard_reports",
        "playbook_rule_id",
        "risk",
        "status",
        "reviewer",
    }


def test_both_mocks_reject_contract_major_mismatch() -> None:
    mismatch_headers = {**HEADERS, "X-Contract-Version": "2.0.0"}
    for app in (graph_app, workbench_app):
        with TestClient(app) as client:
            response = client.get("/openapi.json", headers=mismatch_headers)
        assert response.status_code == 400
        assert response.json()["error"]["code"] == "CONTRACT_VERSION_MISMATCH"
