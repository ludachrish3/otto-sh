"""The bed's checks of the netcat listener the probe opens, shared by every guest that runs them.

``otto host <id> probe`` opens one netcat listener on the host for a moment,
asks each listener check to count it, and closes it. Two things are checked
on a real guest: that a finished survey leaves the host's listeners as they
were (:func:`listeners_of`, read before and after), and that a survey
cancelled while its listener is open still ends that listener within the
backend's reap bound (:func:`assert_an_interrupted_survey_leaves_no_listener`).
One spelling here, so a BusyBox guest and the Ubuntu guest are held to the
same check.
"""

import asyncio
import contextlib
from functools import partial
from unittest.mock import patch

import pytest

from otto.host.host import BaseHost
from otto.host.survey import run_survey
from otto.host.survey.engine import _run_only
from otto.host.survey.inventory import run_inventory
from otto.host.survey.verdict import SURVEY_BUDGET_S
from otto.host.transfer.nc import _INTERRUPTED_REAP_TIMEOUT, NcFileTransfer
from otto.utils import wait_for_async
from tests._fixtures.bed_hygiene import netcat_listeners

_ONE_SECOND = 1.0
"""What the cancelled survey's return may take beyond the reap bound."""

_LEFT_LISTENER_GONE_WITHIN_S = 10.0
"""How long after the cancelled survey returns the guest may still list a netcat line.

Measured on BusyBox 1.35.0: its ``timeout`` runs as a process beside the
command it guards, not as its parent, and notices within about a second that
the command ended, so its own ``timeout 90 nc -l -p PORT`` line can outlive
the netcat by that long. A listener really left behind stays for its
90-second cap, so ten seconds tells the two apart under any load.
"""


async def listeners_of(host: BaseHost, label: str) -> list:
    """Every listener on *host*, read with the survey's own inventory, in a stable order.

    One command goes through ``run`` first, so its session is open before
    the read. The inventory bounds each read at five seconds, and a survey
    opens that session under the login's own budget before its inventory
    runs. A read made here on a session not yet open would spend the bound on
    the login: 2.6 of the five seconds on the emulated Ubuntu guest with the
    bed idle, and more than five beside a loaded lane, against 0.4 seconds
    on the open session.
    """
    await host.run("true")
    inv = await run_inventory(partial(_run_only, host), elevate=False)
    assert not inv.error, (label, inv)
    return sorted(inv.listeners, key=repr)


async def assert_an_interrupted_survey_leaves_no_listener(
    host: BaseHost, observer: BaseHost, label: str
) -> None:
    """Cancel a survey of *host* while its listener is open; no netcat listener is left.

    *observer* is a second host object for the same guest, so each observation
    runs on a session of its own and never waits behind the survey's commands.

    One observation is made first, with no netcat listener on the host. The
    survey then runs as a task and is held at the moment its listener has
    been counted (or the wait for it ended): one observation must then list
    a netcat listener, and every line it lists is the probe's, since there
    were none before. More than one line is expected when the host caps the
    listener's lifetime: the ``timeout`` wrapper and the netcat it runs each
    show as a line. The survey is cancelled there and its real teardown runs.
    The cancelled survey must return within the backend's reap bound and one
    second of the cancel; the guest must then list no netcat listener within
    :data:`_LEFT_LISTENER_GONE_WITHIN_S`, polled, since a BusyBox ``timeout``
    process can outlive its netcat by a second. Both bounds are elapsed time
    on a real guest, which load from other workers lengthens on an emulated
    one, so a caller marks its test ``serial_timing``.
    """
    loop = asyncio.get_running_loop()
    before = await netcat_listeners(observer, label)
    assert before == [], f"{label}: a netcat listener was on the host before the survey: {before}"

    listener_open = asyncio.Event()
    real_count = NcFileTransfer._count_until_up

    async def count_then_hold(self, run, checks, into, listener) -> None:
        await real_count(self, run, checks, into, listener)
        listener_open.set()
        await asyncio.Event().wait()

    # The survey's listener is open for only a few commands, so a poll could
    # miss it. The survey is held right after the listener was counted, until
    # the cancel; only this wait is replaced, never the listener's teardown.
    with patch.object(NcFileTransfer, "_count_until_up", count_then_hold):
        task = asyncio.ensure_future(run_survey(host))
        try:
            waiter = asyncio.ensure_future(listener_open.wait())
            try:
                await asyncio.wait(
                    {task, waiter}, timeout=SURVEY_BUDGET_S, return_when=asyncio.FIRST_COMPLETED
                )
            finally:
                waiter.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await waiter
            assert not task.done(), (
                f"{label}: the survey ended before it opened a listener: {_outcome(task)}"
            )
            assert listener_open.is_set(), (
                f"{label}: the survey opened no listener within {SURVEY_BUDGET_S:g}s"
            )
            listed = await netcat_listeners(observer, label)
            assert listed, (
                f"{label}: with the survey's listener open the host listed no netcat "
                f"listener: {listed}"
            )
            cancelled_at = loop.time()
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            returned_s = loop.time() - cancelled_at
        finally:
            # A failure above leaves the survey running; end it without hiding that failure.
            if not task.done():
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await task
    bound = _INTERRUPTED_REAP_TIMEOUT + _ONE_SECOND
    assert returned_s <= bound, (
        f"{label}: the cancelled survey took {returned_s:.1f}s to return, "
        f"over {bound:.1f}s (the reap bound and one second)"
    )
    still: list[str] = []

    async def none_listed() -> bool:
        still[:] = await netcat_listeners(observer, label)
        return not still

    await wait_for_async(
        none_listed,
        _LEFT_LISTENER_GONE_WITHIN_S,
        interval=0.2,
        on_timeout=lambda: (
            f"{label}: the cancelled survey left a netcat listener: still listed "
            f"{_LEFT_LISTENER_GONE_WITHIN_S:g}s after it returned: {still}"
        ),
    )


def _outcome(task: "asyncio.Future") -> str:
    """A finished task's result or exception, for a failure message."""
    if task.cancelled():
        return "cancelled"
    exc = task.exception()
    return repr(exc) if exc is not None else repr(task.result())
