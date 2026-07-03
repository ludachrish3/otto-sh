"""Top-level ``otto`` CLI: callback, subcommand dispatch, and eager option handlers."""

import dataclasses
import importlib
import os
from pathlib import Path
from typing import (
    TYPE_CHECKING,
    Annotated,
    Any,
    # override,     only available in Python >= 3.12
)

import typer
from typer.core import TyperGroup
from typing_extensions import override

from ..configmodule import (
    get_completion_names,
    get_repos,
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
)
from ..utils import (
    split_on_commas,
)
from ..version import get_version
from .builtin_commands import register_builtin_commands

if TYPE_CHECKING:
    from .registry import CommandSpec

__version__ = get_version()

# TODO: Should rich help menus be optional?
# Uncomment the line below to remove rich help menu formatting globally
# typer.core.HAS_RICH = False  # noqa: ERA001 — intentional documented escape-hatch example

_field_default = os.environ.get(FIELD_DEFAULT_ENV_VAR) is not None
"""Determines the default for debug or field. If OTTO_FIELD_DEFAULT is set to
anything at all, then field is the default. A bare env-presence check —
deliberately NOT ``get_env()``, which runs repo discovery: importing the CLI
must never parse repo settings, or a malformed ``settings.toml`` would brick
``otto --help`` before argv is even seen."""

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


def _lab_completer(ctx: "typer.Context", incomplete: str) -> list[str]:  # noqa: ARG001 — required by Typer autocompletion callback signature
    """Completion source for ``--lab``: lab names referenced by the hosts.json files.

    Prefers the completion-cache snapshot; falls back to a live, data-only scan
    (:func:`~otto.configmodule.completion_cache.collect_lab_names`, no user
    code). ``--lab`` is comma-separated, so only the in-progress segment is
    completed and already-named labs are dropped.
    """
    from ..configmodule import get_completion_names, get_repos
    from ..configmodule.completion_cache import collect_lab_names
    from ..utils import complete_comma_list

    cached = get_completion_names()
    if cached is not None and isinstance(cached.get("labs"), list):
        names = cached["labs"]
    else:
        names = collect_lab_names(get_repos())
    return complete_comma_list(sorted(names), incomplete)


