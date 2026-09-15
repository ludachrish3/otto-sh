"""The survey on the real bed: a unix host and its menu, a drift fixture, the Zephyr sweep, snmp.

Carries ``integration`` (by directory) and ``embedded`` on the Zephyr cases.
Every case asserts a specific host by name; nothing skips on a host being down.
"""

from dataclasses import replace

import pytest

from otto.host.factory import create_host_from_dict
from otto.host.options import FtpOptions
from otto.host.survey import run_survey
from otto.host.survey.engine import dial_for
from otto.host.survey.report import drift_rows, menu_pin
from otto.host.survey.verdict import SWEEP_DIAL_TIMEOUT_S
from tests._fixtures.labdata import element_for, host_data

pytestmark = pytest.mark.timeout(150)

HOP_ID = "test4"
"""The hop ``hop_on_this_loop`` (tests/integration/conftest.py) rebuilds per test."""

_DEAD_PORT = 2323
"""A port nothing serves on either Zephyr guest — the sweep's telnet alternate."""


def _row(s, protocol, port):
    return next(v for v in s.verdicts if v.protocol == protocol and v.port == port)


async def _sweep_dial(host, port: int):
    """Make the very dial the sweep makes: the engine's own binding, at its own timeout.

    ``_embedded_sweep_row`` keeps no verdict and no listener entry for a
    non-open port that is not the console, so what a dead port says is
    invisible in the survey's rows. It is read here instead — through
    :func:`~otto.host.survey.engine.dial_for`, the same callable the sweep is
    handed, so this observation cannot drift from the observed.
    """
    return await dial_for(host, timeout=SWEEP_DIAL_TIMEOUT_S)(port)


@pytest.mark.asyncio
@pytest.mark.parametrize("host1", ["ssh"], indirect=True)
async def test_test1_reports_its_real_menu_and_only_shell_drifts(host1):
    """test1's declared menu is all live; the one drift is the shell it never declared.

    ``shell`` is the carrying session itself, so the session tier states it
    ``supported`` on every host that logs in — and test1's ``valid_transfers``
    does not name it. That is a true working-but-undeclared row and the pin
    that follows from it, not a pin-free menu.

    test1 resolves ``sudo`` and its cred carries a password, so the inventory
    runs ELEVATED here — the only bed proof that the sudo arm works at all
    (the BusyBox guests take the ``su`` arm). The elevated command is a
    textual prefix of the inventory script, so an unwrapped multi-statement
    script is refused by bash in its entirety, the run hangs to its own
    timeout and the report then states a refusal nothing refused: both
    footnotes below are asserted absent, and each is RED on the unwrapped
    script.
    """
    s = await run_survey(host1)
    assert _row(s, "ssh", host1.ssh_options.port).state == "supported"
    assert _row(s, "shell", host1.ssh_options.port).state == "supported"
    assert _row(s, "sftp", host1.ssh_options.port).state == "supported"
    assert _row(s, "scp", host1.ssh_options.port).state == "supported"
    assert _row(s, "ftp", host1.ftp_options.port).state == "supported"
    assert s.working_ports == {}
    rows = drift_rows(host1.valid_terms, host1.valid_transfers, s)
    assert [(r.protocol, r.drift) for r in rows] == [("shell", "working-but-undeclared")], rows
    assert menu_pin(host1.valid_terms, host1.valid_transfers, s) == [
        'valid_transfers = ["ftp", "nc", "scp", "sftp", "shell"]'
    ], s
    assert not any(f.startswith("inventory failed") for f in s.footnotes), s.footnotes
    assert not any(f.startswith("elevation refused") for f in s.footnotes), s.footnotes
    assert not any(f.startswith("owners incomplete") for f in s.footnotes), s.footnotes


@pytest.mark.asyncio
async def test_a_dead_declared_ftp_port_drifts_and_the_inventory_finds_the_real_one():
    """test1 with ftp_options.port deliberately wrong.

    Declared-but-dead on 2121 — dialed before any login is tried, and reported
    by the inventory that outranks that dial — discovered on 21, and a pin
    fragment naming the port the inventory found.
    """
    data = host_data("test1")
    host = create_host_from_dict(data, element=element_for("test1"))
    host = replace(host, ftp_options=FtpOptions(port=2121))
    try:
        s = await run_survey(host)
    finally:
        await host.close()
    dead = _row(s, "ftp", 2121)
    # The declared port is dialed before any login, so no login-tier row is
    # produced for it; of the two rows that remain the inventory's outranks
    # the dial's, and both say the same thing. A login-tier row here would be
    # the fabricated verdict the pre-dial exists to prevent.
    assert (dead.state, dead.tier, dead.vantage) == ("closed", "inventory", "session:ssh"), dead
    assert dead.detail.startswith("nothing bound on :2121"), dead
    assert _row(s, "ftp", 21).state == "supported", [v for v in s.verdicts if v.protocol == "ftp"]
    assert s.working_ports == {"ftp": 21}
    rows = drift_rows(host.valid_terms, host.valid_transfers, s)
    assert any(r.protocol == "ftp" and r.drift == "declared-but-dead" for r in rows), rows
    assert '"ftp_options": {"port": 21}' in menu_pin(host.valid_terms, host.valid_transfers, s)


@pytest.mark.embedded
@pytest.mark.asyncio
@pytest.mark.parametrize("host1", ["zephyr_44_lfs"], indirect=True)
async def test_zephyr_44_sweep_finds_the_console_and_refuses_22(hop_on_this_loop, host1):
    """4.4 answers a dead port correctly, so the sweep's other ports leave no listener rows."""
    s = await run_survey(host1)
    port = host1.telnet_options.port
    assert _row(s, "telnet", port).state == "supported", _row(s, "telnet", port)
    assert _row(s, "telnet", port).vantage == "hop:test4"
    assert _row(s, "console", port).state == "supported"
    assert _row(s, "tftp", 69).state == "not-checkable"
    assert not any(o.startswith("22/tcp") for o in s.other_listeners), s.other_listeners
    snmp = _row(s, "snmp", host1.snmp.port)
    # Ruling 24: pysnmp issues the GET itself, a local UDP call from wherever
    # otto runs -- never routed through the hop the way telnet's row above
    # is -- so the row's vantage names where the check ran, not the path to
    # the relay endpoint it reached.
    assert (snmp.state, snmp.tier, snmp.vantage) == ("supported", "dial", "controller"), snmp
    assert snmp.detail.startswith("sysUpTime "), snmp
    # The control for the 3.7 case below: one port, one hop, one timeout.
    dial = await _sweep_dial(host1, _DEAD_PORT)
    assert dial.state == "closed", dial


@pytest.mark.embedded
@pytest.mark.asyncio
@pytest.mark.parametrize("host1", ["zephyr_fat"], indirect=True)
async def test_zephyr_37_dead_ports_read_timeout_never_closed(hop_on_this_loop, host1):
    """The 3.7 stack's malformed RST: a dead port times out. The survey must not call it closed.

    The sweep states no verdict and no listener for a port that never
    answered, so the survey's evidence is the absence of both; the dial that
    decides it is asserted directly, against the 4.4 guest's ``closed`` on the
    same port from the same hop.
    """
    s = await run_survey(host1, scan_ports=str(_DEAD_PORT))
    console = host1.telnet_options.port
    assert _row(s, "telnet", console).state == "supported"
    dead = [v for v in s.verdicts if v.protocol == "telnet" and v.port != console]
    assert dead == [], dead
    assert not any("closed" in o for o in s.other_listeners), s.other_listeners
    dial = await _sweep_dial(host1, _DEAD_PORT)
    assert dial.state == "timeout", dial
