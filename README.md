# Guarded Browsing Agent ? Track A

Track A supports evidence-grounded legal research through controlled browsing,
source admission and deterministic guard checks. It produces traceable research
dossiers and can request document review from the Workbench. Results require
review by a licensed lawyer and are not legal advice.

Shared interfaces are governed by frozen [Doc 00 v1.0.0](00_Shared_Contracts_and_Conventions.md).
[Doc 01](01_Track_A_LLD_Agent_Access_Operator_Guards_Orchestrator.md) defines Track A;
[Doc 04](04_Integration_and_Merge_Guide.md) defines cross-track integration.

## Architecture

```text
CLI / FastAPI :8100
  ?? Orchestrator: intake ? facts ? plan ? acquire ? verify ? dossier
       ?? Access Layer / Fetcher ? registry-approved sources
       ?? Operator ? Action Guard ? controlled Chromium
       ?? Source Gate, Claim Verifier, Scope and Temporal guards
       ?? Graph client ? Track B :8101
       ?? Optional review client ? Track C :8102
```

| Component | Responsibility |
|---|---|
| Track A | Acquisition policy, browser actions, evidence admission, guards, orchestration, dossiers, recording and CLI/API |
| Track B | Registry corpus, graph/retrieval, temporal authority data, standard-of-review responses, extraction and advisory GNN |
| Track C | Shared contracts/runtime, Workbench editing, evaluation and fault proxy |

The Action Guard remains enabled at every ladder level. The Operator does not
execute COMMIT, SPEND or LOGIN actions. Fetcher enforces registry prefixes,
terms verification, robots policy and rate limits. Page content is untrusted.
Missing or conflicting evidence produces unknown/abstention rather than an
unsupported legal conclusion. GNN signals are advisory.

## Capabilities

- Source acquisition through official adapters, approved playbooks and guarded Chromium.
- Content-addressed evidence, citation checks, provenance and three-valued applicability.
- Persistent runs, budgets, human handoffs, cancellation and evidence-grounded dossiers.
- Optional Workbench create/poll/results handling with visible partial outcomes on failure.
- Hash-chained trajectories, observed usage metrics, explicit replay and CLI controls.
- Offline site simulator, service mocks and security/contract tests.

## Repository structure

| Path | Contents |
|---|---|
| `src/gba_agent/access/`, `adapters/`, `operator/` | Controlled acquisition and browser execution |
| `src/gba_agent/guards/` | Action, source, claim, scope, temporal and advisory checks |
| `src/gba_agent/orchestrator/`, `api/`, `clients/` | Run lifecycle, routes and peer integrations |
| `src/gba_agent/db/` | Persistence models, repositories and Alembic migrations |
| `config/`, `playbooks/` | Registry/site policy and versioned browser playbooks |
| `tests/` | Unit, integration, scenario, simulator and mock-service tests |
| `docker/`, `scripts/` | Local infrastructure and acceptance checks |
| `docs/` | CLI, recording/evaluation interface and acceptance guides |

## Requirements and installation

Use Python **3.12**, uv and Chromium for browser tests. Docker Desktop or a
compatible engine is required for the supplied PostgreSQL development service.
B/C services and PostgreSQL are not required for the offline test suite.

From the repository root, in PowerShell:

```powershell
uv sync --frozen
uv run playwright install chromium
if (-not (Test-Path .env)) { Copy-Item .env.example .env }
```

Edit `.env` locally. Replace credential/model placeholders, keep `DATABASE_URL`
consistent with `AGENTDB_USER` and `AGENTDB_PASSWORD`, and provide a real contact
in `AGENT_USER_AGENT`. The planner and judge model names must differ when the LLM
judge is selected. `.env`, sessions, blobs and replay caches are ignored by Git.
Never commit credentials or production session data.

## PostgreSQL and API startup

Start Docker Desktop, then run:

```powershell
docker version --format '{{.Server.Version}}'
docker compose --env-file .env -f docker/compose.dev.yml up -d --wait agentdb
uv run --env-file .env gba-agent migrate
uv run --env-file .env alembic current
$env:PGCONNECT_TIMEOUT='3'
uv run --env-file .env gba-agent serve --host 127.0.0.1 --port 8100
```

Keep the API terminal open. Migrations must report `0002 (head)`; startup checks
that PostgreSQL is reachable and migrated. `--env-file .env` is explicit because
Alembic reads its process environment. The v1 API runs with one worker.

