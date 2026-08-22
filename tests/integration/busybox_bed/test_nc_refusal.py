"""nc-transfer refusal, pinned as the guests' contract.

Chris's 2026-08-21 ruling: BusyBox nc round-trips are a follow-up spec; what
THIS phase certifies is that otto announces the incapability loudly instead of
failing into a timeout. GET must raise the registered gap's refusal before
anything touches the wire, and the guest's declared nc window must show no
listener afterwards — the true-negative proving the refusal really fired first.

**Why the window is where a failure would show.** These guests are reached
through the carrot hop, so ``NcFileTransfer`` takes its TUNNELLED get path,
and that path's first wire act is to make the GUEST listen:
``nc -Nl -w <t> <port> < <file>``, on a port the allocator picks starting at
:attr:`~otto.host.options.NcOptions.port`. A refusal that fired one layer too
late — after ``_warmup_for_transfer``, or from inside ``_get_files_nc_tunneled``
instead of above the tunnel dispatch — would leave that spawn's evidence
behind. So the assertion is not decoration on the ``pytest.raises``: it is the
observation that distinguishes "refused before the wire" from "refused after
it", which is the entire claim.

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


async def test_nc_get_is_refused_before_the_wire(guest_nc, tmp_path: Path):
    """A netcat GET raises the ``nc-transfer`` gap's refusal, having sent nothing."""
    host, version = guest_nc

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

    listeners = (await host.run("netstat -tln 2>/dev/null")).only
    assert listeners.retcode == 0, (
        f"netstat failed on {host.element} ({listeners.value!r}) — the window "
        "check below cannot mean anything without it"
    )
    # The guest's own telnetd is the control: it proves this table shows
    # listeners at all, so the window's absence below is an observation rather
    # than a vacuous pass on an empty string.
    assert ":23" in listeners.value, (
        f"{host.element}'s own telnetd is not in netstat's table "
        f"({listeners.value!r}) — the probe cannot see listeners"
    )
    # Three digits, so one substring covers the whole band the port allocator
    # would land in first (base .. base+9). Keep that property if the lab's nc
    # ports ever move: every guest's base is a multiple of ten today.
    base = host.nc_options.port
    window = str(base)[:3]
    assert f":{window}" not in listeners.value, (
        f"{host.element} refused the get, but something LISTENS in its nc window "
        f"{base}..{base + 9} — the refusal did not fire before the wire:\n{listeners.value}"
    )
