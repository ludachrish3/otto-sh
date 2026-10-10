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

from ..config.env import (
    DEFAULT_LOG_RETENTION_DAYS,
    FIELD_PRODUCT_ENV_VAR,
    LAB_ENV_VAR,
    LOG_DAYS_ENV_VAR,
    LOG_LVL_ENV_VAR,
    LOG_RICH_ENV_VAR,
    SUT_DIRS_ENV_VAR,
    XDIR_ENV_VAR,
)
from ..logger.levels import LEVEL_NAMES
from ..version import get_version
from .completers import completion_source

if TYPE_CHECKING:
    from ..bootstrap import BootstrapResult
    from ..config.repo import Repo
    from .registry import CommandSpec

# TODO: Should rich help menus be optional?
# Uncomment the line below to remove rich help menu formatting globally
# typer.core.HAS_RICH = False  # noqa: ERA001 — intentional documented escape-hatch example

_root_log_level: str | None = None
"""The ``--log-level`` value the root callback resolved, or None before it ran.

Read by :func:`print_traceback_if_debug` to decide whether a demoted traceback
is wanted. Set from the callback because by the time the frame runs, the Typer
context that carried ``RootOptions`` is gone.

Deliberately the value the operator TYPED, not a level read back off a logger.
The callback does now put otto's verbose floor on root (spec 2026-08-30 §3.1),
so root's level is readable here — but it is process state anything can move
(``otto.logger.install`` in an embedding process, a suite, pytest's
``log_cli``), and "did the operator ask for debug?" is a question about this
invocation rather than about the handler stack it happens to have.
"""

