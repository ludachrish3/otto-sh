"""``otto.output_dir``: the one answer to "where does this library call's output go".

The order is an explicit destination, then the run policy's output directory,
then a fresh run dir under the default xdir, built by the CLI's own builder and
memoised per process and entry. The resolver creates nothing; a writer
materialises the library run dir (with otto's marker) when it writes.

Every test imports what the change added INSIDE its body, so a missing name
fails that test alone and never the collection of the file. Each pins its own
``OTTO_HOME`` (the root conftest's is shared by the whole worker) and runs the
call in a fresh, empty execution context.
"""

import asyncio
import contextvars
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest
from typer.testing import CliRunner

from otto.cli.registry import register_cli_command
from otto.result import CommandResult
from otto.utils import Status
from tests.unit.cli import conftest as _cli_conftest

_RUN_NAME = re.compile(r"^\d{8}_\d{6}_\d{3}(_.+)?$")
_PROBE = "_output_dir_probe"


async def _probe() -> CommandResult:
    return CommandResult(status=Status.Success, value="", command="probe", retcode=0)


register_cli_command(_PROBE, _probe, help="internal test probe", gate=False)

# The CLI harness: lab, bootstrap and console mocked, init_cli_logging and
# create_output_dir real. Bound by assignment so the fixture keeps its name here.
real_main_mocks = _cli_conftest.real_main_mocks


@pytest.fixture
def home(tmp_path, monkeypatch) -> Path:
    """A private ``OTTO_HOME``, no ``OTTO_XDIR``, and an unrelated working directory."""
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


def _with_policy(output_dir: Path, fn, *args, **kwargs):
    """Call *fn* in a fresh context holding only a policy (no run context)."""
    from otto.invocation import RunPolicy, install_policy

    def call():
        install_policy(RunPolicy(output_dir=output_dir))
        return fn(*args, **kwargs)

    return contextvars.Context().run(call)


# ── step 2: the resolver's order ─────────────────────────────────────────────


def test_an_explicit_dest_wins_and_creates_nothing(home, tmp_path):
    from otto.output_dir import LIBRARY_TEST, resolve_output_dir

    given = tmp_path / "given"
    assert _bare(resolve_output_dir, given, entry=LIBRARY_TEST) == given
    assert _with_policy(tmp_path / "run", resolve_output_dir, given, entry=LIBRARY_TEST) == given
    assert not given.exists()
    assert not home.exists()


def test_the_installed_policy_is_the_one_source_even_with_no_context(home, tmp_path):
    from otto.output_dir import LIBRARY_COV_GET, resolve_output_dir

    got = _with_policy(tmp_path / "run", resolve_output_dir, None, entry=LIBRARY_COV_GET)
    assert got == tmp_path / "run"
    assert not home.exists()


def test_with_no_run_dir_a_fresh_run_dir_under_the_default_xdir_is_resolved_and_nothing_is_created(
    home, tmp_path
):
    from otto import layout
    from otto.home_root import default_xdir
    from otto.output_dir import LIBRARY_COV_GET, resolve_output_dir

    got = _bare(resolve_output_dir, None, entry=LIBRARY_COV_GET)

    assert got.parent == layout.command_output_dir(default_xdir(), "cov")
    assert layout.RUN_DIR_NAME_RE.match(got.name)
    assert got.name.endswith("_get")
    assert not (tmp_path / "home").exists()
    assert list((tmp_path / "work").iterdir()) == []


def test_a_policy_with_no_output_dir_gets_the_library_default(home, tmp_path):
    from otto import layout
    from otto.home_root import default_xdir
    from otto.invocation import RunPolicy, install_policy
    from otto.output_dir import LIBRARY_TEST, resolve_output_dir

    def call():
        install_policy(RunPolicy())
        return resolve_output_dir(None, entry=LIBRARY_TEST)

    got = contextvars.Context().run(call)
    assert got.parent == layout.command_output_dir(default_xdir(), "test")
    # No stamping: the default never becomes the installed policy's own.
    assert not home.exists()


def test_the_library_default_is_memoised_per_entry_and_default_xdir(home, tmp_path, monkeypatch):
    from otto.output_dir import LIBRARY_LOGS, LIBRARY_TEST, resolve_output_dir

    first = _bare(resolve_output_dir, None, entry=LIBRARY_LOGS)
    assert _bare(resolve_output_dir, None, entry=LIBRARY_LOGS) == first

    other = _bare(resolve_output_dir, None, entry=LIBRARY_TEST)
    assert other.parent.parent == first.parent.parent
    assert other.parent != first.parent

    monkeypatch.setenv("OTTO_HOME", str(tmp_path / "home2"))
    moved = _bare(resolve_output_dir, None, entry=LIBRARY_LOGS)
    assert moved.parent.parent == tmp_path / "home2" / "otto-xdir"
    # And the first home still answers with its own dir.
    monkeypatch.setenv("OTTO_HOME", str(home))
    assert _bare(resolve_output_dir, None, entry=LIBRARY_LOGS) == first


