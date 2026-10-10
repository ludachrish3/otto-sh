"""The install preview: what ``otto run install`` would do, from configuration alone.

:func:`plan_instruction` walks the lab exactly as the real run does (the
orchestrator's own ``_walk_order``: same repos, same order, same applicability
filter) and asks each owned product or dev tool for its
:meth:`~otto.host.product.Product.plan`. Nothing here contacts a host. What a
real run would decide from a host's answer is named as a gap, never guessed:
a repo whose actions override the body, a host class that overrides the verb,
``--ensure``'s "already installed" check, the log hauls.
"""

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from ..host.host import BaseHost
from ..host.product import ProductPlan
from ..instructions import PREVIEWABLE_INSTRUCTIONS
from ..params import OptionsSource
from .actions import ProjectActions, _owned
from .options import InstallOptions, InstallToolsOptions, UninstallOptions
from .orchestrator import _walk_order

if TYPE_CHECKING:
    from ..context import OttoContext
    from ..host.dev_tool import DevTool
    from ..host.host import Host
    from ..host.product import Product
    from ..instructions import ProjectInstructionBody


@dataclass
class ProductPlanEntry:
    """One product's (or dev tool's) plan, in the position the host installs it."""

    name: str
    plan: ProductPlan


@dataclass
class HostPlan:
    """What one host would do for one repo, and what could not be checked."""

    host_id: str
    products: list[ProductPlanEntry] = field(default_factory=list)
    gaps: list[str] = field(default_factory=list)


@dataclass
class RepoPlan:
    """One repo's hosts, in lab order, and the repo-level gaps."""

    repo: str
    hosts: list[HostPlan] = field(default_factory=list)
    gaps: list[str] = field(default_factory=list)


_HOST_VERBS: dict[str, list[str]] = {
    "install": ["stage", "install"],
    "uninstall": ["uninstall"],
    "install-tools": ["install_dev_tools"],
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

    Method identity rather than ``body.repo``: a subclass that overrides the
    method without the decorator carries no mark (``body_for`` finds the base
    body), yet the real run dispatches to the subclass method all the same.
    """
    return getattr(type(actions), body.method_name) is not getattr(ProjectActions, body.method_name)


def _attached(host: "Host", name: str, owner: str) -> "list[Product] | list[DevTool]":
    """Return what *host* attaches for *owner*: dev tools for ``install-tools``, else products."""
    if name == "install-tools":
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
    return f"debug logs: {globs} are swept once after every repo"


def plan_instruction(name: str, ctx: "OttoContext", kwargs: "dict[str, Any]") -> list[RepoPlan]:
    """Collect every repo's plan for *name* (``install``, ``uninstall`` or ``install-tools``).

    *kwargs* are the CLI's parsed flags, the same mapping the real run builds
    its options from. ``--ensure``, the two log flags and ``--toolchain``
    become gaps; ``--no-dev`` leaves every host's product list empty, since the
    real run installs no dev tool then.

    Raises:
        ValueError: *name* is not an instruction with a preview.
    """
    if name not in PREVIEWABLE_INSTRUCTIONS:
        raise ValueError(f"{name!r} has no install preview")
    source = OptionsSource.from_kwargs(kwargs)
    install = source.build(InstallOptions) if name == "install" else None
    logs = source.build(UninstallOptions) if name == "uninstall" else None
    tools = source.build(InstallToolsOptions) if name == "install-tools" else None
    plans: list[RepoPlan] = []
    for repo, actions, body in _walk_order(name, ctx, ctx.ordered_repos, announce=False):
        repo_plan = RepoPlan(repo.name)
        # The converge runs lab-wide before any body, so an overriding repo is covered too.
        if install is not None and install.ensure:
            repo_plan.gaps.append(_ensure_gap(install.recover_partial))
        overridden = _body_override(actions, body)
        if overridden:
            cls = type(actions)
            repo_plan.gaps.append(
                f"repo `{repo.name}`: `{cls.__module__}.{cls.__qualname__}.{body.method_name}` "
                f"replaces the default {name}; its steps are not previewed"
            )
        for host in actions.ctx.all_hosts():
            host_plan = HostPlan(host.id)
            # ``install-tools --no-dev`` never reaches the host verb, so an override
            # of it would name a step that does not run.
            dispatches = not overridden and (tools is None or tools.dev)
            override = _host_override(host, name) if dispatches else None
            if override is not None:
                host_plan.gaps.append(override)
            elif dispatches:
                for item in _attached(host, name, repo.name):
                    plan = item.plan(host)
                    host_plan.products.append(ProductPlanEntry(item.name, plan))
                    host_plan.gaps += [f"{item.name}: {line}" for line in plan.unchecked]
            if tools is not None and tools.toolchain:
                host_plan.gaps.append(
                    "toolchain tools: installed on this host once after every repo "
                    "(--toolchain); not previewed"
                )
            # A host or repo override says the steps are unknown, so the default
            # contract "collected before anything is removed" is not claimed for it.
            if logs is not None and logs.product_logs and override is None and not overridden:
                host_plan.gaps.append(
                    "product logs: each product's get_logs files and its debug_log_globs are "
                    "collected before anything is removed"
                )
            if logs is not None and logs.debug_logs:
                host_plan.gaps.append(_debug_log_gap(host))
            repo_plan.hosts.append(host_plan)
        plans.append(repo_plan)
    return plans