In another terminal:

```powershell
Invoke-RestMethod http://127.0.0.1:8100/health -TimeoutSec 3
uv run --env-file .env python -c "import os,httpx; r=httpx.get('http://127.0.0.1:8100/openapi.json', headers={'Authorization':'Bearer '+os.environ['SERVICE_TOKEN'],'X-Contract-Version':'1.0.0'}, trust_env=False, timeout=5); print(r.status_code); r.raise_for_status(); print(r.json()['info']['title'])"
```

Health is public. Other routes, including `/docs` and `/openapi.json`, require
bearer authentication and contract headers. Swagger requires browser header
configuration for both requests; authenticated OpenAPI JSON is also available.
The development noVNC endpoint on 6080 is unauthenticated and must remain private.

## Graph and Workbench integration

Live origins default to `GRAPH_API_URL=http://localhost:8101` and
`WORKBENCH_API_URL=http://localhost:8102`. Development mocks use separate ports.
Run each server in its own terminal, using the same local `.env`:

```powershell
uv run --env-file .env uvicorn tests.mocks.mock_graph:app --host 127.0.0.1 --port 18101
uv run --env-file .env uvicorn tests.mocks.mock_workbench:app --host 127.0.0.1 --port 18102
```

| Setting | Behavior |
|---|---|
| `PEER_MODE=live` (default) | Configured real peer origins; missing peers return visible errors/partial outcomes |
| `PEER_MODE=mock` | Explicit mock origins 18101/18102; forbidden in production |
| `PEER_MODE=replay` | Read-only HTTP cassettes in `PEER_REPLAY_DIR`; no network fallback |
| `LLM_MODE=replay` | Independent LLM cassette selection; missing responses fail |
| `LLM_MODE=live` / `record` | Requires an explicitly supplied provider/shared runtime; does not select peer mocks |

Set `PEER_MODE=mock` before starting A to use the mock servers. Mock URLs alone
are insufficient for a complete standalone research run: deployment must supply
the shared `LLMClient`, canonicalization and blob runtime. Offline full-workflow
tests inject explicit fixtures. There is no silent live-to-mock substitution.
Workbench is used only when enabled and supplied with draft/playbook inputs.

## Tests and demonstration

```powershell
uv run pytest -q --tb=short
uv run pytest -q tests/acceptance/test_full_runs.py
uv run pytest -q tests/unit/test_action_guard.py
uv run pytest -q tests/scenarios/test_S2.py
uv run ruff check .
uv run mypy src/gba_agent
```

[DEMO_TRACK_A.md](DEMO_TRACK_A.md) describes the reproducible mock workflow,
actual dossier/guard/trajectory inspection and a two-minute walkthrough.
[docs/CLI_USAGE.md](docs/CLI_USAGE.md) covers run/status/dossier/trace, handoffs
and playbook approval. [IMPLEMENTATION_STATUS.md](IMPLEMENTATION_STATUS.md)
records verification results and remaining work.

## Limitations and known issues

The latest full-suite result is **709 passed, 1 failed, 5 skipped**. The known S2
failure expects Scope pass with unknown facts despite Doc 00's transaction-type
restriction and three-valued rules. Actual scope is `unknown/SCOPE_FACTS_MISSING`.
The assertion remains active; owner approval is required to resolve the conflict.
A0 retains mandatory security controls and is not an unguarded transport baseline.

Real B/C interoperability, the published pinned `gba_contracts` runtime, S3,
shared trajectories and all-traffic fault-proxy integration remain pending.
Fixture legal content is illustrative. Active runs fail `RESTART` after restart;
queued runs can start, and saved dossiers remain retrievable. API health is not
proof of peer readiness. Host API/PostgreSQL startup is verified; the A Docker
image and MinIO integration are not certified.

## Team integration

Start with [HANDOFF.md](HANDOFF.md) and the exact endpoint/schema inventory in
[INTEGRATION_HANDOFF.md](INTEGRATION_HANDOFF.md). Track B supplies graph/corpus
services and reviewed fixtures; Track C supplies shared runtime, Workbench and
contract tests. Agree on origins, authentication, registry and shared mounts;
run frozen contract checks before cross-service scenarios. Use the shared Change
Request process for schema or scenario changes. Architectural choices and open
questions are in [DECISIONS.md](DECISIONS.md) and [QUESTIONS.md](QUESTIONS.md).
