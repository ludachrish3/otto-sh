"""A cancelled ``exec`` on an embedded host is stopped before the cancel returns.

The ``exec-cancel`` conformance contract does not run on a Zephyr guest: the
bed firmware has no command that blocks (#1066), and a Zephyr guest is never
driven to a cancellation on the bed (#260). This is its arm: an embedded
host over a scripted Zephyr shell, whose command hangs until it is
interrupted, cancelled through ``EmbeddedHost.exec`` (so through its line
turn) once the command has begun.
"""

import asyncio

import pytest

from otto.host.command_frame import ZephyrFrame
from tests.unit.host._session_feed import scripted_shell
from tests.unit.host.test_exec_frame import embedded_host_over

pytestmark = [pytest.mark.asyncio]


@pytest.mark.timeout(10)
async def test_a_cancelled_embedded_exec_is_stopped_before_the_cancel_returns():
    s = await scripted_shell(frame=ZephyrFrame(), hang=frozenset({"long"}))
    host = embedded_host_over(s)
    task = asyncio.ensure_future(host.exec("long"))
    await s.began.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert s.ops.count("write:'\\x03'") == 1
    assert not s._needs_recovery
    assert s.alive


@pytest.mark.timeout(10)
async def test_the_embedded_host_answers_the_next_command_after_a_cancel():
    s = await scripted_shell(
        frame=ZephyrFrame(),
        hang=frozenset({"long"}),
        replies={"kernel version": ("Zephyr 3.7", 0)},
    )
    host = embedded_host_over(s)
    task = asyncio.ensure_future(host.exec("long"))
    await s.began.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    r = await host.exec("kernel version")
    assert (r.value, r.retcode) == ("Zephyr 3.7", 0)
