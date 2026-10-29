"""End-to-end proof that otto's commands stay out of a real host's shell history.

Runs against the live unix bed. The unit suite proves the payload is
composed and written; only a real interactive shell can prove the *effect* —
that bash, having read the user's rc files and holding a real ``HISTFILE``,
actually stops recording, and that the file on disk is untouched after the
session ends (bash writes history at exit, so nothing before the end proves
anything).

Two observations per case, because either alone is weak:

- ``$HISTFILE`` **as the live shell sees it** — the mechanism, read back
  through the very session under test.
- the **sha256 of the history file on disk**, sampled before and after through
  a *separate* connection. It must be sampled over ``exec`` rather than the
  session: an ``exec`` channel has no PTY and bash disables history there
  outright, so the measurement cannot perturb what it measures.

``test_opting_in_still_records`` is the positive control. Without it the
suppression assertions could pass against a bed where nothing writes history
for some unrelated reason (no rc file, ``HISTFILE`` already unset, a
restricted shell), and the suite would look green while proving nothing.

HOW A SESSION ENDS DECIDES WHETHER bash SAVES. bash (5.2 on the bed) writes
``~/.bash_history`` on two paths: the ``exit`` builtin, which always saves, and
a SIGHUP that lands while bash is idle in readline, waiting for a command. A
SIGHUP that lands while bash is still busy -- finishing the command whose
output otto just read, or running ``PROMPT_COMMAND`` -- kills bash without it
ever opening the file. Leaving ``async with host:`` closes the channel straight
after the last command's output, sends no ``exit``, and so races bash back to
readline. bash nearly always wins, which makes it a flake, not a failure.

So every test here that NEEDS a write to happen ends its shell with ``exit``
(:func:`_exit_shell`), and the ``busy_prompt`` pair below holds that window
open on purpose (:func:`_busy_until_released`): one test proves a hangup
inside it loses the write, the other proves ``exit`` saves anyway. The
suppression tests end on both paths (see
:func:`test_default_host_leaves_history_untouched`).
"""

import contextlib
import logging
import sys
import uuid
from collections.abc import AsyncIterator, Iterator

import pytest
import pytest_asyncio

from otto import register_login_proxy
from otto.host.element import Element
from otto.host.factory import create_host_from_dict
from otto.utils import Status, wait_for_async
from tests._fixtures._host_pool import UNIX_POOL as _UNIX_POOL
from tests._fixtures._host_pool import lease_unix_host
from tests._fixtures.labdata import host_data

pytestmark = [pytest.mark.integration, pytest.mark.xdist_group("shell_history_e2e")]


# root has NO password on the vagrant bed, so a plain `su root` fails auth.
# vagrant does have passwordless sudo, so elevation must be root-mediated —
# the same constraint (and the same shape of proxy) as test_login_proxy_e2e.py.
# Registered with overwrite=True so re-import under xdist is idempotent.
async def _sudo_su_root(io, ctx):
    await io.send("sudo su -\n")


async def _sudo_su_root_undo(io, ctx):
    await io.send("exit\n")


register_login_proxy("sudo-su-root", _sudo_su_root, undo=_sudo_su_root_undo, overwrite=True)

_CREDS: list[dict[str, str]] = [
    {"login": "vagrant", "password": "vagrant"},
    {"login": "root", "proxy": "sudo-su-root", "via": "vagrant"},
]


@pytest.fixture
def leased_host() -> Iterator[tuple[str, str]]:
    """Lease one Unix host from the pool; yield ``(element, ip)``."""
    with lease_unix_host(_UNIX_POOL) as element:
        yield element, host_data(element)["ip"]


