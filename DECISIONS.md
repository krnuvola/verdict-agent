# Architectural decisions

These decisions preserve frozen Doc 00 v1.0.0. Cross-track changes require the
shared Change Request process. Open contract issues are listed in QUESTIONS.md.

## 2026-11-07 ? Safety and acquisition boundaries

All external source fetching uses Fetcher and a matching registry prefix,
including robots requests and redirects. Unverified terms disable production
acquisition. Redirects are rechecked; policy denials never become alternate
unguarded browser requests. Public immutable capture caches validate digest,
size and metadata; authenticated browser captures are not shared across runs.
Retries are bounded per candidate URL, and source alternates are explicit.

The Action Guard remains mandatory at every ladder level. COMMIT/SPEND/LOGIN
are not executed by the Operator. Unknown classifications are denied with
AG_UNCLASSIFIED_DENY and the contract-valid COMMIT class. The configured search
form exception for a lone `save` label does not override additional commit wording.
A0 retains registry, admission and publication controls; it is not an unguarded baseline.

Chromium uses controlled contexts, intercepted Fetcher exchanges, blocked service
workers/WebSockets and a deny proxy for unintercepted traffic. Observations are
untrusted; live targets and submitters are revalidated. Session state is encrypted
and bound to identity/expiry. Human challenges pause execution; authentication
must be verified before a checkpoint is accepted. Human egress remains restricted.

## 2026-11-07 ? Contracts, persistence and shared runtime

Public schemas, IDs, reason codes and endpoint paths follow Doc 00. Temporary
DTO/registry/replay/trajectory adapters are explicit and shared-first; a broken
installed shared package does not silently select a fallback. No package URL/tag
is assumed. Pin C's actual immutable artifact before shared compatibility signoff.

Create-once repositories reject changed content under reused IDs. Idempotency
receipts compare request hashes. One API worker persists run phases and creation
receipts before scheduling. Queued runs can start after restart; active/waiting
runs fail RESTART. Saved dossiers remain retrievable. Runtime recording failure
cannot produce a successful terminal result.

## 2026-11-07 ? Evidence and legal conclusions

Adapters receive run-scoped Fetcher capabilities. Metadata discovery is distinct
from admitted legal evidence. Type, identity, publication date and graph linkage
remain internal source metadata where SourceRecord has no matching fields.
Admission verifies canonicalization, hash/type/length/linkage and immutable blobs
before a source becomes eligible for claims. Date and known-null semantics remain
three-valued; conflicting facts retain their provenance and remain disputed.

Publishing an authority requires admitted citation evidence and passing Source,
Claim, Scope and Temporal checks, even at weaker ladder configurations. Graph
standards lacking verified basis are retained as unverified responses and surfaced
as unknown; A does not infer replacement legal standards. GNN is advisory.
Condition and evidence gaps remain in the dossier with explicit human-review notices.

## 2026-11-07 ? Browser playbooks and verification

Adapters preserve the registry-selected acquisition method. CourtListener's fixed
read-only citation lookup POST is distinct from prohibited browser POST effects;
its metadata response is not admitted as a legal source document. Playbook repair
is locator-only, preserves action intent and requires explicit human release.
Released versions are immutable; an interrupted identical approval can reconcile.

Claim verification uses exact canonical offsets and quotations. Judge input is
limited to proposition/passage; unavailable judges abstain. Explicit paragraph
markers are required for paragraph pincites. Hashes and guarded verdicts cannot
be replaced by fabricated source text or unsupported model conclusions.

## 2026-11-07 ? Recording, replay and measurements

Prefer the published shared writer/verifier. The local adapter follows frozen
fields, canonical JSON hashes, UTC milliseconds, append/fsync and corruption
refusal. Local sequences start at 1; peer sequences need only increase. V1 is
single-process; automatic repair and cross-process appends are not supported.

Record actual source/peer attempts, failures, guards, handoffs and phase relationships.
Never record secrets, session state, full page bodies or full LLM prompts. Missing
model/usage values remain unavailable, with known subtotals separate. Cost is
configured budget accounting, not a provider bill or cross-service evaluation score.

Replay misses never contact live providers. Local HTTP and LLM cassette formats
are explicitly identified compatibility conventions pending C's confirmation.
Fixture LLM outputs/usage are not real-provider measurements. Offline tests reject
unexpected external egress; live probes require explicit opt-in outside CI.

## 2026-11-08 ? Peer services and optional review

Peer mode defaults to live. Mock origins and read-only HTTP replay require explicit
selection; production rejects mock mode. Runtime clients check response versions,
correlation headers, DTOs and exact error envelopes. Only safe/idempotent requests
are retried, with bounded delays. Proxy and CA routing are explicit.

Workbench runs only when enabled with draft/playbook inputs. Save the research
dossier before optional review, use a run-stable idempotency key, and preserve IDs,
provenance and proposed status. Failures preserve a labelled partial dossier;
A does not create redlines or imply that an unavailable review completed.

CLI run/handoff operations use existing API resources. Serve/migrate invoke the
existing Uvicorn/Alembic entry points. Load `.env` explicitly for subprocesses;
Settings loading dotenv does not export values to Alembic's environment.

## 2026-11-08 ? S2 specification conflict

Keep the literal failing assertion active. Doc 00's explicit transaction-type
restriction and unknown-fact semantics cannot produce S2's expected Scope pass
with only its stated dates. No facts or restrictions are changed locally. Shared
owners must decide whether to amend the scenario setup or expected scope result.
