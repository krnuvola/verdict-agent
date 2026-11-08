"""Source Gate contract and fail-closed admission tests."""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from hashlib import sha256
from pathlib import Path
from typing import Any

import pytest

from gba_agent.access._registry import Registry, load_registry
from gba_agent.api.errors import APIError
from gba_agent.api.trace import new_ulid
from gba_agent.clients.contract_validation import UpstreamContractError
from gba_agent.clients.courtlistener_client import CrosscheckResult
from gba_agent.clients.http import ServiceUnavailableError, UpstreamServiceError
from gba_agent.db.models import SourceRecord as SourceRow
from gba_agent.db.repositories import SourceRecordsRepository
from gba_agent.db.session import session_scope
from gba_agent.guards import reason_codes as rc
from gba_agent.guards.source_gate import Claim, Matter, SourceDetails, SourceGate, SourceRecord
from tests.sitesim.fetcher import RUN_ID, FetchFixture
from tests.sitesim.fetcher import site as site

REGISTRY = Path(__file__).resolve().parents[2] / "config" / "registry.yaml"
DAY = date(2026, 11, 7)
FILE = "https://www.sec.gov/Archives/edgar/data/1234/000000123426000001/report.htm"
OPINION = "https://courts.delaware.gov/opinions/download.aspx?id=42"
STATUTE = "https://delcode.delaware.gov/title8/c001/sc04/index.html"
LOOKUP = "https://www.courtlistener.com/opinion/42/synthetic/"
CASE = "Case:synthetic"
HOLDING = "Holding:synthetic"
PROVISION = "Provision:synthetic"
RAW = b"<p>Synthetic legal document.</p>"
CANON = "Synthetic canonical text. " * 30


@dataclass(frozen=True)
class Canon:
    text: str = CANON
    markers: list[dict[str, Any]] = field(default_factory=lambda: [{"label": "1", "start": 0}])
    canon_version: int | str = 1


def node(
    node_id: str = CASE,
    *,
    source_ids: list[str] | None = None,
    props: dict[str, Any] | None = None,
    known_from: str = "2025-01-01",
) -> dict[str, Any]:
    return {
        "node_id": node_id,
        "node_type": node_id.split(":")[0],
        "known_from": known_from,
        "props": props or {},
        "source_ids": source_ids or [],
    }


def error(status: int, code: str) -> UpstreamServiceError:
    return UpstreamServiceError("graph-api", status, code, "Synthetic response")


