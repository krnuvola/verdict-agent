# Track A CLI

Use the pinned environment: `uv sync --frozen`. Configure `.env` as described in README.
Commands below operate on existing Doc 00 endpoints or existing local migrations
and playbook proposals; they introduce no API routes.

```powershell
uv run --env-file .env gba-agent migrate
uv run --env-file .env gba-agent serve --host 127.0.0.1 --port 8100
uv run --env-file .env gba-agent run create request.json --ladder A5 --idempotency-key example-1
uv run --env-file .env gba-agent run status run_<ULID>
uv run --env-file .env gba-agent run trace run_<ULID> > trace.ndjson
uv run --env-file .env gba-agent run dossier run_<ULID> > dossier.json
uv run --env-file .env gba-agent run cancel run_<ULID>
uv run --env-file .env gba-agent handoff list --status open
uv run --env-file .env gba-agent handoff resolve hof_<ULID> --resolved-by reviewer --outcome resolved
uv run --env-file .env gba-agent playbooks approve 12 --reviewer reviewer
uv run --env-file .env gba-agent canary
```

Create `request.json` with the frozen `CreateRunReq`: `matter`, optional `config`,
`draft_doc_id`, `playbook_id`. `--ladder A0` through `A6` replaces only
`guards_enabled` and `use_workbench`; other budgets/options are preserved.
A6 needs an actual draft/playbook for Workbench review. All browser actions still
pass Action Guard. Site access and evidence publication remain fail closed.

Run/handoff commands call `AGENT_API_URL`, propagate tracing/version/bearer
headers and print the API JSON or trace NDJSON to stdout. API errors go to stderr
with nonzero exit status. Handoffs use the running API so its waiters and audit
events are preserved. Approval operates on the configured local database and
immutable playbook directory. Only a human should run `playbooks approve` after
reviewing the proposal. `serve` uses a single worker; `migrate` runs the existing
Alembic upgrade. They do not install a planner/provider or create secrets.

For LLM replay set `LLM_MODE=replay`. Supply an explicitly replay-safe shared
client, or the local `CassetteReplay` reads `LLM_CACHE_DIR/<request_hash>.json`.
The local adapter stores a raw result object with `model`, `text`, `tool_calls`,
`input_tokens`, `output_tokens`, `request_hash`; it has no network or provider
fallback. A missing cassette fails the run with `UPSTREAM_UNAVAILABLE` and a
recorded error. Live/record clients must be explicitly injected by deployment.
This option controls LLM calls; a fully offline run also needs explicitly
configured Graph/Workbench and Fetcher fixture transports (see tests).

A0 is subject to a recorded contract conflict: the workspace prohibits the
off-registry raw mode described in Doc 01. `ALLOW_UNREGISTERED_FETCH_FOR_BASELINE`
is required to request A0 but does not grant off-registry access, disable Action
Guard, bypass source admission or permit unsupported legal conclusions. Do not
label this implementation an unguarded A0 transport baseline in evaluation.
