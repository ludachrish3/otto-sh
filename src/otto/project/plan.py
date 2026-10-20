"""The install preview: what ``otto run install`` would do, from configuration alone.

:func:`plan_instruction` walks the lab exactly as the real run does (the
orchestrator's own ``_walk_order``: same projects, same order, same applicability
filter) and asks each owned product or dev tool for its
:meth:`~otto.host.product.Product.plan`. Nothing here contacts a host. What a
real run would decide from a host's answer is named as a gap, never guessed:
a project whose actions override the body, a host class that overrides the verb,
``--ensure``'s "already installed" check, the log hauls.
"""

import shlex
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from ..host.host import BaseHost
from ..host.product import ProductPlan
from ..instruction_catalog import PREVIEWABLE_INSTRUCTIONS
from ..params import OptionsSource
from .actions import ProjectActions, _owned
from .options import InstallOptions, InstallToolsOptions, UninstallOptions, UninstallToolsOptions
from .orchestrator import _walk_order

if TYPE_CHECKING:
    from ..host.dev_tool import DevTool
    from ..host.host import Host
    from ..host.product import Product
    from ..instruction_catalog import ProjectInstructionBody
    from ..run_context import OttoContext


@dataclass
class ProductPlanEntry:
    """One product's (or dev tool's) plan, in the position the host installs it."""

    name: str
    plan: ProductPlan


@dataclass
class HostPlan:
    """What one host would do for one project, and what could not be checked.

    ``products`` holds what the instruction installs or removes (for
    ``install-tools``, the dev tools it installs); ``dev_tools`` holds the dev
    tools a removal takes off (``uninstall-tools``).
    """

    host_id: str
    products: list[ProductPlanEntry] = field(default_factory=list)
    gaps: list[str] = field(default_factory=list)
    dev_tools: list[ProductPlanEntry] = field(default_factory=list)


@dataclass
class ProjectPlan:
    """One project's hosts, in lab order, and the project-level gaps."""

    project: str
    hosts: list[HostPlan] = field(default_factory=list)
    gaps: list[str] = field(default_factory=list)


_HOST_VERBS: dict[str, list[str]] = {
    "install": ["stage", "install"],
    "uninstall": ["uninstall"],
    "install-tools": ["install_dev_tools"],
    "uninstall-tools": ["uninstall_dev_tools"],
}
"""The ``BaseHost`` methods a real run of each instruction dispatches to; an
override of any of them on the host's class is a gap."""


def _host_override(host: "Host", name: str) -> str | None:
    """Name the first dispatch method *host*'s class overrides, or None."""
    cls = type(host)
    for verb in _HOST_VERBS[name]:
        if getattr(cls, verb) is not getattr(BaseHost, verb):
            return (
                f"`{cls.__module__}.{cls.__qualname__}.{verb}` replaces the default; "
                "its steps are not previewed"
            )
    return None


def _body_override(actions: ProjectActions, body: "ProjectInstructionBody") -> bool:
    """Whether *actions*' class replaces the base body, marked with ``@instruction`` or not.

    Method identity rather than ``body.project``: a subclass that overrides the
    method without the decorator carries no mark (``body_for`` finds the base
    body), yet the real run dispatches to the subclass method all the same.
    """
    return getattr(type(actions), body.method_name) is not getattr(ProjectActions, body.method_name)


_TOOL_INSTRUCTIONS = ("install-tools", "uninstall-tools")
"""The previewed instructions that act on dev tools rather than products."""


def _attached(host: "Host", name: str, owner: str) -> "list[Product] | list[DevTool]":
    """Return what *host* attaches for *owner*: dev tools for the tool verbs, else products."""
    if name in _TOOL_INSTRUCTIONS:
        return _owned(host.dev_tools, owner)
    return _owned(host.products, owner)


def _ensure_gap(recover_partial: bool) -> str:
    """Say what ``--ensure`` decides from the lab's state, which a preview cannot read."""
    if recover_partial:
        tail = "a partial install is torn down first because --recover-partial is on"
    else:
        tail = "--no-recover-partial installs over a partial install as it stands"
    return (
        "whether the lab is already installed "
        f"(--ensure skips the whole install when it is; {tail})"
    )


