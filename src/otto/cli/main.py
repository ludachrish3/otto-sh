import importlib
import os
import sys
from pathlib import Path
from typing import (
    Annotated,
    Any,
    Optional,
    # override,     only available in Python >= 3.12
)

import typer

from ..configmodule import (
    getCompletionNames,
    getConfigModule,
    getLab,
    getRepos,
    setConfigModule,
)
from ..configmodule.env import (
    DEFAULT_LOG_RETENTION_DAYS,
    FIELD_DEFAULT_ENV_VAR,
    FIELD_PRODUCT_ENV_VAR,
    LAB_ENV_VAR,
    LOG_DAYS_ENV_VAR,
    LOG_LVL_ENV_VAR,
    LOG_RICH_ENV_VAR,
    SUT_DIRS_ENV_VAR,
    XDIR_ENV_VAR,
    OttoEnv,
)
from ..logger import initOttoLogger
from ..utils import (
    splitOnCommas,
)
from ..version import getVersion

__version__ = getVersion()

# TODO: Should rich help menus be optional?
# Uncomment the line below to remove rich help menu formatting globally
#typer.core.HAS_RICH = False

_fieldDefault = OttoEnv.getEnvVar(FIELD_DEFAULT_ENV_VAR) is not None
"""Determines the default for debug or field. If this variable is set to anything at all, then field is the default."""

DESCRIPTION = f'''
O.T.T.O. (Our Trusty Testing Orchestrator)

If a development repo is under test, then {SUT_DIRS_ENV_VAR} must be set in your environment.
It is a comma-separated list of paths to repo root directories.

'''

def version_callback(version: bool):
    if version:
        from rich import print as rprint
        rprint(f"otto version: {__version__}")
        raise typer.Exit

def clear_autocomplete_cache_callback(value: bool) -> None:
    if not value:
        return
    from rich import print as rprint

    from ..configmodule.completion_cache import _cache_path, clear_cache

    cache_path = _cache_path()
    removed = clear_cache()
    if removed:
        rprint(f'Removed completion cache: {cache_path}')
    elif cache_path is None:
        rprint('No completion cache to clear (OTTO_XDIR is not set).')
    else:
        rprint(f'No completion cache found at {cache_path}.')
    raise typer.Exit


def list_labs_callback(value: bool):
    if value:
        from rich import print as rprint
        from rich.panel import Panel
        from rich.table import Table

        # Extract lab search paths from all repos
        panels: list[Panel] = []
        for repo in getRepos():
            panels.append(repo.getLabPanel())

        table = Table(show_header=False, show_footer=False, box=None, expand=True, padding=(0,1,1,1))
        for _ in panels:
            table.add_column(ratio=1)
        table.add_row(*panels)
        rprint(table)

        raise typer.Exit

def log_level_callback(value: str):
    return value.upper()

app = typer.Typer(
    no_args_is_help=True,
    help=DESCRIPTION,
    invoke_without_command=True,
    pretty_exceptions_show_locals=True,
    context_settings={
        'help_option_names': ['-h', '--help'],
    },
)

