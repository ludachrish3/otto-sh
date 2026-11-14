"""DEF-WIT's table, pinned as a table (hermetic helpers) and against real shells (the templates),
and the contract suite's one witness context (``tests/conformance/_witness.py``) over it.

Every process a test here starts is its own child and is killed and reaped
in a ``finally``; the only signal sent is the cleanup's own ``kill -9`` to
that child.
"""

import asyncio
import contextlib
import dataclasses
import shutil
import subprocess
from collections.abc import AsyncIterator, Iterator
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest

from otto.host.host import BaseHost
from otto.host.local_host import LocalHost
from otto.utils import wait_for
from scripts.process_group import process_start, process_state
from tests._fixtures import witness_shell as wit
from tests._fixtures.profiles import Cell
from tests.conformance import _witness
from tests.conformance._cells import LOCAL, hermetic_space
from tests.conformance._resolved import ResolvedCell
from tests.conformance._vocabulary import POSIX, ZEPHYR_SHELLS
from tests.conformance._witness import witness

SHELLS = [argv for argv in (["bash"], ["dash"], ["busybox", "sh"]) if shutil.which(argv[0])]
SHELL_IDS = [argv[0] for argv in SHELLS]


@pytest.fixture
def stimulus(tmp_path: Path) -> "Iterator[tuple[subprocess.Popen[str], Path]]":
    """A live process that wrote its own witness, through the shell writer template."""
    path = tmp_path / "witness"
    proc = subprocess.Popen(
        ["sh", "-c", "exec " + wit.long_running_template(str(path), seconds=60)],
        stdout=subprocess.PIPE,
        text=True,
    )
    try:
        assert proc.stdout is not None
        assert proc.stdout.readline().strip() == "otto-cf-started"
        yield proc, path
    finally:
        if proc.poll() is None:
            proc.kill()
        proc.wait(timeout=5)
        if proc.stdout is not None:
            proc.stdout.close()


def _cases(path: Path, pid: int) -> "dict[str, tuple[str | None, str]]":
    """Witness content (None = no file) -> the table's answer, for a live *pid*."""
    started = process_start(pid)
    assert started is not None
    return {
        "live": (f"{pid} {started}\n", wit.RUNNING),
        "missing": (None, wit.NO_WITNESS),
        "empty": ("", wit.NO_WITNESS),
        "colon field": ("1:2 3", wit.UNOBSERVABLE),
        "live pid, colon start": (f"{pid} 1:2", wit.UNOBSERVABLE),
        "leading zero": ("00 1", wit.UNOBSERVABLE),
        "zero pid": ("0 5", wit.UNOBSERVABLE),
        "negative": ("-5 1", wit.UNOBSERVABLE),
        "pid only": (f"{pid}", wit.UNOBSERVABLE),
        "three fields": (f"{pid} {started} 1", wit.UNOBSERVABLE),
        "two lines": (f"{pid}\n{started}", wit.UNOBSERVABLE),
        "start differs": (f"{pid} {started + 1}", wit.STOPPED),
        "pid gone": ("4194303 5", wit.STOPPED),
    }


def _write(path: Path, content: "str | None") -> None:
    path.unlink(missing_ok=True)
    if content is not None:
        path.write_text(content)


def test_the_writer_records_pid_and_kernel_start_time(stimulus):
    proc, path = stimulus
    assert path.read_text() == f"{proc.pid} {process_start(proc.pid)}\n"


def test_the_hermetic_observer_follows_the_table(stimulus):
    proc, path = stimulus
    for name, (content, answer) in _cases(path, proc.pid).items():
        _write(path, content)
        assert wit.observe(path) == answer, name


@pytest.mark.parametrize("argv", SHELLS, ids=SHELL_IDS)
@pytest.mark.parametrize("opts", ["set -eu", "set +eu"])
def test_the_shell_observer_follows_the_table_and_never_fails(stimulus, argv, opts):
    proc, path = stimulus
    for name, (content, answer) in _cases(path, proc.pid).items():
        _write(path, content)
        script = f"{opts}; {wit.process_state_template(str(path))}; echo rc=$?"
        out = subprocess.run(
            [*argv, "-c", script], capture_output=True, text=True, timeout=10, check=False
        ).stdout.split()
        assert out == [answer, "rc=0"], name


