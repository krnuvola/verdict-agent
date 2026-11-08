"""Read-only local Doc 00 LLM cassette adapter; never constructs a provider."""

from __future__ import annotations

import json
from hashlib import sha256
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict

from gba_agent.clients.llm_contract import LLMCacheMiss, LLMClient, _LLMResult
from gba_agent.config import Settings
from gba_agent.trajectory import canonical


class ToolCall(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str
    name: str
    arguments: dict[str, Any]


class CassetteResult(BaseModel):
    model_config = ConfigDict(extra="forbid")
    model: str
    text: str
    tool_calls: list[ToolCall]
    input_tokens: int
    output_tokens: int
    request_hash: str


class CassetteReplay:
    """Explicit replay fallback. Shared-package cassette envelope needs confirmation."""

    replay_safe = True
    mode = "replay"

    def __init__(self, root: Path, models: dict[str, str | None]) -> None:
        self.root, self.models = root, models

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
    ) -> _LLMResult:
        request_hash = sha256(
            canonical(
                {
                    "role": role,
                    "model": self.models.get(role),
                    "system": system,
                    "messages": messages,
                    "tools": tools,
                    "response_schema": response_schema,
                    "temperature": temperature,
                    "max_tokens": max_tokens,
                    "image_sha256s": [sha256(image).hexdigest() for image in images or []],
                }
            )
        ).hexdigest()
        path = self.root / f"{request_hash}.json"
        try:
            payload = json.loads(path.read_bytes())
        except FileNotFoundError:
            raise LLMCacheMiss(f"Replay cassette missing: {request_hash}") from None
        result = CassetteResult.model_validate(payload)
        if result.request_hash != request_hash or result.model != self.models.get(role):
            raise ValueError("Replay cassette identity mismatch")
        return result


def replay_only(client: LLMClient | None, settings: Settings) -> LLMClient | None:
    """An unmarked injected provider cannot be called in replay mode."""
    if settings.llm_mode != "replay" or (
        client is not None
        and (
            getattr(client, "replay_safe", False) is True
            or getattr(client, "mode", None) == "replay"
        )
    ):
        return client
    return CassetteReplay(
        settings.llm_cache_dir,
        {
            role: getattr(settings, f"llm_model_{role}")
            for role in ("planner", "extractor", "judge", "vlm", "drafter")
        },
    )
