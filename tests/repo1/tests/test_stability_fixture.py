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

import logging
from typing import ClassVar

import pytest
import pytest_asyncio
from _pytest.fixtures import SubRequest
from repo1_common.options import RepoOptions

from otto import options
from otto.config import all_hosts
from otto.host.unix_host import UnixHost
from otto.suite import OttoSuite
from otto.utils import Status

logger = logging.getLogger(__name__)


@options
class _Options(RepoOptions):
    pass


@pytest.mark.asyncio(loop_scope="class")
class TestStabilityFixture(OttoSuite[_Options]):
    """Verify SSH connections survive across stability iterations."""

    _host: ClassVar[UnixHost]
    Options = _Options

    @pytest_asyncio.fixture(autouse=True, scope="class", loop_scope="class")
    async def _establish_connection(self, request: SubRequest):
        """Open an SSH connection to the first host and keep it for all iterations."""
        hosts = list(all_hosts())
        assert hosts, "No hosts configured"
        host = hosts[0]

        # Run a command to force the SSH connection open
        result = await host.exec("echo stability_setup_ok", timeout=10)
        assert result.status.is_ok, f"Failed to establish connection during setup: {result.value}"

        self.__class__._host = host
        yield

    @pytest.mark.integration
    async def test_connection_alive(self, suite_options: _Options) -> None:
        """The SSH connection from class setup must still work on each iteration."""

        logger.info(f"{suite_options=}")

        result = await self._host.exec("echo iteration_ok", timeout=10)
        assert result.status == Status.Success, (
            f"SSH connection failed (likely torn down between iterations): {result.value}"
        )
        assert "iteration_ok" in result.value
