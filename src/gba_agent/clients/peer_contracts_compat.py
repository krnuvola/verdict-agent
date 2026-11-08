"""Temporary peer DTOs copied from frozen Doc 00 sections 6, 9.1 and 9.3.

Data validation only; no B/C algorithms. Replace through contract_validation's
single shared-model import when C publishes the actual package. Existing core
DTOs are reused, rather than defining competing Matter/Dossier/Claim schemas.
"""

from __future__ import annotations

from datetime import date
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from gba_agent.api.contracts_compat import (
    _CompatCondition as ConditionStatus,
)
from gba_agent.api.contracts_compat import (
    _CompatDossier as Dossier,
)
from gba_agent.api.contracts_compat import (
    _CompatMatter as Matter,
)
from gba_agent.api.contracts_compat import (
    _CompatMatterFact as MatterFact,
)
from gba_agent.api.contracts_compat import (
    _CompatProposedEdit as ProposedEdit,
)
from gba_agent.api.contracts_compat import (
    _CompatSourceRecord as SourceRecord,
)
from gba_agent.api.contracts_compat import (
    _CompatSpan as Span,
)
from gba_agent.api.contracts_compat import (
    _CompatStandard as StandardOfReview,
)

Predicate = dict[str, Any]
Tri = Literal["true", "false", "unknown"]


class B(BaseModel):
    model_config = ConfigDict(extra="forbid")


