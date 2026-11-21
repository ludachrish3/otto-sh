"""Releasing a Zephyr telnet console never makes the guest fail a send (#386).

Zephyr's ``shell_telnet`` backend shuts itself down when a send fails because
its client has gone, and on some guests the restart leaves the console refusing
every later connection: ``embedded bed unhealthy`` for the rest of a run, or a
dead shell on a real device. The send fails whenever otto releases the line
while the guest still has output queued. otto therefore drains a single-client
console to quiet inside every client ``close()``.

Each test here releases one guest the same way 30 times, then reads that
guest's own log lines from its hop's journal (by cursor, for its systemd unit
only) and requires zero ``Failed to send``, zero ``Telnet socket … error`` and
no ``Telnet shell backend initialized`` re-init, plus a working re-open after
the last cycle. The arms and their verdicts are in
:mod:`tests._fixtures.console_release_arms`, which a hostless smoke test drives
against fakes.

Before the drain moved into ``close()``, arm A put a ``Failed to send`` on
almost every close of a talking guest (68 across three guests), and arm D
wedged the 3.7 FAT guest at cycle 23 (spec
``2026-11-02-telnet-graceful-close-design.md``, "The RED").

Marked ``release_proof`` as well as ``embedded``: this is the bed proof of the
fix, not a soak, so no default lane, tip or soak runs it. ``make
console-release-bed`` runs it under the bed lease, and ``make
release-campaign`` invokes that target. Each guest's arms run serially on one
worker (its per-device xdist group), and the guests run in parallel::

    make console-release-bed
"""

import pytest

from otto.host.factory import create_host_from_dict
from tests._fixtures.console_release_arms import ArmConfig, run_arm
from tests.conftest import (
    _ZEPHYR_BACKEND_NE as _BACKEND_NE,
)
from tests.conftest import (
    element_for,
    embedded_param_id,
    host_data,
)
from tests.integration.host._hop_journal import GUESTS as _GUESTS
from tests.integration.host._hop_journal import HopJournal

pytestmark = [pytest.mark.embedded, pytest.mark.release_proof, pytest.mark.timeout(1200)]


def _host_factory(backend: str):
    ne = _BACKEND_NE[backend]
    data = {**host_data(ne), "log": "quiet"}  # the flood stays off the test's terminal
    element = element_for(ne)
    return lambda: create_host_from_dict(data, element=element)


def _min_drain_close() -> float:
    from otto.host.telnet import CONSOLE_QUIESCE_QUIET_WINDOW

    return CONSOLE_QUIESCE_QUIET_WINDOW


@_GUESTS
@pytest.mark.parametrize("arm", ["A", "B", "C", "D"])
@pytest.mark.asyncio
async def test_release_leaves_the_console_clean(backend: str, arm: str) -> None:
    """Arms A to D, 30 cycles each: no failed send, no socket error, no backend re-init.

    A: output in flight, then the ordinary close. B: the same, and the close
    must last at least the drain's quiet window (the drain the diagnostic
    called explicitly now runs inside ``close()``). C: an idle close, then an
    immediate re-open. D: a short command to completion, then an immediate
    close.
    """
    cfg = ArmConfig(min_drain_close=_min_drain_close() if arm == "B" else 0.0)
    journal = HopJournal(backend)
    outcome = await run_arm(
        arm,
        guest=embedded_param_id(backend),
        unit=journal.unit,
        build_host=_host_factory(backend),
        journal=journal,
        cfg=cfg,
    )
    assert not outcome.failures(), outcome.report()


@_GUESTS
@pytest.mark.asyncio
async def test_cancel_mid_output_leaves_the_console_clean(backend: str) -> None:
    """Arm E: a command cancelled after its first output line, then closed.

    A cycle whose command had finished before the cancel is void and judges
    nothing. When every cycle is void, the guest emits each command's output in
    one burst (the stock ``shell_module`` sample does), so no mid-output cancel
    is possible there, and the test reports that as an expected failure rather
    than a pass. The journal is judged either way.
    """
    journal = HopJournal(backend)
    outcome = await run_arm(
        "E",
        guest=embedded_param_id(backend),
        unit=journal.unit,
        build_host=_host_factory(backend),
        journal=journal,
        cfg=ArmConfig(),
    )
    assert not outcome.failures(), outcome.report()
    if outcome.all_void:
        pytest.xfail("guest emits output in one burst")
