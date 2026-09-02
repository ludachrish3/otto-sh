"""Top-level ``otto`` CLI: callback, subcommand dispatch, and eager option handlers."""

import dataclasses
import importlib
import os
import sys
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

from ..config import (
    get_completion_names,
    get_repos,
)
from ..config.env import (
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
from ..version import get_version
from .builtin_commands import register_builtin_commands

if TYPE_CHECKING:
    from ..bootstrap import BootstrapResult
    from ..config.repo import Repo
    from .registry import CommandSpec

__version__ = get_version()

# TODO: Should rich help menus be optional?
# Uncomment the line below to remove rich help menu formatting globally
# typer.core.HAS_RICH = False  # noqa: ERA001 — intentional documented escape-hatch example

_root_log_level: str | None = None
"""The ``--log-level`` value the root callback resolved, or None before it ran.

Read by :func:`entry`'s boundary frame to decide whether a demoted traceback
is wanted. Set from the callback because by the time the frame runs, the Typer
context that carried ``RootOptions`` is gone.

Deliberately the value the operator TYPED, not a level read back off a logger.
The callback does now put otto's verbose floor on root (spec 2026-08-30 §3.1),
so root's level is readable here — but it is process state anything can move
(``otto.logger.install`` in an embedding process, a suite, pytest's
``log_cli``), and "did the operator ask for debug?" is a question about this
invocation rather than about the handler stack it happens to have.
"""

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
    r"""Print the otto version string and exit when ``--version`` is passed.

    Builtin ``print``, deliberately, NOT rich's. Two reasons, both observable:

    - ``otto._shim`` answers a bare ``otto --version`` without importing the
      CLI at all (importing rich there would pay back the ~2400 syscalls the
      shim exists to skip), so it must print plainly. If this callback used
      rich the SAME BINARY would disagree with itself on a TTY: ``otto
      --version`` plain, ``otto --version extra`` — which Typer's eager
      callback answers here — highlighted.
    - rich's ``ReprHighlighter`` colourises the numbers in a version string on
      a TTY (``otto version: \x1b[1;36m0.9\x1b[0m.\x1b[1;36m0\x1b[0m``) and
      would read ``[...]`` in a local/dev version as console markup. Neither
      is wanted for a machine-readable one-liner.

    A pipe hides all of this — rich auto-disables colour when stdout is not a
    tty — so ``tests/unit/test_shim.py`` pins it under a pty.
    """
    if version:
        print(f"otto version: {__version__}")  # noqa: T201 — see docstring
        raise typer.Exit


def clear_autocomplete_cache_callback(value: bool) -> None:
    """Delete the shell-completion cache files and exit when the flag is set.

    Two files live in that directory: the fingerprinted ``completion_cache.json``
    and the remote-path sidecar. One flag clears both — a user reaching for the
    escape hatch wants completion state gone, not one half of it.
    """
    if not value:
        return
    from rich import print as rprint

    from ..config.completion_cache import _cache_path, clear_cache
    from ..config.remote_completion_cache import REMOTE_CACHE_FILENAME, clear_remote_cache

    cache_path = _cache_path()
    # Both calls run before anything is reported: `or` would short-circuit and
    # leave the second cache on disk.
    removed = [
        path
        for path, gone in (
            (cache_path, clear_cache()),
            (
                cache_path.with_name(REMOTE_CACHE_FILENAME) if cache_path else None,
                clear_remote_cache(),
            ),
        )
        if gone
    ]
    if removed:
        for path in removed:
            rprint(f"Removed completion cache: {path}")
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
    from ..config import get_completion_names, get_repos
    from ..config.completion_cache import collect_reservation_usernames

    cached = get_completion_names()
    if cached is not None and isinstance(cached.get("usernames"), list):
        names = cached["usernames"]
    else:
        names = collect_reservation_usernames(get_repos())
    return sorted(n for n in names if n.startswith(incomplete))


def _lab_completer(ctx: "typer.Context", incomplete: str) -> list[str]:  # noqa: ARG001 — required by Typer autocompletion callback signature
    """Completion source for ``--lab``: lab names referenced by the lab.json files.

    Prefers the completion-cache snapshot; falls back to a live, data-only scan
    (:func:`~otto.config.completion_cache.collect_lab_names`, no user
    code). ``--lab`` combines labs with ``+``, so only the in-progress segment
    is completed and already-named labs are dropped.
    """
    from ..config import get_completion_names, get_repos
    from ..config.completion_cache import collect_lab_names
    from ..config.lab import LAB_SEPARATOR
    from ..utils import complete_separated_list

    cached = get_completion_names()
    if cached is not None and isinstance(cached.get("labs"), list):
        names = cached["labs"]
    else:
        names = collect_lab_names(get_repos())
    return complete_separated_list(sorted(names), incomplete, sep=LAB_SEPARATOR)


def parse_lab_selection(value: list[str] | None) -> list[str] | None:
    """Typer callback for ``--lab``: split each value on ``+``.

    Typer hands this ``None`` when neither ``--lab`` nor ``OTTO_LAB`` is set — and
    also when ``OTTO_LAB`` is set but empty (verified empirically). Both pass
    straight through as ``None``, so the preamble's no-lab path is unchanged.
    Repeats accumulate and each value is itself split, so ``--lab a+b --lab c``
    selects ``a``, ``b``, and ``c``. Malformed input is a usage error (exit 2).

    The grammar itself lives in :func:`otto.config.lab.split_lab_names` — this is
    only the CLI adapter, translating its ``ValueError`` into a Typer usage error.
    The import is function-local to match this module's existing convention for
    narrowly-used helpers (see ``_lab_completer`` above and ``reservation.py``).
    """
    if not value:
        return None

    from ..config.lab import split_lab_names

    names: list[str] = []
    try:
        for item in value:
            names += split_lab_names(item)
    except ValueError as e:
        raise typer.BadParameter(str(e)) from e
    return names


def parse_project_list(value: "list[str] | None") -> "list[str] | None":
    """Typer callback for ``-I``/``-E``: split each value on commas, strip, normalize.

    The switch repeats and each occurrence also splits on a comma, so
    ``-I a -I b`` and ``-I a,b`` are the same selection. Only the comma
    separates — unlike ``OTTO_SUT_DIRS``, which also splits on ``os.pathsep``,
    because these are names rather than paths. Each segment is stripped, so
    ``"a, b"`` and ``"a,b"`` are equivalent (the convention
    :func:`otto.config.lab.split_lab_names` sets for ``--lab``); unlike that
    one, an empty segment is dropped rather than refused, which is what makes a
    trailing comma or a shell-built ``-I "$NAMES"`` harmless.

    Stripping happens BEFORE normalization, not merely as the emptiness test:
    ``normalize_name`` collapses ``[-_.]+`` runs and lowercases but leaves
    whitespace intact, so an unstripped segment would store a name that matches
    no repo — failing OPEN for ``-E`` (the project you meant to switch off stays
    active) and slipping past the conflict rule, since ``"repo-a"`` and
    ``" repo-a"`` do not overlap.

    Normalization happens HERE so every downstream comparison — the conflict
    rule, :func:`otto.config.scope.active`, the unknown-name check — sees one
    spelling.
    """
    if value is None:
        return None
    from ..models.dependencies import normalize_name

    return [
        normalize_name(stripped)
        for item in value
        for part in item.split(",")
        if (stripped := part.strip())
    ]


def _refuse_contradictory_switches(
    include: "list[str] | None", exclude: "list[str] | None"
) -> None:
    """Exit 2 when a name appears in both -I and -E — a contradictory line is a typo."""
    overlap = sorted(set(include or ()) & set(exclude or ()))
    if overlap:
        raise typer.BadParameter(
            f"project(s) {', '.join(overlap)} appear in both --include-projects "
            "and --exclude-projects — pick one",
            param_hint="--include-projects / --exclude-projects",
        )


def _project_completer(ctx: "typer.Context", incomplete: str) -> list[str]:  # noqa: ARG001 — required by Typer autocompletion callback signature
    """Completion source for -I/-E: the DISCOVERED repo names.

    Phase 1 (:func:`otto.bootstrap.discover`), never ``get_repos()``. The
    difference is the whole point: ``get_repos`` runs ``bootstrap()``, which is
    phase 2 — the dependency pass plus every sibling repo's init module, i.e.
    third-party user code. ``entry()`` deliberately keeps that off the
    completion path ("zero user code"), and a repo whose init is slow or opens
    a socket would otherwise hang every ``otto -I <TAB>`` with the bare
    ``except`` below swallowing the reason. Discovery parses settings only, and
    in completion mode it is already computed and cached, so this is free.

    The unknown-name validation in
    :func:`~otto.cli.invoke.validate_project_switches` is a different question
    and correctly uses the full ``bootstrap().repos``: it runs at USE, where
    user code has to run anyway.
    """
    try:
        # Imported inside the function, not at module scope: the tests patch the
        # `otto.bootstrap.discover` attribute, which a module-scope `from ...
        # import discover` would bind past. Keep it local — hoisting it silently
        # takes the tests' seam with it.
        from ..bootstrap import discover

        names = [repo.name for repo in discover().repos]
    except Exception:  # noqa: BLE001 — completion must never crash the shell
        return []
    return [name for name in names if name.startswith(incomplete)]


def _stub_help(name: str, help_text: "str | None") -> str:
    """Return the help line a stub for *name* shows — one spelling, both paths.

    A command with no declared help still needs a line on the root screen, and
    the registry-backed stub and the cache-backed one MUST choose the same
    one: root help renders from whichever is available (a ``names``-section
    hit skips bootstrap entirely), and a placeholder that appeared on only one
    of them would make the same screen depend on whether the cache happened to
    be warm.
    """
    return help_text or f"(run `otto {name} -h` for details)"


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

    @override
    def get_help(self, ctx: Any) -> str:
        # Root help never runs the leaf-invoke preamble, so this is the only
        # place the ROOT screen's banner gets printed. Subcommand help is
        # covered separately: `_real()` wraps every resolved node's own
        # `get_help` (see `otto.cli.invoke._wrap_get_help`).
        from .invoke import ensure_help_banner

        ensure_help_banner(ctx)
        return super().get_help(ctx)

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
            tmp = typer.Typer(name=spec.name, help=_stub_help(spec.name, spec.help))
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
                from ..config.completion_stubs import build_stub_command, build_stub_group

                help_text = _stub_help(cmd_name, entry.get("help"))
                if children:
                    tmp = build_stub_group(cmd_name, help_text, children)
                    rich_stub: Any = typer.main.get_group(tmp)
                else:
                    # get_command flattens the single-command stub app to the
                    # bare leaf, matching how the real command would resolve.
                    tmp = build_stub_command(cmd_name, options, help=help_text)
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
    *,
    labs: Annotated[
        list[str] | None,
        typer.Option(
            "--lab",
            "-l",
            envvar=LAB_ENV_VAR,
            callback=parse_lab_selection,
            autocompletion=_lab_completer,
            metavar="LAB[+LAB...]",
            help="Name of lab(s) to reserve and use; combine labs with '+'.",
        ),
    ] = None,
    include_projects: Annotated[
        list[str] | None,
        typer.Option(
            "--include-projects",
            "-I",
            callback=parse_project_list,
            autocompletion=_project_completer,
            metavar="NAME[,NAME...]",
            help="Force these projects ACTIVE for this invocation (overrides lab inference).",
        ),
    ] = None,
    exclude_projects: Annotated[
        list[str] | None,
        typer.Option(
            "--exclude-projects",
            "-E",
            callback=parse_project_list,
            autocompletion=_project_completer,
            metavar="NAME[,NAME...]",
            help="Switch these projects OFF for this invocation (overrides lab inference).",
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
    probe: Annotated[
        bool,
        typer.Option(
            "--probe",
            help=(
                "With --dry-run: open a connection to each host the command names "
                "and report reachability. A connection only — never a command."
            ),
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
            help="Delete the shell-completion cache files and exit.",
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
    It stashes the root options on ``ctx.meta``, installs the CONSOLE half of
    logging, and returns. It does not load the lab. The console install is
    unconditional and immediate (spec 2026-08-30 §3.1): root's level, otto's
    console handler and the noise floor go on here, so that every gate and
    probe below can simply ``logger.warning``. That reaches ``otto <cmd>
    --help`` too, which previously configured nothing — a process that prints
    a help screen and exits, so the cost is one handler nobody writes through.
    The rest (lab load, session setup, output dir and its FILE sinks,
    reservation gate) runs lazily in the leaf-invoke
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
        ensure_cli_session,
        ensure_lab_context,
        fail_loud_on_bootstrap_errors,
        report_lab_context_error,
        validate_project_switches,
    )

    if probe and not dry_run:
        # A usage error, not a silent promotion to a dry run: --probe DIALS
        # hosts, and the only thing that makes dialing safe is that no command
        # can follow it. That guarantee is --dry-run's, so the dependency is
        # stated rather than assumed. BadParameter exits 2 like every other
        # click usage error.
        raise typer.BadParameter(
            "--probe requires --dry-run/-n: it opens a connection to each host "
            "the command names, which is only safe because a dry run runs no "
            "command afterwards.",
            param_hint="--probe",
        )

    _refuse_contradictory_switches(include_projects, exclude_projects)

    global _root_log_level  # noqa: PLW0603 — one per-invocation value, read by entry()'s frame
    _root_log_level = log_level

    ctx.meta["_otto_root_options"] = RootOptions(
        labs=labs,
        xdir=xdir,
        log_days=log_days,
        log_level=log_level,
        rich_log_file=rich_log_file,
        show_time=show_time,
        dry_run=dry_run,
        probe=probe,
        as_user=as_user,
        skip_reservation_check=skip_reservation_check,
        include_projects=tuple(include_projects or ()),
        exclude_projects=tuple(exclude_projects or ()),
    )

    # Root capture (spec 2026-08-30 §3.1): the console handler goes up NOW,
    # before any project gate or lab probe runs, so their logger.warning calls
    # are visible. The sinks (per-run files) attach later, in create_output_dir.
    # Completion invocations never get here — `ctx.resilient_parsing` returned
    # at the top of this callback — so nothing configures logging for a TAB.
    from ..logger import management

    management.install_console(log_level, show_time=show_time)

    if show_lab or list_hosts:
        # These root flags inspect live lab state, which depends on the
        # registered world — fail the same way dispatch does rather than
        # surfacing a confusing secondary error from a half-registered world.
        # Same two calls in the same order as `command_preamble`: a typo'd
        # -I/-E name is a typo here too, not an input to the demotion decision.
        validate_project_switches(ctx)
        fail_loud_on_bootstrap_errors(ctx)
        # Open the CLI session BEFORE loading the lab. The console handler is
        # already up (installed above), so this is no longer a rescue from a
        # swallowed warning: the session is what applies each repo's
        # [logging.levels] floor and the HostFilter, and what records the
        # xdir/retention state — all of it before the lab load has anything to
        # say, notably the cross-source override warning
        # (`otto.labs.composite`: "host X in lab Y: A overrides B"), the one
        # notice that tells an operator a local source is shadowing the global
        # database. Every other lab-loading path opens the session first
        # (`ensure_lab_session`); this one had grown its own inline load and
        # skipped it.
        ensure_cli_session(ctx)
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
    from ..config.completion_stubs import build_stub_command

    for entry in commands:
        name = entry.get("name")
        if not name:
            continue
        options = entry.get("options") or []
        parent.add_typer(build_stub_command(name, options))


register_builtin_commands()


def _emit_bootstrap_findings(result: "BootstrapResult") -> None:
    """Startup render site for contained bootstrap findings: errors, then warnings.

    Errors gate dispatch later (``fail_loud_on_bootstrap_errors``); warnings
    never do — both surface here as ``warning:`` stderr lines.
    """
    for err in result.errors:
        typer.echo(f"warning: {err}", err=True)
    for warn in result.warnings:
        typer.echo(f"warning: {warn.message}", err=True)


ROOT_HELP_ARGV: tuple[list[str], ...] = ([], ["--help"], ["-h"])
"""The argv tails (``sys.argv[1:]``) that mean "render the root help screen".

EXACT MATCHES, never a membership test. ``otto run --help`` is a subcommand
invocation that needs the real registry, and ``otto host power --on -h`` is a
leaf's own help — both contain a help token, and a scan for one would route
them to a name list that cannot answer them. The empty tail is ``otto`` with
no arguments, which the root Typer turns into help via ``no_args_is_help``.

``-h`` is here because the root app declares ``help_option_names`` as
``["-h", "--help"]``; adding a spelling there without adding it here costs
only the fast path, never correctness.
"""


RAW_ITERATED_NAMES_KEYS: tuple[str, ...] = ("commands", "suites", "instructions")
"""The ``names`` payload keys that reach a RAW iterator and so must be shape-checked.

``_OttoGroup.list_commands`` / ``_OttoGroup._cached_stub`` iterate
``commands`` directly; ``_attach_cached_stubs`` does the same for
``suites`` and ``instructions`` — all three deep inside click's
help/completion pipeline, well outside any containment ``entry()`` can offer.
``_cached_names_payload`` loops over this constant to shape-check them; it
is the only place it is spelled.

(Literals, not ``:func:``/``:meth:`` roles: these are private module members
that autodoc never documents, so a cross-reference role has no target to find
and fails the ``-W`` docs build instead of linking anywhere.)
"""

DELEGATED_NAMES_KEYS: frozenset[str] = frozenset(
    {
        "hosts",
        "hosts_by_lab",
        "docker_hosts",
        "docker_use_cases",
        "term_backends",
        "transfer_backends",
        "usernames",
        "labs",
    }
)
"""The remaining ``names`` payload keys, each consumed by a completer that does
its own ``isinstance`` check and falls back to a live collection — verified,
not assumed. A twelfth key, ``tests``, lives in its own cache section that
``_cached_names_payload`` never loads.

``tests/unit/config/test_cache_sections.py`` pins that
:data:`RAW_ITERATED_NAMES_KEYS` and this constant together equal the ``names``
collector's live key set, so a key landing in the collector without joining
either constant fails that test by name instead of drifting in silently.
"""


def _cached_names_payload(repos: "list[Repo]") -> "dict[str, Any] | None":
    """Return the ``names`` section's payload if it is safe to install.

    THE ONE READER for both fast paths — root help and completion. They are
    siblings by construction (same section, same containment, same fallback),
    and the shape check below is why they must not be spelled twice: the first
    cut of this task applied it to root help only and left completion reading
    the section raw, which turned a corrupt cache from a silent fallback into
    a rendered traceback in the user's shell mid-TAB.

    ``None`` on any miss — cold cache, moved digest, expired TTL, or a
    section written while bootstrap reported errors (tainted). The caller
    then takes the full load, which is the whole contract: cache-or-load,
    never a degraded screen.

    :data:`RAW_ITERATED_NAMES_KEYS` ARE CHECKED HERE, and
    :data:`DELEGATED_NAMES_KEYS` are DELEGATED — the split is not arbitrary.
    :func:`~otto.config.completion_cache.read_cache` type-checks all twelve
    keys and remains a live reader today —
    :func:`~otto.config.completion_cache.cache_rebuild_is_worthwhile` calls it
    for the merged-view validity check — but a single-section read has no such
    pass, so each key needs an owner here. The three in
    :data:`RAW_ITERATED_NAMES_KEYS` reach a RAW iterator deep inside click's
    help/completion pipeline — :meth:`_OttoGroup.list_commands` /
    :meth:`_OttoGroup._cached_stub` for ``commands``, :func:`_attach_cached_stubs`
    for ``suites`` and ``instructions`` — well outside any containment
    ``entry()`` can offer, so a malformed one is a traceback in the user's
    shell mid-TAB rather than a fallback. Every key in
    :data:`DELEGATED_NAMES_KEYS` is consumed by a completer that does its own
    ``isinstance`` and falls back to a live collection — verified, not
    assumed — so re-checking them here would be a second spelling of a rule
    that already has one. (``tests``, the twelfth key, lives in its own cache
    section this reader never loads.)

    Checked one level DEEP, not just ``isinstance(list)``: ``["plug", "x"]``
    is a list, and every one of the three consumers immediately calls
    ``.get("name")`` on its items.
    """
    from ..config.cache_sections import read_section

    payload = read_section(repos, "names")
    if payload is None:
        return None
    for key in RAW_ITERATED_NAMES_KEYS:
        value = payload.get(key)
        if not isinstance(value, list) or not all(isinstance(item, dict) for item in value):
            return None
    return payload


def entry() -> None:
    """Console-script entry: composition root, then the Typer app.

    Completion invocations and the ROOT HELP SCREEN take the cache fast path
    (zero user code); everything else runs :func:`otto.bootstrap.bootstrap`
    before argv parsing so registered third-party commands exist when the root
    group is consulted. Contained user-code failures print one framed warning
    line each; real command dispatch fails loud in the invoke preamble.

    Root help is served from the ``names`` section alone
    (:data:`ROOT_HELP_ARGV`), so it never validates — and therefore never
    walks — the test corpus: the screen is a list of command names and their
    one-line helps, which init trees and top-level test files determine. On
    any miss it falls through to the same full bootstrap every other
    invocation runs, so a cold cache still lists third-party commands.
    """
    import contextlib

    from .. import bootstrap as bs
    from ..config.completion_cache import (
        DUMP_TESTS_ENV_VAR,
        dump_collected_test_names,
        is_completion_mode,
    )

    if os.environ.get(DUMP_TESTS_ENV_VAR):
        # One-shot "collect and print test names" subprocess, spawned by the
        # --tests completer to warm its collected cache (collection never runs
        # inside the completer itself). Any failure exits non-zero with no
        # payload, so the parent treats it as a miss and keeps the static floor.
        code = 1
        with contextlib.suppress(Exception):
            dump_collected_test_names(bs.discover().repos)
            code = 0
        raise SystemExit(code)

    if is_completion_mode():
        # Completion must never traceback into the shell: any discovery
        # failure just leaves the cache unset and falls through to the
        # slow path below.
        #
        # The `names` section ALONE, not the merged view: every completion
        # source except `--tests` is served from it, and validating the
        # `tests` section here would make every TAB walk the whole corpus to
        # answer `otto ho<TAB>`. `otto.cli.test._tests_completer` reads the
        # section it needs, when it needs it.
        #
        # Through `_cached_names_payload`, exactly as root help does: the
        # `suppress` above covers the READ, and nothing else — the payload it
        # installs is consumed later, inside click, where a malformed
        # `commands` list would traceback into the shell rather than fall
        # through to the slow path.
        with contextlib.suppress(Exception):
            bs.set_completion_names(_cached_names_payload(bs.discover().repos))
    elif sys.argv[1:] in ROOT_HELP_ARGV:
        # Root help: the same names, the same reader, contained the same way —
        # a broken cache must cost a full load, not a traceback in front of
        # the help screen.
        with contextlib.suppress(Exception):
            bs.set_completion_names(_cached_names_payload(bs.discover().repos))

    if bs.get_completion_names() is None:
        try:
            result = bs.bootstrap()
        except (FileNotFoundError, ValueError, bs.BootstrapError) as e:
            # Env-level discovery failure (bad OTTO_SUT_DIRS / OTTO_* values;
            # pydantic validation errors are ValueErrors): nothing user-specific
            # can load, so there is no degraded help worth rendering — fail
            # loud but CLEAN (one line, no traceback). Per-repo config-data
            # errors never reach here; discover() contains those.
            #
            # BootstrapError joins them for the ONE variety bootstrap raises
            # instead of containing: ``ProjectScopeError``, a repo that
            # registered providers without declaring the labs it applies to.
            # That refusal is a message the user is meant to act on — it names
            # the repo and prints the TOML block to paste — and a traceback in
            # front of it is noise, not information.
            typer.echo(f"error: {e}", err=True)
            raise SystemExit(1) from e
        _emit_bootstrap_findings(result)
        from ..config.completion_cache import cache_rebuild_is_worthwhile

        # Filled by the validity check, consumed by write_cache: each
        # section's key set is stat-hashed at most once per invocation.
        section_digests: dict[str, str] = {}
        if cache_rebuild_is_worthwhile(result.repos, digests=section_digests):
            from ..config.completion_cache import (
                collect_backend_names,
                collect_cli_commands,
                collect_current_commands,
                collect_docker_capable_host_ids,
                collect_docker_use_case_names,
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
                    docker_hosts=collect_docker_capable_host_ids(result.repos),
                    docker_use_cases=collect_docker_use_case_names(result.repos),
                    term_backends=backends["term_backends"],
                    transfer_backends=backends["transfer_backends"],
                    usernames=collect_reservation_usernames(result.repos),
                    commands=collect_cli_commands(),
                    labs=collect_lab_names(result.repos),
                    tests=collect_test_names(result.repos),
                    hosts_by_lab=collect_host_ids_by_lab(result.repos),
                    digests=section_digests,
                    # A contained bootstrap error means registration did not
                    # finish, so what was just collected is a PARTIAL picture
                    # of this workspace. Storing it untainted would serve that
                    # partial answer from every later `--help` and TAB until
                    # the TTL — and not even then in practice, because the
                    # broken file's stats are stable until someone edits it,
                    # so the digest never moves. Written-but-never-served is
                    # what keeps the next run on the full path, where the
                    # framed warning is printed again.
                    #
                    # Both sections, not just `names`. The taint is about the
                    # WORKSPACE the collect ran against, and `errors` carries
                    # discovery failures too: a repo whose `settings.toml`
                    # will not parse is absent from `result.repos` entirely,
                    # so its corpus is missing from the `tests` floor exactly
                    # as its instructions are missing from `names`.
                    tainted=bool(result.errors),
                )

    import traceback

    from ..context import reset_cli_context
    from ..errors import OttoError
    from .invoke import print_error

    try:
        app()
    except OttoError as e:
        # THE BOUNDARY FRAME. Every exception otto defines is a sentence
        # written for the person who typed the command — it names the host,
        # the repo, the file to edit. A leaf that forgets to catch its own
        # therefore does not merely look untidy: the message the taxonomy
        # exists to deliver arrives buried under a stack trace of otto's
        # internals, and the reader's takeaway is "otto crashed".
        #
        # Leaves still catch what they can say something BETTER about (a usage
        # hint, a different exit code); this is the floor under the ones that
        # do not, so a new pattern-walking command cannot ship with a traceback
        # for its empty-selection case the way `otto monitor` and `otto cov`
        # both did.
        #
        # Deliberately NOT a bare `except Exception`: anything that is not an
        # OttoError is either click's own control flow (SystemExit, Exit,
        # ClickException — none of them subclasses) or a genuine bug, and a
        # bug's traceback is the most useful thing otto can print about it.
        #
        # The stack is not DESTROYED, only demoted: an OttoError raised from
        # somewhere it has no business being is a bug, and the maintainer
        # chasing it needs the frames. Debug logging prints them.
        #
        # Both spellings of that one knob count: `_root_log_level` is what the
        # root callback actually resolved (the flag, or OTTO_LOG_LEVEL through
        # Typer's `envvar=`), and the environment is read as well for the case
        # where the callback never got to run — which is also the case where
        # nothing has installed a handler.
        #
        # Printed straight to stderr rather than through `logger.debug`. The
        # console handler IS up by now on every path that reached a command
        # (spec 2026-08-30 §3.1), so this is no longer about reaching nobody —
        # it is that a traceback is not log text: the console handler renders
        # rich markup and folds at the console width, and frames carry both
        # brackets and significant leading whitespace. The raw stderr write
        # also keeps working on the branch above, where the callback never ran
        # and there is no handler to write through.
        if "DEBUG" in (
            (_root_log_level or "").upper(),
            os.environ.get(LOG_LVL_ENV_VAR, "").upper(),
        ):
            traceback.print_exc()
        print_error(f"error: {e}")
        raise SystemExit(1) from e
    finally:
        reset_cli_context()
