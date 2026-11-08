"""The reporting gate must retain failures, skips and incomplete coverage."""

import json
from pathlib import Path

import pytest
from scripts.check_acceptance import summarize


@pytest.mark.parametrize("missing_branch", [False, True])
def test_gate_does_not_hide_failed_tests_or_branch_gaps(
    tmp_path: Path, missing_branch: bool
) -> None:
    junit = tmp_path / "tests.xml"
    junit.write_text(
        """<testsuites><testsuite><testcase classname="fixture" name="pass"/>
<testcase classname="fixture" name="failure"><failure message="exact scenario mismatch"/></testcase>
<testcase classname="fixture" name="live"><skipped message="explicit live opt-in"/></testcase>
</testsuite></testsuites>""",
        encoding="utf-8",
    )
    coverage = tmp_path / "coverage.json"
    coverage.write_text(
        json.dumps(
            {
                "totals": {"covered_lines": 90, "num_statements": 100, "percent_covered": 85},
                "files": {
                    "action_guard.py": {
                        "summary": {
                            "num_branches": 2,
                            "covered_branches": 1 if missing_branch else 2,
                        }
                    },
                    "source_gate.py": {"summary": {"num_branches": 2, "covered_branches": 2}},
                },
            }
        ),
        encoding="utf-8",
    )
    report = summarize(junit, coverage)
    assert report["tests"] == {"passed": 1, "failed_or_error": 1, "skipped": 1}
    assert report["failures"][0]["message"] == "exact scenario mismatch"
    assert report["skips"][0]["reason"] == "explicit live opt-in"
    assert report["coverage_gate"] is (not missing_branch)


def test_missing_coverage_is_an_error_not_a_passing_default(tmp_path: Path) -> None:
    junit, coverage = tmp_path / "tests.xml", tmp_path / "coverage.json"
    junit.write_text("<testsuites/>", encoding="utf-8")
    coverage.write_text('{"files":{}}', encoding="utf-8")
    with pytest.raises(ValueError, match="missing"):
        summarize(junit, coverage)
