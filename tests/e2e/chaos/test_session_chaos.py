"""Host/session chaos: interrupt a live remote command at a seeded offset
inside the command-running phase window; force via a SIGINT-immune remote;
characterize the nohup survivor. BedHygiene (autouse) asserts the leased
host is left clean; here we add the remote-reaped / remote-survives assertions the
hygiene diff cannot express (a foreground child reaped by PTY-HUP is gone
either way; a nohup'd one is SUPPOSED to remain, so it is not a leftover).

otto's ``run`` verb types commands into a persistent PTY *shell*
(``SshSession._open`` in ``src/otto/host/session.py`` opens a bare shell with
no command; commands are written to its stdin). That shell parses each line
like any interactive shell would -- including stripping everything after a
``#`` -- before exec'ing it, so a trailing ``# marker`` comment is invisible
in the spawned remote process's argv and can NEVER be grepped for (verified
live: ``printf 'sleep 5 # marker\\n' | bash`` spawns argv ``sleep 5``, no
trace of ``marker``). Every remote-process probe below therefore needles on
something that genuinely appears in argv -- a distinct ``sleep`` DURATION per
test/role, since durations are the only per-test signal ``pgrep -af`` can
actually see. The bracket-trick first character (``[s]leep ...``) is kept
throughout so the probe's own invocation never self-matches.
"""

import re
import time

import pytest

from otto.logger.mode import LogMode
from tests.e2e.chaos._bed import run_probe
from tests.integration.chaos._driver import BANNER, spawn_otto

pytestmark = [
    pytest.mark.chaos,
    pytest.mark.stability,
    pytest.mark.integration,
    pytest.mark.xdist_group("chaos_lane"),
    pytest.mark.timeout(300),
]

# Argv-visible, unique-per-test marker (see module docstring: trailing shell
# comments are stripped by the PTY shell before exec, so uniqueness has to
# live in the duration itself). 311/312/313/314 are chosen clear of every
# other sleep duration used across the chaos suite: test_connection_drop.py
# uses 120, and tests/integration/chaos/test_signal_run.py's tag-based
# markers land on 301-305 (`sleep 3{tag}.{pid}`) -- close enough in the same
# "sleep 3xx" family that reusing that range risked a substring collision if
# that tier-2 suite is ever pointed at the same live bed concurrently.
_SLEEP = "sleep 311"  # seeded-SIGINT test: PTY-HUP-reaped foreground child


def _remote_pids(element: str, needle: str) -> list:
    # bracket-trick first char so the probe's own shell never self-matches
    pat = f"[{needle[0]}]{needle[1:]}"

    async def _find(host):
        result = await host.exec(f"pgrep -af '{pat}' || true", timeout=30, log=LogMode.QUIET)
        out = result.value or ""
        return [ln for ln in out.splitlines() if ln.strip()]

    return run_probe(element, _find)


def test_seeded_sigint_mid_command_cleans_up(chaos_bed, chaos_rng, tmp_path):
    from tests.e2e.chaos._seed import offset_in

    p = spawn_otto(
        ["host", chaos_bed.target.host_id, "run", _SLEEP, "--timeout", "300"],
        xdir=tmp_path,
        target=chaos_bed.target,
    )
    p.wait_for_log(re.escape(f"| {_SLEEP}"), timeout=120.0)  # phase: command running
    # Positive control: prove the probe can actually SEE the remote process
    # before trusting its later absence. Without this, "not seen" after the
    # signal is unfalsifiable -- it would also read as a pass if the probe
    # (or the pgrep pattern) were simply broken.
    assert _remote_pids(chaos_bed.element, _SLEEP), "positive control: remote command not observed"
    time.sleep(offset_in(chaos_rng, 0.0, 2.0))  # the ONE deliberate sleep: seeded injection offset
    p.signal(2)  # SIGINT
    p.wait_for_stderr(BANNER, timeout=15.0)  # phase: teardown running
    rc = p.wait(timeout=60.0)
    assert rc == 130, p.stderr_text()
    p.assert_no_process_group()
    # foreground remote child reaped via PTY HUP
    assert not _remote_pids(chaos_bed.element, _SLEEP), "remote foreground command not reaped"


