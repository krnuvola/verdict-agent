# Track A implementation status

**Baseline:** Doc 00 v1.0.0 and Doc 01. **Verification date:** 2026-11-08.
Track A is suitable for a scoped demonstration and team review. Full acceptance
and cross-service integration are not complete.

## Implemented

| Area | Behavior |
|---|---|
| API/CLI | Authenticated frozen Guard/Orchestrator routes; serve/migrate; run/status/dossier/trace/cancel; handoffs and human playbook approval |
| Access | Registry prefixes, terms/robots/rate limits, bounded retries, adapters and guarded browser routing |
| Operator | Chromium observation/actions, deterministic Action Guard, immutable playbooks and approved locator repair |
| Evidence | Source admission, hashes/canonical spans, claim verification, scope/temporal guards and advisory GNN client |
| Orchestration | Persistent state, budgets, cancellation, handoffs, dossier assembly and optional Workbench handoff |
| Recording | Shared-first trajectory writer, explicit local adapter, hash verification and observed metrics |
| Modes | Explicit live/mock/replay peers; independent LLM replay; no live-to-mock fallback |

## Verification evidence

| Check | Latest recorded result |
|---|---|
| Full pytest | Prior complete run: 709 passed, 1 failed, 5 skipped; 166.29s |
| S2 scenario file | 2 passed, 1 failed; 0.37s |
| Mock full workflows | 4 passed; 7.36s |
| Action Guard tests | 125 passed; 3.42s |
| Ruff | Passed |
| Strict mypy | Passed; 79 source files |
| Coverage | Prior measured 91.49% lines, 88.94% combined; Action Guard 110/110 and Source Gate 154/154 branches |
| Packaging | Wheel/sdist built; isolated locked installation and 77 package-module imports verified |
| Database/API | PostgreSQL healthy; migrations 0002 (head); real health and authenticated OpenAPI/docs returned 200 |

Coverage values describe a previous complete measurement, not a new coverage run.
They do not assert that every individual module meets its target. Five skips are
explicit live-source probes. The suite reports a Starlette/httpx deprecation
warning; the verification host also reports an invalid SSL_CERT_DIR configuration.

The targeted results were produced with `uv run pytest -q` and the paths
`tests/acceptance/test_full_runs.py`, `tests/unit/test_action_guard.py` and
`tests/scenarios/test_S2.py`, respectively. Ruff and mypy were rerun on the same
checkout. The full suite was not rerun with these documentation-only changes.

## Mock-tested boundaries

Simulator runs use synthetic legal-source pages, explicit B/C ASGI transports,
SQLite/in-memory blobs and deterministic planner/judge replay. Tests cover
forbidden actions, unapproved destinations, citation failures, contradictory
facts, access denials, interruption, handoffs, budgets, retries, peer timeouts,
malformed schemas, replay misses and trajectory integrity. The C mock's proposed
edit/ready status is a fixture, not evidence of real document editing.

## Known failing acceptance case

`tests/scenarios/test_S2.py::test_s2_holding_scope_passes_with_doc00_facts` expects
`pass/SCOPE_OK`; actual output is `unknown/SCOPE_FACTS_MISSING` with missing facts
`controller_present` and `transaction_type`. S2 supplies only signing/litigation
dates. Doc 00 section 7.2 requires unknown scope for unknown transaction type,
while section 13 restricts the holding by transaction type yet expects pass.
No facts, frame constraints or expected outcomes have been changed. Shared owners
must approve the scenario/specification resolution. S2's statutory claim still
correctly blocks with `SCOPE_NON_APPLICATION` and its Temporal check passes.

## Integration pending

- B: actual Graph service, reviewed registry/legal fixtures, corpus and real endpoint semantics.
- C: actual pinned contracts package, shared LLM/blob/writer runtime, Workbench and contract tests.
- Joint: stable IDs/provenance, shared storage, immutable canonical blobs, idempotency/restart checks,
  fault-proxy/CA acceptance, artifact media validation and lawyer approval.
- A0: mandatory security controls remain enforced; the off-registry baseline conflict needs review.
- Deployment: host API/PostgreSQL is verified; A's Docker image, MinIO, manual noVNC operation
  and complete standalone HTTP research/review execution are not certified.

Active runs fail RESTART after process restart; queued runs can start normally.
Mock peers alone do not provide a complete standalone shared-runtime composition.
See README, INTEGRATION_HANDOFF and DEMO_TRACK_A for setup, contracts and reproduction.
