"""Run enabled claim guards in the fixed verification order."""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Set
from typing import TYPE_CHECKING

from gba_agent.guards.abstention import HARD_GUARDS, Calibrator, finalise

if TYPE_CHECKING:
    from gba_agent.api.contracts_compat import (
        _CompatClaim as Claim,
    )
    from gba_agent.api.contracts_compat import (
        _CompatGuardReport as GuardReport,
    )
    from gba_agent.api.contracts_compat import (
        _CompatGuardResult as GuardResult,
    )
    from gba_agent.api.contracts_compat import (
        _CompatMatter as Matter,
    )

DEFAULT_GUARDS = frozenset((*HARD_GUARDS, "gnn"))
LADDER_GUARDS = DEFAULT_GUARDS | {"access", "gnn_alert"}

type ClaimCheck = Callable[[Claim, Matter], Awaitable[GuardResult | None]]
type ReportSink = Callable[[Claim, GuardReport], Awaitable[None]]


class GuardPipeline:
    def __init__(
        self,
        *,
        source_gate: ClaimCheck,
        claim_verifier: ClaimCheck,
        temporal: ClaimCheck,
        scope: ClaimCheck,
        gnn_alert: ClaimCheck,
        record: ReportSink | None = None,
        calibrator: Calibrator | None = None,
        min_confidence: float = 0,
    ) -> None:
        self._checks = tuple(
            zip(
                (*HARD_GUARDS, "gnn_alert"),
                (source_gate, claim_verifier, temporal, scope, gnn_alert),
                strict=True,
            )
        )
        self._record = record
        self.observe: Callable[[str, GuardResult], None] | None = None
        self._calibrator, self._min_confidence = calibrator, min_confidence

    async def verify(
        self, claim: Claim, matter: Matter, enabled: Set[str] = DEFAULT_GUARDS
    ) -> GuardReport:
        enabled = enabled | {"gnn_alert"} if "gnn" in enabled else enabled
        results: list[GuardResult] = []
        for name, check in self._checks:
            if name not in enabled:
                continue
            if (result := await check(claim, matter)) is None:
                continue
            results.append(result)
            if self.observe:
                self.observe(claim.claim_id, result)
            if name == "source_gate" and result.verdict == "block":
                break
        report = finalise(
            results,
            enabled,
            claim_id=claim.claim_id,
            calibrator=self._calibrator,
            min_confidence=self._min_confidence,
        )
        if self._record is not None:
            await self._record(claim, report)
        return report
