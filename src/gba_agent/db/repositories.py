"""Typed, idempotent persistence operations for agentdb records."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import inspect, select
from sqlalchemy.dialects.postgresql import insert as postgresql_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from gba_agent.db.models import (
    ActionLog,
    Base,
    BrowserSession,
    Claim,
    CrosscheckCache,
    GuardReport,
    Handoff,
    IdempotencyRecord,
    PlaybookProposal,
    Run,
    SourceRecord,
)


class RepositoryConflict(ValueError):
    """Raised when an idempotency key is reused with different stored data."""


class Repository[ModelT: Base]:
    """Common create-once behavior keyed by a model's primary key."""

    model: type[ModelT]

    def __init__(self, session: Session) -> None:
        self.session = session

    def get(self, identity: Any) -> ModelT | None:
        """Return a record by primary key."""
        return self.session.get(self.model, identity)

    def create_once(self, record: ModelT) -> tuple[ModelT, bool]:
        """Insert once; return the persisted value and whether it was created."""
        state = inspect(record)
        primary_key = tuple(
            getattr(record, column.key or column.name) for column in state.mapper.primary_key
        )
        if any(value is None for value in primary_key):
            raise ValueError(f"{self.model.__name__} requires an explicit primary key")
        identity: Any = primary_key[0] if len(primary_key) == 1 else primary_key

        existing = self.session.get(self.model, identity)
        if existing is not None:
            self._assert_same_request(record, existing)
            return existing, False

        try:
            with self.session.begin_nested():
                self.session.add(record)
                self.session.flush()
        except IntegrityError:
            existing = self.session.get(self.model, identity)
            if existing is None:
                raise
            self._assert_same_request(record, existing)
            return existing, False
        return record, True

    def _assert_same_request(self, requested: ModelT, stored: ModelT) -> None:
        request_state = inspect(requested)
        for column in request_state.mapper.column_attrs:
            key = column.key
            if key not in request_state.dict or key in {
                primary.key for primary in request_state.mapper.primary_key
            }:
                continue
            requested_value = getattr(requested, key)
            if getattr(stored, key) != requested_value:
                raise RepositoryConflict(
                    f"{self.model.__tablename__} primary key already exists with different {key}"
                )


class RunsRepository(Repository[Run]):
    model = Run

    def add(self, run: Run) -> tuple[Run, bool]:
        return self.create_once(run)

    def lock(self, run_id: str) -> Run | None:
        return self.session.get(Run, run_id, with_for_update=True)


class SourceRecordsRepository(Repository[SourceRecord]):
    model = SourceRecord

    @staticmethod
    def contract_data(row: SourceRecord) -> dict[str, Any]:
        return {key: value for key, value in row.record.items() if key != "_source_gate"}

    @staticmethod
    def gate_data(row: SourceRecord) -> dict[str, Any]:
        metadata = row.record.get("_source_gate")
        return dict(metadata) if isinstance(metadata, dict) else {}

    def lock(self, source_id: str) -> SourceRecord | None:
        return self.session.get(SourceRecord, source_id, with_for_update=True)

    def add(self, record: SourceRecord) -> tuple[SourceRecord, bool]:
        return self.create_once(record)

    def for_url(self, url: str, entry_id: str) -> list[SourceRecord]:
        return list(
            self.session.scalars(
                select(SourceRecord)
                .where(
                    SourceRecord.record["url"].as_string() == url,
                    SourceRecord.record["registry_entry_id"].as_string() == entry_id,
                )
                .order_by(SourceRecord.created_at.desc(), SourceRecord.source_id.desc())
            )
        )


class ClaimsRepository(Repository[Claim]):
    model = Claim

    def add(self, claim: Claim) -> tuple[Claim, bool]:
        return self.create_once(claim)


class GuardReportsRepository(Repository[GuardReport]):
    model = GuardReport

    def put(self, report: GuardReport) -> tuple[GuardReport, bool]:
        return self.create_once(report)

    def save_latest(self, claim_id: str, report: dict[str, Any]) -> None:
        """Update the latest report while holding the corresponding claim's row lock."""
        existing = self.get(claim_id)
        if existing is None:
            self.session.add(GuardReport(claim_id=claim_id, report=report))
        else:
            existing.report = report


