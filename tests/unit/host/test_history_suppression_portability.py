"""The first line otto writes into a fresh shell must be silent on every POSIX shell.

otto places :meth:`~otto.host.command_frame.BashFrame.quiet_history` in the
first line it writes into a fresh shell, right after the line's anchor — the
readiness handshake, and the resync probe after a login-proxy hop. Two
properties have to hold on *every* shell otto might land on, old or new, glibc
or busybox:

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

THE OTHER STATEMENT ON THAT LINE IS HERE TOO — see the stty section at the
bottom. ``handshake()`` renders ``stty -echo 2>/dev/null; echo; …; echo <READY>``
(the bare ``echo`` puts READY at the start of a line whatever the terminal's
echo state), and suppression rides in front of it, so they share one line
and one failure mode: anything that aborts the line strands READY and takes a
working host offline. The stty half was measured inside a chrooted BusyBox
root while that harness existed (it could delete ``/bin/stty`` because the
root was its own); the live BusyBox bed that replaced the harness cannot host
it, because removing an applet from a shared guest is a bed mutation.
Injecting the same condition into a real shell here is what keeps the arm
measured.
"""

import os
import pty
import select
import shutil
import subprocess

import pytest

from otto.host.command_frame import BashFrame, SessionMarkers
from otto.host.login_proxy import _identity_pattern, _identity_probe
from otto.host.session import ready_pattern
from otto.host.terminal_mapping import ONLCR, measure
from otto.utils import WaitTimeoutError, wait_for

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


