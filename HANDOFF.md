# Track A handoff

Track A is ready for collaborative review with known limitations. Full integration
acceptance remains blocked by S2 and unavailable shared/peer dependencies.

## Responsibilities

Track A provides the CLI/API, controlled acquisition and Chromium operator,
Action Guard, Source Gate, claim/scope/temporal checks, orchestration, dossiers,
human handoffs and trajectory recording. Track B owns corpus/Graph/retrieval and
legal reasoning; Track C owns shared runtime, Workbench and evaluation.

## Setup and execution

Use Python 3.12, uv and Chromium. From the repository root:

```powershell
uv sync --frozen
uv run playwright install chromium
if (-not (Test-Path .env)) { Copy-Item .env.example .env }
```

Replace local placeholders and match the PostgreSQL DSN to Compose credentials.

```powershell
docker compose --env-file .env -f docker/compose.dev.yml up -d --wait agentdb
uv run --env-file .env gba-agent migrate
uv run --env-file .env gba-agent serve --host 127.0.0.1 --port 8100
```

GET `/health` is public. `/openapi.json`, `/docs` and application routes require
bearer and contract headers. Keep the API terminal open; use one worker. A healthy
API does not establish B/C availability. The noVNC port must remain private.

## Service boundaries

| Owner | Port | Interface |
|---|---|---|
| A | 8100 | `/guards/v1`, `/orch/v1`, root health/OpenAPI |
| B | 8101 | `/graph/v1`: authority/document queries, extraction, standards, GNN and registry |
| C | 8102 | `/wb/v1`: documents, playbooks, reviews, decisions and artifacts |
| Local B/C mocks | 18101 / 18102 | Explicit development fixtures |
| PostgreSQL / MinIO | 5433 / 9000, 9001 | Host development ports |

[INTEGRATION_HANDOFF.md](INTEGRATION_HANDOFF.md) lists all paths, DTOs, configuration,
timeouts and error behavior. Set `PEER_MODE=mock` for local mock servers; live mode
never falls back to them. LLM replay is independent and fails on missing cassettes.
Full offline workflow tests use SQLite, simulated sites and explicit LLM/B/C
fixtures. Serving mock ports alone does not install shared runtime dependencies.

## Verification

```powershell
uv run pytest -q --tb=short
uv run pytest -q tests/acceptance/test_full_runs.py
uv run pytest -q tests/unit/test_action_guard.py
uv run pytest -q tests/scenarios/test_S2.py
uv run ruff check .
uv run mypy src/gba_agent
```

The full suite has one known S2 failure and five opt-in live skips; see
[IMPLEMENTATION_STATUS.md](IMPLEMENTATION_STATUS.md). The Action Guard remains
mandatory, prohibited browser effects are denied, and missing legal evidence is
not upgraded to a verified conclusion. [DEMO_TRACK_A.md](DEMO_TRACK_A.md) covers
observable dossiers, guard reports and trace verification.

## Integration checklist

1. Compare frozen Doc 00 hashes and pin C's actual package artifact when supplied.
2. Configure matching service tokens, origins, registry and shared blob/trajectory paths.
3. Start databases, apply migrations, start B/C, check root health and authenticated OpenAPI.
4. Verify schemas, trace/version headers, exact errors, timeouts and idempotent retries.
5. Run seeded research and optional review scenarios; verify provenance and hash chains.
6. Resolve S2 and A0 through shared contract review; obtain lawyer review of legal fixtures.

B must provide reviewed registry/corpus fixtures, endpoint behavior and persisted
idempotency evidence. C must provide the package artifact/hash, runtime constructors,
Workbench fixtures, canonicalization/trajectory compatibility and proxy/CA setup.
Real peer integration, S3 and production deployment remain unverified.