@app.callback(
    no_args_is_help=True,
    help=DESCRIPTION,
)
def main(
    ctx: typer.Context,
    labs: Annotated[list[str],
        typer.Option('--lab', '-l',
            envvar=LAB_ENV_VAR,
            callback=splitOnCommas,
            metavar='COMMA SEPARATED LIST',
            help='Name of lab(s) to reserve and use.'
        )
    ],

    xdir: Annotated[Path,
        typer.Option('--xdir', '-x',
            envvar=XDIR_ENV_VAR,
            help='Directory in which to store logs and artifacts.'
        ),
    ] = Path(),

    debug: Annotated[bool,
        typer.Option('--field/--debug',
            envvar=FIELD_PRODUCT_ENV_VAR,
            help='Use field or debug products.',
        )
    ] = _fieldDefault,

    log_days: Annotated[int,
        typer.Option(
            min=0,
            envvar=LOG_DAYS_ENV_VAR,
            help='Number of days to retain logs.',
        )
    ] = DEFAULT_LOG_RETENTION_DAYS,

    log_level: Annotated[str,
        typer.Option(
            envvar=LOG_LVL_ENV_VAR,
            metavar='LOG LEVEL',
            callback=log_level_callback,
            help='Level at which to log.',
        )
    ] = 'INFO',

    rich_log_file: Annotated[bool,
        typer.Option(
            envvar=LOG_RICH_ENV_VAR,
            help='Determines whether log files have rich formatting.',
        )
    ] = False,

    verbose: Annotated[bool,
        typer.Option('--verbose', '-v',
                    ),
    ] = False,

    list_labs: Annotated[bool,
        typer.Option('--list-labs',
            callback=list_labs_callback,
            is_eager=True,
            help='List all available lab names.'
        ),
    ] = False,

    show_lab: Annotated[bool,
        typer.Option('--show-lab',
            help='Show specified lab details.'
        ),
    ] = False,

    list_hosts: Annotated[bool,
        typer.Option('--list-hosts',
            help='Show all valid host IDs.'
        ),
    ] = False,

    dry_run: Annotated[bool,
        typer.Option('--dry-run', '-n',
            help='Preview what would be executed without running commands on hosts.',
        )
    ] = False,

    version: Annotated[ Optional[bool],
        typer.Option("--version",
                    callback=version_callback,
                    is_eager=True,
                    help='Show program version and exit.',
                    ),
    ] = None,

    clear_autocomplete_cache: Annotated[bool,
        typer.Option('--clear-autocomplete-cache',
            callback=clear_autocomplete_cache_callback,
            is_eager=True,
            help='Delete the shell-completion cache file and exit.',
        ),
    ] = False,

    as_user: Annotated[Optional[str],
        typer.Option('--as-user',
            metavar='USERNAME',
            help=(
                "Check reservations as USERNAME instead of the current user. "
                "Use when a teammate has the shared lab booked under their name."
            ),
        ),
    ] = None,

    skip_reservation_check: Annotated[bool,
        typer.Option('--skip-reservation-check', '-R',
            help=(
                "Bypass the reservation check entirely. Intended only for "
                "emergencies when the scheduler is wrong or unreachable."
            ),
        ),
    ] = False,
):
    if ctx.resilient_parsing:
        return

    from rich import print as rprint
    from rich.align import Align

    from ..host import HostFilter
    from .banner import banner
    from .callbacks import list_hosts_callback

    rprint(Align.center(banner))

    logger = initOttoLogger(xdir=xdir,
                            log_level=log_level,
                            keep_days=log_days,
                            verbose=verbose,
                            rich_log_file=rich_log_file,
                            )
    for handler in logger.handlers:
        handler.addFilter(HostFilter())

    # Set up config module
    repos = getRepos()

    # Extract lab search paths from all repos
    lab_search_paths: list[Path] = []
    for repo in repos:
        lab_search_paths.extend(repo.labs)

    # Pass search paths to getLab
    lab = getLab(labs, search_paths=lab_search_paths)

    # Build the reservation backend (first repo with a [reservations] section wins;
    # empty settings across all repos yields a null backend — effectively disabled).
    from ..reservations import (
        ReservationBackendError,
        build_backend,
        resolve_username,
    )

    reservation_settings: dict[str, Any] = {}
    reservation_repo_dir: Path = repos[0].sutDir if repos else Path.cwd()
    for repo in repos:
        if repo.reservationSettings:
            reservation_settings = repo.reservationSettings
            reservation_repo_dir = repo.sutDir
            break

    try:
        reservation_backend = build_backend(reservation_settings, reservation_repo_dir)
    except ReservationBackendError as e:
        rprint(
            f"[bold red]Reservation backend unavailable:[/bold red] {e}\n"
            f"Pass [bold]--skip-reservation-check[/bold] / [bold]-R[/bold] to proceed without the check."
        )
        raise typer.Exit(1) from e

    identity = resolve_username(as_user)

    if identity.source == "--as-user":
        rprint(
            f"[bold magenta][reservations] acting as {identity.username!r} "
            f"(--as-user)[/bold magenta]"
        )

    # Enough is known to create the config module now
    setConfigModule(
        lab=lab,
        repos=repos,
        reservation_backend=reservation_backend,
        identity=identity,
        skip_reservation_check=skip_reservation_check,
    )
    configModule = getConfigModule()

    if show_lab:
        from rich.pretty import pprint

        pprintDepth = None
        if not verbose:
            pprintDepth = 3

        pprint(configModule, max_depth=pprintDepth, expand_all=True)
        raise typer.Exit

    # Listing hosts can't be done as a callback because configmodule creation must be done first.
    # It's simpler and cleaner to just call the callback here after configmodule creation.
    if list_hosts:
        list_hosts_callback(True)
        raise typer.Exit()

    if dry_run:
        from ..host import setDryRun

        logger.info("[magenta][DRY RUN] Commands and file transfers will be skipped. "
                    "Connections will still be verified.")
        setDryRun(True)

    configModule.logRepoCommits()


