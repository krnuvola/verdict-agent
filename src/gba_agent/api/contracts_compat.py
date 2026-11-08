"""Temporary Track A API request models used until ``gba_contracts`` is available.

Keep all compatibility definitions in this module so they can be deleted when
the shared package publishes the route models.
"""

from __future__ import annotations

from datetime import date, datetime
from importlib import import_module
from types import ModuleType
from typing import TYPE_CHECKING, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator
from pydantic_core import PydanticCustomError

_shared_models: ModuleType | None
ActionClass: Any
BrowserAction: Any
ActionDecision: Any
Matter: Any
Claim: Any
Dossier: Any


class _CompatHandoffRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    handoff_id: str
    run_id: str
    site_id: str
    reason: Literal["login", "mfa", "captcha", "consent", "payment", "commit_step", "other"]
    instructions: str
    viewer_url: str | None = None
    status: Literal["open", "resolved", "abandoned", "expired"] = "open"
    created_at: datetime
    resolved_at: datetime | None = None
    resolved_by: str | None = None


class _CompatSourceRecord(BaseModel):
    model_config = ConfigDict(extra="forbid")

    source_id: str
    url: str
    registry_entry_id: str | None = None
    retrieved_at: datetime
    session_id: str | None = None
    doc_id: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    media_type: str
    byte_length: int
    http_status: int | None = None
    fetch_method: Literal["api", "bulk", "adapter", "operator", "human"]
    retrieved_by: str
    expected_doc_type: str | None = None
    version_label: str | None = None
    canon_version: str | None = None


class _CompatSpan(BaseModel):
    model_config = ConfigDict(extra="forbid")

    doc_id: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    start: int = Field(ge=0)
    end: int = Field(ge=0)
    quote: str | None = Field(default=None, max_length=500)
    pincite: str | None = None


class _CompatMatterFact(BaseModel):
    model_config = ConfigDict(extra="forbid")

    key: str
    value: Any = None
    status: Literal["known", "unknown", "conflicting"] = "unknown"
    spans: list[_CompatSpan] = Field(default_factory=list)
    extracted_by: Literal["registry", "llm_extracted", "human", "seed"] = "llm_extracted"
    confidence: float | None = Field(default=None, ge=0, le=1)
    reported_date: date | None = None


class _CompatMatter(BaseModel):
    model_config = ConfigDict(extra="forbid")

    matter_id: str
    title: str
    task: Literal["research", "review", "draft"]
    question: str
    as_of: date
    jurisdiction: str = "US-DE"
    facts: dict[str, _CompatMatterFact] = Field(default_factory=dict)
    source_doc_ids: list[str] = Field(default_factory=list)
    created_at: datetime


class _CompatClaim(BaseModel):
    model_config = ConfigDict(extra="forbid")

    claim_id: str
    matter_id: str
    kind: Literal["legal_proposition", "fact", "quotation", "citation"]
    text: str = Field(max_length=2000)
    authority_node_id: str | None = None
    element_node_id: str | None = None
    source_doc_id: str | None = Field(default=None, pattern=r"^sha256:[0-9a-f]{64}$")
    source_id: str | None = None
    quote: str | None = None
    span: _CompatSpan | None = None
    pincite: str | None = None
    as_of: date
    proposed_by: str


class _CompatGuardResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    guard: Literal[
        "access",
        "action_guard",
        "source_gate",
        "scope",
        "temporal",
        "claim_verifier",
        "gnn_alert",
        "abstention",
    ]
    verdict: Literal["pass", "block", "unknown", "flag"]
    reason_code: str
    reason_text: str
    evidence: dict[str, Any] = Field(default_factory=dict)
    deterministic: bool
    guard_version: str
    duration_ms: int = 0


class _CompatGuardReport(BaseModel):
    model_config = ConfigDict(extra="forbid")

    claim_id: str
    results: list[_CompatGuardResult]
    final: Literal["pass", "block", "unknown"]
    abstain_reason: str | None = None
    missing_facts: list[str] = Field(default_factory=list)


