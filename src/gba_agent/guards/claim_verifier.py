"""Independent canonical text, quotation, pincite and support checks (Doc 01 §9.4)."""

from __future__ import annotations

import json
import math
import re
import unicodedata
from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal

from rapidfuzz.fuzz import partial_ratio_alignment

from gba_agent.config import Settings
from gba_agent.guards import reason_codes as rc
from gba_agent.guards.support_judge import (
    JudgeUnavailable,
    NLIPredictor,
    SupportJudge,
    build_support_judge,
)

if TYPE_CHECKING:
    from gba_agent.access.fetcher import BlobStore
    from gba_agent.api.contracts_compat import _CompatClaim as Claim
    from gba_agent.api.contracts_compat import _CompatGuardResult as GuardResult
    from gba_agent.clients.llm_contract import LLMClient
else:
    from gba_agent.api.contracts_compat import ClaimModel as Claim
    from gba_agent.api.contracts_compat import GuardResult

guard_version = "1.0.0"

_DOC_ID = re.compile(r"sha256:([0-9a-f]{64})")
_PUNCTUATION = str.maketrans(
    {
        "‘": "'",
        "’": "'",
        "‚": "'",
        "‛": "'",
        "“": '"',
        "”": '"',
        "„": '"',
        "‟": '"',
        "–": "-",
        "—": "-",
    }
)
_PAGE = re.compile(r"(?:at\s+)?([0-9]{1,9})(?:\s*-\s*([0-9]{1,9}))?", re.I)
_PARA = re.compile(r"para\.\s*([0-9]{1,9})", re.I)
_PARA_MARKER = re.compile(r"^[ \t]*(?:¶[ \t]*|para(?:graph)?\.?[ \t]+)([0-9]{1,9})\b", re.I | re.M)
_SECTION_ID = r"[0-9]+[a-z]*(?:\([a-z0-9]+\))*"
_SECTION = re.compile(rf"section\s+({_SECTION_ID})", re.I)
_SECTION_MARKER = re.compile(rf"(?<!\w)(?:§\s*|section\s+)({_SECTION_ID})(?![\w(])", re.I)

_UNKNOWN = {rc.CV_TEXT_UNAVAILABLE, rc.CV_PINCITE_UNCHECKABLE, rc.CV_SUPPORT_UNCERTAIN}
_REASON_TEXT = {
    rc.CV_OK: "The cited evidence passed verification.",
    rc.CV_TEXT_UNAVAILABLE: "The claim lacks usable canonical text or quotation evidence.",
    rc.CV_SPAN_INVALID: "The span identifies a different document or has invalid Unicode offsets.",
    rc.CV_SPAN_QUOTE_MISMATCH: "The span quotation differs from the exact canonical slice.",
    rc.CV_QUOTE_NOT_FOUND: "The quotation does not match the cited canonical text.",
    rc.CV_PINCITE_WRONG: "The passage does not fall within the stated pincite.",
    rc.CV_PINCITE_UNCHECKABLE: "The pincite cannot be verified from the available markers.",
    rc.CV_SUPPORT_UNCERTAIN: "Support could not be established at the configured threshold.",
    rc.CV_UNSUPPORTED: "The cited passage does not establish the proposition.",
    rc.CV_CONTRADICTED: "The cited passage contradicts the proposition.",
}


@dataclass(frozen=True)
class _Offsets:
    start: int
    end: int

    def overlaps(self, other: _Offsets) -> bool:
        return self.start < other.end and other.start < self.end


@dataclass(frozen=True)
class _Normalized:
    text: str
    source: list[_Offsets]

    def offsets(self, start: int, end: int) -> _Offsets:
        return _Offsets(self.source[start].start, self.source[end - 1].end)


def _normalize_text(text: str) -> _Normalized:
    # V1 guarantees NFC, so each normalized non-space character keeps its original index.
    chars: list[str] = []
    positions: list[_Offsets] = []
    for index, char in enumerate(text.translate(_PUNCTUATION)):
        if char.isspace():
            if not chars:
                continue
            if chars[-1] == " ":
                positions[-1] = _Offsets(positions[-1].start, index + 1)
                continue
            char = " "
        chars.append(char)
        positions.append(_Offsets(index, index + 1))
    if chars and chars[-1] == " ":
        chars.pop()
        positions.pop()
    return _Normalized("".join(chars), positions)


