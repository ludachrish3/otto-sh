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
import dataclasses
import os
import select
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


class _StdoutReader:
    """Bounded reader over the child's stdout pipe, via ``select()`` on the raw fd.

    The pacing owner is ``select()``: each pass blocks until the child writes
    or the remaining budget expires, so a child that goes SILENT without
    exiting is a named failure at the caller's budget — the old blocking
    ``readline()`` reader could not be interrupted, so its budget was
    inter-line only and a silent child wedged the test until pytest-timeout's
    180s kill. Both :func:`_wait_line` and :func:`_finish` consume from this
    one buffer; nothing else may touch ``proc.stdout``, or bytes would be
    stranded in the ``BufferedReader``. (The remaining-budget arithmetic is
    the two-statement fused-read form the ``no-handrolled-deadline-poll``
    gate documents as a non-arm — the same class as ``_pty_driver``'s expect
    loops.)
    """

    def __init__(self, fd: int) -> None:
        self._fd = fd
        self._buf = b""

    @property
    def buffered(self) -> str:
        """Whatever is sitting unconsumed in the buffer, decoded (diagnostics)."""
        return self._buf.decode(errors="replace")

    def next_line(self, deadline: float) -> "str | None":
        """Next decoded line (newline stripped), ``None`` at EOF; ``TimeoutError`` at *deadline*."""
        while b"\n" not in self._buf:
            if not self._pump(deadline):
                if self._buf:  # EOF with an unterminated tail — surface it as a line
                    tail, self._buf = self._buf, b""
                    return tail.decode(errors="replace")
                return None
        raw, _, self._buf = self._buf.partition(b"\n")
        return raw.decode(errors="replace")

    def read_to_eof(self, deadline: float) -> str:
        """Everything up to EOF, decoded; ``TimeoutError`` if the child never closes."""
        while self._pump(deadline):
            pass
        out, self._buf = self._buf, b""
        return out.decode(errors="replace")

    def _pump(self, deadline: float) -> bool:
        """Block for one more chunk: True when read, False at EOF."""
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError
        ready, _, _ = select.select([self._fd], [], [], remaining)
        if not ready:
            raise TimeoutError
        chunk = os.read(self._fd, 65536)
        if not chunk:
            return False
        self._buf += chunk
        return True


@dataclasses.dataclass
class _Child:
    proc: subprocess.Popen
    reader: _StdoutReader

    @property
    def pid(self) -> int:
        return self.proc.pid


@contextlib.contextmanager
def _spawned(tmp_path, mode):
    """Popen with guaranteed reaping + pipe close (unraisables are errors here)."""
    script = tmp_path / "probe.py"
    script.write_text(_CHILD)
    proc = subprocess.Popen(
        [sys.executable, str(script), mode],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )
    try:
        yield _Child(proc=proc, reader=_StdoutReader(proc.stdout.fileno()))
    finally:
        if proc.poll() is None:
            proc.kill()
        proc.wait(timeout=10)
        proc.stdout.close()


def _wait_line(child, marker, timeout=60.0):
    """Read stdout lines until *marker* appears; fail loudly on EOF/timeout.

    *timeout* is a TOTAL budget for reaching the marker (it used to be an
    inter-line budget that could never fire while the child was silent). The
    default MUST exceed the child's own worst-case recovery — the 30s
    graceful-teardown deadline its non-wedged modes arm — plus a
    heavy-load stall margin: a 20s first cut failed one fully-loaded gate
    run when a child stalled past it with an EMPTY buffer (not even the
    handler's stderr notice — the signals had not been handled yet), which
    the pre-wave unbounded reader absorbed invisibly. 60s keeps the named
    failure while staying 3x tighter than the old 180s pytest-timeout
    wedge; the buffered-output diagnostic classifies any recurrence.
    """
    deadline = time.monotonic() + timeout
    lines = []
    while True:
        try:
            line = child.reader.next_line(deadline)
        except TimeoutError:
            pytest.fail(f"timed out waiting for {marker!r}; output so far: {lines}")
        if line is None:
            pytest.fail(f"child EOF before {marker!r}; output so far: {lines}")
        lines.append(line.strip())
        if marker in line:
            return lines


def _finish(child, timeout=60.0):
    # Same budget rule as _wait_line: must outlast the child's 30s deadline
    # net, or the harness fails while the product is still on its documented
    # path to a forced exit.
    try:
        out = child.reader.read_to_eof(time.monotonic() + timeout)
    except TimeoutError:
        pytest.fail(
            f"child still open (no stdout EOF) after {timeout}s — silent wedge; "
            f"buffered output: {child.reader.buffered!r}"
        )
    rc = child.proc.wait(timeout=timeout)
    return rc, out


