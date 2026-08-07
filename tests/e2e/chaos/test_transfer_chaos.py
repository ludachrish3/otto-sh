"""Transfer chaos: interrupt mid-stream both directions; assert no orphaned nc
listener beyond the teardown deadline and characterize partial-file state.
Forces the nc backend (``--transfer nc``) so the GET-path reap (Task 7's
product fix) is exercised on whichever host was leased.

Self-match note: the nc-listener probe IS
``tests/_fixtures/bed_hygiene.py``'s ``_NC_LISTENER_PROBE`` (imported, not
mirrored — a verbatim copy here silently missed that module's move to the
``argv_pattern`` bracket-trick, and the honesty scan caught it). The probe
must never see the invoking shell's own command line, which embeds the
search pattern and would otherwise self-match on a fresh, ever-different
pid on EVERY probe, permanently poisoning the before/after diff with a
spurious "new" line that is never a real orphaned listener.

``has_tunnel`` caveat (self-review finding, recorded here rather than swept
under the rug): the leased veggies hosts (carrot/tomato/pepper) are reached
directly — no ``--hop`` — so ``NcFileTransfer._connections.has_tunnel`` is
``False`` for this bed, and GET dispatches to ``_get_files_nc`` (otto binds a
local server; the remote runs plain ``nc`` as a *client* pushing bytes back —
never ``nc -l``), not ``_get_files_nc_tunneled`` (the reversed-listener path
that spawns a real remote ``nc -l`` and that Task 7's product fix actually
touches). The PUT direction always spawns a real remote ``nc -l`` regardless
of tunnel state, so ``test_sigint_mid_put_no_orphan_listener`` is a genuine,
unconditional exercise of the (pre-existing) put-path reap. The GET test
here is still a valuable general invariant (no orphaned ``nc -l`` survives
teardown on whichever GET code path actually ran on this bed) but is not
itself the end-to-end proof of the tunneled fix — that proof is the tier-1
unit test, ``tests/unit/host/test_transfer_nc_get.py::
TestNcGetTunneledCancellation``, which drives ``_get_files_nc_tunneled``
directly with ``has_tunnel=True`` (RED pre-fix, GREEN post-fix).
"""

import time

import pytest

from otto.logger.mode import LogMode
from otto.utils import wait_for
from tests._fixtures.bed_hygiene import _NC_LISTENER_PROBE
from tests.e2e.chaos._bed import probe_text, run_probe
from tests.e2e.chaos._seed import offset_in
from tests.integration.chaos._driver import BANNER, spawn_otto

pytestmark = [
    pytest.mark.chaos,
    pytest.mark.stability,
    pytest.mark.integration,
    pytest.mark.xdist_group("chaos_lane"),
    pytest.mark.timeout(300),
]


# 512 MiB: empirically calibrated against the live bed (2026-08-01) rather
# than assumed — a 64 MiB payload (the original starting point) completed the
# ENTIRE put/get, connection setup included, in well under a second (measured
# ~300+ MB/s, an artifact of the fixed per-command overhead dominating such a
# short transfer), so a SIGINT sent 0-1s after the "NC put"/"NC get" phase
# marker would already be racing a finished process. Timed directly against
# carrot_seed: put sustains ~62-75 MB/s (512 MiB -> ~6.5s wall time), get
# ~75-125 MB/s (512 MiB -> ~2.2-4.5s) — comfortably longer than the 0-1s
# injection window in both directions without making the test unduly slow.
_PAYLOAD_SIZE = 512 * 1024 * 1024

_REMOTE_PUT_DIR = "/tmp/otto-chaos-put"
_REMOTE_GET_SRC = "/tmp/otto-chaos-src"


def _nc_listeners(element: str) -> list:
    out = probe_text(element, _NC_LISTENER_PROBE)
    return [ln for ln in out.splitlines() if ln.strip()]


def _assert_no_new_listener(element: str, before: list, what: str) -> None:
    """Poll briefly past the 10s teardown deadline for any NEW nc -l to clear."""
    new: list = []

    def cleared() -> bool:
        nonlocal new
        new = [ln for ln in _nc_listeners(element) if ln not in before]
        return not new

    wait_for(
        cleared,
        15.0,
        interval=0.5,
        on_timeout=lambda: f"{what}: orphaned nc listener beyond teardown deadline: {new}",
    )


def _reap_new_nc_listeners(element: str, before: list) -> None:
    """Belt for the assert-failure path: SIGKILL any nc -l listener that
    wasn't present at *before*, by PID, diffed the same way
    `_assert_no_new_listener` diffs them -- not by `pkill -f` on a
    destination-path token.

    A `pkill -f` on `_REMOTE_PUT_DIR`/`_REMOTE_GET_SRC` looks argv-visible
    but is a no-op: those tokens are redirect operands (`> {dst}` / `< {src}`
    in `src/otto/host/transfer/nc.py`), which the shell consumes before
    `execve` -- they land only in the transient `bash -c` wrapper's cmdline,
    never in the `nc` process's own argv. Killing the wrapper doesn't touch
    its already-forked `nc` child, which reparents to init and keeps the
    port bound. `_NC_LISTENER_PROBE` (`pgrep -af "nc -l"`) matches `nc`
    itself (`-l` IS in its argv), so PIDs pulled from it are the real
    listener processes; killing those actually frees the port. Diffed
    against *before* so a pre-existing listener (this bed's, or another
    test's) is never touched.
    """
    new = [ln for ln in _nc_listeners(element) if ln not in before]
    pids = [ln.split()[0] for ln in new if ln.split()]
    if not pids:
        return
    run_probe(
        element,
        lambda h: h.exec(f"kill -9 {' '.join(pids)} || true", timeout=15, log=LogMode.QUIET),
    )


