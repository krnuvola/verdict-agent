"""Contract-shaped local Graph API mock for independent Track A testing.

The seed is explicitly illustrative test data; this module is never imported
or mounted by the production Track A application.
"""

from __future__ import annotations

import json
import re
from datetime import date
from pathlib import Path
from typing import Annotated, Any

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import PlainTextResponse
from pydantic import BaseModel, ConfigDict, Field
from ulid import ULID

from tests.mocks.common import install_contract_middleware

HERE = Path(__file__).parent
SEED = json.loads((HERE / "fixtures" / "seed_graph.json").read_text(encoding="utf-8"))
NODES: dict[str, dict[str, Any]] = {node["node_id"]: node for node in SEED["nodes"]}
EDGES: list[dict[str, Any]] = SEED["edges"]
DOCUMENTS: dict[str, dict[str, Any]] = {}
JOBS: dict[str, dict[str, Any]] = {}
FACT_KEYS = {
    "transaction_type",
    "controller_present",
    "controller_identity",
    "signing_date",
    "announcement_date",
    "closing_date",
    "litigation_commenced_on",
    "interested_director_involved",
    "special_committee_formed",
    "committee_all_disinterested",
    "committee_power_to_say_no",
    "committee_power_to_negotiate",
    "committee_free_to_select_advisors",
    "committee_duty_of_care_met",
    "committee_formed_before_substantive_negotiation",
    "committee_approval_obtained",
    "mom_condition_in_agreement",
    "mom_vote_informed",
    "mom_vote_uncoerced",
    "mom_approval_obtained",
    "disinterested_stockholder_approval_obtained",
    "disinterested_director_approval_obtained",
    "deal_value_usd",
}


class MockModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ScopeFrameRequest(MockModel):
    node_id: str
    as_of: date


class ValidityRequest(MockModel):
    node_id: str
    as_of: date


class RuleCandidatesRequest(MockModel):
    query: str
    as_of: date
    top_k: int = Field(default=10, ge=1)
    node_types: list[str] = Field(default_factory=lambda: ["Provision", "Holding"])
    facts: dict[str, Any] = Field(default_factory=dict)


class StandardRequest(MockModel):
    as_of: date
    facts: dict[str, Any]


class DocumentSearchRequest(MockModel):
    query: str
    as_of: date
    top_k: int = Field(default=10, ge=1)
    registry_entry_ids: list[str] = Field(default_factory=list)
    doc_types: list[str] = Field(default_factory=list)


class RegisterDocumentRequest(MockModel):
    source: dict[str, Any]


class ExtractRequest(MockModel):
    doc_ids: list[str]
    fact_keys: list[str] = Field(default_factory=list)
    as_of: date


class ScoreEdgesRequest(MockModel):
    as_of: date
    edges: list[dict[str, str]]


class PredictMissingRequest(MockModel):
    as_of: date
    anchor_node_ids: list[str]
    exclude_node_ids: list[str] = Field(default_factory=list)
    top_k: int = Field(default=10, ge=1)


def _fact_value(facts: dict[str, Any], key: str) -> tuple[Any, bool]:
    fact = facts.get(key)
    if fact is None:
        return None, False
    if not isinstance(fact, dict):
        return fact, True
    if fact.get("status", "unknown") != "known":
        return None, False
    return fact.get("value"), True


def evaluate(
    predicate: dict[str, Any] | None,
    facts: dict[str, Any],
    missing: set[str],
) -> bool | None:
    """Small test-only Kleene evaluator used by the mock's illustrative fixture."""
    if predicate is None:
        return True
    if "const" in predicate:
        return predicate["const"]
    if "all" in predicate:
        values = [evaluate(child, facts, missing) for child in predicate["all"]]
        if False in values:
            return False
        return None if None in values else True
    if "any" in predicate:
        values = [evaluate(child, facts, missing) for child in predicate["any"]]
        if True in values:
            return True
        return None if None in values else False
    if "not" in predicate:
        value = evaluate(predicate["not"], facts, missing)
        return None if value is None else not value
    if "fact" not in predicate:
        raise ValueError(f"Malformed fixture predicate: {predicate}")
    key = predicate["fact"]
    operators = [
        operator
        for operator in ("eq", "ne", "in", "lt", "lte", "gt", "gte")
        if operator in predicate
    ]
    if key not in FACT_KEYS or len(operators) != 1:
        raise ValueError(f"Malformed fixture fact predicate: {predicate}")
    value, known = _fact_value(facts, key)
    if not known:
        missing.add(key)
        return None
    operator = operators[0]
    target = predicate[operator]
    if operator == "eq":
        return value == target
    if operator == "ne":
        return value != target
    if operator == "in":
        return value in target
    if value is None:
        return False
    if isinstance(value, date):
        value = value.isoformat()
    if operator == "lt":
        return value < target
    if operator == "lte":
        return value <= target
    if operator == "gt":
        return value > target
    return value >= target


