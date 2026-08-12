"""Build a genuine BusyBox-only root, rootless, for the Tier 2 test tier.

Tier 1 symlinks applets into a directory and drives them from dash. That proves
argument parsing, exit codes and usage text, and it cannot prove a GNU tool is
genuinely UNREACHABLE: Tier 1 SCOPES PATH to that one directory, but the host
filesystem underneath is untouched, so a caller that reaches for a GNU tool by
ABSOLUTE path finds the real one anyway. Measured: `/usr/bin/sed --version`
exits 0 with PATH scoped to an unrelated, nonexistent directory — PATH search
never runs, because the caller never asked it to. This tier closes that gap
with a real root where `/usr/bin` does not exist, full stop — no route to a
real GNU tool, absolute path or otherwise. `run_in_rootfs`'s own docstring
carries the sharpest version of this: `/usr/bin/env` itself, invoked by its
usual absolute path, fails inside this root — the one case Tier 1 structurally
cannot reproduce no matter how it scopes PATH.

Applet RESOLUTION inside ash — whether `sh` can find `ls` with PATH emptied —
is a separate, BUILD-CONFIG question this tier's value does NOT rest on:
that's `CONFIG_FEATURE_SH_STANDALONE`, off in BusyBox's own `defconfig` and
measured off across all five busybox.net prebuilts this project fetches (see
`tests/busybox/test_applet_resolution.py`'s `_EXPECTED_STANDALONE_SHELL`
table — Ubuntu's system busybox measures the opposite). An earlier version of
this rationale stated that resolution as a fact about "BusyBox's ash" in
general; it is a fact about one build option, not about ash.

This module builds that root with: `unshare -r` for a user namespace where we
are uid 0, then `chroot` into a directory holding nothing but one BusyBox
binary and its applet symlinks. No sudo, no docker, no system state.

Three mechanics here were measured rather than assumed, and each has a comment
at its site: how applets are enumerated, where `--install -s` is run from, and
how the child's environment is set.

Every refusal in this module raises :class:`RootfsUnavailableError` with the
remedy in the message. Nothing here skips: a skipped BusyBox tier and a passing
one are the same line in a summary, which is how this coverage would evaporate.
"""

import contextlib
import os
import shutil
import signal
import subprocess
import tempfile
from collections.abc import Iterator
from pathlib import Path

from .busybox import BusyBoxRelease, busybox_binary

# /bin gets applet symlinks and /dev gets exactly one file (see
# `_install_dev_null`); nothing else is populated — no `/tmp` either, since no
# script this tier runs writes to one (checked: no `run_in_rootfs` payload
# anywhere under tests/busybox/ references `mktemp` or `/tmp`; an applet that
# defaulted TMPDIR-less to `/tmp` would simply fail the way it fails on any
# path this root doesn't have). A BusyBox device's `/usr/bin` is typically a
# symlink or absent, and leaving it absent is the point of the tier: code
# that shells out to /usr/bin/<gnu tool> must fail here the way it fails on
# the device.
_ROOTFS_DIRS = ("bin", "dev")

# The environment the rootfs shell gets. INJECTED, never inherited: `chroot`
# passes the caller's environment straight through, and the dev VM's PATH
# (a venv, a vscode-server dir) is visible inside the root — harmless only
# because those paths happen not to exist there. Relying on that accident means
# the fixture starts honouring the caller's PATH the day a matching directory
# appears inside the root.
_ROOTFS_PATH = "/bin"

# The PATH the HOST side of every call gets — the layer that has to find
# `unshare` and `chroot` themselves. Declared rather than inherited, and that is
# a measured requirement, not symmetry for its own sake: `chroot` is at
# /usr/sbin/chroot, a directory plenty of interactive PATHs omit, so inheriting
# the caller's PATH made the fixture die with `unshare: failed to execute
# chroot: No such file or directory` (rc=127) the moment a caller's PATH lacked
# it — which the environment-injection guard does deliberately. The rootfs
# shell's own PATH is _ROOTFS_PATH and has nothing to do with this one.
_HOST_PATH = "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"

