"""Source admission and point-in-time authority validation (Doc 01 §9.1)."""

from __future__ import annotations

import asyncio
import json
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import date
from hashlib import sha256
from typing import TYPE_CHECKING, Any, Literal, Protocol
from urllib.parse import unquote, urlsplit

from pydantic import BaseModel, Field, ValidationError
from sqlalchemy.orm import Session, sessionmaker

from gba_agent.access._registry import Registry, RegistryEntry, matches_prefix, origin, safe_url
from gba_agent.access.block_detector import BlockDetector, ResponseInfo, html_signals
from gba_agent.api.errors import APIError
from gba_agent.api.trace import is_ulid
from gba_agent.clients.contract_validation import UpstreamContractError
from gba_agent.clients.http import UpstreamServiceError
from gba_agent.db.models import SourceRecord as SourceRow
from gba_agent.db.repositories import SourceRecordsRepository
from gba_agent.db.session import session_scope
from gba_agent.guards import reason_codes as rc

if TYPE_CHECKING:
    from gba_agent.access.fetcher import BlobStore
    from gba_agent.api.contracts_compat import _CompatAdmission as Admission
    from gba_agent.api.contracts_compat import _CompatClaim as Claim
    from gba_agent.api.contracts_compat import _CompatGuardResult as GuardResult
    from gba_agent.api.contracts_compat import _CompatMatter as Matter
    from gba_agent.api.contracts_compat import _CompatSourceRecord as SourceRecord
else:
    from gba_agent.api.contracts_compat import Admission, GuardResult, SourceRecord
    from gba_agent.api.contracts_compat import ClaimModel as Claim
    from gba_agent.api.contracts_compat import MatterModel as Matter

guard_version = "1.0.0"
__all__ = [
    "Admission",
    "Claim",
    "GuardResult",
    "Matter",
    "SourceDetails",
    "SourceGate",
    "SourceRecord",
]

_FILINGS = frozenset({"merger_agreement", "proxy_statement", "8k", "exhibit", "filing_index"})
_TEXT_MEDIA = frozenset({"text/html", "text/plain", "application/pdf"})
_MEDIA = {
    **{kind: _TEXT_MEDIA for kind in ("opinion", "statute", "bill", "session_law", *_FILINGS)},
    "exhibit": _TEXT_MEDIA
    | {"application/vnd.openxmlformats-officedocument.wordprocessingml.document"},
}
_LINKED = {"opinion": "Case", "statute": "Provision"}
_ACCESSION = re.compile(r"(?<!\d)\d{10}-\d{2}-\d{6}(?!\d)")
_EDGAR_DIRECTORY = re.compile(r"^/Archives/edgar/data/\d+/\d{18}/[^/]+$", re.I)

_TEXT = {
    rc.SG_OK: "The source and authority checks passed.",
    rc.SG_SOURCE_NOT_ADMITTED: "The claim lacks the required admitted source record.",
    rc.SG_NODE_NOT_IN_GRAPH: "The authority does not exist in the Graph API.",
    rc.SG_NODE_NOT_KNOWN_AS_OF: "The authority was not public on the claim date.",
    rc.SG_CITATION_UNVERIFIED: "No verified evidence establishes the authority linkage.",
}


@dataclass(frozen=True)
class SourceDetails:
    """Trusted adapter metadata kept outside the shared SourceRecord schema."""

    document_type: str
    identity: Mapping[str, object] = field(default_factory=dict)
    authority_node_id: str | None = None


class CanonicalContent(Protocol):
    @property
    def text(self) -> str: ...

    @property
    def markers(self) -> list[dict[str, Any]]: ...

    @property
    def canon_version(self) -> int | str: ...


class Graph(Protocol):
    async def get_node(self, node_id: str, as_of: date | str | None = None) -> dict[str, Any]: ...

    async def document_meta(self, doc_id: str) -> dict[str, Any]: ...

    async def register_document(
        self,
        source_record: Mapping[str, Any],
        *,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]: ...


class CitationMatch(Protocol):
    found: bool
    matched_name: str | None
    matched_date: date | None
    url: str | None