class _CompatStandard(BaseModel):
    model_config = ConfigDict(extra="forbid")
    value: Literal["business_judgment", "entire_fairness", "unknown"]
    safe_harbor: Literal["true", "false", "unknown"]
    basis: list[str]
    readings: list[dict[str, Any]] = Field(default_factory=list)


class _CompatCondition(BaseModel):
    model_config = ConfigDict(extra="forbid")
    condition_id: str
    label: str
    status: Literal["met", "unmet", "unknown"]
    fact_keys: list[str]
    spans: list[_CompatSpan] = Field(default_factory=list)
    reason: str


class _CompatAuthority(BaseModel):
    model_config = ConfigDict(extra="forbid")
    node_id: str
    label: str
    proposition: str
    guard_report: _CompatGuardReport
    spans: list[_CompatSpan]
    source_id: str


class _CompatProposedEdit(BaseModel):
    model_config = ConfigDict(extra="forbid")
    edit_id: str
    document_id: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    op: Literal["replace", "insert", "delete"]
    clause_path: str
    paragraph_id: str
    start: int = Field(ge=0)
    end: int = Field(ge=0)
    old_text: str
    new_text: str
    rationale: str
    authority_claim_ids: list[str] = Field(default_factory=list)
    guard_reports: list[_CompatGuardReport] = Field(default_factory=list)
    playbook_rule_id: str | None = None
    risk: Literal["low", "medium", "high"]
    status: Literal["proposed", "accepted", "rejected", "edited"] = "proposed"
    reviewer: str | None = None


class _CompatDossier(BaseModel):
    model_config = ConfigDict(extra="forbid")
    run_id: str
    matter: _CompatMatter
    generated_at: datetime
    standard_of_review: _CompatStandard
    conditions: list[_CompatCondition]
    authorities: list[_CompatAuthority]
    dropped_claims: list[_CompatGuardReport]
    open_questions: list[str]
    abstentions: list[dict[str, Any]]
    edits: list[_CompatProposedEdit] = Field(default_factory=list)
    trajectory_path: str
    disclaimer: str = "Research dossier for review by a licensed lawyer. Not legal advice."


class _CompatAdmission(BaseModel):
    """Internal Doc 01 result pending a shared Admission schema."""

    model_config = ConfigDict(extra="forbid")

    admitted: bool
    reason_code: str


SourceRecord: type[_CompatSourceRecord]
HandoffRequest: type[_CompatHandoffRequest]
ClaimModel: type[_CompatClaim]
MatterModel: type[_CompatMatter]
MatterFactModel: type[_CompatMatterFact]
GuardResult: type[_CompatGuardResult]
GuardReport: type[_CompatGuardReport]
Admission: type[_CompatAdmission]
try:
    _shared_models = import_module("gba_contracts.models")
except ModuleNotFoundError as exc:
    if exc.name not in {"gba_contracts", "gba_contracts.models"}:
        raise
    _shared_models = None


if _shared_models is None:
    ActionClass = Literal["READ", "SEARCH", "LOGIN", "SPEND", "UPLOAD", "COMMIT"]

    class _CompatElementRef(BaseModel):
        model_config = ConfigDict(extra="forbid")

        index: int | None = None
        role: str | None = None
        name: str | None = None
        tag: str | None = None
        input_type: str | None = None
        href: str | None = None
        form_method: str | None = None
        form_action: str | None = None
        text: str | None = None
        selector: str | None = None

    class _CompatBrowserAction(BaseModel):
        model_config = ConfigDict(extra="forbid")

        action_type: Literal[
            "navigate",
            "click",
            "type",
            "select",
            "press_key",
            "scroll",
            "download",
            "upload",
            "wait",
            "back",
            "forward",
            "new_tab",
            "close_tab",
        ]
        target: _CompatElementRef | None = None
        value: str | None = None
        page_url: str
        site_id: str

    class _CompatActionDecision(BaseModel):
        model_config = ConfigDict(extra="forbid")

        action_class: ActionClass
        allowed: bool
        requires_human: bool
        reason_code: str
        reason_text: str

    BrowserAction = _CompatBrowserAction
    ActionDecision = _CompatActionDecision
    Matter = dict[str, Any]
    Claim = dict[str, Any]
    Dossier = _CompatDossier
    HandoffRequest = _CompatHandoffRequest
    SourceRecord = _CompatSourceRecord
    ClaimModel = _CompatClaim
    MatterModel = _CompatMatter
    MatterFactModel = _CompatMatterFact
    GuardResult = _CompatGuardResult
    GuardReport = _CompatGuardReport
    Admission = _CompatAdmission
