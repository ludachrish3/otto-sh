"""scripts/junit_durations.py — ranking and aggregation over JUnit XML."""

from pathlib import Path

import pytest

from scripts.junit_durations import iter_cases, main
from tests._fixtures.paths import CHECKOUT_ROOT

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


# ── --outliers: one tree's distribution and its slow tests, by file ─────────────

_UNIT_XML = """\
<testsuites>
  <testsuite name="pytest" tests="11" time="0">
    <testcase classname="tests.unit.test_quick" name="test_q1" time="0.01"/>
    <testcase classname="tests.unit.test_quick" name="test_q2" time="0.01"/>
    <testcase classname="tests.unit.test_quick" name="test_q3" time="0.01"/>
    <testcase classname="tests.unit.test_quick" name="test_q4" time="0.01"/>
    <testcase classname="tests.unit.test_quick" name="test_q5" time="0.01"/>
    <testcase classname="tests.unit.test_quick" name="test_q6" time="0.01"/>
    <testcase classname="tests.unit.test_quick" name="test_half_second" time="0.5"/>
    <testcase classname="tests.unit.test_heavy" name="test_one_second" time="1.0"/>
    <testcase classname="tests.unit.test_heavy.TestGroup" name="test_in_a_class" time="3.0"/>
    <testcase classname="tests.unit.sub.test_other" name="test_two_seconds" time="2.0"/>
    <testcase classname="tests.e2e.test_lab" name="test_lab_bound" time="50.0"/>
  </testsuite>
</testsuites>
"""


def _outliers(tmp_path: Path, capsys: pytest.CaptureFixture[str], *args: str) -> str:
    report = tmp_path / "unit.xml"
    report.write_text(_UNIT_XML)
    assert main(["--outliers", "--root", str(tmp_path), *args, str(report)]) == 0
    return capsys.readouterr().out


