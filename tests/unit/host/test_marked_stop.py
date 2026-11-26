"""The marked stop: every process started with a token in its environment is killed.

:func:`otto.host.process_stop.reap_marked_script` is how otto stops a remote
shell it cannot signal as a group. What is pinned here is the property the
callers rely on: nothing the marked shell had left to run is run. Killing a
command before the shell that waits for it would let the shell go on to its
next statement, so the sweep freezes every marked process before it kills
any.

Real processes on this machine stand in for the remote ones.
"""

import contextlib
import os
import secrets
import shutil
import signal
import subprocess
from collections.abc import Iterator
from pathlib import Path

import pytest

from otto.host.process_stop import (
    marked_shell_command,
    reap_marked_command,
    reap_marked_script,
)
from otto.utils import wait_for

VARIABLE = "OTTO_TEST_MARK"
ROUNDS = 5


def _marked(token: str) -> list[int]:
    """Pids whose start environment carries *token*."""
    needle = f"{VARIABLE}={token}".encode()
    found = []
    for entry in Path("/proc").iterdir():
        if entry.name.isdigit():
            try:
                if needle in (entry / "environ").read_bytes():
                    found.append(int(entry.name))
            except OSError:
                continue
    return found


def _shell(name: str) -> list[str]:
    """The argv that starts shell *name*; skips the test where BusyBox is not installed."""
    if name != "busybox":
        return [name]
    if shutil.which("busybox") is None:
        pytest.skip("no busybox here")
    return ["busybox", "sh"]


@contextlib.contextmanager
def _waiting_on_a_command(argv: list[str], line: str, token: str) -> Iterator[subprocess.Popen]:
    """A marked shell running *line*, yielded once its first command is running."""
    proc = subprocess.Popen(
        [*argv, "-c", line], env={**os.environ, VARIABLE: token}, start_new_session=True
    )
    try:
        wait_for(lambda: len(_marked(token)) >= 2, 10.0, interval=0.01, on_timeout="no command")
        yield proc
    finally:
        with contextlib.suppress(ProcessLookupError):
            os.killpg(proc.pid, signal.SIGKILL)
        proc.wait()


@pytest.mark.parametrize("shell", ["sh", "bash", "busybox"])
def test_nothing_the_marked_shell_had_left_to_run_is_run(tmp_path, shell):
    """Round after round: the command is killed and the statement after it never runs."""
    argv = _shell(shell)
    for round_ in range(ROUNDS):
        token = secrets.token_hex(16)
        after = tmp_path / f"after{round_}"
        line = f"/bin/sleep 60; touch {after}; /bin/sleep 60"
        with _waiting_on_a_command(argv, line, token) as proc:
            subprocess.run([*argv, "-c", reap_marked_command(VARIABLE, token)], check=True)
            proc.wait(timeout=10)
            assert _marked(token) == []
            assert not after.exists(), f"round {round_}: the shell ran on after its command died"


@pytest.mark.parametrize("shell", ["sh", "bash", "busybox"])
def test_every_marked_process_is_frozen_before_any_is_killed(tmp_path, shell):
    """The order that keeps a waiting shell from running on: all ``STOP`` first, then ``KILL``.

    Those two are the only signals sent.

    The sweep visits ``/proc`` in name order, so it can reach a command
    before the shell that waits for it. ``kill`` is wrapped to record what
    the sweep sends, in the order it sends it.
    """
    argv = _shell(shell)
    token, log = secrets.token_hex(16), tmp_path / "sent"
    with _waiting_on_a_command(["sh"], "/bin/sleep 60; /bin/sleep 60", token) as proc:
        marked = set(_marked(token))
        recorded = f'kill() {{ echo "$1 $2" >> {log}; command kill "$@"; }}; '
        subprocess.run(
            [*argv, "-c", recorded + reap_marked_script(VARIABLE), "sh", token], check=True
        )
        proc.wait(timeout=10)
    sent = [line.split() for line in log.read_text().splitlines()]
    first_kill = next(i for i, (sig, _) in enumerate(sent) if sig == "-KILL")
    frozen = {int(pid) for sig, pid in sent[:first_kill] if sig == "-STOP"}
    assert marked <= frozen, sent
    # No TERM or HUP first: a marked process gets no chance to clean up.
    assert {sig for sig, _ in sent} == {"-STOP", "-KILL"}, sent


def _counting_grep(directory: Path, body: str) -> tuple[dict[str, str], Path]:
    """An environment whose ``grep`` is *body* after it has recorded that it was started.

    Returns the environment and the file holding one line for each start.
    """
    count = directory / "greps"
    stand_in = directory / "bin" / "grep"
    stand_in.parent.mkdir()
    stand_in.write_text(f"#!/bin/sh\necho started >> {count}\n{body}\n")
    stand_in.chmod(0o755)
    return {**os.environ, "PATH": f"{stand_in.parent}{os.pathsep}{os.environ['PATH']}"}, count