else:

    def _shared_model(name: str) -> Any:
        return getattr(_shared_models, name)

    ActionClass = _shared_model("ActionClass")
    BrowserAction = _shared_model("BrowserAction")
    ActionDecision = _shared_model("ActionDecision")
    Matter = _shared_model("Matter")
    Claim = _shared_model("Claim")
    Dossier = _shared_model("Dossier")
    HandoffRequest = _shared_model("HandoffRequest")
    SourceRecord = _shared_model("SourceRecord")
    ClaimModel = _shared_model("Claim")
    MatterModel = _shared_model("Matter")
    MatterFactModel = _shared_model("MatterFact")
    GuardResult = _shared_model("GuardResult")
    GuardReport = _shared_model("GuardReport")
    Admission = getattr(_shared_models, "Admission", _CompatAdmission)


class APIModel(BaseModel):
    """Request-model base matching the shared contract's extra-field policy."""

    model_config = ConfigDict(extra="forbid")


class RunConfig(APIModel):
    guards_enabled: list[str] = Field(
        default_factory=lambda: [
            "access",
            "source_gate",
            "claim_verifier",
            "scope",
            "temporal",
            "gnn",
        ]
    )
    max_steps: int = Field(default=60, gt=0)
    max_cost_usd: float = Field(default=10.0, ge=0)
    max_wall_seconds: int = Field(default=3600, gt=0)
    dry_run: bool = False
    use_workbench: bool = False
    fault_proxy_url: str | None = None


if TYPE_CHECKING:
    _VerifyMatter = _CompatMatter
    _VerifyClaim = _CompatClaim
    _VerifyReport = _CompatGuardReport
else:
    _VerifyMatter = MatterModel
    _VerifyClaim = ClaimModel
    _VerifyReport = GuardReport


class CreateRunReq(APIModel):
    matter: _VerifyMatter
    config: RunConfig = Field(default_factory=RunConfig)
    draft_doc_id: str | None = None
    playbook_id: str | None = None


class CreateRunResp(APIModel):
    run_id: str


class RunStatus(APIModel):
    run_id: str
    state: Literal["queued", "running", "waiting_handoff", "done", "failed", "cancelled"]
    step: str
    steps_used: int
    cost_usd: float
    open_handoffs: list[str]
    error: str | None = None


class VerifyClaimsReq(APIModel):
    matter: _VerifyMatter
    claims: list[_VerifyClaim] = Field(max_length=50)
    guards_enabled: list[str] | None = None

    @model_validator(mode="before")
    @classmethod
    def check_batch_limit(cls, value: Any) -> Any:
        claims = value.get("claims") if isinstance(value, dict) else None
        if isinstance(claims, list) and len(claims) > 50:
            raise PydanticCustomError(
                "too_many_claims",
                "A verify request cannot contain more than 50 claims",
                {"limit": 50, "received": len(claims)},
            )
        return value


class VerifyClaimsResp(APIModel):
    reports: list[_VerifyReport]


class ClassifyActionReq(APIModel):
    action: BrowserAction


class ResolveHandoffReq(APIModel):
    resolved_by: str
    outcome: Literal["resolved", "abandoned"]


class ErrorBody(APIModel):
    code: str
    message: str
    details: dict[str, Any] = Field(default_factory=dict)
    request_id: str | None = None


class ErrorEnvelope(APIModel):
    error: ErrorBody