def test_forget_library_output_dirs_drops_the_memo(home, monkeypatch):
    from otto import layout
    from otto.output_dir import LIBRARY_TEST, forget_library_output_dirs, resolve_output_dir

    real = layout.run_output_dir
    stamps = iter(
        [
            datetime(2026, 7, 3, 12, 0, 0, tzinfo=timezone.utc),
            datetime(2026, 7, 3, 12, 0, 1, tzinfo=timezone.utc),
        ]
    )
    monkeypatch.setattr(
        "otto.layout.run_output_dir",
        lambda xdir, entry, **_kw: real(xdir, entry, started=next(stamps)),
    )
    first = _bare(resolve_output_dir, None, entry=LIBRARY_TEST)
    forget_library_output_dirs()
    second = _bare(resolve_output_dir, None, entry=LIBRARY_TEST)
    assert first != second
    assert first.parent == second.parent


# ── step 3b: every library entry point goes through the one resolver ────────

_T0 = datetime(2026, 7, 3, 12, 0, 0, tzinfo=timezone.utc)


class _Alloc:
    def __init__(self) -> None:
        self.calls: list = []


@pytest.fixture
def alloc(monkeypatch):
    """Record every run-dir allocation, on a deterministic clock (one second apart)."""
    from otto import layout

    real = layout.run_output_dir
    record = _Alloc()

    def wrapper(xdir, entry, *, started=None):
        record.calls.append((xdir, entry))
        return real(xdir, entry, started=_T0 + timedelta(seconds=len(record.calls)))

    monkeypatch.setattr("otto.layout.run_output_dir", wrapper)
    return record


def _log_dest_adapter(tmp_path, monkeypatch, dest):
    from otto import layout
    from otto.host.local_host import LocalHost

    return layout.run_dir_of(LocalHost().log_dest(dest=dest))


def _run_tests_adapter(tmp_path, monkeypatch, dest):
    from otto.suite.run import run_tests
    from tests._fixtures.bootstrap_seam import patch_bootstrap
    from tests._fixtures.run_projects import DOUBLE_TEST_NAME, project_double, pytest_main_returning

    patch_bootstrap(monkeypatch, [project_double(tmp_path / "proj")])
    monkeypatch.setattr("pytest.main", pytest_main_returning())
    return run_tests(names=[DOUBLE_TEST_NAME], output_dir=dest).output_dir


def _git_sut(tmp_path: Path) -> Path:
    from tests._fixtures.gitrepo import TmpGitRepo

    root = tmp_path / "sut"
    if not root.exists():
        sut = TmpGitRepo(root)
        sut.write("f.c", "int a;\n")
        sut.commit("init")
    return root


def _get_coverage_adapter(tmp_path, monkeypatch, dest):
    # Backend mocks prove routing, not artifacts (the collect engine is patched).
    from otto.coverage.collect import CollectResult
    from otto.coverage.get import get_coverage
    from otto.coverage.tree import ProductKey
    from tests.unit.cov.test_get import _embedded_board, _produced, _project

    project = _project({"hosts": ".*"}, root=_git_sut(tmp_path))
    board = _embedded_board("board1")
    monkeypatch.setattr("otto.fleet.all_hosts", lambda pattern=None, **kw: iter([board]))
    monkeypatch.setattr("otto.host.toolchain_discovery.system_gcov_major", lambda gcov="gcov": 13)

    async def collect(cov_dir, **_kw):
        cap = cov_dir / "board1" / "app" / "capture.json"
        return CollectResult(
            cov_dir=cov_dir,
            product_dirs={ProductKey("board1", "app"): cap.parent},
            produce=_produced(cap),
        )

    with patch("otto.coverage.collect.collect_coverage", AsyncMock(side_effect=collect)):
        report = asyncio.run(get_coverage(projects=[project], output_dir=dest))
    return report.cov_dir.parent


def _entries():
    from otto.output_dir import LIBRARY_COV_GET, LIBRARY_LOGS, LIBRARY_TEST

    return {"log_dest": LIBRARY_LOGS, "run_tests": LIBRARY_TEST, "get_coverage": LIBRARY_COV_GET}


_ADAPTERS = {
    "log_dest": _log_dest_adapter,
    "run_tests": _run_tests_adapter,
    "get_coverage": _get_coverage_adapter,
}


def _drive(name, mode, tmp_path, monkeypatch) -> Path:
    """Call adapter *name* in *mode*; return the run dir it used (twice: both must agree)."""
    adapter = _ADAPTERS[name]
    dest = tmp_path / "given" if mode == "explicit" else None
    if mode == "policy":
        used = [
            _with_policy(tmp_path / "run", adapter, tmp_path, monkeypatch, dest) for _ in range(2)
        ]
    else:
        used = [_bare(adapter, tmp_path, monkeypatch, dest) for _ in range(2)]
    assert used[0] == used[1], used
    return used[0]


