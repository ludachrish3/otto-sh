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

# /bin gets applet symlinks, /dev gets exactly one file (see
# `_install_dev_null`), and /tmp is an ordinary empty, writable directory —
# nothing else is populated.
#
# `tmp` was dropped from this tuple in phase 3's final review as unused, and
# the first consumer arrived exactly one phase later: the `shell` transfer
# backend stages a temp file on the target host and `mv`s it into place once
# the write completes, so a root with no writable temp directory cannot
# exercise that backend's central safety property (no reader ever sees a
# partial file). The pre-planning probe for that phase failed with
# `can't create /tmp/...: nonexistent directory` — which is the SAME
# silent-corruption shape `/dev/null`'s earlier absence had: a WRITE REDIRECT
# (`>/tmp/...`) whose own `open()` fails to a missing directory reports the
# WHOLE wrapped command as failed, not "the temp dir is missing", so a caller
# that only checks the exit code learns nothing about which line lost. See
# `run_in_rootfs`'s docstring for the `/dev/null` half of that history.
#
# NOT the same MECHANISM as a write redirect's setup failure, though an
# earlier version of this comment drew the contrast wrong twice over — first
# by claiming `mktemp` shares the redirect's failure shape at all, then by
# claiming it differs by NOT swallowing a `;`-chain. Measured, with no `/tmp`
# present: `echo hi >/tmp/x; echo AFTER` and `mktemp; echo AFTER` both print
# `AFTER` and exit 0 — `;` never looks at the exit status of what came
# before it, for either one, and under `&&` both alike skip the tail. There
# is no chain-propagation difference between the two.
#
# What genuinely differs is WHERE the failure happens. A write redirect's
# `open()` runs BEFORE the command it is attached to even starts, so a
# failure there means that command is never attempted at all — which is what
# "the WHOLE wrapped command" means above and in `run_in_rootfs`'s own
# docstring: not a chain of several commands, but the ONE command plus its
# own redirect, failing as a unit because the shell refuses to start it.
# `mktemp` invoked bare has no such redirect — it DOES run, and fails on its
# OWN internal open() instead, surfacing its own diagnostic
# (`mktemp: (null): No such file or directory`, rc=1) rather than ash's
# `can't create ...: nonexistent directory`.
#
# A BusyBox device's `/usr/bin` is typically a symlink or absent, and leaving
# it absent is the point of the tier: code that shells out to
# /usr/bin/<gnu tool> must fail here the way it fails on the device.
_ROOTFS_DIRS = ("bin", "dev", "tmp")

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
#   + _install_applets()'s exec proof         _PROOF_TIMEOUT_S        + reap
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
# that sum is 140s (144s is the most it may reach), so each constant carries
# whatever slack is left once the others are held fixed — measured, raising
# `_USERNS_PROBE_TIMEOUT_S` alone from 5.0 to 8.0 leaves the sum at 143s
# (143 * 1.25 = 178.75 <= 180) and the guard PASSES. `_REAP_TIMEOUT_S` is the
# tightest of the five regardless — it is charged once per bounded call
# (four sites, one of them x `_RUNS_PER_TEST_BUDGETED`), so a rise there
# costs the sum five times over and has the least room to move. Raise enough
# of one constant, or several together, and it reds; raising any one of them
# by an amount smaller than its current slack does not.
#
# Adding the exec proof cost 10s of a budget that had 9s spare, which is why
# `_USERNS_PROBE_TIMEOUT_S` was halved in the same commit. A new bounded call
# is never free here: it must arrive with the room for it, taken from a term
# that can spare it.
#
# All four are RUNAWAY GUARDS, never discriminators: no assertion anywhere in
# this tier reads elapsed time, so each is as generous as the remaining
# budget allows and tightening one below what its own site needs buys
# nothing but red builds on a loaded host.
#
# ── TIER 3 SPENDS FROM A DIFFERENT WINDOW, and that is why it is not a term ──
#
# `tests/_fixtures/busybox_dropbear.py` adds bounded calls of its own — one
# `_KEYGEN_TIMEOUT_S` per key (three keys), one `_READY_TIMEOUT_S` for the
# daemon's bind wait, and up to two `_STOP_TIMEOUT_S` for its two-stage reap
# — on top of a root built through THIS module. None of them joins the sum
# above, and the reason is structural rather than a rounding call: Tier 3
# builds all of it in the SESSION-scoped `tier3_dropbear` fixture, while the
# sum above bounds what one TEST BODY spends. pyproject sets
# `timeout_func_only = true`, so the per-test SIGALRM this arithmetic protects
# covers the CALL phase only; session setup is bounded by
# `faulthandler_timeout = 300` instead, and the arithmetic there is
# 3 x 30 + 15 + 2 x 10 = 125s of Tier 3 bounds plus this module's own
# probe + build + proof (with reaps, 40s) and a cold-cache fetch (60s) —
# 225s against 300, which fits and has 75s of room. Spend that room and Tier
# 3's named refusals are replaced by a faulthandler dump, the same collapse
# the sum above exists to prevent one window over.
#
# What a Tier 3 test spends in its own CALL phase is ssh round trips against
# an already-running daemon: 0.12s measured for an exec round trip, bounded
# by nothing but the 180s SIGALRM, which is the right instrument for a
# transport with no wedge of its own to name.
#
# Measured 2026-08-13 on this VM, warm: keys 0.01s each (RSA 0.20s), rootfs
# 0.03s, `start()` 0.05s, exec round trip 0.12s, `stop()` 0.00s. Every bound
# above is therefore two to three orders of magnitude of slack, which is what
# a runaway guard should be.
#
# A SECOND TIER 3 DAEMON IS NOT FREE IN THE SAME WAY. Three tests start their
# own — the reap guard in `test_tier3_harness.py`, and the two guards in
# `test_tier3_session.py` that INJECT a hostile condition (no chroot wrapper,
# no mount namespace) rather than observe the ambient one — and each `start()`
# lands in a CALL phase, so it is charged against the 180s per-test timeout
# like any other in-test work.
#
# That budget is PER TEST, not shared, so three of them do not sum: the most
# expensive body is one `_READY_TIMEOUT_S` (15s) plus two of that module's
# `_CLIENT_TIMEOUT_S` (30s each) for the scp and sftp calls, i.e. 75s of the
# 180 in the worst case and well under a second warm. What would need this
# arithmetic revisited is a single test that started several, or one that
# added enough bounded calls beside a start to approach 180 — at which point
# the named refusals collapse into a bare `Timeout >180.0s`, the same failure
# the sum above exists to prevent one window over.
#
# ── `test_tier3_shell_transfer.py` SPENDS UNDER THE PRODUCT'S BOUNDS, NOT OURS ─
#
# That module drives otto's own `UnixHost.put`/`get` against the session
# daemon. It starts no daemon and adds no constant to this file, but its
# per-test worst case is bounded by numbers this tree does not own and cannot
# pass: `Userland.resolve()`'s `_RESOLVE_BUDGET_S` (30s, charged once per host)
# plus `DEFAULT_COMMAND_TIMEOUT` (30s) for each exec the transfer issues — and
# a three-chunk PUT issues five (three chunks, one `md5sum`, one `mv`). 30 +
# 5 x 30 = 180s, i.e. exactly at the per-test SIGALRM with nothing to spare,
# and a GET in the same body pushes past it. `put` and `get` take no timeout
# argument, so this is not tunable from the test side.
#
# It is recorded rather than fixed because of WHICH failure it degrades. A
# dead or refusing daemon fails in milliseconds; only a WEDGED one (a device
# that accepts a command and never answers) walks the full budget, and that is
# the one shape a loopback daemon on this machine does not produce. Measured
# warm: ~0.15s for the whole three-chunk transfer. If a future test in that
# module adds a second host or a fourth direction, it needs this paragraph
# revisited first — the collapse into a bare `Timeout >180.0s` is the same one
# the sum above exists to prevent.