@dataclass
class GraphDouble:
    kind: str = "8k"
    content: Canon = field(default_factory=Canon)
    nodes: dict[str, dict[str, Any] | Exception] = field(default_factory=dict)
    linked: list[str] = field(default_factory=list)
    prior: dict[str, Any] | Exception | None = None
    registration: Exception | None = None
    override: dict[str, Any] = field(default_factory=dict)
    calls: list[tuple[Any, ...]] = field(default_factory=list)

    async def get_node(self, node_id: str, as_of: date | str | None = None) -> dict[str, Any]:
        self.calls.append(("node", node_id, as_of))
        item = self.nodes.get(node_id, error(404, "NODE_NOT_FOUND"))
        if isinstance(item, Exception):
            raise item
        return item

    async def document_meta(self, doc_id: str) -> dict[str, Any]:
        self.calls.append(("meta", doc_id))
        if self.prior is None:
            raise error(404, "DOC_NOT_FOUND")
        if isinstance(self.prior, Exception):
            raise self.prior
        return self.prior

    def meta(self, source: Mapping[str, Any]) -> dict[str, Any]:
        return {
            "doc_id": source["doc_id"],
            "source": dict(source),
            "doc_type": self.kind,
            "doc_date": None,
            "char_length": len(self.content.text),
            "canon_version": "1",
            "node_ids": self.linked,
            "title": None,
            "page_markers": self.content.markers,
        } | self.override

    async def register_document(
        self,
        source_record: Mapping[str, Any],
        *,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        self.calls.append(("register", dict(source_record), idempotency_key))
        if self.registration is not None:
            raise self.registration
        return self.meta(source_record)


@dataclass
class CheckDouble:
    result: CrosscheckResult | Exception
    calls: list[tuple[str, str, date]] = field(default_factory=list)

    async def crosscheck_citation(
        self,
        citation_string: str,
        case_name: str,
        decided_on: date,
    ) -> CrosscheckResult:
        self.calls.append((citation_string, case_name, decided_on))
        if isinstance(self.result, Exception):
            raise self.result
        return self.result


def record(raw: bytes = RAW, **overrides: Any) -> SourceRecord:
    return SourceRecord.model_validate(
        {
            "source_id": f"src_{new_ulid()}",
            "url": FILE,
            "registry_entry_id": "sec_edgar",
            "retrieved_at": datetime(2026, 11, 7, tzinfo=UTC),
            "doc_id": "sha256:" + sha256(raw).hexdigest(),
            "media_type": "text/html",
            "byte_length": len(raw),
            "http_status": 200,
            "fetch_method": "api",
            "retrieved_by": "fixture",
        }
        | overrides
    )


def opinion(raw: bytes = RAW, **overrides: Any) -> SourceRecord:
    return record(
        raw,
        **(
            {
                "url": OPINION,
                "registry_entry_id": "de_courts",
                "fetch_method": "adapter",
                "expected_doc_type": "opinion",
            }
            | overrides
        ),
    )


def statute(**overrides: Any) -> SourceRecord:
    return record(
        **(
            {
                "url": STATUTE,
                "registry_entry_id": "de_code_online",
                "fetch_method": "adapter",
                "expected_doc_type": "statute",
                "version_label": "Synthetic edition",
            }
            | overrides
        )
    )


def matter(**overrides: Any) -> Matter:
    return Matter.model_validate(
        {
            "matter_id": "mtr_fixture",
            "title": "Fixture",
            "task": "research",
            "question": "Check authority",
            "as_of": DAY,
            "created_at": datetime(2026, 11, 7, tzinfo=UTC),
        }
        | overrides
    )


def claim(**overrides: Any) -> Claim:
    return Claim.model_validate(
        {
            "claim_id": "clm_fixture",
            "matter_id": "mtr_fixture",
            "kind": "citation",
            "text": "Synthetic claim",
            "as_of": DAY,
            "proposed_by": "fixture",
        }
        | overrides
    )


def gate(
    site: FetchFixture,
    graph: GraphDouble,
    details: SourceDetails | None = None,
    **options: Any,
) -> SourceGate:
    registry = options.pop("registry", load_registry(REGISTRY))
    return SourceGate(
        registry,
        site.blobs,
        graph,
        site.database,
        **(
            {
                "canonicalize": lambda raw, media: graph.content,
                "source_details": (lambda source: details) if details is not None else None,
                "environment": "test",
            }
            | options
        ),
    )


def row(site: FetchFixture, source_id: str) -> SourceRow:
    with session_scope(site.database) as db:
        result = SourceRecordsRepository(db).get(source_id)
        assert result is not None
        return result


def seed(
    site: FetchFixture, source: SourceRecord, *, admitted: bool = True, **linkage: Any
) -> None:
    with session_scope(site.database) as db:
        SourceRecordsRepository(db).add(
            SourceRow(
                source_id=source.source_id,
                run_id=RUN_ID,
                record=source.model_dump(mode="json") | {"_source_gate": linkage},
                admitted=admitted,
            )
        )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("updates", "raw", "expected"),
    [
        ({"registry_entry_id": None}, RAW, rc.SG_URL_NOT_IN_REGISTRY),
        ({"registry_entry_id": "de_courts"}, RAW, rc.SG_URL_NOT_IN_REGISTRY),
        ({"url": "https://www.sec.gov.evil.test/Archives/filing"}, RAW, rc.SG_URL_NOT_IN_REGISTRY),
        ({"url": "https://www.sec.gov/Archives/%2e%2e/private"}, RAW, rc.SG_URL_NOT_IN_REGISTRY),
        ({"url": "https://www.sec.gov/robots.txt"}, RAW, rc.SG_URL_NOT_IN_REGISTRY),
        ({"media_type": "application/json"}, RAW, rc.SG_TYPE_MISMATCH),
        ({"media_type": "application/pdf"}, RAW, rc.SG_TYPE_MISMATCH),
        ({"expected_doc_type": "8k"}, RAW, rc.SG_TYPE_MISMATCH),
        ({"fetch_method": "operator"}, RAW, rc.SG_TYPE_MISMATCH),
        ({"byte_length": 1}, RAW, rc.SG_HASH_MISMATCH),
        ({"doc_id": "sha256:" + "0" * 64}, RAW, rc.SG_HASH_MISMATCH),
        ({}, b"", rc.SG_HASH_MISMATCH),
        ({"http_status": 206}, RAW, rc.SG_BLOCK_PAGE_CONTENT),
        ({"http_status": 429}, RAW, rc.SG_BLOCK_PAGE_CONTENT),
        ({}, b"<p>Access denied</p>", rc.SG_BLOCK_PAGE_CONTENT),
        ({}, b'<iframe src="https://widget.test/recaptcha"></iframe>', rc.SG_BLOCK_PAGE_CONTENT),
        ({}, b'<input type="password">', rc.SG_BLOCK_PAGE_CONTENT),
        ({"url": "https://www.sec.gov/Archives/unrelated"}, RAW, rc.SG_IDENTITY_MISMATCH),
        ({"canon_version": "2"}, RAW, rc.SG_VERSION_MISSING),
    ],
)
async def test_local_rejections_persist_no_content(
    site: FetchFixture,
    updates: dict[str, Any],
    raw: bytes,
    expected: str,
) -> None:
    source = record(raw, **updates)
    graph = GraphDouble()
    result = await gate(site, graph).admit_document(source, raw)
    assert not result.admitted and result.reason_code == expected
    stored = row(site, source.source_id)
    assert not stored.admitted and stored.admission_reason == expected
    assert graph.calls == [] and site.blobs.data == {}


