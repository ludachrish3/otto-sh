"""Project-level lifecycle: per-project actions over the whole lab.

The layer between a project's products and a one-line ``install`` command.
:class:`~otto.project.actions.ProjectActions` is what one project does to the
fleet -- otto's owner-scoped defaults, or the subclass that project registered
with :func:`~otto.project.actions.register_project_actions` -- and
:mod:`otto.project.state` is the vocabulary its answers are given in.

Composition ACROSS projects (dependency-ordered walks, the single host-level debug
sweep, the ``ensure_*`` converge layer) is :mod:`otto.project.orchestrator`'s,
and its module-level functions are re-exported here: ``otto.project.install()``
and friends are the lab-level verbs that instructions, suites, and fixtures
call with no arguments at all.

Every name is exported lazily (PEP 562), the shape every otto package shares:
naming a state or an options class does not load the orchestrator. The
resolver does not write a resolved name back into the module dict; see
``otto.config``'s ``__dir__`` for why.
"""

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .actions import PROJECT_ACTIONS as PROJECT_ACTIONS
    from .actions import ProjectActions as ProjectActions
    from .actions import actions_for as actions_for
    from .actions import register_project_actions as register_project_actions
    from .options import CleanupOptions as CleanupOptions
    from .options import GetLogsOptions as GetLogsOptions
    from .options import InstallOptions as InstallOptions
    from .options import InstallToolsOptions as InstallToolsOptions
    from .options import StatusOptions as StatusOptions
    from .options import UninstallOptions as UninstallOptions
    from .options import UninstallToolsOptions as UninstallToolsOptions
    from .orchestrator import cleanliness as cleanliness
    from .orchestrator import cleanup as cleanup
    from .orchestrator import ensure_clean as ensure_clean
    from .orchestrator import ensure_installed as ensure_installed
    from .orchestrator import ensure_uninstalled as ensure_uninstalled
    from .orchestrator import get_logs as get_logs
    from .orchestrator import install as install
    from .orchestrator import install_tools as install_tools
    from .orchestrator import is_clean as is_clean
    from .orchestrator import is_uninstalled as is_uninstalled
    from .orchestrator import status as status
    from .orchestrator import uninstall as uninstall
    from .orchestrator import uninstall_tools as uninstall_tools
    from .plan import HostPlan as HostPlan
    from .plan import ProductPlanEntry as ProductPlanEntry
    from .plan import ProjectPlan as ProjectPlan
    from .plan import TailStep as TailStep
    from .plan import plan_instruction as plan_instruction
    from .plan import plan_lab_tail as plan_lab_tail
    from .state import Cleanliness as Cleanliness
    from .state import CleanlinessItem as CleanlinessItem
    from .state import CleanlinessKind as CleanlinessKind
    from .state import CleanlinessReport as CleanlinessReport
    from .state import InstallState as InstallState
    from .state import ProjectScopeSummary as ProjectScopeSummary
    from .state import ProjectStatus as ProjectStatus
    from .state import combine_install_states as combine_install_states

# name -> the module that defines it, imported on first access by __getattr__.
_LAZY_ATTRS: dict[str, str] = {
    "PROJECT_ACTIONS": "otto.project.actions",
    "ProjectActions": "otto.project.actions",
    "actions_for": "otto.project.actions",
    "register_project_actions": "otto.project.actions",
    "CleanupOptions": "otto.project.options",
    "GetLogsOptions": "otto.project.options",
    "InstallOptions": "otto.project.options",
    "InstallToolsOptions": "otto.project.options",
    "StatusOptions": "otto.project.options",
    "UninstallOptions": "otto.project.options",
    "UninstallToolsOptions": "otto.project.options",
    "cleanliness": "otto.project.orchestrator",
    "cleanup": "otto.project.orchestrator",
    "ensure_clean": "otto.project.orchestrator",
    "ensure_installed": "otto.project.orchestrator",
    "ensure_uninstalled": "otto.project.orchestrator",
    "get_logs": "otto.project.orchestrator",
    "install": "otto.project.orchestrator",
    "install_tools": "otto.project.orchestrator",
    "is_clean": "otto.project.orchestrator",
    "is_uninstalled": "otto.project.orchestrator",
    "status": "otto.project.orchestrator",
    "uninstall": "otto.project.orchestrator",
    "uninstall_tools": "otto.project.orchestrator",
    "HostPlan": "otto.project.plan",
    "ProductPlanEntry": "otto.project.plan",
    "ProjectPlan": "otto.project.plan",
    "TailStep": "otto.project.plan",
    "plan_instruction": "otto.project.plan",
    "plan_lab_tail": "otto.project.plan",
    "Cleanliness": "otto.project.state",
    "CleanlinessItem": "otto.project.state",
    "CleanlinessKind": "otto.project.state",
    "CleanlinessReport": "otto.project.state",
    "InstallState": "otto.project.state",
    "ProjectStatus": "otto.project.state",
    "ProjectScopeSummary": "otto.project.state",
    "combine_install_states": "otto.project.state",
}


def __getattr__(name: str) -> object:
    """PEP 562 lazy resolver for otto.project's public exports."""
    import importlib

    if name in _LAZY_ATTRS:
        return getattr(importlib.import_module(_LAZY_ATTRS[name]), name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


def __dir__() -> list[str]:
    """Include the lazy exports in dir()/tab-completion; the module dict holds none of them."""
    return sorted(set(globals()) | set(_LAZY_ATTRS))


__all__ = [
    "PROJECT_ACTIONS",
    "Cleanliness",
    "CleanlinessItem",
    "CleanlinessKind",
    "CleanlinessReport",
    "CleanupOptions",
    "GetLogsOptions",
    "HostPlan",
    "InstallOptions",
    "InstallState",
    "InstallToolsOptions",
    "ProductPlanEntry",
    "ProjectActions",
    "ProjectPlan",
    "ProjectScopeSummary",
    "ProjectStatus",
    "StatusOptions",
    "TailStep",
    "UninstallOptions",
    "UninstallToolsOptions",
    "actions_for",
    "cleanliness",
    "cleanup",
    "combine_install_states",
    "ensure_clean",
    "ensure_installed",
    "ensure_uninstalled",
    "get_logs",
    "install",
    "install_tools",
    "is_clean",
    "is_uninstalled",
    "plan_instruction",
    "plan_lab_tail",
    "register_project_actions",
    "status",
    "uninstall",
    "uninstall_tools",
]