def applicability(
    frame: dict[str, Any], facts: dict[str, Any], as_of: date
) -> tuple[bool | None, list[str], str | None]:
    """Evaluate the seed fixture ScopeFrame following the shared clause order."""
    missing: set[str] = set()
    clauses: list[tuple[str, bool | None]] = []
    start = date.fromisoformat(frame["valid_from"])
    end = date.fromisoformat(frame["valid_to"]) if frame.get("valid_to") else None
    clauses.append(("in_force", start <= as_of and (end is None or as_of < end)))

    addressees = frame.get("addressee_classes", [])
    if not addressees:
        addressee_result: bool | None = True
    else:
        presence: list[bool | None] = []
        for actor in addressees:
            if actor in {"controller", "control_group"}:
                val, known = _fact_value(facts, "controller_present")
                if not known:
                    missing.add("controller_present")
                    presence.append(None)
                else:
                    presence.append(bool(val))
            elif actor == "interested_director":
                val, known = _fact_value(facts, "interested_director_involved")
                if not known:
                    missing.add("interested_director_involved")
                    presence.append(None)
                else:
                    presence.append(bool(val))
            else:
                presence.append(True)
        addressee_result = True if True in presence else None if None in presence else False
    clauses.append(("addressee", addressee_result))

    transaction_types = frame.get("transaction_types", [])
    if not transaction_types:
        transaction_result: bool | None = True
    else:
        transaction_value, known = _fact_value(facts, "transaction_type")
        if not known:
            missing.add("transaction_type")
            transaction_result = None
        else:
            transaction_result = transaction_value in transaction_types
    clauses.append(("transaction_type", transaction_result))
    clauses.append(("applies_when", evaluate(frame.get("applies_when"), facts, missing)))

    exceptions = frame.get("exceptions", [])
    exception_values = [
        evaluate(exception.get("when"), facts, missing) for exception in exceptions
    ]
    exception_result: bool | None
    if True in exception_values:
        exception_result = False
        true_index = exception_values.index(True)
        failed_exception = f"exception:{exceptions[true_index]['exception_id']}"
    elif None in exception_values:
        exception_result = None
        failed_exception = None
    else:
        exception_result = True
        failed_exception = None
    clauses.append(("exception", exception_result))

    if frame.get("non_application") is None:
        non_application = False
    else:
        non_application = evaluate(frame["non_application"], facts, missing)
    clauses.append(("non_application", None if non_application is None else not non_application))
    values = [value for _, value in clauses]
    result = False if False in values else None if None in values else True
    failed_clause = None
    if result is False:
        for name, value in clauses:
            if name == "exception" and failed_exception is not None and value is False:
                failed_clause = failed_exception
                break
            if name != "exception" and value is False:
                failed_clause = name
                break
    return result, sorted(missing), failed_clause


def _node_visible(node: dict[str, Any], as_of: date) -> bool:
    return date.fromisoformat(node["known_from"]) <= as_of


def _as_tri(value: bool | None) -> str:
    return "unknown" if value is None else "true" if value else "false"


def _collect_fact_keys(predicate: dict[str, Any] | None) -> list[str]:
    if predicate is None:
        return []
    keys: set[str] = set()
    if "fact" in predicate:
        keys.add(predicate["fact"])
    for operator in ("all", "any"):
        for child in predicate.get(operator, []):
            keys.update(_collect_fact_keys(child))
    if "not" in predicate:
        keys.update(_collect_fact_keys(predicate["not"]))
    return sorted(keys)