def _debug_log_gap(host: "Host") -> str:
    globs = ", ".join(host.debug_log_globs)
    if not globs:
        return "debug logs: no globs declared; nothing is swept"
    return f"debug logs: {globs} are swept once after every project"


def _toolchain_removal_gap(host: "Host") -> str:
    """Say what the lab-wide toolchain removal runs on *host*: one ``rm -f`` per declared tool."""
    cls = type(host)
    if cls.remove_toolchain_tools is not BaseHost.remove_toolchain_tools:
        return (
            f"toolchain tools: `{cls.__module__}.{cls.__qualname__}.remove_toolchain_tools` "
            "replaces the default; its steps are not previewed"
        )
    tools = host.toolchain.tools
    if not tools:
        return "toolchain tools: none declared; nothing is removed"
    paths = ", ".join(f"rm -f {shlex.quote(str(tool.dest / tool.name))}" for tool in tools)
    return f"toolchain tools: removed once after every project (--toolchain): {paths}"


def plan_instruction(name: str, ctx: "OttoContext", kwargs: "dict[str, Any]") -> list[ProjectPlan]:
    """Collect every project's plan for *name*, an instruction with a preview.

    *name* is one of :data:`~otto.instruction_catalog.PREVIEWABLE_INSTRUCTIONS`.
    *kwargs* are the CLI's parsed flags, the same mapping the real run builds
    its options from. ``--ensure``, the two log flags and ``--toolchain``
    become gaps; ``--no-dev`` leaves every host's tool list empty, since the
    real run installs (or removes) no dev tool then.

    Raises:
        ValueError: *name* is not an instruction with a preview.
    """
    if name not in PREVIEWABLE_INSTRUCTIONS:
        raise ValueError(f"{name!r} has no install preview")
    source = OptionsSource.from_kwargs(kwargs)
    install = source.build(InstallOptions) if name == "install" else None
    logs = source.build(UninstallOptions) if name == "uninstall" else None
    tools_cls = {"install-tools": InstallToolsOptions, "uninstall-tools": UninstallToolsOptions}
    tools = source.build(tools_cls[name]) if name in tools_cls else None
    plans: list[ProjectPlan] = []
    for project, actions, body in _walk_order(name, ctx, ctx.ordered_projects, announce=False):
        project_plan = ProjectPlan(project.name)
        # The converge runs lab-wide before any body, so an overriding project is covered too.
        if install is not None and install.ensure:
            project_plan.gaps.append(_ensure_gap(install.recover_partial))
        overridden = _body_override(actions, body)
        if overridden:
            cls = type(actions)
            project_plan.gaps.append(
                f"project `{project.name}`: "
                f"`{cls.__module__}.{cls.__qualname__}.{body.method_name}` "
                f"replaces the default {name}; its steps are not previewed"
            )
        for host in actions.ctx.all_hosts():
            host_plan = HostPlan(host.id)
            # ``--no-dev`` never reaches the host verb, so an override of it would
            # name a step that does not run.
            dispatches = not overridden and (tools is None or tools.dev)
            override = _host_override(host, name) if dispatches else None
            entries = host_plan.dev_tools if name == "uninstall-tools" else host_plan.products
            if override is not None:
                host_plan.gaps.append(override)
            elif dispatches:
                for item in _attached(host, name, project.name):
                    plan = item.plan(host)
                    entries.append(ProductPlanEntry(item.name, plan))
                    host_plan.gaps += [f"{item.name}: {line}" for line in plan.unchecked]
            if isinstance(tools, InstallToolsOptions) and tools.toolchain:
                host_plan.gaps.append(
                    "toolchain tools: installed on this host once after every project "
                    "(--toolchain); not previewed"
                )
            if isinstance(tools, UninstallToolsOptions) and tools.toolchain:
                host_plan.gaps.append(_toolchain_removal_gap(host))
            # A host or project override says the steps are unknown, so the default
            # contract "collected before anything is removed" is not claimed for it.
            if logs is not None and logs.product_logs and override is None and not overridden:
                host_plan.gaps.append(
                    "product logs: each product's get_logs files and its debug_log_globs are "
                    "collected before anything is removed"
                )
            if logs is not None and logs.debug_logs:
                host_plan.gaps.append(_debug_log_gap(host))
            project_plan.hosts.append(host_plan)
        plans.append(project_plan)
    return plans
