"""Guards for the BusyBox rootfs fixture — Tier 2's foundation.

Tier 1 drives applets from dash through a PATH of symlinks, which cannot prove
a GNU tool is genuinely UNREACHABLE: Tier 1 scopes PATH to one directory, but
the host filesystem underneath is untouched, so code that reaches for a GNU
tool by ABSOLUTE path finds the real one regardless (measured:
`/usr/bin/sed --version` exits 0 with PATH scoped to an unrelated,
nonexistent directory). This tier chroots into a real BusyBox-only root
instead, where `/usr/bin` does not exist at all — no route to a real GNU
tool, absolute path or otherwise.

Whether the shell itself needs PATH to find an applet (`CONFIG_FEATURE_
SH_STANDALONE`) is a separate, build-config question this tier's value does
not depend on: it is off in every busybox.net prebuilt this project fetches,
measured in `tests/busybox/test_applet_resolution.py`. Do not restate that as
a fact about "BusyBox's ash" in general — it isn't one.

The fixture is test infrastructure, so its failures are silent unless something
asserts them — a rootfs that quietly leaked the host's coreutils would make
every measurement taken inside it a lie about an environment it never built.
"""

import contextlib
import os
import signal
import subprocess
import threading
from pathlib import Path

import pytest

from tests._fixtures.busybox import (
    BUSYBOX_MATRIX,
    BusyBoxUnavailableError,
    require_interpreter,
)
from tests._fixtures.busybox_rootfs import (
    RootfsUnavailableError,
    busybox_rootfs,
    require_userns,
    run_in_rootfs,
    userns_available,
)

# The newest row: most applets, so a missing one is the fixture's fault rather
# than the artifact's. Resolution and isolation are version-independent claims.
_NEWEST = BUSYBOX_MATRIX[-1]

# `busybox` is LOAD-BEARING here even though tests/busybox/conftest.py stamps it
# by directory: that stamp depends on collection-hook order and dies with its own
# file, so the declaration is what keeps this tier out of the lanes that would
# fetch from busybox.net. Pinned by G9b in tests/unit/test_tier_marker_invariants.py.
#
# NO `skipif` HERE, deliberately. A missing interpreter is named, not skipped —
# `require_interpreter()` in tests/busybox/conftest.py's session fixture errors
# the tree with apt instructions. The collection-time skip this file first
# carried was measured at 11 skipped against Tier 1's 18 errors for the same
# missing dependency, and it also silenced the two guards below that need no
# interpreter at all.
pytestmark = [pytest.mark.busybox]

# How often the orphan guard's sampler reads /proc while a script runs, and how
# long it waits for that thread to stop. Neither discriminates anything: the
# guard asserts a process COUNT, never a duration, so a slow sample can only
# lower the peak it observed — which fails the positive control loudly rather
# than passing something through.
_SAMPLE_INTERVAL_S = 0.05
_SAMPLER_JOIN_TIMEOUT_S = 5.0


def test_the_rootfs_has_no_coreutils_to_fall_back_on():
    """The whole point of Tier 2: GNU tools must be UNREACHABLE, not just off PATH.

    Tier 1 can hide a leak because the host's `/usr/bin` is always there; code
    that shells out to a GNU tool finds it and passes. Here `/usr/bin` must not
    exist at all, so the same code fails the way it fails on a real device.

    Asserted through absolute paths, deliberately. `command -v sed` would be
    satisfied by an empty PATH — a pass that says nothing about whether the
    binary is reachable by the absolute path real code often uses.
    """
    require_userns()
    with busybox_rootfs(_NEWEST) as root:
        result = run_in_rootfs(
            root,
            "for p in /usr/bin/sed /usr/bin/env /bin/bash /usr/bin/base64; do "
            '  test -e "$p" && echo "LEAKED $p"; '
            "done; echo SWEPT",
        )

    assert result.returncode == 0, f"the probe did not run: {result.stderr}"
    assert "LEAKED" not in result.stdout, (
        f"the rootfs exposes host binaries, so nothing measured inside it is "
        f"about a BusyBox userland: {result.stdout}"
    )
    assert "SWEPT" in result.stdout, (
        "the loop must have actually run — without this, a shell that died on "
        "line one also reports no leaks"
    )


def test_the_shell_inside_is_busybox_ash_and_not_the_hosts_shell():
    """Guard against chrooting into something that still runs the host's /bin/sh."""
    require_userns()
    with busybox_rootfs(_NEWEST) as root:
        result = run_in_rootfs(root, "sh --help 2>&1 | head -1")

    assert result.returncode == 0, f"the probe did not run: {result.stderr}"
    assert f"BusyBox v{_NEWEST.version}" in result.stdout, (
        f"expected the rootfs shell to identify as BusyBox v{_NEWEST.version}, "
        f"got {result.stdout!r} — the chroot may not have taken effect"
    )


