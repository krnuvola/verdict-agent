"""Persist run snapshots, evidence-backed facts, claims, and guard reports."""

from __future__ import annotations

import json
from datetime import UTC, date, datetime
from decimal import Decimal
from hashlib import sha256
from typing import TYPE_CHECKING, Any

from sqlalchemy import delete, select
from sqlalchemy.orm import Session, sessionmaker

from gba_agent.api.contracts_compat import (
    ClaimModel,
    CreateRunReq,
    GuardReport,
    SourceRecord,
    _CompatMatter,
    _CompatMatterFact,
)
from gba_agent.api.errors import APIError
from gba_agent.api.trace import new_ulid
from gba_agent.db.models import Claim, Handoff, IdempotencyRecord, Run
from gba_agent.db.models import GuardReport as ReportRow
from gba_agent.db.models import SourceRecord as SourceRow
from gba_agent.db.repositories import (
    ClaimsRepository,
    GuardReportsRepository,
    IdempotencyRepository,
    RunsRepository,
    SourceRecordsRepository,
)
from gba_agent.db.session import session_scope
from gba_agent.orchestrator.state import TERMINAL_STATES, TRANSITIONS, Step

if TYPE_CHECKING:
    from gba_agent.access.fetcher import BlobStore
    from gba_agent.api.contracts_compat import _CompatClaim, _CompatGuardReport, _CompatSourceRecord