@pytest_asyncio.fixture
async def make_host():
    """Build hosts for a test, and close every one of them at teardown.

    Closing belongs to the fixture rather than to each test because this
    module deliberately drives ``exec`` *outside* ``async with host:`` — the
    history digest has to be sampled before the session opens and again after
    it closes, or it proves nothing about what bash flushed at exit.

    That is exactly the shape that leaks. ``exec`` lazily (re)opens the host's
    shared SSH connection, and leaving the ``async with`` block does not close
    what a later ``exec`` reopened; only ``host.close()`` does. An unclosed
    connection's asyncio transport is then reaped by GC against an
    already-closed event loop, raising ``ResourceWarning`` from ``__del__`` —
    which pytest's ``[unraisable]`` plugin escalates into a teardown ERROR on
    whichever test happens to run *next*, not the one that leaked. The release
    lane makes that deterministic by forcing a ``gc.collect()`` per test
    (``OTTO_DETECT_ASYNCIO_LEAKS=1``); without it the same leak is a
    long-fuse flake that lands on an unrelated test.

    ``AsyncExitStack`` so that one failing ``close`` still unwinds the rest —
    a half-closed teardown would resurrect the very flake this prevents.
    """
    async with contextlib.AsyncExitStack() as stack:

        def _make(ip: str, element: str, **overrides: object):
            data: dict[str, object] = {
                "ip": ip,
                "creds": [dict(c) for c in _CREDS],
            }
            data.update(overrides)
            host = create_host_from_dict(data, element=Element(element))
            # close() is idempotent, so tests that also use `async with host:`
            # are free to close early — this just guarantees it happens.
            stack.push_async_callback(host.close)
            return host

        yield _make


async def _history_digest(host, element: str) -> str:
    """sha256 of ~/.bash_history, measured over a PTY-less exec channel.

    ``exec`` is deliberate: it is non-interactive, so bash keeps no history
    there and the measurement cannot pollute the thing being measured. A
    missing file reads as a stable sentinel rather than an error — a fresh VM
    legitimately has none yet.
    """
    result = await host.exec("cat ~/.bash_history 2>/dev/null | sha256sum || true")
    if result.status is not Status.Success:
        raise AssertionError(f"{element}: could not sample history file: {result!r}")
    return str(result.value).strip()


_WRITER_EXIT_TIMEOUT = 30.0
"""Runaway guard on the shell's exit — deliberately NOT a discriminator.

"The session's shell is gone" means "bash will write nothing more", NOT
"bash wrote its history". Where bash saves at all, it saves before the
process goes away, so once the shell is gone a sample of the file is final:
any write that is coming has landed. But a shell hung up while busy (see the
module docstring) goes away having written nothing. That is why a test that
needs the write ends its shell with ``exit`` first (:func:`_exit_shell`), and
this wait only rules out sampling too early.

Waiting on that predicate is what makes this module safe on a slow or loaded
host: a wall-clock window would have to be guessed, and guessing short fails
the positive control spuriously while guessing anything at all caps how long
a suppression leak has to show up. A deterministic predicate just waits
longer on a slow host and still gives the right answer.

Measured on the bed: the shell disappears about 0.12s after ``exit`` or a
hangup. This 30s bound is two orders of magnitude above that, so it can only
fire on a genuine hang -- and when it fires it raises, rather than quietly
letting a test decide.
"""

_WRITER_POLL = 0.05

# WHY THIS MODULE IS bash-AND-THE-UNIX-BED ONLY, and must stay that way.
#
# The obvious extension is to point these tests at the BusyBox guests. Measured
# on all five (1.16.1, 1.21.1, 1.28.1, 1.31.0, 1.35.0), that would produce a
# suite of tests that CANNOT FAIL. Both primitives used above do work there --
# `$$` names the session's shell and /proc/<pid> is observable on every version
# -- but with recording deliberately switched ON, the only line ash ever
# persists is otto's own opening handshake. Explicit commands sent afterwards
# (`echo <marker>`) never reach ~/.ash_history at all, on any of the five. So a
# digest comparison there would report "nothing leaked" on a target that
# records nothing to begin with, which is precisely the vacuity the positive
# control in this module exists to rule out.
#
# BusyBox is covered instead by tests/unit/host/test_history_suppression_portability.py,
# which proves the payload PARSES and takes effect on the ash dialect. That is
# the part that can actually differ between shells; "the bytes never reach the
# disk" is proven here, once, against a shell that demonstrably writes them.


async def _session_pid(host) -> str:
    """PID of the interactive shell otto is driving.

    Probed with ``run`` (inside the session) rather than ``exec``: ``$$`` must
    name the shell that will do the writing. Verified against the bed --
    ``/proc/<pid>/comm`` is ``bash`` and its cmdline is ``-bash``, the login
    shell, not a subshell of it.
    """
    result = (await host.run("echo $$")).only
    assert result.status is Status.Success, f"could not read the session pid: {result!r}"
    return str(result.value).strip()


