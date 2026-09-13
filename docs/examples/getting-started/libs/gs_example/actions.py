"""Customizing the defaults: a flag of our own on ``otto run install``.

``install`` is a project instruction — one name, one walk shape, and one body
per repo. This repo overrides the body, and its options class inherits the
first-party ``InstallOptions``, so ``--ensure`` and ``--recover-partial`` stay
on the command and ``--variant`` joins them.
"""

# doc: begin actions
from typing import Annotated

import typer

from otto import options
from otto.cli.run import instruction
from otto.project import InstallOptions, ProjectActions, register_project_actions
from otto.result import Result


@options
class BedInstall(InstallOptions):  # inherits --ensure / --recover-partial
    """``otto run install``'s flags, plus the one this repo adds."""

    variant: Annotated[str, typer.Option(help="Agent build variant to install.")] = "field"


@register_project_actions
class BedActions(ProjectActions):
    """This repo's lab lifecycle: otto's defaults, plus the variant we pin."""

    @instruction(options=BedInstall)
    async def install(self, opts: BedInstall) -> Result:
        """Record the variant on every fleet host, then install as otto would."""
        for host in self.ctx.all_hosts():
            await host.run(f"echo variant={opts.variant} > /opt/agent/variant")
        return await super().install(opts)


# doc: end actions
