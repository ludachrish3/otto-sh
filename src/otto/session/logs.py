"""The project-aware logging install: otto's console plus every project's [logging.levels]."""

from pathlib import Path
from typing import TYPE_CHECKING

from .errors import LoggingLevelsConflictError

if TYPE_CHECKING:
    from ..config.project import Project


def merge_logging_levels(projects: "list[Project]") -> dict[str, str]:
    """Union every project's ``[logging.levels]``; a disagreement is an error.

    The same logger set to the same level by two projects is fine (a shared
    vendor SDK quieted twice), two DIFFERENT levels is not — last-one-wins
    would make the floor depend on ``OTTOPATH`` order, so the operator is
    told which two projects to reconcile.
    """
    levels: dict[str, str] = {}
    sources: dict[str, str] = {}
    for project in projects:
        for name, level in project.logging_levels.items():
            if name in levels and levels[name] != level:
                raise LoggingLevelsConflictError(
                    f"[logging.levels] {name}: {sources[name]} says {levels[name]}, "
                    f"{project.name} says {level} — set one value"
                )
            levels[name] = level
            # setdefault, not assignment: with projects A, B (agreeing) and C
            # (differing), an assignment would credit B for the value A
            # established, and the operator would edit the wrong file and see
            # the same error again.
            sources.setdefault(name, project.name)
    return levels


def install_logging(
    *,
    log_level: str = "INFO",
    output_dir: "Path | None" = None,
    overrides: "dict[str, str] | None" = None,
    show_time: bool = False,
) -> None:
    """Install otto's logging with every project's ``[logging.levels]`` and the host-output filter.

    The project-aware opt-in for a library caller;
    :func:`otto.logger.install <otto.logger.management.install>` is the raw
    primitive underneath, which knows nothing of projects. The levels
    are merged first, so a conflict between projects refuses before anything is
    installed. An *overrides* entry wins over the projects'; an unknown level
    name in *overrides* raises from the stdlib after the console handler is
    installed. The
    :class:`~otto.host.host.HostFilter` goes on the console handlers only, so
    ``verbose.log`` keeps every host's output.
    """
    # The composition root: importing it installs the initiator that composes
    # the projects when nothing has yet (spec 2026-10-11 placement-and-phases §3.12).
    from .. import bootstrap

    del bootstrap
    from ..declared import get_projects
    from ..host.host import HostFilter
    from ..logger import management

    levels = merge_logging_levels(get_projects())
    levels.update(overrides or {})
    management.install(log_level, output_dir=output_dir, show_time=show_time, overrides=levels)
    management.attach_console_suppress_filter(HostFilter())
