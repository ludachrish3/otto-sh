"""What goes on the hosts: the software under test, attached by a provider.

A **product** is a unit of software under test. The project registers a
provider — a function otto runs once per lab-ingested host — and the provider
decides which products that host carries, so lab data never names one.
"""

# doc: begin product
from pathlib import Path

from otto.host.host import Host
from otto.host.product import Product, register_product_provider, scan_for_instrumentation
from otto.result import Result
from otto.utils import Status


class AgentBinary(Product):
    """The agent binary every Unix host in the bed runs."""

    name = "agent"
    cov_dir = "/var/cov/agent"

    async def stage(self, host: Host) -> Result:
        """Place the locally built binary on the host, installing nothing yet."""
        return await host.put(Path("build/agent"), Path("/opt/agent"))

    async def install(self, host: Host) -> Result:
        """Turn the staged binary into a running install.

        ``GCOV_PREFIX`` redirects the instrumented build's ``.gcda`` writes to
        :attr:`cov_dir` — the one directory ``otto cov get`` fetches from —
        and ``GCOV_PREFIX_STRIP`` drops the build machine's leading path
        components so the tree under it stays shallow.
        """
        return await host.run(
            "chmod +x /opt/agent/agent && "
            f"GCOV_PREFIX={self.cov_dir} GCOV_PREFIX_STRIP=3 /opt/agent/agent --install"
        )

    async def uninstall(self, host: Host) -> Result:
        """Take the install back off, leaving nothing behind."""
        return await host.run("/opt/agent/agent --uninstall; rm -rf /opt/agent")

    async def is_installed(self, host: Host) -> bool:
        """Answer the question ``otto run status`` folds across the lab."""
        return (await host.run("test -x /opt/agent/agent")).status is Status.Success

    def instrumented(self) -> bool | None:
        """Scan the locally built binary for gcov markers."""
        return scan_for_instrumentation(Path("build/agent"))


def agent_for(host: Host) -> list[Product] | None:
    """Every Unix host carries the agent; anything else carries no product."""
    return [AgentBinary()] if host.os_type == "unix" else None


register_product_provider(agent_for)
# doc: end product
