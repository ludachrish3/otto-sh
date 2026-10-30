"""Every reader of a run's directory finds it where ``create_output_dir`` put it.

otto itself reads no run back by path; the readers are the e2e harness's
``output_dirs`` / ``assert_no_output_dir``, which every e2e test that checks
for (or against) a run directory goes through. Both sides take the command's
directory from :func:`otto.layout.command_output_dir`, and this pins that
they meet.
"""

import subprocess

import pytest

from otto.logger import management
from tests.e2e._otto_subprocess import (
    assert_no_output_dir,
    failure_report,
    junit_files,
    output_dirs,
)


@pytest.fixture(autouse=True)
def _clean_management():
    management.reset()
    yield
    management.reset()


@pytest.mark.parametrize(("command", "sub"), [("test", None), ("host", "exec")])
def test_the_e2e_reader_finds_the_run_the_writer_made(tmp_path, command, sub):
    management.init_cli_logging(xdir=tmp_path, log_level="INFO", keep_days=7)
    out = management.create_output_dir(command, sub)
    assert output_dirs(tmp_path, command) == [out]
    assert output_dirs(tmp_path) == [out]
    with pytest.raises(AssertionError):
        assert_no_output_dir(tmp_path)


def test_the_e2e_reader_ignores_a_users_own_command_named_directory(tmp_path):
    # A project's own ./test holding an otto-pattern child is not a run.
    (tmp_path / "test" / "20200101_000000_000").mkdir(parents=True)
    assert output_dirs(tmp_path) == []
    assert_no_output_dir(tmp_path)


def test_the_e2e_reader_sees_a_run_of_any_command(tmp_path):
    # Not a hand-kept command list: everything under otto-output/ is otto's, so
    # a run of a command no list names (here `link up`) still registers.
    management.init_cli_logging(xdir=tmp_path, log_level="INFO", keep_days=7)
    out = management.create_output_dir("link", "up")
    assert output_dirs(tmp_path) == [out]
    with pytest.raises(AssertionError):
        assert_no_output_dir(tmp_path)


_JUNIT = """<?xml version="1.0" encoding="utf-8"?>
<testsuites><testsuite name="pytest" errors="1" failures="1" tests="3">
<testcase classname="TestCov" name="test_ok"/>
<testcase classname="TestCov" name="test_set_up">
<error message="RuntimeError: submodule not initialized"/></testcase>
<testcase classname="TestCov" name="test_checked"><failure message="assert 1 == 2"/></testcase>
</testsuite></testsuites>
"""


def test_a_failure_report_leads_with_the_runs_own_errors_whatever_its_output_tail(tmp_path):
    """The reason a suite failed reaches the message even when the output tail is all noise."""
    management.init_cli_logging(xdir=tmp_path, log_level="INFO", keep_days=7)
    (management.create_output_dir("test") / "junit.xml").write_text(_JUNIT)
    noisy = subprocess.CompletedProcess(["otto"], 1, stdout="coverage chatter\n" * 500, stderr="")
    assert junit_files(tmp_path) == sorted(output_dirs(tmp_path, "test")[0].glob("junit*.xml"))
    report = failure_report(noisy, tmp_path)
    assert "TestCov::test_set_up error: RuntimeError: submodule not initialized" in report
    assert "TestCov::test_checked failure: assert 1 == 2" in report
    assert "test_ok" not in report


def test_a_failure_report_says_so_when_no_test_ran(tmp_path):
    report = failure_report(subprocess.CompletedProcess(["otto"], 2, "", "boom"), tmp_path)
    assert junit_files(tmp_path) == []
    assert "it wrote no junit" in report
    assert "boom" in report


def test_the_junit_read_is_the_latest_runs(tmp_path):
    management.init_cli_logging(xdir=tmp_path, log_level="INFO", keep_days=7)
    first = management.create_output_dir("test")
    (first / "junit.xml").write_text(_JUNIT)
    second = first.with_name("29991231" + first.name[8:])  # a later run, by otto's naming
    second.mkdir()
    (second / "junit.xml").write_text(_JUNIT.replace("assert 1 == 2", "the latest run"))
    assert junit_files(tmp_path) == [second / "junit.xml"]
    noisy = subprocess.CompletedProcess(["otto"], 1, "", "")
    assert "the latest run" in failure_report(noisy, tmp_path)
