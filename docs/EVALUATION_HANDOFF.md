# Track A recording handoff to Person C

Doc 00 v1.0.0 remains frozen. C owns scoring, aggregation across services,
the published shared writer, dashboard and fault proxy. A supplies the existing
RunStatus/Dossier JSON and `/orch/v1/runs/{id}/trajectory` NDJSON. No new response
fields or endpoints were introduced. CLI examples are in CLI_USAGE.md.

## Writer and integrity

The API prefers `gba_contracts.trajectory.TrajectoryWriter` and caches one writer
per service/run. When that module is absent, the explicitly identified
`gba_agent.trajectory.LocalTrajectoryWriter` writes `agent-api.jsonl` with the
Doc 00 event field layout, UTC millisecond timestamps, actual ULID event IDs,
monotonic sequences (local files start at 1), canonical JSON SHA-256 chaining, append and
fsync. Payload `trajectory_backend` identifies `local-doc00-v1` or the shared
writer in orchestrated events. This is a temporary adapter, not C's replacement.

The local writer is single-process, as required by v1's one-worker deployment.
It checks the existing chain before every append and rejects incomplete lines,
tampering, missing required payload keys, unsafe path identities and registered
secrets. Peer sequences need only increase, as Doc 00 specifies; they need not
start at 1 or be contiguous. It never truncates or repairs files. Local `verify_chain(path)` returns
the first bad line or null. The local merger verifies each service file and
orders by `(ts, service, seq)` without adding a service field to TrajectoryEvent.
Use the shared verifier/merger when the pinned package is available.

Phase work carries payload `step` and a phase-instance `step_id`. Playbook
events preserve their actual playbook `step_id`; tool events carry the actual
provider `tool_call_id`. The local adapter links work to its last recorded
orchestrator phase event using `parent_event_id`. Explicit parent/session fields
remain available to the writer. Session events retain their session ID.

## Events and measurements

Actual Fetcher HTTP attempts, including robots and redirects, are recorded as
`state_change` with `operation=network_request`, sanitized URL, registry entry,
phase (`started`, `finished`, `failed`) and measured elapsed milliseconds.
Existing `fetch`, `block`, `session`, `download`, action and human-handoff events
remain. Source admission uses a state transition with source ID, document hash,
measured guard duration and the actual admission decision. It does not invent a
claim ID for document admission.

Every peer HTTP attempt first records `state_change` with
`operation=peer_request_started`, endpoint and request hash before network I/O,
so a killed process leaves a visible unfinished request. Graph completions use
`graph_query` with endpoint, canonical request hash,
actual HTTP status or observed interruption/unavailability, elapsed milliseconds
and attempt number. Workbench HTTP attempts use `state_change` with
`operation=peer_request`; actual review-created/ready states carry review IDs.
Search events include query, endpoint and actual result count. Tool state
transitions preserve start and failure, while each completed guard is recorded
immediately, before a later guard can fail or be interrupted. Playbook repair
attempts and failures are recorded separately from approved proposals.

The terminal orchestrator `state_change` includes extensible payload `metrics`:

| Value | Meaning |
|---|---|
| `elapsed_wall_seconds` | Measured monotonic time from execution start, including human wait |
| `browser_action_count` | Observed execution attempts; excludes dry-run markers |
| `retry_count` | Observed repeated peer attempts and Fetcher retry decisions |
| `recovery_attempts` | Playbook locator repair attempts actually started |
| `domain_count` | Distinct hosts of observed Fetcher HTTP attempts, including robots |
| `sources_visited` | Distinct successful capture source IDs, including cache hits |
| `evidence_admitted` | Source IDs actually admitted during this execution |
| `guard_outcomes` | Counts by guard and verdict for recorded claim checks |
| `models`, `model_status` | Names actually returned; unavailable when absent |
| `llm_calls`, `failed_llm_calls` | LLM client invocations, including failed cassette/provider calls; failed subset separately counted |
| `input_tokens`, `output_tokens`, `usage_status` | Actual returned usage; null totals if any call lacks usage |
| `observed_input_tokens`, `observed_output_tokens` | Known subtotals, never estimates for missing usage |
| `status`, `budget_stopped` | Actual terminal status and observed budget-stop notice |

These are Track A measurements at terminal transition, not cross-service
evaluation scores. Provider-internal retries and peer-service LLM usage are
explicitly unavailable. The unchanged RunStatus `cost_usd` is existing budget
accounting from configured prices and known tokens; it is not a provider bill
and is incomplete if usage/prices are unavailable. Recovered executions have
unavailable in-memory usage and elapsed values; C can inspect prior events.

Audit persistence failure marks the run failed (`INTERNAL_ERROR`) instead of
reporting success. In v1, active runs interrupted by restart fail `RESTART`;
queued runs start normally. Existing files are preserved. An absent terminal
event, corrupt chain or recording failure must be treated as incomplete evidence.

## Replay and ladder

`ladder_config(A0...A6)` uses Doc 00's exact configuration lists. Claim checks
honor those selections. Action Guard, registry/terms/robots policy, source
admission for evidence and dossier publication requirements remain enforced.
Off-registry A0 raw mode is intentionally unavailable under workspace safety
rules; resolve the recorded conflict before comparing an unguarded A0 baseline.

LLM replay accepts explicitly marked replay clients or uses a read-only local
cassette adapter with Doc 00 request hashing. Cache misses never call a provider.
The cassette envelope awaits C's confirmation; until then local cassettes are
explicitly identified compatibility fixtures. Full offline tests additionally
inject HTTP/Graph/Workbench fixtures, never silently substitute a live service.

## Integration dependencies

- Publish/pin `gba_contracts` v1.0.0 and verify writer constructor, emit signature,
  secret registration, serialization/hash vectors and cassette envelope.
- Verify live B/C tracing and collected per-service chains with C's contract suite.
- Resolve A0 safety conflict before unguarded baseline evaluation.
- Verify migrations and CLI smoke tests in the combined deployment; standalone A/PostgreSQL operation is verified.
- C owns S1-S10 evaluation and scored ladder comparisons; A's tests exercise
  recording and configuration, not legal correctness of illustrative fixtures.
