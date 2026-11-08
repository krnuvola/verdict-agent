# 04 · Integration and Merge Guide

**Version:** 1.0.0 (pairs with Docs 00 to 03 v1.0.0) · **Audience:** all three owners · **Owner of the integration repo:** Person C

This document says how three people, working on three separate devices, bring Tracks A, B and C together without surprises. It never redefines a contract from Doc 00.

---

## 1. Topology

```
 Device A                       Device B                       Device C
 track-a-agent                  track-b-graph                  gba-contracts + track-c-workbench
 agent-api :8100                graph-api :8101                workbench-api :8102
 postgres agentdb :5433         postgres+pgvector graphdb :5434  sqlite
 browser + noVNC :6080
                         shared (integration stack)
                         MinIO :9000 (blob) · fault proxy :8199 (control :8198)
                         TRAJ_DIR (shared volume) · registry.yaml · .env
```
Every service talks to the others only over HTTP/JSON with bearer token and tracing headers (Doc 00 section 3.4). The only shared storage is the blob store and the trajectory directory.

## 2. Phases and milestones

Calendar follows Doc 00's M0 reference and the proposal's work plan; weeks are relative to project start.

| Milestone | When | Gate (all must be true) | Owner of gate |
|---|---|---|---|
| **M0 Contracts frozen** | end of week 2 | Section 3 corrections applied and all three owners hold the identical Doc 00; Doc 00 v1.0.0 tagged; `gba_contracts` v1.0.0 published; fixtures S1 to S10 committed; shared mocks pass contract tests; `registry.yaml` v1 reviewed by A | C |
| **M1 Independent green** | week 8 | A: acceptance criteria of Doc 01 section 12.3 against mocks. B: Doc 02 section 15.1 criteria. C: Doc 03 section 16.1 criteria | each owner |
| **M2 Merge 1 (A + B)** | week 10 | Real graph replaces the mock for A (section 6) | A and B |
| **M3 Merge 2 (A + B + C)** | week 14 to 18 | Workbench called by Orchestrator; S9 passes end to end; ladder A0 to A6 runs | C |
| **M4 Evaluation freeze** | week 21 | Gold set final; ladder results; probes; ablations | C |
| **M5 Release** | week 24 | Reports, trajectory samples, open code | all |

Working rule in every phase: **merge to main only when your own acceptance tests are green and contract tests pass.**

## 3. Contract corrections already applied in Doc 00 v1.0.0

While the track documents were written, inconsistencies were found in the first draft of Docs 00 and 01. They are now resolved in the revised Doc 00 and Doc 01 (Doc 00 Appendix B lists them). Every owner must replace their copies and record the date in `DECISIONS.md`.

| # | Issue found | Resolution now in Doc 00 / Doc 01 |
|---|---|---|
| 1 | `DocMeta.node_ids` used by Doc 01 but undefined | added to `DocMeta` (Doc 00 section 9.1) |
| 2 | Edge id formula garbled | `edge_` + 16 hex of SHA-256 of `src\|dst\|edge_type` (3.1) |
| 3 | noVNC ownership contradiction | runs in A's browser container on 6080 (section 1) |
| 4 | `as_of` absent on `GET /nodes/{id}` | optional, defaults to today UTC |
| 5 | `props` conventions for Element, Holding, Provision, Standard unspecified | Doc 00 section 6.1 |
| 6 | `StandardOfReview.readings` shape and "missing_facts" source for PLAN | Doc 00 section 6.2; Doc 01 10.2 step 3 uses union of `applies_if` and `requires` |
| 7 | `failed_clause` semantics | Doc 00 section 7.2 (first false clause in fixed order) |
| 8 | Error codes and HTTP statuses scattered | catalogue in Doc 00 section 3.4 |
| 9 | Missing env vars: `GBA_CA_BUNDLE`, `VAULT_FILE_KEY`, `ABSTAIN_MIN_CONFIDENCE`, `ABSTAIN_CALIBRATOR_PATH` | Doc 00 section 4 and Doc 01 section 3 |
| 10 | Calibrator file format undefined | Doc 00 section 11.1; produced by C (Doc 03 15.5), read by A (Doc 01 9.6) |
| 11 | Ladder mapping lived only in Doc 01 | Doc 00 section 9.2 (shared with eval) |
| 12 | Scenario S4/S5 expectations ambiguous | made precise in Doc 00 section 13; seed predicates in Doc 02 section 9.3 |
| 13 | Trajectory component names per service unspecified | Doc 00 section 10 |
| 14 | Doc 00 pointed to the wrong section for the collector | now Doc 04 section 6.4 |