@pytest.mark.asyncio
async def test_production_terms_read_permissions_and_verified_path(site: FetchFixture) -> None:
    source = record()
    graph = GraphDouble()
    denied = await gate(site, graph, environment="prod").admit_document(source, RAW)
    assert denied.reason_code == rc.SG_TERMS_UNVERIFIED
    entries = load_registry(REGISTRY).entries
    base = next(item for item in entries if item.entry_id == "sec_edgar")
    reviewed = base.model_copy(
        update={
            "terms_verified": True,
            "terms_url": "https://www.sec.gov/terms",
            "terms_checked_on": DAY,
            "allowed_action_classes": ["SEARCH"],
        }
    )
    result = await gate(
        site,
        graph,
        environment="prod",
        registry=Registry(
            version=1,
            entries=[reviewed],
        ),
    ).admit_document(source, RAW)
    assert result.reason_code == rc.SG_URL_NOT_IN_REGISTRY
    approved = reviewed.model_copy(update={"allowed_action_classes": ["READ"]})
    result = await gate(
        site,
        graph,
        environment="prod",
        registry=Registry(
            version=1,
            entries=[approved],
        ),
    ).admit_document(source, RAW)
    assert result.admitted


@pytest.mark.asyncio
async def test_register_writes_canonical_objects_and_preserves_exact_source_shape(
    site: FetchFixture,
) -> None:
    source = record(url="https://www.sec.gov/Archives/0000001234-26-000001-index.html")
    graph = GraphDouble(linked=["Source:synthetic"])
    guard = gate(site, graph)
    first = await guard.admit_document(source, RAW)
    assert first.admitted and first.reason_code == rc.SG_OK
    digest = source.doc_id.removeprefix("sha256:")
    assert site.blobs.data[f"raw/{digest}"] == RAW
    assert site.blobs.data[f"canon/{digest}.v1.txt"] == CANON.encode()
    assert json.loads(site.blobs.data[f"canon/{digest}.v1.pages.json"]) == graph.content.markers
    payload = json.loads(site.blobs.data[f"source/{source.source_id}.json"])
    assert set(payload) == set(SourceRecord.model_fields)
    assert payload["canon_version"] == "1"
    assert graph.calls == [("register", payload, source.source_id)]
    stored = row(site, source.source_id)
    assert stored.admitted and stored.admission_reason is None
    assert stored.record["_source_gate"]["node_ids"] == ["Source:synthetic"]
    assert await guard.admit_document(source, RAW) == first
    reused = await guard.admit_document(source.model_copy(update={"url": FILE}), RAW)
    assert reused.reason_code == rc.SG_IDENTITY_MISMATCH and row(site, source.source_id).admitted


