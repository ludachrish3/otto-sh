"""``otto.check`` and ``otto.link`` run a privileged command through one core.

:func:`~otto.check.fingerprint.check_root_run` and
:func:`~otto.link.manage._root_run` are both
:func:`~otto.host.errors.root_run_or_unreachable` with their own unreachable
error. Driven with the same input, they elevate on the same answer and map
the same failures, naming the caller's own command. What they SEND differs
by each caller's own input, not by a second decision: a check's probes are
compound scripts and wrap themselves in ``one_command``, and link's
commands are each one simple command, sent as given so a sudoers rule
naming ``tc`` still matches.
"""

import pytest

from otto.check.errors import CheckHostUnreachableError
from otto.check.fingerprint import check_root_run, one_command
from otto.link.manage import _IMPAIR_HOST_TIMEOUT, LinkHostUnreachableError, _root_run
from otto.result import CommandResult, Results
from otto.utils import Status

_CMD = "tc qdisc del dev eth1 root; echo done > /tmp/x"

_CHECK_TIMEOUT = 7.5
"""An explicit check timeout, distinct from link's ``_IMPAIR_HOST_TIMEOUT``."""


def _check(host, cmd):
    return check_root_run(host, cmd, timeout=_CHECK_TIMEOUT)


_ENTRIES = pytest.mark.parametrize(
    ("entry", "unreachable", "sent", "timeout"),
    [
        (_check, CheckHostUnreachableError, one_command(_CMD), _CHECK_TIMEOUT),
        (_root_run, LinkHostUnreachableError, _CMD, _IMPAIR_HOST_TIMEOUT),
    ],
    ids=["check", "link"],
)


class _Host:
    id = "h"

    def __init__(self, user: str, *, raises: Exception | None = None, timed_out=False):
        self.current_user = user
        self.raises = raises
        self.timed_out = timed_out
        self.sent: list[tuple[str, bool]] = []
        self.timeouts: list[float] = []

    async def run(self, cmd: str, *, sudo: bool, timeout: float, **_kw: object) -> Results:
        self.sent.append((cmd, sudo))
        self.timeouts.append(timeout)
        if self.raises is not None:
            raise self.raises
        status = Status.Error if self.timed_out else Status.Failed
        result = CommandResult(command=cmd, value="", status=status, timed_out=self.timed_out)
        return Results.collect([result])


@_ENTRIES
@pytest.mark.parametrize(("user", "sudo"), [("admin", True), ("root", False)])
@pytest.mark.asyncio
async def test_both_entry_points_elevate_on_the_same_answer(
    entry, unreachable, sent, timeout, user, sudo
):
    """A non-ok result is returned, never raised; root or not, the text sent is the same.

    Mutation: wrap in the core and link's ``sent`` goes red (a sudoers rule
    naming ``tc`` would stop matching); drop the wrap from check and its
    ``sent`` goes red.
    """
    host = _Host(user)
    result = await entry(host, _CMD)
    assert host.sent == [(sent, sudo)]
    assert result.status is Status.Failed


@_ENTRIES
@pytest.mark.asyncio
async def test_both_entry_points_map_a_transport_failure_the_same_way(
    entry, unreachable, sent, timeout
):
    with pytest.raises(unreachable) as caught:
        await entry(_Host("admin", raises=ConnectionResetError("gone")), _CMD)
    assert str(caught.value) == (
        f"host 'h' unreachable running {_CMD!r}: ConnectionResetError('gone')"
    )


@_ENTRIES
@pytest.mark.asyncio
async def test_both_entry_points_map_a_timeout_the_same_way(entry, unreachable, sent, timeout):
    """The message names the timeout the command was actually given."""
    host = _Host("admin", timed_out=True)
    with pytest.raises(unreachable) as caught:
        await entry(host, _CMD)
    assert host.timeouts == [timeout]
    assert str(caught.value) == (
        f"host 'h' unreachable running {_CMD!r}: timed out after {timeout}s"
    )