def _standard_result(as_of: date, facts: dict[str, Any]) -> dict[str, Any]:
    rules = [
        node
        for node in NODES.values()
        if node["node_type"] in {"Provision", "Holding"}
        and node.get("scope_frame") is not None
        and _node_visible(node, as_of)
    ]
    evaluated: list[dict[str, Any]] = []
    for node in rules:
        tri, missing, failed_clause = applicability(node["scope_frame"], facts, as_of)
        rule_predicate = node["props"].get(
            "safe_harbor_when" if node["node_type"] == "Provision" else "bjr_when"
        )
        evaluated.append(
            {
                "node": node,
                "tri": tri,
                "missing": missing,
                "failed_clause": failed_clause,
                "predicate": rule_predicate,
            }
        )

    statutes = [rule for rule in evaluated if rule["node"]["node_type"] == "Provision"]
    applicable_statutes = [rule for rule in statutes if rule["tri"] is True]
    unknown_rules = [rule for rule in evaluated if rule["tri"] is None]
    if applicable_statutes:
        safe_missing: set[str] = set()
        harbor = [
            evaluate(rule["predicate"], facts, safe_missing) for rule in applicable_statutes
        ]
        if True in harbor:
            safe_harbor: bool | None = True
            value = "business_judgment"
        elif None in harbor:
            safe_harbor = None
            value = "unknown"
        else:
            safe_harbor = False
            value = "entire_fairness"
        standard = {
            "value": value,
            "safe_harbor": _as_tri(safe_harbor),
            "basis": [rule["node"]["node_id"] for rule in applicable_statutes],
            "readings": (
                [
                    {
                        "regime": rule["node"]["node_id"],
                        "applies_if": [],
                        "requires": _collect_fact_keys(rule["predicate"]),
                        "would_yield": "depends",
                    }
                    for rule in applicable_statutes
                ]
                if safe_harbor is None
                else []
            ),
        }
    elif unknown_rules:
        standard = {
            "value": "unknown",
            "safe_harbor": "unknown",
            "basis": [rule["node"]["node_id"] for rule in unknown_rules],
            "readings": [
                {
                    "regime": rule["node"]["node_id"],
                    "applies_if": rule["missing"],
                    "requires": _collect_fact_keys(rule["predicate"]),
                    "would_yield": "depends",
                }
                for rule in unknown_rules
            ],
        }
    else:
        holdings = [
            rule
            for rule in evaluated
            if rule["node"]["node_type"] == "Holding" and rule["tri"] is True
        ]
        if not holdings:
            standard = {
                "value": "unknown",
                "safe_harbor": "unknown",
                "basis": [],
                "readings": [],
            }
        else:
            missing: set[str] = set()
            outcomes = [evaluate(rule["predicate"], facts, missing) for rule in holdings]
            outcome = True if True in outcomes else None if None in outcomes else False
            standard = {
                "value": (
                    "business_judgment"
                    if outcome is True
                    else "entire_fairness"
                    if outcome is False
                    else "unknown"
                ),
                "safe_harbor": "unknown" if outcome is None else _as_tri(outcome),
                "basis": [rule["node"]["node_id"] for rule in holdings],
                "readings": (
                    [
                        {
                            "regime": rule["node"]["node_id"],
                            "applies_if": [],
                            "requires": _collect_fact_keys(rule["predicate"]),
                            "would_yield": "depends",
                        }
                        for rule in holdings
                    ]
                    if outcome is None
                    else []
                ),
            }

    condition_results: list[dict[str, Any]] = []
    for node in NODES.values():
        if node["node_type"] != "Element":
            continue
        missing: set[str] = set()
        met = evaluate(node["props"].get("met_when"), facts, missing)
        if met is True:
            status = "met"
        else:
            status = "unknown"
        condition_results.append(
            {
                "condition_id": node["node_id"],
                "label": node["label"],
                "status": status,
                "fact_keys": node["props"].get("fact_keys", []),
                "spans": [],
                "reason": f"Mock fixture evaluation: {status}",
            }
        )
    return {"standard": standard, "conditions": condition_results}


app = FastAPI(title="Track A local Graph API mock", version="1.0.0")
install_contract_middleware(app)


@app.get("/health")
def health() -> dict[str, str]:
    return {
        "status": "ok",
        "service": "graph-api-mock",
        "version": "1.0.0",
        "contract_version": "1.0.0",
    }


@app.get("/graph/v1/nodes/{node_id}")
def get_node(
    node_id: str,
    as_of: Annotated[date | None, Query()] = None,
) -> dict[str, Any]:
    node = NODES.get(node_id)
    if node is None:
        raise HTTPException(status_code=404, detail="NODE_NOT_FOUND")
    requested_date = as_of or date.today()
    if not _node_visible(node, requested_date):
        raise HTTPException(status_code=404, detail="NOT_KNOWN_AS_OF")
    return node


