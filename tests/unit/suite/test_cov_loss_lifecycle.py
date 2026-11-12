"""The coverage-loss evidence through ``run_tests``: one owner, five hops, no leak (#777 §3).

The pre-run reset's :class:`~otto.coverage.reports.CleanReport` is carried by
the invocation's ``_Sessions`` to the post-run collection, and from there to
the fetcher. These run real nested pytest sessions (``one_project`` +
``real_shell_bytecode``, as ``test_run_api.py``'s pre-clean test does); the
lifecycle proof patches nothing on the coverage path between ``run_tests``
and the fetcher's archive, only the host, a real-``sh`` double
(:mod:`tests._fixtures.shell_host`).
"""

import dataclasses
import logging
from unittest.mock import AsyncMock, MagicMock

from otto.coverage.reports import CleanReport
from otto.result import Result
from otto.suite.run import RunOptions, run_tests
from otto.utils import Status
from tests._fixtures.bootstrap_seam import fake_bootstrap_result
from tests._fixtures.shell_host import coverage_shell_host, install_shell_lab

_LOSS = "existed at run start and is gone"


def _loss_warnings(caplog) -> list[logging.LogRecord]:
    return [r for r in caplog.records if r.levelno == logging.WARNING and _LOSS in r.getMessage()]


def _force_cov(monkeypatch) -> None:
    """Coverage on, whatever the lab's instrumentation scan would say."""
    import otto.suite.run as run_module

    monkeypatch.setattr(
        run_module,
        "resolve_coverage",
        lambda opts, projects, *, command: dataclasses.replace(opts, cov=True),
    )


def test_run_tests_threads_the_pre_clean_report_to_the_fetcher(
    one_project, real_shell_bytecode, tmp_path, monkeypatch
):
    evidence = frozenset({("t1", "app")})
    one_project(files={"tests/test_thread.py": "def test_thread():\n    pass\n"})
    _force_cov(monkeypatch)
    report = CleanReport(
        hosts={"t1": {"app": Result(status=Status.Success)}}, present_at_reset=evidence
    )
    monkeypatch.setattr("otto.coverage.collect.clean_coverage", AsyncMock(return_value=report))
    collect = AsyncMock(return_value=MagicMock(clean=None))
    monkeypatch.setattr("otto.coverage.collect.collect_coverage", collect)

    result = run_tests(
        names=["test_thread"],
        run_options=RunOptions(cov=True, cov_clean=True),
        output_dir=tmp_path / "out",
    )

    assert result.exit_code == 0
    collect.assert_awaited_once()
    assert collect.call_args.kwargs["present_at_reset"] == evidence


_DELETING_TEST = """\
import pathlib
import shutil

from otto.context import get_context

FLAG = pathlib.Path({flag!r})
COV_DIR = pathlib.Path({cov_dir!r})
SEEN = pathlib.Path({seen!r})


def test_loss():
    ctx = get_context()
    # The latch a cleanup claims (ProjectActions.cleanup calls exactly this);
    # claimed here because the library context's lab has no host to sweep.
    claimed = ctx.claim_cleanup_warning()
    with SEEN.open("a") as seen:
        seen.write(f"{{ctx.collecting_coverage}} {{claimed}}\\n")
    if FLAG.exists():
        shutil.rmtree(COV_DIR, ignore_errors=True)
"""


def test_two_run_tests_on_one_active_context_reach_the_real_fetcher(
    one_project, real_shell_bytecode, tmp_path, monkeypatch, caplog
):
    from otto.config.project import Project
    from otto.context import OttoContext, reset_context, set_context
    from otto.labs.loader import Lab

    cov_dir = tmp_path / "remote" / "app-cov"
    flag = tmp_path / "delete.flag"
    flag.touch()
    seen = tmp_path / "seen.log"
    sut = one_project(
        files={
            # A basename no otto test module uses: the in-process run imports it by
            # basename, and tests/unit/test_lifecycle.py may already sit in sys.modules.
            "tests/test_cov_loss_sut.py": _DELETING_TEST.format(
                flag=str(flag), cov_dir=str(cov_dir), seen=str(seen)
            )
        }
    )
    install_shell_lab(monkeypatch, coverage_shell_host("t1", {"app": cov_dir}))
    _force_cov(monkeypatch)
    ctx = OttoContext(lab=Lab(name="test"), bootstrap=fake_bootstrap_result([Project(root=sut)]))
    before = (ctx.cov_decision, ctx.policy.output_dir, False, False)

    def _state():
        return (
            ctx.cov_decision,
            ctx.policy.output_dir,
            ctx.collecting_coverage,
            ctx.policy.coverage.warned,
        )

    token = set_context(ctx)
    try:
        with caplog.at_level(logging.DEBUG):
            # Run 1: left in place by the pre-run reset (it holds a file the
            # reset never removes; an emptied one goes), removed by the test.
            cov_dir.mkdir(parents=True)
            (cov_dir / "notes.txt").write_text("user data")
            run_tests(
                names=["test_loss"],
                run_options=RunOptions(cov=True, cov_clean=True),
                output_dir=tmp_path / "o1",
            )
            [record] = _loss_warnings(caplog)
            assert f"cov_dir {cov_dir} on t1 for app" in record.getMessage()
            assert _state() == before

            # Run 2: absent at the pre-run reset (run 1's test removed it).
            assert not cov_dir.exists()
            run_tests(
                names=["test_loss"],
                run_options=RunOptions(cov=True, cov_clean=True),
                output_dir=tmp_path / "o2",
            )
            assert len(_loss_warnings(caplog)) == 1
            assert _state() == before

            # Run 3: no pre-run reset, so no evidence, though the dir is lost again.
            cov_dir.mkdir(parents=True)
            run_tests(
                names=["test_loss"],
                run_options=RunOptions(cov=True, cov_clean=False),
                output_dir=tmp_path / "o3",
            )
            assert not cov_dir.exists()
            assert len(_loss_warnings(caplog)) == 1
            assert _state() == before
    finally:
        reset_context(token)
    # Inside each run the collection was scheduled (run 3's --no-cov-clean
    # included), and each run's latch was its own: claimable once per run.
    assert seen.read_text().splitlines() == ["True True", "True True", "True True"]
