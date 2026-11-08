import json
from pathlib import Path
from typing import Any

import pytest
from hypothesis import given
from hypothesis import strategies as st
from pydantic import ValidationError

from gba_agent.api.contracts_compat import GuardResult, _CompatGuardResult
from gba_agent.guards.abstention import HARD_GUARDS, Calibrator, finalise

ENABLED = {*HARD_GUARDS, "gnn"}


def result(guard: str, verdict: str, **evidence: Any) -> _CompatGuardResult:
    return GuardResult.model_validate(
        {
            "guard": guard,
            "verdict": verdict,
            "reason_code": f"{guard}_{verdict}",
            "reason_text": "fixture",
            "evidence": evidence,
            "deterministic": True,
            "guard_version": "1.0.0",
        }
    )


def artifact(**changes: Any) -> dict[str, Any]:
    return {
        "version": 1,
        "feature_order": ["judge_score", "gnn_calibrated", "quote_exact", "crosschecked"],
        "defaults": {
            "judge_score": 0.5,
            "gnn_calibrated": 0.5,
            "quote_exact": 0,
            "crosschecked": 0,
        },
        "weights": [0, 0, 0, 0],
        "bias": 0,
        "isotonic": {"x": [0.25, 0.75], "y": [0.1, 0.9]},
        **changes,
    }


def calibrator(**changes: Any) -> Calibrator:
    return Calibrator.model_validate_json(json.dumps(artifact(**changes)))


def test_abstention_order_and_missing_fact_union() -> None:
    results = [
        result("gnn_alert", "unknown", missing_facts=["ignore"]),
        result("scope", "unknown", missing_facts=["transaction_type", "closing_date"]),
        result("temporal", "unknown", missing_facts=["closing_date"]),
        result("claim_verifier", "unknown"),
        result("source_gate", "unknown"),
    ]
    report = finalise(results, ENABLED, claim_id="clm_fixture")
    assert (report.final, report.abstain_reason) == ("unknown", "source_gate_unknown")
    assert report.missing_facts == ["closing_date", "transaction_type"]
    assert report.results == results
    blocked = finalise([*results, result("scope", "block")], ENABLED, claim_id="clm_fixture")
    assert blocked.final == "block" and blocked.abstain_reason is None
    assert blocked.missing_facts == report.missing_facts
    advisory = finalise(results, {"gnn"}, claim_id="clm_fixture")
    assert advisory.final == "pass" and advisory.missing_facts == []


@given(
    st.lists(st.tuples(st.sampled_from(HARD_GUARDS), st.sampled_from(("pass", "block", "unknown"))))
)
def test_hard_guard_precedence_is_monotonic(cases: list[tuple[str, str]]) -> None:
    results = [result(guard, verdict) for guard, verdict in cases]
    original = finalise(results, ENABLED, claim_id="clm_fixture")
    blocked = finalise([*results, result("scope", "block")], ENABLED, claim_id="clm_fixture")
    assert blocked.final == "block"
    removed = finalise(
        [item for item in results if item.verdict != "unknown"], ENABLED, claim_id="clm_fixture"
    )
    if original.final == "pass":
        assert removed.final == "pass"
    assert removed.final == (
        "block" if any(item.verdict == "block" for item in results) else "pass"
    )


def test_calibration_interpolates_clamps_and_uses_features(tmp_path: Path) -> None:
    path = tmp_path / "calibrator.json"
    assert Calibrator.load(path) is None
    path.write_text(json.dumps(artifact()), encoding="utf-8")
    loaded = Calibrator.load(path)
    assert loaded is not None and loaded.predict({}) == pytest.approx(0.5)
    assert calibrator(bias=-1000).predict({}) == pytest.approx(0.1)
    assert calibrator(bias=1000).predict({}) == pytest.approx(0.9)

    model = calibrator(weights=[0, 0, 4, 0], bias=-2)
    results = [result("claim_verifier", "pass", quote_exact=True), result("gnn_alert", "flag")]
    assert (
        finalise(results, ENABLED, claim_id="c", calibrator=model, min_confidence=0.6).final
        == "pass"
    )
    results[0] = result("claim_verifier", "pass", quote_exact=False)
    report = finalise(results, ENABLED, claim_id="c", calibrator=model, min_confidence=0.6)
    assert (report.final, report.abstain_reason) == ("unknown", "ABSTAIN_LOW_CONFIDENCE")
    assert finalise(results, ENABLED, claim_id="c", min_confidence=0.6).final == "pass"


@pytest.mark.parametrize(
    "changes",
    [
        {"version": 2},
        {"weights": [1]},
        {"bias": float("inf")},
        {"defaults": {"judge_score": 0.5}},
        {"feature_order": ["crosschecked", "quote_exact", "gnn_calibrated", "judge_score"]},
        {"isotonic": {"x": [0.2, 0.2], "y": [0, 1]}},
        {"isotonic": {"x": [0.2, 0.3], "y": [1, 0]}},
        {"isotonic": {"x": [0.2], "y": [0, 1]}},
        {"isotonic": {"x": [], "y": []}},
        {"weights": [1e308, 1e308, 0, 0]},
    ],
)
def test_invalid_calibrator_is_rejected(changes: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        calibrator(**changes)
