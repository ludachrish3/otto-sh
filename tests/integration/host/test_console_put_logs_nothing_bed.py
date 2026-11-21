"""A console ``put`` of a new file makes the device log no error (#972).

On a Zephyr device the telnet shell is also a log backend: every log line is
written to whoever holds the console about a second after the event that caused
it. A put used to begin with ``fs rm <dest>``, which fails on a new file, so the
device logged ``failed to unlink path (-2)`` a second later, and a close landing
on that line could kill the guest's telnet backend for its life. A put now
writes and then truncates, which fails nowhere on the sunny-day path.

The test puts a NEW file, then reads the guest's own journal on its hop (the
shared :class:`~tests.integration.host._hop_journal.HopJournal`) and requires
that neither the remove's nor the truncate's error line was logged. The guests
whose only term is ``console`` (behind ser2net) have no unit mapping and are
left out, exactly as in ``test_console_release_bed``.
"""

import asyncio
import contextlib
import uuid
from pathlib import Path

import pytest

from otto.host.factory import create_host_from_dict
from otto.utils import Status
from tests.conftest import _ZEPHYR_BACKEND_NE as _BACKEND_NE
from tests.conftest import element_for, host_data
from tests.integration.host._hop_journal import GUESTS, HopJournal

pytestmark = [pytest.mark.embedded, pytest.mark.timeout(120)]

# Zephyr prints every error-level log line with this tag; ANY such line in the put's
# window is a failure, whatever its text.
_ERR_TAG = "<err>"

# The device logs a line about a second after its cause, so the journal is read
# only after this long.
_LOG_DELAY = 2.0


def _errors(lines: list[str]) -> list[str]:
    return [line for line in lines if _ERR_TAG in line.lower()]


@GUESTS
@pytest.mark.asyncio
async def test_put_of_a_new_file_logs_no_device_error(backend: str, tmp_path: Path) -> None:
    ne = _BACKEND_NE[backend]
    data = {**host_data(ne), "log": "quiet"}
    journal = HopJournal(backend)
    # Every journal call is made while no console is open: it blocks the event
    # loop (see HopJournal._ssh), which would stall a live console.
    position = journal.mark()

    host = create_host_from_dict(data, element=element_for(ne))
    mount = host.filesystem.mount
    assert mount is not None, f"{backend} has no filesystem to put to"
    # A name no earlier run left behind, so the file is NEW on the device.
    name = f"put972_{uuid.uuid4().hex[:8]}.bin"
    src = tmp_path / name
    src.write_bytes(b"otto 972 new file\n")
    try:
        result = await host.put([src], Path(mount))
        assert result.status == Status.Success, result.msg
        # Removing a file that exists logs nothing, so the cleanup stays inside the window.
        await host.rm(f"{mount}/{name}")
        await asyncio.sleep(_LOG_DELAY)
    finally:
        await host.close()
    bad = _errors([entry.text for entry in journal.since(position)])
    assert not bad, f"the guest logged an error during a put of a new file: {bad}"

    # Positive control: the check above passes vacuously if the journal never shows
    # this guest's log lines (a wrong unit, a guest that logs elsewhere). A command
    # that must fail on the device logs an error, so the journal has to show one.
    # The wait comes BEFORE the close: closing the console while that late line is
    # still arriving is the very hazard (#972) this test is about.
    position = journal.mark()
    host = create_host_from_dict(data, element=element_for(ne))
    try:
        # A raise or a failed result is the expected outcome.
        with contextlib.suppress(Exception):
            await host.rm(f"{mount}/missing972_{uuid.uuid4().hex[:8]}.bin")
        await asyncio.sleep(_LOG_DELAY)
    finally:
        await host.close()
    control = [entry.text for entry in journal.since(position)]
    assert _errors(control), (
        "the journal showed no <err> line after a command that must fail: it does not "
        f"show this guest's log lines, so the check above proves nothing. Saw: {control}"
    )