def test_the_environment_is_injected_rather_than_inherited(monkeypatch):
    """The hostile value is SET HERE, not hoped for.

    `chroot` passes the caller's environment straight through — measured: the
    dev VM's PATH (venv, vscode-server) is visible inside the rootfs, harmless
    only because those directories happen not to exist there. A fixture relying
    on that accident would silently start honouring the caller's PATH the day a
    matching directory appeared inside the root.

    So this test poisons PATH and a marker variable in the parent and demands
    the child see neither. Without the `setenv` lines it would pass on any
    machine whose ambient PATH contains nothing that exists in the rootfs —
    which is every developer's machine and CI's, making it a guard that cannot
    fail.
    """
    monkeypatch.setenv("PATH", "/nonexistent/poison:/usr/bin:/bin")
    monkeypatch.setenv("OTTO_ROOTFS_LEAK_CANARY", "leaked")

    require_userns()
    with busybox_rootfs(_NEWEST) as root:
        result = run_in_rootfs(
            root,
            'echo "PATH=$PATH"; echo "CANARY=${OTTO_ROOTFS_LEAK_CANARY:-unset}"',
        )

    assert result.returncode == 0, f"the probe did not run: {result.stderr}"
    assert "PATH=/bin" in result.stdout, (
        f"run_in_rootfs must inject PATH, not pass the caller's through: {result.stdout!r}"
    )
    assert "CANARY=unset" in result.stdout, (
        f"the caller's environment leaked into the rootfs: {result.stdout!r}"
    )


def test_dev_null_absorbs_writes_and_reads_empty_before_any_write():
    """The fixture's OWN guard against the defect this tier already shipped once.

    An earlier rootfs had no `/dev` at all, so any payload redirecting to
    `/dev/null` failed its own redirect setup — which reported the WHOLE
    compound command as failed, silently skipping the wrapped command
    entirely rather than raising anything a caller would notice. Two of
    `BashFrame`'s payloads (the handshake's `stty -echo 2>/dev/null` and
    `quiet_history()`'s `2>/dev/null`-guarded clauses) went unmeasured for an
    entire task because of exactly this, and every test built on top of
    `run_in_rootfs` passed anyway — see `test_ash_frame_payloads.py` and its
    fix-round report. If `/dev/null` ever stops being provided, THIS is the
    test that must red loudly, rather than every downstream test quietly
    measuring a payload that never ran.

    Ordered deliberately: the read comes FIRST, before anything writes to the
    file. `/dev/null` here is a plain regular file (see
    :func:`tests._fixtures.busybox_rootfs._install_dev_null`), so unlike the
    real device it does not discard on every write — a write TRUNCATES and
    overwrites it, and a read AFTER that write would see the write's own
    content, which would make this guard fail for a reason that has nothing
    to do with whether `/dev/null` exists.
    """
    require_userns()
    with busybox_rootfs(_NEWEST) as root:
        result = run_in_rootfs(
            root,
            'echo "READ=[$(cat /dev/null)]"; '
            "echo STDOUT_NOISE >/dev/null; "
            "ls /nonexistent-xyz-otto-probe 2>/dev/null; "
            "echo DONE",
        )

    assert result.returncode == 0, f"the probe did not run: {result.stderr}"
    assert "READ=[]" in result.stdout, (
        f"reading /dev/null before anything wrote to it must hit EOF "
        f"immediately, the way the real device does: {result.stdout!r}"
    )
    assert "STDOUT_NOISE" not in result.stdout, (
        f"a redirect to /dev/null must actually suppress the output, not "
        f"merely fail to error: {result.stdout!r}"
    )
    assert result.stderr == "", (
        f"`2>/dev/null` must swallow stderr, not surface it: {result.stderr!r}"
    )
    assert "DONE" in result.stdout, (
        "the script must survive both redirects and reach its last line — a "
        "rootfs with no /dev at all fails the WHOLE compound command instead"
    )


