"""Command-frame and shell-dialect contracts on real telnet sessions.

Migrated from ``tests/busybox/test_ash_frame_payloads.py`` and
``tests/busybox/test_ash_file_op_payloads.py`` (both deleted in Task 9), which
ran otto's own payloads under each matrix row's ash inside a chroot, with no
transport underneath them.

THE REAL SESSION IS THE FRAME. Every exec below crosses a live telnet channel
into a booted BusyBox, so the handshake, the BEGIN/END brackets and the exit
code baked into the END marker are proved by the call succeeding at all --
which is why the tests here pin the things a successful call does NOT prove:
that the code crossing the frame is the command's own and not the frame's,
that output arrives in order with no sentinel bleed, that the two payloads ash
is entitled to reject are survivable, and that a glob is expanded by the
device rather than by otto.

WHAT DID NOT MIGRATE, AND WHY. The harness's handshake-without-stty row
removed ``stty`` from its root; the equivalent here would be mutating a shared
live guest, which this bed forbids. That arm stays unit-level (Task 9's
disposition table); the stty-PRESENT arm is exercised by every session in this
directory.
"""

import pytest

from otto.host.command_frame import BashFrame, SessionMarkers
from otto.utils import Status

pytestmark = [pytest.mark.asyncio]

# Set before the history payload runs so the payload has something to
# overwrite. See test_history_suppression_runs_and_sets_histfile.
_HISTFILE_SENTINEL = "/tmp/otto-history-sentinel"

# Where the glob rows stage their files, and where the orphan row leaves its
# proof-of-arrival. Under /tmp, which is the only place these guests give a
# test to write (their /etc holds five entries and nothing else).
_GLOB_DIR = "/tmp/otto-glob"
_ORPHAN_MARKER = "/tmp/otto-orphan-ran"

# The command the orphan row abandons, and the pattern that looks for its
# survivor. `[s]leep` is the classic self-exclusion: the grep process's own
# argv contains the literal brackets, so it never matches its own pattern --
# which keeps the count honest without a second `grep -v grep` in the pipe.
_ORPHAN_COMMAND = "sleep 300"
_ORPHAN_PATTERN = "[s]leep 300"

# Session id for the synthetic markers the recover-probe row builds. Chosen not
# to collide with any live session's own id, though nothing here could be
# mistaken for one: the RECOVER token is disjoint from the END token by
# construction, which is the very property recover() exists to have.
_RECOVER_PROBE_SESSION_ID = "T8RECOVERPROBE"

# The status the recover probe must carry back. Neither 0 (what a blank or
# unexpanded status could coincidentally look like) nor a value ash produces by
# accident, and set in a SUBSHELL so the shell survives to run the probe.
_RECOVER_PROBE_CODE = 7


@pytest.mark.parametrize(("cmd", "code"), [("true", 0), ("false", 1), ("sh -c 'exit 42'", 42)])
async def test_the_exit_code_that_crosses_the_frame_is_the_commands(guest, cmd, code):
    """The code baked into the END marker is the command's, not the frame's.

    42 is in the list because 0 and 1 are also what a frame that lost the code
    entirely would plausibly report: ``retcode`` defaults to -1 and the two
    common shells' own statuses are 0 and 1. A code no part of the machinery
    would invent is what separates "the marker carried it" from "something
    downstream guessed".
    """
    host, version = guest
    res = await host.exec(cmd)
    assert res.retcode == code, (
        f"{host.element} (BusyBox {version}) returned {res.retcode} for {cmd!r}, "
        f"not {code} (status={res.status}, output={res.value!r})"
    )


