"""The coverage verbs' reports: ``ok`` is derived from the results and nothing else."""

from pathlib import Path

from otto.coverage.errors import CoverageCleanError
from otto.coverage.reports import CleanReport, FailedReset, GetReport
from otto.result import Result
from otto.utils import Status

OK = Result(Status.Success)
BAD = Result(Status.Error, msg="find: '/opt/app/cov': Permission denied")
DECLINED = Result(Status.NotRun)


def test_a_clean_report_with_every_reset_ok_is_ok():
    report = CleanReport(hosts={"t1": {"app": OK}, "t2": {}})
    assert report.ok
    assert report.failed == []
    assert report.cleared == [("t1", "app")]


def test_one_failed_reset_makes_the_report_not_ok_and_names_host_and_product():
    report = CleanReport(hosts={"t1": {"app": OK, "lib": BAD}, "z": {"ext": DECLINED}})
    assert not report.ok
    assert report.failed == [FailedReset("t1", "lib", BAD)]
    assert report.failed[0].reason == "find: '/opt/app/cov': Permission denied"


def test_a_declined_reset_is_neither_cleared_nor_failed():
    report = CleanReport(hosts={"z": {"ext": DECLINED}})
    assert report.ok
    assert report.cleared == []
    assert report.not_run == [("z", "ext")]


def test_reason_falls_back_to_value_then_status():
    assert FailedReset("h", "p", Result(Status.Error, value="rc 1")).reason == "rc 1"
    assert FailedReset("h", "p", Result(Status.Error)).reason == "Error"


def test_a_clean_error_names_every_failure():
    report = CleanReport(hosts={"t1": {"lib": BAD}, "z": {"ext": Result(Status.Error, msg="x")}})
    assert str(CoverageCleanError(report)) == (
        "could not clear coverage counters: t1/lib: find: '/opt/app/cov': "
        "Permission denied; z/ext: x"
    )


def test_a_get_report_is_ok_without_a_clean_and_follows_its_clean():
    base = {
        "cov_dir": Path("/o/cov"),
        "tier": "system",
        "captures": [Path("c.json")],
        "manual_captures": [],
    }
    assert GetReport(**base, clean=None).ok
    assert not GetReport(**base, clean=CleanReport(hosts={"t1": {"lib": BAD}})).ok