NodeType = Literal[
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

EdgeType = Literal[
    "cites",
    "interprets",
    "follows",
    "distinguishes",
    "limits",
    "overrules",
    "superseded_by_statute",
    "applies_to",
    "binds",
    "satisfies",
    "published_by",
    "addresses",
    "next_version",
    "amends",
]

TransactionTypeValue = Literal[
    "going_private", "other_controller_deal", "third_party_sale", "interested_director", "other"
]

ActorClassValue = Literal[
    "controller",
    "control_group",
    "independent_director",
    "interested_director",
    "disinterested_stockholder",
    "minority_stockholder",
]

Origin = Literal["registry", "llm_extracted", "human", "seed"]

DOC_ID_PATTERN = "^sha256:[0-9a-f]{64}$"


class ExceptionClause(B):
    exception_id: str
    text: str
    when: Predicate


class ScopeFrame(B):
    addressee_classes: list[ActorClassValue] = []  # empty = any
    transaction_types: list[TransactionTypeValue] = []  # empty = any
    applies_when: Predicate | None = None  # trigger conditions
    exceptions: list[ExceptionClause] = []  # if any is true the rule does not apply
    non_application: Predicate | None = None  # e.g. statute pendency carve-out
    defined_terms: dict[str, Span | None] = {}
    jurisdiction: str = "US-DE"
    valid_from: date
    valid_to: date | None = None  # exclusive
    source_spans: list[Span] = []
    audited: bool = False


class Node(B):
    node_id: str  # '<NodeType>:<slug>'
    node_type: NodeType
    label: str
    props: dict[str, Any] = {}  # conventions in section 6.1
    scope_frame: ScopeFrame | None = None
    valid_from: date | None = None
    valid_to: date | None = None
    known_from: date  # public availability date (decision/enactment/publication)
    source_ids: list[str] = []
    origin: Origin
    audited: bool = False


class Edge(B):
    edge_id: str
    src: str
    dst: str
    edge_type: EdgeType
    condition: Predicate | None = None  # edge takes effect only if this evaluates true
    props: dict[str, Any] = {}
    valid_from: date | None = None
    valid_to: date | None = None
    known_from: date
    source_span: Span | None = None
    confidence: float | None = Field(None, ge=0, le=1)
    origin: Origin
    audited: bool = False


class TreatmentEvent(B):
    edge: Edge  # edge_type in overrules | superseded_by_statute | limits
    effective_date: date
    by_node_id: str


class ValidityResult(B):
    node_id: str
    as_of: date
    in_force: Tri  # from valid_from/valid_to only
    good_law_unconditional: Literal["good", "superseded", "overruled", "limited", "unknown"]
    treatments: list[TreatmentEvent]  # effective_date <= as_of; conditions NOT evaluated here
    reasons: list[str]


class RuleCandidate(B):
    node: Node  # always carries scope_frame for Provision/Holding
    score: float
    spans: list[Span] = []


class ScopeFrameReq(B):
    node_id: str
    as_of: date


class ScopeFrameResp(B):
    node_id: str
    frame: ScopeFrame | None


class ValidityReq(B):
    node_id: str
    as_of: date


# response: ValidityResult
class RuleCandidatesReq(B):
    query: str
    as_of: date
    top_k: int = 10
    node_types: list[NodeType] = ["Provision", "Holding"]
    facts: dict[str, MatterFact] = {}  # optional: used only to pre-rank, never to filter


class RuleCandidatesResp(B):
    items: list[RuleCandidate]


class StandardReq(B):
    as_of: date
    facts: dict[str, MatterFact]


class StandardResp(B):
    standard: StandardOfReview
    conditions: list[
        ConditionStatus
    ]  # Element nodes relevant to the applicable regime(s); spans left empty


class DocSearchReq(B):
    query: str
    as_of: date
    top_k: int = 10
    registry_entry_ids: list[str] = []
    doc_types: list[str] = []


class DocHit(B):
    doc_id: str
    source_id: str
    title: str
    doc_type: str
    doc_date: date | None
    score: float
    spans: list[Span]


class DocSearchResp(B):
    items: list[DocHit]


class DocMeta(B):
    doc_id: str
    source: SourceRecord
    title: str | None
    doc_type: str | None
    doc_date: date | None
    canon_version: str
    char_length: int
    page_markers: list[dict[str, Any]]
    node_ids: list[
        str
    ] = []  # graph nodes created or linked for this document (opinions, statute versions)


class RegisterDocReq(B):
    source: SourceRecord  # blob raw/<hex> must already exist in the blob store


class ExtractReq(B):
    doc_ids: list[str]
    fact_keys: list[str] = []
    as_of: date


class JobStatus(B):
    job_id: str
    status: Literal["queued", "running", "done", "failed"]
    result: list[MatterFact] | None = None
    error: str | None = None


class EdgeProbe(B):
    src: str
    dst: str
    edge_type: EdgeType


class ScoreEdgesReq(B):
    as_of: date
    edges: list[EdgeProbe]


class EdgeScore(B):
    src: str
    dst: str
    edge_type: EdgeType
    score_raw: float
    score_calibrated: float
    flag: bool  # flag = calibrated < threshold


class ScoreEdgesResp(B):
    model_version: str
    scores: list[EdgeScore]


class PredictMissingReq(B):
    as_of: date
    anchor_node_ids: list[str]
    exclude_node_ids: list[str] = []
    top_k: int = 10


class MissingPrediction(B):
    node_id: str
    score: float
    anchor_links: list[EdgeProbe]


class PredictMissingResp(B):
    model_version: str
    items: list[MissingPrediction]


class IngestDocReq(B):
    doc_id: str = Field(pattern=DOC_ID_PATTERN)  # raw bytes already in blob store at raw/<hex>


class ClauseNode(B):
    paragraph_id: str
    clause_path: str
    number: str | None
    heading: str | None
    text: str
    clause_type: str | None
    type_confidence: float | None
    children: list[ClauseNode] = []


class DefinedTerm(B):
    term: str
    paragraph_id: str
    start: int
    end: int


class CrossRef(B):
    from_paragraph_id: str
    start: int
    end: int
    target_clause_path: str | None
    resolved: bool


class DocRecord(B):
    doc_id: str
    media_type: str
    clauses: list[ClauseNode]
    defined_terms: list[DefinedTerm]
    cross_refs: list[CrossRef]
    ocr_used: bool


class CreateReviewReq(B):
    doc_id: str
    playbook_id: str
    matter: Matter
    run_id: str | None = None
    dossier: Dossier | None = (
        None  # if present, Workbench grounds edits in it instead of re-querying
    )


class ReviewStatus(B):
    review_id: str
    state: Literal["queued", "running", "ready", "failed"]
    edits: list[ProposedEdit]
    consistency_report: dict[str, Any]
    error: str | None = None


class EditDecisionReq(B):
    decision: Literal["accept", "reject", "edit"]
    edited_new_text: str | None = None
    reviewer: str
