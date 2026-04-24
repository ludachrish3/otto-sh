from typing import Any

from .configmodule import (
    ConfigModule as ConfigModule,
)
from .configmodule import (
    all_hosts as all_hosts,
)
from .configmodule import (
    do_for_all_hosts as do_for_all_hosts,
)
from .configmodule import (
    run_on_all_hosts as run_on_all_hosts,
)
from .configmodule import (
    get_host as get_host,
)
from .configmodule import (
    getConfigModule as getConfigModule,
)
from .configmodule import (
    setConfigModule as setConfigModule,
)
from .configmodule import (
    tryGetConfigModule as tryGetConfigModule,
)
from .env import (
    OttoEnv as OttoEnv,
)
from .lab import (
    getLab as getLab,
)
from .repo import (
    Repo,
)
from .repo import (
    applyRepoSettings as applyRepoSettings,
)
from .repo import (
    getRepos as _getRepos,
)
from .version import (
    Version as Version,
)

_env = OttoEnv()
_repos = _getRepos(_env.sutDirs)

# ---------------------------------------------------------------------------
# Completion fast path (Phase B of the tab-completion speedup).
#
# When otto is being invoked by shell completion (_OTTO_COMPLETE is set), the
# expensive side effects in applyRepoSettings() — importing every user init
# module and exec'ing every test file — produce no output the completer
# needs beyond the *names* of the instructions and suites they register. If
# a valid fingerprinted cache exists for the current set of SUT dirs, we
# skip those side effects entirely and let cli/main.py build stub
# subcommands directly from the cached names.
#
# Non-completion invocations always take the slow path and rewrite the cache
# afterward, keeping it fresh for the next completion.
# ---------------------------------------------------------------------------
from .completion_cache import (
    collect_current_commands,
    collect_host_ids,
    is_completion_mode,
    read_cache,
    write_cache,
)

_completion_names: dict[str, Any] | None = None

if is_completion_mode():
    _completion_names = read_cache(_repos)

if _completion_names is None:
    # Slow path: either a normal invocation or a completion cache miss.
    # Repos' settings must be applied before getting the Lab object.
    # Repo settings define where the lab definitions are, among other things.
    applyRepoSettings(_repos)

    # Refresh the cache so the next completion can take the fast path.
    # Safe no-op if OTTO_XDIR isn't set.
    _instructions, _suites = collect_current_commands()
    _host_ids = collect_host_ids(_repos)
    try:
        write_cache(_repos, _instructions, _suites, _host_ids)
    except OSError:
        # Cache writes are best-effort — never block real work on them.
        pass


def getRepos() -> list[Repo]:
    return _repos

def getEnv() -> OttoEnv:
    return _env

def getCompletionNames() -> dict[str, Any] | None:
    """Return cached instruction/suite/host data when the completion fast
    path is active, else ``None``.

    Returned keys:

    - ``instructions`` / ``suites``: each a list of
      ``{"name": str, "options": [...]}`` dicts. :mod:`otto.cli.main` rebuilds
      Typer stubs from them.
    - ``hosts``: a plain list of host-ID strings. :mod:`otto.cli.host`'s
      ``host_id`` completer prefers this over live ``hosts.json`` parsing.
    """
    return _completion_names