async def test_the_recover_probes_baked_exit_code_is_the_preceding_commands(guest):
    """The RECOVER marker carries the PRECEDING command's status, not any status.

    A DIFFERENT PAYLOAD from the one above, built by a different method, and it
    needs its own measurement. ``recover()`` bakes the status into the RECOVER
    token rather than into ``end_prefix`` precisely so the two cannot collide,
    and so a dead shell (an echo, a REPL) can only reflect the literal dollar-?
    back instead of digits.

    THE ORPHAN ROW ALREADY PROVES THE DIGITS HALF and nothing proves this one.
    Every timed-out command in this module drives the real recovery, which
    cannot complete unless the compiled RECOVER pattern matches digits on the
    wire -- so "a number arrives" is live-covered five times over. What that
    says nothing about is WHICH number: a frame that baked a constant, or a
    shell that clamped the status, recovers exactly as well. 7 is the
    discriminator.

    Both halves come from the product -- the payload from ``recover()``, the
    search from ``recover_pattern()`` -- so a change to either is caught here
    rather than by a hand-retyped regex that would keep agreeing with itself.
    The markers are synthetic because the live session's are private to it;
    what is being measured is the guest's shell expanding a status into that
    text, which is the same act the real recovery depends on.

    ALL FIVE VERSIONS: this is the recovery payload, and recovery semantics
    take the full sweep on this bed by policy rather than being sampled.

    The trailing newline is stripped for the reason the history row spells out
    -- ``recover()`` ends its payload with one because it is written straight
    into a session, and inside a framed command that newline strands the
    frame's own trailing echo.
    """
    host, version = guest
    frame = BashFrame()
    markers = SessionMarkers.for_session(_RECOVER_PROBE_SESSION_ID)
    probe = frame.recover(markers).rstrip("\n")
    res = await host.exec(f"(exit {_RECOVER_PROBE_CODE}); {probe}")

    match = frame.recover_pattern(markers).search(res.value)
    assert match, (
        f"no RECOVER marker with a status came back from {host.element} "
        f"(BusyBox {version}) -- the shell did not expand a status into "
        f"recover()'s payload at all: {res.value!r}"
    )
    assert match.group(1) == str(_RECOVER_PROBE_CODE), (
        f"{host.element} (BusyBox {version}) reported {match.group(1)} in the "
        f"RECOVER marker where the preceding command exited "
        f"{_RECOVER_PROBE_CODE} -- recover()'s status does not survive here, "
        f"which is a real ash delta and belongs in AshFrame"
    )


async def test_command_output_is_bracketed_in_order_with_no_marker_bleed(guest):
    """Two lines, in the order the command emitted them, and nothing else.

    Equality rather than membership: the frame writes its BEGIN sentinel, the
    command, and its END sentinel onto one line of a pty that echoes what it
    is fed, so "alpha is in the output" would hold for a parser that returned
    the entire raw buffer. What must come back is exactly the command's own
    two lines, in order.
    """
    host, version = guest
    res = await host.exec("echo alpha; echo beta")
    assert res.value.splitlines() == ["alpha", "beta"], (
        f"{host.element} (BusyBox {version}) answered {res.value!r} -- either the "
        f"lines are out of order or a sentinel/prompt leaked into the parsed output"
    )


async def test_history_suppression_runs_and_sets_histfile(guest):
    """The frame's history payload runs under real ash, sets HISTFILE, says nothing.

    THE SENTINEL IS LOAD-BEARING, not scaffolding. otto's own readiness
    handshake already prepends this payload to the first line it writes into a
    fresh shell, so HISTFILE is ALREADY ``/dev/null`` on every session in this
    directory -- measured. Asserting that without first setting HISTFILE to
    something else is a guard that passes with the payload deleted: it would
    be reading the handshake's work and calling it this payload's.

    The empty-output half is the pty's version of the harness's "stderr is
    empty" assertion. On a pty the two streams are one, and the payload's
    ``2>/dev/null`` guards exist precisely so a complaint from a shell that
    dislikes one of its clauses cannot reach the stream otto parses -- so
    output equal to the single expected line is the strongest available form
    of "it emitted nothing of its own".

    ONE LINE, deliberately. The frame wraps a command as ``echo BEGIN; <cmd>;
    echo END$?__`` on a single line, so a payload that ends with a newline
    strands the frame's trailing ``; echo`` at the start of a fresh line and
    the shell answers with a syntax error instead of a marker -- measured on
    this bed as a 30-second timeout. ``quiet_history()`` already ends in
    ``'; '``, so it concatenates directly with no separator of our own.
    """
    host, version = guest
    payload = f'HISTFILE={_HISTFILE_SENTINEL}; {BashFrame().quiet_history()}echo "H=[$HISTFILE]"'
    res = await host.exec(payload)
    assert res.retcode == 0, (
        f"the history payload failed on {host.element} (BusyBox {version}): "
        f"rc={res.retcode} {res.value!r}"
    )
    assert res.value == "H=[/dev/null]", (
        f"{host.element} (BusyBox {version}) answered {res.value!r}. Either the "
        f"payload did not overwrite HISTFILE={_HISTFILE_SENTINEL}, or one of its "
        f"clauses printed a complaint into the stream otto parses"
    )


