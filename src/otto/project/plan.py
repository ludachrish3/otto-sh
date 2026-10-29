"""The install preview: what ``otto run install`` would do, from configuration alone.

:func:`plan_instruction` walks the lab exactly as the real run does (the
orchestrator's own ``_walk_order``: same projects, same order, same applicability
filter) and asks each owned product or dev tool for its
:meth:`~otto.host.product.Product.plan`. Nothing here contacts a host. What a
real run would decide from a host's answer is named as a gap, never guessed:
a project whose actions override the body, a host class that overrides the verb,
``--ensure``'s "already installed" check, the log hauls.
"""

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from ..host.host import BaseHost
from ..host.product import ProductPlan
from ..host.toolchain import toolchain_removal_command
from ..instruction_catalog import PREVIEWABLE_INSTRUCTIONS
from ..params import OptionsSource
from .actions import ProjectActions, _owned
from .options import (
    CleanupOptions,
    InstallOptions,
    InstallToolsOptions,
    UninstallOptions,
    UninstallToolsOptions,
)
from .orchestrator import _walk_order

if TYPE_CHECKING:
    from ..config.project import Project
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
class TailStep:
    """One lab-wide step ``cleanup`` takes once, after every project's body.

    *host_id* names the fleet host the step runs on; ``None`` is a step over
    the whole lab (the impairment reset, the tunnel reap).
    """

    label: str
    line: str
    host_id: str | None = None


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
    "cleanup": ["uninstall", "uninstall_dev_tools"],
}
"""The ``BaseHost`` methods a real run of each instruction dispatches to; an
override of any of them on the host's class is a gap."""


def _replaced(host: "Host", verb: str) -> str | None:
    """Name ``<class>.<verb>`` when *host*'s class overrides that ``BaseHost`` method, else None."""
    cls = type(host)
    if getattr(cls, verb) is getattr(BaseHost, verb):
        return None
    return f"`{cls.__module__}.{cls.__qualname__}.{verb}` replaces the default"


def _host_override(host: "Host", name: str) -> str | None:
    """Name the first dispatch method *host*'s class overrides, or None."""
    for verb in _HOST_VERBS[name]:
        replaced = _replaced(host, verb)
        if replaced is not None:
            return f"{replaced}; its steps are not previewed"
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


def _toolchain_removals(host: "Host") -> list[str]:
    """Return each ``rm -f`` that :meth:`~otto.host.host.BaseHost.remove_toolchain_tools` runs."""
    return [toolchain_removal_command(tool) for tool in host.toolchain.tools]


def _toolchain_removal_gap(host: "Host") -> str:
    """Say what the lab-wide toolchain removal runs on *host*: one ``rm -f`` per declared tool."""
    replaced = _replaced(host, "remove_toolchain_tools")
    if replaced is not None:
        return f"toolchain tools: {replaced}; its steps are not previewed"
    removals = _toolchain_removals(host)
    if not removals:
        return "toolchain tools: none declared; nothing is removed"
    return f"toolchain tools: removed once after every project (--toolchain): {', '.join(removals)}"


_PRODUCT_LOGS_GAP = (
    "product logs: each product's get_logs files and its debug_log_globs are "
    "collected before anything is removed"
)


def _add_entries(
    host: "Host", items: "list[Product] | list[DevTool]", entries: list[ProductPlanEntry]
) -> list[str]:
    """Append each item's plan to *entries*; return its unchecked reads, prefixed with its name."""
    gaps: list[str] = []
    for item in items:
        plan = item.plan(host)
        entries.append(ProductPlanEntry(item.name, plan))
        gaps += [f"{item.name}: {line}" for line in plan.unchecked]
    return gaps


def _docker_staging_gap(host: "Host", project: "Project") -> str | None:
    """Say which ``rm -rf`` cleanup's docker-staging step runs on *host*, or None.

    The keys and the command come from the step's own functions, so the
    preview names what the run removes. Whether a tree exists and whether a
    stack still has containers are live reads, so the line names every key the
    step would consider and says which removals depend on a stack.
    """
    # Lazy: the docker layer is needed only when a cleanup is previewed.
    from ..docker.compose import staging_keys_on
    from ..docker.staging import cleanup_command
    from ..host.docker_host import parent_staging_root

    keys = staging_keys_on(host, project)
    if not keys:
        return None
    root = parent_staging_root(host)
    removals = [
        cleanup_command(root, k.key)
        + (" unless that stack still has containers" if k.stack else "")
        for k in keys
    ]
    return f"docker staging: {'; '.join(removals)}"


def _cleanup_host_plan(
    host: "Host", project: "Project", *, uninstall_replaced: bool, product_logs: bool
) -> HostPlan:
    """Plan ``cleanup``'s three steps on *host*: *project*'s products, dev tools, docker staging.

    The product half is ``self.uninstall(...)``, so a project override of
    ``uninstall`` (*uninstall_replaced*) leaves it unpreviewed and the project
    names it; a host class override of either host verb is named on the host,
    and the other steps are still previewed.
    """
    owner = project.name
    host_plan = HostPlan(host.id)
    if not uninstall_replaced:
        replaced = _host_override(host, "uninstall")
        if replaced is not None:
            host_plan.gaps.append(replaced)
        else:
            host_plan.gaps += _add_entries(host, _owned(host.products, owner), host_plan.products)
            if product_logs:
                host_plan.gaps.append(_PRODUCT_LOGS_GAP)
    replaced = _host_override(host, "uninstall-tools")
    if replaced is not None:
        host_plan.gaps.append(replaced)
    else:
        host_plan.gaps += _add_entries(host, _owned(host.dev_tools, owner), host_plan.dev_tools)
    staging = _docker_staging_gap(host, project)
    if staging is not None:
        host_plan.gaps.append(staging)
    return host_plan


