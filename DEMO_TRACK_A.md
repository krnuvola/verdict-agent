# Track A demonstration guide

The supported demonstration combines a complete offline mock workflow with
standalone API health/schema checks. It does not certify real B/C integration.
All examples use PowerShell from the repository root. Do not use live-source
flags or submit forms. Legal dates/content and model responses are synthetic.

## Install and verify

```powershell
uv sync --frozen
uv run playwright install chromium
uv run pytest -q tests/acceptance/test_full_runs.py
uv run pytest -q tests/unit/test_action_guard.py
uv run pytest -q tests/scenarios/test_S2.py
uv run ruff check .
uv run mypy src/gba_agent
```

The complete offline suite is `uv run pytest -q --tb=short`. Its latest recorded
result is 709 passed, one S2 failure and five opt-in live skips. The S2 file
contains two passing tests and one failing assertion. See IMPLEMENTATION_STATUS.md
for current verification. Offline tests need no persistent server terminals.

## Complete existing mock workflow

```powershell
uv run pytest -q "tests/acceptance/test_full_runs.py::test_full_scenario_run_with_existing_peers[S1-block-TEMPORAL_SUPERSEDED]"
```

This executes A's orchestrator, Fetcher, guards, dossier construction and Workbench
client against explicit in-process B/C ASGI mocks, simulated source HTTP, SQLite,
fixture predicates and scripted LLM replay. It does not call separately served
mock ports. Assertions cover completion, blocked legal claim, contract-shaped
dossier, fixture review/edit, valid hash chain and absence of prohibited effects.
The four full-run cases are S1, S2, S4 and S5; the passing S2 statutory workflow
does not erase the separate holding-scope failure.

## Demonstrate Action Guard denial

```powershell
uv run pytest -q tests/unit/test_action_guard.py::test_commit_pattern_can_never_be_allowed tests/unit/test_action_guard.py::test_injection_and_misleading_labels_cannot_weaken_commit_classification
uv run python -c "from gba_agent.guards.action_guard import classify; from gba_agent.api.contracts_compat import BrowserAction; a=BrowserAction(action_type='click', target={'tag':'button','text':'Submit filing'}, page_url='https://example.test/search', site_id='site'); print(classify(a, {'entry_id':'site','base_domains':['example.test'],'allowed_action_classes':['READ','SEARCH']}, {'search_forms':[],'approved_uploads':[],'approved_destinations':[]}, None).model_dump_json(indent=2))"
```

The decision is COMMIT, allowed=false, requires_human=true,
AG_COMMIT_FORBIDDEN. Classification executes no browser action.

### Export and inspect the actual dossier, guard reports and trajectory

The existing test normally keeps its dossier in an in-memory fixture database.
Use pytest's debugger to export the fixture's completed run objects.
Run this **before** presenting, in the same terminal:

```powershell
uv run pytest -q -s --trace --basetemp=.pytest_cache/demo-track-a "tests/acceptance/test_full_runs.py::test_full_scenario_run_with_existing_peers[S1-block-TEMPORAL_SUPERSEDED]"
```

`--basetemp` replaces this dedicated ignored demo directory on each invocation;
do not place unrelated files there. At the first `(Pdb)` prompt enter:

```text
b tests/acceptance/test_full_runs.py:179
c
```

At the breakpoint in `test_full_scenario_run_with_existing_peers`, enter these
debugger commands, one line at a time. Line 179 is the final Workbench event
assertion in this checkout; if the file changes, locate that assertion first.

```text
!out = Path('.pytest_cache/demo-track-a/artifacts'); out.mkdir(parents=True, exist_ok=True)
!(out / 'dossier.json').write_text(json.dumps(data, indent=2), encoding='utf-8')
!(out / 'guards.json').write_text(json.dumps([report.model_dump(mode='json') for _, report in claims], indent=2), encoding='utf-8')
!(out / 'status.json').write_text(runner.status(run_id).model_dump_json(indent=2), encoding='utf-8')
!(out / 'trajectory.ndjson').write_text(runner.trajectory(run_id), encoding='utf-8')
!(out / 'agent-api.jsonl').write_bytes((tmp_path / run_id / 'agent-api.jsonl').read_bytes())
p {'run_id': run_id, 'state': runner.status(run_id).state, 'verdict': claims[0][1].final, 'reason': reason, 'edits': len(data['edits']), 'trajectory_events': len(events), 'chain_error': verify_chain(out / 'agent-api.jsonl'), 'effects': case.simulator.effects}
clear 1
c
```

A successful S1 run completes with `state=done`, final `block`, reason
`TEMPORAL_SUPERSEDED`, one fixture-proposed edit and a valid chain (`None` error).
Run/claim/source/review IDs vary. The recorded reference run contained 72 events.

Inspect the files from PowerShell after the debugger exits:

