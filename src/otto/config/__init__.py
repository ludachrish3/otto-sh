"""Public API for the config package — lab loading, host access, and repo settings."""

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from ..models.settings import OttoEnvSettings

from .env import (
    load_otto_env as load_otto_env,
)
from .fleet import (
    all_hosts as all_hosts,
)
from .fleet import (
    do_for_all_hosts as do_for_all_hosts,
)
from .fleet import (
    get_host as get_host,
)
from .fleet import (
    get_lab as get_lab,
)
from .fleet import (
    run_on_all_hosts as run_on_all_hosts,
)
from .lab import (
    load_lab as load_lab,
)
from .repo import (
    DockerCompose as DockerCompose,
)
from .repo import (
    DockerImage as DockerImage,
)
from .repo import (
    DockerSettings as DockerSettings,
)
from .repo import (
    MonitorSettings as MonitorSettings,
)
from .repo import (
    Repo,
)
from .version import (
    Version as Version,
)


def get_repos() -> list[Repo]:
    """Return the ``Repo`` objects for the configured SUT directories (bootstraps lazily)."""
    from ..bootstrap import bootstrap

    return bootstrap().repos


def get_env() -> "OttoEnvSettings":
    """Return the startup environment settings (bootstraps discovery lazily)."""
    from ..bootstrap import discover

    return discover()[0]


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
