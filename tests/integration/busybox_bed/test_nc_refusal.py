"""nc-transfer refusal, pinned as the guests' contract.

Chris's 2026-08-21 ruling: BusyBox nc round-trips are a follow-up spec; what
THIS phase certifies is that otto announces the incapability loudly instead of
failing into a timeout. GET must raise the registered gap's refusal before
anything touches the wire, and the guest's set of TCP listeners must be
unchanged across the call — the true-negative proving the refusal really fired
first.

**Why a listener is where a failure would show.** These guests are reached
through the test1 hop, so ``NcFileTransfer`` takes its TUNNELLED get path,
and that path's first wire act is to make the GUEST listen:
``nc -Nl -w <t> <port> < <file>``, on a port the allocator picks. A refusal
that fired one layer too late — after ``_warmup_for_transfer``, or from inside
``_get_files_nc_tunneled`` instead of above the tunnel dispatch — would leave
that spawn's evidence behind, and an OpenBSD-style ``nc -l`` does not give up
on its own (see :attr:`~otto.host.options.NcOptions.listener_timeout`), so the
evidence is still there when the next ``netstat`` runs. So the assertion is not
decoration on the ``pytest.raises``: it is the observation that distinguishes
"refused before the wire" from "refused after it", which is the entire claim.

**A before/after diff, not a port window.** Until 2026-08-22 the guests were
reached through per-guest QEMU hostfwds and their lab entries declared an
identity-mapped ten-port ``nc`` window, so "nothing listens in the window" was
a statement that could be made about a fixed range of numbers. On real TAP NICs
there is no window: the entries declare no ``nc_options`` and the backend picks
its own ports, so no range is known in advance. Reading the listener table
BEFORE the call and again after, and demanding they match, is the honest
replacement — it is strictly stronger, because it covers every port the
allocator could have chosen rather than the ten it used to be pinned to.

**All five guests, not a sample.** The refusal is an API contract, and API
contracts take the full matrix here. It is also the cheapest sweep on the bed:
the guard raises in the transfer's guard phase, before warmup, so no guest is
asked to do anything but answer the userland probes it answers anyway.

This module is the first committed consumer of the ``guest_nc`` fixture, which
pins ``transfer="nc"`` through the host factory — so the menu validation that
accepts ``nc`` for these entries is exercised here too.
"""

from pathlib import Path

import pytest

from otto.host.errors import UnsupportedOnUserlandError

pytestmark = [pytest.mark.asyncio]


def _listening_ports(table: str) -> "set[int]":
    """Every TCP port in a ``netstat -tln`` table, parsed off the LISTEN rows.

    Deliberately independent of address family and of any port range: the rows
    read ``tcp 0 0 0.0.0.0:23 0.0.0.0:* LISTEN``, so the local port is the last
    colon-separated field of column four. ``rsplit`` rather than ``split`` so
    an IPv6 local address parses too.
    """
    ports = set()
    for line in table.splitlines():
        fields = line.split()
        if len(fields) >= 6 and fields[0].startswith("tcp") and fields[-1] == "LISTEN":
            ports.add(int(fields[3].rsplit(":", 1)[1]))
    return ports


async def _listeners(host) -> "set[int]":
    """The guest's LISTEN set, failing loudly if netstat itself did not run."""
    result = (await host.run("netstat -tln 2>/dev/null")).only
    assert result.retcode == 0, (
        f"netstat failed on {host.element} ({result.value!r}) — the listener "
        "comparison cannot mean anything without it"
    )
    return _listening_ports(result.value)


async def test_nc_get_is_refused_before_the_wire(guest_nc, tmp_path: Path):
    """A netcat GET raises the ``nc-transfer`` gap's refusal, having sent nothing."""
    host, version = guest_nc

    # Read the wire BEFORE the call. The refusal is supposed to fire above the
    # tunnel dispatch, so this snapshot and the one after it must be identical;
    # taking only the "after" shot would need a known port range to look in,
    # and there is none any more (see the module docstring).
    before = await _listeners(host)
    # The guest's own telnetd is the control: it proves this table shows
    # listeners at all, so the comparison below is an observation rather than a
    # vacuous pass on an unparsed or empty table.
    assert 23 in before, (
        f"{host.element}'s own telnetd is not in netstat's table (parsed "
        f"{sorted(before)}) — the probe cannot see listeners"
    )

    with pytest.raises(UnsupportedOnUserlandError) as excinfo:
        await host.get([Path("/etc/passwd")], tmp_path)
    message = str(excinfo.value)
    assert "nc-transfer" in message, (
        f"{host.element} (BusyBox {version}) refused with a message that does not "
        f"name the registry surface: {message!r}"
    )
    assert not (tmp_path / "passwd").exists(), (
        f"{host.element} refused the get and a file landed anyway: "
        f"{sorted(p.name for p in tmp_path.iterdir())}"
    )

    after = await _listeners(host)
    assert after == before, (
        f"{host.element} refused the get, but its TCP listener set CHANGED across "
        f"the call: {sorted(after - before)} appeared, {sorted(before - after)} "
        "went away. A new listener is the `nc -Nl` the tunnelled get path spawns "
        "first, which means the refusal did not fire before the wire"
    )
