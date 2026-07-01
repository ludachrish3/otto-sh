"""Top-level ``otto`` CLI: callback, subcommand dispatch, and eager option handlers."""

import importlib
import os
import sys
from logging import getLogger
from pathlib import Path
from typing import (
    Annotated,
    Any,
    # override,     only available in Python >= 3.12
)

import typer
from typer.core import TyperGroup
from typing_extensions import override

from ..configmodule import (
    get_completion_names,
    get_env,
    get_repos,
    load_lab,
)
from ..configmodule.env import (
    DEFAULT_LOG_RETENTION_DAYS,
    FIELD_PRODUCT_ENV_VAR,
    LAB_ENV_VAR,
    LOG_DAYS_ENV_VAR,
    LOG_LVL_ENV_VAR,
    LOG_RICH_ENV_VAR,
    SUT_DIRS_ENV_VAR,
    XDIR_ENV_VAR,
)
from ..logger import management
from ..utils import (
    split_on_commas,
)
from ..version import get_version

__version__ = get_version()

# TODO: Should rich help menus be optional?
# Uncomment the line below to remove rich help menu formatting globally
# typer.core.HAS_RICH = False  # noqa: ERA001 — intentional documented escape-hatch example

_field_default = get_env().field_default is not None
"""Determines the default for debug or field. If OTTO_FIELD_DEFAULT is set to
anything at all, then field is the default. Read once at import via the startup
env singleton (get_env())."""

DESCRIPTION = f"""
O.T.T.O. (Our Trusty Testing Orchestrator)

If a development repo is under test, then {SUT_DIRS_ENV_VAR} must be set in your environment.
It is a list of paths to repo root directories, separated by ``,`` or the OS path separator
(``:`` on Linux/macOS, ``;`` on Windows).

"""


def version_callback(version: bool) -> None:
    """Print the otto version string and exit when ``--version`` is passed."""
    if version:
        from rich import print as rprint

        rprint(f"otto version: {__version__}")
        raise typer.Exit


def clear_autocomplete_cache_callback(value: bool) -> None:
    """Delete the shell-completion cache file and exit when the flag is set."""
    if not value:
        return
    from rich import print as rprint

    from ..configmodule.completion_cache import _cache_path, clear_cache

    cache_path = _cache_path()
    removed = clear_cache()
    if removed:
        rprint(f"Removed completion cache: {cache_path}")
    elif cache_path is None:
        rprint("No completion cache to clear (OTTO_XDIR is not set).")
    else:
        rprint(f"No completion cache found at {cache_path}.")
    raise typer.Exit


def list_labs_callback(value: bool) -> None:
    """Print all available lab names (one panel per repo) and exit when the flag is set."""
    if value:
        from rich import print as rprint
        from rich.panel import Panel
        from rich.table import Table

        # Extract lab search paths from all repos
        panels: list[Panel] = [repo.get_lab_panel() for repo in get_repos()]

        table = Table(
            show_header=False, show_footer=False, box=None, expand=True, padding=(0, 1, 1, 1)
        )
        for _ in panels:
            table.add_column(ratio=1)
        table.add_row(*panels)
        rprint(table)

        raise typer.Exit


def log_level_callback(value: str) -> str:
    """Normalise the ``--log-level`` value to upper-case before Typer stores it."""
    return value.upper()


def _username_completer(ctx: "typer.Context", incomplete: str) -> list[str]:  # noqa: ARG001 — required by Typer autocompletion callback signature
    """Completion source for ``--as-user``: usernames the reservation backend knows.

    Prefers the completion-cache snapshot (slow-path populated, so no backend is
    built in the completion fast path); falls back to a live best-effort
    collection on a cache miss. Empty when the backend can't enumerate users.
    """
    from ..configmodule import get_completion_names, get_repos
    from ..configmodule.completion_cache import collect_reservation_usernames

    cached = get_completion_names()
    if cached is not None and isinstance(cached.get("usernames"), list):
        names = cached["usernames"]
    else:
        names = collect_reservation_usernames(get_repos())
    return sorted(n for n in names if n.startswith(incomplete))