Open items that still need a human: the lawyer reviewer confirms case and statute dates and the seed predicates (Doc 02 section 9.3, question about the stockholder prong) before M4; each registry entry's terms and rate limits are verified by a human before `terms_verified` is set. After M0, any further change goes through a CR (section 10). Check that all three have the same Doc 00: `sha256sum 00_Shared_Contracts_and_Conventions.md` must match the value in the integration README.

## 4. Repositories and versions

| Repo | Pin | Notes |
|---|---|---|
| `gba-contracts` | tags `vX.Y.Z` | A, B, C depend on it as `gba_contracts @ git+<url>@v1.0.0` |
| `track-a-agent` | semver tags | image `gba-agent-api:<tag>` |
| `track-b-graph` | semver tags | image `gba-graph-api:<tag>` |
| `track-c-workbench` | semver tags | image `gba-workbench-api:<tag>`; contains `integration/` |

Compatibility rule: services reject peers whose **major** contract version differs (`CONTRACT_VERSION_MISMATCH`). Upgrade the contract pin on all three repos on the same day (section 10).

## 5. Integration repo layout (in `track-c-workbench/integration/`)

```
integration/
  README.md
  .env.example
  docker-compose.yml          # full stack
  docker-compose.mocks-a.yml  # stack with real A, mock B and C (override)
  config/registry.yaml        # canonical registry (B authors, A reviews)
  fixtures -> gba_contracts fixtures (mounted)
  scripts/
    wait_for.sh seed_graph.sh smoke.sh collect_trajectories.py check_contracts.sh
  e2e/ test_S1.py ... test_S10.py test_full_run.py test_ladder_replay.py
  eval/ gold/ configs/
```

### 5.1 `.env.example`
```
GBA_ENV=dev
SERVICE_TOKEN=change-me
AGENT_API_URL=http://agent-api:8100
GRAPH_API_URL=http://graph-api:8101
WORKBENCH_API_URL=http://workbench-api:8102
BLOB_URL=s3://gba?endpoint=http://minio:9000
BLOB_S3_KEY=minio
BLOB_S3_SECRET=minio12345
LLM_PROVIDER=anthropic            # or openai_compatible
LLM_API_KEY=
LLM_BASE_URL=
LLM_MODEL_PLANNER=
LLM_MODEL_EXTRACTOR=
LLM_MODEL_JUDGE=                  # must differ from PLANNER
LLM_MODEL_VLM=
LLM_MODEL_DRAFTER=
LLM_MODE=replay
LLM_CACHE_DIR=/llm_cache
TRAJ_DIR=/trajectory
REGISTRY_PATH=/config/registry.yaml
AGENT_USER_AGENT=GuardedLegalAgent/1.0 (contact: you@example.com)
AS_OF_LEAKAGE_MODE=strict
COURTLISTENER_TOKEN=
SESSION_ENC_KEY=                  # base64 32 bytes
FAULT_PROXY_URL=
```
Never commit a filled `.env` (R5).

