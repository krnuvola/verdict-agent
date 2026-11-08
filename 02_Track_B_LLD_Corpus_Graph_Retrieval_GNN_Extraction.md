# 02 · Track B LLD: Corpus, Domain Graph, Retrieval, GNN, Fact Extraction

**Owner:** Person B · **Version:** 1.0.0 (pairs with Doc 00 v1.0.0) · **Repo:** `track-b-graph` · **Service:** `graph-api` on port 8101 · **DB:** Postgres 16 + pgvector `graphdb` on 5434

Read Doc 00 first and keep it open. This document never redefines a schema, enum, endpoint, fact key or reason code from Doc 00 or Doc 01. It tells you how to build Track B so it works alone on your device and then plugs into A and C. Rules R1 to R10 of Doc 00 apply in full.

---

## 1. Mission and boundaries

Track B builds the knowledge side: the verified-source corpus, the point-in-time domain graph with scope frames, retrieval, the deterministic standard-of-review engine, the GNN plausibility service and fact extraction.

### 1.1 Deliverables

| ID | Deliverable | Section |
|---|---|---|
| B-0 | Registry content (`registry.yaml`), reviewed by A | 6 |
| B-1 | Repo skeleton, config, DB migrations, API shell, trajectory wiring, test client simulating A and C | 2 to 5 |
| B-2 | Bulk ingestion of registry sources into blob store and DB | 7 |
| B-3 | Canonical text, chunking, hybrid index | 8 |
| B-4 | Domain graph, seed loader, point-in-time queries | 9 |
| B-5 | Standard-of-review engine | 10 |
| B-6 | Retrieval (`/search/documents`, `/query/rule_candidates`) | 11 |
| B-7 | Fact extraction service (async jobs) | 12 |
| B-8 | GNN: dataset, training, baselines, calibration, serving | 13 |
| B-9 | Scope-frame proposal from text (stretch, not on critical path) | 9.6 |
| B-10 | Graph accuracy audit tooling and scenario tests S3, graph parts of S1 to S5 | 15 |

### 1.2 Consume and expose

| Direction | Counterpart | What |
|---|---|---|
| Consume | shared infra | blob store, `registry.yaml`, trajectory dir, `gba_contracts` |
| Consume | LLM provider | through `LLMClient` only, role `extractor` (and optionally `judge` for frame proposals) |
| Expose | Track A | Graph API (Doc 00 section 9.1), all of it |
| Expose | Track C | same API (graph lookups for Workbench grounding when no dossier), `GET /registry`, the real seed graph |
| Expose | eval | `/gnn/*` for probes, graph accuracy report |

B does **not** call A or C over HTTP. B never calls the Guard API.

### 1.3 Out of scope for B
Browsing, sessions, guards, claim verification, Document Workbench, evaluation metrics, fault proxy.

---

## 2. Repository layout

```
track-b-graph/
  pyproject.toml uv.lock README.md DECISIONS.md QUESTIONS.md
  config/registry.yaml            # canonical copy lives in integration repo; B authors it (B-0)
  config/fact_prompts.yaml        # extraction prompt per fact key (section 12)
  seed/                           # hand-written graph (section 9.3)
    nodes/*.yaml  edges/*.yaml  frames/*.yaml
  models/                         # trained GNN artefacts, one dir per model_version (gitignored except baseline)
  src/gba_graph/
    config.py                     # pydantic-settings
    api/ main.py routes_graph.py routes_docs.py routes_extract.py routes_gnn.py
         deps.py errors.py auth.py
    db/ models.py session.py repos.py migrations/ (alembic)
    ingest/ bulk.py sources/{edgar.py,delcode.py,legis.py,courts.py,courtlistener.py}
            register.py linker.py
    index/ chunker.py embedder.py bm25.py hybrid.py reranker.py
    graph/ store.py seed_loader.py pit.py validity.py scope.py standard.py
           frame_proposer.py
    extract/ jobs.py worker.py prompts.py resolver.py merge.py
    gnn/ dataset.py features.py model_rgcn.py model_homog.py model_text.py
         train.py calibrate.py serve.py eval.py
    cli.py                        # gba-graph: serve, migrate, seed, ingest, train, audit
  tests/ unit/ integration/ scenarios/ fixtures/
  docker/ Dockerfile compose.dev.yml   # postgres+pgvector, minio
```

Rule: `graph/` modules are pure (no HTTP, no LLM). `extract/` is the only module that calls an LLM.

## 3. Configuration (B-specific; common variables in Doc 00 section 4)

