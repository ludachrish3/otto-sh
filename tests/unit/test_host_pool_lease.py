"""Host-pool lease mechanics across processes and sessions (no VM — flock over a tmp dir)."""

from __future__ import annotations

import ast
import fcntl
import getpass
import multiprocessing as mp
import os
import subprocess
import sys
import tempfile
import textwrap
import time
from contextlib import ExitStack
from pathlib import Path
from types import SimpleNamespace
from typing import NamedTuple

import pytest

from otto.utils import WaitTimeoutError, wait_for
from scripts.process_group import group_alive, signal_group
from tests._fixtures import _host_pool
from tests._fixtures._host_pool import (
    DOCKER_HOST,
    UNIX_POOL,
    SelfLeaseError,
    docker_host_lease,
    lease_unix_host,
)
from tests._fixtures.bed_locks import BED_LOCK_ROOT_ENV, bed_lock_root
from tests._fixtures.paths import CHECKOUT_ROOT
from tests._fixtures.process_group import abort_group_in_turn


def test_lease_yields_a_pool_member(tmp_path: Path) -> None:
    with lease_unix_host(lock_dir=tmp_path) as element:
        assert element in UNIX_POOL


def test_two_leases_pick_distinct_hosts(tmp_path: Path) -> None:
    with lease_unix_host(lock_dir=tmp_path) as a, lease_unix_host(lock_dir=tmp_path) as b:
        assert a != b  # second lease skips the busy first host


@pytest.mark.serial_timing
def test_lease_releases_on_exit(tmp_path: Path) -> None:
    with lease_unix_host(["test1"], lock_dir=tmp_path) as a:
        assert a == "test1"
    # test1 is free again — re-leasing the single-host pool succeeds immediately
    start = time.monotonic()
    with lease_unix_host(["test1"], lock_dir=tmp_path) as b:
        assert b == "test1"
    assert time.monotonic() - start < 1.0


def _hold(lock_dir: str, secs: float, q) -> None:
    with lease_unix_host(["test1"], lock_dir=Path(lock_dir)):
        q.put("held")
        time.sleep(secs)


def test_lease_is_cross_process(tmp_path: Path) -> None:
    """A second process blocks until the first releases (cross-process safety)."""
    q = mp.Queue()
    p = mp.Process(target=_hold, args=(str(tmp_path), 0.5, q))
    p.start()
    assert q.get(timeout=5) == "held"
    start = time.monotonic()
    with lease_unix_host(["test1"], lock_dir=tmp_path):
        waited = time.monotonic() - start
    p.join()
    assert waited >= 0.3  # had to wait for the other process to release


# Each session first waits at a barrier until the other has arrived, so both
# ask for the lease at the same moment however long xdist takes to start a
# worker. Without the barrier a -n0 session can finish its hold before the
# xdist worker exists, and two separate lock files would still log in order.
_HOLD = textwrap.dedent("""
    import os, time
    from pathlib import Path
    from otto.utils import wait_for
    from tests._fixtures._host_pool import docker_host_lease

    def test_hold():
        barrier = Path(os.environ["HOLD_BARRIER"])
        with barrier.open("a") as f:
            f.write("ready\\n")
        wait_for(
            lambda: barrier.read_text().count("ready") == 2,
            30,
            interval=0.01,
            on_timeout="the other session never reached the barrier",
        )
        with docker_host_lease():
            log = Path(os.environ["HOLD_LOG"])
            with log.open("a") as f:
                f.write(f"start {time.monotonic_ns()}\\n")
            time.sleep(1.5)
            with log.open("a") as f:
                f.write(f"end {time.monotonic_ns()}\\n")
""")


class _Session(NamedTuple):
    cmd: list[str]
    proc: subprocess.Popen[bytes]
    log: Path


def _start_session(
    cmd: list[str], *, env: dict[str, str], cwd: Path, log: Path, stack: ExitStack
) -> _Session:
    """Start a child pytest session in a process group of its own, its output in *log*.

    PYTHONFAULTHANDLER keeps faulthandler armed in every process of the group,
    the xdist worker included, for the whole life of the interpreter: pytest's
    own faulthandler plugin disarms at unconfigure, before the teardown a
    session can hang in. PYTHONUNBUFFERED keeps what was printed before an
    abort. *stack* kills whatever is left of the group on the way out.
    """
    sink = stack.enter_context(log.open("wb"))
    proc = subprocess.Popen(
        cmd,
        env={**env, "PYTHONFAULTHANDLER": "1", "PYTHONUNBUFFERED": "1"},
        cwd=cwd,
        stdout=sink,
        stderr=subprocess.STDOUT,
        start_new_session=True,
    )
    stack.callback(_kill_session, proc)
    return _Session(cmd, proc, log)