def _is_lab_free_flag_invocation(ctx: typer.Context) -> bool:
    """Return True when the pending subcommand tokens contain a lab-free flag.

    Checks the snapshot saved by _OttoGroup.parse_args first (works under
    CliRunner and the real binary), then falls back to sys.argv (belt-and-
    suspenders for any invocation path that bypasses _OttoGroup.parse_args).
    The sys.argv check is scoped to tokens following a known subcommand name
    to avoid false-positives when sys.argv is e.g. a pytest command line.
    """
    subcmd_args: set[str] = set(ctx.meta.get("_pending_subcmd_args", ()))
    if not subcmd_args & _LAB_FREE_FLAGS:
        argv = sys.argv[1:]
        for i, tok in enumerate(argv):
            if tok in _SUBCOMMAND_MODULES:
                subcmd_args = set(argv[i:])
                break
    return bool(subcmd_args & _LAB_FREE_FLAGS)


class _OttoGroup(TyperGroup):
    """Root click group that snapshots pending subcommand tokens into ctx.meta.

    Typer's TyperGroup.invoke clears ``ctx._protected_args`` / ``ctx.args``
    before calling the group callback, so those attributes are always empty by
    the time ``main()`` runs.  This subclass saves a copy into ``ctx.meta``
    during ``parse_args`` (before they are cleared) so that the callback can
    detect help / discovery flags without touching ``sys.argv``.
    """

    @override
    def parse_args(self, ctx: Any, args: list[str]) -> list[str]:
        # ctx: Any mirrors HostGroup.list_commands — Typer's vendored click fork
        # makes typer.Context (typer.models.Context) incompatible with the
        # parent's _click.Context under strict typing.
        result = super().parse_args(ctx, args)
        # Save the pending subcommand tokens (subcommand name + its args) so
        # the main() callback can inspect them even after invoke() clears them.
        ctx.meta["_pending_subcmd_args"] = list(
            getattr(ctx, "_protected_args", []) + getattr(ctx, "args", [])
        )
        return result


app = typer.Typer(
    no_args_is_help=True,
    help=DESCRIPTION,
    invoke_without_command=True,
    pretty_exceptions_show_locals=True,
    cls=_OttoGroup,
    context_settings={
        "help_option_names": ["-h", "--help"],
    },
)


