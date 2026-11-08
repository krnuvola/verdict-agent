"""Map shared scope applicability to the Track A guard catalogue."""

from __future__ import annotations

from collections.abc import Callable
from datetime import date
from typing import TYPE_CHECKING, Any, Literal

import httpx
from pydantic import ValidationError

from gba_agent.clients.contract_validation import UpstreamContractError, shared_models
from gba_agent.clients.graph_client import GraphClient
from gba_agent.clients.http import UpstreamServiceError
from gba_agent.guards import reason_codes as rc

if TYPE_CHECKING:
    from gba_agent.api.contracts_compat import _CompatClaim as Claim
    from gba_agent.api.contracts_compat import _CompatGuardResult as GuardResult
    from gba_agent.api.contracts_compat import _CompatMatter as Matter
    from gba_agent.api.contracts_compat import _CompatMatterFact as MatterFact
else:
    from gba_agent.api.contracts_compat import GuardResult

guard_version = "1.0.0"
type ScopeEvaluator = Callable[
    [dict[str, Any], dict[str, MatterFact], date],
    tuple[bool | Literal["true", "false", "unknown"] | None, list[str], str | None],
]

_FAILURES = {
    "in_force": (rc.SCOPE_NOT_IN_FORCE, "is outside its effective dates"),
    "addressee": (rc.SCOPE_ADDRESSEE_MISMATCH, "does not address the matter's actors"),
    "transaction_type": (rc.SCOPE_TRANSACTION_MISMATCH, "does not cover this transaction type"),
    "applies_when": (rc.SCOPE_CONDITION_UNMET, "has an unmet applicability condition"),
    "exception": (rc.SCOPE_EXCEPTION_APPLIES, "has an applicable exception"),
    "non_application": (rc.SCOPE_NON_APPLICATION, "has an applicable non-application clause"),
}


def _shared_applicability(
    frame: dict[str, Any], facts: dict[str, MatterFact], as_of: date
) -> tuple[Literal["true", "false", "unknown"], list[str], str | None]:
    if shared_models is None:
        raise UpstreamContractError("Shared scope applicability is unavailable")
    from gba_contracts.scope import applicability  # type: ignore[import-not-found]

    result: tuple[Literal["true", "false", "unknown"], list[str], str | None] = applicability(
        shared_models.ScopeFrame.model_validate(frame), facts, as_of
    )
    return result


def _fact_keys(value: object) -> set[str]:
    if isinstance(value, dict):
        fact = value.get("fact")
        keys = {fact} if isinstance(fact, str) else set()
        for item in value.values():
            keys.update(_fact_keys(item))
        return keys
    if isinstance(value, list):
        return {key for item in value for key in _fact_keys(item)}
    return set()


class ScopeGuard:
    def __init__(
        self, graph: GraphClient, *, evaluate: ScopeEvaluator = _shared_applicability
    ) -> None:
        self._graph, self._evaluate = graph, evaluate

    async def check_claim(self, claim: Claim, matter: Matter) -> GuardResult | None:
        node_id = claim.authority_node_id
        evidence: dict[str, Any] = {
            "node_id": node_id,
            "frame_valid_from": None,
            "frame_valid_to": None,
            "failed_clause": None,
            "missing_facts": [],
            "facts": {},
        }
        if claim.kind == "citation":
            return self._result(
                "pass",
                rc.SCOPE_SKIPPED_CITATION_ONLY,
                "Citation-only claims check existence.",
                evidence,
            )
        if node_id and not node_id.startswith(("Provision:", "Holding:")):
            return None
        try:
            if node_id:
                reply = await self._graph.scope_frame(node_id, matter.as_of)
                frame = reply.get("frame") if reply.get("node_id") == node_id else None
                if isinstance(frame, dict):
                    return await self._check_frame(node_id, frame, matter, evidence)
        except (
            UpstreamServiceError,
            UpstreamContractError,
            httpx.RequestError,
            ValidationError,
            KeyError,
            TypeError,
            ValueError,
            ModuleNotFoundError,
        ):
            pass
        return self._result(
            "unknown",
            rc.SCOPE_NO_FRAME,
            f"No usable scope frame was available for {node_id or 'the claim'}.",
            evidence,
        )

    async def _check_frame(
        self, node_id: str, frame: dict[str, Any], matter: Matter, evidence: dict[str, Any]
    ) -> GuardResult:
        evidence.update(
            frame_valid_from=frame.get("valid_from"), frame_valid_to=frame.get("valid_to")
        )
        facts = {
            key: fact.model_copy(update={"value": date.isoformat(fact.value)})
            if isinstance(fact.value, date)
            else fact
            for key, fact in matter.facts.items()
        }
        tri, missing, failed = self._evaluate(frame, facts, matter.as_of)
        keys = _fact_keys(frame)
        addressees = frame.get("addressee_classes", [])
        if "controller" in addressees or "control_group" in addressees:
            keys.add("controller_present")
        if "interested_director" in addressees:
            keys.add("interested_director_involved")
        if frame.get("transaction_types"):
            keys.add("transaction_type")
        evidence.update(
            failed_clause=failed,
            missing_facts=missing,
            facts={
                key: facts[key].value
                for key in sorted(keys)
                if key in facts and facts[key].status == "known"
            },
        )
        try:
            node = await self._graph.get_node(node_id, matter.as_of)
        except (UpstreamServiceError, UpstreamContractError, httpx.RequestError):
            node = {}
        label = node.get("label", node_id) if node.get("node_id") == node_id else node_id
        if tri is True or tri == "true":
            return self._result(
                "pass", rc.SCOPE_OK, f"{label} applies to the matter facts.", evidence
            )
        if (tri is False or tri == "false") and failed:
            if failure := _FAILURES.get(failed.split(":", 1)[0]):
                code, description = failure
                return self._result("block", code, f"{label} {description}.", evidence)
        return self._result(
            "unknown",
            rc.SCOPE_FACTS_MISSING,
            f"Applicability could not be established for {label}.",
            evidence,
        )

    @staticmethod
    def _result(
        verdict: Literal["pass", "block", "unknown"],
        code: str,
        reason: str,
        evidence: dict[str, Any],
    ) -> GuardResult:
        return GuardResult(
            guard="scope",
            verdict=verdict,
            reason_code=code,
            reason_text=reason,
            evidence=evidence,
            deterministic=True,
            guard_version=guard_version,
        )