def test_first_sigint_graceful_teardown_exits_130(tmp_path):
    with _spawned(tmp_path, "graceful") as child:
        _wait_line(child, "PHASE-START")
        os.kill(child.pid, signal.SIGINT)
        rc, out = _finish(child)
    assert rc == 130
    assert "TEARDOWN-DONE" in out
    assert "PHASE-EXITED" in out
    assert "FORCE-HOOK" not in out, "graceful path must not force-exit"
    assert "PHASE-UNREACHED" not in out


def test_sigterm_maps_to_graceful_teardown_exits_143(tmp_path):
    with _spawned(tmp_path, "graceful") as child:
        _wait_line(child, "PHASE-START")
        os.kill(child.pid, signal.SIGTERM)
        rc, out = _finish(child)
    assert rc == 143
    assert "TEARDOWN-DONE" in out
    assert "FORCE-HOOK" not in out


def test_wedged_teardown_deadline_forces_exit(tmp_path):
    with _spawned(tmp_path, "wedged") as child:
        _wait_line(child, "PHASE-START")
        os.kill(child.pid, signal.SIGINT)
        _wait_line(child, "TEARDOWN-START")
        rc, out = _finish(child)  # deadline=1.0 fires long before the 30s wedge
    assert rc == 130
    assert "FORCE-HOOK" in out, "force hooks must run on deadline expiry"
    assert "TEARDOWN-DONE" not in out
    assert "PHASE-EXITED" not in out


@pytest.mark.serial_timing
def test_second_signal_forces_immediately(tmp_path):
    with _spawned(tmp_path, "double") as child:  # deadline=30s: only a 2nd signal forces in time
        _wait_line(child, "PHASE-START")
        os.kill(child.pid, signal.SIGINT)
        _wait_line(child, "TEARDOWN-START")
        t_second = time.monotonic()
        os.kill(child.pid, signal.SIGINT)
        rc, out = _finish(child)
    elapsed = time.monotonic() - t_second
    assert rc == 130
    assert "FORCE-HOOK" in out
    assert "PHASE-EXITED" not in out
    # The discriminator behind "immediately": FORCE-HOOK is printed on BOTH
    # the second-signal force path and the 30s deadline-expiry path, and
    # _finish's 60s budget (deliberately > the deadline, per its comment)
    # would absorb either. Only elapsed time separates them: the force path
    # is handler-speed (~ms), the deadline path cannot finish before 30s.
    # 20s leaves load margin on this VM while sitting well under 30s. This
    # ceiling is deliberately BELOW the product's recovery deadline — the
    # W14 rule (a harness bound must outlast it) applies to budgets that
    # must TOLERATE the documented path; this assert exists to REJECT it.
    assert elapsed < 20.0, (
        f"forced exit took {elapsed:.1f}s — deadline path, not the second-signal force"
    )


@pytest.mark.serial_timing
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
    with _spawned(tmp_path, "double") as child:
        _wait_line(child, "PHASE-START")
        t_kill = time.monotonic()
        os.kill(child.pid, signal.SIGTERM)
        os.kill(child.pid, signal.SIGINT)  # immediately — no marker wait between them
        rc, out = _finish(child)
    elapsed = time.monotonic() - t_kill
    assert rc in (130, 143), f"expected 128+signum of either signal, got {rc}"
    assert "FORCE-HOOK" in out
    assert "PHASE-EXITED" not in out
    # W14 residual, closed: the docstring's "only the force path can produce
    # FORCE-HOOK ... in time" was argument, not measurement — _finish's 60s
    # budget would have absorbed a 30s deadline-expiry run indistinguishably.
    # Same 20s rationale as test_second_signal_forces_immediately.
    assert elapsed < 20.0, f"exit took {elapsed:.1f}s — deadline path, not the mixed-signal force"


def test_silent_child_is_a_named_failure_within_budget(tmp_path):
    """A child that goes quiet WITHOUT exiting fails by name at the budget.

    This pins Wave 14's bounded reader: the old blocking ``readline()`` could
    not be interrupted, so waiting for a marker a silent child never prints
    wedged the test until pytest-timeout's 180s kill — no marker name, no
    captured output, JUnit attributing a timeout instead of a failure. The
    graceful-mode child prints PHASE-START and then sleeps silently, which is
    exactly that shape.
    """
    with _spawned(tmp_path, "graceful") as child:
        _wait_line(child, "PHASE-START")
        started = time.monotonic()
        with pytest.raises(pytest.fail.Exception, match="timed out waiting"):
            _wait_line(child, "PHASE-UNREACHED", timeout=1.0)
        assert time.monotonic() - started < 10.0, (
            "the bounded reader took far longer than its budget to give up"
        )


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