def test_a_zombie_is_stopped(tmp_path):
    proc = subprocess.Popen(["true"])
    try:
        started = process_start(proc.pid)
        assert started is not None
        path = tmp_path / "witness"
        path.write_text(f"{proc.pid} {started}\n")
        # The child has exited unreaped (a zombie) once its stat says Z.
        wait_for(lambda: process_state(proc.pid) == "Z", 5, interval=0.01, on_timeout="no zombie")
        assert wit.observe(path) == wit.STOPPED
    finally:
        proc.wait(timeout=5)


@pytest.mark.parametrize("argv", SHELLS, ids=SHELL_IDS)
def test_the_shell_cleanup_kills_only_a_running_witness_and_removes_the_file(stimulus, argv):
    proc, path = stimulus
    script = f"set -eu; {wit.witness_cleanup_template(str(path))}; echo rc=$?"
    out = subprocess.run(
        [*argv, "-c", script], capture_output=True, text=True, timeout=10, check=False
    ).stdout.split()
    assert out[-2:] == [wit.RUNNING, "rc=0"]
    assert proc.wait(timeout=5) == -9
    assert not path.exists()
    # A stopped witness is only removed.
    path.write_text("4194303 5\n")
    out = subprocess.run(
        [*argv, "-c", script], capture_output=True, text=True, timeout=10, check=False
    ).stdout.split()
    assert out[-2:] == [wit.STOPPED, "rc=0"]
    assert not path.exists()


def test_the_hermetic_cleanup_matches(stimulus):
    proc, path = stimulus
    assert wit.clean(path) == wit.RUNNING
    assert proc.wait(timeout=5) == -9
    assert not path.exists()
    assert wit.clean(path) == wit.NO_WITNESS


# ---------------------------------------------------------------------------
# The vocabulary carries these objects, not second spellings of them.
# ---------------------------------------------------------------------------


def test_the_vocabulary_s_witness_templates_are_witness_shell_s_statements():
    p = "/tmp/otto-cf-x"
    assert POSIX.process_state_template is not None
    assert POSIX.witness_cleanup_template is not None
    assert POSIX.pid_long_running_template is not None
    assert POSIX.witness_running_template is not None
    assert POSIX.witness_finished_template is not None
    assert POSIX.process_state_template.format(path=p) == wit.process_state_template(p)
    assert POSIX.witness_cleanup_template.format(path=p) == wit.witness_cleanup_template(p)
    assert POSIX.pid_long_running_template.format(path=p) == wit.long_running_template(
        p, seconds=10
    )
    assert POSIX.exec_pid_long_running_template is not None
    assert POSIX.exec_pid_long_running_template.format(
        path=p
    ) == "exec " + wit.long_running_template(p, seconds=10)
    assert POSIX.witness_running_template.format(path=p) == wit.running_in_background_template(p)
    assert POSIX.witness_finished_template.format(path=p) == wit.finished_template(p)
    for words in ZEPHYR_SHELLS.values():
        assert words.process_state_template is None
        assert words.witness_cleanup_template is None


@pytest.mark.parametrize("argv", SHELLS, ids=SHELL_IDS)
def test_the_running_stimulus_is_never_a_job_of_the_shell_that_starts_it(argv, tmp_path):
    path = tmp_path / "witness"
    try:
        done = subprocess.run(
            [*argv, "-c", f"{wit.running_in_background_template(str(path))}; jobs"],
            capture_output=True,
            text=True,
            check=True,
            timeout=10,
        )
        assert done.stdout == ""
        wait_for(path.exists, 5, interval=0.01, on_timeout="the running stimulus wrote no witness")
        wait_for(
            lambda: wit.observe(path) == wit.RUNNING,
            5,
            interval=0.01,
            on_timeout="the running stimulus is not running",
        )
    finally:
        assert wit.clean(path) in (wit.RUNNING, wit.NO_WITNESS)