@pytest.mark.asyncio
async def test_source_id_and_conflicting_blob_fail_closed(site: FetchFixture) -> None:
    graph = GraphDouble()
    guard = gate(site, graph)
    assert (
        await guard.admit_document(record(source_id="../unsafe"), RAW)
    ).reason_code == rc.SG_IDENTITY_MISMATCH
    source = record()
    key = "raw/" + source.doc_id.removeprefix("sha256:")
    site.blobs.data[key] = b"conflicting bytes"
    assert (await guard.admit_document(source, RAW)).reason_code == rc.SG_HASH_MISMATCH
    assert site.blobs.data[key] == b"conflicting bytes" and not graph.calls


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("content", "expected"),
    [
        (Canon(text="x" * 500), rc.SG_VERSION_MISSING),
        (Canon(canon_version=2), rc.SG_VERSION_MISSING),
        (Canon(text="Access denied"), rc.SG_BLOCK_PAGE_CONTENT),
    ],
)
async def test_opinion_canonical_checks(
    site: FetchFixture,
    content: Canon,
    expected: str,
) -> None:
    raw = b"%PDF-1.7 fixture"
    graph = GraphDouble(kind="opinion", content=content)
    result = await gate(
        site, graph, SourceDetails("opinion", {"citation": "1 Fixture 2"})
    ).admit_document(
        opinion(raw, media_type="application/pdf"),
        raw,
    )
    assert result.reason_code == expected and not graph.calls


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("source_kind", "detail", "expected"),
    [
        ("statute", SourceDetails("statute", {"section": "144"}), rc.SG_VERSION_MISSING),
        ("opinion", SourceDetails("statute", {"section": "144"}), rc.SG_TYPE_MISMATCH),
        ("opinion", SourceDetails("opinion"), rc.SG_IDENTITY_MISMATCH),
        (
            "opinion",
            SourceDetails("opinion", {"citation": "1 Fixture 2", "decided_on": "bad"}),
            rc.SG_IDENTITY_MISMATCH,
        ),
    ],
)
async def test_admission_requires_independent_identity_and_version(
    site: FetchFixture,
    source_kind: str,
    detail: SourceDetails,
    expected: str,
) -> None:
    source = statute(version_label=None) if source_kind == "statute" else opinion()
    graph = GraphDouble(kind=source_kind)
    result = await gate(site, graph, detail).admit_document(source, RAW)
    assert result.reason_code == expected and not graph.calls


CASE_PROPS = {
    "case_name": "Synthetic v. Fixture",
    "decided_on": "2025-01-01",
    "citation_string": "1 Fixture 2",
}
CASE_DETAILS = SourceDetails(
    "opinion",
    {
        "case_name": "Synthetic v. Fixture",
        "decided_on": date(2025, 1, 1),
        "citation": "1 Fixture 2",
    },
)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "failure", ["mismatch", "missing", "wrong_node", "no_link", "unavailable", None]
)
async def test_graph_identity_is_verified_before_admission(
    site: FetchFixture, failure: str | None
) -> None:
    source = opinion()
    graph = GraphDouble(kind="opinion", linked=[CASE])
    graph.nodes[CASE] = node(props=CASE_PROPS)
    if failure == "mismatch":
        graph.prior = graph.meta(source.model_dump(mode="json"))
        graph.nodes[CASE] = node(props=CASE_PROPS | {"citation_string": "Different"})
    elif failure == "missing":
        graph.nodes.clear()
    elif failure == "wrong_node":
        graph.nodes[CASE] = node("Case:wrong", props=CASE_PROPS)
    elif failure == "no_link":
        graph.linked.clear()
    elif failure == "unavailable":
        graph.prior = ServiceUnavailableError("graph-api", "offline")
    result = await gate(site, graph, CASE_DETAILS).admit_document(source, RAW)
    if failure is None:
        assert result.admitted and result.reason_code == rc.SG_OK
        assert row(site, source.source_id).record["_source_gate"]["node_ids"] == [CASE]
    else:
        assert not row(site, source.source_id).admitted
        assert result.reason_code == (
            rc.SG_SOURCE_NOT_ADMITTED if failure == "unavailable" else rc.SG_IDENTITY_MISMATCH
        )
    if failure in {"mismatch", "unavailable"}:
        assert all(call[0] != "register" for call in graph.calls)


@pytest.mark.asyncio
@pytest.mark.parametrize("matches", [True, False])
async def test_statute_identity_uses_provision_section_version_and_hash(
    site: FetchFixture, matches: bool
) -> None:
    source = statute()
    graph = GraphDouble(
        kind="statute",
        linked=[PROVISION],
        nodes={
            PROVISION: node(
                PROVISION,
                props={
                    "section": "144" if matches else "145",
                    "version_label": source.version_label,
                    "text_doc_id": source.doc_id,
                },
            )
        },
    )
    detail = SourceDetails("statute", {"section": "144"}, authority_node_id=PROVISION)
    result = await gate(site, graph, detail).admit_document(source, RAW)
    assert result.reason_code == (rc.SG_OK if matches else rc.SG_IDENTITY_MISMATCH)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("override", "expected"),
    [
        ({"doc_id": "sha256:" + "0" * 64}, rc.SG_HASH_MISMATCH),
        ({"source": {"doc_id": "different"}}, rc.SG_HASH_MISMATCH),
        ({"canon_version": "2"}, rc.SG_HASH_MISMATCH),
        ({"char_length": 1}, rc.SG_HASH_MISMATCH),
        ({"doc_type": "statute"}, rc.SG_TYPE_MISMATCH),
        ({"doc_type": None}, rc.SG_TYPE_MISMATCH),
        ({"node_ids": [None]}, rc.SG_SOURCE_NOT_ADMITTED),
    ],
)
async def test_graph_metadata_must_match_capture(
    site: FetchFixture,
    override: dict[str, Any],
    expected: str,
) -> None:
    graph = GraphDouble(override=override)
    source = record()
    assert (await gate(site, graph).admit_document(source, RAW)).reason_code == expected
    assert not row(site, source.source_id).admitted