def test_outliers_report_the_distribution_of_the_unit_tree_only(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    out = _outliers(tmp_path, capsys)
    assert "=== tests.unit: 10 case(s), 6.6s summed ===" in out
    # Nearest rank, ceil(n * q / 100) of 10 sorted cases: the 5th, 9th, 10th and 10th.
    assert "p50 10 ms, p90 2000 ms, p95 3000 ms, p99 3000 ms" in out
    assert ">= 1s: 3 case(s), 6.0s summed (91% of the tree), in 2 file(s)" in out
    assert "tests.e2e" not in out


def test_outliers_take_the_threshold_inclusively(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    default = _outliers(tmp_path, capsys)
    assert "test_one_second" in default
    assert "test_half_second" not in default
    raised = _outliers(tmp_path, capsys, "--threshold", "2.5")
    assert ">= 2.5s: 1 case(s), 3.0s summed" in raised
    assert "test_one_second" not in raised


def test_outliers_group_by_file_slowest_first_with_a_class_under_its_file(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    out = _outliers(tmp_path, capsys)
    heavy = out.index("tests.unit.test_heavy")
    assert heavy < out.index("tests.unit.sub.test_other")
    assert heavy < out.index("TestGroup::test_in_a_class") < out.index("test_one_second")
    assert "tests.unit.test_heavy.TestGroup" not in out


def test_outliers_name_what_a_files_source_names(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    (tmp_path / "tests" / "unit" / "sub").mkdir(parents=True)
    unit = tmp_path / "tests" / "unit"
    (unit / "test_heavy.py").write_text("import subprocess\n")
    (unit / "sub" / "test_other.py").write_text("def test_two_seconds(): ...\n")
    lines = _outliers(tmp_path, capsys).splitlines()
    [heavy] = [line for line in lines if "tests.unit.test_heavy" in line]
    [other] = [line for line in lines if "tests.unit.sub.test_other" in line]
    assert heavy.endswith("[source names: subprocess]")
    assert "[" not in other


def test_outliers_are_a_report_never_a_failure(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Slow tests found, or no test of the tree in the report at all: exit 0 either way."""
    assert "3 case(s)" in _outliers(tmp_path, capsys)
    assert "no test cases" in _outliers(tmp_path, capsys, "--under", "tests.nowhere")


_UNREADABLE = {
    "malformed": b"<testsuites><testcase",
    "an unknown encoding": b'<?xml version="1.0" encoding="not-an-encoding"?><testsuites/>',
    "a codec expat cannot use": b'<?xml version="1.0" encoding="shift_jis"?><testsuites/>',
}


@pytest.mark.parametrize("problem", ["missing", *_UNREADABLE])
def test_outliers_name_an_unreadable_report_and_still_exit_0(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], problem: str
) -> None:
    """A report not there, not XML, or in an encoding the parser cannot read is named and
    skipped; the readable one still counts."""
    good = tmp_path / "unit.xml"
    good.write_text(_UNIT_XML)
    bad = tmp_path / "serial.xml"
    if problem in _UNREADABLE:
        bad.write_bytes(_UNREADABLE[problem])
    assert main(["--outliers", "--root", str(tmp_path), str(good), str(bad)]) == 0
    out = capsys.readouterr().out
    assert f"skipped {bad}" in out
    assert "=== tests.unit: 10 case(s)" in out


def test_outliers_with_no_readable_report_still_exit_0(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(["--outliers", str(tmp_path / "absent.xml")]) == 0
    out = capsys.readouterr().out
    assert "skipped" in out
    assert "no test cases in the report(s) read" in out


def test_outliers_count_a_case_with_an_unreadable_time_as_unknown(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    report = tmp_path / "unit.xml"
    report.write_text(
        _UNIT_XML.replace('name="test_q6" time="0.01"', 'name="test_q6" time="broken"')
    )
    assert main(["--outliers", "--root", str(tmp_path), str(report)]) == 0
    out = capsys.readouterr().out
    assert "=== tests.unit: 9 case(s), 6.5s summed ===" in out
    assert "1 case(s) with an unreadable time, not counted" in out


@pytest.mark.parametrize("view", [[], ["--by-file"]])
def test_the_ranked_views_say_how_many_cases_had_an_unreadable_time(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], view: list[str]
) -> None:
    report = tmp_path / "sample.xml"
    report.write_text(_XML.replace('time="2.5"', 'time="broken"'))
    assert main([*view, str(report)]) == 0
    out = capsys.readouterr().out
    assert "=== 3 case(s) across 1 report(s), 6.5s summed ===" in out
    assert "1 case(s) with an unreadable time, not counted" in out


def test_outliers_leave_out_the_hint_of_a_source_they_cannot_read(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "tests" / "unit").mkdir(parents=True)
    (tmp_path / "tests" / "unit" / "test_heavy.py").write_text("import subprocess\n")
    real = Path.read_text

    def unreadable(self: Path, *args: object, **kwargs: object) -> str:
        if self.name == "test_heavy.py":
            raise PermissionError(13, "Permission denied", str(self))
        return real(self, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", unreadable)
    lines = _outliers(tmp_path, capsys).splitlines()
    [heavy] = [line for line in lines if "tests.unit.test_heavy" in line]
    assert "[" not in heavy


def test_outliers_print_a_failure_while_reporting_and_still_exit_0(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    def broken(*_args: object) -> list[str]:
        raise RuntimeError("the report broke")

    monkeypatch.setattr("scripts.junit_durations.outlier_report", broken)
    assert _outliers(tmp_path, capsys) == ""
    assert "unit outliers: the report failed" in caplog.text
    assert "RuntimeError: the report broke" in caplog.text


def _recipe(target: str) -> list[str]:
    """The recipe lines of the Makefile's *target*."""
    lines = (CHECKOUT_ROOT / "Makefile").read_text().splitlines()
    start = next(i for i, line in enumerate(lines) if line.startswith(f"{target}:"))
    end = next(i for i in range(start + 1, len(lines)) if not lines[i].startswith("\t"))
    return lines[start + 1 : end]


@pytest.mark.parametrize("target", ["coverage-python", "unit-outliers"])
def test_every_outliers_call_in_the_makefile_ignores_its_status(target: str) -> None:
    """``make coverage``'s Python leg and ``make unit-outliers`` run it `-`-prefixed."""
    calls = [line for line in _recipe(target) if "unit_outliers" in line]
    assert calls, f"{target} no longer runs the unit outliers report"
    assert all(line.startswith("\t@-") for line in calls), calls
