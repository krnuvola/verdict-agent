"""Build a stable dossier from persisted guard results and Graph findings."""

from __future__ import annotations

from copy import deepcopy
from datetime import datetime
from pathlib import Path
from typing import Any

import yaml  # type: ignore[import-untyped]

from gba_agent.api.contracts_compat import Dossier, _CompatGuardReport, _CompatMatter
from gba_agent.orchestrator.matter_store import FACT_KEYS, MatterStore

UNKNOWN_STANDARD: dict[str, Any] = {
    "value": "unknown",
    "safe_harbor": "unknown",
    "basis": [],
    "readings": [],
}


def required_facts(standard: dict[str, Any]) -> list[str]:
    return list(
        dict.fromkeys(
            key
            for item in standard.get("readings", [])
            for field in ("applies_if", "requires")
            for key in item.get(field, [])
            if key in FACT_KEYS
        )
    )


def public_report(report: _CompatGuardReport) -> dict[str, Any]:
    data = report.model_dump(mode="json")
    for result in data["results"]:
        result["evidence"].pop("human_only", None)
    return data


def build(
    run_id: str,
    matter: _CompatMatter,
    store: MatterStore,
    generated_at: datetime,
    standard: dict[str, Any],
    conditions: list[dict[str, Any]],
    notices: list[dict[str, str]],
    labels: dict[str, str],
    trajectory_path: Path,
    questions_path: Path,
) -> dict[str, Any]:
    questions = yaml.safe_load(questions_path.read_text(encoding="utf-8"))
    standard, conditions = deepcopy(standard), deepcopy(conditions)
    missing = dict.fromkeys(required_facts(standard))
    authorities, dropped = [], []
    abstentions: list[dict[str, Any]] = list(notices)
    for claim, report in store.claims(run_id):
        provenance = {
            "claim_id": claim.claim_id,
            "source_id": claim.source_id,
            "doc_id": claim.source_doc_id,
            "span": claim.span.model_dump(mode="json") if claim.span else None,
            "proposed_by": claim.proposed_by,
        }
        if report is None:
            abstentions.append(
                {
                    "item": claim.claim_id,
                    "reason_code": "CV_SUPPORT_UNCERTAIN",
                    "reason_text": "Unverified claim; lawyer review required.",
                    "status": "unverified",
                    "provenance": provenance,
                }
            )
            continue
        missing.update(dict.fromkeys(report.missing_facts))
        if report.final == "block":
            dropped.append(public_report(report))
        elif report.final == "unknown":
            abstentions.append(
                {
                    "item": claim.claim_id,
                    "reason_code": report.abstain_reason or "CV_SUPPORT_UNCERTAIN",
                    "reason_text": next(
                        (
                            result.reason_text
                            for result in report.results
                            if result.reason_code == report.abstain_reason
                        ),
                        "The claim did not meet the confidence threshold.",
                    ),
                    "status": "unknown",
                    "provenance": provenance,
                    "guard_report": public_report(report),
                }
            )
        elif (
            claim.authority_node_id
            and claim.source_id
            and claim.source_doc_id
            and claim.span
            and claim.span.doc_id == claim.source_doc_id
            and claim.span.end > claim.span.start
            and claim.as_of == matter.as_of
            and claim.matter_id == matter.matter_id
            and all(
                any(r.guard == name and r.verdict == "pass" for r in report.results)
                for name in ("source_gate", "claim_verifier", "scope", "temporal")
            )
            and not any(r.verdict in {"block", "unknown"} for r in report.results)
            and store.admitted(claim.source_doc_id, claim.as_of, claim.source_id)
        ):
            authorities.append(
                {
                    "node_id": claim.authority_node_id,
                    "label": labels.get(claim.authority_node_id, claim.authority_node_id),
                    "proposition": claim.text,
                    "guard_report": public_report(report),
                    "spans": [claim.span.model_dump(mode="json")] if claim.span else [],
                    "source_id": claim.source_id,
                }
            )
        else:
            abstentions.append(
                {
                    "item": claim.claim_id,
                    "reason_code": "CV_SUPPORT_UNCERTAIN",
                    "reason_text": "Unverified evidence or incomplete guards; "
                    "claim is not established law.",
                    "status": "unverified",
                    "provenance": provenance,
                    "guard_report": public_report(report),
                }
            )
    verified_nodes = {item["node_id"] for item in authorities}
    if (standard["value"] != "unknown" or standard["safe_harbor"] != "unknown") and (
        not standard["basis"] or not set(standard["basis"]).issubset(verified_nodes)
    ):
        abstentions.append(
            {
                "item": "standard_of_review",
                "reason_code": "CV_SUPPORT_UNCERTAIN",
                "reason_text": "Graph standard lacks fully verified cited authorities; "
                "lawyer review required.",
                "graph_response": standard,
            }
        )
        standard = {**standard, "value": "unknown", "safe_harbor": "unknown"}
    for condition in conditions:
        if any(
            key not in matter.facts or matter.facts[key].status != "known"
            for key in condition["fact_keys"]
        ):
            condition["status"] = "unknown"
            condition["reason"] = "Missing or conflicting facts; lawyer review required."
        spans = [
            span.model_dump(mode="json")
            for key in condition["fact_keys"]
            if (fact := matter.facts.get(key)) is not None
            for span in fact.spans
        ]
        condition["spans"] = list(
            {(span["doc_id"], span["start"], span["end"]): span for span in spans}.values()
        )
    for key, fact in sorted(matter.facts.items()):
        if fact.status == "conflicting":
            missing[key] = None
            abstentions.append(
                {
                    "item": key,
                    "reason_code": "SCOPE_FACTS_MISSING",
                    "status": "disputed",
                    "reason_text": "Conflicting evidence; lawyer resolution required.",
                    "spans": [span.model_dump(mode="json") for span in fact.spans],
                }
            )
    dossier = Dossier.model_validate(
        {
            "run_id": run_id,
            "matter": matter,
            "generated_at": generated_at,
            "standard_of_review": standard,
            "conditions": conditions,
            "authorities": authorities,
            "dropped_claims": dropped,
            "open_questions": [
                questions[key]
                for key in missing
                if key in questions
                and (key not in matter.facts or matter.facts[key].status != "known")
            ],
            "abstentions": abstentions,
            "trajectory_path": str(trajectory_path),
        }
    )
    data: dict[str, Any] = dossier.model_dump(mode="json")
    return data