@app.get("/graph/v1/nodes/{node_id}/edges")
def get_edges(
    node_id: str,
    as_of: date | None = None,
    edge_type: str | None = None,
    direction: str = "both",
) -> dict[str, list[dict[str, Any]]]:
    requested_date = as_of or date.today()
    if direction not in {"in", "out", "both"}:
        raise HTTPException(status_code=422, detail="direction must be in, out, or both")
    if node_id not in NODES:
        raise HTTPException(status_code=404, detail="NODE_NOT_FOUND")
    visible = []
    for edge in EDGES:
        if date.fromisoformat(edge["known_from"]) > requested_date:
            continue
        if not _node_visible(NODES[edge["src"]], requested_date):
            continue
        if not _node_visible(NODES[edge["dst"]], requested_date):
            continue
        matches_direction = (
            (direction in {"out", "both"} and edge["src"] == node_id)
            or (direction in {"in", "both"} and edge["dst"] == node_id)
        )
        if matches_direction and (edge_type is None or edge["edge_type"] == edge_type):
            visible.append(edge)
    return {"items": visible}


@app.post("/graph/v1/query/scope_frame")
def scope_frame(body: ScopeFrameRequest) -> dict[str, Any]:
    node = NODES.get(body.node_id)
    if node is None or not _node_visible(node, body.as_of):
        raise HTTPException(
            status_code=404,
            detail="NODE_NOT_FOUND" if node is None else "NOT_KNOWN_AS_OF",
        )
    return {"node_id": body.node_id, "frame": node.get("scope_frame")}


@app.post("/graph/v1/query/validity")
def validity(body: ValidityRequest) -> dict[str, Any]:
    node = NODES.get(body.node_id)
    if node is None or not _node_visible(node, body.as_of):
        raise HTTPException(
            status_code=404,
            detail="NODE_NOT_FOUND" if node is None else "NOT_KNOWN_AS_OF",
        )
    start = date.fromisoformat(node["valid_from"]) if node.get("valid_from") else None
    end = date.fromisoformat(node["valid_to"]) if node.get("valid_to") else None
    in_force = start is None or start <= body.as_of
    in_force = in_force and (end is None or body.as_of < end)
    treatments = []
    for edge in EDGES:
        if edge["dst"] != body.node_id:
            continue
        if edge["edge_type"] not in {"overrules", "superseded_by_statute", "limits"}:
            continue
        effective = date.fromisoformat(edge.get("valid_from") or edge["known_from"])
        if effective <= body.as_of and _node_visible(NODES[edge["src"]], body.as_of):
            treatments.append(
                {
                    "edge": edge,
                    "effective_date": effective.isoformat(),
                    "by_node_id": edge["src"],
                }
            )
    kinds = {item["edge"]["edge_type"] for item in treatments}
    good_law = (
        "overruled"
        if "overrules" in kinds
        else "superseded"
        if "superseded_by_statute" in kinds
        else "limited"
        if "limits" in kinds
        else "good"
    )
    return {
        "node_id": body.node_id,
        "as_of": body.as_of.isoformat(),
        "in_force": "true" if in_force else "false",
        "good_law_unconditional": good_law,
        "treatments": treatments,
        "reasons": [f"Fixture validity: in_force={in_force}"],
    }


@app.post("/graph/v1/query/rule_candidates")
def rule_candidates(body: RuleCandidatesRequest) -> dict[str, list[dict[str, Any]]]:
    terms = set(re.findall(r"[a-z0-9]+", body.query.lower()))
    items = []
    for node in NODES.values():
        if node["node_type"] not in body.node_types or node.get("scope_frame") is None:
            continue
        if not _node_visible(node, body.as_of):
            continue
        label_terms = set(re.findall(r"[a-z0-9]+", node["label"].lower()))
        score = len(terms & label_terms) / max(len(terms), 1)
        items.append({"node": node, "score": float(score), "spans": []})
    items.sort(key=lambda item: (-item["score"], item["node"]["node_id"]))
    return {"items": items[: body.top_k]}


@app.post("/graph/v1/query/standard_of_review")
def standard_of_review(body: StandardRequest) -> dict[str, Any]:
    return _standard_result(body.as_of, body.facts)


@app.post("/graph/v1/search/documents")
def search_documents(body: DocumentSearchRequest) -> dict[str, list[Any]]:
    return {"items": []}


@app.get("/graph/v1/documents/{doc_id}/meta")
def document_meta(doc_id: str) -> dict[str, Any]:
    if doc_id not in DOCUMENTS:
        raise HTTPException(status_code=404, detail="DOC_NOT_FOUND")
    return DOCUMENTS[doc_id]["meta"]


