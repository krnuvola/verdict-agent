"""Point-in-time authority and treatment checks (Doc 01 §9.3)."""

from __future__ import annotations

from collections.abc import Callable
from datetime import date
from typing import TYPE_CHECKING, Any, Literal

import httpx
from pydantic import BaseModel, ValidationError

from gba_agent.clients.contract_validation import UpstreamContractError
from gba_agent.clients.graph_client import GraphClient
from gba_agent.clients.http import UpstreamServiceError
from gba_agent.guards import reason_codes as rc

if TYPE_CHECKING:
    from gba_agent.api.contracts_compat import (
        _CompatClaim as Claim,
    )
    from gba_agent.api.contracts_compat import (
        _CompatGuardResult as GuardResult,
    )
    from gba_agent.api.contracts_compat import (
        _CompatMatter as Matter,
    )
    from gba_agent.api.contracts_compat import (
        _CompatMatterFact as MatterFact,
    )
else:
    from gba_agent.api.contracts_compat import ClaimModel as Claim
    from gba_agent.api.contracts_compat import GuardResult
    from gba_agent.api.contracts_compat import MatterModel as Matter

guard_version = "1.0.0"

type PredicateEvaluator = Callable[
    [dict[str, Any] | None, dict[str, MatterFact], set[str]], bool | None
]

_shared_evaluate: PredicateEvaluator | None
try:
    from gba_contracts.predicates import evaluate as _evaluate_predicate  # type: ignore[import-not-found]
except ModuleNotFoundError as exc:
    if exc.name != "gba_contracts":
        raise
    _shared_evaluate = None
else:
    _shared_evaluate = _evaluate_predicate


class _Interval(BaseModel):
    known_from: date
    valid_from: date | None = None
    valid_to: date | None = None

    def starts_later(self, as_of: date) -> bool:
        return self.valid_from is not None and as_of < self.valid_from

    def ended(self, as_of: date) -> bool:
        return self.valid_to is not None and as_of >= self.valid_to


# These projections validate only the fields the guard consumes when the
# shared wire models are unavailable. GraphClient validates the full models.
class _Authority(_Interval):
    node_id: str


class _TreatmentEdge(_Interval):
    edge_id: str
    src: str
    dst: str
    edge_type: Literal["overrules", "superseded_by_statute", "limits"]
    condition: dict[str, Any] | None = None


class _Treatment(BaseModel):
    edge: _TreatmentEdge
    effective_date: date
    by_node_id: str

    def visible(self, as_of: date) -> bool:
        return (
            self.effective_date <= as_of
            and self.edge.known_from <= as_of
            and not self.edge.starts_later(as_of)
            and not self.edge.ended(as_of)
        )


class _Validity(BaseModel):
    node_id: str
    as_of: date
    in_force: Literal["true", "false", "unknown"]
    treatments: list[_Treatment]


_RESULTS: dict[str, tuple[Literal["pass", "block", "unknown"], str]] = {
    rc.TEMPORAL_OK: ("pass", "The authority is in force on the matter date."),
    rc.TEMPORAL_NOT_IN_FORCE: (
        "block",
        "The authority was not in force or public on the matter date.",
    ),
    rc.TEMPORAL_EXPIRED: ("block", "The authority's validity ended by the matter date."),
    rc.TEMPORAL_VALIDITY_UNKNOWN: (
        "unknown",
        "Validity could not be established on the matter date.",
    ),
    rc.TEMPORAL_OVERRULED: ("block", "An applicable authority overrules the cited authority."),
    rc.TEMPORAL_SUPERSEDED: ("block", "An applicable statute supersedes the cited authority."),
    rc.TEMPORAL_LIMITED_WARN: ("pass", "An applicable authority limits the cited authority."),
    rc.TEMPORAL_CONDITION_UNKNOWN: (
        "unknown",
        "An applicable treatment could not be ruled in or out.",
    ),
}


def _result(code: str, evidence: dict[str, object]) -> GuardResult:
    verdict, reason = _RESULTS[code]
    return GuardResult(
        guard="temporal",
        verdict=verdict,
        reason_code=code,
        reason_text=reason,
        evidence=evidence,
        deterministic=True,
        guard_version=guard_version,
    )