def plan_lab_tail(name: str, ctx: "OttoContext", kwargs: "dict[str, Any]") -> list[TailStep]:
    """Return the lab-wide steps *name* takes once after every project, in run order.

    Only ``cleanup`` has them: each fleet host's debug-log sweep (with
    ``--debug-logs``) and toolchain removal, then the impairment reset and the
    tunnel reap over the whole lab, each only when its flag is on. They run
    whether or not any project applies. A host class that replaces a step's
    host verb is named rather than guessed. Nothing here contacts a host, so
    which links are impaired and which hosts carry a tunnel stay unchecked.
    """
    if name != "cleanup":
        return []
    opts = OptionsSource.from_kwargs(kwargs).build(CleanupOptions)
    steps: list[TailStep] = []
    for host in ctx.all_hosts():
        if opts.debug_logs:
            replaced = _replaced(host, "get_debug_logs")
            if replaced is not None:
                steps.append(TailStep("debug logs", f"{replaced}; not previewed", host.id))
            elif host.debug_log_globs:
                fetched = ", ".join(host.debug_log_globs)
                steps.append(TailStep("debug logs", f"fetch {fetched}", host.id))
        replaced = _replaced(host, "remove_toolchain_tools")
        if replaced is not None:
            steps.append(TailStep("toolchain", f"{replaced}; not previewed", host.id))
        else:
            steps += [TailStep("toolchain", line, host.id) for line in _toolchain_removals(host)]
    if opts.reset_impairments:
        steps.append(
            TailStep(
                "impairments",
                "would attempt to clear otto-owned impairments on eligible links "
                "(live eligibility unchecked)",
            )
        )
    if opts.remove_tunnels:
        steps.append(TailStep("tunnels", "would remove otto tunnels on every tunnel-capable host"))
    return steps


def plan_instruction(name: str, ctx: "OttoContext", kwargs: "dict[str, Any]") -> list[ProjectPlan]:
    """Collect every project's plan for *name*, an instruction with a preview.

    *name* is one of :data:`~otto.instruction_catalog.PREVIEWABLE_INSTRUCTIONS`.
    *kwargs* are the CLI's parsed flags, the same mapping the real run builds
    its options from. ``--ensure``, the two log flags and ``--toolchain``
    become gaps; ``--no-dev`` leaves every host's tool list empty, since the
    real run installs (or removes) no dev tool then. ``cleanup`` plans each
    project's products and then its dev tools; its lab-wide steps after every
    project are :func:`plan_lab_tail`'s.

    Raises:
        ValueError: *name* is not an instruction with a preview.
    """
    if name not in PREVIEWABLE_INSTRUCTIONS:
        raise ValueError(f"{name!r} has no install preview")
    source = OptionsSource.from_kwargs(kwargs)
    install = source.build(InstallOptions) if name == "install" else None
    logs = source.build(UninstallOptions) if name == "uninstall" else None
    cleanup = source.build(CleanupOptions) if name == "cleanup" else None
    tools_cls = {"install-tools": InstallToolsOptions, "uninstall-tools": UninstallToolsOptions}
    tools = source.build(tools_cls[name]) if name in tools_cls else None
    plans: list[ProjectPlan] = []
    for project, actions, body in _walk_order(name, ctx, ctx.ordered_projects, announce=False):
        project_plan = ProjectPlan(project.name)
        # The converge runs lab-wide before any body, so an overriding project is covered too.
        if install is not None and install.ensure:
            project_plan.gaps.append(_ensure_gap(install.recover_partial))
        overridden = _body_override(actions, body)
        cls = type(actions)
        if overridden:
            project_plan.gaps.append(
                f"project `{project.name}`: "
                f"`{cls.__module__}.{cls.__qualname__}.{body.method_name}` "
                f"replaces the default {name}; its steps are not previewed"
            )
        if cleanup is not None:
            # cleanup's product half is ``self.uninstall(...)``: an uninstall
            # override replaces those steps, and only those.
            uninstall_replaced = cls.uninstall is not ProjectActions.uninstall
            if uninstall_replaced and not overridden:
                project_plan.gaps.append(
                    f"project `{project.name}`: `{cls.__module__}.{cls.__qualname__}.uninstall` "
                    "replaces the default uninstall, which cleanup calls; its steps are not "
                    "previewed"
                )
            if not overridden:
                project_plan.hosts += [
                    _cleanup_host_plan(
                        host,
                        project,
                        uninstall_replaced=uninstall_replaced,
                        product_logs=cleanup.product_logs,
                    )
                    for host in actions.ctx.all_hosts()
                ]
            else:
                project_plan.hosts += [HostPlan(host.id) for host in actions.ctx.all_hosts()]
            plans.append(project_plan)
            continue
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
                host_plan.gaps.append(_PRODUCT_LOGS_GAP)
            if logs is not None and logs.debug_logs:
                host_plan.gaps.append(_debug_log_gap(host))
            project_plan.hosts.append(host_plan)
        plans.append(project_plan)
    return plans
