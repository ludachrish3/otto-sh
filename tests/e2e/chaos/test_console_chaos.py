"""Console-client-death chaos (todo/zephyr-console-wedge-2026-08-01.md).
Behind `make chaos-embedded` -- CAN WEDGE A BOARD. SIGKILL otto mid-console-
session; assert the NEXT client gets a working shell within a bound, else FAIL
NAMING THE BOARD. Regression-guards the 2026-08-01 fat-board wedge's
client-death candidate cause. Recovery criterion is a SUSTAINED shell (N
round-trips over a settle window), not one accept -- this incident is the
standing counterexample where accept != shell (todo/chaos-reboot-followups.md
Section 4).

Target: `sprout` (fat board, 192.0.2.1, zephyr 3.7, FAT-on-RAM), reached via
the `basil_seed` SSH hop -> telnet 192.0.2.1:23. The repo_e2e SUT's own
`embedded` lab does NOT load here (its `sprout27` entry declares
`command_frame: 'zephyr-inline'`, not a registered frame in this repo), so
this module generates its OWN minimal SUT scoped to just `basil` + `sprout`
from `tests/_fixtures/lab_data/tech1/lab.json` -- mirrors
`tests.integration.chaos._target.make_loopback_target`'s shape (a private
`labs/<name>/tech1/lab.json` + a matching `settings.toml`), with each host's
`labs` list rewritten to the private lab name.

Mid-handshake variant, deliberately PARKED (not shipped): a live probe
against the real board (see task-10-report.md) measured the marker handshake
(`SessionManager._ensure_initialized`'s READY round-trip) completing in
~150-300ms end-to-end, logged only as a DEBUG "handshake start" /
"handshake matched" pair. Gating a SIGKILL to land deterministically inside
that sub-300ms window from the CLI boundary would require racing the test
driver's own 50ms `verbose.log` poll loop (`_driver.py`'s `_POLL`) plus the
QueueListener flush thread against a window that narrow -- there is no
INFO-level "handshake in progress" marker analogous to the command-dispatch
line, and repeatedly tuning timing against a real single-session board (to
find out empirically whether it lands in time) itself risks the exact
contention/wedge this module exists to characterize, only for a diagnostic
question rather than the intended scenario. Per the task-10 brief's own
authorized fallback, this module ships only the mid-command variant. See
task-10-report.md for the measurements and the parked-case note.

Mid-command determinism: `Host._log_command`'s `"| <cmd>"` INFO line (the
same phase marker `tests/integration/chaos/test_signal_run.py` gates its
mid-run SIGINT/SIGTERM scenarios on) fires from
`SessionManager.run_cmd`/`.exec` only AFTER `_ensure_session()`'s handshake
has already completed -- so observing it already proves the console session
is genuinely open, past handshake. A single `kernel version` round-trip
against sprout was live-measured at ~45ms end-to-end (see task-10-report.md)
-- far too close to the driver's own 50ms poll interval to trust a bare
single-command SIGKILL not to land after otto has already finished and
started exiting (the exact Task 5 lesson the brief calls out). So the kill
phase drives `kernel version` chained many times in ONE `otto host sprout
run` invocation (the persistent-session, multi-command form the CLI already
supports) and gates the SIGKILL on the marker for the FIRST copy: with
dozens more queued behind it at that measured per-round-trip rate, the
session is still provably mid-flight, by a wide and deterministic margin,
regardless of the exact instant the signal lands.
"""

import contextlib
import json
import re
import time
from pathlib import Path

import pytest

from tests._fixtures.labdata import lab_data_path
from tests.integration.chaos._driver import spawn_otto
from tests.integration.chaos._target import ChaosTarget

pytestmark = [
    pytest.mark.chaos,
    pytest.mark.stability,
    pytest.mark.embedded,
    pytest.mark.no_hygiene_bracket,  # sprout/basil aren't the veggies unix pool the autouse
    # bracket's `chaos_bed` leases; this module's own sustained-shell check IS the
    # incident's console-responsiveness probe (spec BedHygiene note, Task 10). The lease
    # still happens (the fixture's own signature requires `chaos_bed`, same as
    # test_reboot_chaos.py's multi-host scenarios) but this module never touches it.
    pytest.mark.xdist_group("chaos_lane"),
    pytest.mark.timeout(600),
]

_LAB_NAME = "sprout_console"
_HOST_ID = "sprout"
_BOARD_IP = "192.0.2.1"
_BOARD_NAME = f"{_HOST_ID}/{_BOARD_IP}"

_PROBE_CMD = "kernel version"
# Live-measured ~45ms/round-trip on this same session (task-10-report.md) ->
# ~40 copies queues roughly 1.5-2s of genuine in-flight work behind the FIRST
# marker match, a wide margin against local poll/flush jitter (Task 5's lesson).
_KEEP_BUSY_REPEATS = 40
_MARKER_TIMEOUT = 60.0
_KILL_WAIT_TIMEOUT = 30.0
_ROUND_TRIP_TIMEOUT = 60.0

# Sustained-shell recovery: N small, window short (brief: bound board stress) --
# see chaos-reboot-followups.md (todo/) Section 4's "accept != shell" lesson;
# this incident is that lesson's own standing counterexample.
_SUSTAINED_ROUND_TRIPS = 3
_SETTLE_INTERVAL = 1.0


