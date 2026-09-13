"""The repo's own tooling on the hosts: one dev tool, attached by provider.

A dev tool is the product shape deliberately — same methods, same provider
seam — with a different lifecycle: it is placed
by ``otto run install-tools``, removed by ``otto run cleanup``, and never part
of the installed-or-not answer ``otto run status`` gives about the software
under test.
"""

# doc: begin dev-tool
from pathlib import Path

from otto.host.dev_tool import DevTool, register_dev_tool_provider
from otto.host.host import Host
from otto.result import Result
from otto.utils import Status


class TraceProbe(DevTool):
    """A trace helper the project installs alongside the agent, never under test."""

    name = "trace-probe"

    async def stage(self, host: Host) -> Result:
        """Place the probe script on the host."""
        return await host.put(Path("tools/probe.sh"), Path("/opt/agent/tools"))

    async def install(self, host: Host) -> Result:
        """Make the staged script runnable."""
        return await host.run("chmod +x /opt/agent/tools/probe.sh")

    async def uninstall(self, host: Host) -> Result:
        """Remove the probe — what ``otto run cleanup`` does and ``uninstall`` does not."""
        return await host.run("rm -f /opt/agent/tools/probe.sh")

    async def is_installed(self, host: Host) -> bool:
        """Dev tools answer for themselves; they are never part of the product answer."""
        return (await host.run("test -x /opt/agent/tools/probe.sh")).status is Status.Success


def probe_for(host: Host) -> list[DevTool] | None:
    """Give every Unix host the probe -- the product's host set, decided independently."""
    return [TraceProbe()] if host.os_type == "unix" else None


register_dev_tool_provider(probe_for)
# doc: end dev-tool