| Variable | Meaning | Default |
|---|---|---|
| `DATABASE_URL` | `postgresql+psycopg://user:pass@localhost:5434/graphdb` | required |
| `EMBED_MODEL` | sentence-transformers model id | `BAAI/bge-small-en-v1.5` (dim 384; verify availability) |
| `EMBED_DIM` | must match model | 384 |
| `RERANK_MODEL` | cross-encoder id, empty = off | empty |
| `GNN_MODEL_DIR` | directory with trained models | `./models` |
| `GNN_ACTIVE_MODEL` | model_version to serve | `baseline-text-v0` |
| `GNN_FLAG_THRESHOLD` | flag when calibrated < threshold | 0.3 |
| `INGEST_USER_AGENT` | falls back to `AGENT_USER_AGENT` | required |
| `EXTRACT_CHUNK_CHARS` | chunk size for extraction | 6000 |
| `EXTRACT_MAX_DOCS_PER_JOB` | cap | 10 |
| `COURTLISTENER_TOKEN` | for bulk/API ingestion | required for that source |

Startup checks (fail fast): `SERVICE_TOKEN` set; registry loads and validates; DB reachable and migrated; pgvector extension present; blob store reachable; active GNN model loads (baseline always available).

## 4. Database (graphdb, Alembic migration 0001)

```sql
CREATE EXTENSION IF NOT EXISTS vector;

CREATE TABLE documents (
  doc_id text PRIMARY KEY,                 -- sha256:<hex> of raw bytes
  source_id text NOT NULL,                 -- src_<ULID>
  registry_entry_id text,
  url text NOT NULL, media_type text NOT NULL, title text, doc_type text,
  doc_date date,                           -- decision/enactment/filing/publication date = known_from basis
  version_label text, canon_version text NOT NULL, char_length int NOT NULL,
  page_markers jsonb NOT NULL DEFAULT '[]', source jsonb NOT NULL,
  registered_at timestamptz NOT NULL DEFAULT now());
CREATE INDEX documents_entry ON documents(registry_entry_id);
CREATE INDEX documents_date ON documents(doc_date);

CREATE TABLE chunks (
  chunk_id bigserial PRIMARY KEY, doc_id text NOT NULL REFERENCES documents(doc_id),
  ordinal int NOT NULL, start int NOT NULL, "end" int NOT NULL,
  text text NOT NULL, tsv tsvector NOT NULL, embedding vector(384),
  UNIQUE (doc_id, ordinal));
CREATE INDEX chunks_tsv ON chunks USING gin(tsv);
CREATE INDEX chunks_emb ON chunks USING hnsw (embedding vector_cosine_ops);

CREATE TABLE nodes (
  node_id text PRIMARY KEY, node_type text NOT NULL, label text NOT NULL,
  props jsonb NOT NULL DEFAULT '{}', scope_frame jsonb,
  valid_from date, valid_to date, known_from date NOT NULL,
  source_ids jsonb NOT NULL DEFAULT '[]', origin text NOT NULL,
  audited boolean NOT NULL DEFAULT false, node jsonb NOT NULL,
  updated_at timestamptz NOT NULL DEFAULT now());
CREATE INDEX nodes_type ON nodes(node_type);
CREATE INDEX nodes_known ON nodes(known_from);

CREATE TABLE edges (
  edge_id text PRIMARY KEY, src text NOT NULL, dst text NOT NULL,
  edge_type text NOT NULL, valid_from date, valid_to date, known_from date NOT NULL,
  origin text NOT NULL, edge jsonb NOT NULL,
  UNIQUE (src, dst, edge_type));
CREATE INDEX edges_src ON edges(src, edge_type);
CREATE INDEX edges_dst ON edges(dst, edge_type);

CREATE TABLE node_documents (            -- which documents back which node
  node_id text REFERENCES nodes(node_id), doc_id text REFERENCES documents(doc_id),
  PRIMARY KEY (node_id, doc_id));

CREATE TABLE extraction_jobs (
  job_id text PRIMARY KEY, request jsonb NOT NULL,
  status text NOT NULL CHECK (status IN ('queued','running','done','failed')),
  result jsonb, error text,
  created_at timestamptz DEFAULT now(), updated_at timestamptz DEFAULT now());

CREATE TABLE gnn_models (
  model_version text PRIMARY KEY, kind text NOT NULL, trained_on_cutoff date,
  metrics jsonb, path text NOT NULL, created_at timestamptz DEFAULT now());

CREATE TABLE ingest_runs (
  id bigserial PRIMARY KEY, entry_id text, started_at timestamptz DEFAULT now(),
  finished_at timestamptz, fetched int DEFAULT 0, skipped int DEFAULT 0, failed int DEFAULT 0, log jsonb);

CREATE TABLE idempotency (
  key text PRIMARY KEY, request_hash text NOT NULL, response jsonb NOT NULL,
  created_at timestamptz NOT NULL DEFAULT now());
```

The `node` and `edge` jsonb columns hold the full Doc 00 `Node` / `Edge` model (source of truth for serialisation); the scalar columns exist for indexing. Repositories expose typed methods; no raw SQL outside `db/`.

## 5. Testing without A and C (B-1)

