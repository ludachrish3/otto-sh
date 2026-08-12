"""Run BashFrame's OWN payloads under each matrix row's real ash.

`BashFrame` brackets a command with `echo` markers and bakes `$?` into the END
marker (and, separately, into the RECOVER marker `recover()` emits). Every
construct in that scheme is POSIX, so it is expected to work under ash
unchanged — but "expected" is what this tier exists to replace. The payloads
are taken from the frame itself (`BashFrame().frame(...)`, `.handshake(...)`,
`.recover(...)`, `.quiet_history()`) rather than retyped, so a change to the
frame that ash cannot support fails here instead of in the field. That
principle extends to the PARSE half too: the exit-code and recover tests below
search with `frame.end_pattern(m)` / `frame.recover_pattern(m)` — the same
compiled patterns the product parses with — rather than a hand-retyped regex,
so a change to either pattern is caught here as well as in
`tests/unit/host/test_command_frame.py`.

Two payloads (`handshake`'s `stty -echo 2>/dev/null` and `quiet_history()`'s
`2>/dev/null`-guarded clauses) redirect to `/dev/null`. An earlier version of
this rootfs had none at all, so those redirects failed their own setup step
and silently took down the WHOLE wrapped statement — the payload never ran,
and every test here passed anyway measuring nothing. `tests/_fixtures/
busybox_rootfs.py` now provides `/dev/null` (a plain regular file — see
`_install_dev_null`), so those two payloads actually execute, and their
stderr is now a real, assertable signal: both are expected to produce NONE,
because ash has nothing to say when the redirect itself works.

Everything else in `otto.host.command_frame` (`parse_output`,
`extract_retcode`, `marks_begin`) is pure Python parsing with no shell
involvement on the other end of the wire — already covered by
`tests/unit/host/test_command_frame.py` and out of scope for a rootfs tier
that costs a chroot per test.

Parametrized over the full `BUSYBOX_MATRIX` directly, never pre-filtered by
`can_run`: a filtered row list makes a row with no qemu interpreter vanish
from the run silently instead of failing loudly, which is exactly the defect
`tests/busybox/test_applet_resolution.py` was written to avoid repeating.
`require_interpreter(release.arch)` and `require_userns()` are called INSIDE
each test body instead, so a missing prerequisite names itself rather than
erasing a row from the summary.
"""

import re

import pytest

from otto.host.command_frame import BashFrame, SessionMarkers
from tests._fixtures.busybox import BUSYBOX_MATRIX, require_interpreter
from tests._fixtures.busybox_rootfs import busybox_rootfs, require_userns, run_in_rootfs

pytestmark = [pytest.mark.busybox]

# Built through `for_session`, the same constructor a real session uses, so the
# markers here have the shape the frame is written against rather than a
# hand-rolled approximation of it.
_MARKERS = SessionMarkers.for_session("T")


@pytest.mark.parametrize("release", BUSYBOX_MATRIX, ids=[r.version for r in BUSYBOX_MATRIX])
def test_the_frames_command_brackets_survive_ash(release):
    """Both markers must appear, in order, around the command's own output."""
    require_interpreter(release.arch)
    require_userns()
    frame = BashFrame()
    payload = frame.frame("echo MIDDLE", _MARKERS)

    with busybox_rootfs(release) as root:
        result = run_in_rootfs(root, payload)

    assert result.returncode == 0, f"the payload did not run: {result.stderr}"
    begin = result.stdout.find(_MARKERS.begin)
    middle = result.stdout.find("MIDDLE")
    end = result.stdout.find(_MARKERS.end_prefix)
    assert -1 not in (begin, middle, end), (
        f"BusyBox {release.version} ash did not reproduce the frame's brackets: {result.stdout!r}"
    )
    assert begin < middle < end, (
        f"markers out of order under {release.version} ash: {result.stdout!r}"
    )