def test_a_dev_write_redirect_creates_the_target_but_a_read_of_it_still_fails():
    """The hazard two docstrings on this file got wrong, twice, before this test existed.

    `/dev` is a real, writable directory (see `_ROOTFS_DIRS`), and the chroot
    runs as uid 0 inside the mapped user namespace `busybox_rootfs` builds
    under — so a WRITE redirect to a `/dev/<name>` nobody pre-created does
    NOT fail the way `/dev/null`'s own pre-fix history might suggest. It
    SUCCEEDS, silently creating an ordinary regular file on the spot. A
    future probe written as `cmd >/dev/tty` or `cmd 2>/dev/anything`
    therefore proves NOTHING about whether a real `/dev/tty` exists in this
    rootfs — it always exits 0, whether or not the caller expected it to.
    Only a READ of a name nobody created still fails, with "No such file or
    directory" — the asymmetry this test is named for.

    `_install_dev_null`'s and `run_in_rootfs`'s docstrings both previously
    claimed the opposite: that any uncreated `/dev/<name>` "fails its own
    setup exactly the way `/dev/null` used to." That was false for writes —
    caught in review, not by this test existing first — and this test exists
    so the false version cannot quietly come back.

    Two DIFFERENT names, deliberately: `/dev/zero` for the write half and
    `/dev/urandom` for the read half, so the read failure is not explained
    away by "well, the write above touched that same file" — nothing in this
    script ever writes to `/dev/urandom`.
    """
    require_userns()
    with busybox_rootfs(_NEWEST) as root:
        result = run_in_rootfs(
            root,
            "echo x >/dev/zero; echo WRITE_RC=$?; "
            "test -f /dev/zero && echo CREATED; "
            "cat /dev/urandom; echo READ_RC=$?",
        )

    assert result.returncode == 0, f"the probe did not run: {result.stderr}"
    assert "WRITE_RC=0" in result.stdout, (
        f"a write redirect to an uncreated /dev/<name> was expected to "
        f"SUCCEED here, creating the file on the spot rather than failing "
        f"the way a missing /dev directory would: {result.stdout!r}"
    )
    assert "CREATED" in result.stdout, (
        f"the write must actually have left a regular file behind at "
        f"/dev/zero, not merely reported success: {result.stdout!r}"
    )
    assert "READ_RC=1" in result.stdout, (
        f"reading a /dev/<name> that nothing ever created or wrote to must "
        f"still fail — this is the half of the asymmetry a write-only probe "
        f"cannot see: {result.stdout!r}"
    )


def test_applet_symlinks_resolve_inside_the_root_not_outside_it():
    """`--install -s` from outside bakes absolute OUTSIDE paths; they dangle here.

    Measured: running `busybox --install -s` on the host produced 402 symlinks
    (one per applet) pointing at the builder's own path — `ls <root>/bin`
    afterward reports 403 *entries*, the 402 links plus the copied `busybox`
    binary itself, which is not a symlink — and the chroot then failed with
    `chroot: failed to run command '/bin/sh': No such file or directory`. The
    fixture installs from INSIDE the chroot so the baked path is `/bin/busybox`.

    `readlink` is checked from inside rather than on the host path, because a
    host-side `readlink` on a correct in-chroot link reports a path that does
    not resolve on the host — the assertion would have to be inverted and would
    then also accept genuinely broken links.
    """
    require_userns()
    with busybox_rootfs(_NEWEST) as root:
        result = run_in_rootfs(root, "readlink /bin/sh; test -x /bin/sh && echo EXECUTABLE")

    assert result.returncode == 0, f"the probe did not run: {result.stderr}"
    assert "busybox" in result.stdout, f"/bin/sh must link to busybox: {result.stdout!r}"
    assert "EXECUTABLE" in result.stdout, (
        "/bin/sh must be executable from inside the root; a dangling symlink "
        "reports a link target and still cannot run"
    )


@pytest.mark.parametrize("release", BUSYBOX_MATRIX, ids=[r.version for r in BUSYBOX_MATRIX])
def test_every_matrix_row_builds_a_root_running_that_rows_binary(release):
    """Including 1.16.1, which has no `--list` — the row a `--list`-based
    builder would silently skip.

    Measured: `busybox --list` exits 1 with `--list: applet not found` on
    1.16.1 and enumerates nothing, while `--install -s` works on all five rows
    (323/347/389/396/402 links). A fixture built on `--list` covers four fifths
    of the contract and reports full coverage.

    The probe asks the root which VERSION it is, not merely whether it is
    alive, and that is the difference between a parametrisation and a loop.
    An earlier version ran `echo ALIVE; ls /bin/busybox`, which is satisfied by
    ANY working root: patching the fixture to hand back the newest artifact for
    every release left all five rows green, and on CI it is worse still,
    because the job installs `busybox-static` one step earlier so
    `ls /bin/busybox` also passes with the chroot removed entirely. "The tested
    matrix is the contract" needs each row to prove it tested its own row.

    `sh --help` is the probe because
    `test_the_shell_inside_is_busybox_ash_and_not_the_hosts_shell` already
    proves it discriminates — it is how the host's dash gets caught.
    """
    require_interpreter(release.arch)
    require_userns()

    with busybox_rootfs(release) as root:
        result = run_in_rootfs(root, "sh --help 2>&1 | head -1")

    assert result.returncode == 0, (
        f"{release.version} produced no usable root: {result.stdout}{result.stderr}"
    )
    assert f"BusyBox v{release.version}" in result.stdout, (
        f"the {release.version} root reports {result.stdout.strip()!r} — the "
        f"fixture built a root from some other row's artifact, so this "
        f"parametrisation is measuring one version five times"
    )