FACT_KEYS = frozenset(
    {
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
)
_DATES = {"signing_date", "announcement_date", "closing_date", "litigation_commenced_on"}
_TRANSACTIONS = {
    "going_private",
    "other_controller_deal",
    "third_party_sale",
    "interested_director",
    "other",
}


def fact_value(key: str, value: Any) -> Any:
    if key not in FACT_KEYS:
        raise ValueError("Unknown matter fact key")
    if key in _DATES:
        if value is None or type(value) is date:
            return value.isoformat() if value is not None else None
        if isinstance(value, str) and date.fromisoformat(value).isoformat() == value:
            return value
    elif key == "transaction_type":
        if isinstance(value, str) and value in _TRANSACTIONS:
            return value
    elif key == "controller_identity":
        if isinstance(value, str):
            return value
    elif key == "deal_value_usd":
        if type(value) in (int, float) and Decimal(str(value)).is_finite():
            return value
    elif type(value) is bool:
        return value
    raise ValueError("Invalid matter fact value")


def validate_matter(matter: _CompatMatter) -> None:
    try:
        for key, fact in matter.facts.items():
            if key != fact.key or key not in FACT_KEYS:
                raise ValueError("Unknown or inconsistent matter fact key")
            if fact.status == "known":
                fact_value(key, fact.value)
    except ValueError as exc:
        raise APIError(422, "VALIDATION_ERROR", str(exc)) from None


def merge_fact(matter: _CompatMatter, fact: _CompatMatterFact) -> None:
    previous = matter.facts.get(fact.key)
    if previous is None or previous.status == "unknown":
        matter.facts[fact.key] = fact
        return
    spans = list(previous.spans)
    spans.extend(span for span in fact.spans if span not in spans)
    if previous.extracted_by == "human":
        matter.facts[fact.key] = (
            previous.model_copy(update={"spans": spans})
            if (previous.status == fact.status and previous.value == fact.value)
            else previous
        )
    elif previous.status == "conflicting" or previous.value != fact.value:
        matter.facts[fact.key] = previous.model_copy(
            update={"status": "conflicting", "value": None, "spans": spans, "confidence": None}
        )
    else:
        matter.facts[fact.key] = previous.model_copy(update={"spans": spans})


class MatterStore:
    def __init__(self, database: sessionmaker[Session]) -> None:
        self.database = database

    def create(
        self, body: CreateRunReq, idempotency_key: str | None, request_id: str
    ) -> tuple[str, bool]:
        payload = body.model_dump(mode="json")
        digest = sha256(
            json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
        key = f"orch:v1:runs:{idempotency_key}" if idempotency_key else None
        with session_scope(self.database) as db:
            receipts = IdempotencyRepository(db)
            if key and (existing := receipts.find_unexpired(key)) is not None:
                if existing.request_hash != digest:
                    raise APIError(
                        422, "VALIDATION_ERROR", "Idempotency key identifies another request"
                    )
                return str(existing.response["run_id"]), False
            run_id = f"run_{new_ulid()}"
            config = {
                **payload["config"],
                "_draft_doc_id": body.draft_doc_id,
                "_playbook_id": body.playbook_id,
                "_request_id": request_id,
            }
            db.add(
                Run(
                    run_id=run_id,
                    matter_id=body.matter.matter_id,
                    state="queued",
                    step=Step.INTAKE,
                    config=config,
                    matter=payload["matter"],
                )
            )
            db.flush()
            if key:
                receipts.put(
                    IdempotencyRecord(key=key, request_hash=digest, response={"run_id": run_id})
                )
            return run_id, True

    def row(self, run_id: str) -> Run:
        with self.database() as db:
            row = db.get(Run, run_id)
            if row is None:
                raise APIError(404, "RUN_NOT_FOUND", "Run was not found")
            return row

    @staticmethod
    def request(row: Run) -> CreateRunReq:
        return CreateRunReq.model_validate(
            {
                "matter": row.matter,
                "config": {
                    key: value for key, value in row.config.items() if not key.startswith("_")
                },
                "draft_doc_id": row.config.get("_draft_doc_id"),
                "playbook_id": row.config.get("_playbook_id"),
            }
        )

    def transition(self, run_id: str, step: Step) -> str | None:
        with session_scope(self.database) as db:
            row = RunsRepository(db).lock(run_id)
            if row is None or row.state in TERMINAL_STATES:
                return None
            previous = Step(row.step or Step.INTAKE)
            if (
                previous != step
                and step != Step.DOSSIER
                and step not in TRANSITIONS.get(previous, set())
            ):
                raise RuntimeError("Invalid orchestrator transition")
            row.step, row.state, row.updated_at = step, "running", datetime.now(UTC)
            return "queued" if previous == step == Step.INTAKE else previous

    def terminal(self, run_id: str, state: str, error: str | None = None) -> str | None:
        with session_scope(self.database) as db:
            row = RunsRepository(db).lock(run_id)
            if row is None or row.state in TERMINAL_STATES:
                return None
            previous = row.step or Step.INTAKE
            row.state, row.error, row.updated_at = state, error, datetime.now(UTC)
            if state in {"failed", "done"}:
                row.step = Step.FAILED if state == "failed" else Step.DONE
            return previous

    def recover(self) -> tuple[list[str], list[str]]:
        with session_scope(self.database) as db:
            failed = list(
                db.scalars(select(Run.run_id).where(Run.state.in_(("running", "waiting_handoff"))))
            )
            queued = list(db.scalars(select(Run.run_id).where(Run.state == "queued")))
        return failed, queued

    def handoffs(self, run_id: str) -> list[str]:
        with self.database() as db:
            return list(
                db.scalars(
                    select(Handoff.handoff_id)
                    .where(
                        Handoff.run_id == run_id,
                        Handoff.status == "open",
                    )
                    .order_by(Handoff.handoff_id)
                )
            )

    def save_matter(self, run_id: str, matter: _CompatMatter) -> None:
        with session_scope(self.database) as db:
            row = RunsRepository(db).lock(run_id)
            if row is not None and row.state not in TERMINAL_STATES:
                snapshot = matter.model_dump(mode="json")
                if row.matter.get("facts") != snapshot["facts"]:
                    claims = select(Claim.claim_id).where(Claim.run_id == run_id)
                    db.execute(delete(ReportRow).where(ReportRow.claim_id.in_(claims)))
                row.matter, row.updated_at = snapshot, datetime.now(UTC)

    def usage(self, run_id: str, steps: int, cost: Decimal) -> None:
        with session_scope(self.database) as db:
            row = RunsRepository(db).lock(run_id)
            if row is not None and row.state not in TERMINAL_STATES:
                row.steps_used, row.cost_usd = steps, cost

    def save_claim(self, run_id: str, claim: _CompatClaim) -> None:
        with session_scope(self.database) as db:
            ClaimsRepository(db).add(
                Claim(
                    claim_id=claim.claim_id,
                    run_id=run_id,
                    claim=claim.model_dump(mode="json"),
                )
            )

    def save_report(self, report: _CompatGuardReport) -> None:
        with session_scope(self.database) as db:
            db.get(Claim, report.claim_id, with_for_update=True)
            GuardReportsRepository(db).save_latest(report.claim_id, report.model_dump(mode="json"))

    def claims(self, run_id: str) -> list[tuple[_CompatClaim, _CompatGuardReport | None]]:
        with self.database() as db:
            rows = db.scalars(select(Claim).where(Claim.run_id == run_id).order_by(Claim.claim_id))
            repo = GuardReportsRepository(db)
            return [
                (
                    ClaimModel.model_validate(row.claim),
                    GuardReport.model_validate(report.report)
                    if (report := repo.get(row.claim_id))
                    else None,
                )
                for row in rows
            ]

    def save_dossier(self, run_id: str, dossier: dict[str, Any]) -> None:
        with session_scope(self.database) as db:
            row = RunsRepository(db).lock(run_id)
            if row is not None and row.state not in TERMINAL_STATES:
                row.config = {**row.config, "_dossier": dossier}

    def recording_failed(self, run_id: str) -> None:
        """A terminal result cannot be marked successful when its audit write failed."""
        with session_scope(self.database) as db:
            row = RunsRepository(db).lock(run_id)
            if row is not None:
                row.state, row.step, row.error = "failed", Step.FAILED, "INTERNAL_ERROR"
                row.updated_at = datetime.now(UTC)

    def source(self, source_id: str) -> _CompatSourceRecord | None:
        with self.database() as db:
            row = SourceRecordsRepository(db).get(source_id)
            return (
                SourceRecord.model_validate(SourceRecordsRepository.contract_data(row))
                if row
                else None
            )

    def admitted(self, doc_id: str, as_of: date, source_id: str | None = None) -> str | None:
        with self.database() as db:
            query = (
                select(SourceRow)
                .where(
                    SourceRow.admitted.is_(True),
                    SourceRow.record["doc_id"].as_string() == doc_id,
                )
                .order_by(SourceRow.source_id)
            )
            if source_id is not None:
                query = query.where(SourceRow.source_id == source_id)
            for row in db.scalars(query):
                value = SourceRecordsRepository.gate_data(row).get("doc_date")
                try:
                    if value is None:
                        observed = row.record.get("retrieved_at")
                        if (
                            isinstance(observed, str)
                            and datetime.fromisoformat(observed).date() <= as_of
                        ):
                            return row.source_id
                    elif isinstance(value, str) and date.fromisoformat(value) <= as_of:
                        return row.source_id
                except ValueError:
                    continue
        return None

    def sources_for(self, doc_id: str) -> list[_CompatSourceRecord]:
        with self.database() as db:
            query = select(SourceRow).where(SourceRow.record["doc_id"].as_string() == doc_id)
            return [
                SourceRecord.model_validate(SourceRecordsRepository.contract_data(row))
                for row in db.scalars(query)
            ]

    def canonical(self, blobs: BlobStore, doc_id: str, as_of: date) -> str | None:
        if self.admitted(doc_id, as_of) is None:
            return None
        try:
            return blobs.get(f"canon/{doc_id.removeprefix('sha256:')}.v1.txt").decode()
        except (KeyError, OSError, UnicodeError):
            return None