# ── THE FOUR NUMBERS BELOW ARE COUPLED, to a budget this module does not own ──
#
# Building one root and probing it costs, in the worst case:
#
#     busybox_binary()'s cold-cache fetch     60s  (tests/_fixtures/busybox.py)
#   + require_userns()'s probe                _USERNS_PROBE_TIMEOUT_S + reap
#   + _install_applets()                      _BUILD_TIMEOUT_S        + reap
#   + _RUNS_PER_TEST_BUDGETED x run_in_rootfs _RUN_TIMEOUT_S          + reap
#
# where "reap" is _REAP_TIMEOUT_S, which only elapses on a call that has
# ALREADY timed out — but a budget that ignores it is a budget that is wrong
# exactly when the timeout path it protects is the one being taken.
#
# against pyproject's `timeout = 180` with `timeout_func_only = true`. Let that
# sum approach 180 and a wedged qemu or `unshare` is SIGALRM'd as a bare
# "Timeout >180.0s", so the caller never sees the named error this whole module
# exists to raise — the identical diagnostic collapse that
# `test_the_fetch_budget_fits_inside_both_timeouts` was written to stop on the
# fetch path. The fetch term is not pathological here either: CI runs this tier
# on a DELIBERATELY cold cache, so 60s is its ORDINARY worst case.
#
# One 60s constant spent at every site summed to ~300s and had already broken
# this. Each site now carries its own bound, sized to what that site does.
# Pinned by `test_the_rootfs_budget_fits_inside_the_per_test_timeout` in
# tests/unit/host/test_busybox_artifacts.py.
#
# Be precise about what that guard binds, because it is NOT a tripwire on any
# one constant: it is the SUM above, times 1.25 headroom, against 180. Today
# that sum is 135s (144s is the most it may reach), so each constant carries
# whatever slack is left once the others are held fixed — measured, raising
# `_USERNS_PROBE_TIMEOUT_S` alone from 10.0 to 14.0 leaves the sum at 139s
# (139 * 1.25 = 173.75 <= 180) and the guard PASSES. `_REAP_TIMEOUT_S` is the
# tightest of the four regardless — it is charged once per bounded call
# (three sites, one of them x `_RUNS_PER_TEST_BUDGETED`), so a rise there
# costs the sum four times over and has the least room to move. Raise enough
# of one constant, or several together, and it reds; raising any one of them
# by an amount smaller than its current slack does not.
#
# All four are RUNAWAY GUARDS, never discriminators: no assertion anywhere in
# this tier reads elapsed time, so each is as generous as the remaining
# budget allows and tightening one below what its own site needs buys
# nothing but red builds on a loaded host.

_USERNS_PROBE_TIMEOUT_S = 10.0
"""Bound for `unshare -r id -u`. One fork+exec of a tiny host binary; measured
in single-digit milliseconds, so 10s is already three orders of magnitude of
slack for a contended runner."""

_BUILD_TIMEOUT_S = 15.0
"""Bound for the in-chroot `busybox --install -s /bin`, the one call that writes
the ~400 applet symlinks. Measured at well under a second per row, emulated."""

_RUN_TIMEOUT_S = 15.0
"""Default bound for one :func:`run_in_rootfs` script. Callers running something
genuinely long pass their own ``timeout`` — and if they do, they are spending
from the same 180s per-test budget described above."""

_REAP_TIMEOUT_S = 5.0
"""How long to wait for output after SIGKILLing a timed-out process group.

Not a formality. `communicate()` reads to EOF, and EOF needs every holder of
the pipe to be gone — including grandchildren. Killing the group is what makes
that terminate, so this bound is the backstop for the case where the kill did
not reach something: without it, the timeout path itself blocks for as long as
the orphan lives, which on a `sleep 300` is five minutes inside a 180s test."""

_RUNS_PER_TEST_BUDGETED = 2
"""How many :func:`run_in_rootfs` calls one test may make and still fit the
budget. Not enforced at runtime — it is the assumption the arithmetic above is
built on, so a later tier that needs more scripts per test has to revisit the
sum rather than discover it as a bare timeout."""


def _host_env() -> "dict[str, str]":
    """The environment the host-side `unshare`/`chroot` run with.

    The caller's variables are kept — nothing here needs them scrubbed, and the
    in-chroot `env -i` scrubs the ones that would reach the rootfs shell anyway
    — but PATH is REPLACED, for the reason recorded at :data:`_HOST_PATH`.
    """
    return {**os.environ, "PATH": _HOST_PATH}


