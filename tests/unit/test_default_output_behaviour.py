"""Where output goes when nobody says: the behaviour, through the existing API only.

Every library entry point and the CLI land a run under ONE default root,
``$OTTO_HOME/otto-xdir``, in the CLI's run shape ``<xdir>/<command>/<ts>[_<sub>]/``
(issue #758). These tests import nothing that change added: the expected paths
are spelled out from ``OTTO_HOME`` by hand, so each one runs (and fails for the
real reason) against the code it replaced.

Each test pins its own ``OTTO_HOME`` (the root conftest's one is per worker,
shared by every test on it) and starts in an unrelated working directory that
must stay empty.
"""

import asyncio
import contextvars
import re
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest
from typer.testing import CliRunner

from otto.cli.main import app
from otto.cli.registry import register_cli_command
from otto.result import CommandResult
from otto.utils import Status
from tests.unit.cli import conftest as _cli_conftest

_RUN_NAME = re.compile(r"^\d{8}_\d{6}_\d{3}(_.+)?$")
_PROBE = "_default_output_probe"


async def _probe() -> CommandResult:
    return CommandResult(status=Status.Success, value="", command="probe", retcode=0)


register_cli_command(_PROBE, _probe, help="internal test probe", gate=False)

# The CLI harness: lab, bootstrap and console mocked, init_cli_logging and
# create_output_dir real. Bound by assignment so the fixture keeps its name here.
real_main_mocks = _cli_conftest.real_main_mocks


@pytest.fixture
def home(tmp_path, monkeypatch) -> Path:
    """A private ``OTTO_HOME`` and an unrelated, empty working directory."""
    home = tmp_path / "home"
    monkeypatch.setenv("OTTO_HOME", str(home))
    monkeypatch.delenv("OTTO_XDIR", raising=False)
    work = tmp_path / "work"
    work.mkdir()
    monkeypatch.chdir(work)
    return home


def _bare(fn, *args, **kwargs):
    """Call *fn* in a fresh, empty execution context: no run context and no policy."""
    return contextvars.Context().run(fn, *args, **kwargs)


def _assert_a_run_dir_of(run: Path, command_dir: Path, *, suffix: str = "") -> None:
    assert run.parent == command_dir, run
    assert _RUN_NAME.match(run.name), run.name
    assert run.name.endswith(suffix), run.name


def test_a_bare_log_dest_lands_under_the_home_xdir(home, tmp_path):
    from otto.host.local_host import LocalHost

    got = _bare(LocalHost().log_dest)

    assert got.name == "local"
    assert got.parent.name == "logs"
    _assert_a_run_dir_of(got.parent.parent, home / "otto-xdir" / "host", suffix="_get_logs")
    assert list((tmp_path / "work").iterdir()) == []


@pytest.mark.asyncio
async def test_a_bare_get_coverage_does_not_refuse(home, tmp_path, monkeypatch):
    from otto.coverage.collect import CollectResult
    from otto.coverage.get import get_coverage
    from otto.coverage.tree import ProductKey
    from tests._fixtures.gitrepo import TmpGitRepo
    from tests.unit.cov.test_get import _embedded_board, _produced, _project

    sut = TmpGitRepo(tmp_path / "sut")
    sut.write("f.c", "int a;\n")
    sut.commit("init")
    project = _project({"hosts": ".*"}, root=sut.root)
    board = _embedded_board("board1")
    monkeypatch.setattr("otto.fleet.all_hosts", lambda pattern=None, **kw: iter([board]))

    async def collect(cov_dir, **_kw):
        cap = cov_dir / "board1" / "app" / "capture.json"
        return CollectResult(
            cov_dir=cov_dir,
            product_dirs={ProductKey("board1", "app"): cap.parent},
            produce=_produced(cap),
        )

    with patch("otto.coverage.collect.collect_coverage", AsyncMock(side_effect=collect)):
        report = await contextvars.Context().run(
            asyncio.ensure_future, get_coverage(projects=[project])
        )

    assert report.cov_dir.name == "cov"
    _assert_a_run_dir_of(
        report.cov_dir.parent, (home / "otto-xdir" / "cov").resolve(), suffix="_get"
    )
    assert list((tmp_path / "work").iterdir()) == []


def test_a_bare_run_tests_lands_under_the_home_xdir(home, tmp_path, monkeypatch):
    from otto.suite.run import run_tests
    from tests._fixtures.bootstrap_seam import patch_bootstrap
    from tests._fixtures.run_projects import DOUBLE_TEST_NAME, project_double, pytest_main_returning

    patch_bootstrap(monkeypatch, [project_double(tmp_path / "proj")])
    monkeypatch.setattr("pytest.main", pytest_main_returning())

    result = _bare(run_tests, names=[DOUBLE_TEST_NAME])

    _assert_a_run_dir_of(result.output_dir, home / "otto-xdir" / "test")
    assert list((tmp_path / "work").iterdir()) == []


def _cli_run(args: "list[str]") -> Path:
    """Drive the probe through the CLI; return the run dir it was given."""
    from otto.logger import management

    result = CliRunner().invoke(app, [*args, "--lab", "test_lab", _PROBE])
    assert result.exit_code == 0, result.output
    run = management._state.last_output_dir
    assert run is not None
    return run


def test_the_cli_without_xdir_lands_under_the_home_xdir(real_main_mocks, home, tmp_path):
    run = _cli_run([])

    _assert_a_run_dir_of(run, home / "otto-xdir" / _PROBE)
    assert list((tmp_path / "work").iterdir()) == []


def test_an_explicit_xdir_has_no_otto_output_level(real_main_mocks, home, tmp_path):
    given = tmp_path / "given"
    given.mkdir()

    run = _cli_run(["--xdir", str(given)])

    _assert_a_run_dir_of(run, given / _PROBE)
    assert not (given / "otto-output").exists()