@pytest.mark.asyncio
async def test_graph_outage_persists_rejection_and_safe_retry(site: FetchFixture) -> None:
    source = record()
    graph = GraphDouble(registration=ServiceUnavailableError("graph-api", "offline"))
    guard = gate(site, graph)
    assert (await guard.admit_document(source, RAW)).reason_code == rc.SG_SOURCE_NOT_ADMITTED
    assert not row(site, source.source_id).admitted
    graph.registration = None
    assert (await guard.admit_document(source, RAW)).admitted


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("setup", "expected"),
    [
        ("empty", rc.SG_SOURCE_NOT_ADMITTED),
        ("missing", rc.SG_SOURCE_NOT_ADMITTED),
        ("not_admitted", rc.SG_SOURCE_NOT_ADMITTED),
        ("mismatch", rc.SG_SOURCE_NOT_ADMITTED),
        ("hash_only", rc.SG_SOURCE_NOT_ADMITTED),
        ("future", rc.SG_NODE_NOT_KNOWN_AS_OF),
        ("valid", rc.SG_OK),
    ],
)
async def test_claim_explicit_source_requirements(
    site: FetchFixture, setup: str, expected: str
) -> None:
    source = record()
    if setup not in {"empty", "missing", "hash_only"}:
        seed(
            site,
            source,
            admitted=setup != "not_admitted",
            doc_date="2027-01-01" if setup == "future" else None,
        )
    reference = {} if setup == "empty" else {"source_doc_id": source.doc_id}
    if setup not in {"empty", "hash_only"}:
        reference["source_id"] = source.source_id
    if setup == "mismatch":
        reference["source_doc_id"] = "sha256:" + "0" * 64
    result = await gate(site, GraphDouble()).check_claim(claim(**reference), matter())
    assert result.reason_code == expected
    assert result.verdict == ("pass" if expected == rc.SG_OK else "block")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("response", "expected", "verdict"),
    [
        (error(404, "NODE_NOT_FOUND"), rc.SG_NODE_NOT_IN_GRAPH, "block"),
        (error(404, "NOT_KNOWN_AS_OF"), rc.SG_NODE_NOT_KNOWN_AS_OF, "block"),
        (error(404, "DOC_NOT_FOUND"), rc.SG_CITATION_UNVERIFIED, "unknown"),
        (ServiceUnavailableError("graph-api", "offline"), rc.SG_CITATION_UNVERIFIED, "unknown"),
        (UpstreamContractError("malformed"), rc.SG_CITATION_UNVERIFIED, "unknown"),
        (node("Case:wrong"), rc.SG_CITATION_UNVERIFIED, "unknown"),
        (node(known_from="2027-01-01"), rc.SG_NODE_NOT_KNOWN_AS_OF, "block"),
        (node(), rc.SG_CITATION_UNVERIFIED, "block"),
    ],
)
async def test_claim_queries_graph_at_claim_date(
    site: FetchFixture,
    response: dict[str, Any] | Exception,
    expected: str,
    verdict: str,
) -> None:
    graph = GraphDouble(nodes={CASE: response})
    result = await gate(site, graph).check_claim(claim(authority_node_id=CASE), matter())
    assert result.reason_code == expected and result.verdict == verdict
    assert graph.calls == [("node", CASE, DAY)]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "linkage", ["graph", "stored", "unrelated", "pending", "off_registry", "future"]
)
async def test_claim_case_linkage_must_resolve_to_admitted_registry_document(
    site: FetchFixture,
    linkage: str,
) -> None:
    source = (
        record(url="https://unregistered.test/public") if linkage == "off_registry" else record()
    )
    seed(
        site,
        source,
        admitted=linkage != "pending",
        node_ids=[CASE] if linkage == "stored" else [],
        doc_date="2027-01-01" if linkage == "future" else "2025-01-01",
    )
    ids = [] if linkage in {"stored", "unrelated"} else [source.source_id]
    graph = GraphDouble(nodes={CASE: node(source_ids=ids)})
    values = {"authority_node_id": CASE}
    if linkage in {"stored", "unrelated"}:
        values |= {"source_id": source.source_id, "source_doc_id": source.doc_id}
    result = await gate(site, graph).check_claim(claim(**values), matter())
    assert result.reason_code == (
        rc.SG_OK if linkage in {"stored", "graph"} else rc.SG_CITATION_UNVERIFIED
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("mode", "verdict"),
    [
        ("match", "pass"),
        ("holding", "pass"),
        ("mismatch", "block"),
        ("unregistered", "block"),
        ("failed", "unknown"),
        ("future_date", "block"),
        ("missing_parent", "block"),
    ],
)
async def test_crosscheck_requires_exact_point_in_time_case_context(
    site: FetchFixture,
    mode: str,
    verdict: str,
) -> None:
    node_id = HOLDING if mode in {"holding", "missing_parent"} else CASE
    graph = GraphDouble(
        nodes={
            CASE: node(
                props=CASE_PROPS | ({"decided_on": "2027-01-01"} if mode == "future_date" else {})
            ),
            HOLDING: node(HOLDING, props={"case_node_id": CASE} if mode == "holding" else {}),
        }
    )
    check = CheckDouble(
        APIError(503, "SOURCE_UNRETRIEVABLE", "Unavailable")
        if mode == "failed"
        else CrosscheckResult(
            found=mode != "mismatch",
            matched_date=date(2025, 1, 1),
            matched_name="Synthetic v. Fixture",
            url="https://unregistered.test/opinion/" if mode == "unregistered" else LOOKUP,
        )
    )
    result = await gate(site, graph, crosscheck=check).check_claim(
        claim(authority_node_id=node_id),
        matter(),
    )
    assert result.verdict == verdict
    assert result.reason_code == (rc.SG_OK if verdict == "pass" else rc.SG_CITATION_UNVERIFIED)