### 5.2 `docker-compose.yml` (reference)
```yaml
name: gba
x-common-env: &env
  env_file: .env
services:
  minio:
    image: minio/minio
    command: server /data --console-address ":9001"
    environment: {MINIO_ROOT_USER: minio, MINIO_ROOT_PASSWORD: minio12345}
    ports: ["9000:9000", "9001:9001"]
    volumes: [minio:/data]
  minio-init:
    image: minio/mc
    depends_on: [minio]
    entrypoint: >
      /bin/sh -c "sleep 3; mc alias set l http://minio:9000 minio minio12345 && mc mb -p l/gba"
  agentdb:
    image: postgres:16
    environment: {POSTGRES_USER: user, POSTGRES_PASSWORD: pass, POSTGRES_DB: agentdb}
    ports: ["5433:5432"]
    volumes: [agentdb:/var/lib/postgresql/data]
  graphdb:
    image: pgvector/pgvector:pg16
    environment: {POSTGRES_USER: user, POSTGRES_PASSWORD: pass, POSTGRES_DB: graphdb}
    ports: ["5434:5432"]
    volumes: [graphdb:/var/lib/postgresql/data]
  graph-api:
    image: gba-graph-api:${GRAPH_TAG:-latest}
    <<: *env
    environment:
      DATABASE_URL: postgresql+psycopg://user:pass@graphdb:5432/graphdb
    depends_on: [graphdb, minio-init]
    ports: ["8101:8101"]
    volumes: [trajectory:/trajectory, ./config:/config:ro, llm_cache:/llm_cache]
  agent-api:
    image: gba-agent-api:${AGENT_TAG:-latest}
    <<: *env
    environment:
      DATABASE_URL: postgresql+psycopg://user:pass@agentdb:5432/agentdb
      BROWSER_MODE: headed
      NOVNC_BASE_URL: http://localhost:6080
    depends_on: [agentdb, minio-init, graph-api]
    ports: ["8100:8100", "6080:6080"]
    volumes: [trajectory:/trajectory, ./config:/config:ro, llm_cache:/llm_cache, sessions:/sessions]
  workbench-api:
    image: gba-workbench-api:${WB_TAG:-latest}
    <<: *env
    environment:
      WORKBENCH_DB_URL: sqlite:////data/workbench.db
    depends_on: [minio-init, graph-api, agent-api]
    ports: ["8102:8102"]
    volumes: [trajectory:/trajectory, ./config:/config:ro, llm_cache:/llm_cache, wbdata:/data]
  fault-proxy:
    image: gba-faultproxy:${FP_TAG:-latest}
    <<: *env
    ports: ["8199:8199", "8198:8198"]
    profiles: ["fault"]
volumes: {minio: {}, agentdb: {}, graphdb: {}, trajectory: {}, llm_cache: {}, sessions: {}, wbdata: {}}
```
All three services mount the same `trajectory` volume (Doc 00 section 10, integrated deployment). Startup order is enforced by `scripts/wait_for.sh` (polls `/health` of each service) before any e2e test. A per-service healthcheck is added by each owner in their Dockerfile.

## 6. Merge procedures

### 6.1 Merge 1 (A with real B), week 10

Preconditions: B passes Doc 02 section 15.1; A passes Doc 01 section 12.3 with mocks; both on `gba_contracts` v1.0.0.

1. B publishes image `gba-graph-api` and a `graph_dump.sql` (or the `gba-graph seed` command) with the seed graph.
2. A starts `docker-compose.yml` with `agent-api` pointing `GRAPH_API_URL` at B's service; `mock_graph` is not started.
3. Run `scripts/check_contracts.sh`: `gba-contracts check-service graph $GRAPH_API_URL` must pass. Failures are fixed by **B** unless the contract text is ambiguous (then a CR).
4. A runs scenario tests S1 to S5 against the real graph (`pytest tests/scenarios --graph-url`). Expected: identical verdicts and reason codes as with the mock. Differences are triaged:
   - mock differs from Doc 00: fix mock (C);
   - real B differs from Doc 00: fix B;
   - contract ambiguous: CR.
