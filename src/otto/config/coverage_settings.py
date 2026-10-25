"""The ``[coverage]`` table in ``.otto/settings.toml``: which project declares it, and its selector.

Pure helpers over the already-parsed project list: which project (if any) declared
a ``[coverage]`` section, and what that section's raw settings dict looks
like. Every coverage code path — ``otto test --cov``, ``otto cov``, and the
coverage awareness behind :attr:`otto.context.OttoContext.cov` — resolves
its coverage settings through :func:`has_cov_config`, :func:`get_cov_project`
and :func:`get_cov_config`, so a lab with multiple projects always picks the
same one, and every refusal for a project list that declares none is built by
:func:`missing_cov_config_error`. :func:`load_hosts_pattern` compiles that section's optional
``hosts`` host-id selector, refusing a malformed value by name.

Lives in ``otto.config``, beneath ``otto.coverage``, because reading a
settings table is configuration, not collection: the context layer needs the
answer too, and it may not depend on the coverage pipeline.
"""

import re
from typing import TYPE_CHECKING, Any

from ..errors import OttoError

if TYPE_CHECKING:
    from .project import Project


class CoverageConfigError(OttoError, ValueError):
    """No ``[coverage]`` section is configured for the resolved project(s).

    Raised by ``otto.coverage.collect.collect_coverage`` before any fetch is
    attempted: with no ``[coverage]`` section there is nothing to resolve a
    host selector, a product's ``cov_dir``, or a tier against.

    Also raised at capture and report time by
    ``otto.coverage.tree.iter_product_dirs`` for a cov directory whose shape
    is wrong — a host directory holding counters or a ``capture.json``
    directly instead of the ``cov/<host>/<product>/`` tree this version of
    otto stages — since that too is a configuration the pipeline cannot run
    against, and there is no migration shim for it.
    """


def has_cov_config(cov: dict[str, Any]) -> bool:
    """Return True when the project actually declared coverage settings."""
    return bool(cov.get("embedded") or cov.get("tiers") or cov.get("hosts"))


def get_cov_project(projects: "list[Project]") -> "Project | None":
    """Return the first project with a ``[coverage]`` section in its settings."""
    for project in projects:
        if has_cov_config(project.settings.get("coverage") or {}):
            return project
    return None


_COVERAGE_SETUP_URL = (
    "https://otto-sh.readthedocs.io/en/stable/setup/coverage.html#switch-coverage-on"
)


def missing_cov_config_error(projects: "list[Project]") -> CoverageConfigError:
    """Build the refusal for a project list in which no project declares coverage.

    The one wording every coverage entry point raises when
    :func:`get_cov_project` finds nothing. It tells the two causes
    apart: no project has a ``[coverage]`` table at all, which names every
    settings file checked; or a table exists but sets none of the keys
    :func:`has_cov_config` counts, which names that file and the fix. Both
    link the setup section that shows a working table.
    """
    from .scope import TOML_SETTINGS_PATH

    def path_of(project: "Project") -> str:
        return str(project.root / TOML_SETTINGS_PATH)

    see = f"See {_COVERAGE_SETUP_URL}"
    targetless = [path_of(p) for p in projects if "coverage" in p.settings]
    if targetless:
        tables = "[coverage] tables in" if len(targetless) > 1 else "[coverage] in"
        declare = "declare" if len(targetless) > 1 else "declares"
        return CoverageConfigError(
            f"the {tables} {', '.join(targetless)} {declare} no hosts, tiers or "
            f"embedded target: add one of them. {see}"
        )
    checked = ", ".join(path_of(p) for p in projects) or "no settings file"
    return CoverageConfigError(f"no project declares [coverage]; checked: {checked}. {see}")


def get_cov_config(projects: "list[Project]") -> dict[str, Any]:
    """Extract the ``[coverage]`` config from the first project that has one."""
    project = get_cov_project(projects)
    return project.settings["coverage"] if project else {}


def load_hosts_pattern(cov_config: dict[str, Any]) -> "re.Pattern[str] | None":
    """Compile the optional ``[coverage].hosts`` host-id selector.

    ``None`` when the key is absent — every lab host is a coverage target.
    The value comes straight out of settings.toml, so a wrong shape is refused
    by name rather than with ``re.compile``'s bare TypeError. The empty string
    is refused too: it *looks* like a selector but would fall through as "no
    selector", silently fanning coverage (and ``otto cov clean``'s deletes)
    out to every host — including the SSH hop the selector exists to exclude.

    Raises:
        CoverageConfigError: On a non-string or empty ``hosts`` value.
    """
    raw = cov_config.get("hosts")
    if raw is None:
        return None
    if not isinstance(raw, str):
        raise CoverageConfigError(
            f"[coverage] hosts must be a string (a host-id regex), got {type(raw).__name__}"
        )
    if not raw:
        raise CoverageConfigError(
            "[coverage] hosts must not be empty — omit the key to select every host"
        )
    return re.compile(raw)