def _run(
    argv: list[str], script: str, env: "dict[str, str] | None" = None, *, merge: bool = False
) -> subprocess.CompletedProcess[str]:
    """Run *script* under *argv*, optionally in a replacement environment.

    *env* resolves ``argv[0]`` ABSOLUTELY before launching, because the
    environment the stty section hands in has an empty ``PATH`` — the same
    variable ``subprocess`` searches to find the shell binary itself, so a
    bare name would fail to launch rather than fail to find ``stty``.

    *merge* folds stderr into stdout, which is what a PTY does. Bytes in,
    ``.decode()`` out: no newline translation, so :func:`measure` sees
    ``\r\n`` and a lone ``\r`` exactly as the shell wrote them.
    """
    if env is not None:
        argv = [shutil.which(argv[0]) or argv[0], *argv[1:]]
    proc = subprocess.run(
        [*argv, "-c", script],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT if merge else subprocess.PIPE,
        timeout=10,
        check=False,  # a non-zero exit IS the thing under test
        env=env,
    )
    return subprocess.CompletedProcess(
        proc.args, proc.returncode, proc.stdout.decode(), (proc.stderr or b"").decode()
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
    """$? must be 0 afterwards — the payload rides in the resync probe too.

    The resync probe (``login_proxy._identity_probe``) bakes ``$?`` into its
    reply right after the payload, so a non-zero status here surfaces as
    ``…RECOVER__2__`` on dash / ``__1__`` on busybox. Harmless to the
    identity regex (it only needs digits) but it reads as a failure in a
    debug log.
    """
    m = SessionMarkers.for_session("cafef00d")
    proc = _run(argv, _identity_probe(m, PAYLOAD))
    assert f"{m.recover}0__" in proc.stdout, f"{name}: {proc.stdout!r}"


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
    ``read_transport_until`` discards anything preceding the marker.
    """
    proc = _run(["bash", "--restricted"], PAYLOAD + f"echo {_READY}")
    assert _READY in proc.stdout, f"rbash session stranded: stderr={proc.stderr!r}"


@pytest.mark.parametrize(("name", "argv"), SHELLS, ids=[n for n, _ in SHELLS])
def test_payload_survives_errexit_and_nounset(name: str, argv: list[str]):
    """An rc file may leave `set -eu` on; neither may kill the handshake line."""
    proc = _run(argv, f"set -eu; {PAYLOAD}echo {_READY}")
    assert _READY in proc.stdout, f"{name}: set -eu killed the payload"


def test_payload_composes_onto_a_handshake_without_splitting_the_line():
    """The frame composes suppression into its handshake as exactly one command line."""
    m = SessionMarkers.for_session("cafef00d")
    composed = BashFrame().handshake(m, PAYLOAD)
    assert PAYLOAD in composed
    assert composed.count("\n") == 1
    assert composed.endswith("\n")


@pytest.mark.parametrize(("name", "argv"), SHELLS, ids=[n for n, _ in SHELLS])
def test_the_split_ready_marker_prints_whole_on_a_line_of_its_own(name: str, argv: list[str]):
    """The handshake types READY split across a variable; every shell prints it whole.

    The split keeps READY out of the typed line, and so out of any echo of
    it. It only works if each shell joins the halves again, so the shell's
    output has the marker as the last line.
    """
    m = SessionMarkers.for_session("cafef00d")
    handshake = BashFrame().handshake(m, PAYLOAD)
    assert m.ready not in handshake
    proc = _run(argv, handshake)
    assert proc.stdout.endswith(f"\n{m.ready}\n"), f"{name}: stdout {proc.stdout!r}"


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


# ---------------------------------------------------------------------------
# The handshake's other statement: a device with no `stty`
#
# `handshake()` renders `stty -echo 2>/dev/null; echo; echo <READY>`. `stty` is
# an ORDINARY EXTERNAL COMMAND on the devices otto lands on, not a builtin, so
# a stripped userland can simply not have it — and then the shell, not stty,
# produces the diagnostic ("not found"). That message has to be swallowed by
# the same `2>/dev/null`, and the line has to carry on to `echo; echo <READY>`:
# stdout and stderr are one merged stream on a PTY, so a leaked complaint is
# parsed as command output, and a stranded READY hangs session startup until
# it times out as "shell never became ready" — a working host reported
# unreachable, blaming credentials.
#
# THE CONDITION IS INJECTED, NOT INHERITED. Every shell on every runner this
# suite meets HAS stty (asserted below), so a test that merely ran the
# handshake as-is could never exercise a shell that lacks it, whatever its
# name claimed. The injection is an empty PATH, and it has one hole worth
# naming: a BusyBox built with CONFIG_FEATURE_SH_STANDALONE resolves applets
# INTERNALLY, so `stty` stays reachable there with no PATH at all. Ubuntu's
# busybox is such a build (measured — see `_STANDALONE_SHELLS`), which is
# exactly why the retired artifact harness had to delete `/bin/stty` from a
# root it owned rather than edit an environment variable.
# ---------------------------------------------------------------------------

HANDSHAKE = BashFrame().handshake(SessionMarkers.for_session("cafef00d"))

_STTY_PROBE = "command -v stty >/dev/null 2>&1 && echo FOUND || echo GONE"

# Shells that resolve `stty` with no PATH, so an empty PATH is not a removal
# there. Measured on this runner 2026-08-21: Ubuntu's busybox is a standalone
# build, every other shell in the matrix answers GONE. Kept as a LIST OF NAMES
# rather than probed per run, so a shell that quietly became standalone (or
# stopped being one) reds `test_an_empty_path_is_what_makes_stty_unreachable`
# instead of silently changing what the arm below covers.
_STANDALONE_SHELLS = frozenset({"busybox-sh"})

_STTY_ABSENT_SHELLS = [(name, argv) for name, argv in SHELLS if name not in _STANDALONE_SHELLS]


def _stty_lookup(argv: list[str], env: "dict[str, str] | None" = None) -> str:
    """``FOUND`` or ``GONE``, asked of the shell's own resolver rather than of us."""
    return _run(argv, _STTY_PROBE, env=env).stdout.strip()


@pytest.mark.parametrize(("name", "argv"), SHELLS, ids=[n for n, _ in SHELLS])
def test_an_empty_path_is_what_makes_stty_unreachable(name: str, argv: list[str], tmp_path):
    """The injection control for the arm below, one row per shell.

    Both directions, because either one alone is satisfiable by an accident: a
    runner with no ``stty`` at all would report GONE without the empty PATH
    doing anything, and a standalone applet build reports FOUND however empty
    the PATH is. Only the pair says "PATH is what removed it".

    A shell that flips sides belongs on ``_STANDALONE_SHELLS`` with the reason
    written down — not silently, which is what a per-run probe would allow.
    """
    assert _stty_lookup(argv) == "FOUND", (
        f"{name}: this runner cannot find stty even with its ambient PATH, so "
        f"removing it below removes nothing and the arm is inert"
    )
    expected = "FOUND" if name in _STANDALONE_SHELLS else "GONE"
    assert _stty_lookup(argv, env={**os.environ, "PATH": str(tmp_path)}) == expected, (
        f"{name}: with PATH pointed at an empty directory the shell answered the "
        f"opposite of {expected!r}. Either it became a standalone applet build (add "
        f"it to _STANDALONE_SHELLS and say so) or it stopped being one (remove it) — "
        f"an unlisted standalone shell makes its row in the stty arm measure nothing"
    )


@pytest.mark.parametrize(
    ("name", "argv"), _STTY_ABSENT_SHELLS, ids=[n for n, _ in _STTY_ABSENT_SHELLS]
)
def test_the_handshake_reaches_ready_when_stty_is_unreachable(name: str, argv: list[str], tmp_path):
    """No stty: the shell's own "not found" must be swallowed and READY still land.

    The claim is about the SURROUNDING LINE, not about stty working. otto never
    needs the echo actually turned off to make progress — a session that reads
    its own probe text back is noisy, not broken — but a session that never
    sees READY is a host otto declares unreachable.
    """
    env = {**os.environ, "PATH": str(tmp_path)}
    proc = _run(argv, HANDSHAKE, env=env)
    assert SessionMarkers.for_session("cafef00d").ready in proc.stdout, (
        f"{name}: the handshake never reached READY with stty unresolvable "
        f"(rc={proc.returncode} stdout={proc.stdout!r} stderr={proc.stderr!r}) — a "
        f"host on this shell would time out at startup as 'shell never became ready'"
    )
    assert proc.stderr == "", (
        f"{name}: the shell's own 'not found' complaint leaked past the handshake's "
        f"`2>/dev/null` ({proc.stderr!r}). On a PTY that text is merged into stdout "
        f"and parsed as command output"
    )


def test_the_stty_arm_runs_on_the_shells_every_runner_has():
    """A shrinking parametrization must never be why the arm above is green.

    ``_STTY_ABSENT_SHELLS`` is filtered, and a filter that ate everything would
    collect zero rows and report success. The baseline shells are required on
    any machine that can run this suite, so they are the floor.
    """
    covered = {name for name, _ in _STTY_ABSENT_SHELLS}
    assert set(_REQUIRED_SHELLS) <= covered, (
        f"the stty-absent arm covers {sorted(covered)}, missing "
        f"{sorted(set(_REQUIRED_SHELLS) - covered)}. Excluding a baseline shell "
        f"leaves the arm resting on optional shells a runner may not have"
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


# ---------------------------------------------------------------------------
# A probe line cut short (#761)
#
# A console server that delivers input in pieces lets login(1) (or su) flush
# the start of otto's first line, so the shell runs only its tail. The line
# holds the end of its marker in a variable that only its FIRST statement
# sets, so a tail can print half a marker at most, and otto sends the line
# again whole. These sweeps run every tail of the real line and prove none of
# them answers, on stdout or stderr, plain or under nounset.
# ---------------------------------------------------------------------------

M = SessionMarkers.for_session("cafef00d")
LINE = BashFrame().handshake(M, PAYLOAD)  # the session's real first line, history suppressed
RESYNC = _identity_probe(M, PAYLOAD)  # the su/sudo/exit resync line
_ANCHOR_SPAN = LINE.index("; ") + 2 + 30  # the assignment plus the start of what follows


def _assert_no_tail_answers(argv, line, marker, pattern, offsets, nounset):
    shell = [*argv, "-u"] if nounset else argv
    bad = {}
    for k in offsets:
        out = _run(shell, line[k:], merge=True).stdout
        if marker in out or pattern.search(out):
            bad[k] = out
    assert bad == {}, f"{argv}: tails that answered: {bad}"


@pytest.mark.parametrize("nounset", [False, True], ids=["plain", "nounset"])
@pytest.mark.parametrize(
    ("name", "argv"), list(_REQUIRED_SHELLS.items()), ids=list(_REQUIRED_SHELLS)
)
def test_a_handshake_cut_anywhere_never_answers(name, argv, nounset):
    """login(1) can flush a probe's head; no tail the shell then runs may print READY (#761)."""
    _assert_no_tail_answers(
        argv, LINE, M.ready, ready_pattern(M, line_ending=True), range(1, len(LINE)), nounset
    )


@pytest.mark.parametrize("nounset", [False, True], ids=["plain", "nounset"])
@pytest.mark.parametrize(
    ("name", "argv"), list(_REQUIRED_SHELLS.items()), ids=list(_REQUIRED_SHELLS)
)
def test_a_resync_probe_cut_anywhere_never_answers(name, argv, nounset):
    """su flushes the tty the same way; no tail of the resync probe may answer it (#761)."""
    _assert_no_tail_answers(
        argv, RESYNC, M.recover, _identity_pattern(M), range(1, len(RESYNC)), nounset
    )


@pytest.mark.parametrize("nounset", [False, True], ids=["plain", "nounset"])
@pytest.mark.parametrize(("name", "argv"), SHELLS, ids=[n for n, _ in SHELLS])
def test_a_cut_inside_the_anchor_never_answers_on_any_shell(name, argv, nounset):
    """The anchor region is where shells differ most, so every installed shell runs it."""
    # Offsets 1.._ANCHOR_SPAN cover a suffix-named assignment, `=_READY__` and
    # `_READY__` run as commands, and the first statement after the anchor.
    _assert_no_tail_answers(
        argv, LINE, M.ready, ready_pattern(M, line_ending=True), range(1, _ANCHOR_SPAN), nounset
    )


def test_the_bed_tail_answers_nothing():
    """The exact tail the bed ran, from inside the calibration's ``2>/dev/null``."""
    k = LINE.rindex("dev/null; echo")
    out = _run(["bash"], LINE[k:], merge=True).stdout
    assert M.ready not in out
    assert not ready_pattern(M, line_ending=True).search(out)


@pytest.mark.parametrize("nounset", [False, True], ids=["plain", "nounset"])
@pytest.mark.parametrize(("name", "argv"), SHELLS, ids=[n for n, _ in SHELLS])
def test_the_whole_line_prints_ready_last_calibrated_and_leaves_nothing_set(name, argv, nounset):
    """Run whole, the line prints READY last, calibrated, and unsets its variable."""
    shell = [*argv, "-u"] if nounset else argv
    proc = _run(shell, LINE + 'echo "[${__otto_probe-unset}]"\n')
    assert proc.stdout.endswith(f"\n{M.ready}\n[unset]\n"), f"{name}: {proc.stdout!r}"
    assert measure(proc.stdout.removesuffix("[unset]\n"), M.ready).mapping == "identity"


@pytest.mark.parametrize(("name", "argv"), SHELLS, ids=[n for n, _ in SHELLS])
def test_the_whole_resync_line_answers_with_digits_and_the_user(name, argv):
    """Run whole, the resync probe answers with its exit code and the user."""
    assert _identity_pattern(M).search(_run(argv, RESYNC).stdout)


# ---------------------------------------------------------------------------
# The same cut, typed into an INTERACTIVE shell on a pty
#
# The sweeps above run `sh -c`, where an unbalanced quote is a syntax error.
# A login shell on a pty reads the line interactively instead, and there an
# unbalanced quote is a CONTINUATION: the shell prints PS2 and joins every
# following line into the open word. A tail cut inside a quoted echo word
# would then swallow every whole line otto resends (each carries an even
# number of quotes, so the word never closes) and the shell would never
# become ready. So the probe's echo word carries no quotes at all, and this
# arm types a tail cut inside it, then resends the whole line the way
# confirm_live does, into `bash -i` on a real pty.
# ---------------------------------------------------------------------------

_PTY_PROBE_INTERVAL = 0.5  # the session's own resend interval (_init_probe_interval)


def _interactive_answer(tail: str, line: str, answered) -> str:
    """Type *tail* into ``bash -i`` on a pty, then resend *line* until *answered*(output).

    Returns everything the terminal printed. A shell that never answers
    fails the test with what it printed (a run of PS2 ``> `` is the wedge).
    """
    controller, terminal = pty.openpty()
    env = {"PATH": os.environ["PATH"], "PS1": "$ ", "PS2": "> ", "TERM": "dumb"}
    shell = subprocess.Popen(
        ["bash", "--norc", "--noprofile", "-i"],
        stdin=terminal,
        stdout=terminal,
        stderr=terminal,
        env=env,
        start_new_session=True,
    )
    os.close(terminal)
    out = bytearray()

    def drain() -> str:
        while select.select([controller], [], [], 0)[0]:
            try:
                chunk = os.read(controller, 65536)
            except OSError:
                break
            if not chunk:
                break
            out.extend(chunk)
        return out.decode(errors="replace")

    def resend_until_answered() -> bool:
        if answered(drain()):
            return True
        os.write(controller, line.encode())
        return False

    try:
        wait_for(
            lambda: drain().endswith("$ "),
            5,
            interval=0.05,
            on_timeout=lambda: f"bash -i never prompted: {drain()!r}",
        )
        os.write(controller, tail.encode())
        try:
            wait_for(
                resend_until_answered,
                5,
                interval=_PTY_PROBE_INTERVAL,
                on_timeout="the shell never answered the resent line",
            )
        except WaitTimeoutError:
            pytest.fail(f"no answer after the cut tail {tail!r}; the terminal: {drain()[-300:]!r}")
        return drain()
    finally:
        shell.kill()
        shell.wait(timeout=5)
        os.close(controller)


_NEEDS_BASH = pytest.mark.skipif(shutil.which("bash") is None, reason="needs bash on a pty")


@_NEEDS_BASH
def test_a_handshake_cut_inside_the_echo_word_does_not_wedge_an_interactive_shell():
    """A tail cut inside the echo word leaves the next whole line runnable, calibrated (#761)."""
    pattern = ready_pattern(M, line_ending=True)
    out = _interactive_answer(LINE[LINE.index("__OTTO_") + 3 :], LINE, pattern.search)
    found = pattern.search(out)
    assert found is not None
    assert measure(out[: found.end()], M.ready).mapping == ONLCR


@_NEEDS_BASH
def test_a_resync_probe_cut_inside_the_echo_word_does_not_wedge_an_interactive_shell():
    """The resync probe's echo word takes the same cut; the whole probe still answers."""
    _interactive_answer(RESYNC[RESYNC.index("__OTTO_") + 3 :], RESYNC, _identity_pattern(M).search)