def _normalize_quote(quote: str) -> str:
    return " ".join(unicodedata.normalize("NFC", quote).translate(_PUNCTUATION).split())


def _find_quote(quote: str, text: _Normalized, span: _Offsets | None) -> _Offsets | None:
    if not quote:
        return None
    cursor = 0
    while (start := text.text.find(quote, cursor)) >= 0:
        match = text.offsets(start, start + len(quote))
        if span is None or match.overlaps(span):
            return match
        cursor = start + 1
    return None


def _fuzzy_alignment(quote: str, text: _Normalized) -> dict[str, object]:
    if not quote or not text.text:
        return {}
    alignment = partial_ratio_alignment(quote, text.text)
    if alignment is None or alignment.dest_start >= alignment.dest_end:
        return {}
    target = text.offsets(alignment.dest_start, alignment.dest_end)
    return {"score": alignment.score, "start": target.start, "end": target.end}


def _passage(text: str, span: _Offsets) -> str:
    start = max(0, span.start - 600)
    end = min(len(text), span.end + 600)
    # Canonical HTML and DOCX can use a single newline between paragraphs.
    for boundary in ("\n", "\f"):
        before = text.rfind(boundary, start, span.start)
        if before >= 0:
            start = before + 1
        after = text.find(boundary, span.end, end)
        if after >= 0:
            end = after
    return text[start:end]