def _run_host(argv: "list[str]", timeout: float) -> "subprocess.CompletedProcess[str]":
    """Run a host-side *argv*, killing the whole process GROUP on a timeout.

    `subprocess.run(timeout=...)` kills only the direct child. The chain here is
    `unshare -> chroot -> env -> sh`, four execs of one process today, so
    nothing survives that kill right now. It stops being true the moment a
    script pipes or backgrounds anything: those grandchildren outlive the
    timeout still holding the rootfs open, while :class:`TemporaryDirectory`
    rmtrees it out from under them on the way out of the contextmanager.

    So the child is made a session leader — which makes its pid its
    process-group id — and the timeout path signals the group, not the process.
    Written now rather than after the first flake, because the shape that
    triggers it is exactly the shape the later tiers add.
    """
    with subprocess.Popen(
        argv,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=_host_env(),
        start_new_session=True,
    ) as proc:
        try:
            stdout, stderr = proc.communicate(timeout=timeout)
        except subprocess.TimeoutExpired:
            # Suppressed rather than handled: the group is already gone if the
            # leader reaped its own children, and that is a success, not a
            # condition worth reporting over the timeout we are about to raise.
            with contextlib.suppress(ProcessLookupError, PermissionError):
                os.killpg(proc.pid, signal.SIGKILL)
            try:
                stdout, stderr = proc.communicate(timeout=_REAP_TIMEOUT_S)
            except subprocess.TimeoutExpired:
                # Something outlived the group kill and still holds the pipe.
                # Report the timeout we came here for rather than blocking on
                # it — the caller's diagnosis is the original timeout, not this.
                proc.kill()
                stdout, stderr = "", ""
            raise subprocess.TimeoutExpired(argv, timeout, output=stdout, stderr=stderr) from None
        return subprocess.CompletedProcess(argv, proc.returncode, stdout, stderr)


class RootfsUnavailableError(RuntimeError):
    """The rootfs tier cannot build a root here, and says why in the message."""

    @classmethod
    def for_this_machine(cls) -> "RootfsUnavailableError":
        """The message a developer on a stock Ubuntu 24.04 actually needs.

        The raw kernel refusal is `write failed /proc/self/uid_map: Operation
        not permitted`, which names neither the setting responsible nor the fix.
        Ubuntu 24.04 ships `kernel.apparmor_restrict_unprivileged_userns=1`,
        which permits CREATING a user namespace but denies the uid_map write
        that maps you to root inside it — so there is no CAP_SYS_CHROOT and no
        chroot.
        """
        return cls(
            "cannot create a mapped user namespace, so the BusyBox rootfs tier "
            "cannot build a root. On Ubuntu 24.04 this is "
            "kernel.apparmor_restrict_unprivileged_userns=1: creating a "
            "namespace is allowed but the /proc/self/uid_map write that maps "
            "you to uid 0 is denied, leaving no CAP_SYS_CHROOT. The dev VM "
            "sets it to 0 from the Vagrantfile dev-root provisioner "
            "(/etc/sysctl.d/99-otto-unprivileged-userns.conf); apply the same "
            "sysctl here, or re-provision."
        )


def userns_available() -> bool:
    """Whether `unshare -r` can map this user to uid 0.

    Asked by DOING it, not by reading the sysctl. The sysctl is one of several
    reasons the map can fail (seccomp, a container without the capability, a
    kernel built without user namespaces), and a check that reads only the
    Ubuntu-specific knob reports "available" on every machine that fails for
    one of the others.

    Probed under the same injected PATH the real calls use, so this cannot
    answer "unavailable" for a reason the real calls would not have hit — a
    caller whose own PATH is missing /usr/sbin would otherwise be told the
    kernel refused it, and sent to change a sysctl that is already correct.
    """
    if shutil.which("unshare", path=_HOST_PATH) is None:
        return False
    try:
        result = _run_host(["unshare", "-r", "id", "-u"], _USERNS_PROBE_TIMEOUT_S)
    except (OSError, subprocess.SubprocessError):
        return False
    return result.returncode == 0 and result.stdout.strip() == "0"


def require_userns() -> None:
    """Raise :class:`RootfsUnavailableError` unless a mapped namespace works.

    Never skips. A skipped BusyBox tier is indistinguishable from a passing one
    in a summary line, which is how this coverage would quietly evaporate.
    """
    if not userns_available():
        raise RootfsUnavailableError.for_this_machine()


def _require_exec_mount(root: Path) -> None:
    """Refuse a `noexec` build directory BY NAME, before anything tries to exec.

    The root is built under TMPDIR, and some CI images mount /tmp `noexec`. The
    first exec inside the chroot then fails with a bare `Permission denied`
    that names neither the mount option nor this fixture, leaving the reader no
    thread to pull — the exact failure shape every other refusal here exists to
    avoid.

    Asked of the directory actually being built in rather than of `/tmp`,
    because TMPDIR may point anywhere and the answer is per-filesystem.
    """
    if os.statvfs(root).f_flag & os.ST_NOEXEC:
        raise RootfsUnavailableError(
            f"the BusyBox rootfs build directory {root} is on a filesystem "
            f"mounted `noexec`, so the binary copied into it cannot be "
            f"executed and the chroot would fail with a bare 'Permission "
            f"denied'. This directory comes from TMPDIR (currently "
            f"{os.environ.get('TMPDIR', '/tmp')!r}); point TMPDIR at a "
            f"filesystem mounted exec, or remount this one."
        )