class CitationCrosscheck(Protocol):
    async def crosscheck_citation(
        self,
        citation_string: str,
        case_name: str,
        decided_on: date,
    ) -> CitationMatch: ...


class _GraphDocument(BaseModel):
    doc_id: str
    source: dict[str, Any]
    doc_type: str | None
    doc_date: date | None
    canon_version: str
    char_length: int
    node_ids: list[str] = Field(default_factory=list)


class _Node(BaseModel):
    node_id: str
    node_type: Literal[
        "Provision",
        "Case",
        "Holding",
        "Court",
        "TransactionType",
        "ActorClass",
        "ProcessFact",
        "Standard",
        "Source",
        "Element",
    ]
    known_from: date
    props: dict[str, Any] = Field(default_factory=dict)
    source_ids: list[str] = Field(default_factory=list)


class _Rejected(Exception):
    def __init__(self, reason_code: str) -> None:
        self.reason_code = reason_code


def _shared_canonicalize(raw: bytes, media_type: str) -> CanonicalContent:
    try:
        from gba_contracts.canon import to_canonical_text  # type: ignore[import-not-found]
    except ModuleNotFoundError:
        raise APIError(503, "UPSTREAM_UNAVAILABLE", "Shared canonicalizer is unavailable") from None
    result: CanonicalContent = to_canonical_text(raw, media_type)
    return result


def _digest(raw: bytes) -> str:
    return "sha256:" + sha256(raw).hexdigest()


def _same_capture(left: SourceRecord, right: SourceRecord) -> bool:
    return left.model_dump(exclude={"canon_version"}) == right.model_dump(exclude={"canon_version"})


def _clean(value: object) -> str:
    return " ".join(value.casefold().split()) if isinstance(value, str) else ""


def _citations(identity: Mapping[str, object]) -> set[str]:
    value = identity.get("citation_string") or identity.get("citation")
    values = value if isinstance(value, list) else [value]
    return {_clean(item) for item in values} - {""}


def _iso(value: object) -> date | None:
    if type(value) is date:
        return value
    if isinstance(value, str):
        try:
            return date.fromisoformat(value)
        except ValueError:
            return None
    return None