B depends on no other service at runtime. Provide `tests/client_sim.py`, a thin `httpx` client that behaves like A: registers documents (writes `raw/<hex>` to the blob store, builds a `SourceRecord`, calls `POST /documents/register`), calls every route with correct headers, and validates responses with `gba_contracts` models (or `contracts_local/models.py` until published). A contract test validates every response of every route. Switch to the package by one import change.

---

## 6. Registry content (B-0)

Person B writes `config/registry.yaml` using the schema in Doc 00 section 8, person A reviews. **Every entry has `terms_verified: false`.** Proposed content (all URL prefixes and limits must be verified against the live sites and recorded with the date in `config/REGISTRY_NOTES.md`; R1 applies):

```yaml
version: 1
entries:
  - entry_id: sec_edgar
    name: SEC EDGAR
    role: primary
    authoritative: true
    base_domains: [www.sec.gov, data.sec.gov, efts.sec.gov]
    url_prefixes: ['https://www.sec.gov/Archives/', 'https://data.sec.gov/', 'https://efts.sec.gov/']
    doc_types: [merger_agreement, proxy_statement, 8k, exhibit, filing_index]
    access: api
    requires_login: false
    rate_limit: {requests_per_second: 2, burst: 4, max_concurrent: 2}
    robots_respect: true
    allowed_action_classes: [READ, SEARCH]
    terms_verified: false
    phase: MVP
  - entry_id: de_code_online
    name: Delaware Code Online
    role: primary
    authoritative: true
    base_domains: [delcode.delaware.gov]
    url_prefixes: ['https://delcode.delaware.gov/']
    doc_types: [statute]
    access: adapter
    requires_login: false
    rate_limit: {requests_per_second: 1, burst: 2, max_concurrent: 1}
    robots_respect: true
    allowed_action_classes: [READ, SEARCH]
    terms_verified: false
    phase: MVP
  - entry_id: de_legislature
    name: Delaware General Assembly
    role: primary
    authoritative: true
    base_domains: [legis.delaware.gov]
    url_prefixes: ['https://legis.delaware.gov/']
    doc_types: [bill, session_law]
    access: adapter
    requires_login: false
    rate_limit: {requests_per_second: 1, burst: 2, max_concurrent: 1}
    robots_respect: true
    allowed_action_classes: [READ, SEARCH]
    terms_verified: false
    phase: MVP
  - entry_id: de_courts
    name: Delaware Courts (opinions)
    role: primary
    authoritative: true
    base_domains: [courts.delaware.gov]
    url_prefixes: ['https://courts.delaware.gov/']
    doc_types: [opinion]
    access: adapter
    requires_login: false
    rate_limit: {requests_per_second: 1, burst: 2, max_concurrent: 1}
    robots_respect: true
    allowed_action_classes: [READ, SEARCH]
    terms_verified: false
    phase: MVP
  - entry_id: courtlistener
    name: CourtListener (Free Law Project)
    role: crosscheck
    authoritative: false
    base_domains: [www.courtlistener.com, storage.courtlistener.com]
    url_prefixes: ['https://www.courtlistener.com/', 'https://storage.courtlistener.com/']
    doc_types: [opinion, citation_lookup]
    access: api
    requires_login: false
    rate_limit: {requests_per_second: 1, burst: 2, max_concurrent: 1}
    robots_respect: true
    allowed_action_classes: [READ, SEARCH]
    terms_verified: false
    phase: MVP
```

Also add commented-out stub entries (phase `P6` or `optional`, all `terms_verified: false`) for PACER/RECAP and licensed services so the registry schema is exercised; stubs have `allowed_action_classes: [READ]` and are never fetched by anyone in v1. Add a CI test: no entry has `terms_verified: true` unless `terms_url` and `terms_checked_on` are both set.

Note: the `sitesim` entry for the A-side simulator is added by A in tests only, never in the canonical file.

---

## 7. Bulk ingestion (B-2)

B may populate the corpus offline from registry sources without going through A. It must still obey the registry: same URL-prefix, rate-limit, robots and terms rules as A.

```python
class BulkIngestor:
    def __init__(self, registry, blob: BlobStore, db, http: PoliteHttp): ...
    async def ingest_url(self, entry_id: str, url: str, expected_doc_type: str, version_label: str | None) -> IngestResult
    async def ingest_entry(self, entry_id: str, plan: IngestPlan) -> IngestRun
```

Algorithm per URL (deterministic):
1. URL must match a `url_prefixes` of the named entry; else raise `IngestRefused('URL_NOT_IN_REGISTRY')`. Never fetch otherwise.
2. If `GBA_ENV=prod` and `terms_verified` is false: refuse `TERMS_UNVERIFIED`.
3. robots.txt (cached 24 h) and a token-bucket per entry from the registry values. Honest `User-Agent` = `INGEST_USER_AGENT`. No evasion (R7).
4. Fetch with timeout 30 s. On 403, 429, captcha or login page: log, back off, mark `failed` in the run log. B **never** switches to a non-registry source. Skip silently-changed pages (HTML under 20 KB where a PDF was expected).
5. SHA-256 of raw bytes is `doc_id`. Put `raw/<hex>` (idempotent; same bytes fine, different bytes raises `BlobConflict`).
6. Build `SourceRecord` with `fetch_method='bulk'` (or `api`), `retrieved_by='track_b.bulk.<entry_id>'`, `session_id=None`. Write `source/<source_id>.json`.
7. Call the internal `register_document()` (same code path as `POST /documents/register`).

