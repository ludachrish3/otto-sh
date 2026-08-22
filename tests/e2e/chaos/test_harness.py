"""Lane certification: the leased-bed harness works before any chaos does.

Proves the previously-untested bed-target path end to end
(todo/chaos-realsignal-followups.md §4): otto subprocess -> leased veggies
host over real SSH, phase marker in verbose.log, clean exit, probe oracle
round-trips on an independent connection.

The lane's SECOND target is certified here too: the BusyBox bed's ``bb1350``
guest, reached over telnet through the ``carrot_seed`` hop. Same shape, one
extra thing to prove -- that the subprocess and the oracle, which reach the
guest by two entirely different routes, land on the same device.

``-R`` (``--skip-reservation-check``) decision: NOT prepended. The veggies
SUT (``tests/repo_e2e/.otto/settings.toml``) declares no ``[reservations]``
section at all, so ``ReservationConfigSpec.backend`` defaults to ``"none"``
and ``check_reservations``/``ReservationGate.evaluate`` resolve to a silent
no-op (same as the loopback tier-2 suite) — confirmed by running
``test_clean_run_on_leased_host`` against the live bed without the flag and
observing a clean exit 0, no reservation-gate error. If a future SUT change
adds a real backend to ``tests/repo_e2e``, add ``-R`` to the ``spawn_otto``
argv here (and to every other bed-target module in this lane).
"""

import re
import uuid

import pytest

from tests.e2e.chaos._bed import busybox_probe_text, probe_text
from tests.integration.chaos._driver import spawn_otto

pytestmark = [
    pytest.mark.chaos,
    pytest.mark.stability,
    pytest.mark.integration,
    pytest.mark.xdist_group("chaos_lane"),
    pytest.mark.timeout(300),
]


def test_clean_run_on_leased_host(chaos_bed, tmp_path):
    cmd = "echo CHAOS-CERT"
    p = spawn_otto(
        ["host", chaos_bed.target.host_id, "run", cmd],
        xdir=tmp_path,
        target=chaos_bed.target,
    )
    p.wait_for_log(re.escape(f"| {cmd}"), timeout=120.0)  # phase: command running
    rc = p.wait(timeout=120.0)
    assert rc == 0, p.stderr_text()
    p.assert_no_process_group()


def test_probe_oracle_round_trips(chaos_bed):
    assert "CHAOS-PROBE" in probe_text(chaos_bed.element, "echo CHAOS-PROBE")


@pytest.mark.no_hygiene_bracket  # the guest is not the veggies pool the autouse bracket leases
def test_clean_run_on_the_busybox_guest(busybox_chaos_bed, tmp_path):
    """The guest twin of ``test_clean_run_on_leased_host``: certify the
    BusyBox target's BOTH halves before any chaos rides them.

    Deliberately not two tests. The veggies pair can split subprocess-path
    and oracle-path certification because their oracles are independent
    instruments -- ``leased_bed`` proves reachability with a raw TCP connect,
    so ``test_probe_oracle_round_trips`` is the first thing to exercise a
    real ``UnixHost``. Here the ``busybox_chaos_bed`` fixture's own
    reachability check IS an oracle round-trip (it has to be: a connect probe
    would call a still-booting guest healthy), so a standalone oracle test
    would re-run what the fixture just ran and could not fail on its own.

    What is genuinely uncertified is the JOIN: that the otto subprocess and
    the oracle reach the SAME device over their two very different routes --
    the subprocess through ``-l busybox`` + the committed lab record's
    ``hop``, the oracle through an in-process factory build and a
    ContextVar-installed hop lab. So the subprocess writes a nonce on the
    guest and the oracle, on its own fresh login, reads it back. A harness
    where those two ever pointed at different guests (or at carrot itself)
    would pass every reachability check and quietly answer chaos questions
    about the wrong host.
    """
    nonce = uuid.uuid4().hex[:12]
    marker_file = f"/tmp/chaos-cert-{nonce}"
    cmd = f"echo {nonce} > {marker_file}"
    p = None
    try:
        p = spawn_otto(
            ["host", busybox_chaos_bed.target.host_id, "run", cmd],
            xdir=tmp_path,
            target=busybox_chaos_bed.target,
        )
        # Phase: command running. The pattern is a short PREFIX of the echoed
        # command, not the whole of it, and that is load-bearing rather than
        # laziness: `verbose.log` is rendered, so long lines arrive HARD-WRAPPED
        # (~68 characters after the timestamp/level prefix) and a pattern that
        # spans a wrap point can never match. Caught live here -- the first
        # version of this test gated on the full `| {cmd}`, whose redirect
        # landed on the next line, and it failed with otto having exited 0 after
        # doing exactly the right thing. `| echo <nonce>` is ~34 characters
        # including the host tag, so it cannot wrap, and the nonce keeps it as
        # specific as the whole line was.
        p.wait_for_log(re.escape(f"| echo {nonce}"), timeout=120.0)
        rc = p.wait(timeout=120.0)
        assert rc == 0, p.stderr_text()
        p.assert_no_process_group()
        assert busybox_probe_text(f"cat {marker_file}") == nonce, (
            f"{busybox_chaos_bed.element}: the oracle cannot see the nonce the otto "
            "subprocess wrote -- the two halves of this harness are not on the same guest"
        )
    finally:
        # Belt for the assert-failure path, the same one the session- and
        # transfer-chaos guest arms carry: an early raise (a `wait_for_log`
        # timeout above all) leaves the local subprocess running, and a survivor
        # still holding the guest's telnet session would be read as this test's
        # own leftover on the NEXT run. This arm's command self-terminates, so
        # the belt is cheap insurance rather than a fix for an observed leak.
        if p is not None and p.proc.poll() is None:
            p.signal(9)
        busybox_probe_text(f"rm -f {marker_file} || true")
