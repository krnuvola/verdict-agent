# Track A integration handoff

**Contract baseline:** frozen Doc 00 v1.0.0. Track A follows Doc 01 and the shared
integration process in Doc 04. Host API/PostgreSQL operation and offline peer
fixtures are verified; actual B/C and shared-runtime interoperability are pending.
Legal fixture content is illustrative and requires lawyer review.

## Dependency and endpoint inventory

All paths below are relative to the specified prefix; `/health` and `/openapi.json`
are at the origin root for **every** service. JSON DTO names are verbatim Doc 00
sections 9.1–9.3. Success is 200 unless noted. No endpoints were added to Track A.

| Owner / origin | Prefix | Dependency |
|---|---|---|
| A `http://localhost:8100` | `/guards/v1`, `/orch/v1` | agentdb, shared contracts/runtime |
| B `http://localhost:8101` | `/graph/v1` | B-owned graphdb/pgvector, registry corpus and graph algorithms |
| C `http://localhost:8102` | `/wb/v1` | C-owned workbenchdb and document processing |

| B method/path under `/graph/v1` | Request | Response |
|---|---|---|
| GET `/nodes/{node_id}?as_of=` | optional date | `Node` |
| GET `/nodes/{node_id}/edges?as_of=&edge_type=&direction=in\|out\|both` | optional filters; direction defaults both | `{items:[Edge]}` |
| POST `/query/scope_frame` | `ScopeFrameReq` | `ScopeFrameResp` |
| POST `/query/validity` | `ValidityReq` | `ValidityResult` |
| POST `/query/rule_candidates` | `RuleCandidatesReq` | `RuleCandidatesResp` |
| POST `/query/standard_of_review` | `StandardReq` | `StandardResp` |
| POST `/search/documents` | `DocSearchReq` | `DocSearchResp` |
| GET `/documents/{doc_id}/meta` | URL-encoded ID | `DocMeta` |
| GET `/documents/{doc_id}/text` | URL-encoded ID | canonical `text/plain` |
| POST `/documents/register` | `RegisterDocReq` | `DocMeta`, 201 |
| POST `/extract/facts` | `ExtractReq` | `{job_id}`, 202 |
| GET `/jobs/{job_id}` | ID | `JobStatus` |
| POST `/gnn/score_edges` | `ScoreEdgesReq` | `ScoreEdgesResp` |
| POST `/gnn/predict_missing` | `PredictMissingReq` | `PredictMissingResp` |
| GET `/registry` | none | registry JSON (Doc 00 section 8) |

| C method/path under `/wb/v1` | Request | Response |
|---|---|---|
| POST `/documents` | `IngestDocReq` | `DocRecord`, 201 |
| GET `/documents/{doc_id}` | ID | `DocRecord` |
| PUT `/playbooks/{playbook_id}` | YAML | `{playbook_id,version}` |
| GET `/playbooks/{playbook_id}` | ID | YAML |
| POST `/reviews` | `CreateReviewReq` | `{review_id}`, 202 |
| GET `/reviews/{review_id}` | ID | `ReviewStatus` |
| POST `/reviews/{review_id}/edits/{edit_id}/decision` | `EditDecisionReq` | `ProposedEdit` |
| GET `/reviews/{review_id}/redline.docx` | ID | DOCX; 409 `REVIEW_NOT_READY` |
| GET `/reviews/{review_id}/accepted.docx` | ID | DOCX; 409 `REVIEW_NOT_READY` |
| GET `/reviews/{review_id}/summary.md` | ID | Markdown |

| A method/path | Request | Response |
|---|---|---|
| POST `/guards/v1/verify/claims` | `VerifyClaimsReq` | `VerifyClaimsResp` |
| POST `/guards/v1/action/classify` | `ClassifyActionReq` | `ActionDecision` |
| POST `/orch/v1/runs` | `CreateRunReq` | `CreateRunResp`, 202 |
| GET `/orch/v1/runs/{run_id}` | ID | `RunStatus` |
| POST `/orch/v1/runs/{run_id}/cancel` | ID | `RunStatus` |
| GET `/orch/v1/runs/{run_id}/dossier` | ID | `Dossier` |
| GET `/orch/v1/runs/{run_id}/trajectory` | ID | merged NDJSON |
| GET `/orch/v1/handoffs?status=open` | optional status | `{items:[HandoffRequest]}` |
| POST `/orch/v1/handoffs/{handoff_id}/resolve` | `ResolveHandoffReq` | `HandoffRequest` |

