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
import pathlib
import select
import signal
import subprocess
import sys
import threading
import time

import pytest

from otto.lifecycle import sync_phase
from otto.utils import wait_for

_CHILD = """
import asyncio, os, signal, sys, time
from otto.lifecycle import register_force_exit_hook, sync_phase

mode = sys.argv[1]
# Only the wedged mode wants a fast deadline; everywhere else a live 1s
# deadline is a flake window (a slow box finishing graceful teardown in
# >1s would spuriously force). double=30 also guarantees only the SECOND
# signal can force in time.
deadline = 1.0 if mode == "wedged" else (5.0 if mode == "stranded" else 30.0)
# os.write: force hooks run on the watchdog thread while the MAIN thread may
# be wedged holding stdio locks (print there can deadlock/reentrant-IO).
register_force_exit_hook(lambda: os.write(1, b"FORCE-HOOK\\n"))
with sync_phase(deadline=deadline, what="probe") as guard:
    try:
        # Mid-flight positive control: the guard's handler is installed.
        assert signal.getsignal(signal.SIGINT) is not signal.default_int_handler
        if mode == "stolen":
            # An async command running INSIDE the phase — the everyday shape,
            # since `otto test` runs a whole pytest session inside one and any
            # suite reaching run_command lands here. asyncio's
            # add_signal_handler claims the interpreter's wakeup fd, and
            # closing the loop hands back -1 rather than the previous owner,
            # so the guard's wakeup channel is gone from this point on and
            # never comes back. SIGWINCH keeps the experiment to the fd: it is
            # not one of the signals the guard installs, so this cannot be
            # mistaken for a handler-replacement effect.
            async def _borrow():
                asyncio.get_running_loop().add_signal_handler(signal.SIGWINCH, lambda: None)

            asyncio.run(_borrow())
            assert signal.set_wakeup_fd(-1) == -1, "premise broken: fd was NOT dropped"
            print("FD-STOLEN", flush=True)
        print("PHASE-START", flush=True)
        time.sleep(30)
        print("PHASE-UNREACHED", flush=True)
    except KeyboardInterrupt:
        if mode == "stranded":
            # Block SIGTERM IN THIS THREAD ONLY, then say so. Python-level
            # handlers run only on the main thread, so the second signal the
            # parent is about to send can never reach this guard's handler —
            # while the watchdog thread, created before this mask existed,
            # still has it unblocked, so the interpreter's wakeup byte is
            # still written. That is precisely "delivered, but the Python
            # handler did not run", made deterministic instead of racy.
            signal.pthread_sigmask(signal.SIG_BLOCK, {signal.SIGTERM})
        print("TEARDOWN-START", flush=True)
        if mode == "graceful":
            print("TEARDOWN-DONE", flush=True)
        else:  # wedged/double/stranded: teardown never finishes on its own
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


def _wait_until_blocked(pid, timeout=30.0):
    """Block until *pid*'s main thread is sleeping in a syscall.

    The stranded-signal property only holds while the main thread is inside a
    blocking call, because that is what denies a tripped-but-undispatched
    signal the eval checkpoint it needs. Signalling as soon as the child
    PRINTS is a race against the handful of bytecodes between the print and
    the sleep — lose it and the second signal dispatches normally, which is
    how a first cut of this test passed against the very code it exists to
    fail. `/proc/<pid>/stat` field 3 is the authoritative answer.
    """

    def _sleeping():
        stat = pathlib.Path(f"/proc/{pid}/stat").read_text()
        # rindex: comm sits in parens and may itself contain spaces.
        return stat[stat.rindex(")") + 2] == "S"

    wait_for(
        _sleeping,
        timeout,
        interval=0.01,
        on_timeout=lambda: f"child {pid} never reached a blocking call within {timeout}s",
    )


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
    # Transcript rides along for the same reason as its stranded-signal kin:
    # the two paths are told apart by what the child printed, never by how
    # slow it was, so a bare elapsed figure names the symptom and withholds
    # the evidence.
    assert elapsed < 20.0, (
        f"forced exit took {elapsed:.1f}s — deadline path, not the second-signal "
        f"force; child said:\n{out[-4000:]}"
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
    # The child's whole transcript rides along: this bound separates two
    # code paths, not two speeds, so "it was slow" is never the answer and
    # the transcript (how many interrupt notices the handler wrote, whether
    # teardown ever started) is what tells the two apart.
    assert elapsed < 20.0, (
        f"exit took {elapsed:.1f}s — deadline path, not the mixed-signal force; "
        f"child said:\n{out[-4000:]}"
    )


@pytest.mark.serial_timing
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


@pytest.mark.serial_timing
def test_second_signal_forces_even_when_its_handler_never_runs(tmp_path):
    """The force must not depend on a Python handler that may never run.

    Python-level signal handlers run only on the main thread, and only when
    the interpreter reaches an eval checkpoint with its pending-signal flag
    set. Neither is guaranteed: a signal delivered alongside one whose
    handler RAISES can be left tripped but undispatched (pending signals are
    dispatched in signal-NUMBER order, so SIGINT precedes SIGTERM, and the
    raise ends that pass early), and a teardown wedged in a blocking call
    reaches no checkpoint afterwards. The guard used to write its arm/force
    bytes FROM those handlers, so when that happened no force byte was ever
    written and the phase sat out its whole teardown deadline before forcing
    — the live shape behind test_mixed_signal_pair_forces_regardless_of_order
    failing ~7% of coverage runs, and 52 of 60 runs of a reproduction.

    Here the child masks SIGTERM on its main thread once teardown begins, so
    the second signal provably cannot reach a Python handler, while the
    watchdog thread — created before the mask existed — still has it
    unblocked, so the interpreter's own wakeup byte is still written. That
    turns the race into a certainty: this is exactly "delivered, but the
    handler did not run". Arming and forcing are counted from those wakeup
    bytes now, so the force still happens at once.
    """
    with _spawned(tmp_path, "stranded") as child:
        _wait_line(child, "PHASE-START")
        os.kill(child.pid, signal.SIGINT)  # first signal: arms, teardown begins
        _wait_line(child, "TEARDOWN-START")  # the mask is in place by now
        _wait_until_blocked(child.pid)  # ...and no eval checkpoint remains
        t_kill = time.monotonic()
        os.kill(child.pid, signal.SIGTERM)  # deliverable, but un-handleable
        rc, out = _finish(child)
    elapsed = time.monotonic() - t_kill
    assert "FORCE-HOOK" in out, f"never forced at all; child said:\n{out[-4000:]}"
    assert "PHASE-EXITED" not in out
    assert rc in (130, 143), f"expected 128+signum of either signal, got {rc}"
    # Discriminator, not a budget: 5s is the child's own teardown deadline, so
    # anything at or past it IS the deadline path. Widening this would delete
    # the only thing the test checks.
    assert elapsed < 2.0, (
        f"forced exit took {elapsed:.1f}s — the teardown deadline expired, so the "
        f"second signal never forced; child said:\n{out[-4000:]}"
    )


@pytest.mark.serial_timing
def test_second_signal_forces_after_asyncio_takes_the_wakeup_fd_away(tmp_path):
    """Losing the wakeup fd mid-phase must cost nothing — the handler still counts.

    The wakeup fd is process-global and asyncio takes it in
    ``add_signal_handler``; worse, ``remove_signal_handler`` hands back -1
    rather than the previous owner, so a single async command run inside a
    phase ends the guard's wakeup channel permanently. That is not a corner:
    ``otto test`` enters a phase and then runs an entire in-process pytest
    session inside it, and any suite that calls ``run_command`` installs an
    asyncio loop with signal handlers.

    So the two channels are COUNTED, not switched between: the handler keeps
    writing its bytes even while the wakeup fd is held, and the watchdog
    believes whichever channel has counted more deliveries. A guard that
    instead treated the wakeup fd as its channel while it thought it owned
    one would go completely deaf here — no arm, no force, no deadline, on the
    exact path this guard exists to bound.
    """
    with _spawned(tmp_path, "stolen") as child:
        _wait_line(child, "FD-STOLEN")  # premise: the fd is gone, asserted child-side
        _wait_line(child, "PHASE-START")
        os.kill(child.pid, signal.SIGINT)  # first signal: arms, teardown begins
        _wait_line(child, "TEARDOWN-START")
        t_kill = time.monotonic()
        os.kill(child.pid, signal.SIGTERM)  # second signal: must force at once
        rc, out = _finish(child)
    elapsed = time.monotonic() - t_kill
    assert "FORCE-HOOK" in out, f"never forced at all; child said:\n{out[-4000:]}"
    assert "PHASE-EXITED" not in out
    assert rc in (130, 143), f"expected 128+signum of either signal, got {rc}"
    # Discriminator, not a budget: the child's deadline is 30s, so the only
    # other way out is the deadline path. Widening this deletes the test.
    assert elapsed < 2.0, (
        f"forced exit took {elapsed:.1f}s — the second signal did not force, so the "
        f"handler channel died with the wakeup fd; child said:\n{out[-4000:]}"
    )


def test_wakeup_fd_is_taken_only_when_nobody_else_owns_it(real_sync_phase):
    """The guard offers to take the wakeup fd; it never seizes it.

    That fd is process-global — one owner per interpreter — so taking it
    unconditionally is an API change to every other component in the process,
    however local the diff looks. asyncio owns it whenever a loop has signal
    handlers installed, and an owner that loses it stops being woken by
    signals at all; what that costs a guard is measured by
    ``test_second_signal_forces_after_asyncio_takes_the_wakeup_fd_away``,
    and there is no reason to inflict the same on anyone else.
    """
    r, w = os.pipe()
    try:
        os.set_blocking(w, False)
        prior = signal.set_wakeup_fd(w)
        try:
            with sync_phase(deadline=30.0, what="probe"):
                # Re-setting returns the CURRENT owner: still ours iff the
                # phase left it alone.
                assert signal.set_wakeup_fd(w) == w, (
                    "sync_phase took a signal-wakeup fd that was already owned"
                )
        finally:
            signal.set_wakeup_fd(prior)
    finally:
        os.close(r)
        os.close(w)
    # And with the fd free, it DOES take it — otherwise the conditional above
    # would pass just as well on a guard that never claims anything.
    with sync_phase(deadline=30.0, what="probe"):
        owner = signal.set_wakeup_fd(-1)
        assert owner != -1, "sync_phase did not take a wakeup fd that was free"
        signal.set_wakeup_fd(owner)


def test_release_leaves_a_wakeup_fd_someone_else_took_mid_phase(real_sync_phase):
    """Giving the fd back is conditional too, for the same reason taking it is.

    Ownership can change WHILE the phase runs — asyncio takes the fd in
    ``add_signal_handler`` — so a guard that clears the fd on the way out
    just because it claimed one on the way in destroys the new owner's
    registration, which is precisely the breakage the conditional claim
    exists to avoid, arriving through the exit door instead.
    """
    r, w = os.pipe()
    try:
        os.set_blocking(w, False)
        with sync_phase(deadline=30.0, what="probe"):
            signal.set_wakeup_fd(w)  # what asyncio does on the phase's behalf
        owner = signal.set_wakeup_fd(-1)
        assert owner == w, "sync_phase cleared a wakeup fd it no longer owned"
    finally:
        signal.set_wakeup_fd(-1)
        os.close(r)
        os.close(w)