@pytest.mark.parametrize("mode", ["default", "explicit", "policy"])
@pytest.mark.parametrize("name", list(_ADAPTERS))
def test_every_library_entry_point_resolves_through_the_one_resolver(
    home, tmp_path, monkeypatch, alloc, name, mode
):
    from otto import layout
    from otto.home_root import default_xdir
    from otto.output_dir import resolve_output_dir

    entry = _entries()[name]
    used = _drive(name, mode, tmp_path, monkeypatch)

    if mode == "default":
        assert alloc.calls == [(default_xdir(), entry)]
        assert _bare(resolve_output_dir, None, entry=entry).resolve() == used.resolve()
        assert alloc.calls == [(default_xdir(), entry)]
        assert (
            used.resolve().parent
            == layout.command_output_dir(default_xdir(), entry.command).resolve()
        )
    elif mode == "explicit":
        assert used.resolve() == (tmp_path / "given").resolve()
        assert alloc.calls == []
    else:
        assert used.resolve() == (tmp_path / "run").resolve()
        assert alloc.calls == []


def test_a_library_created_run_dir_carries_the_marker(home, tmp_path, monkeypatch):
    from otto import layout
    from otto.host.local_host import LocalHost

    host = LocalHost()

    async def haul():
        return await host.get_debug_logs()

    result = _bare(asyncio.run, haul())
    assert result.is_ok, result
    run = _bare(lambda: layout.run_dir_of(host.log_dest()))
    assert (run / layout.RUN_MARKER).is_file()
    assert layout.is_run_dir(run)

    for name in ("run_tests", "get_coverage"):
        used = _bare(_ADAPTERS[name], tmp_path, monkeypatch, None)
        assert layout.is_run_dir(used), name

    # An explicit destination is the caller's: it is never marked.
    given = _bare(_ADAPTERS["run_tests"], tmp_path, monkeypatch, tmp_path / "given")
    assert not (given / layout.RUN_MARKER).exists()


def test_a_query_alone_creates_nothing(home, tmp_path):
    from otto.host.local_host import LocalHost

    _bare(LocalHost().log_dest)
    assert not home.exists()


def test_the_cli_and_the_library_create_run_dirs_through_make_run_dir(home, tmp_path, monkeypatch):
    from otto import layout
    from otto.home_root import default_xdir
    from otto.logger import management

    real = layout.make_run_dir
    made: list = []

    def recording(path, *, exist_ok=False):
        made.append(path)
        return real(path, exist_ok=exist_ok)

    monkeypatch.setattr("otto.layout.make_run_dir", recording)
    management.init_cli_logging(xdir=default_xdir(), log_level="INFO", keep_days=7)
    cli_run = management.create_output_dir("test")
    lib_run = _bare(_ADAPTERS["run_tests"], tmp_path, monkeypatch, None)

    assert made == [cli_run, lib_run]
    for run in made:
        assert run.parent.parent == default_xdir()
        assert layout.is_run_dir(run)


# ── the CLI and a library call share the one default xdir ───────────────────


def _cli_run(args: "list[str]") -> Path:
    from otto.cli.main import app
    from otto.logger import management

    result = CliRunner().invoke(app, [*args, "--lab", "test_lab", _PROBE])
    assert result.exit_code == 0, result.output
    run = management._state.last_output_dir
    assert run is not None
    return run


def test_the_cli_and_a_library_call_consume_the_one_default_xdir(
    real_main_mocks, home, tmp_path, monkeypatch
):
    from otto import layout
    from otto.host.local_host import LocalHost

    sentinel = tmp_path / "sentinel"
    monkeypatch.setattr("otto.home_root.default_xdir", lambda: sentinel)

    cli_run = _cli_run([])
    lib_run = _bare(lambda: layout.run_dir_of(LocalHost().log_dest()))

    assert cli_run.parent == sentinel / _PROBE
    assert _RUN_NAME.match(cli_run.name)
    assert lib_run.parent == sentinel / "host"
    assert cli_run.parent.parent == lib_run.parent.parent == sentinel
    assert list((tmp_path / "work").iterdir()) == []
    assert not (home / "otto-xdir").exists()


@pytest.mark.parametrize("how", ["flag", "env"])
def test_an_explicit_xdir_is_created(real_main_mocks, home, tmp_path, monkeypatch, how):
    from otto import layout

    given = tmp_path / "new" / "d"
    if how == "flag":
        run = _cli_run(["--xdir", str(given)])
    else:
        monkeypatch.setenv("OTTO_XDIR", str(given))
        run = _cli_run([])

    assert run.parent == given / _PROBE
    assert layout.is_run_dir(run)
    assert not (home / "otto-xdir").exists()