@contextlib.contextmanager
def busybox_rootfs(release: BusyBoxRelease) -> Iterator[Path]:
    """Yield a directory holding a BusyBox-only root for *release*.

    Built under the caller's own uid; only the chroot needs the namespace. The
    directory is removed on the way out.

    Does NOT probe the namespace itself — callers gate with
    :func:`require_userns` first, and the probe costs from the same per-test
    timeout budget documented at the top of this module, so spending it twice
    per test bought nothing. A caller who skips that gate still fails named
    rather than raw: :func:`_install_applets` raises on the chroot's own
    non-zero exit.

    The `noexec` check comes before :func:`busybox_binary` deliberately, so an
    unusable TMPDIR is reported in milliseconds rather than after a cold-cache
    fetch has been spent on a root that could never have run.
    """
    with contextlib.ExitStack() as stack:
        root = Path(stack.enter_context(tempfile.TemporaryDirectory(prefix="otto-bbroot-")))
        _require_exec_mount(root)

        binary = busybox_binary(release)
        for name in _ROOTFS_DIRS:
            (root / name).mkdir(parents=True, exist_ok=True)

        target = root / "bin" / "busybox"
        shutil.copy2(binary, target)
        target.chmod(0o755)

        _install_applets(root)
        _install_dev_null(root)
        yield root


def _install_applets(root: Path) -> None:
    """Populate `<root>/bin` with applet symlinks, from INSIDE the chroot.

    Two measured choices.

    `--install -s` rather than `--list`: 1.16.1 has no `--list` at all (it exits
    1 with "--list: applet not found" and enumerates nothing), while
    `--install -s` works on every row in the matrix. An enumerator built on
    `--list` covers four of the five versions and reports success.

    Run from inside the chroot rather than on the host: `--install -s` bakes the
    absolute path busybox was invoked through. Invoked as
    `<root>/bin/busybox` on the host it writes 402 symlinks — one per applet,
    matching `--list`'s own count — to a path that does not exist inside the
    new root (`ls <root>/bin` afterward reports 403 *entries*: the 402 links
    plus the copied `busybox` binary itself, which is not a symlink and is not
    part of either count above), and the chroot then fails with
    "failed to run command '/bin/sh': No such file or directory". Invoked as
    `/bin/busybox` after chrooting, it bakes `/bin/busybox` — correct on 1.16.1,
    1.21.1 and 1.35.0 alike, with no /proc mounted.
    """
    result = _run_host(
        ["unshare", "-r", "chroot", str(root), "/bin/busybox", "--install", "-s", "/bin"],
        _BUILD_TIMEOUT_S,
    )
    if result.returncode != 0:
        raise RootfsUnavailableError(
            f"could not install BusyBox applets into {root}: "
            f"rc={result.returncode} {result.stdout}{result.stderr}"
        )


def _install_dev_null(root: Path) -> None:
    """Create `/dev/null` as a plain REGULAR FILE — no `mknod`, no privilege.

    Not a device node: this tier is rootless and does not have CAP_MKNOD, so a
    real character-special `/dev/null` is not on the table. It does not need
    to be one, either. `mkdir -p /dev; : > /dev/null` is enough, because a
    redirect only asks the file for two things and a regular file gives both:
    a write succeeds, and a read that starts from an untouched file hits EOF
    immediately, same as the real device.

    Writes are CAPTURED, not discarded, and that has one consequence worth
    knowing before relying on this file for anything beyond a redirect target:
    each `>/dev/null` or `2>/dev/null` TRUNCATES and overwrites it (regular
    `open(..., "w")` semantics), so a read that happens AFTER an earlier write
    in the same rootfs sees that write's content, not emptiness — unlike the
    real device, which discards on every write regardless of history. Measured
    directly: `echo A >/dev/null; echo "[$(cat /dev/null)]"` prints `[A]` here.
    Harmless for every payload this tier actually measures (none of them read
    `/dev/null` back after writing to it — they only ever redirect INTO it),
    but a future script that both writes and reads it in one run needs to know
    this is not a true sink.

    ONLY `null` is PRE-CREATED — but that does not mean the rest of `/dev` is
    closed the way it used to be. `/dev` is now a real, writable directory,
    and the chroot runs as uid 0 inside the mapped user namespace, so a WRITE
    redirect to any OTHER `/dev/<name>` silently SUCCEEDS by creating an
    ordinary regular file there on the spot: measured, `echo x >/dev/zero`
    and `echo y >/dev/tty` both exit 0 and leave `zero` and `tty` sitting in
    `/dev` afterward (`ls /dev` then reports `null tty zero`), proving
    nothing about whether a real `/dev/zero` or `/dev/tty` was ever there.
    Only a READ of a name nobody created still fails: measured,
    `cat /dev/urandom` exits 1 with "No such file or directory". A probe
    written as `cmd >/dev/tty` therefore CANNOT distinguish "the target
    exists" from "the target doesn't, and the redirect just created it" —
    only a read-based probe can. Pinned by
    `test_a_dev_write_redirect_creates_the_target_but_a_read_of_it_still_fails`
    in `tests/busybox/test_rootfs_fixture.py`.
    """
    (root / "dev" / "null").touch()


