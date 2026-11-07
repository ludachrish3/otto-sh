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
bottom. ``handshake()`` renders ``2>/dev/null stty -echo || :; echo; …; echo <READY>``
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
import re
import shutil
import subprocess

import pytest

from otto.host.command_frame import (
    _PROBE_VAR,
    READY_SUFFIX,
    RECOVER_SUFFIX,
    BashFrame,
    SessionMarkers,
)
from otto.host.console import strip_ansi
from otto.host.login_proxy import _identity_pattern, _identity_probe
from otto.host.session import ready_pattern
from otto.host.terminal_mapping import CALIBRATION_COMMAND, IDENTITY, ONLCR, measure
from tests._fixtures.interactive_shell import (
    FAKE_RAN,
    HAZARD_MARKERS,
    OPTIONAL_SHELLS,
    PROVISIONED_SHELLS,
    READY_TIMEOUT,
    REQUIRED_SHELLS,
    RESEND_MARKERS,
    SHELLS,
    TAIL_MARKERS,
    InteractiveShell,
    cut_probe,
    interactive_shell,
    probe_answer,
    render_probe,
    terminal_reader_fakes,
)

PAYLOAD = BashFrame().quiet_history()


def _run(
    argv: list[str], script: str, env: "dict[str, str] | None" = None, *, merge: bool = False
) -> subprocess.CompletedProcess[str]:
    """Run *script* under *argv*, optionally in a replacement environment.

    *env* resolves ``argv[0]`` ABSOLUTELY before launching, because the
    environment the stty section hands in has an empty ``PATH`` — the same
    variable ``subprocess`` searches to find the shell binary itself, so a
    bare name would fail to launch rather than fail to find ``stty``.

    stdin is ``/dev/null``, so a command a tail runs that reads it ends at
    once rather than waiting on whatever stdin pytest was given.

    *merge* folds stderr into stdout, which is what a PTY does. Bytes in,
    ``.decode()`` out: no newline translation, so :func:`measure` sees
    ``\r\n`` and a lone ``\r`` exactly as the shell wrote them.
    """
    if env is not None:
        argv = [shutil.which(argv[0]) or argv[0], *argv[1:]]
    proc = subprocess.run(
        [*argv, "-c", script],
        stdin=subprocess.DEVNULL,
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
    m = TAIL_MARKERS
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
    m = TAIL_MARKERS
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
    m = TAIL_MARKERS
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
# `handshake()` renders `2>/dev/null stty -echo || :; echo; …; echo <READY>`. `stty` is
# an ORDINARY EXTERNAL COMMAND on the devices otto lands on, not a builtin, so
# a stripped userland can simply not have it — and then the shell, not stty,
# produces the diagnostic ("not found"). That message has to be swallowed by
# the same `2>/dev/null`, and the line has to carry on to `echo`, the
# calibration and `echo <READY>`:
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

HANDSHAKE = BashFrame().handshake(TAIL_MARKERS)

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
    assert TAIL_MARKERS.ready in proc.stdout, (
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
    assert set(REQUIRED_SHELLS) <= covered, (
        f"the stty-absent arm covers {sorted(covered)}, missing "
        f"{sorted(set(REQUIRED_SHELLS) - covered)}. Excluding a baseline shell "
        f"leaves the arm resting on optional shells a runner may not have"
    )


def test_every_installed_shell_of_interest_is_actually_in_the_matrix():
    """A shell present on this box must be TESTED, not quietly passed over.

    The matrix is deliberately opt-in-by-presence (CI's ubuntu-latest has
    neither ksh nor mksh, so these cannot be required), and that design has one
    failure mode: someone edits ``OPTIONAL_SHELLS``, drops an entry, and the
    suite goes green having tested less than it did yesterday — silently,
    because a shrinking parametrization looks identical to a smaller runner.

    This pins the contract the other direction: *installed* implies *tested*.
    It cannot fail on a runner that lacks these shells, so it costs CI nothing.
    """
    # Static half — checked on every runner, including ones without these
    # shells, so removing an entry fails in CI too rather than only on the box
    # that would quietly lose the coverage.
    for name in PROVISIONED_SHELLS:
        assert name in OPTIONAL_SHELLS, (
            f"{name} was removed from OPTIONAL_SHELLS. It is provisioned "
            f"specifically for this matrix (Vagrantfile, 'dev-root' block) and "
            f"has caught defects no other shell here can — put it back, or drop "
            f"it from provisioning too and say why."
        )

    # Dynamic half — installed implies tested, for every shell listed.
    tested = {name for name, _ in SHELLS}
    for name, argv in OPTIONAL_SHELLS.items():
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

LINE = BashFrame().handshake(
    TAIL_MARKERS, PAYLOAD
)  # the session's real first line, history suppressed
RESYNC = _identity_probe(TAIL_MARKERS, PAYLOAD)  # the su/sudo/exit resync line
RECOVERY = BashFrame().recover(TAIL_MARKERS)  # the post-timeout recovery probe
_ANCHOR_SPAN = LINE.index("; ") + 2 + 30  # the assignment plus the start of what follows


@pytest.fixture(scope="module")
def fakes_env(tmp_path_factory) -> dict[str, str]:
    """This environment with :func:`terminal_reader_fakes` first on ``PATH``.

    A sweep then fails on every machine when a tail's first word is one of
    those names, not only on a machine where it happens to be installed.
    """
    fakes = tmp_path_factory.mktemp("terminal-reader-fakes")
    return {**os.environ, "PATH": terminal_reader_fakes(str(fakes))}


def _assert_no_tail_answers(argv, line, marker, pattern, offsets, nounset, env):
    """No tail of *line* from *offsets* prints *marker* or *pattern*, or runs a terminal reader."""
    shell = [*argv, "-u"] if nounset else argv
    bad = {}
    for k in offsets:
        out = _run(shell, line[k:], env, merge=True).stdout
        if marker in out or pattern.search(out) or FAKE_RAN in out:
            bad[k] = out
    assert bad == {}, f"{argv}: tails that answered or ran a terminal reader: {bad}"


@pytest.mark.parametrize("nounset", [False, True], ids=["plain", "nounset"])
@pytest.mark.parametrize(("name", "argv"), list(REQUIRED_SHELLS.items()), ids=list(REQUIRED_SHELLS))
def test_a_handshake_cut_anywhere_never_answers(name, argv, nounset, fakes_env):
    """login(1) can flush a probe's head; no tail the shell then runs may print READY (#761)."""
    _assert_no_tail_answers(
        argv,
        LINE,
        TAIL_MARKERS.ready,
        ready_pattern(TAIL_MARKERS, line_ending=True),
        range(1, len(LINE)),
        nounset,
        fakes_env,
    )


@pytest.mark.parametrize("nounset", [False, True], ids=["plain", "nounset"])
@pytest.mark.parametrize(("name", "argv"), list(REQUIRED_SHELLS.items()), ids=list(REQUIRED_SHELLS))
def test_a_resync_probe_cut_anywhere_never_answers(name, argv, nounset, fakes_env):
    """su flushes the tty the same way; no tail of the resync probe may answer it (#761)."""
    _assert_no_tail_answers(
        argv,
        RESYNC,
        TAIL_MARKERS.recover,
        _identity_pattern(TAIL_MARKERS),
        range(1, len(RESYNC)),
        nounset,
        fakes_env,
    )


@pytest.mark.parametrize("nounset", [False, True], ids=["plain", "nounset"])
@pytest.mark.parametrize(("name", "argv"), list(REQUIRED_SHELLS.items()), ids=list(REQUIRED_SHELLS))
def test_a_recovery_probe_cut_anywhere_never_answers(name, argv, nounset, fakes_env):
    """After an interrupt the console may cut the recovery probe too; no tail may answer it."""
    _assert_no_tail_answers(
        argv,
        RECOVERY,
        TAIL_MARKERS.recover,
        BashFrame().recover_pattern(TAIL_MARKERS),
        range(1, len(RECOVERY)),
        nounset,
        fakes_env,
    )


@pytest.mark.parametrize("nounset", [False, True], ids=["plain", "nounset"])
@pytest.mark.parametrize(("name", "argv"), SHELLS, ids=[n for n, _ in SHELLS])
def test_a_cut_inside_the_anchor_never_answers_on_any_shell(name, argv, nounset, fakes_env):
    """The anchor region is where shells differ most, so every installed shell runs it."""
    # Offsets 1.._ANCHOR_SPAN cover a suffix-named assignment, `=READY__` and
    # `READY__` run as commands, and the first statement after the anchor.
    _assert_no_tail_answers(
        argv,
        LINE,
        TAIL_MARKERS.ready,
        ready_pattern(TAIL_MARKERS, line_ending=True),
        range(1, _ANCHOR_SPAN),
        nounset,
        fakes_env,
    )


def test_the_bed_tail_answers_nothing():
    """The exact tail the bed ran, from inside the calibration's ``2>/dev/null``."""
    k = LINE.index(CALIBRATION_COMMAND) + CALIBRATION_COMMAND.index("dev/null")
    assert LINE[k - 3 : k + 8] == "2>/dev/null", f"lost the calibration's redirection: {LINE!r}"
    out = _run(["bash"], LINE[k:], merge=True).stdout
    assert TAIL_MARKERS.ready not in out
    assert not ready_pattern(TAIL_MARKERS, line_ending=True).search(out)


def _inside_the_calibrations_last_escape(line: str) -> range:
    """The offsets of every tail that starts inside the calibration format's final escape.

    That escape is the format's newline, the last word before ``; echo``. A
    cut between its two backslashes once left the bare word ``n``, which is
    a terminal-reading program on GitHub's ubuntu runner (#853). The range
    starts there.
    """
    start = line.index(CALIBRATION_COMMAND)
    end = start + len(CALIBRATION_COMMAND)
    k = line.rindex("\\\\", start, end)
    assert line[end:].startswith(" || :; echo "), f"lost the calibration's end: {line!r}"
    assert "\\" not in line[k + 2 : end], f"lost the calibration's last escape: {line!r}"
    return range(k + 1, end)


def _inside_the_ids_end(line: str, fragment: str) -> int:
    """The offset in the echo word where the marker id's last characters, *fragment*, start.

    The id is random hex, so it can end in a command's name (``ed``, ``fc``,
    ``b``), and a cut there must not leave that name as a word of its own.
    """
    at = line.index("echo __OTTO_") + len("echo ")
    head = line[at : line.index("$", at)]
    session_id = head.removeprefix("__OTTO_").rstrip("_")
    assert session_id.endswith(fragment), f"the id {session_id!r} does not end in {fragment!r}"
    k = at + len("__OTTO_") + len(session_id) - len(fragment)
    assert line[k:].startswith(fragment), f"lost the echo word: {line!r}"
    return k


@pytest.mark.parametrize(("name", "argv"), SHELLS, ids=[n for n, _ in SHELLS])
def test_no_tail_inside_the_calibrations_last_escape_runs_a_command(name, argv, fakes_env):
    """A tail inside the format's final ``\\n`` used to run ``n`` (#853); none runs anything."""
    _assert_no_tail_answers(
        argv,
        LINE,
        TAIL_MARKERS.ready,
        ready_pattern(TAIL_MARKERS, line_ending=True),
        _inside_the_calibrations_last_escape(LINE),
        False,
        fakes_env,
    )


@pytest.mark.parametrize("kind", ["handshake", "resync", "recovery"])
@pytest.mark.parametrize("fragment", list(HAZARD_MARKERS))
@pytest.mark.parametrize(("name", "argv"), SHELLS, ids=[n for n, _ in SHELLS])
def test_no_tail_inside_the_marker_id_runs_the_ids_end(name, argv, fragment, kind, fakes_env):
    """An id ending in ``ed``, ``fc`` or ``b``: a tail cut there must not run that command (#853).

    The head's separator sits in the echo word, so the word is ``ed_``, not
    ``ed``. ``fc`` is a builtin in bash, zsh, ksh and mksh, where the fake
    on ``PATH`` is not what runs; under ``sh -c`` their ``fc`` has no
    history to edit, so the fake pins the shells without one. The resync
    and recovery words go on with ``$?__`` after the variable, so only the
    handshake's ended at the id; all three are pinned.
    """
    m = HAZARD_MARKERS[fragment]
    line = render_probe(kind, m)
    k = _inside_the_ids_end(line, fragment)
    _assert_no_tail_answers(argv, line, m.ready, probe_answer(kind, m), [k], False, fakes_env)


@pytest.mark.parametrize("nounset", [False, True], ids=["plain", "nounset"])
@pytest.mark.parametrize(("name", "argv"), SHELLS, ids=[n for n, _ in SHELLS])
def test_the_whole_line_prints_ready_last_calibrated_and_leaves_nothing_set(name, argv, nounset):
    """Run whole, the line prints READY last, calibrated, and unsets its variable."""
    shell = [*argv, "-u"] if nounset else argv
    proc = _run(shell, LINE + 'echo "[${__otto_probe-unset}]"\n')
    assert proc.stdout.endswith(f"\n{TAIL_MARKERS.ready}\n[unset]\n"), f"{name}: {proc.stdout!r}"
    assert measure(proc.stdout.removesuffix("[unset]\n"), TAIL_MARKERS.ready).mapping == "identity"


@pytest.mark.parametrize(("name", "argv"), SHELLS, ids=[n for n, _ in SHELLS])
def test_the_whole_resync_line_answers_with_digits_and_the_user(name, argv):
    """Run whole, the resync probe answers with its exit code and the user."""
    assert _identity_pattern(TAIL_MARKERS).search(_run(argv, RESYNC).stdout)


# ---------------------------------------------------------------------------
# The same lines, typed into an INTERACTIVE shell on a pty (#853)
#
# The sweeps above run `sh -c`, where an unbalanced quote is a syntax error.
# A login shell on a pty reads the line interactively instead, and there a
# tail can leave the shell waiting for more: an open quote or an open
# `${` is a CONTINUATION (PS2) that joins every line otto resends (each
# carries an even number of quotes, so the word never closes), and in zsh a
# command made only of redirections runs `$NULLCMD`, a `cat` that reads every
# resend as its input. Either way the shell never becomes ready.
#
# So these cases type a tail cut at each such hazard into a fresh interactive
# shell on its own controlling pty, then the whole line once, the way
# confirm_live resends it (`cut_probe`), and require the answer. Every offset
# is derived from the rendered line, and every locator asserts it found its
# fragment, so a rewrite of the line moves the cut with it instead of
# silently testing something else. The nightly sweep runs every offset.
# ---------------------------------------------------------------------------

INTERACTIVE = [name for name, _ in SHELLS]
_NOUNSET_SHELLS = [name for name in ("bash", "zsh") if name in INTERACTIVE]
_HISTORY = pytest.mark.parametrize("history", [True, False], ids=["history", "no-history"])


def _after_the_calibration_x(line: str) -> int:
    """The offset just past the calibration's ``X``, inside its format argument."""
    k = line.index(CALIBRATION_COMMAND) + CALIBRATION_COMMAND.index("X") + 1
    assert line[k - 1] == "X", f"lost the calibration format: {line!r}"
    assert line[k] == "\\", f"lost the calibration format: {line!r}"
    return k


def _inside_the_zsh_clause(line: str) -> int:
    """The offset of the zsh clause's ``export``, past where a quote after ``eval`` sat."""
    at = line.index("eval ")
    k = line.index("export HISTFILE=/dev/null", at)
    assert "ZSH_VERSION" in line[:at], f"lost the zsh clause: {line!r}"
    assert k - at <= len("eval '"), f"lost the zsh clause's export: {line!r}"
    return k


# Each redirection is named by the statement it belongs to, the first of these
# words that statement contains.
_STATEMENT_NAMES = {
    "command export": "export-group",
    "command set": "set-group",
    "eval": "eval",
    "stty": "stty",
    "printf": "printf",
}


def _statement_at(line: str, k: int) -> str:
    """The statement around offset *k*, from its ``;``, its ``)`` or (closing a group) its ``{``."""
    start = max(line.rfind("; ", 0, k), line.rfind(") ", 0, k)) + 2
    if line[start:k].startswith("}"):
        start = line.rfind("{", 0, k)
    return line[start : line.index(";", k)]


def _redirections(line: str, expected: "set[str]") -> dict[str, int]:
    """The offset of every ``2>/dev/null``, keyed by its statement; exactly *expected* are found."""
    found: dict[str, int] = {}
    for hit in re.finditer(re.escape("2>/dev/null"), line):
        statement = _statement_at(line, hit.start())
        name = next(n for word, n in _STATEMENT_NAMES.items() if word in statement)
        assert name not in found, f"two redirections in the {name} statement of {line!r}"
        found[name] = hit.start()
    assert set(found) == expected, f"redirections {sorted(found)} in {line!r}"
    return found


def _after_the_echo_word_dollar(line: str, m: SessionMarkers) -> int:
    """The offset just past the ``$`` that expands the probe variable in the echo word."""
    word = f"echo {m.recover.removesuffix(RECOVER_SUFFIX)}$"
    k = line.index(word) + len(word)
    assert line[k:].startswith(_PROBE_VAR), f"lost the echo word: {line!r}"
    return k


def _at_the_echo_words_head(line: str, m: SessionMarkers) -> int:
    """The offset where the echo word's marker head starts, just past ``echo ``.

    The recovery probe's old echo word was quoted, and a tail from just past
    its opening quote began inside the word: its closing quote opened a new one.
    """
    head = m.recover.removesuffix(RECOVER_SUFFIX)
    k = line.index(f"echo {head}") + len("echo ")
    assert line[k:].startswith(f"{head}${_PROBE_VAR}"), f"lost the echo word: {line!r}"
    return k


def _inside_the_echo_word(line: str, m: SessionMarkers) -> int:
    """The offset three characters into the echo word's marker head (``__O|TO_…``)."""
    word = f"echo {m.ready.removesuffix(READY_SUFFIX)}"
    k = line.index(word) + len("echo __O")
    assert line[k - 3 : k] == "__O", f"lost the echo word: {line!r}"
    return k


def _inside_the_or_colons(line: str) -> dict[str, dict[str, int]]:
    """Every tail from inside the ``|| :`` after ``stty`` and ``printf``: ``|| :``, ``| :``, ``:``.

    ``|| :`` keeps either statement's failure from ending the line under
    ``set -e`` (#953). A tail from it starts with a syntax error (``||``,
    ``|``) or runs ``:``, and none may leave the resend unanswered.
    """
    found: dict[str, dict[str, int]] = {}
    for statement, text in (("stty", "stty -echo || :"), ("printf", f"{CALIBRATION_COMMAND} || :")):
        k = line.index(text) + len(text) - len("| :")
        assert line[k - 1 :].startswith("|| :; echo"), f"lost the {statement} statement's || :"
        found[statement] = {"oror": k - 1, "pipe": k, "colon": k + len("| ")}
    return found


def _hazards() -> list[tuple[str, str, int]]:
    """``(id, kind, offset)`` of the fixed hazard tails, one per hazard, on the history line.

    The recovery probe carries no history suppression, so its line is cut as it is.

    The tail after an offset is the same with history suppression on or
    off wherever both lines have it, so only the line with the most hazards
    is cut. Every other offset is the nightly sweep's.
    """
    handshake, resync, recovery = (
        render_probe("handshake", TAIL_MARKERS),
        render_probe("resync", TAIL_MARKERS),
        render_probe("recovery", TAIL_MARKERS),
    )
    _redirections(resync, {"export-group", "set-group", "eval"})
    redirect = _redirections(handshake, {"export-group", "set-group", "eval", "stty", "printf"})
    return [
        ("calibration", "handshake", _after_the_calibration_x(handshake)),
        ("zsh-clause", "handshake", _inside_the_zsh_clause(handshake)),
        ("redirection-export-group", "handshake", redirect["export-group"]),
        ("redirection-eval", "handshake", redirect["eval"]),
        ("redirection-stty", "handshake", redirect["stty"]),
        *(
            (f"or-{statement}-{cut}", "handshake", k)
            for statement, cuts in _inside_the_or_colons(handshake).items()
            for cut, k in cuts.items()
        ),
        ("brace", "resync", _after_the_echo_word_dollar(resync, TAIL_MARKERS)),
        ("recovery-quote", "recovery", _at_the_echo_words_head(recovery, TAIL_MARKERS)),
    ]


HAZARDS = _hazards()
_NOUNSET_HAZARDS = (
    "calibration",
    "zsh-clause",
    "redirection-export-group",
    "brace",
    "recovery-quote",
)


def _of(*cases: str) -> list:
    return [pytest.param(kind, k, id=case) for case, kind, k in HAZARDS if case in cases]


def _whole(shell: InteractiveShell, kind: str, history: bool = True) -> str:
    """Type one whole *kind* line and return the terminal up to the end of its answer."""
    mark = len(shell.output)
    shell.send(render_probe(kind, TAIL_MARKERS, history))
    out = shell.wait_for_output(probe_answer(kind, TAIL_MARKERS), READY_TIMEOUT)
    found = probe_answer(kind, TAIL_MARKERS).search(out, mark)
    assert found is not None
    return out[mark : found.end()]


@_HISTORY
@pytest.mark.parametrize("name", INTERACTIVE)
def test_the_whole_handshake_answers_first_try(name, history):
    """Typed whole once, the handshake prints READY and its calibration reads ``onlcr``."""
    with interactive_shell(name) as shell:
        reply = _whole(shell, "handshake", history)
    assert measure(reply, TAIL_MARKERS.ready).mapping == ONLCR, f"{name}: {reply!r}"


@_HISTORY
@pytest.mark.parametrize("name", INTERACTIVE)
def test_the_whole_resync_probe_answers_first_try(name, history):
    """Typed whole once, the resync probe answers with its exit code and the user."""
    with interactive_shell(name) as shell:
        reply = _whole(shell, "resync", history)
    assert _identity_pattern(TAIL_MARKERS).search(reply)


@pytest.mark.parametrize(("kind", "k"), _of("calibration"))
@pytest.mark.parametrize("name", INTERACTIVE)
def test_a_tail_inside_the_calibration_does_not_wedge(name, kind, k):
    """A tail starting inside the calibration's format leaves the resend runnable (#853)."""
    cut_probe(name, kind, k, TAIL_MARKERS, RESEND_MARKERS)


@pytest.mark.parametrize(("kind", "k"), _of("zsh-clause"))
@pytest.mark.parametrize("name", INTERACTIVE)
def test_a_tail_inside_the_zsh_history_clause_does_not_wedge(name, kind, k):
    """A tail starting at the zsh clause's ``export`` leaves the resend runnable (#853)."""
    cut_probe(name, kind, k, TAIL_MARKERS, RESEND_MARKERS)


@pytest.mark.parametrize(
    ("kind", "k"), _of("redirection-export-group", "redirection-eval", "redirection-stty")
)
@pytest.mark.parametrize("name", INTERACTIVE)
def test_a_redirection_only_tail_does_not_feed_cat(name, kind, k):
    """A tail that starts with ``2>/dev/null`` must not start a command reading the console (#853).

    zsh runs a command made only of redirections as ``$NULLCMD``, ``cat`` by
    default, and that ``cat`` would read every resend as its input. One
    redirection of each form: closing a brace group, leading the zsh
    clause's ``eval``, and on the ``stty`` statement.
    """
    cut_probe(name, kind, k, TAIL_MARKERS, RESEND_MARKERS)


@pytest.mark.parametrize(
    ("kind", "k"),
    _of(
        *(
            f"or-{statement}-{cut}"
            for statement in ("stty", "printf")
            for cut in ("oror", "pipe", "colon")
        )
    ),
)
@pytest.mark.parametrize("name", INTERACTIVE)
def test_a_tail_inside_an_or_colon_does_not_wedge(name, kind, k):
    """A tail from inside the ``|| :`` after ``stty`` or the calibration leaves the resend runnable.

    The ``|| :`` keeps a target without either command answering under
    ``set -e`` (#953); a tail that begins inside it is a syntax error or a
    bare ``:``.
    """
    cut_probe(name, kind, k, TAIL_MARKERS, RESEND_MARKERS)


@pytest.mark.parametrize(("kind", "k"), _of("brace"))
@pytest.mark.parametrize("name", INTERACTIVE)
def test_a_tail_inside_the_echo_word_brace_does_not_wedge(name, kind, k):
    """A resync tail cut just past the echo word's ``$`` leaves the resend runnable (#853)."""
    cut_probe(name, kind, k, TAIL_MARKERS, RESEND_MARKERS)


@pytest.mark.parametrize(("kind", "k"), _of("recovery-quote"))
@pytest.mark.parametrize("name", INTERACTIVE)
def test_a_recovery_probe_tail_at_the_echo_words_head_does_not_wedge(name, kind, k):
    """A recovery tail starting at its echo word's marker head leaves the resend runnable.

    The probe used to quote its echo word, so this tail began inside the
    quotes and its closing quote opened a new one: every shell sat at PS2
    and swallowed each resend (#853).
    """
    cut_probe(name, kind, k, TAIL_MARKERS, RESEND_MARKERS)


@pytest.mark.parametrize("name", INTERACTIVE)
def test_a_tail_inside_the_calibrations_last_escape_runs_no_terminal_reader(name):
    """The tail cut between the final escape's backslashes used to run ``n`` and wedge (#853).

    ``n`` (node's version manager on GitHub's ubuntu runner) waited for
    input and ate every resend; here a fake ``n`` first on ``PATH`` does the
    same on any machine (:func:`~tests._fixtures.interactive_shell.interactive_shell`).
    """
    k = _inside_the_calibrations_last_escape(LINE).start
    cut_probe(name, "handshake", k, TAIL_MARKERS, RESEND_MARKERS)


@pytest.mark.parametrize("fragment", list(HAZARD_MARKERS))
@pytest.mark.parametrize("name", INTERACTIVE)
def test_a_handshake_tail_inside_the_marker_id_runs_no_terminal_reader(name, fragment):
    """An id ending in a command's name: the tail cut there leaves the resend answering (#853).

    BusyBox's real ``ed`` once ate the resend this way. Fakes of every name
    lead ``PATH``. ``fc`` is a builtin in bash, zsh, ksh and mksh, so there
    the builtin runs instead of the fake, and what it does with no history
    to edit is the shell's own. The handshake is the line cut: its echo
    word ends at the variable, where the resync and recovery words go on
    with ``$?__``.
    """
    line = render_probe("handshake", HAZARD_MARKERS[fragment])
    k = _inside_the_ids_end(line, fragment)
    cut_probe(name, "handshake", k, HAZARD_MARKERS[fragment], RESEND_MARKERS)


@pytest.mark.parametrize("name", INTERACTIVE)
def test_the_whole_recovery_probe_answers_first_try(name):
    """Typed whole once, the recovery probe answers with the digits of an exit status."""
    with interactive_shell(name) as shell:
        reply = _whole(shell, "recovery", history=False)
    assert BashFrame().recover_pattern(TAIL_MARKERS).search(reply)


@pytest.mark.parametrize(("kind", "k"), _of(*_NOUNSET_HAZARDS))
@pytest.mark.parametrize("name", _NOUNSET_SHELLS)
def test_the_hazard_tails_hold_under_nounset(name, kind, k):
    """An rc file may leave ``set -u`` on; a tail at each hazard still leaves the resend working."""
    cut_probe(name, kind, k, TAIL_MARKERS, RESEND_MARKERS, setup="set -u")


@pytest.mark.parametrize("name", INTERACTIVE)
def test_a_handshake_cut_inside_the_echo_word_does_not_wedge_an_interactive_shell(name):
    """A tail cut inside the echo word leaves the next whole line runnable, calibrated (#761)."""
    cut_probe(
        name, "handshake", _inside_the_echo_word(LINE, TAIL_MARKERS), TAIL_MARKERS, RESEND_MARKERS
    )


@pytest.mark.parametrize("kind", ["handshake", "resync"])
@pytest.mark.parametrize("name", INTERACTIVE)
def test_the_whole_line_holds_under_a_readonly_histfile(name, kind):
    """An admin-locked ``readonly HISTFILE``: the whole line answers, and says nothing about it.

    A "read-only variable" complaint would land on the merged stream the
    session parses as the reply.
    """
    with interactive_shell(name, setup="readonly HISTFILE") as shell:
        reply = strip_ansi(_whole(shell, kind))
    assert not re.search(r"read[- ]?only", reply, re.IGNORECASE), f"{name}: {reply!r}"
    if kind == "handshake":
        assert measure(reply, TAIL_MARKERS.ready).mapping == ONLCR, f"{name}: {reply!r}"


_PRINTF_OFF = {"bash": "enable -n printf", "zsh": "disable printf"}


@pytest.mark.parametrize("name", [name for name in _PRINTF_OFF if name in INTERACTIVE])
def test_the_handshake_reaches_ready_without_printf(name, tmp_path):
    """No ``printf``: READY still lands, uncalibrated, with no "not found" on the stream.

    A shell function standing in for printf would not test this: the
    builtin has to be disabled and ``PATH`` emptied, and the shell's own
    lookup has to confirm it is gone before the line is typed.
    """
    with interactive_shell(name, setup=f"PATH={tmp_path}; {_PRINTF_OFF[name]}") as shell:
        status = re.search(
            r"\nTYPE_(\d+)\r*\n", shell.run('type printf >/dev/null 2>&1; echo TYPE_"$?"')
        )
        assert status is not None
        assert status.group(1) != "0", f"{name}: printf is still reachable"
        reply = _whole(shell, "handshake")
    assert measure(reply, TAIL_MARKERS.ready).calibrated is False, f"{name}: {reply!r}"
    assert not re.search(r"not found", reply, re.IGNORECASE), f"{name}: {reply!r}"


def _reachable(shell: InteractiveShell, command: str) -> bool:
    """Whether *shell*'s own ``type`` finds *command*; ask before ``set -e``, which a miss ends."""
    status = re.search(
        r"\nTYPE_(\d+)\r*\n", shell.run(f'type {command} >/dev/null 2>&1; echo TYPE_"$?"')
    )
    assert status is not None
    return status.group(1) == "0"


def _absent_under_errexit(shell: InteractiveShell, command: str) -> None:
    """Confirm *command* is unreachable in *shell*, then turn ``set -e`` on.

    The lookup fails by design, so it runs before errexit is on: under
    ``set -e`` the failing ``type`` would end the shell instead.
    """
    assert not _reachable(shell, command), f"{shell.name}: {command} is still reachable"
    shell.run("set -e")


@pytest.mark.parametrize("name", [name for name in _PRINTF_OFF if name in INTERACTIVE])
def test_the_handshake_reaches_ready_under_errexit_without_printf(name, tmp_path):
    """``set -e`` and no ``printf``: the failed calibration must not end the line (#953).

    Under errexit a failing statement exits the shell, so READY never
    printed and the host timed out as never ready.
    """
    with interactive_shell(name, setup=f"PATH={tmp_path}; {_PRINTF_OFF[name]}") as shell:
        _absent_under_errexit(shell, "printf")
        reply = _whole(shell, "handshake")
    assert measure(reply, TAIL_MARKERS.ready).calibrated is False, f"{name}: {reply!r}"
    assert not re.search(r"not found", reply, re.IGNORECASE), f"{name}: {reply!r}"


@pytest.mark.parametrize("name", [name for name, _ in _STTY_ABSENT_SHELLS])
def test_the_handshake_reaches_ready_under_errexit_without_stty(name, tmp_path):
    """``set -e`` and no ``stty``: the failed ``stty -echo`` must not end the line (#953).

    An empty ``PATH`` removes ``stty`` on every shell outside
    ``_STANDALONE_SHELLS``. It removes ``printf`` too where printf is
    external rather than a builtin (mksh), so the calibration reads
    ``onlcr`` exactly where printf is still reachable and is unmeasured
    elsewhere.
    """
    with interactive_shell(name, setup=f"PATH={tmp_path}") as shell:
        printf_reachable = _reachable(shell, "printf")
        _absent_under_errexit(shell, "stty")
        reply = _whole(shell, "handshake")
    calibration = measure(reply, TAIL_MARKERS.ready)
    assert calibration.calibrated is printf_reachable, f"{name}: {reply!r}"
    if printf_reachable:
        assert calibration.mapping == ONLCR, f"{name}: {reply!r}"
    assert not re.search(r"not found", reply, re.IGNORECASE), f"{name}: {reply!r}"


@pytest.mark.parametrize(
    ("name", "argv"), _STTY_ABSENT_SHELLS, ids=[n for n, _ in _STTY_ABSENT_SHELLS]
)
def test_restoring_echo_survives_errexit_without_stty(name: str, argv: list[str], tmp_path):
    """``restore_interactive`` under ``set -e`` with no ``stty`` lets the line go on (#953)."""
    restore = BashFrame().restore_interactive()
    assert restore is not None
    env = {**os.environ, "PATH": str(tmp_path)}
    assert _stty_lookup(argv, env=env) == "GONE", f"{name}: stty is still reachable"
    proc = _run(argv, f"set -e; {restore}; echo AFTER", env=env)
    assert proc.stdout == "AFTER\n", f"{name}: rc={proc.returncode} {proc.stdout!r}"
    assert proc.stderr == "", f"{name}: {proc.stderr!r}"


@pytest.mark.parametrize("name", INTERACTIVE)
def test_calibration_reads_identity_without_onlcr(name):
    """A terminal with ``onlcr`` off before the shell starts measures ``identity``."""
    with interactive_shell(name, onlcr=False) as shell:
        reply = _whole(shell, "handshake")
    assert measure(reply, TAIL_MARKERS.ready).mapping == IDENTITY, f"{name}: {reply!r}"
