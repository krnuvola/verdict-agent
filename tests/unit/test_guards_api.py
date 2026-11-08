"""Doc 00 guard-route tests through the API, real guards, and a local database."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator, Iterator
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi import Request
from fastapi.testclient import TestClient
from sqlalchemy import Engine, create_engine, event, select
from sqlalchemy.orm import Session

from gba_agent.api.contracts_compat import _CompatMatterFact
from gba_agent.api.deps import graph_client_dependency
from gba_agent.api.main import app
from gba_agent.api.trace import trace_from_request
from gba_agent.clients.graph_client import GraphClient
from gba_agent.config import get_settings
from gba_agent.db.models import Base, Claim, GuardReport, Run, SourceRecord
from tests.mocks.mock_graph import applicability
from tests.mocks.temporal import known, matter
from tests.unit.test_claim_verifier import DOC_ID, QUOTE, TEXT, Replay, blobs, claim, reply, span
from tests.unit.test_source_gate import record

URL = "/guards/v1/verify/claims"
DAY = "2026-11-07"
NODE = "Provision:synthetic@2024-01-01"
ELEMENT = "Element:synthetic"
REQUEST_ID = "01JDS7K3PZJ4C6S9W2H3G5T8VA"
RUN_ID = f"run_{REQUEST_ID}"
MTR_ID = f"mtr_{REQUEST_ID}"
TOKEN = "guards-contract-test"
HEADERS = {"Authorization": f"Bearer {TOKEN}", "X-Contract-Version": "1.0.0"}
PROPOSITION = "The fixture court applied the rule."
PASSAGE = "The court applied the rule. The condition was met."


@dataclass
class Writer:
    path: Path

    def emit(self, *, component: str, type: str, payload: dict[str, Any], matter_id: str) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", encoding="utf-8") as stream:
            stream.write(
                json.dumps(
                    {
                        "component": component,
                        "type": type,
                        "payload": payload,
                        "matter_id": matter_id,
                    }
                )
                + "\n"
            )


@dataclass
class VerifyFixture:
    client: TestClient
    engine: Engine
    root: Path
    source_id: str
    requests: list[httpx.Request] = field(default_factory=list)
    graph_outage: bool = False
    gnn_outage: bool = False

    def events(self, run_id: str = "adhoc") -> list[dict[str, Any]]:
        path = self.root / run_id / "agent-api.jsonl"
        return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]

    def respond(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        timeout = 120 if request.url.path.endswith("/gnn/score_edges") else 30
        assert request.extensions["timeout"]["read"] == timeout
        if self.graph_outage or (self.gnn_outage and request.url.path.endswith("/gnn/score_edges")):
            return httpx.Response(503, json={"error": {"code": "UPSTREAM_UNAVAILABLE"}})
        if request.url.path == f"/graph/v1/nodes/{NODE}":
            assert request.url.params["as_of"] == DAY
            return httpx.Response(
                200,
                json={
                    "node_id": NODE,
                    "node_type": "Provision",
                    "label": "Synthetic rule",
                    "valid_from": "2024-01-01",
                    "known_from": "2024-01-01",
                    "origin": "seed",
                },
            )
        if request.url.path == "/graph/v1/query/validity":
            assert json.loads(request.content) == {"node_id": NODE, "as_of": DAY}
            return httpx.Response(
                200,
                json={
                    "node_id": NODE,
                    "as_of": DAY,
                    "in_force": "true",
                    "good_law_unconditional": "good",
                    "treatments": [],
                    "reasons": [],
                },
            )
        if request.url.path == "/graph/v1/query/scope_frame":
            assert json.loads(request.content) == {"node_id": NODE, "as_of": DAY}
            return httpx.Response(
                200,
                json={
                    "node_id": NODE,
                    "frame": {
                        "jurisdiction": "US-DE",
                        "valid_from": "2024-01-01",
                        "transaction_types": ["other_controller_deal"],
                    },
                },
            )
        assert request.url.path == "/graph/v1/gnn/score_edges"
        probe = {"src": NODE, "dst": ELEMENT, "edge_type": "addresses"}
        assert json.loads(request.content) == {"as_of": DAY, "edges": [probe]}
        return httpx.Response(
            200,
            json={
                "model_version": "fixture",
                "scores": [
                    {**probe, "score_raw": 0.2, "score_calibrated": 0.2, "flag": True},
                ],
            },
        )

    def payload(self, **changes: Any) -> dict[str, Any]:
        item = claim(
            matter_id=MTR_ID,
            claim_id=f"clm_{REQUEST_ID}",
            source_doc_id=DOC_ID,
            source_id=self.source_id,
            authority_node_id=NODE,
            element_node_id=ELEMENT,
            kind="legal_proposition",
            text=PROPOSITION,
            span=span(TEXT, QUOTE),
            proposed_by="workbench.ground",
        ).model_dump(mode="json")
        return {
            "matter": matter(transaction_type=known("other_controller_deal")).model_dump(
                mode="json"
            ),
            "claims": [item | changes],
        }


def scope_eval(
    frame: dict[str, Any], facts: dict[str, _CompatMatterFact], as_of: date
) -> tuple[bool | None, list[str], str | None]:
    return applicability(
        frame, {key: fact.model_dump(mode="json") for key, fact in facts.items()}, as_of
    )


@pytest.fixture
def verify_api(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[VerifyFixture]:
    for key, value in {
        "SERVICE_TOKEN": TOKEN,
        "GBA_ENV": "test",
        "DATABASE_URL": "postgresql+psycopg://user:pass@localhost/agentdb",
        "AGENT_USER_AGENT": "GuardTest/1.0 (contact: test@example.test)",
        "LLM_PROVIDER": "openai_compatible",
        "LLM_MODE": "replay",
        "LLM_MODEL_PLANNER": "fixture-planner",
        "LLM_MODEL_JUDGE": "fixture-judge",
        "SUPPORT_JUDGE": "llm",
        "ABSTAIN_CALIBRATOR_PATH": str(tmp_path / "calibrator.json"),
    }.items():
        monkeypatch.setenv(key, value)
    get_settings.cache_clear()
    engine = create_engine(f"sqlite:///{tmp_path / 'guards.db'}")
    event.listen(
        engine, "connect", lambda connection, _: connection.execute("PRAGMA foreign_keys=ON")
    )
    Base.metadata.create_all(engine)
    source = record(doc_id=DOC_ID)
    with Session(engine) as session, session.begin():
        session.add(
            SourceRecord(
                source_id=source.source_id,
                record=source.model_dump(mode="json"),
                admitted=True,
                admission_reason="SG_OK",
            )
        )

    replay = Replay(
        {
            f"<proposition>{PROPOSITION}</proposition> <passage>{PASSAGE}</passage>": reply(
                "supports", 0.9
            )
        }
    )
    monkeypatch.setattr(app.state, "database_engine", engine, raising=False)
    monkeypatch.setattr(app.state, "guard_blobs", blobs(), raising=False)
    monkeypatch.setattr(app.state, "guard_llm", replay, raising=False)
    monkeypatch.setattr(app.state, "guard_scope", scope_eval, raising=False)
    monkeypatch.setattr(
        app.state,
        "guard_trajectory",
        lambda service, run_id: Writer(tmp_path / run_id / f"{service}.jsonl"),
        raising=False,
    )
    client = TestClient(app)
    fixture = VerifyFixture(client, engine, tmp_path, source.source_id)

    async def graph(request: Request) -> AsyncIterator[GraphClient]:
        async with GraphClient(
            "http://graph.test",
            TOKEN,
            trace_from_request(request),
            transport=httpx.MockTransport(fixture.respond),
            max_retries=0,
        ) as graph_client:
            yield graph_client

    monkeypatch.setitem(app.dependency_overrides, graph_client_dependency, graph)
    try:
        yield fixture
    finally:
        client.close()
        engine.dispose()
        get_settings.cache_clear()


def test_workbench_claim_uses_all_guards_persists_and_propagates_trace(
    verify_api: VerifyFixture,
) -> None:
    trace = {"X-Run-Id": RUN_ID, "X-Matter-Id": MTR_ID, "X-Request-Id": REQUEST_ID}
    response = verify_api.client.post(URL, headers=HEADERS | trace, json=verify_api.payload())
    assert response.status_code == 200, response.text
    assert all(response.headers[key] == value for key, value in trace.items())
    assert set(response.json()) == {"reports"}
    report = response.json()["reports"][0]
    assert set(report) == {"claim_id", "results", "final", "abstain_reason", "missing_facts"}
    assert report["final"] == "pass" and report["abstain_reason"] is None
    assert [result["reason_code"] for result in report["results"]] == [
        "SG_OK",
        "CV_OK",
        "TEMPORAL_OK",
        "SCOPE_OK",
        "GNN_LOW_PLAUSIBILITY",
    ]
    assert all(
        all(request.headers[key] == value for key, value in (HEADERS | trace).items())
        for request in verify_api.requests
    )
    with Session(verify_api.engine) as session:
        row = session.get(GuardReport, report["claim_id"])
        stored = session.get(Claim, report["claim_id"])
        assert row is not None and row.report == report
        assert stored is not None and stored.run_id is None
        assert stored.claim["proposed_by"] == "workbench.ground"
        assert session.scalars(select(Run)).all() == []
    events = verify_api.events(RUN_ID)
    assert [event["component"] for event in events] == [
        "source_gate",
        "claim_verifier",
        "temporal",
        "scope",
        "gnn",
    ]
    assert [event["payload"]["result"] for event in events] == report["results"]
    assert all(event["matter_id"] == MTR_ID and event["type"] == "guard_result" for event in events)


def test_source_block_short_circuits_and_uses_adhoc_trajectory(verify_api: VerifyFixture) -> None:
    payload = verify_api.payload(source_id="src_missing")
    response = verify_api.client.post(URL, headers=HEADERS, json=payload)
    assert response.status_code == 200
    report = response.json()["reports"][0]
    assert report["final"] == "block"
    assert [item["reason_code"] for item in report["results"]] == ["SG_SOURCE_NOT_ADMITTED"]
    assert not verify_api.requests
    assert [event["component"] for event in verify_api.events()] == ["source_gate"]


def test_graph_and_gnn_outages_have_distinct_effects(verify_api: VerifyFixture) -> None:
    verify_api.gnn_outage = True
    payload = verify_api.payload()
    response = verify_api.client.post(URL, headers=HEADERS, json=payload)
    assert response.status_code == 200
    report = response.json()["reports"][0]
    assert report["final"] == "pass"
    assert report["results"][-1]["reason_code"] == "GNN_UNAVAILABLE"

    verify_api.graph_outage = True
    response = verify_api.client.post(URL, headers=HEADERS, json=payload)
    assert response.status_code == 200
    report = response.json()["reports"][0]
    assert (report["final"], report["abstain_reason"]) == ("unknown", "SG_CITATION_UNVERIFIED")
    assert [item["reason_code"] for item in report["results"]] == [
        "SG_CITATION_UNVERIFIED",
        "CV_OK",
        "TEMPORAL_VALIDITY_UNKNOWN",
        "SCOPE_NO_FRAME",
        "GNN_UNAVAILABLE",
    ]
    assert verify_api.events()[-1]["type"] == "abstain"
    with Session(verify_api.engine) as session:
        row = session.get(GuardReport, report["claim_id"])
        assert row is not None and row.report == report


def test_missing_scope_fact_and_private_fuzzy_diagnostic(verify_api: VerifyFixture) -> None:
    payload = verify_api.payload()
    payload["matter"]["facts"] = {}
    response = verify_api.client.post(URL, headers=HEADERS, json=payload)
    assert response.status_code == 200
    report = response.json()["reports"][0]
    assert (report["final"], report["abstain_reason"]) == ("unknown", "SCOPE_FACTS_MISSING")
    assert report["missing_facts"] == ["transaction_type"]

    payload["claims"][0].update(claim_id="clm_distinct", quote="The court denied the rule.")
    response = verify_api.client.post(URL, headers=HEADERS, json=payload)
    assert response.status_code == 200
    report = response.json()["reports"][0]
    assert report["final"] == "block"
    assert report["results"][1]["reason_code"] == "CV_QUOTE_NOT_FOUND"
    assert "human_only" not in response.text
    assert "human_only" not in repr(verify_api.events())
    with Session(verify_api.engine) as session:
        row = session.get(GuardReport, report["claim_id"])
        assert row is not None and "human_only" in row.report["results"][1]["evidence"]


def test_verify_route_batch_limit_and_openapi(verify_api: VerifyFixture) -> None:
    payload = verify_api.payload()
    item = payload["claims"][0]
    payload["claims"] = [item | {"claim_id": f"clm_test_{index}"} for index in range(50)]
    payload["guards_enabled"] = ["access", "source_gate"]
    accepted = verify_api.client.post(URL, headers=HEADERS, json=payload)
    assert accepted.status_code == 200 and len(accepted.json()["reports"]) == 50
    assert all(len(report["results"]) == 1 for report in accepted.json()["reports"])
    payload["claims"].append(item)
    rejected = verify_api.client.post(URL, headers=HEADERS, json=payload)
    assert rejected.status_code == 422
    assert rejected.json()["error"]["code"] == "TOO_MANY_CLAIMS"
    assert rejected.json()["error"]["details"] == {"limit": 50, "received": 51}
    assert len(verify_api.requests) == 50
    schemas = verify_api.client.get("/openapi.json", headers=HEADERS).json()["components"][
        "schemas"
    ]
    assert schemas["VerifyClaimsReq"]["properties"]["claims"]["maxItems"] == 50
    assert set(schemas["VerifyClaimsResp"]["properties"]) == {"reports"}


def test_verify_rejects_auth_invalid_guard_and_cross_matter_claim(
    verify_api: VerifyFixture,
) -> None:
    payload = verify_api.payload()
    unauthorized = verify_api.client.post(
        URL, headers={"X-Contract-Version": "1.0.0"}, json=payload
    )
    assert (
        unauthorized.status_code == 401 and unauthorized.json()["error"]["code"] == "UNAUTHORIZED"
    )
    for body in (
        payload | {"guards_enabled": ["missing"]},
        payload | {"guards_enabled": []},
        verify_api.payload(matter_id="mtr_wrong"),
    ):
        rejected = verify_api.client.post(URL, headers=HEADERS, json=body)
        assert (
            rejected.status_code == 422 and rejected.json()["error"]["code"] == "VALIDATION_ERROR"
        )
    assert verify_api.requests == []


def test_invalid_calibrator_rejects_configuration_without_a_graph_call(
    verify_api: VerifyFixture,
) -> None:
    (verify_api.root / "calibrator.json").write_text('{"version": 2}', encoding="utf-8")
    response = verify_api.client.post(URL, headers=HEADERS, json=verify_api.payload())
    assert response.status_code == 500
    assert response.json()["error"]["code"] == "INTERNAL_ERROR"
    assert verify_api.requests == []


def test_duplicate_id_with_different_claim_is_rejected(verify_api: VerifyFixture) -> None:
    payload = verify_api.payload()
    payload["guards_enabled"] = ["source_gate"]
    first = verify_api.client.post(URL, headers=HEADERS, json=payload)
    assert first.status_code == 200
    payload["claims"][0]["text"] = "A different proposition."
    second = verify_api.client.post(URL, headers=HEADERS, json=payload)
    assert second.status_code == 422
    assert second.json()["error"]["code"] == "VALIDATION_ERROR"
    assert len(verify_api.events()) == 1
    with Session(verify_api.engine) as session:
        stored = session.get(Claim, payload["claims"][0]["claim_id"])
        assert stored is not None and stored.claim["text"] == PROPOSITION


def test_trajectory_failure_has_a_retryable_error_envelope(
    verify_api: VerifyFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    def unavailable(*_: object) -> Writer:
        raise OSError("fixture outage")

    monkeypatch.setattr(app.state, "guard_trajectory", unavailable)
    payload = verify_api.payload()
    payload["guards_enabled"] = ["source_gate"]
    failed = verify_api.client.post(URL, headers=HEADERS, json=payload)
    assert failed.status_code == 503 and failed.json()["error"]["code"] == "UPSTREAM_UNAVAILABLE"
    monkeypatch.setattr(
        app.state,
        "guard_trajectory",
        lambda service, run_id: Writer(verify_api.root / run_id / f"{service}.jsonl"),
    )
    assert verify_api.client.post(URL, headers=HEADERS, json=payload).status_code == 200
    assert len(verify_api.events()) == 1


def test_missing_judge_abstains_and_absent_anchors_skip_gnn(
    verify_api: VerifyFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(app.state, "guard_llm", None)
    response = verify_api.client.post(
        URL,
        headers=HEADERS,
        json=verify_api.payload(element_node_id=None),
    )
    assert response.status_code == 200
    report = response.json()["reports"][0]
    assert report["final"] == "unknown" and report["abstain_reason"] == "CV_SUPPORT_UNCERTAIN"
    assert [item["guard"] for item in report["results"]] == [
        "source_gate",
        "claim_verifier",
        "temporal",
        "scope",
    ]
