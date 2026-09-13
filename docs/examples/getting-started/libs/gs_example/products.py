"""What goes on the hosts: the software under test, attached by a provider.

A **product** is a unit of software under test. The project registers a
provider — a function otto runs once per lab-ingested host — and the provider
decides which products that host carries, so lab data never names one.
"""

# doc: begin product
from pathlib import Path

from otto.host.host import Host
from otto.host.product import Product, register_product_provider
from otto.result import Result
from otto.utils import Status


class AgentBinary(Product):
    """The agent binary every Unix host in the bed runs."""

    name = "agent"

    async def stage(self, host: Host) -> Result:
        """Place the locally built binary on the host, installing nothing yet."""
        return await host.put(Path("build/agent"), Path("/opt/agent"))

    async def install(self, host: Host) -> Result:
        """Turn the staged binary into a running install."""
        return await host.run("chmod +x /opt/agent/agent && /opt/agent/agent --install")

    async def uninstall(self, host: Host) -> Result:
        """Take the install back off, leaving nothing behind."""
        return await host.run("/opt/agent/agent --uninstall; rm -rf /opt/agent")

    async def is_installed(self, host: Host) -> bool:
        """Answer the question ``otto run status`` folds across the lab."""
        return (await host.run("test -x /opt/agent/agent")).status is Status.Success


def agent_for(host: Host) -> list[Product] | None:
    """Every Unix host carries the agent; anything else carries no product."""
    return [AgentBinary()] if host.os_type == "unix" else None


register_product_provider(agent_for)
# doc: end product