Source plans (`ingest/sources/*.py`): EDGAR (company submissions JSON by CIK, filings for the testbed deals list `config/deals.yaml`, document URLs under `/Archives/`), Delaware Code (DGCL Title 8 sections 144, 141, 203, 251 and others in `config/statutes.yaml`; version label `retrieved <date>`), Legislature (SB 21 and amendments bill text), Delaware courts and CourtListener (opinions in `config/opinions.yaml`: a list of citations/URLs the lawyer reviewer approves). All URL patterns and field names must be verified against the live sites at build time and noted with the date in `ingest/README.md`. Fixtures for tests use recorded cassettes (`respx`/`vcrpy`); `--live` smoke tests are marked `@pytest.mark.live` and not run in CI.

Plan files (`deals.yaml`, `statutes.yaml`, `opinions.yaml`) are lists of explicit URLs plus metadata. No crawling beyond them.

## 8. Canonical text and index (B-3)

`register_document(source: SourceRecord) -> DocMeta`:
1. Verify blob `raw/<hex>` exists and its SHA-256 equals `doc_id` (else 422 `VALIDATION_ERROR`, code detail `HASH_MISMATCH`). Verify `byte_length`.
2. Canonicalise with `gba_contracts.canon.to_canonical_text` only (never reimplement). Write `canon/<hex>.v1.txt` and `.pages.json` if absent (another service may have written them; if present, verify equal bytes, else `BlobConflict` is a hard error).
3. Derive `title`, `doc_type`, `doc_date` (see 8.2). Insert/update `documents` (idempotent on `doc_id`; the same doc with a second `source_id` keeps the first and appends the second to `source.alt_source_ids`).
4. Chunk and index (8.1). Link to graph nodes (9.5).
5. Return `DocMeta` (201), including `node_ids` (graph nodes created or linked in step 4; Doc 00 section 9.1).

### 8.1 Chunking and indexing
- Chunk on paragraph boundaries (`\n\n`) with target 800 characters, hard max 1500, overlap of one paragraph. Offsets are over canonical text so a chunk hit maps to a valid `Span`.
- BM25 via Postgres `tsvector` (`to_tsvector('english', text)`, rank with `ts_rank_cd`). Dense via `EMBED_MODEL` (L2-normalised, cosine).
- Hybrid: Reciprocal Rank Fusion (k = 60) of top 50 BM25 and top 50 dense. Optional cross-encoder rerank of the top 30 when `RERANK_MODEL` is set.
- Statute numbers and party names must hit exactly: if the query contains a token matching `\b\d+\s*(Del\.|DGCL|§)` or a quoted phrase, add a phrase-match boost through `phraseto_tsquery`.

### 8.2 Metadata derivation
`doc_type`: from `expected_doc_type`; else by registry entry default. `doc_date`: EDGAR from the filing index date (`filing date`, not period of report), opinions from the decision date in the header, statutes from `version_label` effective date if parseable else retrieval date, bills from the enactment date when present. If a date cannot be established, `doc_date = null`, and in strict leakage mode the document is hidden from point-in-time search (log a warning). `title` first non-empty line up to 200 characters.

---

## 9. Domain graph (B-4)

### 9.1 IDs and slugs
Node id `<NodeType>:<slug>`, slug `[a-z0-9][a-z0-9._@-]*`. Slug rule: lowercase, replace spaces and `/` with `-`, drop other characters, keep `.`, `@`, `_`. Provisions versioned `Provision:de.dgcl.144b@2025-03-25` (version date = effective date). Cases `Case:<caption-slug>-<year>`. Edge id: `edge_` + first 16 hex of SHA-256 of the UTF-8 string `src|dst|edge_type` (Doc 00 section 3.1).

### 9.2 Node and edge conventions (normative in Doc 00 section 6.1; restated here)
- `Case.props`: `decided_on`, `court_id`, `citation_string`, `docket`, `case_name`.
- `Provision.props`: `code`, `section`, `version_label`, `text_doc_id` (optional), `safe_harbor_when` (predicate, only on provisions that create a safe harbor).
- `Holding.props`: `case_node_id`, `proposition`, `bjr_when` (predicate under which the holding yields business-judgment review).
- `Element.props`: `fact_keys` (list of Doc 00 section 5.2 keys), `met_when` (predicate), `unmet_when` (predicate, optional; if absent, `unmet_when = {not: met_when}` restricted to when all fact_keys are known).
- `ProcessFact:<fact_key>` nodes exist for each fact key used by an Element, linked `ProcessFact --satisfies--> Element`.
- `Standard.props`: `value` (a `StandardValue`).
All predicates are validated with `gba_contracts.predicates.validate(pred, FACT_KEYS)` at load time; the loader fails on any error.

