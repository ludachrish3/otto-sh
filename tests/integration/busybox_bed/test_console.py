"""bb1350's serial console: a BusyBox getty on ttyS1, served by QEMU on test1:2450.

The wiring proof (raw telnet through an SSH forward into test1, no otto
console code) and one login + run over otto's ``console`` term. Here rather
than under ``tests/e2e/console/`` so the rows join the BusyBox bed's xdist
group and its bed-down policy. Every test leaves the line at ``bb1350
login:``; the ``_line_left_at_login`` fixture checks, and resets with
``logout`` before failing a test that did not.
"""

import re
from collections.abc import AsyncIterator

import pytest
import pytest_asyncio

from tests._fixtures.labdata import host_data
from tests.e2e.console._raw import console_answer, ends_at_login, nudge, raw_console, server_cred
from tests.integration.busybox_bed.conftest import _build_guest, _require_guest

pytestmark = [pytest.mark.asyncio, pytest.mark.timeout(180)]

_NE = "bb1350"
_OPTS = host_data(_NE)["console_options"]


@pytest_asyncio.fixture(autouse=True)
async def _line_left_at_login() -> AsyncIterator[None]:
    _require_guest(_NE)
    yield
    answer = await console_answer(_OPTS["server"], _OPTS["port"])
    if ends_at_login(answer):
        return
    host, _version = _build_guest(_NE, term="console")
    try:
        restored = await host.logout()
    finally:
        await host.close()
    # Re-probe rather than trust the reset's own word for it.
    after = await console_answer(_OPTS["server"], _OPTS["port"])
    pytest.fail(
        f"the test left {_NE}'s console ({_OPTS['server']}:{_OPTS['port']}) off its login "
        f"prompt; it answered {answer!r}; `logout` then said {restored.value!r}, and the "
        f"line then answered {after!r}"
        + ("" if ends_at_login(after) else " (STILL NOT at login: reset it by hand)")
    )


async def test_bb1350_console_shows_its_getty():
    server = host_data(_OPTS["server"])
    cred = server_cred(_OPTS["server"])
    async with raw_console(server["ip"], cred.login, cred.password, _OPTS["port"]) as line:
        out = await nudge(line)
    assert re.search(rb"bb1350 login: ?$", out.rstrip(b"\r\n")), out


async def test_bb1350_console_login_and_run():
    host, _version = _build_guest(_NE, term="console")
    try:
        res = (await host.run("id -un")).only
        assert res.retcode == 0, res
        assert res.value.strip() == host_data(_NE)["creds"][0]["login"]
    finally:
        await host.close()