def test_an_unavailable_userns_is_named_not_skipped():
    """The remedy has to be in the message, or the tier reads as broken.

    `unshare -r` is refused outright on a stock Ubuntu 24.04
    (`kernel.apparmor_restrict_unprivileged_userns=1`), and the raw failure —
    `write failed /proc/self/uid_map: Operation not permitted` — says nothing
    about which knob to turn. This asserts the error names both the sysctl and
    that it is provisioned, so a fresh machine is one grep from working.
    """
    error = RootfsUnavailableError.for_this_machine()

    assert "apparmor_restrict_unprivileged_userns" in str(error), (
        "the error must name the sysctl that governs this"
    )
    assert "Vagrantfile" in str(error), (
        "and must point at where the dev VM sets it, so the fix is not folklore"
    )


def _processes_rooted_in(root):
    """PIDs whose `/proc/<pid>/root` resolves inside *root* — our escapees.

    Attribution by chroot root rather than by command name, because the applets
    are called `sleep` like everything else on the machine and killing by name
    is how a test reaps someone else's process. Unreadable entries are other
    users' and are skipped: anything this test spawned, it owns.
    """
    found = []
    for entry in Path("/proc").iterdir():
        if not entry.name.isdigit():
            continue
        try:
            target = str((entry / "root").readlink())
        except OSError:
            continue
        if str(root) in target:
            found.append(f"{entry.name} -> {target}")
    return found


def test_a_timed_out_script_leaves_no_grandchildren_in_the_root():
    """A timeout must kill the process GROUP, not just the process it spawned.

    `subprocess.run(timeout=...)` signals only the direct child, and the chain
    `unshare -> chroot -> env -> sh` is one exec chain, so the direct child IS
    the shell. Anything the shell forks outlives that kill — still holding the
    rootfs open while `TemporaryDirectory` rmtrees it out from under them.

    A PIPELINE is the hostile shape, and that is measured, not assumed. The
    obvious `sleep 300 & sleep 300` proves nothing here: BusyBox ash tail-execs
    the last command, so the shell becomes the foreground `sleep` and the
    backgrounded one leaves no host process behind — that version of this test
    passed with the group kill REMOVED, i.e. it was inert. `sleep 300 | cat`
    forks both stages: 3 processes live in the root during the run and 2 of
    them survive a direct-child kill. Both stages share the shell's process
    group, because `sh -c` runs without job control, so a group kill reaches
    them and a process kill does not — which is exactly the distinction here.

    Two-second trigger, and it is not a stopwatch: nothing reads elapsed time.
    It only has to be shorter than a 300-second sleep, so machine load cannot
    flip the outcome.

    POSITIVE CONTROL, because "no survivors" is also what a run that never
    forked looks like. On a loaded emulated runner the pipeline might not have
    both stages up when the timeout fires, and then an empty survivor list
    means "nothing to kill", not "the kill worked" — green in the failure
    direction. So a sampler watches the root WHILE the script runs and the test
    fails if the hostile condition was never established on this run. It asserts
    the CONDITION (grandchildren existed), never a duration.
    """
    require_userns()
    with busybox_rootfs(_NEWEST) as root:
        # Peak occupancy during the run. The direct child is itself rooted in
        # the root, so >= 2 is the claim that something was forked BESIDE it —
        # which is exactly what a direct-child kill would have orphaned.
        peak = 0
        stop = threading.Event()

        def sample():
            nonlocal peak
            while not stop.is_set():
                peak = max(peak, len(_processes_rooted_in(root)))
                stop.wait(_SAMPLE_INTERVAL_S)

        sampler = threading.Thread(target=sample, daemon=True)
        sampler.start()
        try:
            with pytest.raises(subprocess.TimeoutExpired):
                run_in_rootfs(root, "sleep 300 | cat", timeout=2)
        finally:
            stop.set()
            sampler.join(timeout=_SAMPLER_JOIN_TIMEOUT_S)

        survivors = _processes_rooted_in(root)
        # Reaped BEFORE asserting, so a failing run cannot leave five-minute
        # sleeps on the machine — the assertion still sees what was found.
        for entry in survivors:
            with contextlib.suppress(OSError, ValueError):
                os.kill(int(entry.split()[0]), signal.SIGKILL)

    assert peak >= 2, (
        f"the hostile condition never existed on this run: peak occupancy of "
        f"the root was {peak} process(es), so the pipeline never forked a "
        f"grandchild and 'no survivors' below would prove nothing about the "
        f"group kill. Not a pass — re-run, and if it persists the shell stopped "
        f"forking for `cmd | cat` and this guard needs a new hostile shape"
    )
    assert not survivors, (
        f"a timed-out script left processes alive inside the rootfs: "
        f"{survivors}. They outlive the directory itself, which is removed on "
        f"the way out of the contextmanager"
    )