### 9.3 Seed graph (hand-written, lawyer-reviewed)
`seed/` is YAML that loads into `nodes` and `edges`. The seed must be a **superset of `gba_contracts/fixtures/seed_graph.json`**: every node id and edge in the fixture exists with identical `node_id`, `node_type`, dates and frames (the fixture file is the authority for those dates; the legal text in it is illustrative and must be confirmed by the lawyer reviewer). Required content beyond the fixture:
- Provisions: DGCL 144 (a) pre-amendment version and post-SB 21 versions for (b) and (c) (ids per fixture), each with `next_version` edges and a `ScopeFrame` as described in Doc 00 section 13.
- Holdings: `Holding:mfw-2014`, `Holding:match-group-2024` plus any further holdings the reviewer adds (each with `case_node_id`, `bjr_when`, a frame).
- Elements: committee-approval, mom-approval, committee-independence, committee-power-to-say-no, committee-power-to-negotiate, committee-free-to-select-advisors, committee-duty-of-care, committee-ab-initio, mom-informed, mom-uncoerced, disinterested-stockholder-approval, each with `fact_keys` and `met_when`.
- Standards: business-judgment, entire-fairness.
- Courts and Source nodes for each registry entry (`Source:sec_edgar`, ...) with `published_by` edges.
Seed nodes have `origin='seed'`, `audited=false` until the reviewer signs off (then `true`).

Seed predicate definitions (normative for this repo; lawyer to confirm):
- Committee prong `P_comm` = all of: `committee_all_disinterested`, `committee_power_to_say_no`, `committee_power_to_negotiate`, `committee_approval_obtained` each `eq true` (and, for the pre-SB21 MFW holding only, also `committee_formed_before_substantive_negotiation`, `committee_free_to_select_advisors`, `committee_duty_of_care_met`).
- Stockholder prong `P_stock` = any of `disinterested_stockholder_approval_obtained eq true`, `mom_approval_obtained eq true`. (Doc 00 scenario S4 requires `mom_approval_obtained` to appear in `missing_facts`; recording both facts keeps that true. Log in DECISIONS.md and put the question to the lawyer.)
- `Provision:de.dgcl.144b@2025-03-25.safe_harbor_when` = any(`P_comm`, `P_stock`).
- `Provision:de.dgcl.144c@2025-03-25.safe_harbor_when` = all(`P_comm`, `P_stock`).
- `Holding:mfw-2014.bjr_when` = all(`P_comm_mfw`, `mom_condition_in_agreement eq true`, `mom_vote_informed eq true`, `mom_vote_uncoerced eq true`, `mom_approval_obtained eq true`).

### 9.4 Point-in-time rules (`graph/pit.py`)
Let `mode = AS_OF_LEAKAGE_MODE` (default `strict`).
- Node visible iff `mode == off` or `known_from <= as_of`. Hidden node: `GET /nodes/{id}` returns 404 `NOT_KNOWN_AS_OF`; an id that never existed returns 404 `NODE_NOT_FOUND`.
- Edge visible iff `known_from <= as_of` and both endpoints visible.
- `as_of` query param is optional on `GET /nodes/{id}`; absent means today (UTC) (Doc 00 section 9.1).
- `valid_from`/`valid_to` are never used to hide a node. They only feed `in_force`. This is deliberate: guards need to see out-of-force rules to block them with a reason.

### 9.5 Validity (`graph/validity.py`) for `POST /query/validity`
1. `in_force`: if `valid_from` is null treat start as open; `valid_to` null open-ended. `true` if `valid_from <= as_of` and (`valid_to` null or `as_of < valid_to`) else `false`. Both null: `true`. (`unknown` only if the node record is flagged `props.validity_unknown=true`.)
2. `treatments`: all visible incoming edges to `node_id` with `edge_type` in `overrules`, `superseded_by_statute`, `limits`, where `effective_date <= as_of`. `effective_date = edge.valid_from` else `edge.known_from`. `by_node_id = edge.src`, which must itself be visible. Conditions are **not** evaluated here.
3. `good_law_unconditional`: priority `overruled` > `superseded` > `limited` > `good`; `unknown` if `in_force` is `unknown`. Reasons list one line per treatment and per in-force finding.
4. Edges with a condition are included regardless (A evaluates them with the shared evaluator).