class TemporalGuard:
    def __init__(self, graph: GraphClient, *, evaluate: PredicateEvaluator | None = None) -> None:
        self._graph = graph
        self._evaluate = evaluate if evaluate is not None else _shared_evaluate

    async def check_claim(self, claim: Claim, matter: Matter) -> GuardResult:
        return await self.check(claim.authority_node_id, matter)

    async def check(self, node_id: str | None, matter: Matter) -> GuardResult:
        as_of = matter.as_of
        evidence: dict[str, object] = {"node_id": node_id, "as_of": as_of.isoformat()}
        if not node_id:
            return _result(rc.TEMPORAL_VALIDITY_UNKNOWN, evidence)

        try:
            validity = _Validity.model_validate(await self._graph.validity(node_id, as_of))
            if validity.node_id != node_id or validity.as_of != as_of:
                return _result(rc.TEMPORAL_VALIDITY_UNKNOWN, evidence)
            if validity.in_force == "unknown":
                return _result(rc.TEMPORAL_VALIDITY_UNKNOWN, evidence)
            authority = _Authority.model_validate(await self._graph.get_node(node_id, as_of))
        except UpstreamServiceError as exc:
            evidence["graph_code"] = exc.code
            if exc.status_code == 404 and exc.code == "NOT_KNOWN_AS_OF":
                return _result(rc.TEMPORAL_NOT_IN_FORCE, evidence)
            return _result(rc.TEMPORAL_VALIDITY_UNKNOWN, evidence)
        except (UpstreamContractError, ValidationError, httpx.RequestError):
            return _result(rc.TEMPORAL_VALIDITY_UNKNOWN, evidence)

        if authority.node_id != node_id:
            return _result(rc.TEMPORAL_VALIDITY_UNKNOWN, evidence)
        if authority.known_from > as_of:
            evidence["graph_code"] = "NOT_KNOWN_AS_OF"
            return _result(rc.TEMPORAL_NOT_IN_FORCE, evidence)
        starts_later, ended = authority.starts_later(as_of), authority.ended(as_of)
        if (validity.in_force == "true") != (not starts_later and not ended):
            return _result(rc.TEMPORAL_VALIDITY_UNKNOWN, evidence)
        evidence.update(
            valid_from=authority.valid_from.isoformat() if authority.valid_from else None,
            valid_to=authority.valid_to.isoformat() if authority.valid_to else None,
        )
        if starts_later:
            return _result(rc.TEMPORAL_NOT_IN_FORCE, evidence)
        if ended:
            return _result(rc.TEMPORAL_EXPIRED, evidence)
        return self._check_treatments(validity, matter, evidence)

    def _check_treatments(
        self, validity: _Validity, matter: Matter, evidence: dict[str, object]
    ) -> GuardResult:
        applicable: dict[str, set[str]] = {
            "overrules": set(),
            "superseded_by_statute": set(),
            "limits": set(),
        }
        unresolved: set[str] = set()
        missing_facts: set[str] = set()
        facts = {
            key: fact.model_copy(update={"value": date.isoformat(fact.value)})
            if isinstance(fact.value, date)
            else fact
            for key, fact in matter.facts.items()
        }
        for event in validity.treatments:
            edge = event.edge
            if not event.visible(matter.as_of):
                continue
            if edge.dst != validity.node_id or edge.src != event.by_node_id:
                return _result(rc.TEMPORAL_VALIDITY_UNKNOWN, evidence)
            missing: set[str] = set()
            applies = self._condition(edge.condition, facts, missing)
            if applies is True:
                applicable[edge.edge_type].add(event.by_node_id)
            elif applies is None:
                unresolved.add(edge.edge_id)
                missing_facts.update(missing)

        if applicable["overrules"]:
            evidence["overruled_by"] = sorted(applicable["overrules"])
            return _result(rc.TEMPORAL_OVERRULED, evidence)
        if applicable["superseded_by_statute"]:
            evidence["superseded_by"] = sorted(applicable["superseded_by_statute"])
            return _result(rc.TEMPORAL_SUPERSEDED, evidence)
        if unresolved:
            evidence["unresolved_treatments"] = sorted(unresolved)
            evidence["missing_facts"] = sorted(missing_facts)
            return _result(rc.TEMPORAL_CONDITION_UNKNOWN, evidence)
        if applicable["limits"]:
            evidence["limited_by"] = sorted(applicable["limits"])
            return _result(rc.TEMPORAL_LIMITED_WARN, evidence)
        return _result(rc.TEMPORAL_OK, evidence)

    def _condition(
        self, condition: dict[str, Any] | None, facts: dict[str, MatterFact], missing: set[str]
    ) -> bool | None:
        if condition is None:
            return True
        if self._evaluate is None:
            return None
        try:
            result: object = self._evaluate(condition, facts, missing)
        except (KeyError, TypeError, ValueError):
            return None
        return result if isinstance(result, bool) else None