@pytest.mark.parametrize("release", BUSYBOX_MATRIX, ids=[r.version for r in BUSYBOX_MATRIX])
@pytest.mark.parametrize(
    ("cmd", "code"),
    [("true", 0), ("(exit 3)", 3), ("(exit 42)", 42), ("/bin/false", 1)],
    ids=["rc0", "rc3", "rc42", "false-applet"],
)
def test_the_exit_code_baked_into_the_end_marker_is_the_commands(release, cmd, code):
    """`$?` must expand to the command's status, not the echo's.

    Four cases, not one: a frame that emitted a literal 0 would pass a
    zero-only check, and that is the failure mode worth catching — every
    command otto runs reports success. Three of the four (`true`/`0`,
    `(exit 3)`, `(exit 42)`) are synthetic — a subshell forcing `$?` to a
    chosen value — so `false-applet` is included specifically because it is
    NOT synthetic: it is a real BusyBox applet's own exit status (1), not a
    number this test picked.

    Spelled `/bin/false`, by ABSOLUTE PATH, and that spelling is the case.
    A bare `false` never reaches the applet: measured on all five rows,
    `type false` answers `false is a shell builtin`, so ash resolves it
    internally and `/bin/busybox` is never exec'd. `/bin/false` is a real
    `--install -s` symlink to busybox on every row and exits 1 through it.
    An earlier draft of this comment used the bare spelling and still called
    it an applet's status; it was not one, and the absolute path is what
    makes the sentence true.

    The reviewer who found the `/dev/null` defect measured that a nonexistent
    command (127) behaves identically to the synthetic cases across all five
    rows — no live delta — but a case whose status came from a command rather
    than from this test is cheap to keep and worth having.

    `(exit {code})` — a SUBSHELL, not a bare `exit {code}` (which the task
    brief's draft used verbatim). Measured directly under bash, dash and sh
    before writing this comment: a bare `exit N` inside `frame()`'s
    `cmd; echo "END$?__"` compound line terminates the WHOLE shell process on
    every one of them, so the trailing echo never runs and the payload never
    emits an END marker at all — on any shell, not just ash. That is a test
    bug, not an ash delta: `(exit N)` sets `$?` for the rest of the script
    without tearing it down, which is what this test actually needs to probe
    `$?`-expansion rather than shell-termination semantics every POSIX shell
    already shares.
    """
    require_interpreter(release.arch)
    require_userns()
    frame = BashFrame()
    payload = frame.frame(cmd, _MARKERS)

    with busybox_rootfs(release) as root:
        result = run_in_rootfs(root, payload)

    match = frame.end_pattern(_MARKERS).search(result.stdout)
    assert match, f"no END marker with a status under {release.version} ash: {result.stdout!r}"
    if match.group(1) == "127" and code != 127 and cmd.startswith("/"):
        # 127 is the shell's own "not found" status, not a `$?`-survival
        # failure: the false-applet case reaches for `/bin/false` by absolute
        # path (see this test's docstring), and a build whose userland lacks
        # that applet or its `--install -s` symlink would report 127 here for
        # a reason that has nothing to do with AshFrame's marker scheme. The
        # generic message below reads as "write an AshFrame override", which
        # is the wrong fix for a missing applet — this branch exists so that
        # misdiagnosis can't happen, not because it has fired on any of the
        # five matrix rows measured so far (`/bin/false` is present on all
        # five; see the docstring above).
        #
        # Scoped to the absolute-path case ON PURPOSE. An earlier version
        # tested only the status, so a 127 from `true` or `(exit 3)` — which
        # exec nothing and cannot go missing — was reported as a missing
        # applet, sending the reader to audit the rootfs over what could only
        # be a frame defect. Measured by emitting a literal 127 from
        # `frame()`: the `rc3` row failed with "the applet ... is missing from
        # this rootfs", for a case with no applet in it. That is this very
        # misdiagnosis class, inverted, so the branch has to be as narrow as
        # the claim it makes.
        pytest.fail(
            f"BusyBox {release.version} ash reported exit 127 running {cmd!r} — "
            f"that is this build's own 'not found' status, meaning the applet "
            f"(or its --install -s symlink) is missing from this rootfs, not "
            f"a frame delta. Investigate the rootfs/artifact, not AshFrame."
        )
    assert match.group(1) == str(code), (
        f"BusyBox {release.version} ash reported exit {match.group(1)} where the "
        f"command exited {code} — the frame's `$?` does not survive here, which "
        f"is a real ash delta and belongs in AshFrame"
    )