class SourceGate:
    def __init__(
        self,
        registry: Registry,
        blobs: BlobStore,
        graph: Graph,
        database: sessionmaker[Session],
        *,
        environment: Literal["dev", "test", "prod"] = "dev",
        source_details: Callable[[SourceRecord], SourceDetails | None] | None = None,
        crosscheck: CitationCrosscheck | None = None,
        canonicalize: Callable[[bytes, str], CanonicalContent] = _shared_canonicalize,
    ) -> None:
        self._registry, self._blobs, self._graph, self._database = registry, blobs, graph, database
        self._environment, self._details = environment, source_details
        self._crosscheck, self._canonicalize = crosscheck, canonicalize
        self._detector = BlockDetector()
        self._admission_lock = asyncio.Lock()

    def _terms(self, entry: RegistryEntry) -> bool:
        return self._environment != "prod" or bool(
            entry.terms_verified and entry.terms_url and entry.terms_checked_on
        )

    def _entry(self, url: str, entry_id: str | None) -> RegistryEntry:
        parsed = safe_url(url)
        entry = self._registry.match_url(url) if parsed is not None else None
        if (
            parsed is None
            or entry is None
            or entry.entry_id != entry_id
            or not any(
                matches_prefix(url, prefix) and origin(parsed) == origin(urlsplit(prefix))
                for prefix in entry.url_prefixes
            )
        ):
            raise _Rejected(rc.SG_URL_NOT_IN_REGISTRY)
        if not self._terms(entry):
            raise _Rejected(rc.SG_TERMS_UNVERIFIED)
        if "READ" not in entry.allowed_action_classes or parsed.path == "/robots.txt":
            raise _Rejected(rc.SG_URL_NOT_IN_REGISTRY)
        return entry

    def _blocked(self, record: SourceRecord, media_type: str, sample: str) -> bool:
        event = self._detector.detect(
            ResponseInfo(
                record.http_status,
                {},
                record.url,
                record.url,
                media_type,
                sample,
                html_signals(sample, media_type),
                record.session_id is not None,
            )
        )
        return event is not None or record.http_status not in {None, 200}

    def _prepare(
        self,
        record: SourceRecord,
        raw: bytes,
        details: SourceDetails | None,
    ) -> tuple[set[str], str, CanonicalContent]:
        entry = self._entry(record.url, record.registry_entry_id)
        media_type = record.media_type.split(";", 1)[0].strip().lower()
        allowed = {kind for kind in entry.doc_types if media_type in _MEDIA.get(kind, ())}
        if details is not None:
            allowed.intersection_update({details.document_type})
        if record.expected_doc_type is not None and (
            details is None or details.document_type != record.expected_doc_type
        ):
            raise _Rejected(rc.SG_TYPE_MISMATCH)
        if not allowed or record.fetch_method not in {entry.access, "human"}:
            raise _Rejected(rc.SG_TYPE_MISMATCH)
        if not raw or len(raw) != record.byte_length or _digest(raw) != record.doc_id:
            raise _Rejected(rc.SG_HASH_MISMATCH)
        if media_type == "application/pdf" and not raw.startswith(b"%PDF-"):
            raise _Rejected(rc.SG_TYPE_MISMATCH)
        sample = (
            raw[:8192].decode("utf-8", "replace")
            if media_type in {"text/html", "text/plain"}
            else ""
        )
        if self._blocked(record, media_type, sample):
            raise _Rejected(rc.SG_BLOCK_PAGE_CONTENT)
        if allowed == {"statute"} and not _clean(record.version_label):
            raise _Rejected(rc.SG_VERSION_MISSING)
        try:
            content = self._canonicalize(raw, media_type)
        except (UnicodeError, ValueError):
            raise _Rejected(rc.SG_TYPE_MISMATCH) from None
        if str(content.canon_version) != "1" or record.canon_version not in {None, "1"}:
            raise _Rejected(rc.SG_VERSION_MISSING)
        if media_type not in {"text/html", "text/plain"} and self._blocked(
            record, media_type, content.text[:8192]
        ):
            raise _Rejected(rc.SG_BLOCK_PAGE_CONTENT)
        if len(allowed) == 1:
            self._document_checks(next(iter(allowed)), record, details, content)
        elif allowed <= _FILINGS:
            self._filing_identity(record.url)
        return allowed, media_type, content

    @staticmethod
    def _filing_identity(url: str) -> None:
        path = unquote(urlsplit(url).path)
        if not (_ACCESSION.search(path) or _EDGAR_DIRECTORY.fullmatch(path)):
            raise _Rejected(rc.SG_IDENTITY_MISMATCH)

    def _document_checks(
        self,
        kind: str,
        record: SourceRecord,
        details: SourceDetails | None,
        content: CanonicalContent,
    ) -> None:
        if kind == "statute" and not _clean(record.version_label):
            raise _Rejected(rc.SG_VERSION_MISSING)
        if kind == "opinion" and len(content.text) <= 500:
            raise _Rejected(rc.SG_VERSION_MISSING)
        if kind in _FILINGS:
            self._filing_identity(record.url)
        if kind in _LINKED and (details is None or not self._has_identity(kind, details.identity)):
            raise _Rejected(rc.SG_IDENTITY_MISMATCH)

    @staticmethod
    def _has_identity(kind: str, identity: Mapping[str, object]) -> bool:
        if kind != "opinion":
            return bool(_clean(identity.get("section")))
        if "decided_on" in identity and _iso(identity["decided_on"]) is None:
            return False
        return bool(
            _citations(identity)
            or (_clean(identity.get("case_name")) and _iso(identity.get("decided_on")))
        )

    def _node_matches(
        self,
        node: _Node,
        kind: str,
        record: SourceRecord,
        details: SourceDetails,
    ) -> bool:
        identity, props = details.identity, node.props
        if node.node_type != _LINKED[kind]:
            return False
        if kind != "opinion":
            return (
                _clean(identity.get("section")) == _clean(props.get("section"))
                and _clean(record.version_label) == _clean(props.get("version_label"))
                and props.get("text_doc_id", record.doc_id) == record.doc_id
            )
        citations = _citations(identity)
        name, decided = _clean(identity.get("case_name")), _iso(identity.get("decided_on"))
        return (
            (not name or name == _clean(props.get("case_name")))
            and (not decided or decided == _iso(props.get("decided_on")))
            and (not citations or _clean(props.get("citation_string")) in citations)
        )

    async def _check_nodes(
        self,
        ids: list[str],
        kind: str,
        record: SourceRecord,
        details: SourceDetails,
    ) -> None:
        matches = [node_id for node_id in ids if node_id.startswith(_LINKED[kind] + ":")]
        if not matches or (details.authority_node_id and details.authority_node_id not in matches):
            raise _Rejected(rc.SG_IDENTITY_MISMATCH)
        for node_id in matches:
            try:
                node = _Node.model_validate(await self._graph.get_node(node_id))
            except UpstreamServiceError as exc:
                if exc.status_code == 404 and exc.code == "NODE_NOT_FOUND":
                    raise _Rejected(rc.SG_IDENTITY_MISMATCH) from None
                raise
            if node.node_id != node_id or not self._node_matches(node, kind, record, details):
                raise _Rejected(rc.SG_IDENTITY_MISMATCH)

    def _blob(self, key: str, raw: bytes, media_type: str) -> None:
        if not self._blobs.exists(key):
            self._blobs.put(key, raw, media_type)
        elif self._blobs.get(key) != raw:
            raise _Rejected(rc.SG_HASH_MISMATCH)

    @staticmethod
    def _json(value: object) -> bytes:
        return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()

    async def _register(
        self,
        record: SourceRecord,
        raw: bytes,
        details: SourceDetails | None,
        allowed: set[str],
        media_type: str,
        content: CanonicalContent,
    ) -> _GraphDocument:
        if details is not None and details.document_type in _LINKED:
            if details.authority_node_id is not None:
                await self._check_nodes(
                    [details.authority_node_id], details.document_type, record, details
                )
            try:
                prior = _GraphDocument.model_validate(
                    await self._graph.document_meta(record.doc_id)
                )
            except UpstreamServiceError as exc:
                if exc.status_code != 404 or exc.code != "DOC_NOT_FOUND":
                    raise
            else:
                if prior.doc_id != record.doc_id or prior.doc_type != details.document_type:
                    raise _Rejected(rc.SG_IDENTITY_MISMATCH)
                await self._check_nodes(prior.node_ids, details.document_type, record, details)

        digest = record.doc_id.removeprefix("sha256:")
        self._blob(f"raw/{digest}", raw, media_type)
        self._blob(f"canon/{digest}.v1.txt", content.text.encode(), "text/plain")
        self._blob(f"canon/{digest}.v1.pages.json", self._json(content.markers), "application/json")
        payload = record.model_dump(mode="json")
        self._blob(f"source/{record.source_id}.json", self._json(payload), "application/json")
        meta = _GraphDocument.model_validate(
            await self._graph.register_document(
                payload,
                idempotency_key=record.source_id,
            )
        )
        if (
            meta.doc_id != record.doc_id
            or meta.source.get("doc_id") != record.doc_id
            or meta.canon_version != "1"
            or meta.char_length != len(content.text)
        ):
            raise _Rejected(rc.SG_HASH_MISMATCH)
        if meta.doc_type not in allowed:
            raise _Rejected(rc.SG_TYPE_MISMATCH)
        self._document_checks(meta.doc_type, record, details, content)
        if meta.doc_type in _LINKED and details is not None:
            await self._check_nodes(meta.node_ids, meta.doc_type, record, details)
        return meta

    def _reported(self, record: SourceRecord) -> SourceDetails | None:
        with session_scope(self._database) as db:
            repo = SourceRecordsRepository(db)
            row = repo.get(record.source_id)
            if row is not None:
                if not _same_capture(SourceRecord.model_validate(repo.contract_data(row)), record):
                    raise _Rejected(rc.SG_IDENTITY_MISMATCH)
                metadata = repo.gate_data(row)
                if metadata.get("adapter_conflict") is True:
                    raise _Rejected(rc.SG_IDENTITY_MISMATCH)
                adapter = metadata.get("adapter")
                if isinstance(adapter, dict) and isinstance(
                    kind := adapter.get("document_type"), str
                ):
                    identity = adapter.get("identity")
                    if isinstance(identity, dict):
                        return SourceDetails(kind, identity)
        return None

    def _persist(
        self,
        record: SourceRecord,
        reason: str,
        meta: _GraphDocument | None = None,
    ) -> None:
        with session_scope(self._database) as db:
            repo = SourceRecordsRepository(db)
            row = repo.lock(record.source_id)
            if row is None:
                row, _ = repo.add(
                    SourceRow(source_id=record.source_id, record=record.model_dump(mode="json"))
                )
            elif not _same_capture(SourceRecord.model_validate(repo.contract_data(row)), record):
                raise _Rejected(rc.SG_IDENTITY_MISMATCH)
            payload = record.model_dump(mode="json")
            metadata = repo.gate_data(row)
            metadata.pop("node_ids", None)
            metadata.pop("doc_date", None)
            if meta is not None:
                if metadata.get("adapter_conflict") is True:
                    raise _Rejected(rc.SG_IDENTITY_MISMATCH)
                metadata.update(
                    {
                        "node_ids": meta.node_ids,
                        "doc_date": meta.doc_date.isoformat() if meta.doc_date else None,
                    }
                )
            if metadata:
                payload["_source_gate"] = metadata
            row.record = payload
            row.admitted = reason == rc.SG_OK
            row.admission_reason = None if row.admitted else reason

    async def admit_document(self, record: SourceRecord, raw: bytes) -> Admission:
        if not record.source_id.startswith("src_") or not is_ulid(record.source_id[4:]):
            return Admission(admitted=False, reason_code=rc.SG_IDENTITY_MISMATCH)
        async with self._admission_lock:
            try:
                reported = self._reported(record)
            except (_Rejected, ValidationError):
                return Admission(admitted=False, reason_code=rc.SG_IDENTITY_MISMATCH)
            try:
                details = self._details(record) if self._details else reported
                allowed, media_type, content = self._prepare(record, raw, details)
                normalized = record.model_copy(update={"canon_version": "1"})
                meta = await self._register(normalized, raw, details, allowed, media_type, content)
                self._persist(normalized, rc.SG_OK, meta)
            except _Rejected as exc:
                reason = exc.reason_code
            except (
                UpstreamServiceError,
                UpstreamContractError,
                ValidationError,
                APIError,
                OSError,
            ):
                reason = rc.SG_SOURCE_NOT_ADMITTED
            else:
                return Admission(admitted=True, reason_code=rc.SG_OK)
            try:
                self._persist(record, reason)
            except _Rejected as exc:
                reason = exc.reason_code
            return Admission(admitted=False, reason_code=reason)

    def _admitted(self, source_id: str) -> tuple[SourceRecord, dict[str, Any]] | None:
        with session_scope(self._database) as db:
            repo = SourceRecordsRepository(db)
            row = repo.get(source_id)
            if row is None or not row.admitted:
                return None
            try:
                record = SourceRecord.model_validate(repo.contract_data(row))
                self._entry(record.url, record.registry_entry_id)
            except (_Rejected, ValidationError):
                return None
            if record.source_id != source_id or record.byte_length <= 0:
                return None
            return record, repo.gate_data(row)

    @staticmethod
    def _result(
        claim: Claim,
        reason: str,
        verdict: Literal["pass", "block", "unknown"] = "block",
    ) -> GuardResult:
        return GuardResult(
            guard="source_gate",
            verdict=verdict,
            reason_code=reason,
            reason_text=_TEXT[reason],
            evidence={
                "source_id": claim.source_id,
                "source_doc_id": claim.source_doc_id,
                "authority_node_id": claim.authority_node_id,
                "as_of": claim.as_of.isoformat(),
            },
            deterministic=True,
            guard_version=guard_version,
        )

    async def _authority(self, node_id: str, as_of: date) -> _Node:
        try:
            node = _Node.model_validate(await self._graph.get_node(node_id, as_of=as_of))
        except UpstreamServiceError as exc:
            if exc.status_code == 404:
                if exc.code == "NODE_NOT_FOUND":
                    raise _Rejected(rc.SG_NODE_NOT_IN_GRAPH) from None
                if exc.code == "NOT_KNOWN_AS_OF":
                    raise _Rejected(rc.SG_NODE_NOT_KNOWN_AS_OF) from None
            raise
        if node.node_id != node_id:
            raise UpstreamContractError("Graph returned a different authority")
        if node.known_from > as_of:
            raise _Rejected(rc.SG_NODE_NOT_KNOWN_AS_OF)
        return node

    async def _crosschecked(self, node: _Node, as_of: date) -> bool:
        if self._crosscheck is None:
            return False
        if node.node_type == "Holding":
            parent = node.props.get("case_node_id")
            if not isinstance(parent, str) or not parent.startswith("Case:"):
                return False
            node = await self._authority(parent, as_of)
            if node.node_type != "Case":
                return False
        citation, name = node.props.get("citation_string"), node.props.get("case_name")
        decided = _iso(node.props.get("decided_on"))
        if not isinstance(citation, str) or not isinstance(name, str) or decided is None:
            return False
        if not citation.strip() or not name.strip() or decided > as_of:
            return False
        result = await self._crosscheck.crosscheck_citation(citation, name, decided)
        if (
            not result.found
            or result.matched_date != decided
            or not result.matched_name
            or not result.url
        ):
            return False
        entry = self._registry.match_url(result.url)
        if entry is None or entry.role != "crosscheck":
            return False
        self._entry(result.url, entry.entry_id)
        return True

    async def check_claim(self, claim: Claim, matter: Matter) -> GuardResult:
        if claim.matter_id != matter.matter_id or claim.as_of > matter.as_of:
            raise APIError(
                422, "VALIDATION_ERROR", "Claim does not match the matter or its as-of date"
            )
        explicit = self._admitted(claim.source_id) if claim.source_id is not None else None
        if (claim.source_id is not None or claim.source_doc_id is not None) and (
            explicit is None or explicit[0].doc_id != claim.source_doc_id
        ):
            return self._result(claim, rc.SG_SOURCE_NOT_ADMITTED)
        if explicit is not None:
            known = _iso(explicit[1].get("doc_date"))
            if known is not None and known > claim.as_of:
                return self._result(claim, rc.SG_NODE_NOT_KNOWN_AS_OF)
        if claim.authority_node_id is None:
            if explicit is None:
                return self._result(claim, rc.SG_SOURCE_NOT_ADMITTED)
            return self._result(claim, rc.SG_OK, "pass")
        try:
            node = await self._authority(claim.authority_node_id, claim.as_of)
            if node.node_type in {"Case", "Holding"}:
                if explicit is not None:
                    ids = explicit[1].get("node_ids")
                    if claim.source_id not in node.source_ids and (
                        not isinstance(ids, list) or node.node_id not in ids
                    ):
                        return self._result(claim, rc.SG_CITATION_UNVERIFIED)
                    return self._result(claim, rc.SG_OK, "pass")
                for source_id in node.source_ids:
                    linked = self._admitted(source_id)
                    if linked is not None:
                        public_on = _iso(linked[1].get("doc_date"))
                        if public_on is None or public_on <= claim.as_of:
                            return self._result(claim, rc.SG_OK, "pass")
                if not await self._crosschecked(node, claim.as_of):
                    return self._result(claim, rc.SG_CITATION_UNVERIFIED)
                result = self._result(claim, rc.SG_OK, "pass")
                result.evidence["crosschecked"] = True
                return result
        except _Rejected as exc:
            reason = exc.reason_code if exc.reason_code in _TEXT else rc.SG_CITATION_UNVERIFIED
            return self._result(claim, reason)
        except (UpstreamServiceError, UpstreamContractError, ValidationError, APIError):
            return self._result(claim, rc.SG_CITATION_UNVERIFIED, "unknown")
        return self._result(claim, rc.SG_OK, "pass")
