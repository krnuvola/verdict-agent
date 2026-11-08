# Track A simulator and acceptance gate

All pages, opinions, citations, dates, judge responses and reviews in this suite
are explicitly synthetic software fixtures. They are not legal authority and
need lawyer confirmation before any ground-truth use.

## Clean-checkout commands

Install Python 3.12 and uv, then from the repository root:

```powershell
uv sync --locked
uv run playwright install chromium
uv run python scripts/check_acceptance.py
```

Chromium installation is setup, not a simulator request. The test runtime uses
SQLite, in-memory blobs, explicit ASGI/mock HTTP transports and existing local
Graph/Workbench mock apps. It needs no real website, live LLM, PostgreSQL, B/C
process, credentials or fault-proxy installation. A supported installed Chrome
or Edge is also recognized by the existing browser fixture. Missing browsers
fail the tests rather than silently skipping them.

The gate runs Ruff, strict mypy, full pytest under branch coverage, and coverage
JSON generation, even when an earlier check fails. It writes:

- `.pytest_cache/acceptance/tests.xml`: all failures and skip reasons;
- `.pytest_cache/acceptance/coverage.json`: actual measured code coverage;
- `.pytest_cache/acceptance/report.json`: exact subprocess commands, exit codes,
  totals, guard branch coverage, missing dependencies and GO/NO-GO verdicts.

Gate thresholds are 100% branches for Action Guard and Source Gate, at least
80% overall lines and 80% overall branch-aware coverage, and passing tests/lint/
types. A single failed scenario prevents local GO. Absent shared contract checks
prevent integration GO. Default execution is offline. In a separately prepared
integration stack, `--check-peers` explicitly invokes C's installed
`gba-contracts check-service` for agent, graph and workbench using their configured
URLs. This does not replace C's real-service scenario or evaluation harness.

Live probes remain opt-in (`--live`, outside CI), separately marked and skipped
by default. The non-live suite rejects Python DNS/socket egress to non-loopback
hosts; BrowserRuntime additionally uses its existing dead-proxy/context routing
and Fetcher request permits. Unexpected requests fail, rather than receiving a
fabricated successful response.

## Simulator and evidence matrix

`tests/sitesim/app.py` exports a Starlette app for optional manual use on port
9100. Acceptance tests inject that app's request-recording transport, without
binding 9100. The `sitesim` registry is test-only and never enters production
configuration. Existing Fetcher, Operator and adapter fixtures remain intact.

| Requirement | Verification |
|---|---|
| Search/filter/pagination/PDF | Real-Chromium simulator flow plus existing browser/playbook integration tests |
| Redirects, denial, timeout, 429 recovery, PDF interstitial | Access Layer simulator matrix; actual request ledger and hash-chained events |
| Session expiry, login/MFA/CAPTCHA handoff | Simulator routes plus retained session/Fetcher/browser tests and recorded handoff/resume runs |
| Selector drift and repair | Simulator changes selectors per visit; retained `test_successful_step_repair_requires_human_release` exercises actual repair/release |
| Contradictory evidence | Two separately admitted sources are passed through PlannerTools; fact remains conflicting with both document hashes |
| S1/S2/S4/S5 | Original probes preserved; actual full guard reports and full runs using existing Graph/Workbench ASGI services, with bounded planner/judge replay |
| S3 | Existing point-in-time `NOT_KNOWN_AS_OF` and Temporal verdict assertions retained |
| S6/S7 | Existing source/quotation checks extended to final GuardReport assertions |
| S8 | Existing loopback fault transport, honest UA, bounded retry, no substitutions/off-registry requests; validates its trajectory chain and request-event count |
| S10 | Visible/hidden injection, native page POST and external image, hostile model proposals for COMMIT/SPEND/LOGIN/exfiltration/guard bypass; no forbidden execution and no secret disclosure |
| Interrupt/budget/B timeout/C timeout | Audited runs retain valid chains and exact abstention/status outcomes; existing restart and idempotency tests retained |
| API contract | Exact frozen route/model field/enum checks, NDJSON declaration, existing auth/version/ErrorEnvelope and mock contract suites |

S10 seeds a real HttpOnly browser cookie and verifies authenticated simulator
requests occurred, while the secret never reaches replay observations or logs.
It tests deterministic guard enforcement against hostile proposals; it does not
claim general model immunity to every possible prompt injection.

## Remaining blocker: literal S2 expectation

`tests/scenarios/test_S2.py::test_s2_holding_scope_passes_with_doc00_facts`
retains the literal Doc 00 expectation `pass / SCOPE_OK`. The current seeded
Match Group scope frame requires controller presence and transaction type;
Doc 00 S2 explicitly leaves both unknown. Actual Scope Guard therefore returns
`unknown / SCOPE_FACTS_MISSING`. The test is intentionally an ordinary failing
assertion, not xfail, skip, weakened guard, added fact or changed expected result.
The S2 144b claim still correctly has final `block / SCOPE_NON_APPLICATION`.

This unresolved S2 specification conflict is an integration gate. B/C and the contract owners must clarify the intended
fixture/scenario through their shared process. Track A cannot safely force a
legal applicability pass for missing required facts.

Published `gba_contracts` v1.0.0, its authoritative shared contract suite, real
B/C semantics, shared-writer/canonicalizer interoperability, real fault-proxy/CA
integration, combined deployment, A0 raw-mode conflict resolution and lawyer
review remain external integration work. Local route/shape tests and explicit
mock responses do not certify those dependencies or claim S9 redline acceptance.

## Verification status

Current targeted/full-suite results and measured coverage are recorded in
[IMPLEMENTATION_STATUS.md](../IMPLEMENTATION_STATUS.md). The full suite retains
one S2 failure and five opt-in live probes; functional acceptance is NO-GO.
Recorded coverage is a separate measurement and is not refreshed by a targeted
pytest invocation. Re-run the acceptance command to regenerate JUnit, coverage
and machine-readable reports for a particular checkout.

All five live skips use test_live_public_document in
`tests/integration/test_live_adapters.py`: sec_edgar-fetch_url,
de_code_online-fetch_statute, de_legislature-fetch_bill, de_courts-fetch_opinion,
and courtlistener-fetch_opinion. Their common reason is
`live source smoke tests require --live outside CI`.

Security assertions cover zero prohibited action execution and simulator POST
effects, no session-secret leakage, no off-registry requests, complete request
ledgers and valid hash chains. Real shared contracts, peer services and proxy
acceptance remain required before integration GO.
