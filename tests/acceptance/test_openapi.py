"""Frozen Doc 00 route/model invariants, independent of the generated schema."""

from tests.unit.test_api_service import api_client as api_client
from tests.unit.test_api_service import headers


def test_openapi_has_exact_frozen_track_a_operations(api_client) -> None:
    response = api_client.get("/openapi.json", headers=headers())
    assert response.status_code == 200
    schema = response.json()
    expected = {
        ("/health", "get"),
        ("/orch/v1/runs", "post"),
        ("/orch/v1/runs/{run_id}", "get"),
        ("/orch/v1/runs/{run_id}/cancel", "post"),
        ("/orch/v1/runs/{run_id}/dossier", "get"),
        ("/orch/v1/runs/{run_id}/trajectory", "get"),
        ("/orch/v1/handoffs", "get"),
        ("/orch/v1/handoffs/{handoff_id}/resolve", "post"),
        ("/guards/v1/verify/claims", "post"),
        ("/guards/v1/action/classify", "post"),
    }
    actual = {
        (path, method)
        for path, operations in schema["paths"].items()
        for method in operations
        if method in {"get", "post", "put", "patch", "delete"}
    }
    assert actual == expected
    models = schema["components"]["schemas"]

    def response_model(path, status):
        ref = schema["paths"][path]["get"]["responses"][status]["content"]["application/json"][
            "schema"
        ]["$ref"]
        return models[ref.rsplit("/", 1)[-1]]

    dossier = response_model("/orch/v1/runs/{run_id}/dossier", "200")
    assert set(dossier["properties"]) == {
        "run_id",
        "matter",
        "generated_at",
        "standard_of_review",
        "conditions",
        "authorities",
        "dropped_claims",
        "open_questions",
        "abstentions",
        "edits",
        "trajectory_path",
        "disclaimer",
    }
    assert dossier["additionalProperties"] is False
    status = response_model("/orch/v1/runs/{run_id}", "200")
    assert set(status["properties"]) == {
        "run_id",
        "state",
        "step",
        "steps_used",
        "cost_usd",
        "open_handoffs",
        "error",
    }
    verify = schema["paths"]["/guards/v1/verify/claims"]["post"]["responses"]["200"]["content"][
        "application/json"
    ]["schema"]["$ref"]
    report_ref = models[verify.rsplit("/", 1)[-1]]["properties"]["reports"]["items"]["$ref"]
    report = models[report_ref.rsplit("/", 1)[-1]]
    assert set(report["properties"]) == {
        "claim_id",
        "results",
        "final",
        "abstain_reason",
        "missing_facts",
    }
    assert report["properties"]["final"]["enum"] == ["pass", "block", "unknown"]
    assert (
        "application/x-ndjson"
        in schema["paths"]["/orch/v1/runs/{run_id}/trajectory"]["get"]["responses"]["200"][
            "content"
        ]
    )
