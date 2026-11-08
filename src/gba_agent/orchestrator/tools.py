"""Closed planner tool surface. All external acquisition uses Fetcher."""

from __future__ import annotations

import math
import re
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date
from time import perf_counter
from typing import Annotated, Any, Literal

import httpx
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from gba_agent.access.fetcher import BlobStore, Fetcher, FetchRequest, OperatorFetchRequest
from gba_agent.adapters.base import AdapterContext, AdapterRegistry, AdapterTask, NotSupported
from gba_agent.api.contracts_compat import (
    ClaimModel,
    MatterFactModel,
    SourceRecord,
    _CompatMatter,
    _CompatSourceRecord,
    _CompatSpan,
)
from gba_agent.api.errors import APIError
from gba_agent.api.trace import new_ulid
from gba_agent.clients.contract_validation import UpstreamContractError
from gba_agent.clients.graph_client import GraphClient
from gba_agent.clients.http import UpstreamServiceError
from gba_agent.guards.source_gate import SourceGate
from gba_agent.orchestrator.matter_store import MatterStore, fact_value, merge_fact

type DocID = Annotated[str, Field(pattern=r"^sha256:[0-9a-f]{64}$")]
type Query = Annotated[str, Field(min_length=1, max_length=1000)]
type Notice = Callable[[str, str, str], None]
GRAPH_ERRORS = (UpstreamServiceError, UpstreamContractError, httpx.RequestError, ValidationError)


class _Args(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, hide_input_in_errors=True)


class _Query(_Args):
    query: Query
    top_k: int = Field(default=10, ge=1, le=25)


class _Search(_Query):
    doc_types: list[str] | None = Field(default=None, max_length=20)


class _Adapter(_Args):
    site_id: str
    kind: str
    params: dict[str, Any]


class _Fetch(_Args):
    url: Annotated[str, Field(min_length=1, max_length=8192)]


class _Operate(_Args):
    site_id: str
    goal: Query
    start_url: Annotated[str, Field(min_length=1, max_length=8192)]
    success_criteria: Query


class _Read(_Args):
    doc_id: DocID
    start: int = Field(ge=0)
    end: int = Field(ge=0)


class _Find(_Args):
    doc_id: DocID
    query: Query


class _Fact(_Args):
    key: str
    value: Any
    span: _CompatSpan


class _Propose(_Args):
    kind: Literal["legal_proposition", "fact", "quotation", "citation"]
    text: str = Field(min_length=1, max_length=2000)
    authority_node_id: str | None = None
    element_node_id: str | None = None
    source_doc_id: DocID | None = None
    source_id: str | None = None
    quote: str | None = Field(default=None, max_length=500)
    span: _CompatSpan | None = None
    pincite: str | None = None
    as_of: str | None = None
    proposed_by: Literal["planner"] = "planner"


_TOOLS: dict[str, tuple[type[_Args], str]] = {
    "search_corpus": (_Search, "Search the Graph corpus at the matter date."),
    "rule_candidates": (_Query, "Find candidate authorities and their scope summaries."),
    "run_adapter": (_Adapter, "Acquire evidence through a registered site adapter."),
    "fetch_url": (_Fetch, "Acquire a document from an approved registry URL."),
    "operate_site": (_Operate, "Run a read-only guarded browser task at an approved site."),
    "read_doc": (_Read, "Read at most 4000 canonical characters of an admitted document."),
    "find_in_doc": (_Find, "Find up to five passage previews in an admitted document."),
    "set_fact": (_Fact, "Record a fact with an exact canonical evidence span."),
    "propose_claim": (_Propose, "Submit a cited claim for independent verification."),
    "finish": (_Args, "Finish the current research round."),
}
TOOL_SCHEMAS: list[dict[str, Any]] = [
    {"name": name, "description": description, "input_schema": args.model_json_schema()}
    for name, (args, description) in _TOOLS.items()
]

_TOKEN = re.compile(r"\w+", re.UNICODE)
_PARAGRAPH = re.compile(r"[^\n\f]+")