@pytest.mark.asyncio
async def test_matter_date_is_not_overridden_by_later_claim(site: FetchFixture) -> None:
    with pytest.raises(APIError) as exc:
        await gate(site, GraphDouble()).check_claim(claim(as_of=date(2027, 1, 1)), matter())
    assert exc.value.code == "VALIDATION_ERROR"


@pytest.mark.asyncio
async def test_adapter_report_is_available_to_gate_without_changing_source_schema(
    site: FetchFixture,
) -> None:
    import httpx

    from gba_agent.access.fetcher import Fetcher
    from gba_agent.adapters.base import AdapterContext, AdapterRegistry, AdapterTask
    from gba_agent.adapters.de_code import DelawareCodeAdapter

    raw = "<h2>§ 144. Synthetic heading</h2>".encode()

    def response(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/robots.txt":
            return httpx.Response(200, text="User-agent: *\nDisallow:\n")
        assert str(request.url) == STATUTE
        return httpx.Response(200, content=raw, headers={"content-type": "text/html"})

    registry = load_registry(REGISTRY)
    fetcher = Fetcher(
        site.settings,
        site.database,
        site.blobs,
        registry=registry,
        transport=httpx.MockTransport(response),
        clock=lambda: site.time,
        sleep=site.sleep,
    )
    (document,) = await AdapterRegistry(scripted=[DelawareCodeAdapter()]).run(
        "de_code_online",
        AdapterTask(
            kind="fetch_statute",
            params={
                "title": 8,
                "chapter": 1,
                "section": 144,
                "subchapter": 4,
            },
        ),
        AdapterContext(fetcher, RUN_ID),
    )
    (stored,) = site.sources()
    source = SourceRecord.model_validate(SourceRecordsRepository.contract_data(stored))
    assert document.raw == raw and not stored.admitted
    graph = GraphDouble(
        kind="statute",
        linked=[PROVISION],
        nodes={
            PROVISION: node(
                PROVISION,
                props={
                    "section": "144",
                    "version_label": source.version_label,
                },
            )
        },
    )
    assert (await gate(site, graph).admit_document(source, raw)).admitted
    assert (
        row(site, source.source_id).record["_source_gate"]["adapter"]["identity"]["section"]
        == "144"
    )
    with pytest.raises(APIError, match="conflict"):
        fetcher.report_adapter_document(
            source.source_id, source.doc_id, "statute", {"section": "145"}
        )
    with pytest.raises(APIError, match="verified capture"):
        fetcher.report_adapter_document("missing", source.doc_id, "statute", {})
    assert not row(site, source.source_id).admitted
    assert (
        await gate(site, graph).admit_document(source, raw)
    ).reason_code == rc.SG_IDENTITY_MISMATCH


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("mode", "expected"),
    [
        ("default_available", rc.SG_OK),
        ("default_missing", rc.SG_SOURCE_NOT_ADMITTED),
        ("invalid_content", rc.SG_TYPE_MISMATCH),
    ],
)
async def test_canonicalizer_dependency_and_invalid_content(
    site: FetchFixture,
    mode: str,
    expected: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import builtins
    import sys
    from types import ModuleType

    registry = load_registry(REGISTRY)
    original_import = builtins.__import__
    if mode == "default_missing":

        def import_missing(name: str, *args: Any, **kwargs: Any) -> Any:
            if name == "gba_contracts.canon":
                raise ModuleNotFoundError(name="gba_contracts")
            return original_import(name, *args, **kwargs)

        monkeypatch.setattr(builtins, "__import__", import_missing)
    elif mode == "default_available":
        package = ModuleType("gba_contracts")
        canon = ModuleType("gba_contracts.canon")
        canon.__dict__["to_canonical_text"] = lambda raw, media: Canon()
        monkeypatch.setitem(sys.modules, "gba_contracts", package)
        monkeypatch.setitem(sys.modules, "gba_contracts.canon", canon)
    options: dict[str, Any] = {}
    if mode == "invalid_content":

        def invalid(raw: bytes, media: str) -> Canon:
            raise ValueError("Unreadable source")

        options["canonicalize"] = invalid
    guard = SourceGate(registry, site.blobs, GraphDouble(), site.database, **options)
    assert (await guard.admit_document(record(), RAW)).reason_code == expected


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("mode", "expected"),
    [
        ("existing_valid", rc.SG_OK),
        ("existing_wrong_digest", rc.SG_IDENTITY_MISMATCH),
        ("existing_wrong_type", rc.SG_IDENTITY_MISMATCH),
        ("preselected_other", rc.SG_IDENTITY_MISMATCH),
        ("wrong_node_type", rc.SG_IDENTITY_MISMATCH),
        ("node_unavailable", rc.SG_SOURCE_NOT_ADMITTED),
    ],
)
async def test_existing_graph_metadata_and_node_verification(
    site: FetchFixture,
    mode: str,
    expected: str,
) -> None:
    source = opinion()
    graph = GraphDouble(kind="opinion", linked=[CASE], nodes={CASE: node(props=CASE_PROPS)})
    detail = CASE_DETAILS
    if mode.startswith("existing"):
        graph.prior = graph.meta(source.model_dump(mode="json"))
        if mode == "existing_wrong_digest":
            graph.prior["doc_id"] = "another"
        elif mode == "existing_wrong_type":
            graph.prior["doc_type"] = "statute"
    if mode == "preselected_other":
        detail = SourceDetails("opinion", {"citation": "1 Fixture 2"}, "Case:other")
        graph.nodes["Case:other"] = node("Case:other", props=CASE_PROPS)
    if mode == "wrong_node_type":
        graph.nodes[CASE] = node(props=CASE_PROPS) | {"node_type": "Holding"}
    if mode == "node_unavailable":
        graph.nodes[CASE] = ServiceUnavailableError("graph-api", "offline")
    assert (await gate(site, graph, detail).admit_document(source, RAW)).reason_code == expected


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "mode", ["no_date", "bad_record", "wrong_row_id", "no_meta", "wrong_linked_kind"]
)
async def test_claim_provenance_data_and_non_case_authority(site: FetchFixture, mode: str) -> None:
    source = record()
    seed(site, source)
    with session_scope(site.database) as db:
        persisted = SourceRecordsRepository(db).get(source.source_id)
        assert persisted is not None
        if mode == "bad_record":
            persisted.record = persisted.record | {"doc_id": "bad"}
        elif mode == "wrong_row_id":
            persisted.record = persisted.record | {"source_id": "different"}
        elif mode == "no_meta":
            persisted.record = SourceRecordsRepository.contract_data(persisted)
    authority = PROVISION if mode == "wrong_linked_kind" else CASE
    graph = GraphDouble(nodes={authority: node(authority, source_ids=[source.source_id])})
    result = await gate(site, graph).check_claim(claim(authority_node_id=authority), matter())
    assert result.verdict == ("block" if mode in {"bad_record", "wrong_row_id"} else "pass")


