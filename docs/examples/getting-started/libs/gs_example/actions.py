"""Customizing the defaults: a flag of our own on ``otto run install``.

``install`` is a project instruction — one name, one walk shape, and one body
per repo. This repo overrides the body, and its options class inherits the
first-party ``InstallOptions``, so ``--ensure`` and ``--recover-partial`` stay
on the command and ``--variant`` joins them.

``--variant`` itself lives on a small class of its own, ``BedVariant``, which is
registered for ``otto run`` and ``otto test``: a registration reaches every
command of a verb, so only the flag every command should carry is registered.
``otto test --variant`` then steers the install an ``ensure`` marker runs, and
``--ensure`` stays on ``install`` alone.
"""

# doc: begin actions
from typing import Annotated

import typer

from otto import options
from otto.instructions import instruction
from otto.project import InstallOptions, ProjectActions, register_project_actions
from otto.result import Result


# Registered for both verbs: every `otto run` command and `otto test` take
# --variant, and under `otto test` it reaches the install body an
# `@pytest.mark.ensure("installed")` test converges through.
@options(verbs=["run", "test"])
class BedVariant:
    """The one flag this repo adds, shared by ``otto run`` and ``otto test``."""

    variant: Annotated[str, typer.Option(help="Agent build variant to install.")] = "field"


# install's own options, never registered: that would put --ensure on every
# command of both verbs.
@options
class BedInstall(BedVariant, InstallOptions):  # inherits --ensure / --recover-partial
    """``otto run install``'s flags, plus the one this repo adds."""


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
