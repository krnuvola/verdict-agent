# 03 · Track C LLD: Contracts Package, Document Workbench, Evaluation Harness, Integration

**Owner:** Person C · **Version:** 1.0.0 (pairs with Doc 00 v1.0.0) · **Repos:** `gba-contracts` and `track-c-workbench` (with the `integration/` folder) · **Service:** `workbench-api` on port 8102 · **DB:** SQLite file (default)

Read Doc 00 first and keep it open. This document never redefines a schema, enum, endpoint, fact key or reason code from Docs 00 or 01. Rules R1 to R10 of Doc 00 apply in full. Track C is the only track whose first deliverable blocks the others, so **C-0 and C-2 come first**.

---

## 1. Mission and boundaries

Track C owns (1) the shared `gba_contracts` package that A and B import, (2) shared fixtures and mocks, (3) the Document Workbench (read and edit agreements as tracked changes), (4) the evaluation harness and fault proxy, (5) the integration repo (compose, end-to-end tests).

### 1.1 Deliverables

| ID | Deliverable | Section |
|---|---|---|
| C-0 | `gba_contracts` package v1.0.0 | 3 |
| C-1 | Fixtures from the Doc 00 section 13 table (S1 to S10) | 4 |
| C-2 | Shared mock servers (`gba_contracts.mocks`) and contract-test CLI | 5 |
| C-3 | Workbench skeleton, ingest, clause tree, defined terms, cross-references | 7, 8 |
| C-4 | Clause classification and deal-point extraction | 9 |
| C-5 | Playbook engine and graph grounding | 10, 11 |
| C-6 | Edit proposer, consistency checker | 12, 13 |
| C-7 | Redline writer, accepted-copy writer, summary | 14 |
| C-8 | Evaluation harness (probes, ladder, metrics, calibrator) | 15, 16 |
| C-9 | Fault-injection proxy | 17 |
| C-10 | Integration repo, compose, e2e (details in Doc 04) | Doc 04 |

### 1.2 Consume and expose