def _make_sprout_target(root: Path) -> ChaosTarget:
    """Generate a scoped SUT containing ONLY basil + sprout, each host's
    `labs` rewritten to a private lab name -- avoids the full `embedded` lab
    (repo_e2e's copy fails to load here: `sprout27` declares
    `command_frame: 'zephyr-inline'`, not a registered frame). Mirrors
    ``tests.integration.chaos._target.make_loopback_target``'s shape
    (private ``labs/<name>/tech1/lab.json`` + matching ``settings.toml``).
    """
    tech_dir = root / "labdata" / "tech1"
    tech_dir.mkdir(parents=True)
    all_hosts = json.loads(lab_data_path().read_text())["hosts"]
    hosts = []
    basil = None
    for raw in all_hosts:
        if raw["element"] not in ("basil", "sprout"):
            continue
        host = dict(raw)
        host["labs"] = [_LAB_NAME]
        hosts.append(host)
        if host["element"] == "basil":
            basil = host
    assert basil is not None, "tech1/lab.json missing 'basil' -- fixture shape changed"
    (tech_dir / "lab.json").write_text(json.dumps({"hosts": hosts, "links": []}, indent=2))

    sut = root / "sut"
    (sut / ".otto").mkdir(parents=True)
    (sut / ".otto" / "settings.toml").write_text(
        f"""\
name = "sprout_console_harness"
version = "0.1.0"
lab_data_type = "json"
labs = [
    "{tech_dir}",
]

[lab]
backend = "json"
"""
    )
    cred = basil["creds"][0]
    return ChaosTarget(
        sut_dir=sut,
        lab=_LAB_NAME,
        host_id=_HOST_ID,
        ssh_host=basil["ip"],  # unused by spawn_otto (sut_dir/lab only); kept for shape parity
        ssh_port=22,
        ssh_username=cred["login"],
        ssh_client_key=None,
        ssh_password=cred["password"],
    )


def _round_trip(target: ChaosTarget, xdir: Path) -> None:
    """One FRESH-client console round-trip: a brand-new otto subprocess opens
    a brand-new console session, runs the probe command, and must get a real
    answer back. Deliberately a fresh process each time (not one held-open
    connection re-poked) -- this is "the next client" the incident's recovery
    criterion is about.
    """
    xdir.mkdir()
    p = spawn_otto(["host", target.host_id, "run", _PROBE_CMD], xdir=xdir, target=target)
    rc = p.wait(timeout=_ROUND_TRIP_TIMEOUT)
    out = p.stdout_text()
    failure = (
        f"{_BOARD_NAME}: console did not answer a working shell after client death "
        f"(rc={rc}) -- the wedge reproduced.\nstdout:\n{out}\nstderr:\n{p.stderr_text()}"
    )
    assert rc == 0, failure
    assert "Zephyr" in out, failure


def test_console_client_death_leaves_next_client_a_shell(tmp_path):
    """SIGKILL otto mid-console-session (mid-command variant; see module
    docstring for the parked mid-handshake case), then assert a SUSTAINED
    shell from fresh clients: 3 consecutive `run` round-trips over a short
    settle window, not one lucky accept. NEVER reboots or power-cycles the
    board -- only the local otto subprocess is killed. If any round-trip
    fails, this FAILS naming the board (sprout/192.0.2.1): that is the wedge
    reproduced, and per the brief this is a real finding requiring a human
    (the board needs a manual restart) -- there is no retry/recovery
    attempted here.
    """
    target = _make_sprout_target(tmp_path / "bed")

    kill_xdir = tmp_path / "kill"
    kill_xdir.mkdir()
    p = spawn_otto(
        ["host", target.host_id, "run", *([_PROBE_CMD] * _KEEP_BUSY_REPEATS)],
        xdir=kill_xdir,
        target=target,
    )
    try:
        # phase: the persistent session's handshake completed and the FIRST
        # of the chained commands has been dispatched (Host._log_command
        # fires only after SessionManager._ensure_session()'s handshake --
        # see otto.host.session.SessionManager.run_cmd). ~39 more copies are
        # still queued behind this marker at this point, so the console
        # session is provably still open by a wide margin when we signal.
        p.wait_for_log(re.escape(f"| {_PROBE_CMD}"), timeout=_MARKER_TIMEOUT)
        p.signal(9)  # SIGKILL -- no teardown possible; this IS the client-death being characterized
        p.wait(timeout=_KILL_WAIT_TIMEOUT)
    finally:
        # Belt-and-suspenders: never leave our OWN process holding the
        # single console session open, whatever happened above (e.g. the
        # marker wait itself raised).
        if p.proc.poll() is None:
            with contextlib.suppress(ProcessLookupError):
                p.signal(9)
            with contextlib.suppress(Exception):
                p.wait(timeout=_KILL_WAIT_TIMEOUT)

    # Sustained-shell recovery: 3 FRESH clients, each a genuine round-trip,
    # spaced over a short settle window. First failure fails loud, naming
    # the board -- no retry loop (a wedged board needs a human, not hammering).
    for i in range(_SUSTAINED_ROUND_TRIPS):
        _round_trip(target, tmp_path / f"recover_{i}")
        if i < _SUSTAINED_ROUND_TRIPS - 1:
            time.sleep(_SETTLE_INTERVAL)