class ClaimVerifier:
    def __init__(
        self,
        blobs: BlobStore,
        judge: SupportJudge | None = None,
        *,
        support_min_score: float = 0.7,
    ) -> None:
        if not math.isfinite(support_min_score) or not 0 <= support_min_score <= 1:
            raise ValueError("Support threshold must be between zero and one")
        self._blobs, self._judge, self._threshold = blobs, judge, support_min_score

    @classmethod
    def configured(
        cls,
        blobs: BlobStore,
        settings: Settings,
        *,
        client: LLMClient | None = None,
        nli_predictor: NLIPredictor | None = None,
    ) -> ClaimVerifier:
        return cls(
            blobs,
            build_support_judge(settings, client=client, nli_predictor=nli_predictor),
            support_min_score=settings.support_min_score,
        )

    def _load_text(self, digest: str) -> str | None:
        try:
            text = self._blobs.get(f"canon/{digest}.v1.txt").decode("utf-8")
        except (KeyError, OSError, UnicodeError):
            return None
        if not text.strip() or not unicodedata.is_normalized("NFC", text):
            return None
        return text

    @staticmethod
    def _result(
        reason: str, evidence: dict[str, object], *, deterministic: bool = True
    ) -> GuardResult:
        verdict: Literal["pass", "block", "unknown"] = "block"
        if reason == rc.CV_OK:
            verdict = "pass"
        elif reason in _UNKNOWN:
            verdict = "unknown"
        return GuardResult(
            guard="claim_verifier",
            verdict=verdict,
            reason_code=reason,
            reason_text=_REASON_TEXT[reason],
            evidence=evidence,
            deterministic=deterministic,
            guard_version=guard_version,
        )

    def _page_at(self, digest: str, text: str, offset: int) -> int | None:
        try:
            data = json.loads(self._blobs.get(f"canon/{digest}.v1.pages.json"))
        except (KeyError, OSError, UnicodeError, ValueError):
            return None
        if not isinstance(data, list):
            return None
        previous = -1
        page = None
        for marker in data:
            if not isinstance(marker, dict):
                return None
            start, label = marker.get("start"), marker.get("label")
            if type(start) is not int or not previous < start <= len(text):
                return None
            if not isinstance(label, str) or not label.strip():
                return None
            previous = start
            if start <= offset:
                page = int(label) if re.fullmatch(r"[0-9]{1,9}", label) else None
        return page

    def _pincite(self, value: str, digest: str, text: str, span: _Offsets | None) -> str:
        if span is None:
            return rc.CV_PINCITE_UNCHECKABLE
        value = value.translate(_PUNCTUATION).strip()
        if pages := _PAGE.fullmatch(value):
            first, last = int(pages[1]), int(pages[2] or pages[1])
            if first > last:
                return rc.CV_PINCITE_WRONG
            page = self._page_at(digest, text, span.start)
            if page is None:
                return rc.CV_PINCITE_UNCHECKABLE
            return rc.CV_OK if first <= page <= last else rc.CV_PINCITE_WRONG
        if paragraph := _PARA.fullmatch(value):
            preceding = None
            for marker in _PARA_MARKER.finditer(text, 0, span.start):
                preceding = marker[1]
            if preceding is None:
                return rc.CV_PINCITE_UNCHECKABLE
            return rc.CV_OK if int(preceding) == int(paragraph[1]) else rc.CV_PINCITE_WRONG
        if section := _SECTION.fullmatch(value):
            markers = {
                marker[1].casefold()
                for marker in _SECTION_MARKER.finditer(text, max(0, span.start - 2000), span.start)
            }
            if not markers:
                return rc.CV_PINCITE_UNCHECKABLE
            return rc.CV_OK if section[1].casefold() in markers else rc.CV_PINCITE_WRONG
        return rc.CV_PINCITE_UNCHECKABLE

    async def verify(self, claim: Claim) -> GuardResult:
        evidence: dict[str, object] = {"source_doc_id": claim.source_doc_id}
        # V1 and the citation-only path in V6.
        doc_id = _DOC_ID.fullmatch(claim.source_doc_id or "")
        if doc_id is None or (text := self._load_text(doc_id[1])) is None:
            return self._result(rc.CV_TEXT_UNAVAILABLE, evidence)
        if claim.kind == "citation":
            return self._result(rc.CV_OK, evidence)

        # V2 binds the span to the same document before slicing Python code points.
        span = None
        quote = claim.quote
        pincites = [claim.pincite]
        if claim.span is not None:
            span = _Offsets(claim.span.start, claim.span.end)
            if claim.span.doc_id != claim.source_doc_id or not 0 <= span.start < span.end <= len(
                text
            ):
                return self._result(rc.CV_SPAN_INVALID, evidence)
            evidence["span"] = {"start": span.start, "end": span.end}
            if claim.span.quote is not None and claim.span.quote != text[span.start : span.end]:
                return self._result(rc.CV_SPAN_QUOTE_MISMATCH, evidence)
            if quote is None:
                quote = claim.span.quote
            pincites.append(claim.span.pincite)

        # V3 permits only a normalized exact match. Fuzzy alignment is diagnostic.
        if quote is not None:
            normalized = _normalize_text(text)
            target = _normalize_quote(quote)
            exact = _find_quote(target, normalized, span)
            evidence["quote_exact"] = exact is not None
            if exact is None:
                if alignment := _fuzzy_alignment(target, normalized):
                    evidence["human_only"] = {"fuzzy_match": alignment}
                return self._result(rc.CV_QUOTE_NOT_FOUND, evidence)
            evidence["quote_offsets"] = {"start": exact.start, "end": exact.end}
            span = span or exact

        # V4 verifies both pincites when the claim and its span each supply one.
        results = {
            self._pincite(pincite, doc_id[1], text, span)
            for pincite in dict.fromkeys(pincites)
            if pincite is not None
        }
        if rc.CV_PINCITE_WRONG in results:
            return self._result(rc.CV_PINCITE_WRONG, evidence)
        if rc.CV_PINCITE_UNCHECKABLE in results:
            return self._result(rc.CV_PINCITE_UNCHECKABLE, evidence)

        # V6 skips proposition support for a verified quotation.
        if claim.kind == "quotation":
            return self._result(rc.CV_OK if quote is not None else rc.CV_TEXT_UNAVAILABLE, evidence)

        # V5 judges only the located passage, never planner reasoning or the whole document.
        if span is None or self._judge is None:
            return self._result(rc.CV_SUPPORT_UNCERTAIN, evidence)
        passage = _passage(text, span)
        if not text[span.start : span.end].strip() or not claim.text.strip():
            return self._result(rc.CV_SUPPORT_UNCERTAIN, evidence)
        deterministic = getattr(self._judge, "deterministic", False) is True
        try:
            judgment = await self._judge.judge(proposition=claim.text, passage=passage)
        except JudgeUnavailable:
            return self._result(rc.CV_SUPPORT_UNCERTAIN, evidence, deterministic=deterministic)
        evidence["judge_label"] = judgment.label
        evidence["judge_score"] = judgment.score
        if judgment.label == "contradicts":
            return self._result(rc.CV_CONTRADICTED, evidence, deterministic=deterministic)
        if judgment.label == "neutral":
            return self._result(rc.CV_UNSUPPORTED, evidence, deterministic=deterministic)
        return self._result(
            rc.CV_OK if judgment.score >= self._threshold else rc.CV_SUPPORT_UNCERTAIN,
            evidence,
            deterministic=deterministic,
        )