Check exact names against frozen section 9.2 when generating consumers; OpenAPI
contract tests exercise the local A routes. `/health` needs no authentication and
returns `{status:"ok",service,version,contract_version:"1.0.0"}`. `/openapi.json`
uses normal service authentication.

## HTTP boundary behavior

Clients propagate bearer `SERVICE_TOKEN`, `X-Request-Id`, `X-Run-Id`, optional
`X-Matter-Id`, and `X-Contract-Version: 1.0.0`. Runtime dependency injection
requires peer response correlation/version headers. A different major version
raises `CONTRACT_VERSION_MISMATCH`; missing/malformed headers or error envelopes
raise visible `UPSTREAM_UNAVAILABLE`. Direct low-level test constructors retain
`strict_headers=False` for legacy fixture compatibility; supplied versions are
always checked. Deployments use the strict factory.

Timeout: 30 seconds per attempt, 120 for GNN calls. At most two retries (three
attempts) for safe methods, explicitly read-only query POSTs, or POSTs with an
idempotency key. Transport errors and 429/500/502/503/504 are retryable only on
these paths. Backoff is bounded; resource POSTs without keys are never retried.
Peers own the frozen 24-hour idempotency storage requirement. Track A does not
claim a peer persisted a key merely because it sends one.

Outgoing JSON requests and named JSON responses validate through
`gba_contracts.models` when installed, otherwise the explicitly named local
`clients/peer_contracts_compat.py` DTO adapter copied from frozen schemas.
Unknown fields and missing required fields fail. Unnamed receipts and edge lists
are checked. Registry JSON and text/binary responses still need shared-schema
and semantic/media validation against actual peers; no production legal meaning
is inferred from these responses. Workbench failure preserves the dossier as a
labelled partial outcome. Graph errors remain unknown/dependency failures.

## Configuration

Create `.env` from `.env.example`, replace placeholders and keep it untracked.
All service origins are configurable; no developer-machine path is required.

| Variable / setting | Purpose |
|---|---|
| `SERVICE_TOKEN` | Shared environment bearer token; keep private |
| `DATABASE_URL`, `AGENTDB_USER`, `AGENTDB_PASSWORD` | A DSN and matching development PostgreSQL credentials |
| `AGENT_API_URL` | A origin, default http://localhost:8100 |
| `GRAPH_API_URL`, `WORKBENCH_API_URL` | Real B/C origins, default ports 8101/8102 |
| `PEER_MODE` | live (default), explicit mock or read-only replay |
| `GRAPH_MOCK_API_URL`, `WORKBENCH_MOCK_API_URL` | Explicit development origins, default ports 18101/18102 |
| `PEER_REPLAY_DIR` | Local HTTP cassette root; default .peer_replay |
| `LLM_MODE`, `LLM_CACHE_DIR` | Independent model runtime/cassette selection; replay misses fail |
| `LLM_PROVIDER`, `LLM_BASE_URL`, `LLM_API_KEY`, `LLM_MODEL_*` | Shared provider configuration; planner and judge must differ when used |
| `BLOB_URL` | file://./.blobs or shared S3/MinIO URL |
| `BLOB_S3_KEY`, `BLOB_S3_SECRET` | S3 credentials for C's shared implementation |
| `REGISTRY_PATH`, `TRAJ_DIR` | Reviewed registry and shared trajectory mount |
| `SESSION_DIR`, `SESSION_ENC_KEY` | Encrypted browser checkpoints; key required before sessions are used |
| `AGENT_USER_AGENT` | Honest agent identity with contact; quote spaces in dotenv |
| `GBA_CA_BUNDLE` | Optional mounted CA bundle |
| `RunConfig.fault_proxy_url` | Explicit per-run browser/peer HTTP proxy; replay stays offline |

Live mode never substitutes mocks. Mock mode is forbidden in production. LLM
selection does not select peer mocks. Runtime clients ignore ambient proxy
variables and use explicit proxy/CA settings. All-traffic acceptance, including
C's LLM implementation, still needs the real proxy and shared runtime.

## Local startup

From the repository root with Python 3.12, uv and Docker available:

```powershell
uv sync --frozen
uv run playwright install chromium
if (-not (Test-Path .env)) { Copy-Item .env.example .env }
# Replace placeholders and match DATABASE_URL to AGENTDB_* before continuing.
docker compose --env-file .env -f docker/compose.dev.yml up -d --wait agentdb
uv run --env-file .env gba-agent migrate
uv run --env-file .env alembic current
uv run --env-file .env gba-agent serve --host 127.0.0.1 --port 8100
```

The current migration head is 0002. Keep the API terminal open. Startup verifies
migrated PostgreSQL. Health does not require B/C; research/review operations do.
In separate terminals, explicit local peers can be started with:

```powershell
uv run --env-file .env uvicorn tests.mocks.mock_graph:app --host 127.0.0.1 --port 18101
uv run --env-file .env uvicorn tests.mocks.mock_workbench:app --host 127.0.0.1 --port 18102
```

Set PEER_MODE=mock before launching A. Both mock processes need the same token.
They are not included in A's production app/image. The Workbench mock covers
review handoff behavior, not the complete production API inventory above.
Full-run tests inject canonical/blob/LLM fixtures explicitly; mock URLs alone do
not provide a complete shared runtime. Deployments supply compatible instances
through application composition (`orch_llm`/`guard_llm`, `guard_blobs` and shared
trajectory integration).

## Deployment and startup order

1. Match Doc 00 hashes and package versions; configure tokens, origins and mounts.
2. Start A/B/C databases and shared blob storage; apply each owner's migrations.
3. Start B/C and verify root health plus authenticated OpenAPI and version headers.
4. Start A, check health/OpenAPI, then run shared contracts and seeded scenarios.

Compose provides A, agentdb and MinIO definitions, not B/C production services.
B/C must join a reachable network or have their URLs supplied. Container localhost
is not the host: use service DNS on the shared network or an appropriate Docker
Desktop host origin. Share reviewed registry, trajectory and blob configuration
per Doc 04. Host A/PostgreSQL is verified; the A image, S3 and combined deployment
are not certified. Keep unauthenticated development noVNC on 6080 private.

## Replay and optional review

The replaceable local HTTP adapter reads
`PEER_REPLAY_DIR/{graph,workbench}/{request_sha256}.json`. Each file contains
exactly `request_sha256`, integer `status_code`, `headers`, and base64
`body_base64`. The key hashes canonical `{method,target,body_sha256}` using the
exact request path/query and body bytes. Credentials are not key material;
stored auth/cookies are discarded and current trace IDs are rebound. Missing
or malformed cassettes fail; no automatic live recorder or fallback is provided.
This local envelope must be reconciled with C's published cassette format.

Workbench requires enabled review plus draft/playbook inputs and is not run in
dry-run mode. Creation uses a run-stable idempotency key; polling is at five-second
intervals with a twenty-minute limit. Preserve the research dossier, IDs,
provenance and proposed edit status. Failed/unavailable reviews remain explicitly
partial (WORKBENCH_FAILED); A neither fabricates edits nor creates redlines.

## Shared-package migration

C must provide the actual immutable package location/version or commit, artifact
hash, Python support, exports and contract tests. No invented package URL/tag is
present in pyproject.toml. Add the exact dependency and regenerate uv.lock only
when the artifact is available. Verify shared DTOs, canonicalization/IDs,
trajectory vectors, LLM/blob constructors and real peer contracts before removing
compatibility adapters. A partially installed incompatible package must fail;
it must not mix silently with local models.

## Verification and ownership

```powershell
uv run pytest -q tests/mocks/test_mock_contracts.py tests/acceptance/test_openapi.py tests/integration/test_peer_readiness.py
uv run pytest -q tests/acceptance/test_full_runs.py
uv run ruff check .
uv run mypy src/gba_agent
```

B supplies reviewed registry/corpus/authority fixtures, all Graph responses,
GNN model versions, extraction jobs, blob access and persisted idempotency tests.
C supplies the shared package/runtime, Workbench fixtures/consistency reports,
stable edit IDs, artifact media, writer/canonicalizer tests and fault proxy/CA.
Joint checks must cover headers, exact errors, malformed schemas, timeout/retry,
restart/idempotency, immutable evidence, provenance and per-service hash chains.

S2's frozen Scope-pass expectation conflicts with unknown required facts; A0's
raw baseline conflicts with mandatory safety controls. Resolve both through the
shared contract process. Real registry/text/DOCX semantics, S3, scored evaluation
and lawyer review are pending. Local health or mock tests alone do not establish
integration acceptance. See IMPLEMENTATION_STATUS.md and QUESTIONS.md.
