"""End-to-end proof that otto's commands stay out of a real host's shell history.

Runs against the live unix bed. The unit suite proves the payload is
composed and written; only a real interactive shell can prove the *effect* —
that bash, having read the user's rc files and holding a real ``HISTFILE``,
actually stops recording, and that the file on disk is untouched after the
session closes (bash writes history at exit, so nothing before close proves
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
"""

import contextlib
import uuid
from collections.abc import Iterator

import pytest
import pytest_asyncio

from otto import register_login_proxy
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
def leased_host(tmp_path_factory) -> Iterator[tuple[str, str]]:
    """Lease one Unix host from the pool; yield ``(element, ip)``."""
    lock_dir = tmp_path_factory.getbasetemp().parent
    with lease_unix_host(lock_dir, _UNIX_POOL) as element:
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
                "element": element,
                "creds": [dict(c) for c in _CREDS],
            }
            data.update(overrides)
            host = create_host_from_dict(data)
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

bash writes ~/.bash_history in its exit path and only then does the process go
away, so "the session's shell is gone" *happens after* "the history write
landed". Waiting on that predicate is what makes this module safe on a slow or
loaded host: a wall-clock window would have to be guessed, and guessing short
fails the positive control spuriously while guessing anything at all caps how
long a suppression leak has to show up. A deterministic predicate just waits
longer on a slow host and still gives the right answer.

Measured on the bed: the shell disappears 0.12s after close, and the first
sample taken afterwards saw the write 10 times out of 10. This 30s bound is
two orders of magnitude above that, so it can only fire on a genuine hang --
and when it fires it raises, rather than quietly letting a test decide.
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
            f"{_WRITER_EXIT_TIMEOUT:.0f}s after the session closed, so "
            f"~/.bash_history cannot be sampled meaningfully"
        ),
    )


async def _shells_histfile(host) -> str:
    """What ``$HISTFILE`` is inside the interactive session otto actually drives."""
    result = (await host.run('echo "HISTFILE=[$HISTFILE]"')).only
    assert result.status is Status.Success, f"probe failed: {result!r}"
    return str(result.value).strip()


@pytest.mark.asyncio
async def test_default_host_leaves_history_untouched(leased_host, make_host):
    """The whole point: a default UnixHost writes nothing to ~/.bash_history."""
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
    # Leaving the context closes the session — the moment bash flushes history.
    # Both checks below are taken after that write has provably completed, so
    # the grep further down is protected by this wait too, not just the digest.
    await _await_writer_exit(host, pid, element)

    after = await _history_digest(host, element)
    assert after == before, f"{element}: otto's commands reached ~/.bash_history"

    # Emit a verdict word, not a count: `grep -c` prints "0" AND exits 1 on no
    # match, so any `|| fallback` fires too and the output is ambiguous —
    # while asserting the *marker* is absent from a count can never fail at all.
    leaked = await host.exec(
        f"if grep -q -F {marker} ~/.bash_history 2>/dev/null; then echo LEAKED; else echo CLEAN; fi"
    )
    assert str(leaked.value).strip() == "CLEAN", (
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
    """
    element, ip = leased_host
    host = make_host(ip, element, shell_history=True)
    backup = f"/tmp/otto-history-backup-{uuid.uuid4().hex[:8]}"

    before = await _history_digest(host, element)
    # Record whether the file existed at all: restoring a `cp` of a missing
    # file would CREATE an empty ~/.bash_history where the bed had none. The
    # digest wouldn't notice (both hash as empty) but the bed would be altered.
    existed = str((await host.exec("test -f ~/.bash_history && echo yes || echo no")).value).strip()
    await host.exec(f"cp ~/.bash_history {backup} 2>/dev/null || : > {backup}")
    try:
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

        await _await_writer_exit(host, pid, element)
        after = await _history_digest(host, element)
        assert after != before, (
            f"{element}: history did not change even with recording ENABLED — the "
            f"digest cannot detect pollution, so this module's suppression "
            f"assertions are vacuous"
        )
    finally:
        # Restore byte-for-byte; leave no trace of the control on a shared bed.
        # `rm` is unconditional (`;` not `&&`) so a failed restore can't also
        # leak the backup file, and a bed that had no history file gets none.
        restore = f"cp {backup} ~/.bash_history" if existed == "yes" else "rm -f ~/.bash_history"
        await host.exec(f"{restore}; rm -f {backup}")

    # No writer-exit wait here: the restore above is a synchronous `cp` over an
    # exec channel that has already returned, so there is no pending write to
    # race with.
    assert await _history_digest(host, element) == before, (
        f"{element}: failed to restore ~/.bash_history after the positive control"
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
    async with host, host.as_user("root"):
        assert (await host.run(f"echo {marker}")).only.status is Status.Success
        # Root's shell is the one that would write /root/.bash_history, so it
        # is that shell's exit we must wait on, not the unprivileged one's.
        root_pid = await _session_pid(host)

    await _await_writer_exit(host, root_pid, element)
    after = await _root_digest()
    assert after == before, f"{element}: otto polluted root's history"