def _kill_session(proc: subprocess.Popen[bytes]) -> None:
    signal_group(proc.pid)
    proc.wait()


def _outputs(runs: list[_Session]) -> str:
    return "\n".join(f"--- {' '.join(r.cmd)}\n{r.log.read_text()}" for r in runs)


def _wait_for_sessions(runs: list[_Session], timeout: float) -> list[int]:
    """Return the sessions' exit codes, or fail with the stacks of the ones still running.

    Every process of a session still running at *timeout* is aborted in turn
    (:func:`~tests._fixtures.process_group.abort_group_in_turn`), so the
    failure shows where each was stuck instead of only that one was.
    """
    try:
        wait_for(
            lambda: all(r.proc.poll() is not None for r in runs),
            timeout,
            on_timeout=f"a child session was still running after {timeout}s",
        )
    except WaitTimeoutError as e:
        hung = [r.proc for r in runs if r.proc.poll() is None]
        survivors = [pid for proc in hung for pid in abort_group_in_turn(proc.pid)]
        for proc in hung:
            if proc.pid not in survivors:
                proc.wait()  # dead already: reap it
        cut = f"; {survivors} outlived SIGABRT, so a dump may be cut short" if survivors else ""
        pytest.fail(
            f"{e}; SIGABRT made each of its processes dump its threads{cut}:\n{_outputs(runs)}"
        )
    return [r.proc.returncode for r in runs]


_WEDGES = {
    # The worker never finishes its test.
    "in_the_test": (
        "test_wedge.py",
        """
        import time
        from pathlib import Path

        def test_wedged_in_the_worker():
            Path({started!r}).touch()
            time.sleep(600)
        """,
        "test_wedged_in_the_worker",
    ),
    # The test passes; both processes hang at exit, after pytest disarmed its faulthandler.
    "after_unconfigure": (
        "conftest.py",
        """
        import atexit, time
        from pathlib import Path

        def _wedged_at_exit():
            Path({started!r}).touch()
            time.sleep(600)

        atexit.register(_wedged_at_exit)
        """,
        "_wedged_at_exit",
    ),
}