@pytest.mark.parametrize("release", BUSYBOX_MATRIX, ids=[r.version for r in BUSYBOX_MATRIX])
def test_the_recover_probes_baked_exit_code_is_the_preceding_commands(release):
    """`recover()` bakes `$?` into the RECOVER marker, not `end_prefix` — prove it separately.

    `BashFrame.recover`'s own comment (command_frame.py:199-208) explains why:
    a real shell must emit `..._RECOVER__<digits>__`, distinct from
    `end_pattern`, while a dead echo/REPL can only reflect the literal text
    `$?` back. That is a claim about a DIFFERENT payload than `frame()`'s END
    marker (previous test), built by a different method with its own
    docstring, so it needs its own measurement rather than riding on the END
    marker's.

    `(exit 7)` runs in a SUBSHELL: it sets `$?` to 7 for the rest of the
    script without terminating it, the way a bare `exit 7` would. 7 is
    neither 0 (what an unset/blank `$?` might coincidentally look like) nor
    a value ash could produce by accident, so the digit-equality assertion
    below cannot be satisfied by coincidence.
    """
    require_interpreter(release.arch)
    require_userns()
    frame = BashFrame()
    payload = "(exit 7)\n" + frame.recover(_MARKERS)

    with busybox_rootfs(release) as root:
        result = run_in_rootfs(root, payload)

    match = frame.recover_pattern(_MARKERS).search(result.stdout)
    assert match, (
        f"no RECOVER marker with a status under {release.version} ash — `$?` did "
        f"not expand into it: {result.stdout!r}"
    )
    assert match.group(1) == "7", (
        f"BusyBox {release.version} ash reported {match.group(1)} in the RECOVER "
        f"marker where the preceding command exited 7 — recover()'s `$?` does "
        f"not survive here, which is a real ash delta and belongs in AshFrame"
    )


@pytest.mark.parametrize("release", BUSYBOX_MATRIX, ids=[r.version for r in BUSYBOX_MATRIX])
def test_the_handshake_reaches_ready_with_stty_present_and_clean_stderr(release):
    """Baseline direction: `stty` IS present, and its own stderr is genuinely swallowed.

    `stty` is present on all five prebuilts (`--install -s` symlinks every
    applet the build has, and every row in `BUSYBOX_MATRIX` has one) — the
    companion test below is what actually removes it. This test is the
    OTHER half of that pair: with `/dev/null` real, `stty -echo`'s own
    failure (there is no controlling tty on a piped subprocess — measured
    below) is genuinely discarded by `2>/dev/null` rather than merely
    assigned to a redirect target that doesn't exist, so this is the first
    point at which "stty's stderr is swallowed" is an assertable fact rather
    than an assumption.
    """
    require_interpreter(release.arch)
    require_userns()
    frame = BashFrame()

    with busybox_rootfs(release) as root:
        assert (root / "bin" / "stty").exists(), (
            "the baseline direction needs stty actually present to mean anything"
        )
        result = run_in_rootfs(root, frame.handshake(_MARKERS))

    assert _MARKERS.ready in result.stdout, (
        f"BusyBox {release.version} ash did not reach READY with stty present: "
        f"rc={result.returncode} {result.stdout!r} {result.stderr!r}"
    )
    assert result.stderr == "", (
        f"stty's own stderr must be swallowed by `2>/dev/null`, not leak into "
        f"the handshake's output: {result.stderr!r}"
    )


@pytest.mark.parametrize("release", BUSYBOX_MATRIX, ids=[r.version for r in BUSYBOX_MATRIX])
def test_the_handshake_does_not_kill_an_ash_that_lacks_stty(release):
    """The hostile condition is INJECTED, never inherited.

    `stty` is present on all five `BUSYBOX_MATRIX` rows — measured, not
    assumed (see the companion baseline test above) — so a test that merely
    ran the handshake as-is could never have exercised a shell that lacks
    `stty`, whatever its name claimed. Task 2's `_EXPECTED_STANDALONE_SHELL`
    table (`tests/busybox/test_applet_resolution.py`) already established
    that none of the five prebuilts is a standalone shell: ash cannot resolve
    an applet without a matching PATH entry, only `--install -s`'s symlinks
    make one reachable at all. That coupling is exactly what makes deleting
    the `/bin/stty` symlink this tier itself installed a GENUINE removal of
    the applet, not a cosmetic one — ash has no built-in fallback to fall
    back to.

    `stty -echo 2>/dev/null` must leave the shell alive either way: the frame
    already redirects stty's stderr, so the claim under test is that the
    surrounding shell survives and still prints READY, not that `stty` works.
    """
    require_interpreter(release.arch)
    require_userns()
    frame = BashFrame()

    with busybox_rootfs(release) as root:
        (root / "bin" / "stty").unlink()
        result = run_in_rootfs(root, frame.handshake(_MARKERS))

    assert _MARKERS.ready in result.stdout, (
        f"BusyBox {release.version} ash did not reach the READY marker through "
        f"the handshake with stty deleted: rc={result.returncode} "
        f"{result.stdout!r} {result.stderr!r}"
    )
    assert result.stderr == "", (
        f"the shell's own 'not found' complaint must be swallowed by "
        f"`2>/dev/null` too, not just stty's: {result.stderr!r}"
    )


