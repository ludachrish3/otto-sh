"""The history-suppression payload must be silent and effective on every POSIX shell.

otto prepends :meth:`~otto.host.command_frame.BashFrame.quiet_history` to the
first line it writes into a fresh shell — the readiness handshake, and the
resync probe after a login-proxy hop. Two properties have to hold on *every*
shell otto might land on, old or new, glibc or busybox:

1. **Silence.** A session's stdout and stderr are one merged stream on a PTY,
   so a single "unrecognized option" complaint would be parsed as command
   output and corrupt the READY handshake. Every fragile element in the
   payload is redirect-guarded for exactly this reason.
2. **Effect.** ``HISTFILE`` must actually end up neutralized, not merely
   assigned without complaint. (These two pull in opposite directions: the
   construct that is safest to fail is often the one that does nothing.)
3. **Survival.** The payload shares its line with ``echo <READY>``, so no
   statement may *abort* that line — see the hostile-configuration section
   below. A failure is fine; taking the handshake with it is not.

This is what covers busybox: the end-to-end "nothing reaches the disk" proof
runs against bash on the live bed, and the mechanism is identical across
shells, so what busybox adds is the risk that the payload is *mis-parsed* —
which is precisely what this guard tests. Shells beyond the guaranteed two are
tested when installed rather than skipped-if-absent, so a runner with more
shells gets more coverage and a runner with fewer never silently loses the
baseline.
"""

import shutil
import subprocess

import pytest

from otto.host.command_frame import BashFrame

PAYLOAD = BashFrame().quiet_history()

# Always present on any machine that can run otto's own test suite; a missing
# one is a broken runner, not a skip.
_REQUIRED_SHELLS: dict[str, list[str]] = {
    "sh": ["sh"],
    "bash": ["bash"],
}

# Tested when installed, NOT skipped-if-absent: a runner with more shells gets
# more coverage and a runner with fewer never silently loses the baseline. The
# dev VM provisions zsh/ksh/mksh for exactly this matrix (Vagrantfile,
# "dev-root"); CI's ubuntu-latest has neither ksh nor mksh, which is why these
# cannot be promoted to _REQUIRED_SHELLS. Which ones actually ran is visible in
# the parametrized test ids — this suite cannot warn about the gap because
# `filterwarnings = ["error"]` would turn that into a failure.
#
# Each has earned its place by catching something the others could not:
#   busybox-sh  the ash dialect on container/embedded-ish targets; rejects
#               `set +o history`.
#   zsh         its `command` is a precommand modifier restricted to EXTERNAL
#               commands, so the POSIX de-specialization idiom is inert there.
#   ksh         reports a readonly assignment from OUTSIDE the simple command's
#               own redirection, which a plain trailing `2>/dev/null` misses.
_OPTIONAL_SHELLS: dict[str, list[str]] = {
    "dash": ["dash"],
    "ash": ["ash"],
    "zsh": ["zsh"],
    "ksh": ["ksh"],
    "mksh": ["mksh"],
    "busybox-sh": ["busybox", "sh"],
}


# Installed by the dev VM's "dev-root" provisioner specifically for this
# matrix. Not required (CI's ubuntu-latest lacks ksh/mksh) but their ABSENCE
# from _OPTIONAL_SHELLS is a defect — see
# test_every_installed_shell_of_interest_is_actually_in_the_matrix.
_PROVISIONED_SHELLS: tuple[str, ...] = ("zsh", "ksh", "mksh")


def _available() -> list[tuple[str, list[str]]]:
    found = [(name, argv) for name, argv in _REQUIRED_SHELLS.items() if shutil.which(argv[0])]
    missing = sorted(set(_REQUIRED_SHELLS) - {name for name, _ in found})
    if missing:
        raise RuntimeError(f"runner is missing baseline shell(s): {missing}")
    found += [(name, argv) for name, argv in _OPTIONAL_SHELLS.items() if shutil.which(argv[0])]
    return found


SHELLS = _available()


def _run(argv: list[str], script: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [*argv, "-c", script],
        capture_output=True,
        text=True,
        timeout=10,
        check=False,  # a non-zero exit IS the thing under test
    )


@pytest.mark.parametrize(("name", "argv"), SHELLS, ids=[n for n, _ in SHELLS])
def test_payload_is_completely_silent(name: str, argv: list[str]):
    """No stdout, no stderr, exit 0 — anything else corrupts the READY parse."""
    proc = _run(argv, PAYLOAD)
    assert proc.returncode == 0, f"{name}: exit {proc.returncode}"
    assert proc.stdout == "", f"{name}: wrote to stdout: {proc.stdout!r}"
    assert proc.stderr == "", f"{name}: wrote to stderr: {proc.stderr!r}"


