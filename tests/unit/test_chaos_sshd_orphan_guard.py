"""The tier-2 loopback sshd must not outlive a SIGKILLed test worker.

``LoopbackSshd.stop()`` runs in a ``finally`` and covers every exit the worker
is alive for.  It cannot cover the one where the worker is SIGKILLed — a gate
run stopped by hand, an OOM kill, ``kill -9`` on a wedged suite — because no
Python finalizer runs at all.  The fixture is session-scoped, so its teardown
is the last thing to run and therefore the most exposed.

That is not hypothetical: an ``sshd`` from ``popen-gw3/chaos0`` was found alive
on the dev VM two days after the run that spawned it, reparented to init, still
holding its port, with the tmp directory containing its config already deleted
by pytest's numbered-dir rotation.  It went unnoticed because the orphan check
run afterwards grepped for ``nox``/``pytest`` — which cannot match a process
whose argv is ``sshd: /usr/sbin/sshd …``.

So the kernel holds the other end, via ``PR_SET_PDEATHSIG``.  These tests prove
it by killing a real parent, both ways round: with the guard the child dies,
without it the child survives.  Asserting that ``preexec_fn`` is wired up would
prove nothing about whether the signal is actually delivered.
"""

import contextlib
import os
import signal
import subprocess
import sys
import textwrap
from collections.abc import Iterator
from pathlib import Path

from otto.utils import WaitTimeoutError, wait_for
from tests._fixtures.paths import PROJECT_ROOT
from tests.integration.chaos._sshd import _die_with_parent

# Generous on purpose: a runaway guard, not a discriminator. The kernel
# delivers PDEATHSIG at parent exit, so the real figure is microseconds; this
# only has to be long enough that sibling load cannot counterfeit a failure.
_REAP_TIMEOUT = 5.0
_POLL = 0.02

# How long the negative control watches an UNGUARDED child before accepting
# that it outlived its parent. Widening this only makes the control STRICTER —
# more time for an unexpected death to surface — so it is a floor rather than a
# discriminator, kept short because every run pays it in full.
_CONTROL_WINDOW = 0.5

_SPAWNER = """
    import subprocess, sys
    sys.path.insert(0, {root!r})
    {preexec_import}
    child = subprocess.Popen(["sleep", "300"]{preexec_arg})
    print(child.pid, flush=True)
    import time; time.sleep(300)
"""


def _alive(pid: int) -> bool:
    return Path(f"/proc/{pid}").exists()


def _wait_gone(pid: int, timeout: float, *, on_timeout: str) -> None:
    """Block until *pid* leaves ``/proc``, or raise ``WaitTimeoutError`` at *timeout*.

    Both directions of this suite are expressed through the one poll: the
    guarded test lets the expiry fail it, the negative control catches the
    expiry and requires it.
    """
    wait_for(lambda: not _alive(pid), timeout=timeout, interval=_POLL, on_timeout=on_timeout)


@contextlib.contextmanager
def _parent_with_child(*, guarded: bool) -> "Iterator[tuple[subprocess.Popen[str], int]]":
    """A parent process holding a ``sleep 300`` child, guarded or not.

    A context manager rather than a plain helper because both processes AND
    the stdout pipe have to be cleaned up on every path. The first version
    leaked the pipe and the suite's own unraisable plugin failed it — fitting,
    for a test about processes outliving their owner.
    """
    src = textwrap.dedent(
        _SPAWNER.format(
            # The spawned parent is a bare interpreter, so it needs the repo on
            # sys.path to import the helper under test. PROJECT_ROOT rather than
            # the cwd (pytest can be invoked from anywhere) and rather than
            # `parents[N]` off this file (G9: depth arithmetic breaks silently
            # when a file moves).
            root=str(PROJECT_ROOT),
            preexec_import=(
                "from tests.integration.chaos._sshd import _die_with_parent" if guarded else ""
            ),
            preexec_arg=", preexec_fn=_die_with_parent" if guarded else "",
        )
    )
    child_pid = -1
    with subprocess.Popen([sys.executable, "-c", src], stdout=subprocess.PIPE, text=True) as parent:
        try:
            assert parent.stdout is not None
            child_pid = int(parent.stdout.readline().strip())
            yield parent, child_pid
        finally:
            if parent.poll() is None:
                parent.kill()
            # Never leave behind the very thing this test is about.
            if child_pid > 0 and _alive(child_pid):
                with contextlib.suppress(ProcessLookupError):
                    os.kill(child_pid, signal.SIGKILL)


def test_the_daemon_dies_when_its_parent_is_sigkilled():
    """The guard, executed: SIGKILL the parent, the child must go with it."""
    with _parent_with_child(guarded=True) as (parent, child_pid):
        assert _alive(child_pid), "child never started"
        parent.kill()
        parent.wait(timeout=_REAP_TIMEOUT)
        _wait_gone(
            child_pid,
            _REAP_TIMEOUT,
            on_timeout=(
                f"child {child_pid} outlived its SIGKILLed parent — PR_SET_PDEATHSIG "
                "is not being applied, so a stopped gate run will strand the loopback "
                "sshd again"
            ),
        )


def test_without_the_guard_the_daemon_survives():
    """Negative control: the mutation this suite must be able to see.

    If an unguarded child also died with its parent, the test above would pass
    for reasons unrelated to ``PR_SET_PDEATHSIG`` and would keep passing after
    the ``preexec_fn`` was dropped.
    """
    with _parent_with_child(guarded=False) as (parent, child_pid):
        assert _alive(child_pid), "child never started"
        parent.kill()
        parent.wait(timeout=_REAP_TIMEOUT)
        # Expiry IS the assertion: the child must still be there at the end of
        # the window. Caught rather than left to `pytest.raises` so the failure
        # text says what a non-expiry MEANS, not just that nothing was raised.
        survived = False
        try:
            _wait_gone(child_pid, _CONTROL_WINDOW, on_timeout="still alive, as required")
        except WaitTimeoutError:
            survived = True
        assert survived, (
            "an UNGUARDED child died with its parent, so this suite cannot tell "
            "whether the guard does anything"
        )


def test_the_helper_is_wired_into_the_daemon_spawn():
    """Behaviour above proves the mechanism; this proves sshd actually uses it."""
    import ast
    import inspect
    from pathlib import Path

    from tests.integration.chaos import _sshd as sshd_module

    tree = ast.parse(Path(inspect.getfile(sshd_module)).read_text())
    popens = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "Popen"
    ]
    assert popens, "no subprocess.Popen in _sshd.py — this guard has rotted"
    for call in popens:
        guarded = any(
            kw.arg == "preexec_fn"
            and isinstance(kw.value, ast.Name)
            and kw.value.id == _die_with_parent.__name__
            for kw in call.keywords
        )
        assert guarded, (
            f"_sshd.py:{call.lineno} spawns a daemon without preexec_fn="
            f"{_die_with_parent.__name__}; a SIGKILLed worker would strand it"
        )