def test_the_finished_stimulus_leaves_a_stopped_witness(tmp_path):
    path = tmp_path / "witness"
    subprocess.run(["sh", "-c", wit.finished_template(str(path))], check=True, timeout=10)
    assert wit.WITNESS_FORMAT.fullmatch(path.read_text())
    assert wit.observe(path) == wit.STOPPED


# ---------------------------------------------------------------------------
# The witness context: hermetic observer, and the remote observer over a fake host.
# ---------------------------------------------------------------------------


_NO_HOST = cast("BaseHost", None)
"""The host a hermetic witness is given: its observer reads the runner's ``/proc`` and never
touches the host."""


def _hermetic_cell() -> ResolvedCell:
    return next(r for r in hermetic_space() if r.kind == LOCAL)


def _write_witness(path: str, pid: int) -> None:
    started = process_start(pid)
    assert started is not None
    Path(path).write_text(f"{pid} {started}\n")


@contextlib.contextmanager
def _zombie() -> "Iterator[int]":
    """A child that has exited unreaped: its ``stat`` reads ``Z`` until the ``finally`` reaps it."""
    proc = subprocess.Popen(["true"])
    try:
        wait_for(lambda: process_state(proc.pid) == "Z", 5, interval=0.01, on_timeout="no zombie")
        yield proc.pid
    finally:
        proc.wait(timeout=5)


@contextlib.contextmanager
def _sleeper() -> "Iterator[subprocess.Popen[bytes]]":
    proc = subprocess.Popen(["sleep", "30"])
    try:
        yield proc
    finally:
        if proc.poll() is None:
            proc.kill()
        proc.wait(timeout=5)


@pytest.mark.asyncio
async def test_a_hermetic_witness_reads_running_then_stopped_and_its_exit_removes_the_file():
    with _sleeper() as proc:
        async with witness(_hermetic_cell(), _NO_HOST) as w:
            assert w.path.startswith("/tmp/otto-cf-timeout-")
            _write_witness(w.path, proc.pid)
            assert await w.state() == "running"
            proc.kill()
            proc.wait(timeout=5)
            assert await w.state() == "stopped"
        assert not Path(w.path).exists()


@pytest.mark.asyncio
async def test_a_hermetic_witness_reads_a_zombie_as_stopped():
    with _zombie() as pid:
        async with witness(_hermetic_cell(), _NO_HOST, purpose="zombie") as w:
            _write_witness(w.path, pid)
            assert await w.state() == "stopped"


@pytest.mark.asyncio
async def test_the_witness_exit_kills_a_process_still_running():
    with _sleeper() as proc:
        async with witness(_hermetic_cell(), _NO_HOST) as w:
            _write_witness(w.path, proc.pid)
        assert proc.wait(timeout=5) == -9


@pytest.mark.asyncio
async def test_a_never_written_witness_raises_no_start_witness(monkeypatch):
    monkeypatch.setattr(_witness, "_START_WAIT_S", 0.2)
    async with witness(_hermetic_cell(), _NO_HOST) as w:
        with pytest.raises(AssertionError, match="no start witness"):
            await w.state()


@pytest.mark.asyncio
async def test_a_witness_written_after_state_is_asked_is_awaited(monkeypatch):
    looks: list[str] = []
    real = wit.observe

    def observe_then_write(path: str) -> str:
        # The first look finds nothing; the stimulus writes right after it.
        answer = real(path)
        looks.append(answer)
        if len(looks) == 1:
            _write_witness(path, proc.pid)
        return answer

    monkeypatch.setattr(wit, "observe", observe_then_write)
    with _sleeper() as proc:
        async with witness(_hermetic_cell(), _NO_HOST) as w:
            assert await w.state() == "running"
    assert looks[:2] == [wit.NO_WITNESS, wit.RUNNING]


class _ShellHost:
    """A stand-in remote host: ``run`` runs its line in a fresh local ``sh``."""

    def __init__(self) -> None:
        self.lines: list[str] = []

    async def run(self, cmd: str) -> Any:
        self.lines.append(cmd)
        proc = await asyncio.create_subprocess_exec("sh", "-c", cmd, stdout=asyncio.subprocess.PIPE)
        out, _ = await proc.communicate()
        return SimpleNamespace(only=SimpleNamespace(value=out.decode().rstrip("\n")))