async def _await_writer_exit(host, pid: str, element: str) -> None:
    """Block until the shell that owns the history file has exited.

    Sampling ~/.bash_history before this returns is meaningless: the read goes
    over a separate exec channel from the session that writes the file, so it
    can beat the write to disk. That cost one observed flake in the positive
    control, and -- far more quietly -- would let every suppression assertion
    call a not-yet-flushed leak clean.

    It proves only that bash will write nothing MORE, not that it wrote
    anything: a shell hung up while busy exits without saving. Tests that need
    the write call :func:`_exit_shell` first; this is then a no-op guard.

    ``wait_for_async`` owns the loop: a hand-rolled deadline poll here is what
    the ``no-handrolled-deadline-poll`` lint rule exists to stop, and it would
    also have swallowed the timeout into a bare ``AssertionError`` instead of
    otto's ``WaitTimeoutError``.
    """

    async def _shell_is_gone() -> bool:
        alive = await host.exec(f"test -d /proc/{pid} && echo ALIVE || echo GONE")
        return str(alive.value).strip() == "GONE"

    await wait_for_async(
        _shell_is_gone,
        timeout=_WRITER_EXIT_TIMEOUT,
        interval=_WRITER_POLL,
        on_timeout=(
            f"{element}: the session's shell (pid {pid}) was still alive "
            f"{_WRITER_EXIT_TIMEOUT:.0f}s after the session ended, so "
            f"~/.bash_history cannot be sampled meaningfully"
        ),
    )


async def _exit_shell(host, pid: str, element: str, release: str | None = None) -> None:
    """End the session's shell with ``exit`` and wait until it is gone.

    Called inside ``async with host:``, before leaving the block closes the
    channel. ``exit`` runs bash's exit builtin, which saves history whatever
    bash was doing when the line arrived: if it is still busy, the line waits
    in the terminal until bash is back in readline. Leaving the block instead
    hangs up on bash, and a hangup that lands while bash is busy loses the
    write (see the module docstring). Waiting for the PID here, rather than
    after the block, means the close that follows has nothing left to race.

    *release* names a :func:`_busy_until_released` gate to remove after the
    ``exit`` is sent, so a deliberately busy bash can reach the waiting line.
    """
    await host.send("exit\n")
    if release is not None:
        await _release(host, release)
    await _await_writer_exit(host, pid, element)


def _note_on(exc: BaseException, text: str) -> None:
    """Attach *text* to an exception already on its way out, never replacing it.

    ``add_note`` exists from Python 3.11; on 3.10 the note goes to the log.
    """
    if sys.version_info >= (3, 11):
        exc.add_note(text)
    else:
        logging.getLogger(__name__).error(text)


async def _restore_history(
    host, element: str, backup: str, existed: str, before: str
) -> str | None:
    """Put ~/.bash_history back from *backup*; return what is wrong, or ``None``.

    `rm` of the backup is unconditional (`;` not `&&`) so a failed restore
    can't also leak the backup file, and a bed that had no history file gets
    none.
    """
    restore = f"cp {backup} ~/.bash_history" if existed == "yes" else "rm -f ~/.bash_history"
    await host.exec(f"{restore}; rm -f {backup}")
    # No writer-exit wait here: the restore is a synchronous `cp` over an exec
    # channel that has already returned.
    after = await _history_digest(host, element)
    if after != before:
        return f"{element}: failed to restore ~/.bash_history (digest {after}, expected {before})"
    return None


@contextlib.asynccontextmanager
async def _history_restored(host, element: str) -> AsyncIterator[str]:
    """Back up ~/.bash_history, yield its digest, and restore it byte-for-byte.

    For the tests that write history on purpose. The bed is shared, so the
    file is put back exactly as found and the restore is verified by digest
    on every path. When the body succeeded, a bad restore fails the test.
    When the body already raised, a bad restore is attached to that exception
    as a note and never replaces it.

    The restore is byte-exact only when the body ended its shell first: a
    shell still alive at restore time can write after the `cp`.
    """
    before = await _history_digest(host, element)
    # Record whether the file existed at all: restoring a `cp` of a missing
    # file would CREATE an empty ~/.bash_history where the bed had none. The
    # digest wouldn't notice (both hash as empty) but the bed would be altered.
    # An answer that is neither word would pick the `rm` restore and delete a
    # real history file, so it stops the test here instead.
    existed = str((await host.exec("test -f ~/.bash_history && echo yes || echo no")).value).strip()
    assert existed in ("yes", "no"), (
        f"{element}: could not tell whether ~/.bash_history exists: {existed!r}"
    )
    backup = f"/tmp/otto-history-backup-{uuid.uuid4().hex[:8]}"
    if existed == "yes":
        # The backup is proven a byte-exact copy before anything may write:
        # a failed `cp` would otherwise "restore" garbage over the real file.
        copied = await host.exec(f"cp ~/.bash_history {backup} && sha256sum < {backup}")
        assert copied.status is Status.Success, (
            f"{element}: could not back up ~/.bash_history: {copied!r}"
        )
        assert str(copied.value).strip() == before, (
            f"{element}: the backup of ~/.bash_history does not match it: {copied!r}"
        )
    failure: BaseException | None = None
    try:
        yield before
    except BaseException as exc:
        failure = exc
        raise
    finally:
        if failure is None:
            problem = await _restore_history(host, element, backup, existed, before)
            assert problem is None, problem
        else:
            try:
                problem = await _restore_history(host, element, backup, existed, before)
            except Exception as restore_exc:  # noqa: BLE001 — must never replace the body's failure
                problem = f"{element}: restoring ~/.bash_history raised {restore_exc!r}"
            if problem is not None:
                _note_on(failure, problem)


