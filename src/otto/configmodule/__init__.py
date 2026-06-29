"""Public API for the configmodule package — lab loading, host access, and repo settings."""

import contextlib
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from ..models.settings import OttoEnvSettings

from .configmodule import (
    all_hosts as all_hosts,
)
from .configmodule import (
    do_for_all_hosts as do_for_all_hosts,
)
from .configmodule import (
    get_host as get_host,
)
from .configmodule import (
    get_lab as get_lab,
)
from .configmodule import (
    run_on_all_hosts as run_on_all_hosts,
)
from .env import (
    load_otto_env as load_otto_env,
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
    Repo,
)
from .repo import (
    apply_repo_settings as apply_repo_settings,
)
from .repo import (
    get_repos as _get_repos,
)
from .version import (
    Version as Version,
)

_env = load_otto_env()
_repos = _get_repos(_env.sut_dirs)

# ---------------------------------------------------------------------------
# Completion fast path (Phase B of the tab-completion speedup).
#
# When otto is being invoked by shell completion (_OTTO_COMPLETE is set), the
# expensive side effects in apply_repo_settings() — importing every user init
# module and exec'ing every test file — produce no output the completer
# needs beyond the *names* of the instructions and suites they register. If
# a valid fingerprinted cache exists for the current set of SUT dirs, we
# skip those side effects entirely and let cli/main.py build stub
# subcommands directly from the cached names.
#
# Non-completion invocations always take the slow path and rewrite the cache
# afterward, keeping it fresh for the next completion.
# ---------------------------------------------------------------------------
from .completion_cache import (  # noqa: E402 — import after completion-mode branch (fast-path optimization)
    collect_backend_names,
    collect_current_commands,
    collect_docker_capable_host_ids,
    collect_host_ids,
    collect_reservation_usernames,
    is_completion_mode,
    read_cache,
    write_cache,
)


# Defined BEFORE apply_repo_settings() below: that call exec's user/test files at
# import time, and those files may `from otto.configmodule import get_repos`. If
# these accessors were defined later (after apply_repo_settings) the import would
# hit a partially-initialized module → circular ImportError.
def get_repos() -> list[Repo]:
    """Return the list of ``Repo`` objects built from the configured SUT directories."""
    return _repos


def get_env() -> "OttoEnvSettings":
    """Return the ``OttoEnvSettings`` instance loaded at module import time."""
    return _env


_completion_names: dict[str, Any] | None = None

if is_completion_mode():
    _completion_names = read_cache(_repos)

if _completion_names is None:
    # Slow path: either a normal invocation or a completion cache miss.
    # Repos' settings must be applied before getting the Lab object.
    # Repo settings define where the lab definitions are, among other things.
    apply_repo_settings(_repos)

    # Refresh the cache so the next completion can take the fast path.
    # Safe no-op if OTTO_XDIR isn't set.
    _instructions, _suites = collect_current_commands()
    _host_ids = collect_host_ids(_repos)
    _docker_host_ids = collect_docker_capable_host_ids(_repos)
    _backends = collect_backend_names()
    _usernames = collect_reservation_usernames(_repos)
    # Cache writes are best-effort — never block real work on them.
    with contextlib.suppress(OSError):
        write_cache(
            _repos,
            _instructions,
            _suites,
            _host_ids,
            _docker_host_ids,
            term_backends=_backends["term_backends"],
            transfer_backends=_backends["transfer_backends"],
            usernames=_usernames,
        )


def get_completion_names() -> dict[str, Any] | None:
    """Return cached instruction/suite/host data when the completion fast path is active.

    Return ``None`` when not active.

    Returned keys:

    - ``instructions`` / ``suites``: each a list of
      ``{"name": str, "options": [...]}`` dicts. :mod:`otto.cli.main` rebuilds
      Typer stubs from them.
    - ``hosts``: a plain list of host-ID strings. :mod:`otto.cli.host`'s
      ``host_id`` completer prefers this over live ``hosts.json`` parsing.
    - ``term_backends``: a ``list[str]`` of registered term backend names.
      :mod:`otto.cli.host`'s ``--term`` completer prefers this over the live
      registry.
    - ``transfer_backends``: a list of
      ``{"name": str, "host_families": [str, ...]}`` dicts for registered
      transfer backends. :mod:`otto.cli.host`'s ``--transfer`` completer
      prefers this over the live registry.
    """
    return _completion_names
