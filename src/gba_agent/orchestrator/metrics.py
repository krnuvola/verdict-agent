"""Observed Track A counters only; no scoring or inferred usage."""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from time import perf_counter
from typing import Any
from urllib.parse import urlsplit


@dataclass
class RunMetrics:
    started: float = field(default_factory=perf_counter)
    browser_actions: int = 0
    retries: int = 0
    recoveries: int = 0
    domains: set[str] = field(default_factory=set)
    sources: set[str] = field(default_factory=set)
    admitted: set[str] = field(default_factory=set)
    guards: Counter[str] = field(default_factory=Counter)
    models: set[str] = field(default_factory=set)
    input_tokens: int = 0
    output_tokens: int = 0
    usage_complete: bool = True
    llm_calls: int = 0
    failed_llm_calls: int = 0
    budget_stopped: bool = False
    model_complete: bool = True

    def observe(self, event: str, payload: dict[str, Any]) -> None:
        if event == "error" and payload.get("where") == "llm":
            self.llm_calls += 1
            self.failed_llm_calls += 1
            self.usage_complete = False
            self.model_complete = False
        if event == "abstain" and payload.get("reason_code") == "BUDGET_EXCEEDED":
            self.budget_stopped = True
        if event == "action_executed" and payload.get("detail") != "DRY_RUN":
            self.browser_actions += 1
        if event == "fetch":
            if payload.get("status") == "ok":
                if payload.get("source_id"):
                    self.sources.add(payload["source_id"])
        if payload.get("operation") == "network_request" and payload.get("phase") == "started":
            host = urlsplit(str(payload["url"])).hostname
            if host:
                self.domains.add(host)
        if payload.get("operation") == "peer_request" and payload.get("attempt", 1) > 1:
            self.retries += 1
        if event == "block" and payload.get("action_taken") == "retry":
            self.retries += 1
        if payload.get("operation") == "playbook_repair" and payload.get("phase") == "started":
            self.recoveries += 1
        if event == "guard_result":
            result = payload["result"]
            self.guards[f"{result['guard']}:{result['verdict']}"] += 1
        if payload.get("operation") == "source_admission" and payload.get("admitted"):
            self.admitted.add(payload["source_id"])
        if event == "llm_call":
            self.llm_calls += 1
            if isinstance(payload.get("model"), str):
                self.models.add(payload["model"])
            else:
                self.model_complete = False
            for key in ("input_tokens", "output_tokens"):
                count = payload.get(key)
                if type(count) is int and count >= 0:
                    setattr(self, key, getattr(self, key) + count)
                else:
                    self.usage_complete = False

    def snapshot(self, status: str) -> dict[str, Any]:
        return {
            "status": status,
            "budget_stopped": self.budget_stopped,
            "model_status": "observed" if self.model_complete else "unavailable",
            "elapsed_wall_seconds": perf_counter() - self.started,
            "browser_action_count": self.browser_actions,
            "retry_count": self.retries,
            "recovery_attempts": self.recoveries,
            "domain_count": len(self.domains),
            "sources_visited": sorted(self.sources),
            "evidence_admitted": sorted(self.admitted),
            "guard_outcomes": dict(sorted(self.guards.items())),
            "models": sorted(self.models),
            "llm_calls": self.llm_calls,
            "failed_llm_calls": self.failed_llm_calls,
            "input_tokens": self.input_tokens if self.usage_complete else None,
            "output_tokens": self.output_tokens if self.usage_complete else None,
            "usage_status": "observed" if self.usage_complete else "unavailable",
            "observed_input_tokens": self.input_tokens,
            "observed_output_tokens": self.output_tokens,
            "scope": "Track A events only; domains from observed HTTP attempts including robots; "
            "source IDs include cached captures",
            "unavailable_metrics": ["provider_internal_retries", "peer_service_usage"],
        }
