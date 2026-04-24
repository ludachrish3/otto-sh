"""OttoSuite that exercises class-scoped async fixture survival across
stability iterations using real SSH connections.

This suite catches the bug where stability iterations tear down and
rebuild class-scoped fixtures, causing SSH connections (which cache
event loop references internally) to fail with "Future attached to a
different loop" on subsequent iterations.

The class-scoped fixture establishes an SSH connection to a real host
once.  Each test iteration reuses that connection.  If the stability
implementation incorrectly tears down the fixture between iterations,
the connection breaks.

Usage::

    otto test --iterations 3 TestStabilityFixture
"""

from dataclasses import dataclass
from typing import ClassVar

import pytest
import pytest_asyncio
from _pytest.fixtures import SubRequest
from repo1_common.options import RepoOptions

from otto.configmodule import all_hosts
from otto.host.remoteHost import RemoteHost
from otto.logger import getOttoLogger
from otto.suite import OttoSuite, register_suite
from otto.utils import Status

logger = getOttoLogger()

@dataclass
class _Options(RepoOptions):
    pass


@register_suite()
@pytest.mark.asyncio(loop_scope="class")
class TestStabilityFixture(OttoSuite[_Options]):
    """Verify SSH connections survive across stability iterations."""

    _host: ClassVar[RemoteHost]
    Options = _Options

    @pytest_asyncio.fixture(autouse=True, scope="class", loop_scope="class")
    async def _establish_connection(self, request: SubRequest):
        """Open an SSH connection to the first host and keep it for all iterations."""
        hosts = list(all_hosts())
        assert hosts, "No hosts configured"
        host = hosts[0]

        # Run a command to force the SSH connection open
        result = await host.oneshot("echo stability_setup_ok", timeout=10)
        assert result.status.is_ok, (
            f"Failed to establish connection during setup: {result.output}"
        )

        self.__class__._host = host
        yield

    @pytest.mark.integration
    async def test_connection_alive(self, suite_options: _Options) -> None:
        """The SSH connection from class setup must still work on each iteration."""

        logger.info(f'{suite_options=}')

        result = await self._host.oneshot("echo iteration_ok", timeout=10)
        assert result.status == Status.Success, (
            f"SSH connection failed (likely torn down between iterations): "
            f"{result.output}"
        )
        assert "iteration_ok" in result.output
