"""What goes on the hosts: the software under test.

The ``agent`` product is declared in ``.otto/settings.toml`` — artifact,
where it stages, how it is checked and removed — and names this class for
the one step a command string cannot express: passing the element's role from
lab metadata to the install. A command string can set ``GCOV_PREFIX``
(``{cov_dir}``) and chain checks with the shell, but a value that differs per
host is not one of its two placeholders, so that install is code.
"""

# doc: begin product-class
import shlex

from otto.host import DeclaredProduct, Host
from otto.result import Result


class AgentBinary(DeclaredProduct):
    """The agent binary every Unix host in the bed runs."""

    async def install(self, host: Host) -> Result:
        """Install the staged binary, telling it the element's role from lab metadata.

        ``GCOV_PREFIX`` redirects the instrumented build's ``.gcda`` writes to
        :attr:`cov_dir` — the one directory ``otto cov get`` fetches from —
        and ``GCOV_PREFIX_STRIP`` drops the build machine's leading path
        components so the tree under it stays shallow. The role is the one
        per-host value: it differs by element, so no declared string carries it.
        """
        binary = (await self.resolved_stage_dir(host)) / self.artifact.name
        element = host.element
        role = element.metadata.get("role", "node") if element is not None else "node"
        env = f"GCOV_PREFIX={self.cov_dir} GCOV_PREFIX_STRIP=3"
        return await host.run(
            f"chmod +x {binary} && {env} {binary} --install --role {shlex.quote(str(role))}"
        )
        # doc: end product-class