def find_passages(text: str, query: str) -> list[dict[str, Any]]:
    terms = set(_TOKEN.findall(query.casefold()))
    paragraphs = list(_PARAGRAPH.finditer(text))
    counts = [Counter(_TOKEN.findall(item[0].casefold())) for item in paragraphs]
    lengths = [sum(count.values()) for count in counts]
    mean = sum(lengths) / max(len(lengths), 1) or 1
    frequencies = {term: sum(term in count for count in counts) for term in terms}
    scored: list[tuple[float, int, int]] = []
    for paragraph, count, length in zip(paragraphs, counts, lengths, strict=True):
        score = sum(
            math.log(1 + (len(paragraphs) - frequencies[term] + 0.5) / (frequencies[term] + 0.5))
            * (2.2 * count[term])
            / (count[term] + 1.2 * (0.25 + 0.75 * length / mean))
            for term in sorted(terms)
            if count[term]
        )
        if score > 0:
            scored.append((-score, paragraph.start(), paragraph.end()))
    return [
        {
            "start": start,
            "end": min(end, start + 300),
            "preview": text[start : min(end, start + 300)],
        }
        for _, start, end in sorted(scored)[:5]
    ]


@dataclass
class PlannerTools:
    run_id: str
    matter: _CompatMatter
    store: MatterStore
    blobs: BlobStore
    graph: GraphClient
    fetcher: Fetcher
    adapters: AdapterRegistry
    source_gate: SourceGate
    notice: Notice
    dry_run: bool = False
    emit: Callable[[str, str, dict[str, object]], None] | None = None

    def text(self, doc_id: str) -> str:
        value = self.store.canonical(self.blobs, doc_id, self.matter.as_of)
        if value is None:
            raise APIError(422, "SG_SOURCE_NOT_ADMITTED", "No usable admitted document")
        return value

    def valid_span(self, span: _CompatSpan) -> bool:
        text = self.store.canonical(self.blobs, span.doc_id, self.matter.as_of)
        return (
            text is not None
            and 0 <= span.start < span.end <= len(text)
            and bool(text[span.start : span.end].strip())
            and (span.quote is None or span.quote == text[span.start : span.end])
        )

    async def admit(self, record: _CompatSourceRecord) -> dict[str, object]:
        if self.store.admitted(record.doc_id, self.matter.as_of, record.source_id):
            if self.emit:
                self.emit(
                    "source_gate",
                    "state_change",
                    {
                        "from": "previously_admitted",
                        "to": "admitted",
                        "operation": "source_admission",
                        "admitted": True,
                        "reused": True,
                        "source_id": record.source_id,
                        "doc_id": record.doc_id,
                        "reason_code": "SG_OK",
                    },
                )
            return {"doc_id": record.doc_id, "source_id": record.source_id, "admitted": True}
        raw = self.fetcher.fetched_bytes(record)
        started = perf_counter()
        result = await self.source_gate.admit_document(record, raw)
        if self.emit:
            self.emit(
                "source_gate",
                "state_change",
                {
                    "from": "captured",
                    "to": "admitted" if result.admitted else "rejected",
                    "source_id": record.source_id,
                    "doc_id": record.doc_id,
                    "admitted": result.admitted,
                    "operation": "source_admission",
                    "result": {
                        "guard": "source_gate",
                        "verdict": "pass" if result.admitted else "block",
                        "reason_code": result.reason_code,
                        "reason_text": "Document admission.",
                        "evidence": {"source_id": record.source_id, "doc_id": record.doc_id},
                        "deterministic": True,
                        "guard_version": "1.0.0",
                        "duration_ms": round((perf_counter() - started) * 1000),
                    },
                },
            )
        eligible = (
            result.admitted
            and self.store.admitted(record.doc_id, self.matter.as_of, record.source_id) is not None
        )
        if not eligible:
            code = "SG_NODE_NOT_KNOWN_AS_OF" if result.admitted else result.reason_code
            self.notice(
                record.doc_id, code, "The captured document was unavailable for this matter."
            )
            return {"doc_id": record.doc_id, "admitted": False, "reason_code": code}
        return {"doc_id": record.doc_id, "source_id": record.source_id, "admitted": True}

    async def ensure_document(self, doc_id: str) -> bool:
        if not re.fullmatch(r"sha256:[0-9a-f]{64}", doc_id):
            return False
        local = self.store.admitted(doc_id, self.matter.as_of)
        try:
            meta = await self.graph.document_meta(doc_id)
        except UpstreamServiceError as exc:
            if exc.status_code != 404 or exc.code != "DOC_NOT_FOUND":
                raise
            for record in self.store.sources_for(doc_id):
                if (await self.admit(record))["admitted"]:
                    return True
            return False
        if meta.get("doc_id") != doc_id:
            raise UpstreamContractError("Graph returned a different document")
        if (public := meta.get("doc_date")) is not None and date.fromisoformat(
            public
        ) > self.matter.as_of:
            return False
        if local:
            return True
        if isinstance(meta.get("source"), dict):
            record = SourceRecord.model_validate(meta["source"])
            if record.doc_id == doc_id and self.blobs.exists(
                f"raw/{doc_id.removeprefix('sha256:')}"
            ):
                return (await self.admit(record))["admitted"] is True
        return False

    async def call(self, name: str, raw: dict[str, Any]) -> dict[str, Any]:
        spec = _TOOLS.get(name)
        if spec is None:
            return {"error": "UNKNOWN_TOOL"}
        try:
            args = spec[0].model_validate(raw)
            return await self._call(name, args)
        except APIError as exc:
            if name in {"fetch_url", "run_adapter", "operate_site"}:
                self.notice(name, "SOURCE_UNRETRIEVABLE", "The approved acquisition failed.")
            return {"error": exc.code}
        except NotSupported:
            self.notice(name, "SOURCE_UNRETRIEVABLE", "No registered adapter supports this task.")
            return {"error": "SOURCE_UNRETRIEVABLE"}
        except (UpstreamServiceError, UpstreamContractError, httpx.RequestError):
            self.notice(name, "UPSTREAM_UNAVAILABLE", "A research dependency was unavailable.")
            return {"error": "UPSTREAM_UNAVAILABLE"}
        except (ValidationError, ValueError, TypeError):
            return {"error": "VALIDATION_ERROR"}
        except (OSError, KeyError):
            self.notice(name, "UPSTREAM_UNAVAILABLE", "An evidence blob was unavailable.")
            return {"error": "UPSTREAM_UNAVAILABLE"}

    async def _call(self, name: str, args: _Args) -> dict[str, Any]:
        if name == "finish":
            return {"finished": True}
        if isinstance(args, _Read):
            text = self.text(args.doc_id)
            if not args.start < args.end <= len(text) or args.end - args.start > 4000:
                return {"error": "VALIDATION_ERROR"}
            return {
                "doc_id": args.doc_id,
                "start": args.start,
                "end": args.end,
                "text": text[args.start : args.end],
            }
        if isinstance(args, _Find):
            return {
                "doc_id": args.doc_id,
                "spans": find_passages(self.text(args.doc_id), args.query),
            }
        if isinstance(args, _Fact):
            if not self.valid_span(args.span):
                return {"error": "SG_SOURCE_NOT_ADMITTED"}
            value = fact_value(args.key, args.value)
            merge_fact(
                self.matter,
                MatterFactModel.model_validate(
                    {
                        "key": args.key,
                        "value": value,
                        "status": "known",
                        "spans": [args.span.model_dump()],
                        "extracted_by": "llm_extracted",
                    }
                ),
            )
            self.store.save_matter(self.run_id, self.matter)
            return {"key": args.key, "status": self.matter.facts[args.key].status}
        if isinstance(args, _Propose):
            return self._propose(args)
        if isinstance(args, _Search):
            return await self._search(args)
        if isinstance(args, _Query):
            response = await self.graph.rule_candidates(
                args.query,
                self.matter.as_of,
                top_k=args.top_k,
                facts=self.facts(),
            )
            return {"items": [self._candidate(item) for item in response["items"][: args.top_k]]}
        if self.dry_run:
            self.notice(name, "DRY_RUN", "External acquisition was skipped for this dry run.")
            return {"error": "DRY_RUN"}
        if isinstance(args, _Adapter):
            documents = await self.adapters.run(
                args.site_id,
                AdapterTask.model_validate({"kind": args.kind, "params": args.params}),
                AdapterContext(self.fetcher, self.run_id, self.matter.matter_id),
            )
            captures = [
                await self.admit(record)
                for document in documents
                if document.source_id and (record := self.store.source(document.source_id))
            ]
            return {"documents": captures}
        request: FetchRequest
        if isinstance(args, _Operate):
            request = OperatorFetchRequest(
                url=args.start_url,
                site_id=args.site_id,
                goal=args.goal,
                success_criteria=args.success_criteria,
                run_id=self.run_id,
                matter_id=self.matter.matter_id,
            )
        elif isinstance(args, _Fetch):
            request = FetchRequest(
                url=args.url, run_id=self.run_id, matter_id=self.matter.matter_id
            )
        else:
            raise AssertionError("Unhandled planner tool")
        outcome = await self.fetcher.fetch(request)
        if outcome.source_record is None:
            self.notice(name, "SOURCE_UNRETRIEVABLE", "The approved acquisition failed.")
            return {"admitted": False, "reason_code": outcome.reason_code or "SOURCE_UNRETRIEVABLE"}
        return await self.admit(outcome.source_record)

    def _propose(self, args: _Propose) -> dict[str, object]:
        data = args.model_dump()
        data["as_of"] = date.fromisoformat(args.as_of) if args.as_of else self.matter.as_of
        if data["as_of"] > self.matter.as_of:
            return {"error": "VALIDATION_ERROR"}
        if args.kind == "legal_proposition" and (
            not args.authority_node_id or args.span is None or not (args.quote or args.span.quote)
        ):
            return {"error": "VALIDATION_ERROR"}
        if args.span is not None and data["source_doc_id"] is None:
            data["source_doc_id"] = args.span.doc_id
        if data["source_doc_id"] and args.source_id is None:
            data["source_id"] = self.store.admitted(data["source_doc_id"], data["as_of"])
        claim = ClaimModel.model_validate(
            {
                **data,
                "claim_id": f"clm_{new_ulid()}",
                "matter_id": self.matter.matter_id,
            }
        )
        comparable = claim.model_dump(mode="json", exclude={"claim_id"})
        for existing, _ in self.store.claims(self.run_id):
            if existing.model_dump(mode="json", exclude={"claim_id"}) == comparable:
                return {"claim_id": existing.claim_id}
        self.store.save_claim(self.run_id, claim)
        return {"claim_id": claim.claim_id}

    def facts(self) -> dict[str, Any]:
        return {key: fact.model_dump(mode="json") for key, fact in self.matter.facts.items()}

    async def _search(self, args: _Search) -> dict[str, Any]:
        response = await self.graph.search_documents(
            args.query,
            self.matter.as_of,
            top_k=args.top_k,
            doc_types=args.doc_types,
        )
        hits = []
        for item in response["items"][: args.top_k]:
            doc_id = item["doc_id"]
            try:
                admitted = await self.ensure_document(doc_id)
            except (*GRAPH_ERRORS, APIError, OSError, KeyError, ValueError):
                admitted = False
            text = self.store.canonical(self.blobs, doc_id, self.matter.as_of) if admitted else None
            spans = []
            for span in item.get("spans", [])[:5] if text is not None else []:
                start, end = span.get("start"), span.get("end")
                if type(start) is int and type(end) is int and 0 <= start < end <= len(text or ""):
                    end = min(end, start + 300)
                    spans.append({"start": start, "end": end, "preview": (text or "")[start:end]})
            hits.append(
                {
                    "doc_id": doc_id,
                    "title": str(item.get("title", ""))[:300] if admitted else "",
                    "admitted": admitted,
                    "spans": spans,
                }
            )
        return {"items": hits}

    @staticmethod
    def _candidate(item: dict[str, Any]) -> dict[str, Any]:
        node = item["node"]
        frame = node.get("scope_frame")
        fields = (
            "jurisdiction",
            "addressee_classes",
            "transaction_types",
            "applies_when",
            "exceptions",
            "non_application",
            "valid_from",
            "valid_to",
        )
        return {
            "node_id": node["node_id"],
            "label": str(node.get("label", ""))[:300],
            "node_type": node.get("node_type"),
            "scope_frame": {key: frame[key] for key in fields if key in frame}
            if isinstance(frame, dict)
            else None,
        }
