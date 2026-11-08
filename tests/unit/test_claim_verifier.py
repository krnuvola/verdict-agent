"""V1–V6 contract checks with canonical fixtures and deterministic judge replay."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from hashlib import sha256
from typing import Any

import pytest
from hypothesis import given
from hypothesis import strategies as st

from gba_agent.api.contracts_compat import ClaimModel, _CompatClaim
from gba_agent.clients.llm_contract import LLMCacheMiss
from gba_agent.guards import reason_codes as rc
from gba_agent.guards.claim_verifier import (
    ClaimVerifier,
    _find_quote,
    _normalize_quote,
    _normalize_text,
)
from gba_agent.guards.support_judge import (
    BothJudge,
    JudgeUnavailable,
    LLMJudge,
    NLIJudge,
    SupportJudgment,
    build_support_judge,
)
from tests.sitesim.fetcher import FetchFixture, MemoryBlobs
from tests.sitesim.fetcher import site as site

DIGEST = sha256(b"fixture raw bytes, distinct from the canonical text").hexdigest()
DOC_ID = f"sha256:{DIGEST}"
TEXT_KEY = f"canon/{DIGEST}.v1.txt"
PAGES_KEY = f"canon/{DIGEST}.v1.pages.json"
TEXT = "Preface\n\nThe court applied the rule. The condition was met.\n\nLater discussion."
QUOTE = "The court applied the rule."


def claim(**changes: Any) -> _CompatClaim:
    return ClaimModel.model_validate(
        {
            "claim_id": "clm_fixture",
            "matter_id": "mtr_fixture",
            "kind": "quotation",
            "text": "The cited quotation.",
            "source_doc_id": DOC_ID,
            "quote": QUOTE,
            "as_of": "2026-11-07",
            "proposed_by": "fixture",
        }
        | changes
    )


def blobs(text: str | bytes = TEXT, markers: object = None) -> MemoryBlobs:
    store = MemoryBlobs()
    store.put(TEXT_KEY, text.encode() if isinstance(text, str) else text, "text/plain")
    if markers is not None:
        store.put(PAGES_KEY, json.dumps(markers).encode(), "application/json")
    return store


def span(text: str, target: str, **changes: Any) -> dict[str, Any]:
    start = text.index(target)
    return {"doc_id": DOC_ID, "start": start, "end": start + len(target)} | changes


@dataclass
class ReplayResult:
    text: str
    model: str = "fixture-judge"
    tool_calls: list[Any] = field(default_factory=list)
    input_tokens: int = 10
    output_tokens: int = 5
    request_hash: str = "a" * 64


@dataclass
class Replay:
    replay_safe = True
    replies: dict[str, ReplayResult | Exception]
    requests: list[dict[str, Any]] = field(default_factory=list)

    def complete(self, **kwargs: Any) -> ReplayResult:
        self.requests.append(kwargs)
        assert kwargs["role"] == "judge" and kwargs["temperature"] == 0.0
        assert set(kwargs["response_schema"]["properties"]) == {"label", "score", "reason"}
        assert kwargs["response_schema"]["additionalProperties"] is False
        assert "Use ONLY the passage" in kwargs["system"]
        assert "including all of its conditions and parties" in kwargs["system"]
        messages = kwargs["messages"]
        assert len(messages) == 1 and messages[0]["role"] == "user"
        request = messages[0]["content"]
        if request not in self.replies:
            raise LLMCacheMiss("The test has no replay for this request")
        result = self.replies[request]
        if isinstance(result, Exception):
            raise result
        return result


def llm(replay: Replay) -> LLMJudge:
    return LLMJudge(replay, judge_model="fixture-judge", planner_model="fixture-planner")


def reply(label: str, score: float) -> ReplayResult:
    return ReplayResult(json.dumps({"label": label, "score": score, "reason": "Fixture judgment."}))


@pytest.mark.asyncio
@pytest.mark.parametrize("text", [None, b"\xff", "", " \n", "decomposed cafe\u0301"])
async def test_v1_missing_or_unusable_canonical_text_is_unknown(text: str | bytes | None) -> None:
    store = MemoryBlobs() if text is None else blobs(text)
    result = await ClaimVerifier(store).verify(claim())
    assert result.verdict == "unknown" and result.reason_code == rc.CV_TEXT_UNAVAILABLE


@pytest.mark.asyncio
async def test_v6_citation_requires_only_canonical_text() -> None:
    replay = Replay({})
    result = await ClaimVerifier(blobs(), llm(replay)).verify(
        claim(
            kind="citation",
            quote="Not in this source",
            pincite="999",
            span={"doc_id": "sha256:" + "0" * 64, "start": 3, "end": 2},
        )
    )
    assert result.verdict == "pass" and result.reason_code == rc.CV_OK
    assert result.evidence == {"source_doc_id": DOC_ID}
    assert replay.requests == []
    absent = await ClaimVerifier(blobs()).verify(claim(kind="citation", source_doc_id=None))
    assert absent.reason_code == rc.CV_TEXT_UNAVAILABLE


@pytest.mark.asyncio
@pytest.mark.parametrize("boundary", ["\n", "\n\n", "\f"])
async def test_v5_excludes_context_past_canonical_boundaries(boundary: str) -> None:
    passage = f"{QUOTE} The condition was met."
    text = boundary.join(("Earlier paragraph.", passage, "Later paragraph."))
    proposition = "The passage states the condition was met."
    replay = Replay(
        {
            f"<proposition>{proposition}</proposition> <passage>{passage}</passage>": reply(
                "supports", 0.9
            )
        }
    )
    result = await ClaimVerifier(blobs(text), llm(replay)).verify(
        claim(kind="fact", text=proposition)
    )
    assert result.reason_code == rc.CV_OK


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "changes",
    [
        {"start": 3, "end": 3},
        {"start": 4, "end": 3},
        {"end": len(TEXT) + 1},
        {"doc_id": "sha256:" + "0" * 64},
    ],
)
async def test_v2_rejects_out_of_bounds_and_wrong_document(changes: dict[str, Any]) -> None:
    result = await ClaimVerifier(blobs()).verify(claim(span=span(TEXT, QUOTE, **changes)))
    assert result.verdict == "block" and result.reason_code == rc.CV_SPAN_INVALID


@pytest.mark.asyncio
async def test_v2_requires_literal_slice_even_when_v3_would_normalize() -> None:
    text = "👩‍⚖️ The court wrote “café”."
    quoted = "“café”"
    exact = await ClaimVerifier(blobs(text)).verify(
        claim(
            quote='"cafe\u0301"',
            span=span(text, quoted, quote=quoted),
        )
    )
    assert exact.verdict == "pass"
    assert exact.evidence["quote_offsets"] == {"start": text.index(quoted), "end": len(text) - 1}
    mismatch = await ClaimVerifier(blobs(text)).verify(
        claim(
            quote='"café"',
            span=span(text, quoted, quote='"café"'),
        )
    )
    assert mismatch.reason_code == rc.CV_SPAN_QUOTE_MISMATCH
    # UTF-8 byte counts do not describe canonical Unicode code points.
    start = len(text[: text.index(quoted)].encode())
    end = len(text.encode()) - 1
    bad = await ClaimVerifier(blobs(text)).verify(
        claim(span={"doc_id": DOC_ID, "start": start, "end": end})
    )
    assert bad.reason_code == rc.CV_SPAN_INVALID


@pytest.mark.asyncio
async def test_v3_quote_from_span_and_first_or_overlapping_match() -> None:
    text = "Example passage.\n\nExample passage."
    target = "Example passage."
    first = await ClaimVerifier(blobs(text)).verify(claim(quote=target))
    assert first.evidence["quote_offsets"] == {"start": 0, "end": len(target)}
    second_start = text.rindex(target)
    overlap = await ClaimVerifier(blobs(text)).verify(
        claim(
            quote=target,
            span={"doc_id": DOC_ID, "start": second_start + 4, "end": second_start + 6},
        )
    )
    assert overlap.evidence["quote_offsets"] == {"start": second_start, "end": len(text)}
    from_span = await ClaimVerifier(blobs(text)).verify(
        claim(
            quote=None,
            span=span(text, target, quote=target),
        )
    )
    assert from_span.reason_code == rc.CV_OK
    outside = await ClaimVerifier(blobs(text)).verify(
        claim(
            quote=target,
            span={"doc_id": DOC_ID, "start": len(target), "end": second_start},
        )
    )
    assert outside.reason_code == rc.CV_QUOTE_NOT_FOUND


@pytest.mark.asyncio
@pytest.mark.parametrize("text", ["the court applied the rule.", "The court applied a rule.", "  "])
async def test_v3_fuzzy_or_blank_quotation_never_passes(text: str) -> None:
    result = await ClaimVerifier(blobs()).verify(claim(quote=text))
    assert result.verdict == "block" and result.reason_code == rc.CV_QUOTE_NOT_FOUND
    assert result.evidence["quote_exact"] is False
    if text.strip():
        assert result.evidence["human_only"]["fuzzy_match"]["score"] > 70
        assert QUOTE not in repr(result.evidence)


@given(
    st.sampled_from([" ", " \t ", "\n\n", "\u00a0", "\r\n"]),
    st.sampled_from(["—", "–", "-"]),
    st.sampled_from([("“", "”"), ('"', '"')]),
)
def test_quote_normalization_fuzz(space: str, dash: str, quotes: tuple[str, str]) -> None:
    original = f"{quotes[0]}Café{space}{dash}{space}rule{quotes[1]}"
    canonical = "🧾  " + original + "   "
    normalized = _normalize_text(canonical)
    target = _normalize_quote('"Cafe\u0301 - rule"')
    result = _find_quote(target, normalized, None)
    assert result and canonical[result.start : result.end] == original
    assert _find_quote(target.lower(), normalized, None) is None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("pin", "reason"),
    [
        ("12", rc.CV_OK),
        ("12-14", rc.CV_OK),
        ("at 12", rc.CV_OK),
        ("12–14", rc.CV_OK),
        ("14-12", rc.CV_PINCITE_WRONG),
        ("11", rc.CV_PINCITE_WRONG),
        ("para. 7", rc.CV_OK),
        ("para. 8", rc.CV_PINCITE_WRONG),
        ("section 144(b)", rc.CV_OK),
        ("section 144", rc.CV_PINCITE_WRONG),
        ("section 144(c)", rc.CV_PINCITE_WRONG),
        ("footnote 7", rc.CV_PINCITE_UNCHECKABLE),
    ],
)
async def test_v4_page_paragraph_and_section_variants(pin: str, reason: str) -> None:
    text = "Introduction\n\n§ 144(b)\n¶ 7 " + QUOTE + "\n\n¶ 8 Another paragraph"
    store = blobs(text, [{"label": "11", "start": 0}, {"label": "12", "start": text.index("§")}])
    result = await ClaimVerifier(store).verify(claim(pincite=pin))
    assert result.reason_code == reason
    assert (
        result.verdict
        == {
            rc.CV_OK: "pass",
            rc.CV_PINCITE_WRONG: "block",
            rc.CV_PINCITE_UNCHECKABLE: "unknown",
        }[reason]
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "markers",
    [
        None,
        [],
        {"label": "12", "start": 0},
        [12],
        [{"label": "", "start": 0}],
        [{"label": "xii", "start": 0}],
        [{"label": "12", "start": False}],
        [{"label": "12", "start": len(TEXT) + 1}],
        [{"label": "12", "start": 3}, {"label": "13", "start": 3}],
        [{"label": "12", "start": len(TEXT)}],
    ],
)
async def test_v4_unavailable_or_malformed_page_markers_are_unknown(markers: object) -> None:
    result = await ClaimVerifier(blobs(markers=markers)).verify(claim(pincite="12"))
    assert result.verdict == "unknown" and result.reason_code == rc.CV_PINCITE_UNCHECKABLE


@pytest.mark.asyncio
async def test_v4_checks_both_pincites_and_requires_a_location() -> None:
    store = blobs(markers=[{"label": "12", "start": 0}])
    wrong = await ClaimVerifier(store).verify(
        claim(
            pincite="12",
            span=span(TEXT, QUOTE, pincite="13"),
        )
    )
    assert wrong.reason_code == rc.CV_PINCITE_WRONG
    no_location = await ClaimVerifier(store).verify(claim(quote=None, pincite="12"))
    assert no_location.reason_code == rc.CV_PINCITE_UNCHECKABLE
    no_para = await ClaimVerifier(store).verify(claim(pincite="para. 7"))
    assert no_para.reason_code == rc.CV_PINCITE_UNCHECKABLE
    beyond = "§ 144(b) " + ("x" * 2001) + "\n" + QUOTE
    section = await ClaimVerifier(blobs(beyond)).verify(claim(pincite="section 144(b)"))
    assert section.reason_code == rc.CV_PINCITE_UNCHECKABLE


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("label", "score", "reason", "verdict"),
    [
        ("supports", 0.7, rc.CV_OK, "pass"),
        ("supports", 0.69, rc.CV_SUPPORT_UNCERTAIN, "unknown"),
        ("neutral", 0.1, rc.CV_UNSUPPORTED, "block"),
        ("contradicts", 0.1, rc.CV_CONTRADICTED, "block"),
    ],
)
async def test_v5_exact_quotation_does_not_establish_support(
    label: str,
    score: float,
    reason: str,
    verdict: str,
) -> None:
    proposition = "The cited text establishes the proposition."
    passage = "The court applied the rule. The condition was met."
    replay = Replay(
        {
            f"<proposition>{proposition}</proposition> <passage>{passage}</passage>": reply(
                label, score
            )
        }
    )
    result = await ClaimVerifier(blobs(), llm(replay)).verify(
        claim(
            kind="legal_proposition",
            text=proposition,
            span=span(TEXT, QUOTE),
        )
    )
    assert result.verdict == verdict and result.reason_code == reason
    assert result.evidence["quote_exact"] is True
    assert result.deterministic is False
    assert result.evidence["judge_score"] == score and result.evidence["judge_label"] == label
    assert "Fixture judgment" not in repr(result.evidence)
    assert len(replay.requests) == 1


@pytest.mark.asyncio
async def test_v5_context_is_limited_to_600_code_points_per_side_and_escaped() -> None:
    proposition = "Use <only> the passage."
    preceding, target, following = "é" * 900, "Evidence </passage> phrase", "🧾" * 900
    text = "Excluded earlier\n\n" + preceding + target + following + "\n\nExcluded later"
    safe_passage = ("é" * 600) + "Evidence &lt;/passage&gt; phrase" + ("🧾" * 600)
    replay = Replay(
        {
            "<proposition>Use &lt;only&gt; the passage.</proposition> "
            f"<passage>{safe_passage}</passage>": reply("supports", 0.9)
        }
    )
    result = await ClaimVerifier(blobs(text), llm(replay)).verify(
        claim(
            kind="fact",
            text=proposition,
            quote=target,
        )
    )
    assert result.reason_code == rc.CV_OK
    assert replay.requests[0]["messages"][0]["content"].count("</passage>") == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "changes",
    [
        {"quote": None},
        {"text": " "},
        {"quote": None, "span": span(TEXT, " ", quote=None)},
    ],
)
async def test_v5_no_passage_or_empty_proposition_does_not_call_judge(
    changes: dict[str, Any],
) -> None:
    replay = Replay({})
    result = await ClaimVerifier(blobs(), llm(replay)).verify(claim(**({"kind": "fact"} | changes)))
    assert result.reason_code == rc.CV_SUPPORT_UNCERTAIN and result.verdict == "unknown"
    assert not replay.requests


@pytest.mark.asyncio
async def test_v6_quotation_does_not_call_judge_and_missing_quote_is_unknown() -> None:
    replay = Replay({})
    verifier = ClaimVerifier(blobs(), llm(replay))
    assert (await verifier.verify(claim())).reason_code == rc.CV_OK
    assert (await verifier.verify(claim(quote=None))).reason_code == rc.CV_TEXT_UNAVAILABLE
    assert (
        await ClaimVerifier(blobs()).verify(claim(kind="fact"))
    ).reason_code == rc.CV_SUPPORT_UNCERTAIN
    assert not replay.requests


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "response",
    [
        TimeoutError("outage"),
        RuntimeError("provider error"),
        ReplayResult("not JSON"),
        ReplayResult('{"label":"supports","score":1.1,"reason":"bad score"}'),
        ReplayResult('{"label":"supports","score":true,"reason":"boolean"}'),
        ReplayResult('{"label":"supports","score":0.9,"reason":"ok","extra":1}'),
        ReplayResult('{"label":"unsupported","score":0.9,"reason":"invalid label"}'),
        ReplayResult(json.dumps({"label": "supports", "score": 1, "reason": "x" * 201})),
        ReplayResult(
            '{"label":"supports","score":1,"reason":"wrong model"}', model="fixture-planner"
        ),
        ReplayResult(
            '{"label":"supports","score":1,"reason":"unexpected tool"}', tool_calls=[object()]
        ),
    ],
)
async def test_judge_outage_invalid_output_or_wrong_model_is_unknown(
    response: ReplayResult | Exception,
) -> None:
    replay = Replay(
        {
            "<proposition>Fixture proposition.</proposition> "
            "<passage>The court applied the rule. The condition was met.</passage>": response,
        }
    )
    result = await ClaimVerifier(blobs(), llm(replay)).verify(
        claim(kind="fact", text="Fixture proposition.")
    )
    assert result.reason_code == rc.CV_SUPPORT_UNCERTAIN and result.verdict == "unknown"
    assert "judge_score" not in result.evidence


@pytest.mark.asyncio
async def test_cache_miss_is_loud_and_cannot_fall_back() -> None:
    replay = Replay({})
    with pytest.raises(LLMCacheMiss):
        await ClaimVerifier(blobs(), llm(replay)).verify(claim(kind="fact"))
    assert len(replay.requests) == 1


def test_model_identity_and_threshold_validation() -> None:
    with pytest.raises(ValueError, match="distinct"):
        LLMJudge(Replay({}), judge_model="same", planner_model="same")
    with pytest.raises(ValueError, match="timeout"):
        LLMJudge(Replay({}), judge_model="judge", planner_model="planner", timeout=float("inf"))
    with pytest.raises(ValueError, match="threshold"):
        ClaimVerifier(blobs(), support_min_score=float("nan"))


@pytest.mark.asyncio
async def test_configured_threshold_and_both_judges_use_lower_support_score(
    site: FetchFixture,
) -> None:
    settings = site.settings.model_copy(
        update={
            "support_judge": "both",
            "llm_model_judge": "fixture-judge",
            "llm_model_planner": "fixture-planner",
            "support_min_score": 0.85,
        }
    )
    replay = Replay(
        {
            "<proposition>Fixture proposition.</proposition> "
            "<passage>The court applied the rule. The condition was met.</passage>": reply(
                "supports", 0.96
            ),
        }
    )

    def nli(model: str, premise: str, hypothesis: str) -> dict[str, float]:
        assert model == settings.nli_model and premise.startswith(QUOTE)
        assert hypothesis == "Fixture proposition."
        return {"entailment": 0.8, "contradiction": 0.1, "neutral": 0.1}

    verifier = ClaimVerifier.configured(blobs(), settings, client=replay, nli_predictor=nli)
    result = await verifier.verify(claim(kind="fact", text="Fixture proposition."))
    assert result.reason_code == rc.CV_SUPPORT_UNCERTAIN and result.evidence["judge_score"] == 0.8


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("scores", "label"),
    [
        ({"entailment": 0.1, "contradiction": 0.8, "neutral": 0.1}, "contradicts"),
        ({"entailment": 0.1, "contradiction": 0.1, "neutral": 0.8}, "neutral"),
    ],
)
async def test_both_judges_require_agreement(scores: dict[str, float], label: str) -> None:
    class Supports:
        async def judge(self, *, proposition: str, passage: str) -> SupportJudgment:
            return SupportJudgment(label="supports", score=0.95, reason="Fixture")

    result = await BothJudge(
        Supports(),
        NLIJudge("fixture", lambda model, premise, hypothesis: scores),
    ).judge(proposition="fixture", passage="fixture")
    assert result.label == label


@pytest.mark.asyncio
async def test_local_nli_missing_tied_invalid_and_outage_are_uncertain(site: FetchFixture) -> None:
    def broken(model: str, passage: str, proposition: str) -> dict[str, float]:
        raise RuntimeError("NLI failure")

    judges = (
        build_support_judge(site.settings),
        NLIJudge("fixture", lambda model, premise, hypothesis: {"entailment": 1}),
        NLIJudge(
            "fixture",
            lambda model, premise, hypothesis: {
                "entailment": 0.5,
                "contradiction": 0.5,
                "neutral": 0,
            },
        ),
        NLIJudge("fixture", broken),
    )
    for judge in judges:
        with pytest.raises(JudgeUnavailable):
            await judge.judge(proposition="fixture", passage="fixture")
    settings = site.settings.model_copy(update={"support_judge": "llm"})
    with pytest.raises(ValueError, match="configured judge"):
        build_support_judge(settings)