| Direction | Counterpart | What |
|---|---|---|
| Consume | A | Guard API `POST /guards/v1/verify/claims` (verify every edit's authority claims), Orchestrator API (eval runs), merged trajectory |
| Consume | B | Graph API (node lookup, `rule_candidates`, document text, `search/documents`) only when no dossier is supplied |
| Expose | A and B | the `gba_contracts` package, fixtures, mocks, contract tests |
| Expose | A and eval | Workbench API (Doc 00 section 9.3) |
| Expose | everyone | fault proxy on 8199, integration stack, eval reports |

### 1.3 Out of scope
Browsing, guards, graph construction, GNN training, fact extraction from proxy statements.

---

## 2. Repository layouts

### 2.1 `gba-contracts`
```
gba-contracts/
  pyproject.toml uv.lock README.md CHANGELOG.md DECISIONS.md
  src/gba_contracts/
    __init__.py            # __version__ = '1.0.0'; CONTRACT_VERSION = '1.0.0'
    models.py              # verbatim from Doc 00 section 6
    facts.py               # FACT_KEYS: dict[key] -> type letter; ENUM_VALUES
    predicates.py          # verbatim Doc 00 section 7.1
    scope.py               # applicability() per Doc 00 section 7.2
    canon.py               # to_canonical_text(), CanonResult, CANON_VERSION
    registry.py            # load_registry(), validate_registry(), RegistryEntry
    blobstore.py           # BlobStore, FileBlobStore, S3BlobStore, BlobConflict
    llm.py                 # LLMClient, LLMResult, AnthropicClient, OpenAICompatClient, ReplayClient, LLMCacheMiss
    trajectory.py          # TrajectoryWriter, verify_chain, merge
    http.py                # common headers helpers, ErrorEnvelope helpers, make_health()
    ids.py                 # new_id(prefix), is_valid_id()
    hashing.py             # canonical_json(), sha256_hex(), doc_id_for(bytes)
    fixtures/              # section 4
    mocks/                 # section 5 (mock_graph, mock_agent, mock_workbench)
    contract_tests/        # section 5.3
    cli.py                 # gba-contracts check-service, gen-fixtures, verify-chain
  tests/
```
Releases are git tags `vX.Y.Z`. Consumers install `gba_contracts @ git+<url>@v1.0.0`.

### 2.2 `track-c-workbench`
```
track-c-workbench/
  pyproject.toml uv.lock README.md DECISIONS.md QUESTIONS.md
  config/playbooks/delaware-controller-v1.yaml
  src/gba_workbench/
    config.py
    api/ main.py routes.py deps.py errors.py auth.py
    db/ models.py session.py (SQLite via SQLAlchemy; Alembic)
    ingest/ docx_reader.py pdf_reader.py html_reader.py ocr.py structure.py
            defined_terms.py crossrefs.py
    classify/ rules.py llm_check.py deal_points.py
    playbook/ schema.py engine.py
    ground/ authority.py claims.py
    edits/ proposer.py validator.py consistency.py
    render/ redline.py accepted.py summary.py ooxml.py
    clients/ graph_client.py agent_client.py
    pipeline.py            # review state machine
    cli.py                 # gba-wb: serve, playbook validate, review run
  src/gba_eval/
    probes.py ladder.py metrics.py trajectory_metrics.py gold.py
    calibrator.py report.py bootstrap.py cli.py
  src/gba_faultproxy/ addon.py rules.py control_api.py cli.py
  integration/               # Doc 04
  tests/ unit/ integration/ scenarios/ golden/
```

## 3. `gba_contracts` package (C-0)

Content is fixed by Doc 00. Implementation notes and exact signatures:

| Module | Contract |
|---|---|
| `models.py` | verbatim Doc 00 section 6, no additions. `Predicate = dict[str, Any]`. |
| `facts.py` | `FACT_KEYS` mapping each section 5.2 key to its type letter; `ENUMS` for enumerated fact values. Used by `predicates.validate` and by B and A. |
| `predicates.py` | verbatim Doc 00 section 7.1 plus unit tests (100% branch): every operator, Kleene truth tables, missing facts from both branches, known-null date semantics, `conflicting` treated as unknown, `validate` rejects unknown keys. |
| `scope.py` | `applicability(frame, facts, as_of) -> (Tri, missing: list[str], failed_clause: str | None)` exactly per Doc 00 section 7.2. Evaluate every clause (no short-circuit) so `missing` is complete. Combine with Kleene AND. `failed_clause` is the **first** clause in the order `in_force`, `addressee`, `transaction_type`, `applies_when`, `exception:<id>`, `non_application` whose value is `False`; `None` when result is true or unknown. Presence mapping for addressee per 7.2. Tests: every clause failing alone, all combos of unknown, S1 to S5 frames. 100% branch. |
| `canon.py` | `to_canonical_text(raw: bytes, media_type: str) -> CanonResult(text, markers: list[{label, start}], canon_version=1)`. Implement exactly the four steps of Doc 00 section 3.3 in order. HTML via `lxml.html`; PDF via `pdfminer.six` default layout with `\f` page separators; plain text UTF-8 with replacement; DOCX paragraphs joined by `\n`. Star-page markers `[*123]` for non-PDFs. Provide `canon_keys(doc_id)` returning `canon/<hex>.v1.txt` and `.pages.json`. Golden tests with fixed inputs and outputs; property test that `quote == text[start:end]` for any span built from the text. Determinism: same bytes give identical output across runs. |
| `registry.py` | `load_registry(path) -> Registry` validating every field of Doc 00 section 8 (types, required fields, unique `entry_id`, role and access enums, that `terms_verified: true` requires `terms_url` and `terms_checked_on`). `match_url(url) -> RegistryEntry | None` by longest `url_prefixes` match. |
| `blobstore.py` | `put(key, data, content_type)`, `get(key)`, `exists(key)`. `file://` and `s3://bucket?endpoint=...` (boto3 or minio SDK; pin it). Immutable keys; `BlobConflict` if bytes differ. Keys validated against `raw/<64hex>`, `canon/<64hex>.v1.txt`, `canon/<64hex>.v1.pages.json`, `source/src_<ULID>.json`, `out/<review_id>/...`. |
| `llm.py` | Protocol per Doc 00 section 12; `AnthropicClient`, `OpenAICompatClient` (selected by `LLM_PROVIDER`); `ReplayClient` and a recording wrapper (`LLM_MODE`). `request_hash` per the formula in section 12. 3 retries with 1 s, 4 s, 16 s backoff on 429/5xx/timeout only. `LLMCacheMiss` in replay mode. Model per role from `LLM_MODEL_<ROLE>`; error if `LLM_MODEL_JUDGE == LLM_MODEL_PLANNER` when both are set and the judge role is used. No model names hard-coded. Never log prompts or API keys (hash and sizes only). |
| `trajectory.py` | `TrajectoryWriter(service, run_id)` writing `TRAJ_DIR/<run_id>/<service>.jsonl`; `seq` monotonic per file; fsync after each event; hash chain exactly per Doc 00 section 10; payload validation against the required-keys table; rejects payloads containing known secret strings (registry of secrets via `register_secret(value)`). `verify_chain(path) -> int | None`. `merge(run_id)` orders by `(ts, service, seq)`. |
| `http.py` | `make_health(service, version)`; `ErrorEnvelope` builders; header propagation helper (`X-Request-Id`, `X-Run-Id`, `X-Matter-Id`, `X-Contract-Version`); bearer-auth FastAPI dependency; contract-version check returning `CONTRACT_VERSION_MISMATCH` when majors differ. |
| `ids.py` | `new_id('run')` etc. per Doc 00 section 3.1 (`python-ulid`). Validation regexes for each prefix. |
| `hashing.py` | `canonical_json`, `sha256_hex`, `doc_id_for(raw)` returning `sha256:<hex>`. |

Publish steps: tag `v1.0.0-rc1` as soon as `models.py`, `predicates.py`, `ids.py`, `hashing.py` and `canon.py` pass their tests (target day 3) so A and B can switch from `contracts_local/`; tag `v1.0.0` at milestone M0 (Doc 04 section 2).

## 4. Fixtures (C-1)

Generated by a script `gba-contracts gen-fixtures` from the Doc 00 section 13 table; outputs are committed. Layout under `gba_contracts/fixtures/`:
```
seed_graph.json                  # nodes + edges, all ids from section 13 (+ Provision 144a stub, Court nodes)
matters/S1.json ... S10.json     # Matter models (validated)
docs/                            # synthetic documents (HTML/plain/PDF-generated) incl. a synthetic 144b text with a known quotable sentence,
                                 # a synthetic opinion for Match Group with a known pincite, a page with injected-instruction text (S10)
claims/S*.json                   # Claim lists per scenario
expected/S*.json                 # expected GuardReport.final, reason codes, failed_clause, missing_facts, standard_of_review
draft_agreement_S9.docx          # synthetic 2026 controller (non-going-private) merger agreement, no committee approval closing condition
playbooks/delaware-controller-v1.yaml
```
Rules: each scenario S1 to S10 has `matter`, `probe`, `expected` files matching the Doc 00 section 13 expected-result column exactly (reason codes and verdicts verbatim). Dates in fixtures are illustrative; the lawyer must confirm before any result is ground truth. A test (`test_fixtures_valid.py`) loads every fixture through the Pydantic models and runs `predicates.validate` on every frame. The seed graph must contain the conditional edge `Provision:de.dgcl.144b@2025-03-25 --superseded_by_statute--> Holding:match-group-2024` with `valid_from 2025-03-25` and the exact condition from Doc 00. Scenario S9 draft: article titles "Article VI Conditions" with Section 6.1 Conditions to Each Party, 6.2 Conditions to Parent, 6.3 Conditions to Company; defined terms ("Company", "Merger", "Special Committee", "Controller", "Effective Time") and several cross-references; and no committee-approval condition.

## 5. Shared mocks and contract tests (C-2)

Delivered in week 1 so A and B do not have to build their own for long (Docs 01 and 02 allow local mocks until this is published; then delete them).

### 5.1 `mocks/mock_graph.py`
FastAPI app implementing Doc 00 section 9.1 on seed_graph.json. Must: implement point-in-time filtering (`NOT_KNOWN_AS_OF`), return `ValidityResult` with unevaluated conditions, `scope_frame` without evaluation, `rule_candidates` by substring match on label (frames always attached), `standard_of_review` using `predicates` + `scope.applicability` with the algorithm of Doc 02 section 10 (a smaller reference implementation is acceptable, but S1, S2, S4, S5 outputs must match `expected/`), `documents/*` from `fixtures/docs`, `extract/facts` returning canned facts per matter id, `gnn/*` returning calibrated 0.9 for edges in the seed and 0.1 otherwise (flag below 0.3).

### 5.2 `mocks/mock_agent.py` and `mock_workbench.py`
`mock_agent`: `/guards/v1/verify/claims` using fixture expected reports keyed by claim text hash, `/guards/v1/action/classify` returning `AG_READ_OK` for navigate and `AG_COMMIT_FORBIDDEN` for labels matching "submit", `/orch/v1/runs*` returning a canned dossier. `mock_workbench`: `/wb/v1/reviews` returning one canned insert edit for S9.
All mocks: bearer auth, tracing headers, `ErrorEnvelope`, `/health`, `/openapi.json`. Start with `gba-contracts mock graph|agent|workbench --port N`.

### 5.3 Contract tests (`gba_contracts.contract_tests`)
`gba-contracts check-service <graph|agent|workbench> <base_url>` runs: (1) `/health` shape; (2) `/openapi.json` fetched and compared to the generated reference OpenAPI for that service (diff reported by path/method/schema); (3) for each route a fixture request through `httpx` and response validation with the models; (4) auth (401 on missing/wrong token); (5) error envelope shape; (6) `X-Contract-Version` mismatch gives `CONTRACT_VERSION_MISMATCH`; (7) schemathesis against the OpenAPI. Mocks must pass these tests in CI of `gba-contracts`.

---

## 6. Workbench configuration

| Variable | Meaning | Default |
|---|---|---|
| `WORKBENCH_DB_URL` | SQLAlchemy URL | `sqlite:///./workbench.db` |
| `PLAYBOOK_DIR` | playbook files | `./config/playbooks` |
| `OCR_MIN_CHARS_PER_PAGE` | below this a PDF page is treated as scanned | 200 |
| `OCR_LANG` | tesseract language | `eng` |
| `EDIT_MAX_PER_REVIEW` | cap on proposed edits | 40 |
| `REDLINE_AUTHOR` | author on tracked changes | `GBA Workbench` |
| `GUARD_VERIFY_TIMEOUT_S` | call to A | 60 |
| `WB_USE_DOSSIER_ONLY` | do not query B when a dossier is supplied | true |

Startup checks: `SERVICE_TOKEN`, LLM role `drafter` and `extractor` models set (when `LLM_MODE=live`), playbooks load and validate, blob store reachable.

## 7. Workbench data and states

SQLite tables (Alembic 0001): `documents(doc_id pk, media_type, record json, created_at)`, `playbooks(playbook_id, version, yaml, created_at, pk(playbook_id, version))`, `reviews(review_id pk, doc_id, playbook_id, playbook_version, state, request json, edits json, consistency json, error, created_at, updated_at)`, `edit_decisions(edit_id pk, review_id, decision, edited_new_text, reviewer, decided_at)`, `idempotency(...)`.

Review state machine (persisted in `reviews.state` as Doc 00 `ReviewStatus.state`): `queued` → `running` (substeps logged as `state_change` events: INGEST, CLASSIFY, COMPARE, GROUND, PROPOSE, VERIFY, RENDER) → `ready` or `failed`. A failure of a single edit never fails the review; the edit is dropped and recorded in `consistency_report.dropped_edits`.

## 8. Ingestion and structure (C-3)

`POST /wb/v1/documents` (`IngestDocReq`): raw bytes read from blob `raw/<hex>`; verify hash. Produce `DocRecord`.
- **DOCX** (the editable path): `python-docx` plus `lxml` on `word/document.xml`. Keep, for every paragraph, its XML index, style, numbering properties and runs. Redlines are written back into this same XML (section 14).
- **PDF**: `pdfminer.six` layout text with `\f`; if average characters per page is under `OCR_MIN_CHARS_PER_PAGE` run `ocrmypdf` (`ocr_used=true`) then extract. PDF and HTML drafts support read, classify and edit proposals but redline output returns 422 `UNSUPPORTED_FOR_REDLINE` (decision logged); only DOCX produces `redline.docx` and `accepted.docx`.
- **HTML**: `lxml.html`, block elements to paragraphs.
- The text used for offsets of `ProposedEdit` is the **paragraph text** (concatenated run text after NFC normalisation, no other canonicalisation; tabs preserved). It is distinct from canonical text of Doc 00 section 3.3, which is used only for citing authorities. `start`/`end` are character offsets in that paragraph text.

**Clause tree (`ingest/structure.py`)**: detect headings and numbering by regex over paragraph text and numbering metadata: `Article <roman|arabic>`, `Section N.N`, `N.N`, `(a)`, `(i)`, `(A)`. `ClauseNode.clause_path` format `Article VI > Section 6.2 > (a)` using the exact number tokens found. `number` the token; `heading` the heading text. Paragraphs not under any heading attach to a synthetic `Preamble` node (`Preamble`, `Recitals`, `Signature` where detected).
**`paragraph_id`**: `p_` + first 12 hex of SHA-256 of `doc_id + '|' + clause_path + '|' + ordinal_within_clause` (stable for the same raw bytes).
**Defined terms**: patterns `("Term")`, `(the "Term")`, `"Term" means`, `"Term" shall mean`; record `DefinedTerm(term, paragraph_id, start, end)` at the defining occurrence. Also collect uses (internal) by exact capitalised-term match.
**Cross-references**: `Section 6.2`, `Sections 6.2 and 6.3`, `Article VI`, `clause (a)` within the current section; resolved by lookup in the clause tree; `CrossRef(from_paragraph_id, start, end, target_clause_path, resolved)`. Unresolved references are listed, not silently dropped.

## 9. Classification and deal-point extraction (C-4)

Clause types (`ClauseNode.clause_type`): `definitions`, `conditions_closing`, `committee_approval`, `mom_condition`, `stockholder_vote`, `fiduciary_out`, `termination_fee`, `termination_rights`, `no_shop`, `representations`, `covenants`, `miscellaneous`, `other`. 
v1 classifier (`classify/rules.py`): heading keyword and body-regex scoring per type, output `type_confidence` in 0..1. For confidence below 0.6 call `LLMClient(role='extractor')` with the heading and first 1200 characters (in `<document_text>` markers) and a closed enum response schema; take the LLM label only if it is in the enum; otherwise `other`. A fine-tuned legal encoder is an optional later upgrade behind the same interface (`ClauseClassifier.classify(paragraphs) -> list[(type, conf)]`); not required for v1.
Deal points (`classify/deal_points.py`) mapping a document to boolean presence facts used by the playbook: `has_committee_approval_condition`, `has_mom_condition`, `has_stockholder_approval_condition`, `has_fiduciary_out`, `termination_fee_present`. Each carries the supporting paragraph ids or `None`. Computed by regex first; LLM check only for ambiguous clauses. Deal points are about the **draft agreement text** and are never written into `Matter.facts`.

## 10. Playbooks (C-5)

Endpoints `PUT/GET /wb/v1/playbooks/{playbook_id}` (YAML body, versioned automatically: each PUT with different content increments `version`; same content returns the existing version).

Playbook schema (Pydantic in `playbook/schema.py`, validated at PUT; 422 `VALIDATION_ERROR` otherwise):
```yaml
playbook_id: delaware-controller-v1
description: ...
jurisdiction: US-DE
applies_to:                       # Predicate over Matter facts; if false or unknown, no rules run (reported)
  all:
    - {fact: transaction_type, eq: other_controller_deal}
    - {fact: controller_present, eq: true}
rules:
  - rule_id: pb-committee-approval-condition
    title: Special committee approval closing condition
    clause_type_target: conditions_closing
    target_clause_paths: ['Article VI > Section 6.1']   # edits allowed ONLY under these paths (prefix match)
    detect:                                             # over draft deal points
      deal_point: has_committee_approval_condition
      expect: true
    risk: high
    preferred: "text of preferred clause"
    fallbacks: ["fallback 1"]
    insert_template: "(c) the Transaction shall have been approved by {{Special_Committee}} ..."   # tokens {{DefinedTerm}} must resolve to defined terms
    defined_terms_required: [Special_Committee, Merger]
    authority:
      node_id: "Provision:de.dgcl.144b@2025-03-25"
      element_node_id: "Element:committee-approval"
      proposition: "Approval by a committee of disinterested directors can provide a safe harbor for a controlling stockholder transaction that is not a going-private transaction."
    requires_facts: [special_committee_formed]          # optional; unknown facts yield a 'needs_fact' note, not an edit
```
Playbooks are versioned and immutable once released (a PUT creates a new version). Edit `playbook_rule_id` = `<playbook_id>@<version>:<rule_id>`.

## 11. Grounding and authority claims (C-5)

For each rule that fires:
1. Resolve the authority. If `CreateReviewReq.dossier` is present, find an `AuthorityUse` with the same `node_id` and use its `spans`, `source_id`, `proposition` (do not re-query the graph, `WB_USE_DOSSIER_ONLY`). If absent, call B: `GET /nodes/{id}?as_of`, `POST /search/documents` and `GET /documents/{doc_id}/text` to find a supporting sentence, require an exact substring match of a quote under 300 characters in canonical text, build the `Span`.
2. Build a `Claim`: `kind='legal_proposition'`, `text=proposition`, `authority_node_id`, `element_node_id`, `source_doc_id`, `source_id`, `quote`, `span`, `as_of = matter.as_of`, `proposed_by='workbench.ground'`, `claim_id='clm_'+ULID`, `matter_id`.
3. `POST /guards/v1/verify/claims` to A with the matter and claims (batch per review, max 50). Attach each `GuardReport` to the edit (`guard_reports`, `authority_claim_ids`). `final != 'pass'`: the edit is **not proposed**; it is recorded in `consistency_report.dropped_edits` with the report (never softened). `unknown`: also not proposed; recorded under `open_items`.
4. If A is unavailable (timeout, 5xx): the review continues with no edits for authority-bearing rules and `consistency_report.guard_unavailable=true`. Never attach a made-up pass.

## 12. Edit proposer (C-6)

`edits/proposer.py` uses `LLMClient(role='drafter', response_schema=EDIT_SCHEMA, temperature=0)`. Input: the rule, the target clause paragraphs (ids and text, in `<document_text>` markers), defined terms list, cross-ref list. Output schema: a list of `{op, paragraph_id, start, end, new_text, rationale}`.

Constraints enforced by `edits/validator.py` before accepting any model output (violation drops the proposal):
1. `paragraph_id` exists and its clause path starts with one of the rule's `target_clause_paths`.
2. `0 <= start <= end <= len(paragraph_text)`; for `insert`, `start == end`; `old_text = paragraph_text[start:end]` computed by the Workbench (never taken from the model).
3. `new_text` non-empty for insert/replace; each `{{DefinedTerm}}` in templates replaced with the actual defined term from the document; any capitalised defined term in `new_text` must exist in `defined_terms`.
4. Operation count per rule ≤ 3; total ≤ `EDIT_MAX_PER_REVIEW`.
5. `rationale` must cite `authority_claim_ids` that exist and passed the guards (section 11).
6. `risk` from the playbook rule.
Ids: `edit_id = 'edt_'+ULID`. Status `proposed`.

For an *insert of a new condition paragraph* where no suitable paragraph exists, the insert target is the last paragraph of the target clause and `start == end == len(paragraph_text)` with `new_text` starting with a newline marker that the renderer converts into a new paragraph (render rule in section 14). This is how the S9 expected "insert edit in the conditions article" is produced.

## 13. Consistency checker (C-6)

`edits/consistency.py` is deterministic and runs on the document **as if all proposed edits were applied** (simulate on the structure model, not the XML). Output `consistency_report` (dict, exact keys):
```json
{
  "defined_terms_ok": true, "unresolved_terms": [],
  "cross_refs_ok": true, "broken_cross_refs": [],
  "numbering_ok": true, "numbering_issues": [],
  "out_of_scope_changes": 0,
  "authority_guards_ok": true,
  "dropped_edits": [], "open_items": [], "guard_unavailable": false,
  "rules_not_applicable": []
}
```
Checks: (a) every defined term used after edits still has a defining occurrence; (b) every cross-reference resolved before edits is resolved after; new insertions introducing references must resolve; (c) numbering sequences within each section remain contiguous (inserting `(c)` where `(c)` exists renumbers following items, which is an *edit* the proposer must emit as additional `replace` ops within the same clause path; otherwise `numbering_ok=false`); (d) `out_of_scope_changes` counts changed characters whose paragraph is outside every approved path (target 0); (e) all attached `guard_reports.final == 'pass'`.
S9 requires all checks green and zero changes outside approved paths.

## 14. Rendering (C-7)

`render/ooxml.py` operates on `word/document.xml` of the original `.docx` (preserve all other parts byte-identical).
- **redline.docx**: for each proposed edit insert `<w:ins w:id w:author=REDLINE_AUTHOR w:date=UTC>` runs and `<w:del>` with `<w:delText>` for deletions, splitting runs at `start`/`end` while preserving run properties; a comment (`word/comments.xml`) anchored to the change with the rationale, authority node label and playbook rule id. A new-paragraph insert becomes a new `<w:p>` with `<w:rPr><w:ins .../></w:rPr>` paragraph mark insertion. Includes all edits regardless of status.
- **accepted.docx**: the original with `accepted` and `edited` (using `edited_new_text`) edits applied as plain text, no tracked changes; rejected and proposed edits ignored.
- **summary.md**: plain-language list per edit (clause path, what changes, why, authority label, risk, status), plus the consistency summary and any dropped/open items. Ends with the disclaimer from `Dossier.disclaimer`.
Validation tests: output opens with `python-docx` and with LibreOffice headless conversion (CI optional); XML diff outside the edited paragraphs is empty; accepting all changes in the redline equals `accepted.docx` text (compare extracted text). Endpoint behaviour per Doc 00 section 9.3; `redline.docx` returns 409 `REVIEW_NOT_READY` if state is not `ready`.

`POST /wb/v1/reviews/{review_id}/edits/{edit_id}/decision` sets `status` to accepted, rejected or edited (edit requires `edited_new_text`, validated by section 12 rules 2 and 3), records the reviewer, emits `edit_decided`. Nothing leaves the workspace without approval: `accepted.docx` contains only approved edits.

Trajectory events written by the Workbench (`TrajectoryWriter('workbench-api', run_id)`, component `workbench`): `state_change`, `llm_call`, `graph_query` (when it calls B), `guard_result` (copy of each report received, with `claim_id`), `edit_proposed`, `edit_decided`, `error`. `run_id` from the request's `run_id` or `X-Run-Id`, else `adhoc`.

---

## 15. Evaluation harness (C-8)

Package `gba_eval`; CLI `gba-eval`.

### 15.1 Gold set format (`gold/deals/<deal_id>.json`)
```json
{
  "deal_id": "...", "matter": { "...Matter..." },
  "labels": {
    "standard_of_review": "business_judgment|entire_fairness|unknown",
    "safe_harbor": "true|false|unknown",
    "conditions": {"<Element node id>": "met|unmet|unknown"},
    "key_authorities": ["<node ids>"],
    "facts": {"<fact_key>": {"value": "...", "status": "known|unknown"}}
  },
  "annotator": "...", "reviewed_on": "YYYY-MM-DD"
}
```
Silver set `gold/silver/<opinion_id>.json`: `{opinion_doc_id, as_of (decision date minus 1 day), cited_authorities: [node ids]}`. A spreadsheet template for the lawyer reviewer is generated by `gba-eval gold-template`.

### 15.2 Probes (guard precision and recall)
`probes.py` builds injected probes from fixtures and calls A's Guard API: fabricated authority (S6-style), group A rule in group B matter (S4-style), superseded rule (S1), non-application (S2), fabricated quote (S7), point-in-time (S3), prompt injection (S10, via the site simulator or fixture page), injected block and expired session (via the fault proxy, S8). Metrics: per probe type, precision, recall, F1 of "blocked/unknown when it should be" versus "pass when valid"; plus a set of valid control claims to measure over-blocking.

### 15.3 Ladder runner
`ladder.py`: for each gold deal and each ladder step A0 to A6, `POST /orch/v1/runs` with `RunConfig.guards_enabled` per the Doc 01 section 10.5 table (`A6` sets `use_workbench=true` with `draft_doc_id` and `playbook_id`), poll to completion, fetch the dossier and merged trajectory, store under `results/<ladder>/<deal_id>/`. Runs are seeded by `LLM_MODE=replay` for CI and `live` for the real experiment. A0 requires A to be started with `ALLOW_UNREGISTERED_FETCH_FOR_BASELINE=true` (never in prod).

### 15.4 Metrics (`metrics.py`, `trajectory_metrics.py`)
All computed from dossier, gold, and merged trajectory; each is a pure function with unit tests on fixtures:

| Proposal metric | Definition |
|---|---|
| Standard-of-review accuracy | dossier.standard_of_review.value == gold; `unknown` counts as abstain, reported separately from wrong |
| Per-condition F1 | over `met/unmet/unknown` per Element, macro-averaged |
| Authority Recall@k, MRR | rank of gold `key_authorities` in dossier authorities order |
| Scope-misapplication rate | share of passing authority uses whose scope applicability on gold facts is false |
| Superseded-authority rate | share of passing uses with an applicable blocking treatment |
| Invalid-citation rate | cited nodes not in graph or not crosschecked |
| Span-support rate, pincite accuracy | judged from `GuardResult` evidence plus a lawyer sample |
| Registry share | fetched and cited docs with `registry_entry_id` not null |
| Substitution rate after a block | fetches to non-registry URLs after a `block` event for the intended source, divided by blocks |
| Session-recovery success | `session` events `expired` followed by `restored/refreshed` or a resolved handoff, divided by expired |
| Coverage under rate limits | share of requested documents ending `fetch status ok` |
| Abstention calibration | reliability curve and ECE over `unknown` outcomes |
| Cost | steps, searches, blocks, re-searches, wall time per dossier |
| Operator metrics | task success on the site-simulator suite, blocked actions executed (target 0), over-blocking rate, download hash match |
| Draft metrics | lawyer acceptance rate per edit (from `edit_decided`), unintended-change rate (`out_of_scope_changes`), defined-term and cross-reference break rate, authority-support rate for rationales |
Confidence intervals: percentile bootstrap, 10,000 resamples, seed fixed (`bootstrap.py`), per-error-type breakdown tables. Reports: `results/report.md` and `results/metrics.json`.

### 15.5 Abstention calibrator
`calibrator.py` trains a logistic combiner plus isotonic regression (scikit-learn) with features `judge_score`, `gnn_calibrated`, `quote_exact`, `crosschecked` against the label "claim is correct per gold/lawyer", on a validation split, and writes `abstention_calibrator.json` in exactly the format of Doc 00 section 11.1 (`feature_order`, `defaults`, `weights`, `bias`, `isotonic.x`, `isotonic.y`). Track A evaluates it in pure Python. Include a stand-alone reference `predict()` in `gba_contracts` and a test that it reproduces scikit-learn's output on a sample.

## 16. Test plan

| Area | Tests |
|---|---|
| gba_contracts | 100% branch for `predicates.py` and `scope.py`; canon golden tests; blob conflict; llm record/replay and miss; trajectory chain tamper detection; registry validation negatives; mocks pass contract tests |
| Fixtures | all load and validate; expected files agree with the `gba_contracts` scope/predicate evaluators |
| Workbench ingest | docx/pdf/html fixtures; OCR path on a rasterised PDF; clause paths and paragraph ids stable across runs |
| Structure | defined terms and cross-refs on S9 draft; unresolved references reported |
| Playbook | schema validation; applies_to unknown yields `rules_not_applicable` |
| Proposer and validator | each of the six validator rules has a failing case; model output with an out-of-path paragraph is dropped |
| Consistency | each check has pass and fail fixtures; numbering renumber case |
| Render | XML diff outside edits empty; redline accept-all equals accepted; opens with python-docx |
| S9 end to end | against mock_agent and mock_graph (and later real A and B): at least one insert edit in the conditions article, rationale cites 144b, authority claim passes, consistency checks green, zero out-of-scope changes |
| Eval | each metric function on fixtures; bootstrap deterministic; calibrator output loadable by a stand-alone loader |
| Fault proxy | rules fire; S8 403 returned for the EDGAR fixture URL; logs complete |
| Coverage | 80% line overall; 100% branch for `predicates.py`, `scope.py`, `edits/validator.py` |

### 16.1 Acceptance criteria for Track C
1. `gba_contracts` v1.0.0 tagged; A and B build against it unchanged.
2. Mocks pass all contract tests; A and B can run their scenario suites with only the mocks.
3. Workbench contract tests pass; S9 passes against mocks.
4. Redline opens in Word-compatible readers; accept-all equals accepted copy.
5. Eval harness runs the ladder on at least three fixture matters in replay mode and writes the report.
6. Fault proxy reproduces S8 deterministically.
7. mypy strict on `gba_contracts` and `gba_workbench.edits`; ruff clean.

## 17. Fault-injection proxy (C-9)

`gba_faultproxy` is an HTTP(S) forward proxy built on `mitmproxy` (pinned) with an addon, listening on **8199**; control API (FastAPI) on **8198** with bearer auth. It exists only for tests and evaluation.

Rule schema (`POST /rules`, list):
```json
{ "rule_id": "...", "match": {"url_regex": "...", "method": "GET|*"},
  "action": {"type": "status|delay|body|drop|expire_session", "status": 403, "headers": {}, "body": "...", "delay_ms": 0, "retry_after": 5},
  "times": null, "enabled": true }
```
`GET /rules`, `DELETE /rules/{id}`, `POST /reset`, `GET /log` (ndjson of requests with matched rule), `GET /stats` (request counts per host). Without a matching rule, traffic passes through unchanged. Scenario helpers: `s8_edgar_403()` (403 with body containing "undeclared automated tool" for the EDGAR URL), `silent_content_change()`, `rate_limit(retry_after)`, `captcha_page()`.
HTTPS: the proxy generates a local CA at `./.fp_ca/mitmproxy-ca-cert.pem`; clients honour env `GBA_CA_BUNDLE` (Track A's PoliteClient and browser context must trust it when `RunConfig.fault_proxy_url` is set: logged as a Doc 04 integration item). Request log is what the evaluation harness uses to prove "zero requests to non-registry hosts" (`GET /stats`).

## 18. Interfaces exposed (summary for A and B)
Package `gba_contracts` (models, predicates, scope, canon, registry, blobstore, llm, trajectory, http helpers, fixtures, mocks, contract tests); Workbench API of Doc 00 section 9.3 on port 8102 prefix `/wb/v1`; fault proxy on 8199 (control 8198); `gba-eval` reports; `abstention_calibrator.json`; trajectory file `TRAJ_DIR/<run_id>/workbench-api.jsonl`.

## 19. Build order and timeline

| Order | Work | Depends on | Done when |
|---|---|---|---|
| 1 | C-0 models, predicates, ids, hashing, canon | Doc 00 | tests green, `v1.0.0-rc1` tag (day 3) |
| 2 | C-0 rest (registry, blobstore, llm, trajectory, http) | 1 | tests green |
| 3 | C-1 fixtures, C-2 mocks, contract tests | 2 | mocks pass; announce to A and B |
| 4 | `v1.0.0` tag | M0 | Doc 04 milestone M0 |
| 5 | C-3 Workbench ingest, structure | 3 | S9 draft parsed |
| 6 | C-4 and C-5 classify, playbook, grounding | 5 | rule fires on S9 |
| 7 | C-6 proposer, validator, consistency | 6 | S9 passes against mocks |
| 8 | C-7 render | 7 | redline/accepted/summary ok |
| 9 | C-9 fault proxy | 3 | S8 reproducible |
| 10 | C-8 eval harness | 3, 9 | replay ladder report |
| 11 | C-10 integration | A, B | Doc 04 e2e green |