@app.get("/graph/v1/documents/{doc_id}/text", response_class=PlainTextResponse)
def document_text(doc_id: str) -> str:
    if doc_id not in DOCUMENTS:
        raise HTTPException(status_code=404, detail="DOC_NOT_FOUND")
    return DOCUMENTS[doc_id]["text"]


@app.post("/graph/v1/documents/register", status_code=201)
def register_document(body: RegisterDocumentRequest) -> dict[str, Any]:
    source = body.source
    doc_id = source["doc_id"]
    if doc_id in DOCUMENTS:
        # Explicitly seeded canonical fixture metadata is immutable on re-registration.
        return DOCUMENTS[doc_id]["meta"]
    meta = {
        "doc_id": doc_id,
        "source": source,
        "title": None,
        "doc_type": source.get("expected_doc_type"),
        "doc_date": None,
        "canon_version": source.get("canon_version") or "1",
        "char_length": 0,
        "page_markers": [],
        "node_ids": [],
    }
    DOCUMENTS[doc_id] = {"meta": meta, "text": ""}
    return meta


@app.post("/graph/v1/extract/facts", status_code=202)
def extract_facts(body: ExtractRequest) -> dict[str, str]:
    job_id = "job_" + str(ULID())
    JOBS[job_id] = {
        "job_id": job_id,
        "status": "done",
        "result": [],
        "error": None,
    }
    return {"job_id": job_id}


@app.get("/graph/v1/jobs/{job_id}")
def job_status(job_id: str) -> dict[str, Any]:
    job = JOBS.get(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="JOB_NOT_FOUND")
    return job


@app.post("/graph/v1/gnn/score_edges")
def score_edges(body: ScoreEdgesRequest) -> dict[str, Any]:
    scores = []
    for probe in body.edges:
        exists = any(
            edge["src"] == probe["src"]
            and edge["dst"] == probe["dst"]
            and edge["edge_type"] == probe["edge_type"]
            for edge in EDGES
        )
        score = 0.9 if exists else 0.1
        scores.append(
            {
                **probe,
                "score_raw": score,
                "score_calibrated": score,
                "flag": score < 0.3,
            }
        )
    return {"model_version": "mock-seed-v1", "scores": scores}


@app.post("/graph/v1/gnn/predict_missing")
def predict_missing(body: PredictMissingRequest) -> dict[str, Any]:
    visible_ids = {
        node_id
        for node_id, node in NODES.items()
        if node["node_type"] in {"Case", "Holding"} and _node_visible(node, body.as_of)
    }
    items = [
        {
            "node_id": node_id,
            "score": 0.5,
            "anchor_links": [
                {"src": anchor, "dst": node_id, "edge_type": "addresses"}
                for anchor in body.anchor_node_ids
            ],
        }
        for node_id in sorted(visible_ids - set(body.exclude_node_ids))
    ]
    return {"model_version": "mock-seed-v1", "items": items[: body.top_k]}


@app.get("/graph/v1/registry")
def registry() -> dict[str, Any]:
    entries = [
        ("sec_edgar", ["www.sec.gov", "data.sec.gov", "efts.sec.gov"], ["READ", "SEARCH"]),
        ("de_code_online", ["delcode.delaware.gov"], ["READ", "SEARCH"]),
        ("de_legislature", ["legis.delaware.gov"], ["READ", "SEARCH"]),
        ("de_courts", ["courts.delaware.gov"], ["READ", "SEARCH"]),
        (
            "courtlistener",
            ["www.courtlistener.com", "storage.courtlistener.com"],
            ["READ", "SEARCH"],
        ),
    ]
    return {
        "version": 1,
        "entries": [
            {
                "entry_id": entry_id,
                "name": entry_id,
                "role": "primary" if entry_id != "courtlistener" else "crosscheck",
                "authoritative": entry_id != "courtlistener",
                "base_domains": domains,
                "url_prefixes": [f"https://{domain}/" for domain in domains],
                "doc_types": [],
                "access": "api" if entry_id in {"sec_edgar", "courtlistener"} else "adapter",
                "requires_login": False,
                "rate_limit": {
                    "requests_per_second": 1,
                    "burst": 2,
                    "max_concurrent": 1,
                },
                "robots_respect": True,
                "allowed_action_classes": actions,
                "terms_verified": False,
                "terms_url": None,
                "terms_checked_on": None,
                "phase": "MVP",
            }
            for entry_id, domains, actions in entries
        ],
    }


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("tests.mocks.mock_graph:app", host="127.0.0.1", port=8101)
