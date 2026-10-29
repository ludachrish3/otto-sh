"""``otto test --cov`` and ``otto cov report`` agree on a run dir's coverage paths.

Both entry points default to the same two directories of a run: the staged
counters (``<run>/cov``) and the HTML report (``<run>/cov_report``). Each takes
them from :mod:`otto.layout`, and this drives both with the same run dir, so a
rename there moves both entry points together or fails here.
"""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from typer.testing import CliRunner

from otto import layout
from otto.cli.cov import cov_app
from otto.context import OttoContext, reset_context, set_context
from otto.coverage import reporter as reporter_module
from otto.coverage.report_inputs import ReportInputs
from otto.invocation import RunPolicy
from otto.labs.loader import Lab
from otto.suite import run as run_module
from otto.suite.run import RunOptions


def _store() -> MagicMock:
    store = MagicMock()
    store.overall_pct.return_value = 50.0
    store.file_count.return_value = 1
    return store


def _test_cov_defaults(run):
    """Return the (cov dir, [report inputs], report dir) ``otto test --cov --cov-report`` uses."""
    collect = AsyncMock(return_value=SimpleNamespace(clean=None))
    report = AsyncMock(return_value=_store())
    with (
        patch("otto.coverage.collect.collect_coverage", collect),
        patch("otto.coverage.report_inputs.resolve_report_inputs", return_value=ReportInputs()),
        patch.object(reporter_module, "run_coverage_report", report),
    ):
        asyncio.run(run_module._post_run_coverage([], run, RunOptions(cov=True, cov_report=True)))
    return collect.await_args.args[0], report.await_args.args[0], report.await_args.args[1]


def _cov_report_defaults(run):
    """Return the ([report inputs], report dir) ``otto cov report <run>`` uses inside run *run*."""
    report = AsyncMock(return_value=_store())
    token = set_context(OttoContext(lab=Lab(name="t"), policy=RunPolicy(output_dir=run)))
    try:
        with patch.object(reporter_module, "run_coverage_report", report):
            result = CliRunner().invoke(cov_app, ["report", str(run)])
    finally:
        reset_context(token)
    assert result.exit_code == 0, result.output
    return report.call_args.args[0], report.call_args.args[1]


def test_both_entry_points_default_to_the_layouts_cov_and_report_dirs(tmp_path):
    run = tmp_path.resolve() / "run"
    host = layout.cov_dir(run) / "host1"
    host.mkdir(parents=True)
    (host / "main.gcda").write_bytes(b"\x00")

    test_cov, test_inputs, test_report = _test_cov_defaults(run)
    report_inputs, report_dir = _cov_report_defaults(run)

    assert test_cov == layout.cov_dir(run)
    assert test_inputs == report_inputs == [layout.cov_dir(run)]
    assert test_report == report_dir == layout.cov_report_dir(run)