@app.callback(
    no_args_is_help=True,
    help=DESCRIPTION,
)
def main(  # noqa: PLR0913 — CLI command params
    ctx: typer.Context,
    labs: Annotated[
        list[str] | None,
        typer.Option(
            "--lab",
            "-l",
            envvar=LAB_ENV_VAR,
            callback=split_on_commas,
            metavar="COMMA SEPARATED LIST",
            help="Name of lab(s) to reserve and use.",
        ),
    ] = None,
    xdir: Annotated[
        Path,
        typer.Option(
            "--xdir",
            "-x",
            envvar=XDIR_ENV_VAR,
            help="Directory in which to store logs and artifacts.",
        ),
    ] = Path(),
    debug: Annotated[  # noqa: ARG001 — required by Typer CLI option signature; consumed by framework before function body
        bool,
        typer.Option(
            "--field/--debug",
            envvar=FIELD_PRODUCT_ENV_VAR,
            help="Use field or debug products.",
        ),
    ] = _field_default,
    log_days: Annotated[
        int,
        typer.Option(
            min=0,
            envvar=LOG_DAYS_ENV_VAR,
            help="Number of days to retain logs.",
        ),
    ] = DEFAULT_LOG_RETENTION_DAYS,
    log_level: Annotated[
        str,
        typer.Option(
            envvar=LOG_LVL_ENV_VAR,
            metavar="LOG LEVEL",
            callback=log_level_callback,
            help="Level at which to log.",
        ),
    ] = "INFO",
    rich_log_file: Annotated[
        bool,
        typer.Option(
            envvar=LOG_RICH_ENV_VAR,
            help="Determines whether log files have rich formatting.",
        ),
    ] = False,
    show_time: Annotated[
        bool,
        typer.Option(
            "--show-time",
            help="Show per-line timestamps on the live console (log files are always timestamped).",
        ),
    ] = False,
    lab_depth: Annotated[
        int,
        typer.Option(
            "--lab-depth",
            min=0,
            help="Depth for --show-lab output (0 = unlimited).",
        ),
    ] = 3,
    list_labs: Annotated[  # noqa: ARG001 — required by Typer eager callback option signature
        bool,
        typer.Option(
            "--list-labs",
            callback=list_labs_callback,
            is_eager=True,
            help="List all available lab names.",
        ),
    ] = False,
    show_lab: Annotated[
        bool,
        typer.Option("--show-lab", help="Show specified lab details."),
    ] = False,
    list_hosts: Annotated[
        bool,
        typer.Option("--list-hosts", help="Show all valid host IDs."),
    ] = False,
    dry_run: Annotated[
        bool,
        typer.Option(
            "--dry-run",
            "-n",
            help="Preview what would be executed without running commands on hosts.",
        ),
    ] = False,
    version: Annotated[  # noqa: ARG001 — required by Typer eager callback option signature
        bool | None,
        typer.Option(
            "--version",
            callback=version_callback,
            is_eager=True,
            help="Show program version and exit.",
        ),
    ] = None,
    clear_autocomplete_cache: Annotated[  # noqa: ARG001 — required by Typer eager callback option signature
        bool,
        typer.Option(
            "--clear-autocomplete-cache",
            callback=clear_autocomplete_cache_callback,
            is_eager=True,
            help="Delete the shell-completion cache file and exit.",
        ),
    ] = False,
    as_user: Annotated[
        str | None,
        typer.Option(
            "--as-user",
            metavar="USERNAME",
            autocompletion=_username_completer,
            help=(
                "Check reservations as USERNAME instead of the current user. "
                "Use when a teammate has the shared lab booked under their name."
            ),
        ),
    ] = None,
    skip_reservation_check: Annotated[
        bool,
        typer.Option(
            "--skip-reservation-check",
            "-R",
            help=(
                "Bypass the reservation check entirely. Intended only for "
                "emergencies when the scheduler is wrong or unreachable."
            ),
        ),
    ] = False,
) -> None:
    """Load the lab, initialise logging, check reservations, and install the runtime context.

    This is the Typer root callback executed before every ``otto`` subcommand.
    Lab-free subcommands (e.g. ``otto schema``) skip the lab/reservation
    bootstrap; all others require ``--lab`` and build an ``OttoContext``.
    """
    if ctx.resilient_parsing:
        return

    # Lab-free utility subcommands (e.g. `otto schema`) need none of the
    # lab / reservation / context bootstrap below — and forcing `--lab` on them
    # would be nonsensical. Skip the whole callback body for them; the
    # subcommand runs on its own.
    if ctx.invoked_subcommand in _LAB_FREE_SUBCOMMANDS:
        return

    # Help requests and discovery flags (--list-suites, --list-tests, etc.)
    # touch no host state — skip the --lab requirement for them too.
    if _is_lab_free_flag_invocation(ctx):
        return

    # `--lab` is no longer a hard-required Typer option (so lab-free subcommands
    # can run without it); enforce it here — before any banner/logger side
    # effects — for everything that does need a lab.
    if not labs:
        # Use Typer's own echo/Exit rather than ``click.UsageError``: Typer >= 0.26
        # vendors its own click fork, so a *real* ``click.UsageError`` is not caught
        # by Typer's error handler and would escape uncaught (exit 1, no message).
        typer.echo("Error: Missing option '--lab' / '-l' (env var: 'OTTO_LAB').", err=True)
        raise typer.Exit(code=2)

    from rich import print as rprint
    from rich.align import Align

    from ..host import HostFilter
    from .banner import banner
    from .callbacks import list_hosts_callback

    rprint(Align.center(banner))

    management.init_cli_logging(
        xdir=xdir,
        log_level=log_level,
        keep_days=log_days,
        show_time=show_time,
        rich_log_file=rich_log_file,
    )
    logger = getLogger("otto")
    management.attach_console_suppress_filter(HostFilter())

    # Set up config module
    repos = get_repos()

    # Stash the product / external logger prefixes (init roots, libs sub-packages,
    # explicit [logging] capture) so the per-subcommand create_output_dir attaches
    # the shared QueueHandler to them once it exists. Done here (after
    # init_cli_logging set the log level) so capture honours the verbose floor.
    prefixes: set[str] = set()
    for repo in repos:
        prefixes |= repo.product_log_prefixes()
    management.set_capture_prefixes(prefixes)

    # Extract + aggregate lab search paths across all repos (for the default
    # json backend).
    lab_search_paths: list[Path] = []
    for repo in repos:
        lab_search_paths.extend(repo.labs)

    # Reduce repos' [host_preferences] tables in OTTO_SUT_DIRS order; later repos
    # overlay earlier ones. Selections (list) are atomic — last repo to set a
    # (selector, capability) wins it; option tables (dict) merge per key.
    merged_host_preferences: dict[str, dict[str, Any]] = {}
    for repo in repos:
        for selector, entries in repo.host_preferences.items():
            dest = merged_host_preferences.setdefault(selector, {})
            for key, val in entries.items():
                if isinstance(val, list):
                    dest[key] = list(val)
                else:
                    dest.setdefault(key, {}).update(val)

    # Select the host-source backend: the first repo that declares a [lab] block
    # wins (mirrors reservations' "first repo declares" rule). With no [lab]
    # block anywhere, lab_settings stays {} and the factory falls back to the
    # built-in json backend over the aggregated search paths.
    lab_settings: dict[str, Any] = {}
    lab_repo_dir: Path = repos[0].sut_dir if repos else Path.cwd()
    for repo in repos:
        if repo.lab_settings:
            lab_settings = repo.lab_settings
            lab_repo_dir = repo.sut_dir
            break

    from ..storage import LabRepositoryError, build_lab_repository

    try:
        lab_repository = build_lab_repository(
            lab_settings, lab_repo_dir, search_paths=lab_search_paths
        )
    except (ValueError, LabRepositoryError) as e:
        rprint(f"[bold red]Host source unavailable:[/bold red] {e}")
        raise typer.Exit(1) from e

    lab = load_lab(labs, preferences=merged_host_preferences, repository=lab_repository)

    # Synthesize placeholder Docker container hosts from each repo's
    # `[docker]` settings. They appear in `--list-hosts` and tab-completion
    # immediately; operations against them surface a clear "run otto docker
    # up" error until `compose_up` overwrites the placeholder with a real
    # entry.
    from ..docker.compose import register_declared_container_hosts

    register_declared_container_hosts(lab, repos)

    # Resolve reservation identity + backend (first repo with a [reservations]
    # section wins). With -R the backend is NOT constructed at all, so a broken
    # or hanging scheduler can never block lab access (break-glass).
    from ..reservations import (
        ReservationBackendError,
        build_reservation_state,
    )

    try:
        reservation_state = build_reservation_state(
            repos,
            as_user=as_user,
            skip_reservation_check=skip_reservation_check,
            cwd_fallback=Path.cwd(),
        )
    except ReservationBackendError as e:
        rprint(
            f"[bold red]Reservation backend unavailable:[/bold red] {e}\n"
            f"Pass [bold]--skip-reservation-check[/bold] / [bold]-R[/bold] to proceed without the check."  # noqa: E501 — long rich markup string
        )
        raise typer.Exit(1) from e

    identity = reservation_state.identity
    if identity is not None and identity.source == "--as-user":
        logger.info(
            f"[bold magenta][reservations] acting as {identity.username!r}"
            f" (--as-user)[/bold magenta]"
        )

    ctx.meta["otto_reservation"] = reservation_state

    # Install the runtime context: lab + dry_run flag.
    from ..context import OttoContext, set_context

    set_context(OttoContext(lab=lab, dry_run=dry_run))

    if show_lab:
        from rich.pretty import pprint

        pprint(lab, max_depth=(None if lab_depth == 0 else lab_depth), expand_all=True)
        raise typer.Exit

    # Listing hosts can't be done as a callback because context creation must be done first.
    # It's simpler and cleaner to just call the callback here after context creation.
    if list_hosts:
        list_hosts_callback(True)
        raise typer.Exit

    if dry_run:
        logger.info(
            "[magenta][DRY RUN] Commands and file transfers will be skipped. "
            "Connections will still be verified."
        )

    for repo in repos:
        logger.debug(f"{repo.sut_dir}: {repo.commit}")


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
    "run": (".run", "run_app"),
    "test": (".test", "suite_app"),
    "monitor": (".monitor", "monitor_app"),
    "cov": (".cov", "cov_app"),
    "host": (".host", "host_app"),
    "docker": (".docker", "docker_app"),
    "reservation": (".reservation", "reservation_app"),
    "schema": (".schema", "schema_app"),
}

