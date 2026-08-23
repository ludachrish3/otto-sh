"""Transfer chaos: interrupt mid-stream both directions; assert no orphaned nc
listener beyond the teardown deadline and characterize partial-file state.
Forces the nc backend (``--transfer nc``) so the GET-path reap (Task 7's
product fix) is exercised on whichever host was leased.

A THIRD ARM asks the same question of the BusyBox bed's guest, where the
answer has a different shape: nc is refused there by a registered userland
gap, ``shell`` is the transfer those devices actually get, and its remote
state is a staged temp rather than a listener. The two premises do not
substitute for each other -- see that test's own docstring.

Self-match note: the nc-listener probe IS
``tests/_fixtures/bed_hygiene.py``'s ``_NC_LISTENER_PROBE`` (imported, not
mirrored — a verbatim copy here silently missed that module's move to the
``argv_pattern`` bracket-trick, and the honesty scan caught it). The probe
must never see the invoking shell's own command line, which embeds the
search pattern and would otherwise self-match on a fresh, ever-different
pid on EVERY probe, permanently poisoning the before/after diff with a
spurious "new" line that is never a real orphaned listener.

``has_tunnel`` caveat (self-review finding, recorded here rather than swept
under the rug): the leased unix hosts (test1/test2/test3) are reached
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

import re
import time
import uuid

import pytest

from otto.logger.mode import LogMode
from otto.utils import wait_for
from tests._fixtures.bed_hygiene import _NC_LISTENER_PROBE
from tests.e2e.chaos._bed import busybox_probe_text, probe_text, run_probe
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
# test1: put sustains ~62-75 MB/s (512 MiB -> ~6.5s wall time), get
# ~75-125 MB/s (512 MiB -> ~2.2-4.5s) — comfortably longer than the 0-1s
# injection window in both directions without making the test unduly slow.
_PAYLOAD_SIZE = 512 * 1024 * 1024

_REMOTE_PUT_DIR = "/tmp/otto-chaos-put"
_REMOTE_GET_SRC = "/tmp/otto-chaos-src"

# --- BusyBox guest arm ------------------------------------------------------
# 512 KiB, calibrated live the same way `_PAYLOAD_SIZE` was, against the guest
# rather than against a unix host: a shell PUT to bb1350 through the hop
# measured 32 KiB in 1.5s, 128 KiB in 3.1s and 512 KiB in 9.9s wall (2026-08-21,
# CLI-to-CLI including ~1.3s of connect/probe setup), i.e. ~60 KiB/s of actual
# streaming. 512 KiB therefore buys ~8.6s of in-flight transfer, which is a wide
# margin around the 0.5-2.0s injection window below. Two orders of magnitude
# smaller than the nc arms' payload and NOT an oversight: base64 over a typed
# pty line is a fundamentally slower channel than a raw socket, and the number
# that matters is the injection window's margin, not the byte count.
_GUEST_PAYLOAD_SIZE = 512 * 1024


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


# The first chunk command's INFO echo (`Host._log_command`). Codec-specific by
# construction: `Base64Codec.send_chunks` writes each chunk as
# `printf '%s' '<b64>' | base64 -d >> <temp>`, and the anchor guest is pinned
# partly BECAUSE its userland has a real base64 applet (see
# `_bed.BUSYBOX_CHAOS_ELEMENT`) -- on the 1.16.1 guest the same transfer would
# run the uuencode codec and emit a heredoc, not this line.
_GUEST_CHUNK_MARKER = "| printf '%s'"


def _guest_names(directory: str) -> list:
    """Every name in *directory* on the guest, via the shell's own glob.

    NOT ``ls``: BusyBox ``ls`` colours its output whenever stdout is a tty,
    and every command on a ``term: telnet`` host is on one -- a bare ``ls``
    comes back wrapped in SGR escapes (measured on this guest), so a name
    comparison would silently be comparing against ``\\x1b[0;0mfoo\\x1b[m``.
    ``printf '%s\\n' <dir>/*`` is expanded by ash itself: no applet, no
    colour, one absolute path per line. An unmatched glob comes back as the
    literal pattern (ash has no nullglob), which is why the ``*`` filter is
    here rather than a directory-empty special case.
    """
    out = busybox_probe_text(f"printf '%s\\n' {directory}/*")
    return [ln.strip() for ln in out.splitlines() if ln.strip() and "*" not in ln]


@pytest.mark.no_hygiene_bracket  # the guest is not the unix pool the autouse bracket leases
def test_sigint_mid_shell_put_leaves_nothing_behind_on_the_busybox_guest(
    busybox_chaos_bed, chaos_rng, tmp_path
):
    """Interrupt a shell PUT mid-stream on the BusyBox guest: nothing may be
    left on the device -- no truncated file at the real destination, no staged
    temp beside it -- and the console must still serve a shell afterwards.

    WHY ``--transfer shell`` AND NOT ``nc``. The two nc arms above are this
    module's premise on a unix host: an interrupted transfer must not
    strand a remote ``nc -l``. That premise does not travel to the guest --
    otto REFUSES nc transfers to these guests by the registered
    ``nc_dash_n`` gap, so an nc arm here could only certify the refusal,
    which ``tests/integration/busybox_bed/test_nc_refusal.py`` already pins
    on all five guests. ``shell`` is the transfer the guests actually use,
    and it has its own remote-state question, which is what this arm asks.

    THE INVARIANT IS THE STAGING SKELETON'S, and it is genuinely at risk
    here. ``ShellFileTransfer`` names a temp in the destination's own
    directory, fills it chunk by chunk, verifies it, and only then ``mv``s it
    onto the real path -- so an interrupt is supposed to be unable to leave a
    short file where the real one goes. Every step of that runs as a separate
    typed command over the pty, and the interrupt lands between two of them,
    which is precisely the window an eager rename (or a chunk loop writing
    straight to the destination) would lose.

    THE ASSERTION IS FALSIFIABLE FROM BOTH SIDES, which is worth stating
    because "file absent" is the shape that usually is not. It cannot pass
    vacuously on a transfer that never started: ``wait_for_log`` on the first
    chunk command raises unless chunks were genuinely being dispatched. And
    it cannot pass vacuously on a transfer that FINISHED before the signal --
    that outcome leaves the destination present, which fails this same
    assertion. The payload is sized (see ``_GUEST_PAYLOAD_SIZE``) so the
    second case takes a wide margin to reach.

    THE LEFTOVER TEMP IS NOW ASSERTED TOO, and it was this arm that found it.
    The first version of this test only CHARACTERIZED what the interrupt left
    behind, printing it rather than asserting either way, because the
    product had not decided: measured here, the staged temp DID survive a
    SIGINT, since ``asyncio.CancelledError`` is a ``BaseException`` and walked
    past ``_put_one``'s ``except OSError`` without reaching any of the
    ``_cleanup_temp`` calls its error paths make. Pinning that would have
    fixed a wart as a contract; pinning its absence would have failed against
    a promise the product had not made. The decision is made now -- an
    interrupted PUT cleans up after itself
    (``ShellFileTransfer._cleanup_temp_interrupted``, shielded and bounded) --
    so the leftover is a second hard assertion rather than a printed number.

    The two assertions are NOT redundant, and neither implies the other: the
    destination check is about the temp-then-``mv`` skeleton (a file at the
    real path means the rename ran early), the leftover check is about the
    interrupt's own cleanup (a file at ``<dest>.otto-*`` means the unwind
    skipped it). The shipped defect failed the second while passing the
    first, which is exactly why both are here.

    The staged temps' SIZES are still printed rather than asserted: how many
    chunk commands landed before the signal is a property of the injection
    window, not of the product, and the number that matters is that the set
    is empty.
    """
    src = tmp_path / "guest-payload.bin"
    with src.open("wb") as f:
        f.truncate(_GUEST_PAYLOAD_SIZE)
    # Unique per run: the guest's /tmp is a tmpfs that outlives any single
    # test (only a guest restart clears it), and it already carries other
    # suites' files. A fresh directory IS this arm's hygiene bracket -- its
    # "before" is empty by construction, so anything found afterwards is
    # unambiguously this scenario's, with no snapshot/diff needed.
    remote_dir = f"/tmp/otto-chaos-guest-put-{uuid.uuid4().hex[:8]}"
    dest = f"{remote_dir}/{src.name}"
    p = None  # bound inside the try; finally must not assume it got there
    try:
        # otto's `put` never mkdir's the remote destination itself.
        busybox_probe_text(f"mkdir -p {remote_dir}")
        p = spawn_otto(
            [
                "host",
                "--transfer",
                "shell",
                busybox_chaos_bed.target.host_id,
                "put",
                str(src),
                remote_dir,
            ],
            xdir=tmp_path,
            target=busybox_chaos_bed.target,
        )
        p.wait_for_log(re.escape(_GUEST_CHUNK_MARKER), timeout=120.0)  # phase: chunks streaming
        # the ONE deliberate sleep: seeded injection offset. Floor at 0.5s
        # rather than the nc arms' 0.0: the chunk command's log line is
        # written BEFORE the command is typed, so an offset of ~0 could
        # signal with zero bytes actually landed -- a state indistinguishable
        # from "never started" in the leftover characterization below.
        time.sleep(offset_in(chaos_rng, 0.5, 2.0))
        p.signal(2)  # SIGINT
        p.wait_for_stderr(BANNER, timeout=15.0)  # phase: teardown running
        rc = p.wait(timeout=60.0)
        assert rc == 130, p.stderr_text()
        p.assert_no_process_group()

        # Guest-specific risk, asserted before anything is read off the
        # device: an interrupt lands mid-typed-command, and a console left
        # waiting at ash's PS2 continuation prompt would wedge every later
        # session. This probe is a FRESH login through the hop, so it fails
        # loud (and guest-named, via the G5 probe contract) if the console
        # did not recover -- rather than surfacing as a confusing timeout
        # inside the leftover read.
        assert "GUEST-USABLE" in busybox_probe_text("echo GUEST-USABLE"), (
            f"{busybox_chaos_bed.element}: console did not serve a clean shell after "
            "an interrupted shell PUT"
        )

        names = _guest_names(remote_dir)
        assert dest not in names, (
            f"{busybox_chaos_bed.element}: interrupted shell PUT left a file AT THE REAL "
            f"DESTINATION {dest} -- the staged-temp-then-rename skeleton did not hold. "
            f"Directory contents: {names}"
        )

        staged = [n for n in names if n.startswith(f"{dest}.otto-")]
        sizes = {n: busybox_probe_text(f"stat -c %s {n}") for n in staged}
        assert staged == [], (
            f"{busybox_chaos_bed.element}: interrupted shell PUT left its staged temp on the "
            f"device -- the interrupt's own cleanup did not run. Sizes: {sizes} "
            f"(of {_GUEST_PAYLOAD_SIZE} bytes). Directory contents: {names}"
        )

        # Characterization of the injection window only -- see the docstring.
        log_text = "\n".join(
            f.read_text(errors="replace") for f in sorted(tmp_path.rglob("verbose.log"))
        )
        print(  # noqa: T201 — characterization output is the point; captured on failure/verbose
            "partial-file policy (shell put, interrupted): "
            f"chunk-command echoes before the signal = {log_text.count(_GUEST_CHUNK_MARKER)}, "
            f"destination {dest} absent, staged temps left = {sizes} "
            f"(of {_GUEST_PAYLOAD_SIZE} bytes)"
        )
    finally:
        # Belt for the assert-failure path: `p.wait_for_log` above can raise
        # before `p` is ever signaled/reaped, leaving the local subprocess
        # running. SIGKILL it if it's still alive before doing anything else.
        if p is not None and p.proc.poll() is None:
            p.signal(9)
        # The guest's tmpfs only clears on a restart, and this test's own
        # staged temp is EXPECTED to be here -- so removing the whole unique
        # directory is the cleanup, not an admission of a leak.
        busybox_probe_text(f"rm -rf {remote_dir} || true")