def _bed_cell(term: str, opener: Any) -> ResolvedCell:
    return dataclasses.replace(
        _hermetic_cell(), cell=Cell("test1", term, "sftp"), kind="bed-unix", open_host=opener
    )


@pytest.mark.asyncio
async def test_a_remote_witness_on_ssh_reads_through_one_second_host():
    second = _ShellHost()
    opens: list[int] = []

    @contextlib.asynccontextmanager
    async def opener() -> "AsyncIterator[BaseHost]":
        opens.append(1)
        yield cast("BaseHost", second)

    under_test = _ShellHost()
    with _sleeper() as proc:
        async with witness(_bed_cell("ssh", opener), cast("BaseHost", under_test)) as w:
            _write_witness(w.path, proc.pid)
            assert await w.state() == "running"
            assert await w.state() == "running"
        assert proc.wait(timeout=5) == -9
    assert opens == [1]
    assert under_test.lines == []
    assert second.lines[-1] == wit.witness_cleanup_template(w.path)
    assert not Path(w.path).exists()


@pytest.mark.asyncio
async def test_a_remote_witness_on_a_console_reads_through_the_host_under_test():
    @contextlib.asynccontextmanager
    async def opener() -> "AsyncIterator[BaseHost]":
        raise AssertionError("a console takes one client: no second host")
        yield  # pragma: no cover

    host = _ShellHost()
    with _sleeper() as proc:
        async with witness(_bed_cell("console", opener), cast("BaseHost", host)) as w:
            _write_witness(w.path, proc.pid)
            assert await w.state() == "running"
            proc.kill()
            proc.wait(timeout=5)
            assert await w.state() == "stopped"
    assert host.lines[0] == wit.process_state_template(w.path)


@pytest.mark.asyncio
async def test_a_remote_answer_that_is_not_running_or_stopped_is_an_observation_failure():
    host = _ShellHost()
    async with witness(_bed_cell("console", None), cast("BaseHost", host)) as w:
        Path(w.path).write_text("0 5\n")
        with pytest.raises(AssertionError, match="observation failed"):
            await w.state()


# ---------------------------------------------------------------------------
# The observer through a real LocalSession (bash on pipes): it never ends the shell.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_the_observer_through_a_local_session_answers_and_keeps_the_shell(tmp_path):
    path = str(tmp_path / "witness")

    async def ask(host: LocalHost) -> "list[str]":
        answer = (await host.run(wit.process_state_template(path))).only.value
        after = (await host.run("echo next")).only.value
        return [answer, after]

    async with LocalHost() as host:
        assert await ask(host) == [wit.NO_WITNESS, "next"]
        with _sleeper() as proc:
            _write_witness(path, proc.pid)
            assert await ask(host) == [wit.RUNNING, "next"]
            proc.kill()
            proc.wait(timeout=5)
            assert await ask(host) == [wit.STOPPED, "next"]
        with _zombie() as pid:
            _write_witness(path, pid)
            assert await ask(host) == [wit.STOPPED, "next"]


@pytest.mark.asyncio
async def test_a_remote_cell_with_no_observer_refuses_to_start_a_witness():
    cell = dataclasses.replace(
        _bed_cell("ssh", None),
        vocabulary=dataclasses.replace(POSIX, process_state_template=None),
    )
    with pytest.raises(RuntimeError, match="no witness observer"):
        async with witness(cell, cast("BaseHost", _ShellHost())):
            pass


@pytest.mark.asyncio
async def test_a_failing_cleanup_never_replaces_the_body_s_failure(monkeypatch):
    def broken_clean(path: str) -> str:
        raise OSError("cleanup broke")

    monkeypatch.setattr(wit, "clean", broken_clean)
    with pytest.raises(AssertionError, match="the body's own"):
        async with witness(_hermetic_cell(), _NO_HOST):
            raise AssertionError("the body's own failure")
    with pytest.raises(OSError, match="cleanup broke"):
        async with witness(_hermetic_cell(), _NO_HOST):
            pass
