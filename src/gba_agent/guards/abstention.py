"""Deterministic hard-guard precedence and optional confidence calibration."""

from __future__ import annotations

import math
from bisect import bisect_right
from collections.abc import Mapping, Sequence, Set
from pathlib import Path
from typing import TYPE_CHECKING, Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from gba_agent.guards import reason_codes as rc

if TYPE_CHECKING:
    from gba_agent.api.contracts_compat import _CompatGuardReport as GuardReport
    from gba_agent.api.contracts_compat import _CompatGuardResult as GuardResult
else:
    from gba_agent.api.contracts_compat import GuardReport

HARD_GUARDS = ("source_gate", "claim_verifier", "temporal", "scope")
_FEATURES = ("judge_score", "gnn_calibrated", "quote_exact", "crosschecked")
type _Unit = Annotated[float, Field(ge=0, le=1, allow_inf_nan=False)]
type _Finite = Annotated[float, Field(allow_inf_nan=False)]


class _CalibratorModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True, hide_input_in_errors=True)


class _Isotonic(_CalibratorModel):
    x: tuple[_Unit, ...] = Field(min_length=1)
    y: tuple[_Unit, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def check_table(self) -> _Isotonic:
        if len(self.x) != len(self.y):
            raise ValueError("Calibration table must have matching x and y lengths")
        if any(left >= right for left, right in zip(self.x, self.x[1:], strict=False)):
            raise ValueError("Calibration x must increase strictly")
        if any(left > right for left, right in zip(self.y, self.y[1:], strict=False)):
            raise ValueError("Calibration y must be nondecreasing")
        return self


class Calibrator(_CalibratorModel):
    version: Literal[1]
    feature_order: tuple[
        Literal["judge_score", "gnn_calibrated", "quote_exact", "crosschecked"], ...
    ]
    defaults: dict[str, _Unit]
    weights: tuple[_Finite, _Finite, _Finite, _Finite]
    bias: _Finite
    isotonic: _Isotonic

    @model_validator(mode="after")
    def check_features(self) -> Calibrator:
        if self.feature_order != _FEATURES or self.defaults.keys() != set(_FEATURES):
            raise ValueError("Calibration features must match the v1 contract")
        if not math.isfinite(abs(self.bias) + sum(map(abs, self.weights))):
            raise ValueError("Calibration coefficients exceed the finite numeric range")
        return self

    @classmethod
    def load(cls, path: Path) -> Calibrator | None:
        try:
            return cls.model_validate_json(path.read_bytes())
        except FileNotFoundError:
            return None

    def predict(self, features: Mapping[str, float | bool]) -> float:
        z = self.bias + sum(
            weight * features.get(key, self.defaults[key])
            for key, weight in zip(self.feature_order, self.weights, strict=True)
        )
        exponent = math.exp(-abs(z))
        score = 1 / (1 + exponent) if z >= 0 else exponent / (1 + exponent)
        x, y = self.isotonic.x, self.isotonic.y
        right = bisect_right(x, score)
        if right == 0:
            return y[0]
        if right == len(x):
            return y[-1]
        left = right - 1
        fraction = (score - x[left]) / (x[right] - x[left])
        return y[left] + fraction * (y[right] - y[left])


def _features(results: Sequence[GuardResult]) -> dict[str, float | bool]:
    fields = {
        "claim_verifier": (("judge_score", "judge_score"), ("quote_exact", "quote_exact")),
        "gnn_alert": (("score_calibrated", "gnn_calibrated"),),
        "source_gate": (("crosschecked", "crosschecked"),),
    }
    features: dict[str, float | bool] = {}
    for result in results:
        for source, feature in fields.get(result.guard, ()):
            value = result.evidence.get(source)
            if isinstance(value, (int, float)) and 0 <= value <= 1:
                features[feature] = value
    return features


def finalise(
    results: Sequence[GuardResult],
    enabled: Set[str],
    *,
    claim_id: str,
    calibrator: Calibrator | None = None,
    min_confidence: float = 0,
) -> GuardReport:
    hard = [
        result
        for name in HARD_GUARDS
        if name in enabled
        for result in results
        if result.guard == name
    ]
    missing: dict[str, None] = {}
    for result in hard:
        facts = result.evidence.get("missing_facts")
        if isinstance(facts, list):
            missing.update(dict.fromkeys(fact for fact in facts if isinstance(fact, str)))

    final: Literal["pass", "block", "unknown"] = "pass"
    reason = None
    if any(result.verdict == "block" for result in hard):
        final = "block"
    elif first := next((result for result in hard if result.verdict == "unknown"), None):
        final, reason = "unknown", first.reason_code
    elif calibrator is not None and min_confidence > 0:
        features = _features(
            [
                result
                for result in results
                if result.guard in enabled or (result.guard == "gnn_alert" and "gnn" in enabled)
            ]
        )
        if calibrator.predict(features) < min_confidence:
            final, reason = "unknown", rc.ABSTAIN_LOW_CONFIDENCE
    return GuardReport(
        claim_id=claim_id,
        results=list(results),
        final=final,
        abstain_reason=reason,
        missing_facts=list(missing),
    )