@pytest.mark.parametrize("shell", ["sh", "bash"])
def test_a_sweep_starts_one_grep_however_many_processes_are_running(tmp_path, shell):
    """The stop's cost in new processes does not grow with the host's process count.

    A ``grep`` for each process is one new process each, which on a slow host
    is longer than the stop has. Twenty more processes are started here, none
    marked: a stop that looked at each with its own ``grep`` would start
    sixty or more, and this one starts one for each sweep (two ``STOP``
    sweeps, then at most ten ``KILL`` sweeps).
    """
    real = shutil.which("grep")
    assert real is not None
    env, count = _counting_grep(tmp_path, f'exec {real} "$@"')
    token = secrets.token_hex(16)
    others = [subprocess.Popen(["/bin/sleep", "60"]) for _ in range(20)]
    try:
        with _waiting_on_a_command(["sh"], "/bin/sleep 60; /bin/sleep 60", token) as proc:
            subprocess.run([shell, "-c", reap_marked_command(VARIABLE, token)], env=env, check=True)
            proc.wait(timeout=10)
            assert _marked(token) == []
    finally:
        for other in others:
            other.kill()
            other.wait()
    sweeps = len(count.read_text().splitlines())
    # Two STOP sweeps, one KILL sweep that kills and one that finds nothing, at least.
    assert 4 <= sweeps <= 12, sweeps


@pytest.mark.parametrize("shell", ["sh", "bash", "busybox"])
def test_a_sweep_that_can_list_nothing_kills_nothing_and_ends(tmp_path, shell):
    """A ``grep`` that fails and lists nothing is "nothing matched": no signal, and the stop ends.

    That is what a host with no ``/proc``, no ``grep`` or too many processes
    for one command's arguments gives. The marked shell runs on. The sweep is
    run with ``kill`` wrapped, so a signal sent would be recorded.
    """
    argv = _shell(shell)
    env, count = _counting_grep(tmp_path, "exit 2")
    token, log = secrets.token_hex(16), tmp_path / "sent"
    with _waiting_on_a_command(["sh"], "/bin/sleep 60; /bin/sleep 60", token) as proc:
        marked = sorted(_marked(token))
        recorded = f'kill() {{ echo "$1 $2" >> {log}; command kill "$@"; }}; '
        # `command grep` is not used by the script, so a shell whose own grep
        # would be preferred to the one on PATH is given the stand-in by name.
        named = f'grep() {{ {tmp_path / "bin" / "grep"} "$@"; }}; '
        subprocess.run(
            [*argv, "-c", named + recorded + reap_marked_script(VARIABLE), "sh", token],
            env=env,
            check=True,
        )
        assert sorted(_marked(token)) == marked
        assert proc.poll() is None
    assert not log.exists()
    # Two STOP sweeps and the one KILL sweep that finds nothing.
    assert len(count.read_text().splitlines()) == 3


def test_the_marked_shell_command_starts_a_login_shell_carrying_the_token(tmp_path):
    """The shell the command starts has the token in its own start environment."""
    env = {**os.environ, "SHELL": shutil.which("bash") or "bash", "HOME": str(tmp_path)}
    asks = (
        "tr '\\0' '\\n' < /proc/$$/environ | grep OTTO_TEST_MARK; shopt -q login_shell; echo $?\n"
    )
    done = subprocess.run(
        ["sh", "-c", marked_shell_command(VARIABLE, "feed")],
        input=asks,
        env=env,
        capture_output=True,
        text=True,
        timeout=20,
        check=True,
    )
    assert done.stdout.splitlines()[-2:] == [f"{VARIABLE}=feed", "0"]


@pytest.mark.parametrize(
    ("line", "written"),
    [("echo err >&2; echo out", "err\nout\n"), ("echo out; echo err >&2", "out\nerr\n")],
)
def test_the_marked_shell_writes_stderr_into_its_stdout_pipe(tmp_path, line, written):
    """Started on two pipes, as an ssh server starts it, the shell writes both to the first.

    The server forwards two pipes in the order it finds them, not the order
    they were written; one pipe keeps the written order.
    """
    env = {**os.environ, "SHELL": shutil.which("bash") or "bash", "HOME": str(tmp_path)}
    done = subprocess.run(
        ["sh", "-c", marked_shell_command(VARIABLE, "feed")],
        input=line + "\n",
        env=env,
        capture_output=True,
        text=True,
        timeout=20,
        check=True,
    )
    assert (done.stdout, done.stderr) == (written, "")
