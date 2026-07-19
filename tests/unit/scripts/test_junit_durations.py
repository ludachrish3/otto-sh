"""scripts/junit_durations.py — ranking and aggregation over JUnit XML."""

from pathlib import Path

import pytest

from scripts.junit_durations import iter_cases, main

_XML = """\
<testsuites>
  <testsuite name="pytest" tests="3" time="9.0">
    <testcase classname="tests.e2e.test_slow" name="test_big" time="5.5"/>
    <testcase classname="tests.e2e.test_slow" name="test_mid" time="2.5"/>
    <testcase classname="tests.unit.test_fast" name="test_small" time="1.0"/>
  </testsuite>
</testsuites>
"""


def _write_report(tmp_path: Path) -> Path:
    report = tmp_path / "sample.xml"
    report.write_text(_XML)
    return report


def test_iter_cases_yields_every_testcase(tmp_path: Path) -> None:
    report = _write_report(tmp_path)
    cases = list(iter_cases(report))
    assert ("tests.e2e.test_slow", "test_big", 5.5) in cases
    assert len(cases) == 3


def test_main_ranks_slowest_first_and_honors_top(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    report = _write_report(tmp_path)
    assert main(["--top", "2", str(report)]) == 0
    out = capsys.readouterr().out
    assert out.index("test_big") < out.index("test_mid")
    assert "test_small" not in out


def test_by_file_aggregates_per_classname(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    report = _write_report(tmp_path)
    assert main(["--by-file", str(report)]) == 0
    out = capsys.readouterr().out
    assert out.index("tests.e2e.test_slow") < out.index("tests.unit.test_fast")
    assert "8.0" in out  # 5.5 + 2.5 summed for the slow module