@pytest.mark.parametrize("wedge", sorted(_WEDGES))
def test_a_hung_session_fails_with_its_stacks(tmp_path: Path, wedge: str) -> None:
    """A child xdist session that never ends is aborted, and where it hangs is the failure."""
    filename, source, frame = _WEDGES[wedge]
    started = tmp_path / "started"
    (tmp_path / filename).write_text(textwrap.dedent(source.format(started=str(started))))
    if not (tmp_path / "test_wedge.py").exists():
        (tmp_path / "test_wedge.py").write_text("def test_passes():\n    pass\n")
    cmd = [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", "-o", "addopts="]
    with ExitStack() as stack:
        runs = [
            _start_session(
                [*cmd, "--rootdir", str(tmp_path), "-n", "1", str(tmp_path / "test_wedge.py")],
                env=dict(os.environ),
                cwd=tmp_path,
                log=tmp_path / "wedge.out",
                stack=stack,
            )
        ]

        def wedged() -> bool:
            if runs[0].proc.poll() is not None:
                raise AssertionError(f"the session ended before it wedged:\n{_outputs(runs)}")
            return started.exists()

        wait_for(wedged, 60, on_timeout="the session never reached its wedge")
        with pytest.raises(pytest.fail.Exception, match=rf"line \d+ in {frame}"):
            _wait_for_sessions(runs, 0.1)
        pgid = runs[0].proc.pid
    # The orphaned worker is reaped by init, not by this process: this assumes
    # a reaping init, as the dev VM (systemd) and CI's runners have.
    wait_for(lambda: not group_alive(pgid), 10, on_timeout="the aborted session left a process")


@pytest.mark.serial_timing
def test_two_sessions_with_different_basetemps_contend_for_one_host(tmp_path: Path) -> None:
    """An xdist session and a -n0 session with its own --basetemp hold one lock file."""
    (tmp_path / "test_hold_a.py").write_text(_HOLD)
    (tmp_path / "test_hold_b.py").write_text(_HOLD)
    env = {
        **os.environ,
        "BED_LOCK_ROOT": str(tmp_path / "locks"),
        "HOLD_LOG": str(tmp_path / "log"),
        "HOLD_BARRIER": str(tmp_path / "barrier"),
        "PYTHONPATH": str(CHECKOUT_ROOT),
    }
    base = [
        sys.executable,
        "-m",
        "pytest",
        "-q",
        "-p",
        "no:cacheprovider",
        "-o",
        "addopts=",
        "--rootdir",
        str(tmp_path),
    ]
    with ExitStack() as stack:
        runs = [
            _start_session(
                [*base, "-n", "1", str(tmp_path / "test_hold_a.py")],
                env=env,
                cwd=tmp_path,
                log=tmp_path / "a.out",
                stack=stack,
            ),
            _start_session(
                [
                    *base,
                    "-n0",
                    "--basetemp",
                    str(tmp_path / "custom"),
                    str(tmp_path / "test_hold_b.py"),
                ],
                env=env,
                cwd=tmp_path,
                log=tmp_path / "b.out",
                stack=stack,
            ),
        ]
        assert _wait_for_sessions(runs, 60) == [0, 0], _outputs(runs)
    events = [line.split() for line in (tmp_path / "log").read_text().splitlines()]
    assert [kind for kind, _ in events] == ["start", "end", "start", "end"], events


def test_the_root_is_made_on_first_use(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = tmp_path / "a" / "b"
    monkeypatch.setenv("BED_LOCK_ROOT", str(root))
    assert bed_lock_root() == root
    assert root.is_dir()


def test_the_default_root_names_no_session(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The default is one fixed path per user, whatever the session's temp dir."""
    expected = Path(f"/tmp/otto-bed-locks-{getpass.getuser()}")
    monkeypatch.delenv(BED_LOCK_ROOT_ENV, raising=False)
    assert bed_lock_root() == expected
    monkeypatch.setenv("TMPDIR", str(tmp_path))
    monkeypatch.setattr(tempfile, "tempdir", None)  # gettempdir() caches; make it re-read TMPDIR
    assert bed_lock_root() == expected


def test_a_unit_test_never_resolves_the_real_root(tmp_path_factory: pytest.TempPathFactory) -> None:
    """The unit tree's autouse fixture keeps every unit test's locks under its basetemp."""
    assert tmp_path_factory.getbasetemp().resolve() in bed_lock_root().resolve().parents


def test_a_subprocess_started_from_the_environment_inherits_the_unit_root(
    tmp_path_factory: pytest.TempPathFactory,
) -> None:
    """A pytest or python child built from ``os.environ`` locks under its unit test's basetemp."""
    child = subprocess.run(
        [
            sys.executable,
            "-c",
            "from tests._fixtures.bed_locks import bed_lock_root; print(bed_lock_root())",
        ],
        env={**os.environ, "PYTHONPATH": str(CHECKOUT_ROOT)},
        capture_output=True,
        text=True,
        timeout=60,
        check=True,
    )
    child_root = Path(child.stdout.strip())
    assert child_root == bed_lock_root()
    assert tmp_path_factory.getbasetemp().resolve() in child_root.resolve().parents


def test_a_default_lease_lives_under_the_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("BED_LOCK_ROOT", str(tmp_path))
    with docker_host_lease() as element:
        assert element == DOCKER_HOST
        fd = os.open(tmp_path / f"unix_pool.{DOCKER_HOST}", os.O_RDWR)
        try:
            with pytest.raises(BlockingIOError):
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        finally:
            os.close(fd)


# The calls that take a lock directory, and how many leading positional
# arguments each may pass before one is the directory.
_LOCK_CALLS = {
    "lease_unix_host": 1,
    "console_access": 0,
    "serialized_console": 0,
    "serialized_family": 0,
}


def _directory_passing_calls(source: str) -> list[int]:
    """Lines where a lock call outside a lock-forwarding function names its own directory.

    A function with a ``lock_dir`` parameter may forward it: that is the
    injection seam itself. Anyone else takes the default, the bed-lock root.
    """
    found: list[int] = []

    def visit(node: ast.AST, forwarding: bool) -> None:
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
            args = node.args
            forwarding = any(a.arg == "lock_dir" for a in [*args.args, *args.kwonlyargs])
        if isinstance(node, ast.Call) and not forwarding:
            name = getattr(node.func, "id", None) or getattr(node.func, "attr", None)
            if name in _LOCK_CALLS and (
                len(node.args) > _LOCK_CALLS[name]
                or any(k.arg == "lock_dir" for k in node.keywords)
            ):
                found.append(node.lineno)
        for child in ast.iter_child_nodes(node):
            visit(child, forwarding)

    try:
        tree = ast.parse(source)
    except SyntaxError:  # a deliberately broken fixture file: nothing imports it
        return found
    visit(tree, False)
    return found


def test_no_bed_lock_outside_the_unit_tree_names_its_own_directory() -> None:
    """Every lease and lock outside tests/unit takes the bed-lock root, never its own directory."""
    offenders = [
        f"{p.relative_to(CHECKOUT_ROOT)}:{line}"
        for p in (CHECKOUT_ROOT / "tests").rglob("*.py")
        if "tests/unit" not in p.as_posix()
        for line in _directory_passing_calls(p.read_text())
    ]
    assert not offenders, offenders


def test_the_lock_call_scanner_sees_the_shapes_it_judges() -> None:
    """Positive controls: the shapes the guard must refuse, and the ones it must allow."""
    refused = [
        "lease_unix_host(lock_dir=d)\n",
        "lease_unix_host(UNIX_POOL, lock_dir=tmp_path_factory.mktemp('x'))\n",
        "d = tmp_path_factory.getbasetemp().parent\nconsole_access(d, exclusive=True)\n",
        "serialized_console(tmp_path)\n",
        "_console_safety.serialized_family(d)\n",
    ]
    allowed = [
        "lease_unix_host(('test2',))\n",
        "docker_host_lease()\n",
        "console_access(exclusive=False)\n",
        "def serialized_console(lock_dir=None):\n    console_access(lock_dir, exclusive=True)\n",
    ]
    assert [bool(_directory_passing_calls(src)) for src in refused] == [True] * len(refused)
    assert [bool(_directory_passing_calls(src)) for src in allowed] == [False] * len(allowed)


def _refuse_polling(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make a lease that would poll fail at once, rather than wait for pytest-timeout.

    ``time.sleep`` between rounds is the lease loop's only use of ``time``, and
    it is reached only when every candidate it tried was busy.
    """
    monkeypatch.setattr(
        _host_pool,
        "time",
        SimpleNamespace(sleep=lambda _s: pytest.fail("the lease polled on a busy host")),
    )


def test_a_lease_of_a_host_this_process_holds_raises_at_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A nested lease of a held host names its holder instead of waiting on itself."""
    monkeypatch.setenv(BED_LOCK_ROOT_ENV, str(tmp_path))
    _refuse_polling(monkeypatch)
    with lease_unix_host([DOCKER_HOST]):
        refused = pytest.raises(
            SelfLeaseError, match=r"test3 by test_a_lease_of_a_host_this_process"
        )
        with refused, docker_host_lease():
            pass
    with docker_host_lease() as element:  # released on the way out: leasable again
        assert element == DOCKER_HOST


def test_a_partial_self_hold_does_not_raise(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A lease whose candidates include a host this process holds takes a free one.

    Only a lease with NOTHING but self-held candidates is refused; here the
    free candidate is leased in the first round, without polling.
    """
    _refuse_polling(monkeypatch)
    held = lease_unix_host(["test1"], lock_dir=tmp_path)
    with held, lease_unix_host(["test1", "test2"], lock_dir=tmp_path) as element:
        assert element == "test2"


def test_the_chaos_bed_never_draws_the_docker_host(monkeypatch: pytest.MonkeyPatch) -> None:
    """The chaos bed leases from the pool less the docker host, which its docker scenarios lease.

    With every host of its pool held by this process, the bed has nothing left
    to draw: it refuses at once, so it never reaches the docker host.
    """
    from tests.e2e.chaos._bed import CHAOS_POOL, leased_bed

    assert DOCKER_HOST not in CHAOS_POOL
    assert set(CHAOS_POOL) == set(UNIX_POOL) - {DOCKER_HOST}
    _refuse_polling(monkeypatch)
    with ExitStack() as held:
        for element in CHAOS_POOL:
            held.enter_context(lease_unix_host([element]))
        with pytest.raises(SelfLeaseError), leased_bed():
            pass