async def test_ash_rejects_set_plus_o_history_but_the_shell_survives(guest):
    """Why the history payload needs its guards, measured on the real shell.

    ash's ``set`` has no ``+o history`` at all -- it is a bash shell option --
    so every one of these builds rejects it outright. That is harmless BY
    DESIGN: the payload's ``command`` prefix strips the special-builtin status
    that would otherwise abort a non-interactive shell, and ``|| :`` swallows
    the code. But a task whose deliverable is a measurement has to record what
    it measured, not just that the net effect was survivable.

    ``command set +o history``, mirroring the payload's own construction, so
    what is measured is the shape otto actually sends. The rejection reaches
    ``value`` rather than a separate stderr for the same reason the test above
    can assert emptiness: on a pty there is one stream.
    """
    host, version = guest
    res = await host.exec("command set +o history; echo AFTER=$?")
    assert "illegal option" in res.value, (
        f"{host.element} (BusyBox {version}) was expected to reject `+o history` "
        f"outright: {res.value!r}"
    )
    assert "history" in res.value, (
        f"{host.element}'s rejection did not name the option it rejected: {res.value!r}"
    )
    after = [line for line in res.value.splitlines() if line.startswith("AFTER=")]
    assert after, (
        f"nothing after the rejection reported a status on {host.element}, so the "
        f"shell did not survive it: {res.value!r}"
    )
    assert after[0] != "AFTER=0", (
        f"the rejection must be a real non-zero status -- {host.element} said {res.value!r}"
    )
    alive = await host.exec("echo alive")
    assert alive.value.strip() == "alive", (
        f"the rejected `set +o history` took {host.element}'s shell down with it: {alive.value!r}"
    )


async def test_glob_expands_and_filters_under_live_ash(guest):
    """The device expands the pattern; the payload's guard drops the unmatched one.

    Two halves, both load-bearing, both from the harness. SELECTION: the
    non-matching file is what makes this a test of selection rather than of
    listing -- a payload that expanded to the whole directory would satisfy an
    assertion that only counted the matches. EMPTINESS: POSIX sh leaves an
    unmatched pattern LITERAL, and the payload's ``[ -e ]`` guard is the only
    thing between that literal and a caller who believes it holds a real path.

    Driven through ``host.glob`` rather than through a retyped shell line, so
    the payload under test is the product's own -- the principle the two
    harness modules were built on, and the reason a change otto's own glob
    cannot support fails here rather than in the field.
    """
    host, version = guest
    prep = await host.exec(
        f"rm -rf {_GLOB_DIR} && mkdir -p {_GLOB_DIR} && "
        f"touch {_GLOB_DIR}/messages1 {_GLOB_DIR}/messages2 {_GLOB_DIR}/other"
    )
    assert prep.retcode == 0, f"could not stage {_GLOB_DIR} on {host.element}: {prep.value!r}"
    try:
        matched = await host.glob(f"{_GLOB_DIR}/messages*")
        assert sorted(matched) == [f"{_GLOB_DIR}/messages1", f"{_GLOB_DIR}/messages2"], (
            f"{host.element} (BusyBox {version}) expanded `messages*` to {matched} -- "
            f"either the pattern came back unexpanded, or the expansion swept in "
            f"the non-matching file"
        )
        missed = await host.glob(f"{_GLOB_DIR}/nomatch*")
        assert missed == [], (
            f"{host.element} answered {missed} for a pattern that matches nothing; "
            f"a caller handed that literal believes a file exists that does not"
        )
    finally:
        await host.exec(f"rm -rf {_GLOB_DIR}")


