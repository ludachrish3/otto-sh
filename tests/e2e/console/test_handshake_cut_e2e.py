"""A first line the shell ran only the tail of is sent again whole (#761).

A console server that delivers input in pieces (ser2net's default) lets
login(1) flush the start of otto's first line, so the shell runs only its
tail. Whether login flushes is physical timing no test controls, so this one
cuts the line itself: on test2's live console shell it re-runs the
handshake (``enter_frame``) with a transport that throws away the line's
head and sends only the tail the bed once ran, from inside the
calibration's ``2>/dev/null``.

That tail used to print READY with no calibration line in front of it, and
the session then kept ``\\r\\n`` in every value. Now a tail can print only
half a marker, so the handshake waits, sends the whole line again, and
measures the terminal from that.
"""

import asyncio
import re

import pytest

from otto.host.command_frame import BashFrame
from tests._fixtures.labdata import make_console_host

pytestmark = [
    pytest.mark.integration,
    pytest.mark.asyncio,
    pytest.mark.timeout(60),
    # One worker for the module: test2's console is one single-client line.
    pytest.mark.xdist_group("console_e2e"),
    pytest.mark.usefixtures("test2_console"),
]

_TAIL_RAN = re.compile(r"dev/null: No such file or directory[^\n]*\n")
"""What only the cut tail prints: it starts at ``dev/null`` and runs it as a command."""


async def test_a_probe_line_that_lost_its_head_is_resent_whole(monkeypatch):
    host = make_console_host("test2")
    try:
        # Open the live shell first, so no login flush is in play: the shell
        # is at a prompt with echo off, and the tail below is what it runs.
        assert (await host.run("true")).only.retcode == 0
        session = host._session_mgr._session
        assert session is not None
        original = session.write_transport
        writes: list[object] = []
        cut: dict[str, str] = {}

        async def cut_head(data):
            if "full" not in cut and isinstance(data, str) and "stty -echo" in data:
                # Inside the calibration's leading `2>/dev/null`, so the tail
                # runs `dev/null` as a command and skips the calibration.
                k = data.rindex("dev/null printf")
                cut["full"], cut["tail"] = data, data[k:]
                writes.append(cut["tail"])
                await original(data=cut["tail"])
                # Proof the shell executed the tail, read before confirm_live
                # starts its own expect: READY can only follow this line.
                cut["tail_ran"] = await asyncio.wait_for(
                    session.read_transport_until(pattern=_TAIL_RAN), 5
                )
                return
            writes.append(data)
            await original(data=data)

        monkeypatch.setattr(session, "write_transport", cut_head)
        await session.enter_frame(frame=BashFrame())

        assert "No such file or directory" in cut["tail_ran"]
        # The mapping first: on a line whose tail answered, there is no
        # calibration line, and this is what the defect looked like.
        assert session.terminal_mapping == "onlcr"
        assert writes[0] == cut["tail"]
        assert cut["full"] in writes[1:], "the line was never sent again whole"
        assert (await host.run("printf 'a\\n\\n'")).only.value == "a\n"
    finally:
        await host.close()
