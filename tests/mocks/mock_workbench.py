"""Contract-shaped local Workbench API mock for independent Track A testing."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from fastapi import FastAPI, Header, HTTPException
from pydantic import BaseModel, ConfigDict, Field
from ulid import ULID

from tests.mocks.common import install_contract_middleware


class MockModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class CreateReviewRequest(MockModel):
    doc_id: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    playbook_id: str
    matter: dict[str, Any]
    run_id: str | None = None
    dossier: dict[str, Any] | None = None


REVIEWS: dict[str, dict[str, Any]] = {}
RECEIPTS: dict[str, tuple[datetime, str]] = {}
CONSISTENCY_REPORT = {
    "defined_terms_ok": True,
    "unresolved_terms": [],
    "cross_refs_ok": True,
    "broken_cross_refs": [],
    "numbering_ok": True,
    "numbering_issues": [],
    "out_of_scope_changes": 0,
    "authority_guards_ok": True,
    "dropped_edits": [],
    "open_items": [],
    "guard_unavailable": False,
    "rules_not_applicable": [],
}

app = FastAPI(title="Track A local Workbench API mock", version="1.0.0")
install_contract_middleware(app)


@app.get("/health")
def health() -> dict[str, str]:
    return {
        "status": "ok",
        "service": "workbench-api-mock",
        "version": "1.0.0",
        "contract_version": "1.0.0",
    }


@app.post("/wb/v1/reviews", status_code=202)
def create_review(
    body: CreateReviewRequest, idempotency_key: str | None = Header(default=None)
) -> dict[str, str]:
    now = datetime.now(UTC)
    if idempotency_key and (receipt := RECEIPTS.get(idempotency_key)):
        if receipt[0] > now:
            return {"review_id": receipt[1]}
    review_id = "rev_" + str(ULID())
    edit_id = "edt_" + str(ULID())
    edit = {
        "edit_id": edit_id,
        "document_id": body.doc_id,
        "op": "insert",
        "clause_path": "Article VI > Section 6.1",
        "paragraph_id": "p_mock_s9",
        "start": 0,
        "end": 0,
        "old_text": "",
        "new_text": "(c) Synthetic test-only insertion.",
        "rationale": "Canned S9 fixture edit; not legal guidance.",
        "authority_claim_ids": [],
        "guard_reports": [],
        "playbook_rule_id": f"{body.playbook_id}@1:mock-rule",
        "risk": "high",
        "status": "proposed",
        "reviewer": None,
    }
    REVIEWS[review_id] = {
        "review_id": review_id,
        "state": "ready",
        "edits": [edit],
        "consistency_report": dict(CONSISTENCY_REPORT),
        "error": None,
    }
    if idempotency_key:
        RECEIPTS[idempotency_key] = (now + timedelta(hours=24), review_id)
    return {"review_id": review_id}


@app.get("/wb/v1/reviews/{review_id}")
def get_review(review_id: str) -> dict[str, Any]:
    review = REVIEWS.get(review_id)
    if review is None:
        raise HTTPException(status_code=404, detail="REVIEW_NOT_FOUND")
    return review


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("tests.mocks.mock_workbench:app", host="127.0.0.1", port=8102)