# ---------------------------------------------------------------------------
# Dispatch-aware subcommand registration
#
# Importing every subcommand module at startup pulls in fastapi, jinja2, etc.
# via cli.monitor and cli.cov — ~100 ms of wall time on the tab-completion
# critical path. Instead, import only the subcommand module actually being
# invoked; for the others, register an empty placeholder Typer so the
# top-level help and shell completion still see the command name.
#
# When no subcommand is apparent (e.g. `otto --help`, `otto --list-labs`, or
# plain `otto`), load every real module so help output is unchanged.
# ---------------------------------------------------------------------------

_SUBCOMMAND_MODULES: dict[str, tuple[str, str]] = {
    'run':         ('.run',         'run_app'),
    'test':        ('.test',        'suite_app'),
    'monitor':     ('.monitor',     'monitor_app'),
    'cov':         ('.cov',         'cov_app'),
    'host':        ('.host',        'host_app'),
    'reservation': ('.reservation', 'reservation_app'),
}


def _requested_subcommands() -> set[str]:
    """Determine which subcommands to import for this invocation.

    Inspects ``sys.argv`` and (in completion mode) ``COMP_WORDS`` for tokens
    that name a known subcommand.
    """
    completion_mode = bool(os.environ.get('_OTTO_COMPLETE'))

    tokens: set[str] = set(sys.argv[1:])
    if completion_mode:
        tokens.update(os.environ.get('COMP_WORDS', '').split())

    matched = set(_SUBCOMMAND_MODULES) & tokens
    if matched:
        return matched

    # Shell completion with no matched subcommand (e.g. `otto <TAB>`) only
    # needs the top-level command list — placeholder Typers are sufficient.
    if completion_mode:
        return set()

    # Non-completion with no subcommand token — `otto` alone, `otto --help`,
    # `otto --list-labs`, etc. Load everything so help output is complete.
    return set(_SUBCOMMAND_MODULES)


def _placeholder_subapp(name: str) -> typer.Typer:
    """Empty Typer with just a name/help — used for subcommands that aren't
    being completed in this invocation so the top-level help still lists them."""
    return typer.Typer(
        name=name,
        help=f'(run `otto {name} -h` for details)',
    )


def _attach_cached_stubs(
    parent: typer.Typer,
    commands: list[dict],
) -> None:
    """Rebuild per-suite / per-instruction stubs under ``parent`` from the cache.

    Imports are local so the cache module isn't pulled in during tests or
    non-completion invocations that don't exercise this path.
    """
    from ..configmodule.completion_stubs import build_stub_command

    for entry in commands:
        name = entry.get('name')
        if not name:
            continue
        options = entry.get('options') or []
        parent.add_typer(build_stub_command(name, options))


def _register_subcommands() -> None:
    """Attach subcommand Typers to ``app`` for this invocation.

    Two paths:
    - *Fast path* (completion cache hit): import only the subcommand module
      the user is actively completing so its real callback-level options
      (``--cov``, ``--list-suites``, …) reach the completer. For ``test`` /
      ``run`` we additionally attach stub children for each cached suite /
      instruction so ``otto test TestDevice --<TAB>`` has a signature to
      introspect. Non-targeted subcommands get an empty placeholder.
    - *Slow path* (cache miss or non-completion): same dispatch — import the
      wanted subcommand modules and use placeholders for the rest. When no
      subcommand is apparent (e.g. ``otto --help``) ``_requested_subcommands``
      returns the full set so help output stays complete.
    """
    cached = getCompletionNames()
    wanted = _requested_subcommands()

    for name, (modpath, attr) in _SUBCOMMAND_MODULES.items():
        if name in wanted:
            mod = importlib.import_module(modpath, package=__package__)
            sub_app = getattr(mod, attr)
            if cached is not None and name == 'test':
                _attach_cached_stubs(sub_app, cached.get('suites', []))
            elif cached is not None and name == 'run':
                _attach_cached_stubs(sub_app, cached.get('instructions', []))
            app.add_typer(sub_app)
        else:
            app.add_typer(_placeholder_subapp(name))


_register_subcommands()