@pytest.mark.asyncio
async def test_mixed_registry_document_type_still_requires_statute_version(
    site: FetchFixture,
) -> None:
    registry = load_registry(REGISTRY)
    sec = next(entry for entry in registry.entries if entry.entry_id == "sec_edgar")
    mixed = Registry(version=1, entries=[sec.model_copy(update={"doc_types": ["8k", "statute"]})])
    result = await gate(site, GraphDouble(kind="statute"), registry=mixed).admit_document(
        record(), RAW
    )
    assert result.reason_code == rc.SG_VERSION_MISSING


@pytest.mark.asyncio
async def test_malformed_adapter_sidecar_does_not_confirm_reported_type(site: FetchFixture) -> None:
    source = opinion()
    seed(site, source, admitted=False, adapter={"document_type": "opinion", "identity": "invalid"})
    result = await gate(site, GraphDouble()).admit_document(source, RAW)
    assert result.reason_code == rc.SG_TYPE_MISMATCH


@pytest.mark.asyncio
async def test_conflicting_source_insert_during_graph_registration_is_not_overwritten(
    site: FetchFixture,
) -> None:
    source = record()
    other = source.model_copy(
        update={"url": "https://www.sec.gov/Archives/0000001234-26-000001.htm"}
    )

    class ConcurrentGraph(GraphDouble):
        async def register_document(
            self,
            source_record: Mapping[str, Any],
            *,
            idempotency_key: str | None = None,
        ) -> dict[str, Any]:
            seed(site, other, admitted=False)
            return await super().register_document(source_record, idempotency_key=idempotency_key)

    result = await gate(site, ConcurrentGraph()).admit_document(source, RAW)
    assert result.reason_code == rc.SG_IDENTITY_MISMATCH
    assert row(site, source.source_id).record["url"] == other.url


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["invalid_parent_kind", "missing_context"])
async def test_incomplete_case_context_never_crosschecks(site: FetchFixture, mode: str) -> None:
    case = node(props={} if mode == "missing_context" else CASE_PROPS)
    if mode == "invalid_parent_kind":
        case["node_type"] = "Provision"
    graph = GraphDouble(nodes={CASE: case, HOLDING: node(HOLDING, props={"case_node_id": CASE})})
    checker = CheckDouble(
        CrosscheckResult(found=True, matched_date=DAY, matched_name="Fixture", url=LOOKUP)
    )
    authority = HOLDING if mode == "invalid_parent_kind" else CASE
    result = await gate(site, graph, crosscheck=checker).check_claim(
        claim(authority_node_id=authority),
        matter(),
    )
    assert result.verdict == "block" and result.reason_code == rc.SG_CITATION_UNVERIFIED
    assert not checker.calls


