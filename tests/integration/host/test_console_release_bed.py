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

import shlex

import pytest

from otto.host.factory import create_host_from_dict

# Private, but the lab tooling's, and already imported this way by
# tests/integration/host/conftest.py: the one password-SSH invocation otto's own tests use.
from scripts.lab_health import _run_ssh
from tests._fixtures.console_release_arms import (
    ArmConfig,
    JournalEntry,
    parse_journal_json,
    run_arm,
)
from tests.conftest import (
    _ZEPHYR_BACKEND_NE as _BACKEND_NE,
)
from tests.conftest import (
    EMBEDDED_BACKENDS,
    element_for,
    embedded_param_id,
    host_data,
)
from tests.integration.host.conftest import _hop_ssh_target

pytestmark = [pytest.mark.embedded, pytest.mark.release_proof, pytest.mark.timeout(1200)]

# The guests on the telnet term. A guest whose only term is ``console`` sits
# behind ser2net, whose release #761's bed run re-checks.
_TELNET_GUESTS: list[str] = [
    b for b in EMBEDDED_BACKENDS if host_data(_BACKEND_NE[b]).get("valid_terms") != ["console"]
]

# Each telnet guest's systemd unit on its hop, from docs/contributing.md's
# Zephyr guest table (the Vagrantfile names them zephyr-qemu-<cfg>.service).
# The repository has no code-level guest-to-unit mapping, and the console-wedge
# capture in tests/integration/host/conftest.py reads the whole
# ``zephyr-qemu-*`` glob for that reason. This test needs the guest's OWN
# unit, because other guests' lines are not its verdict. A stale entry cannot
# pass quietly: every arm first checks on the hop that its unit is active.
_GUEST_UNITS: dict[str, str] = {
    "zephyr_fat": "zephyr-qemu-v3_7_fat_ram.service",
    "zephyr_lfs": "zephyr-qemu-v3_7_lfs.service",
    "zephyr_27_fat": "zephyr-qemu-v2_7_fat_ram.service",
    "zephyr_44_lfs": "zephyr-qemu-v4_4_lfs.service",
}

# A guest added to the matrix with no unit here would collect no row at all,
# and a removed one would leave a key that names nothing. Either is a silent
# change of what this test covers, so both fail at import.
if set(_GUEST_UNITS) != set(_TELNET_GUESTS):
    raise RuntimeError(
        f"_GUEST_UNITS {sorted(_GUEST_UNITS)} does not match the telnet guests "
        f"{sorted(_TELNET_GUESTS)}. Add or remove the entry, with its unit from "
        "docs/contributing.md's Zephyr guest table."
    )

_GUESTS = pytest.mark.parametrize(
    "backend", [pytest.param(b, id=embedded_param_id(b)) for b in _TELNET_GUESTS]
)

_SSH_TIMEOUT = 60.0

# Run on the hop: print the cursor of the journal entry ``journalctl -n 1`` emits.
_PRINT_CURSOR = 'import sys, json; print("CURSOR=" + json.loads(sys.stdin.readline())["__CURSOR"])'


class HopJournal:
    """One guest's journal on its hop, over the lab tooling's password SSH."""

    def __init__(self, backend: str) -> None:
        target = _hop_ssh_target(backend)
        if target is None:
            pytest.fail(f"no hop credentials in lab data for {backend!r}", pytrace=False)
        self.ip, self.login, self.password = target
        self.unit = _GUEST_UNITS[backend]

    def _ssh(self, remote: str) -> str:
        """Run *remote* on the hop and return its output; any failure fails the test.

        This blocks the event loop for up to ``_SSH_TIMEOUT``, and that is
        acceptable only because of when it runs: ``run_arm`` calls ``mark()``
        before its first cycle builds a host and ``since()`` after the final
        re-open has closed, so no console, keepalive or drain is waiting on the
        loop, and each guest's test is alone on its worker. It stays
        synchronous because it is the lab tooling's one password-SSH
        invocation, and threads beside the loop are not an option here. A call
        added between cycles would stall a live console and must not be made.
        """
        rc, out, err = _run_ssh(self.ip, self.login, self.password, remote, timeout=_SSH_TIMEOUT)
        if rc != 0:
            pytest.fail(
                f"hop {self.ip}: {remote[:80]!r} failed (rc={rc}): {err[-300:]}", pytrace=False
            )
        return out

    def mark(self) -> str:
        """The newest journal entry's cursor, after checking the guest's unit is active."""
        out = self._ssh(
            f"echo ACTIVE=$(systemctl is-active {self.unit}); "
            "journalctl -n 1 -o json --no-pager | "
            f"python3 -c {shlex.quote(_PRINT_CURSOR)}"
        )
        kv = dict(line.partition("=")[::2] for line in out.splitlines())
        if kv.get("ACTIVE") != "active":
            pytest.fail(
                f"{self.unit} is {kv.get('ACTIVE')!r} on the hop, not active: the guest is down "
                "or _GUEST_UNITS no longer names its unit",
                pytrace=False,
            )
        if not kv.get("CURSOR"):
            pytest.fail(f"no journal cursor from the hop: {out[-300:]!r}", pytrace=False)
        return kv["CURSOR"]

    def since(self, position: str) -> list[JournalEntry]:
        """Every entry for the guest's unit after the cursor *position*."""
        after = shlex.quote(position)
        return parse_journal_json(
            self._ssh(f"journalctl -u {self.unit} --after-cursor {after} -o json --no-pager")
        )


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