# Subcommands that introspect otto itself rather than operate on a lab. The
# top-level callback skips its lab / reservation bootstrap (and the `--lab`
# requirement) for these.
_LAB_FREE_SUBCOMMANDS: frozenset[str] = frozenset({"schema"})

# Flags that request help or discovery information about registered suites /
# instructions. When any of these appear in the pending subcommand tokens the
# invocation touches no host state, so the `--lab` requirement is skipped.
# NOTE: `--list-hosts` is intentionally excluded — it queries live lab state.
_LAB_FREE_FLAGS: frozenset[str] = frozenset(
    {"--help", "-h", "--list-suites", "--list-tests", "--list-markers", "--list-instructions"}
)


def _requested_subcommands() -> set[str]:
    """Determine which subcommands to import for this invocation.

    Inspects ``sys.argv`` and (in completion mode) ``COMP_WORDS`` for tokens
    that name a known subcommand.
    """
    completion_mode = bool(os.environ.get("_OTTO_COMPLETE"))

    tokens: set[str] = set(sys.argv[1:])
    if completion_mode:
        tokens.update(os.environ.get("COMP_WORDS", "").split())

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
    """Empty Typer with just a name/help.

    Used for subcommands that aren't being completed in this invocation so the top-level
    help still lists them.
    """
    return typer.Typer(
        name=name,
        help=f"(run `otto {name} -h` for details)",
    )


def _attach_cached_stubs(
    parent: typer.Typer,
    commands: list[dict[str, Any]],
) -> None:
    """Rebuild per-suite / per-instruction stubs under ``parent`` from the cache.

    Imports are local so the cache module isn't pulled in during tests or
    non-completion invocations that don't exercise this path.
    """
    from ..configmodule.completion_stubs import build_stub_command

    for entry in commands:
        name = entry.get("name")
        if not name:
            continue
        options = entry.get("options") or []
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
    cached = get_completion_names()
    wanted = _requested_subcommands()

    for name, (modpath, attr) in _SUBCOMMAND_MODULES.items():
        if name in wanted:
            mod = importlib.import_module(modpath, package=__package__)
            sub_app = getattr(mod, attr)
            if cached is not None and name == "test":
                _attach_cached_stubs(sub_app, cached.get("suites", []))
            elif cached is not None and name == "run":
                _attach_cached_stubs(sub_app, cached.get("instructions", []))
            app.add_typer(sub_app)
        else:
            app.add_typer(_placeholder_subapp(name))


_register_subcommands()