@pytest.mark.asyncio
async def test_adapter_parallel_citations_must_include_the_graph_citation(
    site: FetchFixture,
) -> None:
    graph = GraphDouble(kind="opinion", linked=[CASE], nodes={CASE: node(props=CASE_PROPS)})
    for citation, expected in [("1 Fixture 2", rc.SG_OK), ("3 Fixture 4", rc.SG_IDENTITY_MISMATCH)]:
        source = opinion()
        details = SourceDetails(
            "opinion",
            {
                "case_name": CASE_PROPS["case_name"],
                "decided_on": "2025-01-01",
                "citation": ["9 Parallel 10", citation],
            },
        )
        assert (
            await gate(site, graph, details).admit_document(source, RAW)
        ).reason_code == expected


@pytest.mark.asyncio
async def test_concurrent_adapter_conflict_prevents_admission(site: FetchFixture) -> None:
    source = record()
    seed(site, source, admitted=False)

    class ConcurrentGraph(GraphDouble):
        async def register_document(
            self,
            source_record: Mapping[str, Any],
            *,
            idempotency_key: str | None = None,
        ) -> dict[str, Any]:
            with session_scope(site.database) as db:
                stored = SourceRecordsRepository(db).get(source.source_id)
                assert stored is not None
                stored.record = stored.record | {"_source_gate": {"adapter_conflict": True}}
            return await super().register_document(source_record, idempotency_key=idempotency_key)

    result = await gate(site, ConcurrentGraph()).admit_document(source, RAW)
    assert result.reason_code == rc.SG_IDENTITY_MISMATCH
    assert not row(site, source.source_id).admitted
