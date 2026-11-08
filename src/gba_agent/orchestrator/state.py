"""Run phases and accounting for active research time."""

from __future__ import annotations

import asyncio
from decimal import Decimal
from enum import StrEnum
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from gba_agent.api.contracts_compat import RunConfig


class Step(StrEnum):
    INTAKE = "INTAKE"
    FACTS = "FACTS"
    PLAN = "PLAN"
    ACQUIRE = "ACQUIRE"
    PROPOSE = "PROPOSE"
    VERIFY = "VERIFY"
    GAP = "GAP"
    DOSSIER = "DOSSIER"
    DRAFT = "DRAFT"
    DONE = "DONE"
    FAILED = "FAILED"


TERMINAL_STATES = frozenset({"done", "failed", "cancelled"})
TRANSITIONS = {
    Step.INTAKE: {Step.FACTS},
    Step.FACTS: {Step.PLAN},
    Step.PLAN: {Step.ACQUIRE},
    Step.ACQUIRE: {Step.PROPOSE},
    Step.PROPOSE: {Step.VERIFY},
    Step.VERIFY: {Step.GAP},
    Step.GAP: {Step.ACQUIRE},
    Step.DOSSIER: {Step.DRAFT, Step.DONE},
    Step.DRAFT: {Step.DONE},
}


class BudgetExceeded(Exception):
    """The run has no remaining research budget."""


class Budget:
    def __init__(self, config: RunConfig, prices: dict[str, dict[str, float]]) -> None:
        self.config, self.prices = config, prices
        self.steps, self.cost = 0, Decimal(0)
        self.remaining = float(config.max_wall_seconds)
        self.timer: asyncio.Timeout | None = None
        self.denied = False

    def start(self, timer: asyncio.Timeout) -> None:
        self.timer = timer
        self.resume()

    def pause(self) -> None:
        if self.timer is not None and (deadline := self.timer.when()) is not None:
            self.remaining = max(0, deadline - asyncio.get_running_loop().time())
            if not self.timer.expired():
                self.timer.reschedule(None)

    def resume(self) -> None:
        if self.timer is not None and self.timer.when() is None and not self.timer.expired():
            self.timer.reschedule(asyncio.get_running_loop().time() + self.remaining)

    def reserve(self, model: str | None = None) -> None:
        limit = Decimal(str(self.config.max_cost_usd))
        priced = model is not None and any(self.prices.get(model, {}).values())
        if (
            self.steps >= self.config.max_steps
            or self.cost > limit
            or (priced and self.cost == limit)
        ):
            self.denied = True
            raise BudgetExceeded
        self.steps += 1

    def charge(self, model: str, input_tokens: int, output_tokens: int) -> None:
        rates = self.prices.get(model)
        if rates is not None:
            self.cost += (
                Decimal(str(rates["in_per_mtok"])) * input_tokens
                + Decimal(str(rates["out_per_mtok"])) * output_tokens
            ) / 1_000_000

    def check_cost(self) -> None:
        if self.denied or self.cost > Decimal(str(self.config.max_cost_usd)):
            raise BudgetExceeded
