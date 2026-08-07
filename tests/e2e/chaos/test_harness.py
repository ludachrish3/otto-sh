"""Lane certification: the leased-bed harness works before any chaos does.

Proves the previously-untested bed-target path end to end
(todo/chaos-realsignal-followups.md §4): otto subprocess -> leased veggies
host over real SSH, phase marker in verbose.log, clean exit, probe oracle
round-trips on an independent connection.

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

import pytest

from tests.e2e.chaos._bed import probe_text
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
