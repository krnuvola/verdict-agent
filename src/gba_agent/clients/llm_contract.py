"""Shared LLMClient and the temporary structural contract used by offline tests."""

from __future__ import annotations

from collections.abc import Sequence
from typing import TYPE_CHECKING, Any, Protocol


class _ToolCall(Protocol):
    id: str
    name: str
    arguments: dict[str, Any]


class _LLMResult(Protocol):
    @property
    def tool_calls(self) -> Sequence[_ToolCall]: ...

    model: str
    text: str
    input_tokens: int
    output_tokens: int
    request_hash: str


class _LLMClient(Protocol):
    def complete(
        self,
        *,
        role: str,
        system: str,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        response_schema: dict[str, Any] | None = None,
        images: list[bytes] | None = None,
        max_tokens: int = 4096,
        temperature: float = 0.0,
    ) -> _LLMResult: ...


class _LLMCacheMiss(LookupError):
    """A replay cache has no matching request."""


if TYPE_CHECKING:
    LLMClient = _LLMClient
    LLMCacheMiss = _LLMCacheMiss
else:
    try:
        from gba_contracts.llm import LLMCacheMiss, LLMClient  # type: ignore[import-not-found,no-redef]
    except ModuleNotFoundError as error:
        if error.name != "gba_contracts":
            raise
        LLMClient = _LLMClient
        LLMCacheMiss = _LLMCacheMiss
