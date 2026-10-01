"""The docker verbs' report types: ok/failed derive from the results, nothing else."""

import dataclasses

import pytest

from otto.docker.reports import BuildReport, FailedImage, HostReport, RepoBuild, TeardownReport
from otto.result import CommandResult
from otto.utils import Status


def _ok(value="") -> CommandResult:
    return CommandResult(Status.Success, value=value, command="docker x", retcode=0)


def _skipped(value="") -> CommandResult:
    return CommandResult(Status.Skipped, value=value, command="", retcode=0)


def _fail(value="boom", command="docker x") -> CommandResult:
    return CommandResult(Status.Failed, value=value, command=command, retcode=1)


def test_host_report_ok_when_every_result_is_ok():
    report = HostReport(hosts={"test3": [_ok(), _skipped()], "alt2": [_ok()]})
    assert report.ok
    assert report.failed == {}


def test_host_report_failed_names_only_the_failing_results_per_host():
    bad = _fail("error during connect")
    report = HostReport(hosts={"test3": [_ok()], "alt2": [_ok(), bad]})
    assert not report.ok
    assert report.failed == {"alt2": [bad]}


def test_teardown_report_carries_the_use_case_and_is_a_host_report():
    report = TeardownReport(hosts={"test3": [_ok()]}, use_case="integration")
    assert isinstance(report, HostReport)
    assert report.use_case == "integration"
    assert report.ok


def test_build_report_ok_treats_skipped_as_ok():
    images = {"api": _skipped("r1-api:abc"), "w": _ok("r1-w:abc")}
    report = BuildReport(repos=[RepoBuild("r1", "test3", "built", images)])
    assert report.ok
    assert report.failed == []


def test_build_report_failed_lists_repo_image_and_result():
    bad = _fail("syntax error", command="docker build ...")
    report = BuildReport(
        repos=[
            RepoBuild("r1", "test3", "built", {"api": _ok("t")}),
            RepoBuild("r2", "test3", "built", {"db": bad}),
            RepoBuild("r3", "test3", "no_images"),
        ]
    )
    assert not report.ok
    assert report.failed == [FailedImage("r2", "db", bad)]


def test_no_images_entry_has_no_image_results_and_never_fails():
    entry = RepoBuild("r3", "test3", "no_images")
    assert entry.images == {}
    assert BuildReport(repos=[entry]).ok


def test_reports_are_frozen():
    report = HostReport(hosts={})
    with pytest.raises(dataclasses.FrozenInstanceError):
        report.hosts = {}  # type: ignore[misc]


def test_build_report_displaced_defaults_empty():
    assert BuildReport(repos=[]).displaced == []


def test_the_new_names_are_lazily_exported():
    import otto.docker as pkg

    for name in (
        "build_on",
        "compose_build",
        "DockerBuildError",
        "BuildReport",
        "RepoBuild",
        "FailedImage",
        "HostReport",
        "TeardownReport",
    ):
        assert name in pkg.__all__, name
        assert getattr(pkg, name) is not None