DESCRIPTION = f"""
O.T.T.O. (Our Trusty Testing Orchestrator)

If a development repo is under test, then {SUT_DIRS_ENV_VAR} must be set in your environment.
It is a list of paths to repo root directories, separated by `,` or the OS path separator
(`:` on Linux/macOS, `;` on Windows).

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
        print(f"otto version: {get_version()}")  # noqa: T201 — see docstring
        raise typer.Exit


def list_labs_callback(value: bool) -> None:
    """Print all available lab names (one panel per repo) and exit when the flag is set."""
    if value:
        from rich import print as rprint
        from rich.panel import Panel
        from rich.table import Table

        from ..bootstrap import get_repos

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
    """Upper-case the ``--log-level`` value, refusing a name otto does not know."""
    level = value.upper()
    if level not in LEVEL_NAMES:
        choices = ", ".join(LEVEL_NAMES)
        raise typer.BadParameter(f"{value!r} is not a log level; choose from {choices}")
    return level


@completion_source(kind="static", values=LEVEL_NAMES, match_case=True)
def _log_level_completer(ctx: "typer.Context", incomplete: str) -> list[str]:  # noqa: ARG001 — required by Typer autocompletion callback signature
    """Completion source for ``--log-level``: the level names, in the fragment's case.

    People type levels in lower case, so a lower-case fragment completes to
    lower-case names (``deb`` -> ``debug``); anything else gets the canonical
    upper-case names. The callback upper-cases whatever is given. The shim's
    ``match_case`` static source reproduces exactly this, including Typer's
    own prefix filter, which drops a mixed-case fragment's upper-case answer
    (applied here too, so a direct call answers what the shell is shown).
    """
    hits = [name for name in LEVEL_NAMES if name.lower().startswith(incomplete.lower())]
    answered = [name.lower() for name in hits] if incomplete.islower() else hits
    return [name for name in answered if name.startswith(incomplete)]


@completion_source(kind="payload", key="usernames", sort=True)
def _username_completer(ctx: "typer.Context", incomplete: str) -> list[str]:  # noqa: ARG001 — required by Typer autocompletion callback signature
    """Completion source for ``--holder``: usernames the reservation backend knows.

    Prefers the completion-cache snapshot (slow-path populated, so no backend is
    built in the completion fast path); falls back to a live best-effort
    collection on a cache miss. Empty when the backend can't enumerate users.
    """
    from ..bootstrap import get_completion_names, get_repos
    from ..config.completion_cache import collect_reservation_usernames

    cached = get_completion_names()
    if cached is not None and isinstance(cached.get("usernames"), list):
        names = cached["usernames"]
    else:
        names = collect_reservation_usernames(get_repos())
    return sorted(n for n in names if n.startswith(incomplete))


@completion_source(kind="payload", key="labs", sort=True, sep="+")
def _lab_completer(ctx: "typer.Context", incomplete: str) -> list[str]:  # noqa: ARG001 — required by Typer autocompletion callback signature
    """Completion source for ``--lab``: lab names referenced by the lab.json files.

    Prefers the completion-cache snapshot; falls back to a live, data-only scan
    (:func:`~otto.config.completion_cache.collect_lab_names`, no user
    code). ``--lab`` combines labs with ``+``, so only the in-progress segment
    is completed and already-named labs are dropped.
    """
    from ..bootstrap import get_completion_names, get_repos
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
    from ..session import ProjectSelectionError, check_project_overlap
    from .invoke import report_project_selection_error

    try:
        check_project_overlap(include or [], exclude or [])
    except ProjectSelectionError as e:
        report_project_selection_error(e)


@completion_source(kind="payload", key="projects", sort=False)
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


PENDING_SUBCMD_ARGS_KEY = "_pending_subcmd_args"
"""The ``ctx.meta`` key the root group's dispatch protocol reads; the
completion tree serialiser sets it to resolve each child real."""


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
    _cache_revision: int

    @override
    def parse_args(self, ctx: Any, args: list[str]) -> list[str]:
        # ctx: Any mirrors HostGroup.list_commands — Typer's vendored click fork
        # makes typer.Context (typer.models.Context) incompatible with the
        # parent's _click.Context under strict typing.
        result = super().parse_args(ctx, args)
        # Save the pending subcommand tokens (subcommand name + its args) so
        # the main() callback can inspect them even after invoke() clears them.
        ctx.meta[PENDING_SUBCMD_ARGS_KEY] = list(
            getattr(ctx, "_protected_args", []) + getattr(ctx, "args", [])
        )
        return result

    def _dispatch_target(self, ctx: Any) -> str | None:
        """Return the subcommand name pending dispatch, if any."""
        pending = ctx.meta.get(PENDING_SUBCMD_ARGS_KEY) or []
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

    def _caches(self) -> "tuple[dict[str, Any], dict[str, Any]]":
        """Return the stub and real command caches, dropped whenever CLI_COMMANDS changed."""
        from .registry import CLI_COMMANDS

        if getattr(self, "_cache_revision", None) != CLI_COMMANDS.revision:
            self._stub_cache = {}
            self._real_cache = {}
            self._cache_revision = CLI_COMMANDS.revision
        return self._stub_cache, self._real_cache

    def _stub(self, spec: "CommandSpec") -> Any:
        """Return (building + caching once) a lightweight help-only stub for *spec*."""
        cache, _ = self._caches()
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
        _, cache = self._caches()
        if spec.name not in cache:
            from ..bootstrap import get_completion_names
            from .registry import resolve_spec_command

            loader = spec.loader
            cached_names = get_completion_names()
            if cached_names is not None and isinstance(loader, str):
                # Completion fast path: the registries are not populated, so
                # `run` gains its cached instruction stubs and `test` its
                # cached verb flags before conversion.
                mod_name, _, attr = loader.partition(":")
                if spec.name == "test":
                    from .test import build_test_app

                    sub_app = build_test_app(cached_names.get("test_options", []))
                else:
                    sub_app = getattr(importlib.import_module(mod_name), attr)
                if spec.name == "run":
                    _attach_cached_stubs(sub_app, cached_names.get("instructions", []))
                spec = dataclasses.replace(spec, loader=sub_app)
            from .invoke import wrap_leaf_callbacks

            cache[spec.name] = wrap_leaf_callbacks(resolve_spec_command(spec), spec)
        return cache[spec.name]

    @override
    def list_commands(self, ctx: Any) -> list[str]:
        from ..bootstrap import get_completion_names
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
        from ..bootstrap import get_completion_names
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
            cache, _ = self._caches()
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
    field: Annotated[
        bool,
        typer.Option(
            "--field/--debug",
            envvar=FIELD_PRODUCT_ENV_VAR,
            help=(
                "Install the field or the debug variant of each product: a \\[\\[products]] "
                'entry with `variant = "field"` is used only under --field, one with '
                '`variant = "debug"` only under --debug, one without under both.'
            ),
        ),
    ] = False,
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
            autocompletion=_log_level_completer,
            help=f"Level at which to log: {', '.join(LEVEL_NAMES)} (any case).",
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
            "-t",
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
    list_products: Annotated[
        bool,
        typer.Option(
            "--list-products",
            help=(
                "List the products the repos declare; with --lab, which hosts each one "
                "lands on and what was left out."
            ),
        ),
    ] = False,
    list_tools: Annotated[
        bool,
        typer.Option(
            "--list-tools",
            help=(
                "List the dev tools the repos declare; with --lab, which hosts each one "
                "lands on and what was left out."
            ),
        ),
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
    holder: Annotated[
        str | None,
        typer.Option(
            "--holder",
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
    it inline here before printing and exiting, and ``--list-products`` /
    ``--list-tools``, which do the same only when ``--lab`` is given.
    It installs the run's policy (``--field``/``--debug`` and ``--dry-run``,
    with the teardown deadline from ``OTTO_TEARDOWN_DEADLINE``) and registers
    its reset on the root context's ``call_on_close``, so the policy never
    outlives the invocation.
    """
    if ctx.resilient_parsing:
        return

    from .invoke import RootOptions, ensure_inline_lab

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

    from ..bootstrap import discovered_teardown_deadline
    from ..invocation import RunPolicy, install_policy, reset_binding

    policy = RunPolicy(dry_run=dry_run, variant="field" if field else "debug")
    policy_binding = install_policy(policy)
    # Undone when Click closes this invocation's root context: exactly once, on this
    # thread and in this execution context, for the console script and app() /
    # CliRunner alike. Registered before anything else runs, so an interrupt
    # escaping discovery still resets it. Close callbacks run last-in first-out,
    # so the context ensure_lab_context installs later is reset before this policy.
    ctx.find_root().call_on_close(lambda: reset_binding(policy_binding))
    deadline = discovered_teardown_deadline()  # after installing: a failed discovery leaves 10 s
    if deadline is not None:
        policy.teardown_deadline = deadline

    ctx.meta["_otto_root_options"] = RootOptions(
        labs=labs,
        xdir=xdir,
        log_days=log_days,
        log_level=log_level,
        rich_log_file=rich_log_file,
        show_time=show_time,
        dry_run=dry_run,
        probe=probe,
        holder=holder,
        skip_reservation_check=skip_reservation_check,
        field=field,
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

    if list_products or list_tools:
        # Read-only listings that contact no host. Without --lab they describe
        # what the repos declare, so they do not demand one (unlike --list-hosts).
        from .listing import show_listing

        if list_products:
            show_listing(ctx, "products")
        if list_tools:
            show_listing(ctx, "dev_tools")
        if not (show_lab or list_hosts):
            raise typer.Exit

    if show_lab or list_hosts:
        # These root flags inspect live lab state: gates, session and lab load
        # exactly as dispatch orders them (`ensure_inline_lab` — shared with
        # `otto host --list-hosts`, which used to load nothing and traceback),
        # then print and exit.
        ensure_inline_lab(ctx)
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
    """Rebuild per-instruction stubs under ``parent`` from the cache.

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


def _cache_is_writable(repos: list["Repo"]) -> bool:
    """:func:`~otto.config.completion_cache.cache_is_writable`, imported only when asked.

    ``entry()`` asks only on the paths that read the cache; an ordinary command
    must not import the cache module just to skip it.
    """
    from ..config.completion_cache import cache_is_writable

    return cache_is_writable(repos)


RAW_ITERATED_NAMES_KEYS: tuple[str, ...] = ("commands", "instructions", "test_options")
"""The ``names`` payload keys that reach a RAW iterator and so must be shape-checked.

``_OttoGroup.list_commands`` / ``_OttoGroup._cached_stub`` iterate
``commands`` directly; ``_attach_cached_stubs`` does the same for
``instructions``, and ``otto.cli.test.build_test_app`` for ``test_options`` —
all deep inside click's
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
        "host_drops",
        "docker_hosts",
        "docker_use_cases",
        "docker_images",
        "docker_services_by_use_case",
        "repos",
        "term_backends",
        "transfer_backends",
        "usernames",
        "labs",
        "host_classes_by_id",
        "projects",
        "links",
        "logins_by_host",
        "docker_default_parent_by_lab",
    }
)
"""The remaining ``names`` payload keys. Each is consumed by a completer that
does its own ``isinstance`` check and falls back to a live collection —
verified, not assumed — except ``host_drops``, ``projects`` and ``links``,
which no Typer completer reads at all. ``host_drops`` is
the outlet's payload: read by ``otto cache info`` straight from the cache
file. ``host_classes_by_id`` is consumed by
``otto.cli.expose.cached_host_class_for_id`` to scope a host
verb's TAB menu without building the host; ``projects`` and ``links`` are
consumed by the shim's resolver, which reads the whole payload behind one
broad ``except`` and hands over — their Typer-side completers
(``_project_completer`` and the link-id completer) stay live, spec §5 lists
no change to them. ``logins_by_host`` is read by
``otto.cli.completers.host_user_completer``, which scopes a host verb's
``--user`` menu to the typed host's logins. ``docker_images``,
``docker_services_by_use_case`` and ``repos`` are the declared names the docker
verbs complete from (images, the services of a typed use-case, ``--repo``).
``docker_default_parent_by_lab`` maps each lab to the parent the docker verbs
default to by the one rule (a lab the rule refuses is absent); the docker
completers read it to scope a container id to the default parent.
Test names are in no section:
they live in the per-file test tables (``otto.config.collected_tests``).

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
    :func:`~otto.config.completion_cache.read_cache` type-checks every
    key and remains a live reader today —
    :func:`~otto.config.completion_cache.cache_is_stale` calls it
    for the validity check — but a single-section read has no such
    pass, so each key needs an owner here. The two in
    :data:`RAW_ITERATED_NAMES_KEYS` reach a RAW iterator deep inside click's
    help/completion pipeline — :meth:`_OttoGroup.list_commands` /
    :meth:`_OttoGroup._cached_stub` for ``commands``, :func:`_attach_cached_stubs`
    for ``instructions`` — well outside any containment
    ``entry()`` can offer, so a malformed one is a traceback in the user's
    shell mid-TAB rather than a fallback. Every key in
    :data:`DELEGATED_NAMES_KEYS` reaches its reader inside a containment that
    already falls back — a completer's own ``isinstance`` for most of them,
    ``otto cache info``'s own reader for ``host_drops``, the shim's broad
    ``except``-and-hand-over for ``projects`` and ``links`` (see that
    constant's own note) — so re-checking them here would be a second
    spelling of a rule that already has one.

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


def print_traceback_if_debug() -> None:
    """Print the exception being handled to stderr, only when logging at DEBUG.

    For an ``except`` leg that renders a failure as one line: the frames are
    demoted, not destroyed, because the maintainer chasing a failure that
    should not have happened needs them.

    Both spellings of the one knob count: ``_root_log_level`` is what the root
    callback actually resolved (the flag, or ``OTTO_LOG_LEVEL`` through
    Typer's ``envvar=``), and the environment is read as well for the case
    where the callback never got to run, which is also the case where nothing
    has installed a handler.

    Printed straight to stderr rather than through ``logger.debug``: a
    traceback is not log text. The console handler renders rich markup and
    folds at the console width, and frames carry both brackets and
    significant leading whitespace. The raw write also works where no
    handler was ever installed.
    """
    if "DEBUG" in (
        (_root_log_level or "").upper(),
        os.environ.get(LOG_LVL_ENV_VAR, "").upper(),
    ):
        import traceback

        traceback.print_exc()


def entry(cache_stale: bool = False) -> None:
    """Console-script entry: composition root, then the Typer app.

    Completion invocations and the ROOT HELP SCREEN take the cache fast path
    (zero user code); everything else runs :func:`otto.bootstrap.bootstrap`
    before argv parsing so registered third-party commands exist when the root
    group is consulted. Contained user-code failures print one framed warning
    line each; real command dispatch fails loud in the invoke preamble. Repo
    test files are not part of that bootstrap, and a cache rebuild never
    reads them: test names come only from pytest's collections, which write
    the per-file test tables.

    Root help is served from the ``names`` section alone
    (:data:`ROOT_HELP_ARGV`): the screen is a list of command names and their
    one-line helps, which the init trees determine. On
    any miss it falls through to the same full bootstrap every other
    invocation runs, so a cold cache still lists third-party commands.

    Root help and completion are also the ONLY paths that check the cache's
    validity and rebuild it on a miss. Every other invocation bootstraps and
    dispatches without touching the completion cache at all — no read, no
    validity check, no write: that check stats every key path, and a
    command that never consults the cache has no business paying for it.

    *cache_stale* is set by the bash shim when it handed this TAB over
    because the cache itself is stale. The names fast path is then skipped,
    so this invocation bootstraps and rebuilds, and the next TAB is answered
    by the shim again.
    """
    import contextlib

    from .. import bootstrap as bs
    from ..config.completion_cache import (
        DUMP_TESTS_ENV_VAR,
        discard_collect_refresh_request,
        is_completion_mode,
        spawn_requested_refresh,
    )

    if os.environ.get(DUMP_TESTS_ENV_VAR):
        # The collect child, spawned by a test-name completer: it seeds or
        # refreshes the per-file test tables and prints nothing (collection
        # never runs inside the completer, whose stdout is the shell's). It
        # bootstraps as `otto test` does (libs on the path, init modules
        # imported), so it collects what a run would, but only once it holds
        # the collect lock and its deadline is armed. Any failure exits
        # non-zero with the cooldown stamped, so the next TABs neither wait
        # nor spawn again for a while; the waiting parent answers what the
        # tables hold.
        code = 1
        try:
            from ..config.collected_tests import collect_child_main
            from ..suite.run import _refresh_tables

            code = collect_child_main(lambda: bs.bootstrap().repos, _refresh_tables)
        except Exception as exc:  # noqa: BLE001 — the child reports by exit code and cooldown only
            with contextlib.suppress(Exception):
                from ..config.completion_cache import stamp_collect_cooldown

                stamp_collect_cooldown(f"{type(exc).__name__}: {exc}")
        raise SystemExit(code)

    completion = is_completion_mode()
    if completion:
        discard_collect_refresh_request()
    reads_cache = completion or sys.argv[1:] in ROOT_HELP_ARGV
    if completion and not cache_stale:
        # Completion must never traceback into the shell: any discovery
        # failure just leaves the cache unset and falls through to the
        # slow path below.
        #
        # The `names` section: every completion source except `otto test`'s
        # NAMES and `-m` is served from it. Those two read each repo's test
        # table (`otto.config.collected_tests.completion_view`), when they
        # need it, so `otto ho<TAB>` never looks at the test tree.
        #
        # Through `_cached_names_payload`, exactly as root help does: the
        # `suppress` above covers the READ, and nothing else — the payload it
        # installs is consumed later, inside click, where a malformed
        # `commands` list would traceback into the shell rather than fall
        # through to the slow path.
        with contextlib.suppress(Exception):
            bs.set_completion_names(_cached_names_payload(bs.discover().repos))
    elif not completion and sys.argv[1:] in ROOT_HELP_ARGV:
        # Root help: the same names, the same reader, contained the same way —
        # a broken cache must cost a full load, not a traceback in front of
        # the help screen. Completion never reaches this branch: its argv
        # tail (`sys.argv[1:] == []`) coincides with a root-help tail, but a
        # bash TAB is never the root help screen.
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
            # errors never reach here: discover() contains a settings file
            # that will not parse, and bootstrap() records a bad [os_profiles]
            # table as that repo's load error, as it does a failed init module.
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

        # Only the paths that READ the cache refresh it. Validating it stats
        # every key path, and an ordinary command would pay that on every
        # invocation for a cache it never consults: a round trip per file on
        # a network filesystem. Completion and root help are the readers; a
        # stale bash TAB reaches here with cache_stale set.
        #
        # Writability first: it costs no key-set stats, and when no entry could
        # be stored (no home, an inventory with no stable fingerprint) every
        # root help and TAB lands here, so the validity check is skipped too.
        if reads_cache and _cache_is_writable(result.repos):
            from ..config.completion_cache import cache_is_stale
            from ..config.corpus_snapshot import corpus_snapshot

            # The validity check and the shim's stat triples ask about the same
            # key paths; the scope makes that one stat per path.
            with corpus_snapshot():
                # Filled by the validity check, consumed by write_cache: each
                # section's key set is hashed at most once per invocation, and
                # the scope makes each path's stat one syscall.
                section_digests: dict[str, str] = {}
                if cache_is_stale(result.repos, digests=section_digests):
                    from ..config.completion_cache import (
                        collect_backend_names,
                        collect_cli_commands,
                        collect_current_commands,
                        collect_docker_capable_host_ids,
                        collect_docker_default_parent_by_lab,
                        collect_docker_image_names,
                        collect_docker_services_by_use_case,
                        collect_docker_use_case_names,
                        collect_host_classes_by_id,
                        collect_host_drops,
                        collect_host_ids,
                        collect_host_ids_by_lab,
                        collect_lab_names,
                        collect_links,
                        collect_logins_by_host,
                        collect_project_names,
                        collect_repo_names,
                        collect_reservation_usernames,
                        collect_test_verb_options,
                        write_cache,
                    )
                    from ..config.completion_tree import build_shim_payload

                    instructions = collect_current_commands()
                    backends = collect_backend_names()
                    with contextlib.suppress(OSError):
                        write_cache(
                            result.repos,
                            instructions,
                            collect_host_ids(result.repos),
                            test_options=collect_test_verb_options(),
                            docker_hosts=collect_docker_capable_host_ids(result.repos),
                            docker_use_cases=collect_docker_use_case_names(result.repos),
                            docker_images=collect_docker_image_names(result.repos),
                            docker_services_by_use_case=collect_docker_services_by_use_case(
                                result.repos
                            ),
                            repos_names=collect_repo_names(result.repos),
                            term_backends=backends["term_backends"],
                            transfer_backends=backends["transfer_backends"],
                            usernames=collect_reservation_usernames(result.repos),
                            commands=collect_cli_commands(),
                            labs=collect_lab_names(result.repos),
                            hosts_by_lab=collect_host_ids_by_lab(result.repos),
                            host_drops=collect_host_drops(result.repos),
                            host_classes_by_id=collect_host_classes_by_id(result.repos),
                            projects=collect_project_names(),
                            links=collect_links(result.repos),
                            logins_by_host=collect_logins_by_host(result.repos),
                            docker_default_parent_by_lab=collect_docker_default_parent_by_lab(
                                result.repos
                            ),
                            # No explicit `app`: the Section's `_collect_shim` and this
                            # call both default to `otto.cli.main.app`, so the tree
                            # has one source and cannot drift between the two.
                            shim=build_shim_payload(result.repos),
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
                            # so its instructions are missing from `names` and its
                            # test table from the shim's `tables`.
                            tainted=bool(result.errors),
                        )

    from ..errors import OttoError
    from .invoke import print_error, render_instrumentation_refusal

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
        # chasing it needs the frames. Debug logging prints them, and both log
        # files always keep them. The files take the FULL message: a coverage
        # refusal's plain verdict listing lives in `str(e)`, and a log file
        # cannot hold the table the console gets instead.
        print_traceback_if_debug()
        from ..logger.management import record_command_failure

        record_command_failure(e, f"error: {e}")
        # A coverage refusal carries its per-product verdicts as structure as
        # well as text; on a console those render as the rounded table and the
        # error line keeps only the headline. Here rather than in a leaf
        # because `otto test --cov` raises it from inside `run_tests`, which
        # the leaf deliberately does not catch — this frame is the one place
        # every coverage-running verb passes through. Anything else comes back
        # as its own full message, unchanged.
        print_error(f"error: {render_instrumentation_refusal(e)}")
        raise SystemExit(1) from e
    except BaseException as e:
        # A crash. An Exception was marked by Typer for its own excepthook,
        # which prints the rich traceback once the re-raise leaves the
        # process; anything else (a leaked `asyncio.CancelledError`) gets the
        # interpreter's. All this frame adds is the same failure in both log
        # files, written as soon as the logging listener reaches it.
        #
        # The three exits that are not failures pass untouched. Click's own
        # control flow never lands here as anything else: in standalone mode a
        # usage error, `typer.Exit`, `--help` and Ctrl-C all leave `app()` as
        # SystemExit, and an interrupt otto raises itself is a
        # KeyboardInterrupt.
        if not isinstance(e, (SystemExit, KeyboardInterrupt, GeneratorExit)):
            from ..logger.management import record_command_failure

            record_command_failure(e, f"uncaught {type(e).__name__} ended the command")
        raise
    finally:
        if completion:
            # After the answer is printed: a completer that answered from a
            # table with moved files asked for the refresh behind it.
            spawn_requested_refresh()