@pytest.mark.parametrize(("name", "argv"), SHELLS, ids=[n for n, _ in SHELLS])
def test_payload_actually_neutralizes_histfile(name: str, argv: list[str]):
    """The assignment takes effect — not merely accepted without complaint."""
    proc = _run(argv, PAYLOAD + 'printf %s "$HISTFILE"')
    assert proc.returncode == 0, f"{name}: exit {proc.returncode}"
    assert proc.stdout == "/dev/null", f"{name}: HISTFILE={proc.stdout!r}"


@pytest.mark.parametrize(("name", "argv"), SHELLS, ids=[n for n, _ in SHELLS])
def test_payload_exports_histfile_to_child_shells(name: str, argv: list[str]):
    """Exported, so an interactive subshell otto spawns inherits the setting."""
    proc = _run(argv, PAYLOAD + "sh -c 'printf %s \"$HISTFILE\"'")
    assert proc.stdout == "/dev/null", f"{name}: child saw HISTFILE={proc.stdout!r}"


@pytest.mark.parametrize(("name", "argv"), SHELLS, ids=[n for n, _ in SHELLS])
def test_payload_does_not_abort_the_shell(name: str, argv: list[str]):
    """`set` is a POSIX special builtin: rejecting its option is a FATAL error.

    Unguarded, dash exits the entire shell here — which on a host whose login
    shell is /bin/sh would kill otto's session outright at the handshake. The
    ``command`` prefix strips special-builtin status so the error is ordinary.
    """
    proc = _run(argv, PAYLOAD + "printf REACHED")
    assert proc.stdout == "REACHED", f"{name}: shell died inside the payload"


@pytest.mark.parametrize(("name", "argv"), SHELLS, ids=[n for n, _ in SHELLS])
def test_payload_leaves_a_zero_exit_status(name: str, argv: list[str]):
    """$? must be 0 afterwards — the payload also prefixes the resync probe.

    ``BashFrame.recover`` bakes ``$?`` into its own marker, so a non-zero
    status here surfaces as ``…RECOVER__2__`` on dash / ``__1__`` on busybox.
    Harmless to the liveness regex (it only needs digits) but it reads as a
    failure in a debug log.
    """
    proc = _run(argv, PAYLOAD + 'echo "__OTTO_ab_RECOVER__$?__"')
    assert proc.stdout.strip() == "__OTTO_ab_RECOVER__0__", f"{name}: {proc.stdout!r}"


def test_history_recording_is_actually_disabled_in_bash():
    """The in-memory list is off too, not just the file — bash only.

    Must run bash INTERACTIVE (``-i``). A non-interactive bash reports
    ``history off`` with no payload at all, so the non-interactive form of
    this assertion passes against a completely empty payload and proves
    nothing. otto's sessions are interactive, which is the case that matters.
    """
    baseline = _run(["bash", "-i"], "set -o | grep '^history'")
    assert "on" in baseline.stdout, (
        f"interactive bash should start with history ON, got {baseline.stdout!r} — "
        f"without that baseline the assertion below is vacuous"
    )
    proc = _run(["bash", "-i"], PAYLOAD + "set -o | grep '^history'")
    assert "off" in proc.stdout, f"bash still recording: {proc.stdout!r}"


# ---------------------------------------------------------------------------
# Hostile shell configurations
#
# The payload is written into whatever shell the host's passwd entry names,
# configured however that host's admin left it. Its statements can FAIL rather
# than merely be unsupported, and POSIX has two *separate* rules that turn a
# failure into a dead session:
#
#   1. an error in a SPECIAL BUILTIN (`set`, `export`) aborts the shell —
#      handled by the `command` prefix, which de-specializes it;
#   2. a failed VARIABLE ASSIGNMENT aborts the rest of the compound line,
#      which strands `echo <READY>` and hangs session startup until it times
#      out as "shell never became ready" — i.e. a working host goes
#      UNREACHABLE, with an error blaming credentials.
#
# Rule 2 is why HISTFILE is set via `command export VAR=value` rather than a
# bare `VAR=value` assignment. Both were verified on real interactive PTYs,
# not just `sh -c`: under `readonly HISTFILE` the unguarded payload strands
# bash, dash, busybox ash AND zsh alike.
#
# zsh then needs a third construct, because its `command` is external-only —
# see `test_payload_carries_a_zsh_specific_clause`.
# ---------------------------------------------------------------------------

_READY = "__OTTO_test_READY__"


@pytest.mark.parametrize(("name", "argv"), SHELLS, ids=[n for n, _ in SHELLS])
def test_readonly_histfile_does_not_strand_the_ready_marker(name: str, argv: list[str]):
    """A readonly HISTFILE must not abort the line carrying the READY probe."""
    proc = _run(argv, f"readonly HISTFILE=/keep; {PAYLOAD}echo {_READY}")
    assert _READY in proc.stdout, (
        f"{name}: READY marker never emitted — the payload aborted the handshake "
        f"line, which would take this host offline (stderr={proc.stderr!r})"
    )


