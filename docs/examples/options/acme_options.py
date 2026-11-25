"""The example the options guide (``docs/setup/options-classes.md``) shows.

A product supports a debug level, and ``--debug-level 3`` on the command line
must reach the function that installs the product on a host. The page
includes the pieces between the ``# doc: begin`` and ``# doc: end`` markers;
``tests/unit/docs/test_options_example.py`` runs them.
"""

import logging
from dataclasses import dataclass
from typing import Annotated

import pytest
import typer

import otto
from otto.host import Host, Product, UnixHost
from otto.instructions import instruction
from otto.lab import all_hosts
from otto.result import Result

logger = logging.getLogger(__name__)


# doc: begin project-wide
@otto.options(verbs=["run", "test"])
class Acme:
    """The acme project's options, on every `otto run` and `otto test` command."""

    debug_level: Annotated[int, typer.Option(help="Product debug log level.")] = 0
    # doc: end project-wide


class Agent(Product):
    """The agent the project installs on every host."""

    name = "agent"

    async def stage(self, host: Host) -> Result:
        """Nothing to copy: the agent ships in the host image."""
        return await host.run("true")

    # doc: begin product
    async def install(self, host: Host) -> Result:
        """Install the agent, logging at --debug-level."""
        level = otto.get_options(Acme).debug_level
        return await host.run(f"agent-install --log-level {level}")
        # doc: end product

    async def uninstall(self, host: Host) -> Result:
        """Remove the agent."""
        return await host.run("agent-uninstall")

    async def is_installed(self, host: Host) -> bool:
        """Whether the agent answers."""
        return (await host.run("agent-status")).is_ok


# doc: begin host-method
@dataclass(slots=True, kw_only=True)
class AcmeHost(UnixHost):
    """The acme project's own host class."""

    async def restart_agent(self) -> Result:
        """Restart the agent, logging at --debug-level."""
        level = otto.get_options(Acme).debug_level
        return await self.run(f"agent-restart --log-level {level}")
        # doc: end host-method


# doc: begin test
@pytest.mark.ensure("installed")
class TestAgent:
    """The agent, once installed."""

    async def test_agent_logs_at_the_debug_level(self) -> None:
        """The agent logs at the level --debug-level gave."""
        level = otto.get_options(Acme).debug_level
        for host in all_hosts():
            result = await host.run("agent-ctl log-level")
            assert result.only.value == str(level)
            # doc: end test


# doc: begin instruction
@instruction()
async def set_log_level() -> None:
    """Set the agent's log level on every host to --debug-level."""
    level = otto.get_options(Acme).debug_level
    for host in all_hosts():
        await host.run(f"agent-ctl log-level {level}")
        logger.info(f"{host.id}: agent logs at level {level}")
        # doc: end instruction
