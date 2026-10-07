"""The repo-aware logging install: otto's console plus every repo's [logging.levels]."""

from pathlib import Path
from typing import TYPE_CHECKING

from .errors import LoggingLevelsConflictError

if TYPE_CHECKING:
    from ..config.repo import Repo


def merge_logging_levels(repos: "list[Repo]") -> dict[str, str]:
    """Union every repo's ``[logging.levels]``; a disagreement is an error.

    The same logger set to the same level by two repos is fine (a shared
    vendor SDK quieted twice), two DIFFERENT levels is not — last-one-wins
    would make the floor depend on ``OTTO_SUT_DIRS`` order, so the operator is
    told which two repos to reconcile.
    """
    levels: dict[str, str] = {}
    sources: dict[str, str] = {}
    for repo in repos:
        for name, level in repo.logging_levels.items():
            if name in levels and levels[name] != level:
                raise LoggingLevelsConflictError(
                    f"[logging.levels] {name}: {sources[name]} says {levels[name]}, "
                    f"{repo.name} says {level} — set one value"
                )
            levels[name] = level
            # setdefault, not assignment: with repos A, B (agreeing) and C
            # (differing), an assignment would credit B for the value A
            # established, and the operator would edit the wrong file and see
            # the same error again.
            sources.setdefault(name, repo.name)
    return levels


def install_logging(
    *,
    log_level: str = "INFO",
    output_dir: "Path | None" = None,
    overrides: "dict[str, str] | None" = None,
    show_time: bool = False,
) -> None:
    """Install otto's logging with every repo's ``[logging.levels]`` and the host-output filter.

    The repo-aware opt-in for a library caller;
    :func:`otto.logger.install <otto.logger.management.install>` is the raw
    primitive underneath, which knows nothing of repos. The levels
    are merged first, so a conflict between repos refuses before anything is
    installed. An *overrides* entry wins over the repos'; an unknown level
    name in *overrides* raises from the stdlib after the console handler is
    installed. The
    :class:`~otto.host.host.HostFilter` goes on the console handlers only, so
    ``verbose.log`` keeps every host's output.
    """
    from ..bootstrap import get_repos
    from ..host import HostFilter
    from ..logger import management

    levels = merge_logging_levels(get_repos())
    levels.update(overrides or {})
    management.install(log_level, output_dir=output_dir, show_time=show_time, overrides=levels)
    management.attach_console_suppress_filter(HostFilter())