@contextlib.asynccontextmanager
async def _busy_until_released(host, element: str) -> AsyncIterator[str]:
    """Create a gate file on the host and yield its path; remove it on exit.

    :func:`_gate_prompt` turns it into a ``PROMPT_COMMAND``. Set inside one
    session only (never in the bed's rc files), that keeps bash busy -- out
    of readline -- until the gate file is removed. otto reads a command's
    output before bash runs ``PROMPT_COMMAND``, so the command that sets it
    returns, and from then on
    bash is busy for as long as the test says: a hangup is certain to land
    in the window, with no wall-clock guess about how fast the close is.

    The gate is removed when the block exits, on every path. A test that
    hangs up waits for the shell's PID to go first; one that sends ``exit``
    removes the gate itself (:func:`_release`) so bash can reach the line.
    """
    gate = f"/tmp/otto-gate-{uuid.uuid4().hex[:8]}"
    made = await host.exec(f"touch {gate}")
    assert made.status is Status.Success, f"{element}: could not create {gate}: {made!r}"
    try:
        yield gate
    finally:
        await _release(host, gate)


async def _release(host, gate: str) -> None:
    """Remove the gate file, letting a busy ``PROMPT_COMMAND`` loop finish."""
    await host.exec(f"rm -f {gate}")


def _gate_prompt(gate: str) -> str:
    """The ``PROMPT_COMMAND`` that loops until *gate* is gone."""
    return f"while [ -e {gate} ]; do sleep 0.1; done"


async def _marker_recorded(host, marker: str) -> bool:
    """Whether *marker* appears in ~/.bash_history, read over ``exec``.

    A verdict word, not a count: `grep -c` prints "0" AND exits 1 on no
    match, so any `|| fallback` fires too and the output is ambiguous.
    """
    found = await host.exec(
        f"if grep -q -F {marker} ~/.bash_history 2>/dev/null; then echo FOUND; else echo ABSENT; fi"
    )
    verdict = str(found.value).strip()
    assert verdict in ("FOUND", "ABSENT"), f"could not grep ~/.bash_history: {found!r}"
    return verdict == "FOUND"


async def _shells_histfile(host) -> str:
    """What ``$HISTFILE`` is inside the interactive session otto actually drives."""
    result = (await host.run('echo "HISTFILE=[$HISTFILE]"')).only
    assert result.status is Status.Success, f"probe failed: {result!r}"
    return str(result.value).strip()


