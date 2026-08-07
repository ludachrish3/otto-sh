"""The shared host error taxonomy: unreachable vs. ran-and-failed.

``exec_or_raise`` is the contract these two types exist to make expressible,
so it is tested as a contract table — one row per way a host call can end,
each asserting WHICH type comes out. The stub host is deliberate: the
question is the mapping, and a real host would only add transport to it.
"""

import pytest

from otto.errors import OttoError
from otto.host.errors import HostCommandError, HostUnreachableError, exec_or_raise
from otto.logger.mode import LogMode
from otto.result import CommandResult
from otto.utils import Status


class _StubHost:
    """Minimal host: one recorded call, one canned outcome."""

    def __init__(self, outcome):
        self.id = "box"
        self._outcome = outcome
        self.calls: list[tuple[str, float, LogMode]] = []

    async def exec(self, cmd, timeout=None, log=LogMode.NORMAL):
        self.calls.append((cmd, timeout, log))
        if isinstance(self._outcome, BaseException):
            raise self._outcome
        return self._outcome


def _result(
    status: Status, *, timed_out: bool = False, msg: str = "", value: str = ""
) -> CommandResult:
    return CommandResult(status, value=value, msg=msg, command="x", retcode=0, timed_out=timed_out)


class _MyUnreachableError(OttoError, RuntimeError):
    """Stand-in for a package's own domain class (otto.link has a real pair)."""


class _MyFailedError(OttoError, RuntimeError):
    """The ran-and-failed half of the same stand-in pair."""


class TestExecOrRaise:
    @pytest.mark.asyncio
    async def test_transport_failure_is_unreachable(self):
        host = _StubHost(ConnectionError("no route"))
        with pytest.raises(HostUnreachableError, match="unreachable running 'uptime'"):
            await exec_or_raise(host, "uptime", timeout=5.0)

    @pytest.mark.asyncio
    async def test_oserror_is_unreachable_too(self):
        host = _StubHost(OSError("socket gone"))
        with pytest.raises(HostUnreachableError):
            await exec_or_raise(host, "uptime", timeout=5.0)

    @pytest.mark.asyncio
    async def test_timeout_is_unreachable_not_command_failure(self):
        """A timed-out command produced no verdict about the host's state."""
        host = _StubHost(_result(Status.Failed, timed_out=True))
        with pytest.raises(HostUnreachableError, match=r"timed out after 3\.0s"):
            await exec_or_raise(host, "uptime", timeout=3.0)

    @pytest.mark.asyncio
    async def test_non_ok_is_a_command_failure(self):
        host = _StubHost(_result(Status.Failed, msg="boom"))
        with pytest.raises(HostCommandError, match="'uptime' failed on 'box': boom"):
            await exec_or_raise(host, "uptime", timeout=5.0)

    @pytest.mark.asyncio
    async def test_ok_returns_the_result_and_forwards_timeout_and_log(self):
        host = _StubHost(_result(Status.Success, value="ok"))
        result = await exec_or_raise(host, "uptime", timeout=9.0)
        assert result.value == "ok"
        # QUIET by default: every caller is a read done on the caller's behalf,
        # not a command the user asked to see.
        assert host.calls == [("uptime", 9.0, LogMode.QUIET)]

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("outcome", "expected"),
        [
            (ConnectionError("down"), _MyUnreachableError),
            (_result(Status.Failed, timed_out=True), _MyUnreachableError),
            (_result(Status.Failed, msg="boom"), _MyFailedError),
        ],
        ids=["transport", "timed_out", "non_ok"],
    )
    async def test_caller_substitutes_its_own_pair(self, outcome, expected):
        """The SEQUENCE is shared; the taxonomy belongs to the caller.

        otto.link raises LinkHostUnreachableError/LinkCommandFailedError
        through this exact seam, so an `except` around an impair can mean
        "link work failed" without catching every host call otto makes.
        """
        host = _StubHost(outcome)
        with pytest.raises(expected):
            await exec_or_raise(
                host, "uptime", timeout=3.0, unreachable=_MyUnreachableError, failed=_MyFailedError
            )


def test_the_two_classes_are_peers_not_a_hierarchy():
    """Neither implies the other — catching one must not catch the other."""
    assert not issubclass(HostUnreachableError, HostCommandError)
    assert not issubclass(HostCommandError, HostUnreachableError)
    for cls in (HostUnreachableError, HostCommandError):
        assert issubclass(cls, OttoError)
        assert issubclass(cls, RuntimeError)