def run_in_rootfs(
    root: Path, script: str, timeout: float = _RUN_TIMEOUT_S
) -> "subprocess.CompletedProcess[str]":
    """Run *script* under the rootfs's own ash, with an injected environment.

    THE ROOT HAS `/dev/null` — ONLY `/dev/null` — see :data:`_ROOTFS_DIRS` and
    :func:`_install_dev_null`. It is a plain regular file, not a device node
    (this tier is rootless and has no `mknod` privilege), and that is enough:
    a redirect only needs writes to succeed and reads to hit EOF, and a
    regular file gives both, so `>/dev/null`, `2>/dev/null` and `</dev/null`
    all behave the way they do on a real device. This used to be false — an
    earlier version of this root had no `/dev` at all, and every one of those
    redirects failed its own setup step ("can't create /dev/null: nonexistent
    directory"), which reported the WHOLE compound command as failed
    regardless of what the wrapped command was. That silently starved two of
    `BashFrame`'s payloads (the handshake's `stty -echo 2>/dev/null` and
    `quiet_history()`'s `2>/dev/null`-guarded clauses) of ever actually
    running under a Tier 2 measurement that believed it had run them — see
    `tests/busybox/test_ash_frame_payloads.py` and its fix-round report for
    the concrete defect this caused.

    EVERYTHING ELSE under `/dev` is UNPOPULATED, not UNREACHABLE — and that
    distinction is the trap. `/dev` is a real, writable directory now, and
    the chroot's uid 0 (mapped by the user namespace) can write anywhere in
    it, so a WRITE redirect to a `/dev/<name>` nobody pre-created —
    `>/dev/zero`, `>/dev/tty`, `2>/dev/anything` — SUCCEEDS and CREATES an
    ordinary regular file there, exit 0, no error at all. This is the
    OPPOSITE of `/dev/null`'s own old failure mode, where an entirely
    missing `/dev` directory made every redirect fail its SETUP step before
    the wrapped command ran — do not conflate the two. Only a READ of a name
    nobody created still fails ("No such file or directory"), so a caller
    that needs to prove a `/dev/<name>` genuinely exists must READ it, never
    write to it; a caller that needs one pre-created for real has to add it
    the way `_install_dev_null` adds `null`. Pinned by
    `test_a_dev_write_redirect_creates_the_target_but_a_read_of_it_still_fails`
    in `tests/busybox/test_rootfs_fixture.py`. `/dev/null` itself is a
    regular file that CAPTURES writes rather than a device that discards
    them — see :func:`_install_dev_null` for the one consequence that
    matters there (a read after an earlier write in the same rootfs sees
    that write's content, not emptiness).

    `env -i` is BusyBox's own applet at `/bin/env`, not `/usr/bin/env`: the
    rootfs has no `/usr/bin`, so reaching for the host's usual path fails with
    "failed to run command '/usr/bin/env'" — the harness must live by the same
    rule it is testing.

    There are TWO environments in play here and they are not redundant, however
    much the argv below reads that way. The host-side `env=` (see
    :func:`_host_env`) belongs to `unshare` and `chroot`, host binaries found by
    execvp, so a PATH that omits /usr/sbin kills the call with rc=127 before any
    namespace exists (measured). `/bin/env -i` runs one layer further in, after
    the chroot has taken effect, and is what the rootfs SHELL sees: it discards
    everything inherited across the chroot boundary and hands ash the single
    PATH this module declares. Collapsing either one into the other silently
    changes which side of the boundary is scrubbed.
    """
    return _run_host(
        [
            "unshare",
            "-r",
            "chroot",
            str(root),
            "/bin/env",
            "-i",
            f"PATH={_ROOTFS_PATH}",
            "/bin/sh",
            "-c",
            script,
        ],
        timeout,
    )