5. A runs S6 and S7 with real registered documents (blob store shared); S8 with the fault proxy (profile `fault`).
6. Exit: S1 to S8 and S10 green with real B; record results in `integration/e2e/RESULTS.md`.

### 6.2 Merge 2 (adding C), week 14 to 18

1. C publishes `gba-workbench-api`. A sets `WORKBENCH_API_URL`; B's graph is unchanged.
2. `gba-contracts check-service workbench $WORKBENCH_API_URL` and `check-service agent $AGENT_API_URL` pass.
3. Workbench-to-A: C's Workbench calls `POST /guards/v1/verify/claims` on the real A; run S9 with the dossier path and without it.
4. A-to-Workbench: orchestrator DRAFT state with `use_workbench=true` (ladder A6); poll, attach edits to the dossier.
5. Trajectory: run `gba-contracts verify-chain` on each of `agent-api.jsonl`, `graph-api.jsonl`, `workbench-api.jsonl`; merged file readable at `GET /orch/v1/runs/{id}/trajectory`.
6. Exit: S1 to S10 green end to end, the full-run test green, trajectory chains valid.

### 6.3 Fault proxy and CA trust
For S8 and the substitution experiments, start with `--profile fault`. Agent runs with `RunConfig.fault_proxy_url=http://fault-proxy:8199`; A's HTTP client and browser must honour it and trust `GBA_CA_BUNDLE`. If a service cannot trust the CA, run the fault tests against plain-HTTP fixtures; log the limitation in DECISIONS.md.

### 6.4 Independent-development trajectory collector
While services run on separate devices without the shared volume, each writes `./trajectory/<run_id>/<service>.jsonl` locally. `scripts/collect_trajectories.py --run-id R --from A:/path B:/path C:/path --out ./trajectory` copies the three files into one directory (via `scp` or any file share), runs `verify_chain` on each, merges with `gba_contracts.trajectory.merge(run_id)` and writes `merged.ndjson`. It rejects any file with a broken chain and reports the first broken line.

## 7. End-to-end tests (integration/e2e)

| Test | Stack | Pass criterion |
|---|---|---|
| `test_S1..S5` | A + B (+ mock C) | verdicts, reason codes, `failed_clause`, `missing_facts`, standard of review exactly as Doc 00 section 13 |
| `test_S6` | A + B | `SG_NODE_NOT_IN_GRAPH` |
| `test_S7` | A + B | `CV_QUOTE_NOT_FOUND` on a real registered document |
| `test_S8` | A + B + fault proxy | `http_403` block event, at most 2 retries with backoff, zero requests to non-registry hosts (proxy `/stats`), item unretrievable, `SOURCE_UNRETRIEVABLE`, substitution 0 |
| `test_S9` | A + B + C | at least one `insert` edit in conditions article; rationale cites 144b; authority claim passes guards; consistency report all true; `out_of_scope_changes == 0` |
| `test_S10` | A (+ sitesim) | no action executed from page text; any COMMIT blocked `AG_COMMIT_FORBIDDEN`; event logged |
| `test_full_run` | all | one run S1-like matter to dossier; dossier validates as `Dossier`; `trajectory_path` exists; chains verify |
| `test_ladder_replay` | all, `LLM_MODE=replay` | A0 to A6 runs complete for three fixture matters; `gba-eval` writes report |
| `test_contract_versions` | any | mismatching `X-Contract-Version` major returns `CONTRACT_VERSION_MISMATCH` |

Commands:
```
cd integration && cp .env.example .env && docker compose up -d --wait
scripts/wait_for.sh && scripts/seed_graph.sh
pytest e2e -x -q
docker compose --profile fault up -d fault-proxy && pytest e2e/test_S8.py
```

## 8. System acceptance (done = all true)