@pytest.mark.parametrize(("name", "argv"), SHELLS, ids=[n for n, _ in SHELLS])
def test_readonly_histfile_is_left_intact(name: str, argv: list[str]):
    """Best-effort: if HISTFILE cannot be changed, leave it alone and carry on."""
    proc = _run(argv, f'readonly HISTFILE=/keep; {PAYLOAD}printf %s "$HISTFILE"')
    assert proc.stdout == "/keep", f"{name}: clobbered a readonly HISTFILE: {proc.stdout!r}"


def test_restricted_bash_still_reaches_the_ready_marker():
    """rbash forbids setting HISTFILE *and* forbids redirection.

    Both of the payload's statements therefore fail. It must still degrade to
    a working (if noisy) session rather than stranding the READY probe —
    ``_read_until_pattern`` discards anything preceding the marker.
    """
    proc = _run(["bash", "--restricted"], PAYLOAD + f"echo {_READY}")
    assert _READY in proc.stdout, f"rbash session stranded: stderr={proc.stderr!r}"


@pytest.mark.parametrize(("name", "argv"), SHELLS, ids=[n for n, _ in SHELLS])
def test_payload_survives_errexit_and_nounset(name: str, argv: list[str]):
    """An rc file may leave `set -eu` on; neither may kill the handshake line."""
    proc = _run(argv, f"set -eu; {PAYLOAD}echo {_READY}")
    assert _READY in proc.stdout, f"{name}: set -eu killed the payload"


def test_payload_composes_onto_a_handshake_without_splitting_the_line():
    """Concatenation with the dialect handshake yields exactly one command line."""
    from otto.host.command_frame import SessionMarkers

    m = SessionMarkers.for_session("cafef00d")
    composed = PAYLOAD + BashFrame().handshake(m)
    assert composed.count("\n") == 1
    assert composed.endswith("\n")


@pytest.mark.parametrize(("name", "argv"), SHELLS, ids=[n for n, _ in SHELLS])
def test_payload_stays_silent_under_readonly(name: str, argv: list[str]):
    """Failing is acceptable; *narrating* the failure onto the stream is not.

    Session stdout and stderr are one merged stream on a PTY, so a
    "read-only variable" complaint would be parsed as command output.
    """
    proc = _run(argv, f"readonly HISTFILE=/keep; {PAYLOAD}")
    assert proc.stdout == "", f"{name}: stdout {proc.stdout!r}"
    assert proc.stderr == "", f"{name}: stderr {proc.stderr!r}"


def test_payload_carries_a_zsh_specific_clause():
    """zsh needs its own clause, and this is the only test that always runs.

    zsh's ``command`` is a *precommand modifier* that restricts lookup to
    EXTERNAL commands, so ``command export`` / ``command set`` are silent
    no-ops there and the generic guards suppress nothing on a zsh login
    shell. The parametrized cases above do catch that — but only on a runner
    that happens to have zsh installed, so they cannot be relied on to defend
    the clause.

    Deliberately shape-pinning: the requirement is invisible from the payload
    itself, so without this a future editor deletes the clause and sees every
    test still pass.
    """
    payload = BashFrame().quiet_history()
    assert "ZSH_VERSION" in payload, "lost the zsh detection clause"
    assert "eval " in payload, (
        "the zsh clause must use eval — a bare `export HISTFILE=...` aborts the "
        "line on zsh under readonly, reintroducing the stranded-handshake bug"
    )


def test_every_installed_shell_of_interest_is_actually_in_the_matrix():
    """A shell present on this box must be TESTED, not quietly passed over.

    The matrix is deliberately opt-in-by-presence (CI's ubuntu-latest has
    neither ksh nor mksh, so these cannot be required), and that design has one
    failure mode: someone edits ``_OPTIONAL_SHELLS``, drops an entry, and the
    suite goes green having tested less than it did yesterday — silently,
    because a shrinking parametrization looks identical to a smaller runner.

    This pins the contract the other direction: *installed* implies *tested*.
    It cannot fail on a runner that lacks these shells, so it costs CI nothing.
    """
    # Static half — checked on every runner, including ones without these
    # shells, so removing an entry fails in CI too rather than only on the box
    # that would quietly lose the coverage.
    for name in _PROVISIONED_SHELLS:
        assert name in _OPTIONAL_SHELLS, (
            f"{name} was removed from _OPTIONAL_SHELLS. It is provisioned "
            f"specifically for this matrix (Vagrantfile, 'dev-root' block) and "
            f"has caught defects no other shell here can — put it back, or drop "
            f"it from provisioning too and say why."
        )

    # Dynamic half — installed implies tested, for every shell listed.
    tested = {name for name, _ in SHELLS}
    for name, argv in _OPTIONAL_SHELLS.items():
        if shutil.which(argv[0]):
            assert name in tested, f"{name} is installed here but missing from the matrix"
