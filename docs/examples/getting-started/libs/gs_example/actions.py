"""Customizing the defaults: a flag of our own on ``otto run install``.

``install`` is a project instruction — one name, one walk shape, and one body
per project. This project overrides the body to record which build variant goes
on every host before it installs as otto would.

``--variant`` lives on a small class of its own, ``BedVariant``, registered for
``"run install"`` alone, so it joins ``--ensure`` and ``--recover-partial`` on
that one command and on no other. The body reads it with ``otto.get_options``;
under a command that does not offer it (``otto test`` with an ``ensure``
marker) the read gives the class's default.
"""

# doc: begin actions
from typing import Annotated

import typer

import otto
from otto import options
from otto.instructions import instruction
from otto.project import InstallOptions, ProjectActions, register_project_actions
from otto.result import Result


# Registered for one command: `otto run install --variant debug`.
@options(verbs=["run install"])
class BedVariant:
    """The one flag this project adds to ``otto run install``."""

    variant: Annotated[str, typer.Option(help="Agent build variant to install.")] = "field"


@register_project_actions
class BedActions(ProjectActions):
    """This project's lab lifecycle: otto's defaults, plus the variant we pin."""

    @instruction(options=InstallOptions)  # otto's own class: --ensure / --recover-partial
    async def install(self, opts: InstallOptions) -> Result:
        """Record the variant on every fleet host, then install as otto would."""
        variant = otto.get_options(BedVariant).variant
        for host in self.ctx.all_hosts():
            await host.run(f"echo variant={variant} > /opt/agent/variant")
        return await super().install(opts)


# doc: end actions