async def test_a_timed_out_command_leaves_no_orphan_process(guest):
    """A command that outlives its timeout must not strand a process on the guest.

    Migrated from the rootfs harness's grandchildren-reap test, which measured
    the same intent against a chroot it could kill by process group. Here the
    recovery is otto's: a timed-out exec is answered by the session's own
    resync, and what this asserts is that the abandoned command is gone from
    the DEVICE afterwards, not merely gone from otto's bookkeeping.

    ALL FIVE VERSIONS, by policy (Chris, 2026-08-21). Recovery semantics ride
    version-variant applet and shell behavior, so this is one of the sweeps
    that does not get sampled.

    TWO CONTROLS, because "no survivors" is also what a probe that cannot see
    anything looks like, and what a command that never ran looks like:

    * the instrument control runs the exact pattern against a process this
      test starts and then reaps, so the count is shown answering 1 and then 0
      on this guest, on this run;
    * the arrival control is a marker file the abandoned command touches
      before it sleeps. Without it a timeout that fired before the command
      ever reached the guest -- a slow login, a wedged pty -- would leave
      nothing to find and pass.

    The timeout does NOT raise. ``exec`` answers a timeout with
    ``Status.Error`` and ``timed_out=True`` (the session recovers rather than
    propagating), so that is what is asserted; a ``pytest.raises`` here would
    fail on every row.

    Nothing reads a clock. The five-second bound only has to be shorter than a
    three-hundred-second sleep, so machine load cannot flip the outcome.
    """
    host, version = guest
    # The marker is proved GONE, not merely removed: a stale one left by an
    # earlier red run would make the arrival control below answer RAN for a
    # command that never reached this guest.
    cleared = await host.exec(
        f"rm -f {_ORPHAN_MARKER}; [ -f {_ORPHAN_MARKER} ] && echo STALE || echo GONE"
    )
    assert cleared.value.strip() == "GONE", (
        f"could not clear {_ORPHAN_MARKER} on {host.element} ({cleared.value!r}), so "
        f"the arrival control below would read a stale marker as this run's proof"
    )

    control = await host.exec(
        f"{_ORPHAN_COMMAND} & c=$!; sleep 1; ps | grep -c '{_ORPHAN_PATTERN}'; "
        f"kill $c 2>/dev/null; wait $c 2>/dev/null; ps | grep -c '{_ORPHAN_PATTERN}'"
    )
    counts = control.value.split()
    assert len(counts) == 2, (
        f"the instrument control on {host.element} answered {control.value!r}, not "
        f"two counts -- `ps` or `grep -c` is not behaving as this row assumes"
    )
    assert int(counts[0]) >= 1, (
        f"`ps | grep -c '{_ORPHAN_PATTERN}'` did not see a {_ORPHAN_COMMAND!r} this "
        f"test had just started on {host.element} ({control.value!r}), so the count "
        f"below cannot mean the timeout reaped anything"
    )
    assert int(counts[1]) == 0, (
        f"the control's own {_ORPHAN_COMMAND!r} survived being killed on "
        f"{host.element} ({control.value!r}) -- the count cannot come back down, so "
        f"a zero below would say nothing"
    )

    res = await host.exec(f"touch {_ORPHAN_MARKER}; {_ORPHAN_COMMAND}", timeout=5)
    assert res.timed_out, (
        f"{_ORPHAN_COMMAND!r} did not time out on {host.element} (BusyBox {version}): "
        f"status={res.status} timed_out={res.timed_out} value={res.value!r}"
    )
    assert res.status is Status.Error, (
        f"a timed-out command must answer Status.Error on {host.element}, not "
        f"{res.status} ({res.value!r})"
    )

    after = await host.exec(
        f"[ -f {_ORPHAN_MARKER} ] && echo RAN || echo NEVER; "
        f"ps | grep -c '{_ORPHAN_PATTERN}'; rm -f {_ORPHAN_MARKER}"
    )
    lines = after.value.splitlines()
    assert len(lines) == 2, f"the survivor probe answered {after.value!r}, not two lines"
    assert lines[0] == "RAN", (
        f"the abandoned command never reached {host.element} -- the timeout fired "
        f"before it ran, so 'no survivors' below would prove nothing ({after.value!r})"
    )
    assert int(lines[1]) == 0, (
        f"{host.element} (BusyBox {version}) is still running {int(lines[1])} "
        f"{_ORPHAN_COMMAND!r} after otto abandoned it: the timeout recovered otto's "
        f"session and stranded the process on the device"
    )