@pytest.mark.asyncio
@pytest.mark.parametrize("ending", ["exit", "hangup"])
async def test_default_host_leaves_history_untouched(leased_host, make_host, ending):
    """The whole point: a default UnixHost writes nothing to ~/.bash_history.

    Checked on both ways a session can end, because bash can save on either:

    - ``exit``: bash's exit builtin, which always reaches the save. This is
      the strict case -- if suppression ever broke, this ending is certain to
      write, so a clean digest here cannot be luck.
    - ``hangup``: leaving ``async with host:``, which is how otto itself ends
      a session. A hangup while bash is idle in readline saves too, so this
      proves suppression holds on the path users actually get. (A hangup
      while bash is busy saves nothing, which can only make this case pass
      more easily -- hence the ``exit`` case.)
    """
    element, ip = leased_host
    host = make_host(ip, element)

    before = await _history_digest(host, element)

    # A marker unique per run, so a failure names exactly which run leaked.
    marker = f"otto-history-probe-{uuid.uuid4().hex[:8]}"
    async with host:
        assert await _shells_histfile(host) == "HISTFILE=[/dev/null]"
        for _ in range(3):
            assert (await host.run(f"echo {marker}")).only.status is Status.Success
        pid = await _session_pid(host)
        if ending == "exit":
            await _exit_shell(host, pid, element)
    # Whatever the ending, nothing is sampled until the shell is gone: from
    # then on bash can write nothing more, so both checks below are final.
    await _await_writer_exit(host, pid, element)

    after = await _history_digest(host, element)
    assert after == before, f"{element}: otto's commands reached ~/.bash_history"
    assert not await _marker_recorded(host, marker), (
        f"{element}: marker {marker} found in ~/.bash_history"
    )


@pytest.mark.asyncio
async def test_opting_in_still_records(leased_host, make_host):
    """Positive control: with ``shell_history=True`` otto's commands DO land.

    This is what makes every suppression assertion in this module meaningful.
    It is not enough to check that ``$HISTFILE`` looks sane under opt-in — the
    digest comparison the other tests rely on has to be shown capable of
    *detecting* pollution, or a bed that silently never writes history would
    make them all pass while proving nothing.

    So this deliberately pollutes, asserts the digest moved, and then restores
    the file byte-for-byte, verifying the digest returns to its original
    value. The bed is left exactly as found.

    The shell is ended with ``exit``, not a hangup: a hangup can land while
    bash is still finishing the last command, and then bash saves nothing
    (see the module docstring). That race is what made this control flaky.
    """
    element, ip = leased_host
    host = make_host(ip, element, shell_history=True)

    async with _history_restored(host, element) as before:
        marker = f"otto-control-probe-{uuid.uuid4().hex[:8]}"
        async with host:
            seen = await _shells_histfile(host)
            assert seen != "HISTFILE=[/dev/null]", f"{element}: suppressed despite opt-in"
            assert seen != "HISTFILE=[]", (
                f"{element}: the bed's shell has no HISTFILE at all, so the "
                f"suppression assertions here prove nothing — fix the bed, not the test"
            )
            for _ in range(3):
                assert (await host.run(f"echo {marker}")).only.status is Status.Success
            pid = await _session_pid(host)
            await _exit_shell(host, pid, element)

        after = await _history_digest(host, element)
        assert after != before, (
            f"{element}: history did not change even with recording ENABLED — the "
            f"digest cannot detect pollution, so this module's suppression "
            f"assertions are vacuous"
        )


async def _busy_prompt_session(host, marker: str, gate: str) -> str:
    """Record *marker*, then keep bash busy until *gate* goes; return the PID.

    The opt-in session's shell keeps history, so the marker is due to be
    saved. The PID is read before the busy ``PROMPT_COMMAND`` is set, because
    once it is set bash reads no further command until the gate is released.
    """
    assert (await host.run(f"echo {marker}")).only.status is Status.Success
    pid = await _session_pid(host)
    busy = (await host.run(f"PROMPT_COMMAND='{_gate_prompt(gate)}'")).only
    assert busy.status is Status.Success, f"could not set the busy PROMPT_COMMAND: {busy!r}"
    return pid


@pytest.mark.asyncio
async def test_busy_prompt_hangup_loses_the_history_write(leased_host, make_host):
    """A hangup while bash is busy kills it before it saves history.

    The control for the test below: it proves the gated ``PROMPT_COMMAND``
    really holds bash out of readline, so the ``exit`` test is not passing on
    a bed where the window never opens. It also pins the bash behaviour that
    made the positive control flaky. The gate stays until the shell is gone,
    so the hangup lands in the window however slow the close is.

    It turns red if a hangup while busy starts saving: a bash that saves on
    any SIGHUP, or a session close that sends ``exit`` and lets bash run it
    before hanging up. #717 plans that graceful close for ``shell_history``
    sessions, and is the expected reason for this test to change; when it
    does, this module's ``exit`` endings can be revisited. A close that sends
    ``exit`` and hangs up straight away does NOT turn it red, and rightly:
    the ``exit`` waits behind the gate, so the busy shell still loses the
    write. So this control cannot see a time-bounded graceful close: #717
    needs its own test of that close on an idle, ungated shell.
    """
    element, ip = leased_host
    host = make_host(ip, element, shell_history=True)

    async with _history_restored(host, element) as before:
        marker = f"otto-hangup-probe-{uuid.uuid4().hex[:8]}"
        async with _busy_until_released(host, element) as gate:
            async with host:
                pid = await _busy_prompt_session(host, marker, gate)
            await _await_writer_exit(host, pid, element)

        assert await _history_digest(host, element) == before, (
            f"{element}: a hangup while bash was busy still saved history. Either "
            f"bash now saves on SIGHUP outside readline, or the session's close now "
            f"sends `exit` and bash ran it (#717 plans that for shell_history "
            f"sessions) -- this module's `exit` endings can then be revisited"
        )
        assert not await _marker_recorded(host, marker), (
            f"{element}: marker {marker} saved despite a hangup while bash was busy"
        )


