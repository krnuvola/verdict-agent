"""Deterministic operator tool and replay failure handling."""

from dataclasses import dataclass
from typing import Any

import pytest

from gba_agent.operator.perception import Observation
from gba_agent.operator.vlm import InvalidProposal, decide


@dataclass
class Tool:
    name: str
    arguments: dict[str, Any]
    id: str = "recorded"


@dataclass
class Result:
    tool_calls: list[Tool]
    model: str = "test"
    text: str = ""
    input_tokens: int = 1
    output_tokens: int = 1
    request_hash: str = "a" * 64


@dataclass
class Replay:
    response: Result | None

    def complete(self, **kwargs: Any) -> Result:
        if self.response is None:
            raise LookupError("Replay cache miss")
        assert kwargs["temperature"] == 0.0
        return self.response


def observation() -> Observation:
    return Observation(
        observation_id=1,
        url="https://fixture.test/",
        title="Fixture",
        elements=[],
        text_excerpt="Public test",
        ax_digest="a" * 64,
        network=[],
        downloads=[],
        dialogs=[],
        tabs=[],
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "tools",
    [
        [],
        [Tool("unexpected", {})],
        [Tool("act", {"action": {"action_type": "invalid"}})],
        [Tool("extract", {"note": 3})],
        [Tool("done", {"summary": "x" * 2001})],
    ],
)
async def test_invalid_replay_is_rejected(tools: list[Tool]) -> None:
    with pytest.raises(InvalidProposal):
        await decide(
            Replay(Result(tools)),
            observation(),
            goal="Read",
            success_criteria="Public text",
            feedback="",
            run_id="run",
            emit=None,
        )


@pytest.mark.asyncio
async def test_missing_replay_does_not_fall_back_to_live() -> None:
    with pytest.raises(LookupError, match="cache miss"):
        await decide(
            Replay(None),
            observation(),
            goal="Read",
            success_criteria="Public text",
            feedback="",
            run_id="run",
            emit=None,
        )


@pytest.mark.asyncio
async def test_extraction_tool_returns_note() -> None:
    decision = await decide(
        Replay(Result([Tool("extract", {"note": "Public note"})])),
        observation(),
        goal="Read",
        success_criteria="Public text",
        feedback="",
        run_id="run",
        emit=None,
    )
    assert decision.name == "extract" and decision.text == "Public note"