@pytest.mark.parametrize("release", BUSYBOX_MATRIX, ids=[r.version for r in BUSYBOX_MATRIX])
def test_history_suppression_leaves_the_shell_alive_and_sets_histfile(release):
    """`quiet_history()` already claims ash compatibility in its own comment.

    That claim was UNTESTED against a real ash, and the first version of this
    test could not have tested it either: this rootfs had no `/dev`, so every
    `2>/dev/null` in `quiet_history()`'s payload failed its own redirect setup
    and took the whole guarded clause down with it — the payload never ran,
    and a docstring here previously claimed HISTFILE's assignment was "not
    observable" as a result. It is observable, and always was: `echo
    $HISTFILE` needs no `/dev` at all. It read empty only because the payload
    that would have set it never executed.

    With `/dev/null` now real (`tests/_fixtures/busybox_rootfs.py`), three
    things are asserted, all newly true: the shell survives (a payload that
    aborts it takes a working host offline, which is the whole reason this
    test exists), `$HISTFILE` was actually assigned `/dev/null` by the
    payload's first clause, and stderr is empty — `quiet_history()`'s
    `2>/dev/null` guards exist specifically to swallow the errors this
    payload risks on stricter shells (see the Rule 1 / Rule 2 comment on
    `BashFrame.quiet_history`), and with a real `/dev/null` that swallowing
    is now a checkable fact rather than an assumption.

    This does NOT claim history is actually neutralized in an interactive
    sense (no history file gets written by a non-interactive `sh -c`, with or
    without HISTFILE) — that is a claim about interactive shell behavior,
    already pinned per-shell, including busybox-sh when installed, by
    `tests/unit/host/test_history_suppression_portability.py`. What is new
    here is that the payload runs at all and that HISTFILE ends up set.
    """
    require_interpreter(release.arch)
    require_userns()
    frame = BashFrame()
    payload = frame.quiet_history() + '\necho "HISTFILE=[$HISTFILE]"\necho STILL_ALIVE\n'

    with busybox_rootfs(release) as root:
        result = run_in_rootfs(root, payload)

    assert "STILL_ALIVE" in result.stdout, (
        f"history suppression killed BusyBox {release.version} ash: "
        f"rc={result.returncode} {result.stdout!r} {result.stderr!r}"
    )
    assert "HISTFILE=[/dev/null]" in result.stdout, (
        f"quiet_history()'s first clause must actually assign HISTFILE: {result.stdout!r}"
    )
    assert result.stderr == "", (
        f"quiet_history()'s `2>/dev/null` guards must swallow every error this "
        f"payload risks, not leak one into the handshake stream: {result.stderr!r}"
    )


@pytest.mark.parametrize("release", BUSYBOX_MATRIX, ids=[r.version for r in BUSYBOX_MATRIX])
def test_ash_rejects_set_plus_o_history_but_the_shell_survives(release):
    """The real delta this task set out to measure, now that payloads actually run.

    ash's `set` has no `+o history` option at all: every one of the five
    BusyBox builds rejects it outright with `set: illegal option +o history`.
    `set -o`/`set +o` toggle a handful of ash-specific flags (`noglob`,
    `xtrace`, ...); `history` is bash's own shell option and ash never
    defines it. This is harmless BY DESIGN — `quiet_history()`'s `command`
    prefix and `|| :` guards exist for exactly this class of rejection (see
    `BashFrame.quiet_history`'s Rule 1 comment) — but a task whose deliverable
    is a measurement has to record what it measured, not just that the net
    effect was survivable.

    Pinned directly against real ash rather than folded into the history
    test above, because the two are different claims: the history test
    proves the WRAPPED payload survives; this one proves WHY there is
    something to survive in the first place. `command set +o history`
    (mirroring `quiet_history()`'s own construction) rather than a bare
    `set +o history`, so what is measured is the same shape the product
    actually sends.
    """
    require_interpreter(release.arch)
    require_userns()

    with busybox_rootfs(release) as root:
        result = run_in_rootfs(root, 'command set +o history; echo "AFTER=$?"\n')

    assert "illegal option" in result.stderr, (
        f"BusyBox {release.version} ash was expected to reject `+o history` "
        f"outright: rc={result.returncode} {result.stdout!r} {result.stderr!r}"
    )
    assert "history" in result.stderr, (
        f"BusyBox {release.version} ash's rejection was expected to name the "
        f"option it rejected: {result.stderr!r}"
    )
    assert re.search(r"AFTER=\d+", result.stdout), (
        f"the rejection must not abort the shell — a later command must still "
        f"run and report a status: {result.stdout!r}"
    )
