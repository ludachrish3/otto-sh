"""sync_phase: real-signal subprocess matrix + in-process handler hygiene.

Signal delivery is proven against a REAL child process signalled by PID
(never pattern-matched kills), with mid-flight positive controls inside the
child so a vacuously-green assertion cannot hide a never-installed handler.

Placement (deliberate): the repo's convention sends real signal DELIVERY to
tier-2 subprocess coverage, and each of these tests spawns exactly such a
fresh-interpreter subprocess — they live in the unit tier because they are
fast, bed-free, and the primitive they pin is correctness-critical enough
that every `pytest tests/unit` run should exercise it. In-process tests that
install REAL handlers request ``real_sync_phase`` (root conftest), which
re-arms the worker's chained SIGINT faulthandler in teardown.
"""

import contextlib
import os
import signal
import subprocess
import sys
import threading
import time

import pytest

from otto.lifecycle import sync_phase

_CHILD = """
import os, signal, sys, time
from otto.lifecycle import register_force_exit_hook, sync_phase

mode = sys.argv[1]
# Only the wedged mode wants a fast deadline; everywhere else a live 1s
# deadline is a flake window (a slow box finishing graceful teardown in
# >1s would spuriously force). double=30 also guarantees only the SECOND
# signal can force in time.
deadline = 1.0 if mode == "wedged" else 30.0
# os.write: force hooks run on the watchdog thread while the MAIN thread may
# be wedged holding stdio locks (print there can deadlock/reentrant-IO).
register_force_exit_hook(lambda: os.write(1, b"FORCE-HOOK\\n"))
with sync_phase(deadline=deadline, what="probe") as guard:
    try:
        # Mid-flight positive control: the guard's handler is installed.
        assert signal.getsignal(signal.SIGINT) is not signal.default_int_handler
        print("PHASE-START", flush=True)
        time.sleep(30)
        print("PHASE-UNREACHED", flush=True)
    except KeyboardInterrupt:
        print("TEARDOWN-START", flush=True)
        if mode == "graceful":
            print("TEARDOWN-DONE", flush=True)
        else:  # wedged/double: teardown never finishes on its own
            time.sleep(30)
print("PHASE-EXITED", flush=True)
if guard.interrupted_signum is None:
    sys.exit(0)
sys.exit(128 + guard.interrupted_signum)
"""