class SessionsRepository(Repository[BrowserSession]):
    model = BrowserSession

    def add(self, browser_session: BrowserSession) -> tuple[BrowserSession, bool]:
        return self.create_once(browser_session)

    def find_latest(self, site_id: str, principal: str) -> BrowserSession | None:
        return self.session.scalar(
            select(BrowserSession)
            .where(BrowserSession.site_id == site_id, BrowserSession.principal == principal)
            .order_by(BrowserSession.created_at.desc(), BrowserSession.session_id.desc())
            .limit(1)
        )


class HandoffsRepository(Repository[Handoff]):
    model = Handoff

    def add(self, handoff: Handoff) -> tuple[Handoff, bool]:
        return self.create_once(handoff)

    def lock(self, handoff_id: str) -> Handoff | None:
        return self.session.get(Handoff, handoff_id, with_for_update=True)

    def list_by_status(self, status: str) -> list[Handoff]:
        return list(
            self.session.scalars(
                select(Handoff)
                .where(Handoff.status == status)
                .order_by(Handoff.created_at, Handoff.handoff_id)
            )
        )


class ActionLogRepository(Repository[ActionLog]):
    model = ActionLog

    def add_once(self, action: ActionLog) -> tuple[ActionLog, bool]:
        return self.create_once(action)


class PlaybookProposalsRepository(Repository[PlaybookProposal]):
    model = PlaybookProposal

    def add_once(self, proposal: PlaybookProposal) -> tuple[PlaybookProposal, bool]:
        return self.create_once(proposal)

    def propose(self, proposal: PlaybookProposal) -> PlaybookProposal:
        self.session.add(proposal)
        self.session.flush()
        return proposal

    def lock(self, proposal_id: int) -> PlaybookProposal | None:
        return self.session.get(PlaybookProposal, proposal_id, with_for_update=True)


class CrosscheckCacheRepository(Repository[CrosscheckCache]):
    model = CrosscheckCache

    def put(self, record: CrosscheckCache) -> tuple[CrosscheckCache, bool]:
        return self.create_once(record)

    def store(self, citation: str, result: dict[str, Any], checked_at: datetime) -> None:
        dialect = self.session.get_bind().dialect.name
        values = {"citation": citation, "result": result, "checked_at": checked_at}
        if dialect == "postgresql":
            pg = postgresql_insert(CrosscheckCache).values(**values)
            self.session.execute(
                pg.on_conflict_do_update(
                    index_elements=["citation"],
                    set_={"result": result, "checked_at": checked_at},
                )
            )
        elif dialect == "sqlite":
            sqlite = sqlite_insert(CrosscheckCache).values(**values)
            self.session.execute(
                sqlite.on_conflict_do_update(
                    index_elements=["citation"],
                    set_={"result": result, "checked_at": checked_at},
                )
            )
        else:
            raise ValueError("Unsupported cross-check cache database")


class IdempotencyRepository(Repository[IdempotencyRecord]):
    model = IdempotencyRecord

    def put(
        self, record: IdempotencyRecord, *, now: datetime | None = None
    ) -> tuple[IdempotencyRecord, bool]:
        current_time = now or datetime.now(UTC)
        existing = self.find_unexpired(record.key, now=current_time)
        if existing is not None:
            if existing.request_hash != record.request_hash:
                raise RepositoryConflict(
                    "idempotency key already exists with a different request hash"
                )
            return existing, False
        previous = self.get(record.key)
        if previous is not None:
            previous.request_hash = record.request_hash
            previous.response = record.response
            previous.created_at = current_time
            self.session.flush()
            return previous, True
        return self.create_once(record)

    def find_unexpired(self, key: str, *, now: datetime | None = None) -> IdempotencyRecord | None:
        """Return the matching response while its 24-hour idempotency window is active."""
        cutoff = (now or datetime.now(UTC)) - timedelta(hours=24)
        return self.session.scalar(
            select(IdempotencyRecord).where(
                IdempotencyRecord.key == key,
                IdempotencyRecord.created_at > cutoff,
            )
        )