def test_a_missing_interpreter_is_named_not_skipped(tmp_path):
    """The tier's OTHER unavailability, held to the same rule as the userns one.

    This is the condition that used to be a `skipif` on this module — measured
    at 11 skipped where Tier 1 gave 18 errors for the identical missing
    dependency. The verdict now comes from `require_interpreter`, so this
    asserts what that verdict says.

    Driven through the injectable *machine* and *binfmt_root* parameters rather
    than the real ones, because on an x86_64 runner `can_run` short-circuits to
    True and every branch of this would be dead exactly where CI runs — the
    same reason `tests/_fixtures/busybox.py` made them injectable in the first
    place. *tmp_path* stands in for an empty /proc/sys/fs/binfmt_misc: a machine
    with no handlers registered at all.
    """
    with pytest.raises(BusyBoxUnavailableError) as excinfo:
        require_interpreter(machine="aarch64", binfmt_root=tmp_path)

    message = str(excinfo.value)
    assert "qemu-user-static" in message, "the error must name the package to install"
    assert "apt install" in message, "and the command, so the fix is not folklore"


def test_a_noexec_build_directory_is_named_not_a_bare_permission_denied(monkeypatch):
    """A `noexec` TMPDIR must be diagnosed, not discovered as `Permission denied`.

    Some CI images mount /tmp `noexec`. The rootfs is built under TMPDIR, so the
    first exec inside the chroot dies with a bare `Permission denied` naming
    neither the mount option nor this fixture — and every other refusal in this
    module carries its remedy, so this one should too.

    The hostile condition is INJECTED. Creating a real noexec mount needs
    privileges this tier deliberately does not have, so `os.statvfs` is patched
    to set ST_NOEXEC on the answer for the directory under test. That is the
    weaker of the two available injections and it is worth being explicit about
    what it therefore does NOT prove: that ST_NOEXEC is the right flag to read
    on a genuinely noexec mount. It does prove the branch exists, fires, and
    names the directory and the variable that chose it.
    """
    real_statvfs = os.statvfs

    def noexec_statvfs(path):
        result = real_statvfs(path)
        return os.statvfs_result(
            (
                result.f_bsize,
                result.f_frsize,
                result.f_blocks,
                result.f_bfree,
                result.f_bavail,
                result.f_files,
                result.f_ffree,
                result.f_favail,
                result.f_flag | os.ST_NOEXEC,
                result.f_namemax,
            )
        )

    # The fixture module does `import os`, so this is the object it calls
    # through; monkeypatch restores it at teardown.
    monkeypatch.setattr(os, "statvfs", noexec_statvfs)

    with pytest.raises(RootfsUnavailableError) as excinfo, busybox_rootfs(_NEWEST):
        pass  # pragma: no cover - the refusal fires before the body

    message = str(excinfo.value)
    assert "noexec" in message, "the error must name the mount option responsible"
    assert "TMPDIR" in message, "and the variable that chose the directory"
    assert "otto-bbroot-" in message, (
        f"and the directory it actually refused, not a generic '/tmp': {message!r}"
    )


def test_availability_and_the_guard_agree():
    """`require_userns` must raise exactly when `userns_available` says no.

    Two entry points for one fact drift apart silently: a `require_` that
    checks something subtly different is how a tier ends up raising on a
    machine that would have worked.

    Be honest about what this covers on any given run: only ONE branch executes,
    decided by the machine. On the dev VM and CI (both provisioned for userns)
    that is the positive branch, so the `pytest.raises` half is never exercised
    there and must not be reported as verified. The mutation that reddens the
    positive half is making `require_userns` raise unconditionally.
    """
    if userns_available():
        require_userns()  # must not raise
    else:
        with pytest.raises(RootfsUnavailableError):
            require_userns()