### 9.6 Document-to-node linking (`ingest/linker.py`)
On registration, create or link nodes:
- Opinion (entry `de_courts` or `courtlistener`): normalise case caption, decided date and citation from the extraction of the document header (regex first, `extractor` LLM only if regex fails). Match an existing `Case` by (normalised case name, decided date) or citation. If none exists create `Case:<slug>` with `origin='registry'`, `audited=false`, `known_from = decided_on`, `props` minimal, no scope frame. Link in `node_documents` and add `src_...` to `source_ids`.
- Statute version (`de_code_online`, `de_legislature`): match `Provision` by `props.section` + `version_label`/effective date; create if absent.
- Filings: no graph node (facts come via extraction).
The linked ids are returned in `DocMeta.node_ids` (Doc 00 section 9.1) and are what A's Source Gate stores.
Stretch B-9: `frame_proposer.py` asks the `extractor` role for a draft `ScopeFrame` with `source_spans` for new Provision/Holding nodes. Result stored with `audited=false`, `origin='llm_extracted'`; never used in evaluation until audited.

---

## 10. Standard-of-review engine (B-5)

Deterministic, no LLM, uses `gba_contracts.predicates` and `gba_contracts.scope.applicability`. Endpoint: `POST /query/standard_of_review` (`StandardReq` → `StandardResp`).

Algorithm:
1. Load visible nodes `Provision` and `Holding` whose frames are in force at `as_of` (`valid_from <= as_of < valid_to`). Treat each as a *regime rule*. Statutory regime: Provisions with `safe_harbor_when`. Common-law regime: Holdings with `bjr_when`. Exclude Holdings that are validly treated away: for each applicable-true incoming `superseded_by_statute`/`overrules` edge whose condition evaluates `true`, the holding is *displaced*.
2. For each rule compute `(tri, missing, failed_clause) = applicability(frame, facts, as_of)`.
3. **Statutory path:** if any statutory Provision has `tri == true`: `h = evaluate(safe_harbor_when)`; `safe_harbor = to_tri(h)`; `value = business_judgment` if true; `entire_fairness` if false; `unknown` if None. `basis = [provision ids used]`.
4. **Common-law path:** if no statutory Provision is applicable-true, and every statutory provision is `false` (non-application, not in force, etc.) or none is `true`/`unknown`, use non-displaced Holdings that are `true`: `h = evaluate(bjr_when)`. `safe_harbor = 'unknown'` if none was relevant, else `to_tri(h)`. Value as above.
5. **Unknown path** (`readings` shape fixed in Doc 00 section 6.2): if any candidate rule has `tri == None` (and no applicable-true rule resolves it), `value = 'unknown'`, `safe_harbor = 'unknown'`, `basis` = ids of rules examined, and `readings` = one entry per plausible regime: `{regime: <rule id>, applies_if: <missing facts>, requires: <fact keys needed to evaluate safe_harbor_when/bjr_when>, would_yield: ...}`. For S5 both statutory readings and the MFW reading appear. For S2 the 144b reading is excluded (non-application) and a single `mfw` reading lists the MFW fact keys.
6. `conditions`: for each Element reachable through `addresses` or `satisfies` from any regime rule used in steps 3-5 (plus, always, the elements of regime rules with `tri != false`), compute status: `met` if `evaluate(met_when) is True`; `unmet` if `evaluate(unmet_when)` is True; else `unknown`. `fact_keys` from props; `spans` are left empty (A fills them from matter facts); `reason` is a template sentence.
7. Scenario expectations to unit-test: S1 `safe_harbor=true`, basis `[Provision:de.dgcl.144b@2025-03-25]`; S2 `value=unknown` with MFW reading; S4 `safe_harbor=unknown`, `mom_approval_obtained` in some reading's `requires` (and 144c basis); S5 both readings, `transaction_type` listed as missing.

---

## 11. Retrieval (B-6)

### `POST /search/documents`
Hybrid retrieve over `chunks`, group by `doc_id` (best chunk score), apply filters: `registry_entry_ids`, `doc_types`; leakage filter `doc_date <= as_of` (strict mode). Return `DocHit` with up to 3 `Span`s (chunk offsets, `quote` ≤ 500 chars equal to `canon[start:end]` truncated by shrinking `end`, never by cutting inside the quote). Registry corpus only by construction (documents are only inserted through registration).

