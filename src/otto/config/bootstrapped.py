"""What bootstrap discovered, read through ``otto.config``.

The configured repos (as found, and in dependency order), the startup
environment settings, whether bootstrap has started, and the completion fast
path's cached names. Each accessor imports :mod:`otto.bootstrap` in its own
body, so reading one runs discovery or bootstrap on first use and importing
this module runs neither.
"""

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from ..models.settings import OttoEnvSettings
    from .repo import Repo


def get_repos() -> "list[Repo]":
    """Return the ``Repo`` objects for the configured SUT directories (bootstraps lazily)."""
    from ..bootstrap import bootstrap

    return bootstrap().repos


def is_bootstrapped() -> bool:
    """Report whether bootstrap has already STARTED (running or done), WITHOUT forcing it.

    A probe, not a trigger: unlike :func:`get_repos`/:func:`get_ordered_repos`,
    reading this never runs discovery or a repo's init imports. True for the
    whole span from the phase-2 import pass onward — including mid-bootstrap,
    where ``get_repos()`` already answers correctly and for free — and false
    only for a process that has not started bootstrap at all. Callers that
    must not pay bootstrap's cost as a side effect of merely asking — e.g.
    :func:`otto.declared.declared_for_host`, reached from
    ``create_host_from_dict`` in bare-library and pre-bootstrap processes —
    check this first and treat ``False`` as "nothing loaded yet" rather than
    calling :func:`get_repos`.
    """
    from ..bootstrap import is_bootstrapped as _is

    return _is()


def get_ordered_repos() -> "list[Repo]":
    """Return configured repos in dependency-topological order (bootstraps lazily).

    Dependencies first, dependents after — the walk order the ``otto.project``
    orchestrator installs in (and reverses to uninstall). Skipped repos
    (unsatisfied required deps) are absent, exactly as they are absent from
    phase-2 registration.
    """
    from ..bootstrap import bootstrap

    return bootstrap().ordered_repos


def get_env() -> "OttoEnvSettings":
    """Return the startup environment settings (bootstraps discovery lazily)."""
    from ..bootstrap import discover

    return discover().env


def get_completion_names() -> dict[str, Any] | None:
    """Return cached instruction/suite/host data when the completion fast path is active.

    Return ``None`` when not active.

    Returned keys:

    - ``instructions`` / ``suites``: each a list of
      ``{"name": str, "options": [...]}`` dicts. :mod:`otto.cli.main` rebuilds
      Typer stubs from them.
    - ``hosts``: a plain list of host-ID strings. :mod:`otto.cli.host`'s
      ``host_id`` completer prefers this over live ``lab.json`` parsing.
    - ``term_backends``: a ``list[str]`` of registered term backend names.
      :mod:`otto.cli.host`'s ``--term`` completer prefers this over the live
      registry.
    - ``transfer_backends``: a list of
      ``{"name": str, "host_families": [str, ...]}`` dicts for registered
      transfer backends. :mod:`otto.cli.host`'s ``--transfer`` completer
      prefers this over the live registry.
    """
    from ..bootstrap import get_completion_names as _get

    return _get()