_USERNS_PROBE_TIMEOUT_S = 5.0
"""Bound for `unshare -r id -u`. One fork+exec of a tiny host binary; measured
in single-digit milliseconds, so 5s is already nearly three orders of magnitude
of slack for a contended runner.

Was 10.0. Halved to pay for :data:`_PROOF_TIMEOUT_S` without pushing the
coupled sum above what the per-test timeout allows — see the block above. The
budget is the reason, and it is a real one: this term and the proof's are the
two cheapest in the sum, so they are where the room came from."""

_PROOF_TIMEOUT_S = 5.0
"""Bound for the post-install `/bin/sh -c :` inside the new root.

One exec of a static binary through a freshly written symlink, on the same
order as the userns probe. It buys the difference between "thirteen tests
report the harness's argv" and "one named error reports the install" — see
:func:`_install_applets`."""

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

    The caller's variables are kept and PATH is REPLACED, for the reason
    recorded at :data:`_HOST_PATH`.

    This is NOT what the rootfs shell sees. :func:`run_in_rootfs` passes its
    own scrubbed environment instead of using this one, so nothing here
    crosses the chroot boundary into a measurement. The remaining callers are
    the ones that only ever exec host binaries — the `unshare` probe and
    `--install -s` — where the caller's variables are harmless.
    """
    return {**os.environ, "PATH": _HOST_PATH}


def _host_tool(name: str) -> str:
    """Absolute path to a host binary this tier execs.

    Resolved here so the caller can hand the child a SCRUBBED environment
    without also losing the ability to find `unshare` and `chroot`. Those two
    are looked up through :data:`_HOST_PATH` (`chroot` lives in /usr/sbin,
    which a login PATH often omits), and once resolved they no longer depend
    on the PATH the child is given — which is what lets
    :func:`run_in_rootfs` pass the rootfs's own PATH straight through instead
    of correcting it from inside the chroot with an applet.
    """
    found = shutil.which(name, path=_HOST_PATH)
    if found is None:
        raise RootfsUnavailableError(
            f"the rootfs tier needs `{name}` on the host and it is not on {_HOST_PATH}"
        )
    return found


def _run_host(
    argv: "list[str]", timeout: float, env: "dict[str, str] | None" = None
) -> "subprocess.CompletedProcess[str]":
    """Run a host-side *argv*, killing the whole process GROUP on a timeout.

    `subprocess.run(timeout=...)` kills only the direct child. The chain here is
    `unshare -> chroot -> sh`, three execs of one process today, so
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
        env=_host_env() if env is None else env,
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

    # `--install -s` EXITING 0 IS NOT EVIDENCE THAT IT INSTALLED ANYTHING.
    #
    # This is not defensive padding; it is the fix for a real CI failure that
    # this tier reported as thirteen unrelated ones (issue #227). On 1.16.1 the
    # install returned 0 and the root came out without the applet the harness
    # itself then reached for, so every downstream test failed with
    # "chroot: failed to run command ...: No such file or directory" — a message
    # about the harness's own argv, naming nothing about the install that
    # actually went wrong. One silent success became thirteen misleading
    # diagnoses, none of them pointing here.
    #
    # So the postcondition is asserted where it is created. `sh` is the only
    # applet the harness cannot do without (`run_in_rootfs` execs it directly),
    # and the count travels with the error because "installed 1 applet" and
    # "installed 300 but not this one" are different bugs with the same symptom.
    # EVERY LINK IS REPOINTED AT `/bin/busybox`, rather than trusted.
    #
    # `--install -s` bakes a target derived from how busybox was invoked, and
    # what it derives is not portable: measured identical (`/bin/busybox`) on
    # all five rows under qemu-i386 here, yet 1.16.1 came out of a native
    # x86_64 runner with a `sh` link that existed and did not resolve — execve
    # gave ENOENT for the TARGET while the link itself was present, which is
    # why the first version of this postcondition (`is_symlink()`) passed and
    # let thirteen tests fail downstream instead. `/bin/busybox` is the path
    # this function copied the binary to, so writing it explicitly makes the
    # link set correct by construction on every platform rather than correct
    # by coincidence on one.
    links = [p for p in (root / "bin").iterdir() if p.is_symlink()]
    for link in links:
        link.unlink()
        link.symlink_to("/bin/busybox")

    # THE POSTCONDITION IS EXECUTED, NOT INSPECTED.
    #
    # A symlink's existence is not the property this tier needs; running a
    # command through it is. `is_symlink()` was a proxy for that and it was
    # the wrong one, so the check now spends one exec proving the thing it
    # claims. The error carries the state a reader would otherwise have to
    # ask CI for — count, target, and whether the target is really there —
    # because this failure has already cost two round trips for want of it.
    proof = _run_host(
        [_host_tool("unshare"), "-r", _host_tool("chroot"), str(root), "/bin/sh", "-c", ":"],
        _PROOF_TIMEOUT_S,
        env={"PATH": _ROOTFS_PATH},
    )
    if proof.returncode != 0:
        sh = root / "bin" / "sh"
        target = str(sh.readlink()) if sh.is_symlink() else "(not a symlink)"
        busybox = root / "bin" / "busybox"
        raise RootfsUnavailableError(
            f"the root at {root} installed {len(links)} applet symlink(s) and "
            f"`--install -s` exited 0, but /bin/sh will not run: rc={proof.returncode} "
            f"{proof.stdout}{proof.stderr}".rstrip()
            + f" | /bin/sh -> {target} | /bin/busybox present={busybox.exists()} "
            f"size={busybox.stat().st_size if busybox.exists() else 0}"
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

    THE CHAIN EXECS NO APPLET BEFORE `sh`, and that is a fix, not a
    simplification. It used to run `/bin/env -i PATH=... /bin/sh`, scrubbing
    the environment one layer inside the chroot with BusyBox's own `env`
    applet. That made the harness depend on an applet being installed in order
    to find out which applets are installed, and it broke exactly where a
    circular dependency like that is worst: on CI, 1.16.1's root came out with
    no `/bin/env`, and all thirteen of its Tier 2 tests failed with
    "chroot: failed to run command '/bin/env'" — thirteen reports about the
    harness's own argv, none about the artifact (issue #227). The dev VM never
    saw it, because the i686 rows run under qemu-i386 here and natively on an
    x86_64 runner.

    The scrub still happens; it just happens on the side of the boundary that
    needs no applet. `chroot` does not alter the environment, so what ash sees
    IS the child's environment — passing `{"PATH": _ROOTFS_PATH}` to the
    subprocess is exactly what `env -i PATH=...` was achieving, one exec
    earlier and with nothing to install first.

    That works only because `unshare` and `chroot` are resolved to absolute
    paths (see :func:`_host_tool`) rather than found through the child's PATH.
    The two requirements really are in tension — `chroot` lives in /usr/sbin
    while ash must see only /bin, and a child PATH that omits /usr/sbin kills
    the call with rc=127 before any namespace exists (measured) — but
    resolving the host tools up front dissolves the tension instead of
    correcting for it after the fact.
    """
    return _run_host(
        [
            _host_tool("unshare"),
            "-r",
            _host_tool("chroot"),
            str(root),
            "/bin/sh",
            "-c",
            script,
        ],
        timeout,
        env={"PATH": _ROOTFS_PATH},
    )