def test_sigint_immune_remote_hits_deadline_force(chaos_bed, tmp_path):
    """A remote command trapping SIGINT drives otto to its teardown deadline.

    OTTO_TEARDOWN_DEADLINE small-but-nonzero: graceful teardown starts, the
    remote won't die on the channel's HUP fast enough, deadline fires -> force
    path. Asserts prompt exit on the honest double-outcome contract and no
    LOCAL orphans; the remote trap self-exits on its own timeout.

    No remote probe here (the force path abandons the remote sweep by
    design), but the trap's ``sleep`` still gets its OWN unique duration
    (312, distinct from the 311/313/314 used elsewhere in this module): the
    force path never kills the remote, so this trap outlives the test by
    design, and a shared duration would let that orphan masquerade as a
    sibling test's marker for as long as it lives.
    """
    trap = "trap '' INT; sleep 312"
    p = spawn_otto(
        ["host", chaos_bed.target.host_id, "run", trap, "--timeout", "300"],
        xdir=tmp_path,
        target=chaos_bed.target,
        extra_env={"OTTO_TEARDOWN_DEADLINE": "3"},
    )
    p.wait_for_log(re.escape("| trap"), timeout=120.0)
    p.signal(2)
    p.wait_for_stderr(BANNER, timeout=15.0)
    rc = p.wait(timeout=60.0)
    assert rc in (130, -2), p.stderr_text()  # graceful-in-time OR forced (Plan 3 contract)
    p.assert_no_process_group()


def test_nohup_remote_survives_graceful_teardown(chaos_bed, tmp_path):
    """Characterization (todo/chaos-realsignal-followups.md §5): otto reaps by
    PTY HUP, not by signalling the remote. A nohup'd command has no controlling
    terminal to lose, so it SURVIVES a graceful teardown — documented contract,
    not a leak. Teardown must clean up otto's OWN session state regardless.

    Deviation from the brief's literal transcription (recorded per the live-bed
    rule: root-cause first, never paper over with a widened assertion): a bare
    ``nohup sleep 313 & echo LAUNCHED-...`` returns control to the shell (and the
    whole ``otto host run`` invocation, and the local process) in well under
    50ms of the marker printing (measured: ~35ms) — faster than this driver's
    wait_for_log poll (50ms) plus test-side scheduling can react, so SIGTERM
    consistently lands AFTER otto's ``_main`` has already removed its signal
    handlers and the process is exiting on its own. That is a real race, but
    not the one this test means to characterize (100% repro, not a live-bed
    flake) — it would always assert on the wrong side of a coin flip between
    rc 0 (natural exit outran the signal) and rc -15 (raw kill after handler
    teardown), never the graceful rc 143 the docstring describes. Appending a
    foreground ``sleep 314`` after the nohup'd launch keeps the remote session
    (and otto's ``run`` verb) genuinely mid-command — the same mechanism the
    first two tests rely on — so SIGTERM has a real window while handlers are
    installed. The distinct duration (314 vs 313, and both distinct from the
    311/312 used earlier in this module) is deliberate: it keeps the
    foreground hold's process name out of the ``sleep 313`` pgrep pattern used
    below, so that pattern can only ever match the nohup survivor — and (the
    bug this whole fix wave addresses) so that survivor can never be confused
    with an orphan left behind by a sibling test. The "expected alive" assert
    below is itself the positive control for this test: it only passes if the
    probe can genuinely see a live process by this argv-visible needle.
    """
    marker = "otto-chaos-nohup"
    survivor = f"nohup sleep 313 >/dev/null 2>&1 & echo LAUNCHED-{marker}; sleep 314"
    p = spawn_otto(
        ["host", chaos_bed.target.host_id, "run", survivor, "--timeout", "300"],
        xdir=tmp_path,
        target=chaos_bed.target,
    )
    p.wait_for_log(f"LAUNCHED-{marker}", timeout=120.0)  # nohup launched; now blocked on the hold
    p.signal(15)  # SIGTERM
    rc = p.wait(timeout=60.0)
    assert rc == 143, p.stderr_text()
    p.assert_no_process_group()
    try:
        # The survivor is EXPECTED to be alive — that is the characterized behavior.
        assert _remote_pids(chaos_bed.element, "sleep 313"), (
            "nohup survivor unexpectedly gone — contract changed; update the docstring"
        )
    finally:
        # Both clauses target real argv: "sleep 313" is the nohup survivor
        # this test expects to still be running; "sleep 314" is its
        # foreground hold, which PTY-HUP reaping should already have cleaned
        # up by the time we get here (same mechanism as the first two
        # tests) -- this second clause is a best-effort belt-and-braces, not
        # load-bearing.
        run_probe(
            chaos_bed.element,
            lambda h: h.exec(
                "pkill -f 'sleep 313' || true; pkill -f 'sleep 314' || true",
                timeout=30,
                log=LogMode.QUIET,
            ),
        )