@pytest.mark.asyncio
async def test_busy_prompt_exit_still_saves_history(leased_host, make_host):
    """``exit`` saves history even when bash is busy as it arrives.

    The regression test for the positive control's flake. The same gated
    window that loses the write on a hangup (the test above) must not lose it
    when the shell is ended the way :func:`_exit_shell` ends it. ``exit`` is
    sent while bash is still busy, so it waits in the terminal; only then is
    the gate released, and bash reads it the moment it is back in readline.
    It turns red if :func:`_exit_shell` stops sending ``exit`` before the
    session closes.
    """
    element, ip = leased_host
    host = make_host(ip, element, shell_history=True)

    async with _history_restored(host, element) as before:
        marker = f"otto-exit-probe-{uuid.uuid4().hex[:8]}"
        async with _busy_until_released(host, element) as gate, host:
            pid = await _busy_prompt_session(host, marker, gate)
            await _exit_shell(host, pid, element, release=gate)

        assert await _history_digest(host, element) != before, (
            f"{element}: ending with `exit` while bash was busy wrote no history"
        )
        assert await _marker_recorded(host, marker), (
            f"{element}: marker {marker} missing although the shell ended with `exit`"
        )


@pytest.mark.asyncio
async def test_suppression_survives_a_login_proxy_hop(leased_host, make_host):
    """``su`` starts a fresh shell that re-reads rc files, resetting HISTFILE.

    This is the only test covering the resync-probe half of the feature: the
    payload rides ``_resync_shell``'s probe so the elevated shell is quieted by
    the very first line it executes.
    """
    element, ip = leased_host
    host = make_host(ip, element)

    async with host:
        assert await _shells_histfile(host) == "HISTFILE=[/dev/null]"
        async with host.as_user("root"):
            assert await _shells_histfile(host) == "HISTFILE=[/dev/null]", (
                f"{element}: HISTFILE reset when su'ing to root — the resync "
                f"probe did not carry the suppression payload"
            )
        # Back in the original shell, still quiet.
        assert await _shells_histfile(host) == "HISTFILE=[/dev/null]"


@pytest.mark.asyncio
async def test_root_history_untouched_across_elevation(leased_host, make_host):
    """Root's own history file must not collect otto's liveness probes either."""
    element, ip = leased_host
    host = make_host(ip, element)

    async def _root_digest() -> str:
        result = await host.exec("sudo cat /root/.bash_history 2>/dev/null | sha256sum || true")
        # Same guard `_history_digest` carries: without it a failed sample
        # hashes to a different string and reports as "otto polluted root's
        # history", blaming the feature for a broken measurement.
        if result.status is not Status.Success:
            raise AssertionError(f"{element}: could not sample root's history: {result!r}")
        return str(result.value).strip()

    before = await _root_digest()
    marker = f"otto-root-probe-{uuid.uuid4().hex[:8]}"
    # No `_exit_shell` needed: leaving `as_user` runs the proxy's undo, which
    # ends root's shell with `exit`. That is bash's always-saving path, so a
    # broken suppression is certain to show in root's digest. Only the login
    # shell underneath is then hung up, and it does not own root's file.
    async with host, host.as_user("root"):
        assert (await host.run(f"echo {marker}")).only.status is Status.Success
        # Root's shell is the one that would write /root/.bash_history, so it
        # is that shell's exit we must wait on, not the unprivileged one's.
        root_pid = await _session_pid(host)

    await _await_writer_exit(host, root_pid, element)
    after = await _root_digest()
    assert after == before, f"{element}: otto polluted root's history"
