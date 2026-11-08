"""The bounded planner uses the shared LLMClient and a fixed tool dispatcher."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable
from hashlib import sha256
from time import perf_counter
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from gba_agent.api.contracts_compat import _CompatMatter
from gba_agent.api.errors import APIError
from gba_agent.clients.llm_contract import LLMCacheMiss, LLMClient, _LLMResult
from gba_agent.orchestrator.prompts import PLANNER_SYSTEM
from gba_agent.orchestrator.state import Budget, BudgetExceeded
from gba_agent.orchestrator.tools import TOOL_SCHEMAS, PlannerTools
from gba_agent.trajectory import canonical

type Emit = Callable[[str, str, dict[str, object]], None]


class Plan(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)
    tasks: list[str] = Field(max_length=25)


def untrusted(value: object) -> str:
    safe = json.dumps(value, ensure_ascii=True, separators=(",", ":")).replace("<", r"\u003c")
    return f"<untrusted_page_content>{safe}</untrusted_page_content>"


class BudgetedLLM:
    records_calls = True

    def __init__(
        self,
        client: LLMClient,
        budget: Budget,
        persist: Callable[[], None],
        emit: Emit,
        models: dict[str, str | None],
    ) -> None:
        self.client, self.budget, self.persist, self.emit = client, budget, persist, emit
        self.models = models

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
        self.budget.reserve(self.models.get(role))
        self.persist()
        started = perf_counter()
        try:
            result = self.client.complete(
                role=role,
                system=system,
                messages=messages,
                tools=tools,
                response_schema=response_schema,
                images=images,
                max_tokens=max_tokens,
                temperature=temperature,
            )
        except Exception:
            self.emit(
                "planner" if role == "planner" else "claim_verifier",
                "error",
                {
                    "where": "llm",
                    "message": "LLM call failed.",
                    "role": role,
                    "elapsed_ms": (perf_counter() - started) * 1000,
                },
            )
            raise
        metadata = call_metadata(role, result)
        metadata["elapsed_ms"] = (perf_counter() - started) * 1000
        if (
            isinstance(metadata["model"], str)
            and type(metadata["input_tokens"]) is int
            and type(metadata["output_tokens"]) is int
        ):
            self.budget.charge(result.model, result.input_tokens, result.output_tokens)
        self.persist()
        component = (
            "planner" if role == "planner" else "claim_verifier" if role == "judge" else "operator"
        )
        self.emit(component, "llm_call", metadata)
        return result


def call_metadata(role: str, result: _LLMResult) -> dict[str, object]:
    return {
        "role": role,
        "model": getattr(result, "model", None),
        "input_tokens": getattr(result, "input_tokens", None),
        "output_tokens": getattr(result, "output_tokens", None),
        "request_hash": getattr(result, "request_hash", None),
    }


class Planner:
    def __init__(
        self,
        llm: LLMClient,
        matter: _CompatMatter,
        budget: Budget,
        persist: Callable[[], None],
        emit: Emit,
    ) -> None:
        self.llm, self.budget, self.persist, self.emit = llm, budget, persist, emit
        self.system = PLANNER_SYSTEM.format(
            title=matter.title,
            question=matter.question,
            as_of=matter.as_of.isoformat(),
        )

    async def _complete(self, messages: list[dict[str, Any]], *, plan: bool = False) -> _LLMResult:
        try:
            reply = await asyncio.to_thread(
                self.llm.complete,
                role="planner",
                system=self.system,
                messages=messages,
                tools=None if plan else TOOL_SCHEMAS,
                temperature=0.0,
                response_schema=Plan.model_json_schema() if plan else None,
            )
        except (LLMCacheMiss, BudgetExceeded):
            raise
        except Exception:
            raise APIError(
                503, "UPSTREAM_UNAVAILABLE", "The research planner is unavailable"
            ) from None
        if not getattr(self.llm, "records_calls", False):
            self.emit("planner", "llm_call", call_metadata("planner", reply))
        self.budget.check_cost()
        return reply

    async def plan(self, context: dict[str, Any]) -> list[str]:
        result = await self._complete(
            [
                {
                    "role": "user",
                    "content": "Plan a bounded list of research tasks. " + untrusted(context),
                }
            ],
            plan=True,
        )
        try:
            if not result.tool_calls:
                plan = Plan.model_validate_json(result.text)
                if all(0 < len(task.strip()) <= 500 for task in plan.tasks):
                    return plan.tasks
        except ValidationError:
            pass
        raise APIError(503, "UPSTREAM_UNAVAILABLE", "The planner did not return a valid task list")

    async def research(
        self, tools: PlannerTools, context: dict[str, Any], propose: Callable[[], None]
    ) -> None:
        messages: list[dict[str, Any]] = [
            {
                "role": "user",
                "content": "Use the supplied tools and finish when this round is complete. "
                + untrusted(context),
            }
        ]
        while True:
            reply = await self._complete(messages)
            calls = list(reply.tool_calls)
            if not calls or len({call.id for call in calls}) != len(calls):
                raise APIError(
                    503, "UPSTREAM_UNAVAILABLE", "The planner did not return valid tool calls"
                )
            requests: list[dict[str, Any]] = []
            results: list[dict[str, Any]] = []
            for call in calls:
                self.budget.reserve()
                self.persist()
                if call.name == "propose_claim":
                    propose()
                self.emit(
                    "planner",
                    "state_change",
                    {
                        "from": "requested",
                        "to": "running",
                        "operation": "tool",
                        "tool": call.name,
                        "tool_call_id": call.id,
                        "request_hash": sha256(canonical(call.arguments)).hexdigest(),
                    },
                )
                value = await tools.call(call.name, call.arguments)
                self.emit(
                    "planner",
                    "state_change",
                    {
                        "from": "running",
                        "to": "failed" if "error" in value else "finished",
                        "operation": "tool",
                        "tool": call.name,
                        "tool_call_id": call.id,
                        "reason_code": value.get("error"),
                    },
                )
                if value.get("finished") is True:
                    return
                requests.append(
                    {
                        "type": "tool_use",
                        "id": call.id,
                        "name": call.name,
                        "input": call.arguments,
                    }
                )
                results.append(
                    {
                        "type": "tool_result",
                        "tool_use_id": call.id,
                        "content": untrusted(value),
                    }
                )
                self.budget.check_cost()
            messages.extend(
                ({"role": "assistant", "content": requests}, {"role": "user", "content": results})
            )