def test_sigint_mid_put_no_orphan_listener(chaos_bed, chaos_rng, tmp_path):
    big = tmp_path / "payload.bin"
    # Sparse file: same size, a fraction of the write cost of materializing
    # `_PAYLOAD_SIZE` real zero bytes in memory first.
    with big.open("wb") as f:
        f.truncate(_PAYLOAD_SIZE)
    # otto's `put` never mkdir's the remote destination itself (mirrors
    # tests/e2e/host/test_host_transfer_e2e.py's roundtrip test) — the
    # directory must already exist.
    run_probe(
        chaos_bed.element,
        lambda h: h.exec(f"mkdir -p {_REMOTE_PUT_DIR}", timeout=30, log=LogMode.QUIET),
    )
    before = _nc_listeners(chaos_bed.element)
    p = None  # bound inside the try; finally must not assume it got there
    try:
        p = spawn_otto(
            [
                "host",
                "--transfer",
                "nc",
                chaos_bed.target.host_id,
                "put",
                str(big),
                _REMOTE_PUT_DIR,
            ],
            xdir=tmp_path,
            target=chaos_bed.target,
        )
        p.wait_for_log(r"NC put", timeout=120.0)  # phase: transfer streaming (DEBUG line)
        # the ONE deliberate sleep: seeded injection offset
        time.sleep(offset_in(chaos_rng, 0.0, 1.0))
        p.signal(2)  # SIGINT
        p.wait_for_stderr(BANNER, timeout=15.0)  # phase: teardown running
        rc = p.wait(timeout=60.0)
        assert rc == 130, p.stderr_text()
        p.assert_no_process_group()
        # No NEW listener may outlive the deadline.
        _assert_no_new_listener(chaos_bed.element, before, "PUT")
    finally:
        # Belt for the assert-failure path: `p.wait_for_log` above can raise
        # before `p` is ever signaled/reaped, leaving the local subprocess
        # running. SIGKILL it if it's still alive before doing anything else.
        if p is not None and p.proc.poll() is None:
            p.signal(9)
        # Nested so a raising rm probe (post-G5 a dead probe raises) still
        # reports loudly WITHOUT skipping the listener reap below it.
        try:
            run_probe(
                chaos_bed.element,
                lambda h: h.exec(
                    f"rm -rf {_REMOTE_PUT_DIR} || true", timeout=30, log=LogMode.QUIET
                ),
            )
        finally:
            # Belt: an assertion failure above must not strand the remote
            # `nc -l` listener (kill by PID, diffed against `before` -- see
            # `_reap_new_nc_listeners`'s docstring for why a `pkill -f` on the
            # destination-dir token can't do this).
            _reap_new_nc_listeners(chaos_bed.element, before)


def test_sigint_mid_get_no_orphan_listener(chaos_bed, chaos_rng, tmp_path):
    """The GET-path counterpart — see the module docstring's ``has_tunnel``
    caveat for exactly which GET code path this direct-SSH bed exercises."""
    # Seed a big remote file first, then GET it and interrupt mid-stream.
    run_probe(
        chaos_bed.element,
        lambda h: h.exec(
            f"head -c {_PAYLOAD_SIZE} /dev/zero > {_REMOTE_GET_SRC}",
            timeout=60,
            log=LogMode.QUIET,
        ),
    )
    before = _nc_listeners(chaos_bed.element)
    p = None  # bound inside the try; finally must not assume it got there
    try:
        p = spawn_otto(
            [
                "host",
                "--transfer",
                "nc",
                chaos_bed.target.host_id,
                "get",
                _REMOTE_GET_SRC,
                str(tmp_path),
            ],
            xdir=tmp_path,
            target=chaos_bed.target,
        )
        p.wait_for_log(r"NC get", timeout=120.0)
        # the ONE deliberate sleep: seeded injection offset
        time.sleep(offset_in(chaos_rng, 0.0, 1.0))
        p.signal(2)  # SIGINT
        p.wait_for_stderr(BANNER, timeout=15.0)  # phase: teardown running
        rc = p.wait(timeout=60.0)
        assert rc == 130, p.stderr_text()
        p.assert_no_process_group()
        _assert_no_new_listener(chaos_bed.element, before, "GET")
        # Partial-file characterization: record what the local dest looks
        # like. NOT asserted — the hard invariant is the no-orphan-listener
        # check above.
        dest = tmp_path / "otto-chaos-src"
        got = dest.stat().st_size if dest.exists() else None
        print(  # noqa: T201 — characterization output is the point; captured on failure/verbose
            f"partial-file policy (nc get, interrupted): local dest size = {got} "
            f"(of {_PAYLOAD_SIZE})"
        )
    finally:
        # Belt for the assert-failure path: `p.wait_for_log` above can raise
        # before `p` is ever signaled/reaped, leaving the local subprocess
        # running. SIGKILL it if it's still alive before doing anything else.
        if p is not None and p.proc.poll() is None:
            p.signal(9)
        # Nested so a raising rm probe (post-G5 a dead probe raises) still
        # reports loudly WITHOUT skipping the listener reap below it.
        try:
            run_probe(
                chaos_bed.element,
                lambda h: h.exec(f"rm -f {_REMOTE_GET_SRC} || true", timeout=30, log=LogMode.QUIET),
            )
        finally:
            # Belt: an assertion failure above must not strand a remote `nc -l`
            # listener (the tunneled GET path, not exercised on this direct-SSH
            # bed per the module docstring's `has_tunnel` caveat, but harmless
            # to guard defensively). Kill by PID, diffed against `before` -- see
            # `_reap_new_nc_listeners`'s docstring for why a `pkill -f` on the
            # redirect-operand token can't do this.
            _reap_new_nc_listeners(chaos_bed.element, before)