```powershell
$demo = '.pytest_cache/demo-track-a/artifacts'
Get-Content "$demo/status.json"
Get-Content "$demo/guards.json"
$dossier = Get-Content "$demo/dossier.json" -Raw | ConvertFrom-Json
$dossier.dropped_claims | ConvertTo-Json -Depth 20
$dossier.abstentions | ConvertTo-Json -Depth 20
$dossier.disclaimer
Get-Content "$demo/trajectory.ndjson" | Select-Object -First 3
Get-Content "$demo/trajectory.ndjson" | ForEach-Object { $_ | ConvertFrom-Json } | Where-Object type -eq 'guard_result' | ConvertTo-Json -Depth 20
uv run python -c "from pathlib import Path; from gba_agent.trajectory import verify_chain; print(verify_chain(Path('.pytest_cache/demo-track-a/artifacts/agent-api.jsonl')))"
```

Expected chain result: `None` (no broken line). The blocked claim is in
`dropped_claims`, not established authorities. `guards.json` shows Source Gate
`SG_OK`, Claim Verifier `CV_OK`, Temporal `TEMPORAL_SUPERSEDED`, Scope `SCOPE_OK`,
final `block`. The dossier has the disclaimer:
`Research dossier for review by a licensed lawyer. Not legal advice.`
The review receipt says `ready` only because the existing C mock returns it;
the proposed edit is a fixture, not a generated legal redline or accepted edit.

## Standalone service check

Follow README's `.env`, PostgreSQL and migration setup, then keep the API terminal open:

```powershell
docker compose --env-file .env -f docker/compose.dev.yml up -d --wait agentdb
uv run --env-file .env gba-agent migrate
uv run --env-file .env alembic current
$env:PGCONNECT_TIMEOUT='3'
uv run --env-file .env gba-agent serve --host 127.0.0.1 --port 8100
```

In another terminal:

```powershell
Invoke-RestMethod http://127.0.0.1:8100/health -TimeoutSec 3
uv run --env-file .env python -c "import os,httpx; r=httpx.get('http://127.0.0.1:8100/openapi.json', headers={'Authorization':'Bearer '+os.environ['SERVICE_TOKEN'],'X-Contract-Version':'1.0.0'}, trust_env=False, timeout=5); print(r.status_code); r.raise_for_status(); print(r.json()['info']['title'])"
```

Verified reference behavior: migrations 0002 (head), public health 200 with
service agent-api/version 0.1.0/contract 1.0.0; authenticated OpenAPI and docs 200.
Plain schema/docs requests require headers and return 422 without them. For
Swagger at http://127.0.0.1:8100/docs configure browser headers for both docs and
schema requests; external Swagger assets also require network access. Do not
remove authentication for a demonstration.

Optional mock HTTP servers need separate terminals, kept open while used:

```powershell
uv run --env-file .env uvicorn tests.mocks.mock_graph:app --host 127.0.0.1 --port 18101
uv run --env-file .env uvicorn tests.mocks.mock_workbench:app --host 127.0.0.1 --port 18102
```

Set PEER_MODE=mock before starting A, using the same service token for all three.
Health is at each origin's root. Mocks are development fixtures; full research
runs additionally require shared runtime/cassettes. There is no turnkey complete
standalone HTTP research demo from mock ports alone. Existing API runs can be
inspected with `gba-agent run status`, `run dossier` and `run trace`; offline
fixture IDs are not resources in the standalone PostgreSQL instance.

Stop local servers with Ctrl+C. Stop PostgreSQL without deleting its volume:

```powershell
docker compose --env-file .env -f docker/compose.dev.yml stop agentdb
```

## Two-minute walkthrough

Generate the artifacts before presenting.

1. 0:00?0:25: run the single S1 workflow test and identify the explicit mock boundaries.
2. 0:25?0:50: show status.json and guards.json: completion is done while the superseded claim is blocked.
3. 0:50?1:15: show the dossier's dropped claim, fixture review status and lawyer disclaimer.
4. 1:15?1:40: show trajectory guard events, source hashes and chain verification; print the COMMIT denial.
5. 1:40?2:00: show real health/authenticated OpenAPI and disclose S2 plus pending real B/C integration.

## Limits

S2's holding-scope assertion conflicts with Doc 00's own three-valued rules and
transaction restriction: the evaluator returns missing controller_present and
transaction_type, not pass. Neither facts nor expected verdicts are changed.
Shared owners must resolve the contract/scenario mismatch. Mandatory controls
also remain enabled at A0; it is not an unguarded baseline.

Mock Workbench ready status and edits do not demonstrate C's editing engine.
Fixture doctrine, model verdicts and usage do not establish legal ground truth
or real-provider measurements. B/C services, shared package/runtime, S3,
all-traffic proxy and combined deployment remain integration dependencies.
Dossiers and trajectories are inspectable evidence of A's control flow and
fail-closed behavior, not production/legal acceptance.
