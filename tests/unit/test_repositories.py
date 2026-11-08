"""Typed repository behavior and migration schema tests."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, event, inspect
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
from gba_agent.db.repositories import (
    ActionLogRepository,
    ClaimsRepository,
    CrosscheckCacheRepository,
    GuardReportsRepository,
    HandoffsRepository,
    IdempotencyRepository,
    PlaybookProposalsRepository,
    RepositoryConflict,
    RunsRepository,
    SessionsRepository,
    SourceRecordsRepository,
)

TABLE_NAMES = {
    "runs",
    "source_records",
    "claims",
    "guard_reports",
    "sessions",
    "handoffs",
    "action_log",
    "playbook_proposals",
    "crosscheck_cache",
    "idempotency",
}
EXPECTED_COLUMNS = {
    "runs": {
        "run_id",
        "matter_id",
        "state",
        "step",
        "config",
        "matter",
        "steps_used",
        "cost_usd",
        "error",
        "created_at",
        "updated_at",
    },
    "source_records": {
        "source_id",
        "run_id",
        "record",
        "admitted",
        "admission_reason",
        "created_at",
    },
    "claims": {"claim_id", "run_id", "claim", "created_at"},
    "guard_reports": {"claim_id", "report", "created_at"},
    "sessions": {
        "session_id",
        "site_id",
        "principal",
        "state",
        "enc_path",
        "created_at",
        "last_used_at",
        "expires_at",
    },
    "handoffs": {"handoff_id", "run_id", "request", "status", "created_at", "resolved_at"},
    "action_log": {"id", "run_id", "ts", "action", "decision", "executed", "result"},
    "playbook_proposals": {
        "id",
        "site_id",
        "task",
        "base_version",
        "diff",
        "status",
        "created_at",
        "decided_by",
        "decided_at",
    },
    "crosscheck_cache": {"citation", "result", "checked_at"},
    "idempotency": {"key", "request_hash", "response", "created_at"},
}


@pytest.fixture
def db_session(tmp_path: Path) -> Any:
    engine = create_engine(f"sqlite:///{tmp_path / 'agent-test.db'}")
    event.listen(
        engine,
        "connect",
        lambda connection, _: connection.execute("PRAGMA foreign_keys=ON"),
    )
    Base.metadata.create_all(engine)
    with Session(engine, expire_on_commit=False) as session:
        with session.begin():
            yield session
    engine.dispose()


def test_models_define_exact_tables_and_documented_indexes(db_session: Any) -> None:
    inspector = inspect(db_session.get_bind())

    assert set(inspector.get_table_names()) == TABLE_NAMES
    for table_name, expected_columns in EXPECTED_COLUMNS.items():
        assert {column["name"] for column in inspector.get_columns(table_name)} == expected_columns
    assert {index["name"] for index in inspector.get_indexes("source_records")} == {
        "source_records_run"
    }


def test_initial_migration_emits_postgresql_schema(capsys: pytest.CaptureFixture[str]) -> None:
    project_root = Path(__file__).resolve().parents[2]
    config = Config(str(project_root / "alembic.ini"))
    config.set_main_option(
        "sqlalchemy.url", "postgresql+psycopg://user:password@localhost:5433/agentdb"
    )
    command.upgrade(config, "head", sql=True)
    generated_sql = capsys.readouterr().out

    for table_name in TABLE_NAMES:
        assert f"CREATE TABLE {table_name}" in generated_sql
    assert "CREATE INDEX source_records_run" in generated_sql
    assert "record->>'doc_id'" in generated_sql
    assert "FOREIGN KEY(run_id) REFERENCES runs (run_id)" in generated_sql
    assert "FOREIGN KEY(claim_id) REFERENCES claims (claim_id)" in generated_sql


def test_run_and_claim_create_are_idempotent(db_session: Any) -> None:
    runs = RunsRepository(db_session)
    claims = ClaimsRepository(db_session)
    run = Run(
        run_id="run_test",
        matter_id="mtr_test",
        state="queued",
        config={},
        matter={},
    )

    saved_run, created = runs.add(run)
    repeated_run, repeated_created = runs.add(
        Run(
            run_id="run_test",
            matter_id="mtr_test",
            state="queued",
            config={},
            matter={},
        )
    )
    assert created is True
    assert repeated_created is False
    assert repeated_run is saved_run

    claim = Claim(claim_id="clm_test", run_id="run_test", claim={"text": "supported"})
    assert claims.add(claim)[1] is True
    assert claims.add(
        Claim(claim_id="clm_test", run_id="run_test", claim={"text": "supported"})
    )[1] is False


def test_idempotent_primary_key_conflict_is_explicit(db_session: Any) -> None:
    repository = RunsRepository(db_session)
    repository.add(
        Run(
            run_id="run_conflict",
            matter_id="mtr_test",
            state="queued",
            config={},
            matter={},
        )
    )

    with pytest.raises(RepositoryConflict):
        repository.add(
            Run(
                run_id="run_conflict",
                matter_id="mtr_other",
                state="queued",
                config={},
                matter={},
            )
        )


def test_other_table_repositories_persist_idempotently(db_session: Any) -> None:
    RunsRepository(db_session).add(
        Run(
            run_id="run_all",
            matter_id="mtr_all",
            state="queued",
            config={},
            matter={},
        )
    )
    ClaimsRepository(db_session).add(
        Claim(claim_id="clm_repo", run_id="run_all", claim={})
    )

    records: list[tuple[Any, Any, Any, Any]] = [
        (
            SourceRecordsRepository(db_session),
            SourceRecord(source_id="src_1", record={}),
            "add",
            "src_1",
        ),
        (
            GuardReportsRepository(db_session),
            GuardReport(claim_id="clm_repo", report={}),
            "put",
            "clm_repo",
        ),
        (
            SessionsRepository(db_session),
            BrowserSession(
                session_id="ses_1",
                site_id="site",
                principal="default",
                state="active",
            ),
            "add",
            "ses_1",
        ),
        (
            HandoffsRepository(db_session),
            Handoff(
                handoff_id="hof_1",
                run_id="run_all",
                request={},
                status="open",
            ),
            "add",
            "hof_1",
        ),
        (
            ActionLogRepository(db_session),
            ActionLog(
                id=1,
                run_id="run_all",
                action={},
                decision={},
                executed=False,
            ),
            "add_once",
            1,
        ),
        (
            PlaybookProposalsRepository(db_session),
            PlaybookProposal(
                id=1,
                site_id="site",
                task="search",
                base_version=1,
                diff={},
            ),
            "add_once",
            1,
        ),
        (
            CrosscheckCacheRepository(db_session),
            CrosscheckCache(citation="citation", result={}),
            "put",
            "citation",
        ),
        (
            IdempotencyRepository(db_session),
            IdempotencyRecord(key="key", request_hash="hash", response={}),
            "put",
            "key",
        ),
    ]

    for repository, model, method_name, identity in records:
        first, created = getattr(repository, method_name)(model)
        assert created is True
        assert repository.get(identity) is first


def test_idempotency_repository_rejects_reused_key_with_different_hash(db_session: Any) -> None:
    repository = IdempotencyRepository(db_session)
    repository.put(IdempotencyRecord(key="request-1", request_hash="a", response={"ok": True}))

    with pytest.raises(RepositoryConflict):
        repository.put(
            IdempotencyRecord(key="request-1", request_hash="b", response={"ok": True})
        )


def test_idempotency_key_can_be_reused_after_24_hours(db_session: Any) -> None:
    repository = IdempotencyRepository(db_session)
    now = datetime(2026, 11, 7, tzinfo=UTC)
    old_record = IdempotencyRecord(
        key="expired-request",
        request_hash="old-hash",
        response={"result": "old"},
        created_at=now - timedelta(hours=25),
    )
    repository.put(old_record, now=now)

    updated, created = repository.put(
        IdempotencyRecord(
            key="expired-request",
            request_hash="new-hash",
            response={"result": "new"},
        ),
        now=now,
    )
    retry, retry_created = repository.put(
        IdempotencyRecord(
            key="expired-request",
            request_hash="new-hash",
            response={"result": "ignored-on-retry"},
        ),
        now=now + timedelta(hours=1),
    )

    assert created is True
    assert updated.request_hash == "new-hash"
    assert updated.response == {"result": "new"}
    assert retry_created is False
    assert retry.response == {"result": "new"}


def test_guard_report_and_claim_foreign_key_is_enforced(db_session: Any) -> None:
    with pytest.raises(IntegrityError):
        GuardReportsRepository(db_session).put(
            GuardReport(claim_id="missing-claim", report={})
        )