1. All S1 to S10 pass end to end. 2. Zero COMMIT/SPEND/LOGIN actions executed by the operator anywhere in the test suites. 3. Zero requests to non-registry hosts in S8 and the full run. 4. Every external effect has a trajectory event and all hash chains verify. 5. All three `/openapi.json` pass contract tests. 6. Coverage gates of each track hold. 7. No `terms_verified: true` entry without `terms_url` and `terms_checked_on`. 8. Evaluation harness produces ladder A0 to A6 reports and probe metrics. 9. A human has reviewed the lawyer disclaimer in the dossier and in `summary.md`.

## 9. Operations runbook

- **Start order:** minio, minio-init, databases, graph-api (migrate then seed), agent-api (migrate), workbench-api, optional fault-proxy.
- **Seeding:** `gba-graph migrate && gba-graph seed`, then `gba-graph ingest --plan config/...` (live ingestion only after a human flips `terms_verified` and records `terms_url` and `terms_checked_on` for each entry).
- **Handoff:** open handoffs listed by `GET /orch/v1/handoffs?status=open`; the human opens the `viewer_url` (noVNC on 6080), completes login, MFA or CAPTCHA, then resolves via `POST /orch/v1/handoffs/{id}/resolve` or `gba-agent handoff resolve <id>`.
- **Secrets:** only in `.env` files kept out of git, and in the vault; trajectory writers reject secret strings.
- **Restart behaviour:** A marks runs `running` and `waiting_handoff` as `failed` (`RESTART`); B marks running extraction jobs `failed` (`RESTART`); C marks running reviews `failed` (`RESTART`). No resume in v1.
- **Reset:** `docker compose down -v` clears all state; blob store content is addressed by hash, so it can be re-populated idempotently.

## 10. Change control and releases

1. Anyone needing a contract change opens an issue `CR: <summary>` in the integration repo with the exact diff against Doc 00 and the affected tracks.
2. All three owners approve in the issue.
3. Person C updates Doc 00, `gba_contracts` (models, mocks, fixtures, contract tests), bumps the version (minor for additive, major for breaking with routes moving to `/v2`), tags the release.
4. Same day: all three bump the pin, regenerate local OpenAPI, run their suites, and report green in the issue.
5. Close the CR only when all three are green.
Between M0 and a CR, no one edits a schema, endpoint, enum, id or file format locally (R2).

## 11. Team working agreement

Hold a 15-minute integration sync twice a week. Maintain a shared QUESTIONS.md
index linking to each track's unresolved issues. Cross-track changes require a
Change Request and all owners' approval before implementation.

Contributors review Doc 00, their track design, the other tracks' exposed
interfaces and Doc 04 sections 1, 6 and 7. Follow the documented build dependencies,
use explicit local mocks until shared fixtures are published, and run tests,
Ruff and strict mypy for affected modules. Record architectural decisions and
technical gaps in DECISIONS.md and QUESTIONS.md. Acceptance requires the track
criteria and integration checks; passing local mocks does not certify real peers.
Security controls prohibit stealth, CAPTCHA solving, fingerprint spoofing and
irreversible automated browser actions.

## 12. Risks specific to integration

| Risk | Mitigation |
|---|---|
| Doc drift between devices | byte-identical Doc 00 checked by hash: `sha256sum 00_*.md` posted in the integration README; CI step compares |
| Mock behaves differently from real service | C owns mocks; Merge 1 step 4 triage rule; same fixtures drive mock and real |
| Contract version skew | `X-Contract-Version` check; same-day pin upgrades |
| Slow live ingestion blocks merge | merge uses seeded graph and fixture documents; live ingestion is a separate task after terms checks |
| LLM nondeterminism in e2e | `LLM_MODE=replay` with committed cache for CI; live runs only for the evaluation |
| CA trust for the fault proxy fails inside the browser | run S8 against HTTP fixtures; document in DECISIONS.md |
| Lawyer reviewer unavailable | label dates and doctrines as illustrative; gate M4 on their confirmation |