@contextlib.contextmanager
def _spawned(tmp_path, mode):
    """Popen with guaranteed reaping + pipe close (unraisables are errors here)."""
    script = tmp_path / "probe.py"
    script.write_text(_CHILD)
    proc = subprocess.Popen(
        [sys.executable, str(script), mode],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    try:
        yield proc
    finally:
        if proc.poll() is None:
            proc.kill()
        proc.wait(timeout=10)
        proc.stdout.close()


def _wait_line(proc, marker, timeout=20.0):
    """Read stdout lines until *marker* appears; fail loudly on EOF/timeout."""
    deadline = time.monotonic() + timeout
    lines = []
    # Not a wait_for poll: the pacing is the blocking readline() on a
    # line-oriented child, so the clock is an inter-line budget (it cannot
    # fire while the child is silent), and _finish() reading the same
    # buffered stream afterwards forbids a select()-based bounded rewrite.
    # ast-grep-ignore: no-handrolled-deadline-poll
    while time.monotonic() < deadline:
        line = proc.stdout.readline()
        if not line:
            pytest.fail(f"child EOF before {marker!r}; output so far: {lines}")
        lines.append(line.strip())
        if marker in line:
            return lines
    pytest.fail(f"timed out waiting for {marker!r}; output so far: {lines}")


def _finish(proc, timeout=20.0):
    out = proc.stdout.read()
    rc = proc.wait(timeout=timeout)
    return rc, out


def test_first_sigint_graceful_teardown_exits_130(tmp_path):
    with _spawned(tmp_path, "graceful") as proc:
        _wait_line(proc, "PHASE-START")
        os.kill(proc.pid, signal.SIGINT)
        rc, out = _finish(proc)
    assert rc == 130
    assert "TEARDOWN-DONE" in out
    assert "PHASE-EXITED" in out
    assert "FORCE-HOOK" not in out, "graceful path must not force-exit"
    assert "PHASE-UNREACHED" not in out


def test_sigterm_maps_to_graceful_teardown_exits_143(tmp_path):
    with _spawned(tmp_path, "graceful") as proc:
        _wait_line(proc, "PHASE-START")
        os.kill(proc.pid, signal.SIGTERM)
        rc, out = _finish(proc)
    assert rc == 143
    assert "TEARDOWN-DONE" in out
    assert "FORCE-HOOK" not in out


def test_wedged_teardown_deadline_forces_exit(tmp_path):
    with _spawned(tmp_path, "wedged") as proc:
        _wait_line(proc, "PHASE-START")
        os.kill(proc.pid, signal.SIGINT)
        _wait_line(proc, "TEARDOWN-START")
        rc, out = _finish(proc)  # deadline=1.0 fires long before the 30s wedge
    assert rc == 130
    assert "FORCE-HOOK" in out, "force hooks must run on deadline expiry"
    assert "TEARDOWN-DONE" not in out
    assert "PHASE-EXITED" not in out


def test_second_signal_forces_immediately(tmp_path):
    with _spawned(tmp_path, "double") as proc:  # deadline=30s: only a 2nd signal forces in time
        _wait_line(proc, "PHASE-START")
        os.kill(proc.pid, signal.SIGINT)
        _wait_line(proc, "TEARDOWN-START")
        os.kill(proc.pid, signal.SIGINT)
        rc, out = _finish(proc)
    assert rc == 130
    assert "FORCE-HOOK" in out
    assert "PHASE-EXITED" not in out


def test_mixed_signal_pair_forces_regardless_of_order(tmp_path):
    """SIGTERM + SIGINT back-to-back must force, whatever the interleaving.

    Two DIFFERENT signals do not coalesce at the OS level and may be handled
    at the same eval checkpoint, so the second handler's force byte can reach
    the self-pipe BEFORE the first handler's arm byte. This pins the
    watchdog's F-forces-regardless-of-order protocol: a sequential
    A-then-F reader would mistake the early force byte for a foreign wake,
    retire, and leave the wedged teardown hanging unrescuably (the child
    would then die at _finish's timeout, not at the force). Delivery order
    is not deterministic, so the exit code may reflect either signal —
    but with deadline=30 and a 30s wedge, only the force path can produce
    FORCE-HOOK and a 128+signum exit in time.
    """
    with _spawned(tmp_path, "double") as proc:
        _wait_line(proc, "PHASE-START")
        os.kill(proc.pid, signal.SIGTERM)
        os.kill(proc.pid, signal.SIGINT)  # immediately — no marker wait between them
        rc, out = _finish(proc)
    assert rc in (130, 143), f"expected 128+signum of either signal, got {rc}"
    assert "FORCE-HOOK" in out
    assert "PHASE-EXITED" not in out


def test_handlers_installed_then_restored(real_sync_phase):
    calls = []

    def sentinel(signum, frame):
        calls.append(signum)

    prior_int = signal.signal(signal.SIGINT, sentinel)
    prior_term = signal.signal(signal.SIGTERM, sentinel)
    try:
        with sync_phase(what="t") as guard:
            # Positive control: the sentinel is displaced while inside.
            assert signal.getsignal(signal.SIGINT) is not sentinel
            assert signal.getsignal(signal.SIGTERM) is not sentinel
            assert guard.interrupted_signum is None
        assert signal.getsignal(signal.SIGINT) is sentinel
        assert signal.getsignal(signal.SIGTERM) is sentinel
    finally:
        signal.signal(signal.SIGINT, prior_int)
        signal.signal(signal.SIGTERM, prior_term)


def test_watchdog_spawned_then_retired(real_sync_phase):
    """The force path's watchdog lives exactly as long as the guard."""
    with sync_phase(what="t") as guard:
        watchdog = guard._watchdog
        assert watchdog is not None
        assert watchdog.is_alive()  # positive control: it was really spawned
    watchdog.join(timeout=5.0)
    assert not watchdog.is_alive(), "watchdog must retire on clean exit, not linger armed"


def test_install_handlers_false_is_inert(real_sync_phase):
    """The library/test seam: no handlers displaced, no watchdog spawned."""
    sentinel_calls = []

    def sentinel(signum, frame):
        sentinel_calls.append(signum)

    # real_sync_phase is requested for its TEARDOWN (this test's own sentinel
    # install/restore below disarms the worker's chained SIGINT faulthandler).
    prior = signal.signal(signal.SIGINT, sentinel)
    try:
        with sync_phase(what="t", install_handlers=False) as guard:
            # Differential control: the sentinel is NOT displaced.
            assert signal.getsignal(signal.SIGINT) is sentinel
            assert guard._watchdog is None
        assert guard.interrupted_signum is None
    finally:
        signal.signal(signal.SIGINT, prior)


def test_rejects_non_main_thread():
    result = {}

    def enter():
        try:
            with sync_phase(what="t"):
                pass
        except RuntimeError as e:
            result["error"] = str(e)

    t = threading.Thread(target=enter)
    t.start()
    t.join()
    assert "main thread" in result.get("error", "")


def test_non_main_thread_inert_mode_allowed():
    """install_handlers=False composes with worker threads (library callers)."""
    result = {}

    def enter():
        try:
            with sync_phase(what="t", install_handlers=False) as guard:
                result["signum"] = guard.interrupted_signum
        except RuntimeError as e:  # pragma: no cover - failure surface
            result["error"] = str(e)

    t = threading.Thread(target=enter)
    t.start()
    t.join()
    assert "error" not in result
    assert result == {"signum": None}