class _OttoGroup(TyperGroup):
    """Root group: registry-backed lazy dispatch + pending-token snapshot.

    ``list_commands`` names every registered :class:`CommandSpec`, plus any
    third-party command name captured in the completion cache but not (yet)
    in the live registry — e.g. on the completion fast path, where bootstrap
    is skipped so plugin init modules never ran. ``get_command`` resolves the
    real command (importing its module) only for the token actually being
    dispatched or completed — every other registry name gets a lightweight
    stub whose help comes from the spec, so ``otto --help`` imports zero
    subcommand modules; a cache-only name gets an equivalent stub built from
    the cached name/help. The registry always takes priority: a cached name
    that's also registered resolves through the registry branch, so a stale
    cache entry can never shadow real dispatch.
    """

    _stub_cache: dict[str, Any]
    _real_cache: dict[str, Any]

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

    def _dispatch_target(self, ctx: Any) -> str | None:
        """Return the subcommand name pending dispatch, if any."""
        pending = ctx.meta.get("_pending_subcmd_args") or []
        return pending[0] if pending else None

    def _wants_real(self, ctx: Any, cmd_name: str) -> bool:
        """Return whether *cmd_name* is the invocation's actual dispatch/completion target.

        The pending-token snapshot covers completion descent too: click's
        completion resolver builds the root context through ``make_context``
        → our ``parse_args`` override, so `otto run <TAB>` sees ``run`` as
        the dispatch target. (A COMP_WORDS membership check used to sit here
        as belt-and-braces; it also matched command names typed as option
        VALUES, importing unrelated modules during enumeration.)
        """
        return cmd_name == self._dispatch_target(ctx)

    def _stub(self, spec: "CommandSpec") -> Any:
        """Return (building + caching once) a lightweight help-only stub for *spec*."""
        cache = getattr(self, "_stub_cache", None) or {}
        self._stub_cache = cache
        if spec.name not in cache:
            tmp = typer.Typer(
                name=spec.name, help=spec.help or f"(run `otto {spec.name} -h` for details)"
            )
            # get_group (not get_command): an empty stub Typer has zero
            # registered commands, which get_command rejects outright.
            stub: Any = typer.main.get_group(tmp)
            stub.name = spec.name
            cache[spec.name] = stub
        return cache[spec.name]

    def _real(self, spec: "CommandSpec") -> Any:
        """Return (importing + caching once) the real resolved command for *spec*."""
        cache = getattr(self, "_real_cache", None) or {}
        self._real_cache = cache
        if spec.name not in cache:
            from .registry import resolve_spec_command

            loader = spec.loader
            cached_names = get_completion_names()
            if cached_names is not None and isinstance(loader, str):
                # Completion fast path: attach cached suite/instruction stubs
                # to the freshly imported sub-app before conversion.
                mod_name, _, attr = loader.partition(":")
                sub_app = getattr(importlib.import_module(mod_name), attr)
                if spec.name == "test":
                    _attach_cached_stubs(sub_app, cached_names.get("suites", []))
                elif spec.name == "run":
                    _attach_cached_stubs(sub_app, cached_names.get("instructions", []))
                spec = dataclasses.replace(spec, loader=sub_app)
            from .invoke import wrap_leaf_callbacks

            cache[spec.name] = wrap_leaf_callbacks(resolve_spec_command(spec), spec)
        return cache[spec.name]

    @override
    def list_commands(self, ctx: Any) -> list[str]:
        from .registry import CLI_COMMANDS

        static = [n for n in super().list_commands(ctx) if n not in CLI_COMMANDS]
        cached = [
            name
            for c in (get_completion_names() or {}).get("commands", [])
            if (name := c.get("name")) and name not in CLI_COMMANDS
        ]
        return static + CLI_COMMANDS.names() + cached

    @override
    def get_command(self, ctx: Any, cmd_name: str) -> Any:
        from .registry import CLI_COMMANDS

        static = super().get_command(ctx, cmd_name)
        if static is not None:
            return static
        if cmd_name in CLI_COMMANDS:
            spec = CLI_COMMANDS.get(cmd_name)
            if self._wants_real(ctx, cmd_name):
                return self._real(spec)
            return self._stub(spec)
        return self._cached_stub(cmd_name)

    def _cached_stub(self, cmd_name: str) -> Any:
        """Return a stub for *cmd_name* sourced from the completion cache.

        Fast-path-only fallback for third-party commands: the registry never
        holds them here (bootstrap didn't run), but the cache snapshot from a
        prior slow-path run does. An entry with serialized child metadata
        (``commands``) rebuilds a nested group of stubs so the group's
        subcommands tab-complete; a leaf entry with cached ``options``
        rebuilds them for ``--<TAB>``. Dispatch never reaches this branch — a
        dispatch target either resolves via ``CLI_COMMANDS`` (bootstrap ran
        first, per :func:`entry`) or is an unknown command Typer rejects. The
        synthesized spec's ``lab_free`` is forward-looking metadata only; stubs
        are never dispatched and dispatch resolves through CLI_COMMANDS on the
        slow path.
        """
        from .registry import CommandSpec

        cached = {
            name: c
            for c in (get_completion_names() or {}).get("commands", [])
            if (name := c.get("name"))
        }
        entry = cached.get(cmd_name)
        if entry is None:
            return None
        children = entry.get("commands") or []
        options = entry.get("options") or []
        if children or options:
            cache = getattr(self, "_stub_cache", None) or {}
            self._stub_cache = cache
            if cmd_name not in cache:
                from ..configmodule.completion_stubs import build_stub_command, build_stub_group

                if children:
                    tmp = build_stub_group(cmd_name, entry.get("help"), children)
                    rich_stub: Any = typer.main.get_group(tmp)
                else:
                    # get_command flattens the single-command stub app to the
                    # bare leaf, matching how the real command would resolve.
                    tmp = build_stub_command(cmd_name, options, help=entry.get("help"))
                    rich_stub = typer.main.get_command(tmp)
                rich_stub.name = cmd_name
                cache[cmd_name] = rich_stub
            return cache[cmd_name]
        spec = CommandSpec(
            name=cmd_name,
            loader=None,
            help=entry.get("help"),
            lab_free=bool(entry.get("lab_free")),
        )
        return self._stub(spec)


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
            autocompletion=_lab_completer,
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
    """Record root options for lazy lab loading; handle inline root-flag actions.

    This is the Typer root callback executed before every ``otto`` subcommand.
    It no longer loads the lab or initialises logging: it stashes the root
    options on ``ctx.meta`` and returns. The real work (lab load, session
    setup, output dir, reservation gate) runs lazily in the leaf-invoke
    :func:`~otto.cli.invoke.command_preamble`, so ``--help`` / discovery paths
    are structurally incapable of touching host state. The only exceptions are
    ``--show-lab`` / ``--list-hosts``, which inspect live lab state and so load
    it inline here before printing and exiting.
    """
    if ctx.resilient_parsing:
        return

    from .invoke import (
        LabContextError,
        RootOptions,
        ensure_lab_context,
        fail_loud_on_bootstrap_errors,
        report_lab_context_error,
    )

    ctx.meta["_otto_root_options"] = RootOptions(
        labs=labs,
        xdir=xdir,
        log_days=log_days,
        log_level=log_level,
        rich_log_file=rich_log_file,
        show_time=show_time,
        dry_run=dry_run,
        as_user=as_user,
        skip_reservation_check=skip_reservation_check,
    )

    if show_lab or list_hosts:
        # These root flags inspect live lab state, which depends on the
        # registered world — fail the same way dispatch does rather than
        # surfacing a confusing secondary error from a half-registered world.
        fail_loud_on_bootstrap_errors()
        # Load the lab now, print, exit.
        try:
            ensure_lab_context(ctx)
        except LabContextError as e:
            report_lab_context_error(e)
        if show_lab:
            from rich.pretty import pprint

            from ..context import get_context

            pprint(
                get_context().lab,
                max_depth=(None if lab_depth == 0 else lab_depth),
                expand_all=True,
            )
        else:
            from .callbacks import list_hosts_callback

            list_hosts_callback(True)
        raise typer.Exit


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


