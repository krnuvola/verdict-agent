"""Support judgments use only the independently retrieved passage."""

from __future__ import annotations

import asyncio
import math
from collections.abc import Callable, Mapping
from html import escape
from typing import Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from gba_agent.clients.llm_contract import LLMCacheMiss, LLMClient
from gba_agent.config import Settings

_JUDGE_SYSTEM = (
    "You verify whether a passage supports a proposition. Use ONLY the passage. "
    "Do not use outside\n"
    "knowledge. Answer with JSON {label: 'supports'|'contradicts'|'neutral', score: number 0..1, "
    "reason: string<=200 chars}.\n"
    "supports = the passage states or necessarily implies the proposition, including all of its "
    "conditions and parties.\n"
    "neutral = the passage is related but does not establish the proposition "
    "or omits a condition or "
    "party.\n"
    "contradicts = the passage states the opposite or an exception that defeats it."
)


class SupportJudgment(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, hide_input_in_errors=True)

    label: Literal["supports", "contradicts", "neutral"]
    score: float = Field(ge=0, le=1, allow_inf_nan=False)
    reason: str = Field(max_length=200)


class JudgeUnavailable(RuntimeError):
    """The configured judge could not deliver a valid independent judgment."""


class SupportJudge(Protocol):
    async def judge(self, *, proposition: str, passage: str) -> SupportJudgment: ...


class LLMJudge:
    deterministic = False

    def __init__(
        self,
        client: LLMClient,
        *,
        judge_model: str,
        planner_model: str,
        timeout: float = 30,
    ) -> None:
        if not judge_model.strip() or judge_model == planner_model:
            raise ValueError("The support judge must use a model distinct from the planner")
        if not math.isfinite(timeout) or timeout <= 0:
            raise ValueError("The support judge requires a finite positive timeout")
        self._client, self._model, self._timeout = client, judge_model, timeout

    async def judge(self, *, proposition: str, passage: str) -> SupportJudgment:
        message = (
            f"<proposition>{escape(proposition, quote=False)}</proposition> "
            f"<passage>{escape(passage, quote=False)}</passage>"
        )
        try:
            async with asyncio.timeout(self._timeout):
                result = await asyncio.to_thread(
                    self._client.complete,
                    role="judge",
                    system=_JUDGE_SYSTEM,
                    messages=[{"role": "user", "content": message}],
                    response_schema=SupportJudgment.model_json_schema(),
                    temperature=0.0,
                )
        except LLMCacheMiss:
            raise
        except Exception as exc:
            # The shared client has provider-specific exceptions. This boundary never retries
            # outside that client or forwards a provider error body to the guard report.
            raise JudgeUnavailable("The support judge request failed") from exc
        if result.model != self._model or result.tool_calls:
            raise JudgeUnavailable("The support judge returned an unexpected model or tool call")
        try:
            return SupportJudgment.model_validate_json(result.text)
        except ValidationError as exc:
            raise JudgeUnavailable("The support judge returned invalid JSON") from exc


# A deployment supplies a local cross-encoder and its probabilities, in premise/hypothesis order.
NLIPredictor = Callable[[str, str, str], Mapping[str, float]]
_NLI_LABELS = {"entailment": "supports", "contradiction": "contradicts", "neutral": "neutral"}


class NLIJudge:
    deterministic = True

    def __init__(self, model: str, predictor: NLIPredictor | None = None) -> None:
        self._model, self._predictor = model, predictor

    async def judge(self, *, proposition: str, passage: str) -> SupportJudgment:
        if self._predictor is None:
            raise JudgeUnavailable("The local NLI cross-encoder is unavailable")
        try:
            scores = await asyncio.to_thread(self._predictor, self._model, passage, proposition)
            if scores.keys() != _NLI_LABELS.keys() or not all(
                type(value) in {int, float} and math.isfinite(value) and 0 <= value <= 1
                for value in scores.values()
            ):
                raise JudgeUnavailable("The local NLI scores are invalid")
            best = max(scores.values())
            winners = [label for label, score in scores.items() if score == best]
            if len(winners) != 1:
                raise JudgeUnavailable("The local NLI result is uncertain")
        except JudgeUnavailable:
            raise
        except Exception as exc:
            raise JudgeUnavailable("The local NLI prediction failed") from exc
        return SupportJudgment.model_validate(
            {
                "label": _NLI_LABELS[winners[0]],
                "score": best,
                "reason": "Local NLI classification.",
            }
        )


class BothJudge:
    deterministic = False

    def __init__(self, llm: SupportJudge, nli: SupportJudge) -> None:
        self._llm, self._nli = llm, nli

    async def judge(self, *, proposition: str, passage: str) -> SupportJudgment:
        llm = await self._llm.judge(proposition=proposition, passage=passage)
        nli = await self._nli.judge(proposition=proposition, passage=passage)
        if llm.label == nli.label == "supports":
            return min((llm, nli), key=lambda result: result.score)
        return next(
            result
            for label in ("contradicts", "neutral")
            for result in (llm, nli)
            if result.label == label
        )


def build_support_judge(
    settings: Settings,
    *,
    client: LLMClient | None = None,
    nli_predictor: NLIPredictor | None = None,
) -> SupportJudge:
    nli = NLIJudge(settings.nli_model, nli_predictor)
    if settings.support_judge == "nli":
        return nli
    if client is None or settings.llm_model_judge is None:
        raise ValueError("LLM support judging requires a configured judge client and model")
    llm = LLMJudge(
        client,
        judge_model=settings.llm_model_judge,
        planner_model=settings.llm_model_planner,
    )
    return BothJudge(llm, nli) if settings.support_judge == "both" else llm