### `POST /query/rule_candidates`
Index text for each rule node: `label + props.proposition/section text + linked document chunks`. Retrieve with the hybrid ranker over node text; `facts` only add a small score prior (e.g. +0.05 when `transaction_type` matches a frame's `transaction_types`), never filter. Visibility per section 9.4. **Drop any Provision or Holding that has no `scope_frame`** (log a warning event) so a bare passage is never returned. Return `RuleCandidate{node, score, spans}`.

### Other routes
`GET /nodes/{id}/edges` filters by `edge_type`, `direction`, visibility. `POST /query/scope_frame` returns `frame=null` if the node has none. `GET /documents/{doc_id}/text` returns `text/plain` canonical text from the blob store; `doc_id` in the path is URL-encoded (`sha256%3A...`). `GET /registry` returns the loaded registry as JSON. `GET /health`, `GET /openapi.json` per Doc 00.

---

## 12. Fact extraction (B-7)

Async: `POST /extract/facts` → 202 `{job_id}`; `GET /jobs/{job_id}`. Worker runs in-process (asyncio task queue backed by `extraction_jobs`; on restart `running` jobs become `failed` with `RESTART`). Max `EXTRACT_MAX_DOCS_PER_JOB` documents; more gives 422.

Per document and per requested fact key (default: all keys of Doc 00 section 5.2 except `controller_identity`, `deal_value_usd`, which are requested only if listed):
1. Load canonical text; chunk to `EXTRACT_CHUNK_CHARS` with paragraph boundaries; pre-filter chunks by BM25 against the key's `search_terms` from `config/fact_prompts.yaml`, keep top 4.
2. Call `LLMClient.complete(role='extractor', response_schema=EXTRACT_SCHEMA, temperature=0)` with the key description, the allowed value type, and the chunk wrapped in `<document_text>` markers (text inside is data). Output JSON: `{key, value, status: 'known'|'unknown', quote, confidence}`. The prompt instructs: answer only from the passage; if not stated, `unknown`; never infer.
3. **Span resolution (`resolver.py`)**: normalise as in the Claim Verifier V3 (NFC, whitespace collapse, curly to ASCII, no case folding) and find `quote` in the chunk's canonical text; compute canonical offsets; build `Span(doc_id, start, end, quote)`. If not found: discard the extraction (status `unknown`). Every `known` fact has at least one span, no exceptions.
4. **Validation**: bool facts only `true`/`false` when known; date facts ISO `YYYY-MM-DD`; enum facts in the Doc 00 enum; numbers parsed. A known bool with null value is never emitted.
5. `reported_date` = the document's `doc_date`. `extracted_by='llm_extracted'`.
6. **Merge across documents (`merge.py`)**: same key, same value: keep the highest confidence and union spans. Different values: prefer the later `reported_date` only for fields marked `time_varying` in `fact_prompts.yaml` (`closing_date`, `litigation_commenced_on`), otherwise `status='conflicting'`, `value=None`, spans from both. Keys with no known value are returned as `status='unknown'` only if requested explicitly.
7. Rules: `litigation_commenced_on` is never emitted as known-null from absence of text; known-null only if the text states that no action was filed. Dates in proxy statements are taken from the *event* described, not the document date.
8. Cache by `request_hash` through the LLM replay cache. Emit `llm_call` trajectory events.
Return `JobStatus.result = list[MatterFact]`.
Evaluation hook: `gba-graph extract-eval --gold gold/facts/*.json` computes per-key precision/recall and span-exactness (Track C consumes the numbers).

---

## 13. GNN service (B-8)

### 13.1 Dataset (`gnn/dataset.py`)
Build a heterogeneous graph from visible nodes/edges. Node features: type one-hot, text embedding of `label + proposition/text` (frozen `EMBED_MODEL`), year of `known_from` (scaled). Edge types trained: `cites`, `interprets`, `follows`, `limits`, `addresses`. **Temporal split**: edges with `known_from < T` train; `T <= known_from < T2` validation (calibration); `>= T2` test. T, T2 from config. Negative sampling: for each positive, 5 negatives by corrupting `dst` with a same-type node, excluding true edges; also type-consistent hard negatives (an authority linked to a different Element).

### 13.2 Models (`gnn/model_*.py`)
- `rgcn-v1`: PyTorch Geometric `RGCNConv` (2 layers, hidden 128), DistMult-style decoder per edge type.
- `hgt-v1` (optional): `HGTConv`.
- `homog-caselink-v1`: homogeneous GCN comparison (edge types collapsed).
- `text-only-v1`: logistic regression / MLP on concatenated frozen embeddings of (src, dst, edge_type), no graph.
- `baseline-text-v0`: the always-available fallback (cosine similarity of text embeddings passed through a fixed logistic, no training required) so the service works before any training.

### 13.3 Calibration and threshold
Fit isotonic regression on the validation split: raw score → probability the edge is real. `score_calibrated` from it; `flag = score_calibrated < GNN_FLAG_THRESHOLD` (default 0.3). Report ECE.

### 13.4 Serving (`gnn/serve.py`)
- `POST /gnn/score_edges`: build the subgraph visible at `as_of` **excluding the probed edge itself** (no self-leakage); embed, score each `EdgeProbe`; return `ScoreEdgesResp(model_version, scores)`. Unknown node ids → 404 `NODE_NOT_FOUND` for the whole request. Timeout budget 120 s per Doc 00.
- `POST /gnn/predict_missing`: for each `anchor_node_id` (Element/Standard), score candidate `Case`/`Holding` nodes visible at `as_of`, not in `exclude_node_ids`, for edge type `addresses`; aggregate max over anchors; return top_k `MissingPrediction` with `anchor_links`.
- Model artefacts in `models/<model_version>/{model.pt|model.joblib, calibrator.json, meta.json}`. `gnn_models` table records metrics. `GNN_ACTIVE_MODEL` selects the served one; `meta.json` has `trained_on_cutoff`.
- Advisory only: no other route uses GNN scores.

### 13.4 Evaluation (`gnn/eval.py`)
Link prediction on the temporal test split: AUC, average precision, Hits@10, MRR; stratified by edge type; compare all models above. Also "authority recall" on the silver set: for each opinion in test, fraction of the cited authorities recovered in top-k by `predict_missing`. Output `reports/gnn_eval.json` and a markdown table. A null or small effect is a valid result (hypothesis H3); do not tune against the test split.

---

## 14. HTTP layer
FastAPI app `gba_graph.api.main:app`, port 8101, implementing Doc 00 sections 3.4 and 9.1 exactly (bearer auth except `/health`, tracing headers propagated, `ErrorEnvelope`, idempotency on `POST /documents/register` and `POST /extract/facts`). Error codes used: `NODE_NOT_FOUND`, `NOT_KNOWN_AS_OF`, `VALIDATION_ERROR`, `JOB_NOT_FOUND`, `UPSTREAM_UNAVAILABLE`, `CONTRACT_VERSION_MISMATCH`, `DOC_NOT_FOUND`. Every request writes a `graph_query` event with `endpoint`, `request_hash`, `status`, `ms` via `TrajectoryWriter('graph-api', run_id)` (`run_id` from `X-Run-Id`, else `adhoc`). Component name in events is `graph`.

## 15. Test plan

| Area | Tests |
|---|---|
| Contracts | every route response validated by `gba_contracts` models; schemathesis against `/openapi.json` |
| Canon | register uses `to_canonical_text`; span `quote == canon[start:end]` property test over random chunks |
| Point in time | S3 exactly (404 `NOT_KNOWN_AS_OF` for 144b at 2024-12-01; Match Group visible); edges hidden; `off` mode shows all; hypothesis: no node with `known_from > as_of` ever appears in any response |
| Validity | table tests for in_force, treatment priority, effective date, conditions not evaluated |
| Frames | `rule_candidates` never returns a node without a frame; seed predicates all pass `validate` |
| Standard of review | S1, S2, S4, S5 expected outputs; monotonicity: adding a known true fact never changes `safe_harbor` from true to false |
| Registry | no `terms_verified: true` without terms fields; prefix test; ingestor refuses non-registry URLs (request-recording transport shows zero such requests) |
| Ingestion | cassette tests per source; block (403, 429, captcha page) handled without fallback outside registry |
| Extraction | span exists in canonical text for every known fact; invented quote is discarded; conflicting merge |
| GNN | temporal split has no leakage (no test edge in training); calibrator monotone; probe edge excluded from message passing; baseline serves with no training |
| Coverage | at least 80% line overall; 100% branch for `graph/pit.py`, `graph/validity.py`, `graph/standard.py` |

### 15.1 Acceptance criteria for Track B (all before integration)
1. All Graph API routes pass the shared contract tests and schemathesis.
2. Seed graph is a superset of `seed_graph.json` and all predicates validate.
3. Scenario unit tests for S1, S2, S3, S4, S5 graph-side expectations pass.
4. `GET /nodes/...` and `/edges` never return anything with `known_from > as_of` in strict mode.
5. Extraction returns spans for every known fact; graph audit report exists (sampled accuracy of nodes/edges/frames, minimum 50 items).
6. `GNN_ACTIVE_MODEL=baseline-text-v0` works with no training; trained models pass the leakage test.
7. mypy clean on `graph/` and `api/`; ruff clean; coverage thresholds met.

## 16. Build order and timeline

| Order | Work | Depends on | Done when |
|---|---|---|---|
| 1 | B-1 skeleton, DB, config, client_sim | Doc 00 | health OK, contract tests green on stubs |
| 2 | B-0 registry | Doc 00 section 8 | reviewed by A, committed to integration repo |
| 3 | B-4 graph store, seed loader, PIT, validity | fixtures | S3 passes |
| 4 | B-5 standard engine | 3 | S1, S2, S4, S5 pass |
| 5 | B-3 canon/index, register | 1 | register + search work on fixture docs |
| 6 | B-6 retrieval | 5 | rule_candidates frames-only |
| 7 | B-2 bulk ingestion | 2, 5 | real corpus loaded under rate limits |
| 8 | B-7 extraction | 5 | extraction eval on gold sample |
| 9 | B-8 GNN | 3, 7 | model table and eval report |
| 10 | Integration | A, C | Doc 04 milestones |

## 17. Interfaces exposed (summary for A and C)
The full Graph API of Doc 00 section 9.1 on port 8101 with prefix `/graph/v1`; `GET /registry`; canonical text and page markers in the blob store (`canon/...`); the real seed graph (superset of the fixture ids); trajectory file `TRAJ_DIR/<run_id>/graph-api.jsonl`; `registry.yaml` content.
