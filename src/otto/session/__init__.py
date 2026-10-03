"""
otto.session — the decisions that prepare a run, shared by the CLI and the library.

``otto --lab X <verb>`` and :func:`otto.open_context <otto.context.open_context>`
call the same functions in the same order:
:func:`select_projects` (the project switches), :func:`check_repos` (a broken
active repo stops the run), :func:`build_lab` (the repos' lab sources, host
preferences, inventory and declared containers), then, once the context is
installed, :func:`check_dependencies`. A library
caller who wants otto's console calls :func:`install_logging`. Nothing here
prints or exits; every refusal is a typed :class:`~otto.errors.OttoError`
carrying its facts as attributes.

Every name is exported lazily (PEP 562); see ``otto.config``'s ``__dir__``.
"""

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .activation import check_instruction_active as check_instruction_active
    from .dependencies import check_dependencies as check_dependencies
    from .errors import DependencyRefusedError as DependencyRefusedError
    from .errors import InstructionInactiveError as InstructionInactiveError
    from .errors import LabBuildError as LabBuildError
    from .errors import LoggingLevelsConflictError as LoggingLevelsConflictError
    from .errors import ProjectSelectionError as ProjectSelectionError
    from .errors import RepoLoadError as RepoLoadError
    from .lab import build_lab as build_lab
    from .lab import merge_host_preferences as merge_host_preferences
    from .logs import install_logging as install_logging
    from .logs import merge_logging_levels as merge_logging_levels
    from .projects import DemotedRepo as DemotedRepo
    from .projects import ProjectSelection as ProjectSelection
    from .projects import RepoCheck as RepoCheck
    from .projects import check_project_overlap as check_project_overlap
    from .projects import check_repos as check_repos
    from .projects import select_projects as select_projects

# name -> the module that defines it, imported on first access by __getattr__.
_LAZY_ATTRS: dict[str, str] = {
    "DemotedRepo": "otto.session.projects",
    "DependencyRefusedError": "otto.session.errors",
    "InstructionInactiveError": "otto.session.errors",
    "LabBuildError": "otto.session.errors",
    "LoggingLevelsConflictError": "otto.session.errors",
    "ProjectSelection": "otto.session.projects",
    "ProjectSelectionError": "otto.session.errors",
    "RepoCheck": "otto.session.projects",
    "RepoLoadError": "otto.session.errors",
    "build_lab": "otto.session.lab",
    "check_dependencies": "otto.session.dependencies",
    "check_instruction_active": "otto.session.activation",
    "check_project_overlap": "otto.session.projects",
    "check_repos": "otto.session.projects",
    "install_logging": "otto.session.logs",
    "merge_host_preferences": "otto.session.lab",
    "merge_logging_levels": "otto.session.logs",
    "select_projects": "otto.session.projects",
}


def __getattr__(name: str) -> object:
    """PEP 562 lazy resolver for otto.session's public exports."""
    import importlib

    if name in _LAZY_ATTRS:
        return getattr(importlib.import_module(_LAZY_ATTRS[name]), name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


def __dir__() -> list[str]:
    """Include the lazy exports in dir()/tab-completion; the module dict holds none of them."""
    return sorted(set(globals()) | set(_LAZY_ATTRS))


__all__ = [
    "DemotedRepo",
    "DependencyRefusedError",
    "InstructionInactiveError",
    "LabBuildError",
    "LoggingLevelsConflictError",
    "ProjectSelection",
    "ProjectSelectionError",
    "RepoCheck",
    "RepoLoadError",
    "build_lab",
    "check_dependencies",
    "check_instruction_active",
    "check_project_overlap",
    "check_repos",
    "install_logging",
    "merge_host_preferences",
    "merge_logging_levels",
    "select_projects",
]
