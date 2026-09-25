"""The bed's serial wiring, proved with NO otto console code.

Raw telnetlib3 (through an SSH forward into the console server, or straight
at its address) nudges each console and expects what the device on the far
end of the serial line prints: test2's getty, and each ARM Zephyr guest's
shell. If these fail, every otto console test after them would fail for the
bed's reasons, not otto's; they are the wiring proof. BusyBox's ``bb1350``
row lives in ``tests/integration/busybox_bed/test_console.py`` with its bed
group.
"""

import re

import pytest

from tests._fixtures.labdata import host_data
from tests.e2e.console._raw import nudge, raw_console, raw_direct, server_cred, strip_ansi_bytes

pytestmark = [pytest.mark.timeout(120)]

_TEST2 = host_data("test2")["console_options"]
_SERVER = host_data(_TEST2["server"])

# The ARM Zephyr console rows. The console is single-client, so a raw nudge
# must never race another driver: zephyr37_nofs joins the group the embedded
# host suites stamp for it (zephyr_no_fs), zephyr37_llext the llext coverage
# e2e's group. zephyr44_llext has no other driver in these lanes today; its
# group is named after it so a future one serialises by joining it. These
# rows do not get tests/integration/host's embedded protections (the shared
# console lock, the wedge fast-fail): an 8 s read-only nudge needs neither.
_ZEPHYR_ROWS = [
    pytest.param(
        "zephyr37_nofs", marks=pytest.mark.xdist_group("zephyr_no_fs"), id="zephyr37_nofs"
    ),
    pytest.param(
        "zephyr37_llext", marks=pytest.mark.xdist_group("zephyr37_llext"), id="zephyr37_llext"
    ),
    pytest.param(
        "zephyr44_llext", marks=pytest.mark.xdist_group("zephyr44_llext"), id="zephyr44_llext"
    ),
]


def _ends_at_login(out: bytes, name: str) -> bool:
    return re.search(rb"%s login: ?$" % re.escape(name.encode()), out.rstrip(b"\r\n")) is not None


@pytest.mark.integration
@pytest.mark.xdist_group("console_e2e")
@pytest.mark.asyncio
async def test_the_server_serves_test2s_getty_through_an_ssh_forward(test2_lease):
    cred = server_cred(_TEST2["server"])
    async with raw_console(_SERVER["ip"], cred.login, cred.password, _TEST2["port"]) as line:
        out = strip_ansi_bytes(await nudge(line))
    assert _ends_at_login(out, "test2"), out


@pytest.mark.integration
@pytest.mark.xdist_group("console_e2e")
@pytest.mark.asyncio
async def test_a_direct_dial_reaches_the_same_listener(test2_lease):
    async with raw_direct(_SERVER["ip"], _TEST2["port"]) as line:
        out = strip_ansi_bytes(await nudge(line))
    assert _ends_at_login(out, "test2"), out


@pytest.mark.embedded
@pytest.mark.asyncio
@pytest.mark.parametrize("ne", _ZEPHYR_ROWS)
async def test_the_arm_zephyr_console_shows_its_shell(ne):
    opts = host_data(ne)["console_options"]
    server = host_data(opts["server"])
    cred = server_cred(opts["server"])
    async with raw_console(server["ip"], cred.login, cred.password, opts["port"]) as line:
        out = strip_ansi_bytes(await nudge(line))
    assert b"uart:~$" in out, f"{ne} ({opts['server']}:{opts['port']}): {out!r}"