register_builtin_commands()


def entry() -> None:
    """Console-script entry: composition root, then the Typer app.

    Completion invocations take the cache fast path (zero user code); everything
    else runs :func:`otto.bootstrap.bootstrap` before argv parsing so registered
    third-party commands exist when the root group is consulted. Contained
    user-code failures print one framed warning line each; real command
    dispatch fails loud in the invoke preamble.
    """
    import contextlib

    from .. import bootstrap as bs
    from ..configmodule.completion_cache import (
        DUMP_TESTS_ENV_VAR,
        dump_collected_test_names,
        is_completion_mode,
        read_cache,
    )

    if os.environ.get(DUMP_TESTS_ENV_VAR):
        # One-shot "collect and print test names" subprocess, spawned by the
        # --tests completer to warm its collected cache (collection never runs
        # inside the completer itself). Any failure exits non-zero with no
        # payload, so the parent treats it as a miss and keeps the static floor.
        code = 1
        with contextlib.suppress(Exception):
            _env, repos = bs.discover()
            dump_collected_test_names(repos)
            code = 0
        raise SystemExit(code)

    if is_completion_mode():
        # Completion must never traceback into the shell: any discovery
        # failure just leaves the cache unset and falls through to the
        # slow path below.
        with contextlib.suppress(Exception):
            _env, repos = bs.discover()
            bs.set_completion_names(read_cache(repos))

    if bs.get_completion_names() is None:
        try:
            result = bs.bootstrap()
        except (FileNotFoundError, ValueError) as e:
            # Env-level discovery failure (bad OTTO_SUT_DIRS / OTTO_* values;
            # pydantic validation errors are ValueErrors): nothing user-specific
            # can load, so there is no degraded help worth rendering — fail
            # loud but CLEAN (one line, no traceback). Per-repo config-data
            # errors never reach here; discover() contains those.
            typer.echo(f"error: {e}", err=True)
            raise SystemExit(1) from e
        for err in result.errors:
            typer.echo(f"warning: {err}", err=True)
        from ..configmodule.completion_cache import (
            collect_backend_names,
            collect_cli_commands,
            collect_current_commands,
            collect_docker_capable_host_ids,
            collect_host_ids,
            collect_host_ids_by_lab,
            collect_lab_names,
            collect_reservation_usernames,
            collect_test_names,
            write_cache,
        )

        instructions, suites = collect_current_commands()
        backends = collect_backend_names()
        with contextlib.suppress(OSError):
            write_cache(
                result.repos,
                instructions,
                suites,
                collect_host_ids(result.repos),
                collect_docker_capable_host_ids(result.repos),
                term_backends=backends["term_backends"],
                transfer_backends=backends["transfer_backends"],
                usernames=collect_reservation_usernames(result.repos),
                commands=collect_cli_commands(),
                labs=collect_lab_names(result.repos),
                tests=collect_test_names(result.repos),
                hosts_by_lab=collect_host_ids_by_lab(result.repos),
            )

    app()
