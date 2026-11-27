"""The CLI command registry: how commands — first- and third-party — join ``otto``.

A :class:`CommandSpec` describes one top-level command or group: its name, a
loader (a live Typer app, a plain/async function, or a lazy ``"pkg.mod:attr"``
string imported only on dispatch), the help line shown by ``otto --help``
*without* importing the module, and dispatch metadata (``lab_free``,
``output_dir``, ``gate``, ``dry_run_preview``, ``accepts_options``). First-party subcommands and
third-party plugins register through the same :func:`register_cli_command`.
"""

import functools
import importlib
import inspect
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

import typer
from typer.models import TyperInfo
from typing_extensions import final

from ..registry import Registry, get_registering_project, registration_boundary
from .builtin_commands import register_builtin_commands
from .invoke import prepare_command_target

if TYPE_CHECKING:
    from ..declared import BootstrapResult
    from ..params import Registration

__all__ = ["CLI_COMMANDS", "CommandSpec", "cli_command", "register_cli_command"]


@final
@dataclass(frozen=True)
class CommandSpec:
    """One registered top-level CLI command or group."""

    name: str
    """CLI name as the user types it (e.g. ``"run"``, ``"flash"``)."""

    loader: Any
    """A ``typer.Typer`` app, a plain/async function, or a lazy ``"pkg.mod:attr"`` string."""

    help: str | None = None
    """One-line help for ``otto --help`` — rendered without importing the module."""

    lab_free: bool = False
    """True when the command never needs the lab (e.g. ``schema``)."""

    output_dir: bool = True
    """Whether invocations create a per-command output directory."""

    gate: bool = True
    """Whether invocations run the reservation gate (ignored when ``lab_free``)."""

    accepts_options: bool | None = None
    """Whether this command offers the flags of registered options classes.

    ``None``: the registration said nothing, so a command that uses the lab
    does and a ``lab_free`` one does not. A leaf beneath a group can differ
    from its group with ``__cli_accepts_options__``; whether a command accepts
    is read by :func:`~otto.cli.invoke.leaf_accepts_options`."""

    dry_run_preview: bool = False
    """Whether ``--dry-run`` lets this command's leaves run their bodies.

    ``False`` (the default) means the seam stops the invocation after
    validation and prints the would-run block, so a command author who never
    considered dry runs cannot contact a device under one. ``True`` buys the
    deeper, configuration-only preview: the body runs and short-circuits at
    its own ``is_dry_run()`` branch (the shape ``link``/``tunnel`` ship). An
    individual leaf may opt itself in without opting in the whole group -- see
    ``cli_exposed(dry_run_preview=True)`` and
    :func:`~otto.cli.invoke.stop_at_dry_run_seam`."""

    async_leaves: bool = False
    """True when every leaf under this command must be ``async def``.

    Set for ``run``: an instruction is the lab-work lane, and only a coroutine
    reaches the lifecycle bridge. Enforced at INVOCATION (see
    ``cli/invoke._wrap_invoke``) rather than at registration, so the routes
    ``@instruction``'s own check cannot see — a directly-registered
    ``InstructionEntry``, ``@run_app.command()``, a sub-group added with
    ``add_typer`` — are all covered by the one seam every executed leaf
    passes through.

    NOT set for ``test``: the ``otto test`` leaf is sync, because
    ``pytest.main`` is."""


CLI_COMMANDS: Registry[CommandSpec] = Registry(
    "CLI command", entry=CommandSpec, register_hint="otto.register_cli_command()"
)
"""Every registered top-level ``otto`` command or group, keyed by CLI name.

``CLI_COMMANDS.origin(name)`` is the module that registered a command."""


@registration_boundary
def register_cli_command(
    name: str,
    loader: Any,
    *,
    help: str | None = None,  # noqa: A002 — mirrors typer's own `help=` keyword
    lab_free: bool = False,
    output_dir: bool = True,
    gate: bool = True,
    async_leaves: bool = False,
    dry_run_preview: bool = False,
    accepts_options: bool | None = None,
    overwrite: bool = False,
) -> None:
    """Register a top-level ``otto`` command or group.

    *loader* is a ``typer.Typer`` app (group), a plain/async function (leaf
    command), or a ``"pkg.mod:attr"`` string resolved lazily on dispatch.
    A taken name raises :class:`~otto.registry.DuplicateRegistration`, naming
    both registering modules, unless *overwrite* is true, which replaces the
    command deliberately.

    *dry_run_preview* opts every leaf under this command out of the
    ``--dry-run`` seam default (see :attr:`CommandSpec.dry_run_preview`).

    *accepts_options* says whether the command offers the flags of registered
    options classes; left ``None``, a command that uses the lab does and a
    *lab_free* one does not (see :attr:`CommandSpec.accepts_options`).

    The command is credited to the module that called this function (or
    applied :func:`cli_command`), which is what the completion cache reads to
    tell a third-party command from one of otto's own.
    """
    if help is None and isinstance(loader, typer.Typer):
        # A live app already carries its Typer-native help — read it once here
        # so the spec (the single source of truth for root help AND the
        # completion cache) inherits it instead of rendering a placeholder.
        # Lazy "pkg.mod:attr" loaders have nothing to read without importing;
        # that is what explicit help= is for.
        help = _live_app_help(loader)  # noqa: A001 — mirrors typer's own `help=` keyword
    spec = CommandSpec(
        name=name,
        loader=loader,
        help=help,
        lab_free=lab_free,
        output_dir=output_dir,
        gate=gate,
        async_leaves=async_leaves,
        dry_run_preview=dry_run_preview,
        accepts_options=accepts_options,
    )
    CLI_COMMANDS.register(name=name, entry=spec, overwrite=overwrite)


def cli_command(
    *,
    options: type | None = None,
    name: str | None = None,
    help: str | None = None,  # noqa: A002 — mirrors typer's own `help=` keyword
    lab_free: bool = False,
    output_dir: bool = True,
    gate: bool = True,
    dry_run_preview: bool = False,
    accepts_options: bool | None = None,
) -> Callable[..., Any]:
    """Register an async function as a top-level ``otto`` command.

    The ergonomics match ``@instruction``: an ``OttoContext``-annotated
    parameter is injected (hidden from the CLI), and ``options=`` expands a
    pydantic-dataclass into flags. The command name defaults to the function
    name with underscores dashed.

    A LAB-BOUND command must be ``async def``; a ``lab_free=True`` one may be
    sync. Only a coroutine reaches the lifecycle bridge, so a sync lab-bound
    command leaks the hosts it opens and ignores the interrupt policy.

    ``lab_free`` is the best axis available here, not a perfect one: it means
    "otto will not load a lab, open a session or run the gate for you", NOT
    "touches no hosts" — ``otto monitor`` is lab-free, sync, and calls
    ``all_hosts()`` itself. Read the exemption as "I drive the lifecycle
    myself", which is exactly what monitor does. ``async def`` is in turn
    necessary and not sufficient: a body that blocks the event loop is no more
    interruptible than a sync one (see :func:`~otto.instructions.instruction`).

    ``accepts_options`` is :func:`register_cli_command`'s: whether the command
    offers the flags of registered options classes. The command's options
    are prepared when it is invoked (``otto.cli.invoke.prepare_command_options``):
    ``options=`` is its own class (credited to the registering project), the
    classes covering it are added when it accepts, all of them are validated
    before the body and shown under ``--dry-run``, and the function receives
    the own class's instance and its own parameters only.
    """

    @registration_boundary
    def decorator(func: Callable[..., Any]) -> Callable[..., Any]:
        if not lab_free and not inspect.iscoroutinefunction(func):
            raise TypeError(
                f"lab-bound command {getattr(func, '__name__', repr(func))!r} must be "
                "`async def`: only a coroutine reaches otto's lifecycle bridge, so a "
                "plain `def` runs with its hosts never swept and an interrupt never "
                "turned into a clean exit. Write it as `async def` — a body with "
                "nothing to await is still correct — or, if it touches no lab hosts, "
                "register it with lab_free=True, where sync is exactly right."
            )
        target = prepare_command_target(func, options, project=get_registering_project())
        cmd_name = name or getattr(func, "__name__", repr(func)).replace("_", "-")
        doc_line = ((func.__doc__ or "").strip().splitlines() or [""])[0]
        # Register the prepared callable itself — resolve_spec_command's
        # function-loader branch wraps it in a throwaway Typer on dispatch
        # (same as expose._synthesize_command), so it always resolves to a
        # leaf command, never a same-named nested group.
        register_cli_command(
            cmd_name,
            target,
            help=help or (doc_line or None),
            lab_free=lab_free,
            output_dir=output_dir,
            gate=gate,
            dry_run_preview=dry_run_preview,
            accepts_options=accepts_options,
        )
        return func

    return decorator


def _typer_app_flattens(app: typer.Typer) -> bool:
    """Whether Typer's native ``get_command`` would flatten *app* to a bare leaf.

    Mirrors the predicate inside ``typer.main.get_command``: an app with no
    callback (root or ``info``), no sub-groups, and exactly one registered
    command collapses into that single command rather than a group. ``monitor``
    (one ``@monitor_app.command()``, no callback) is the motivating case — its
    documented flat CLI (``otto monitor --live``) depends on this.
    """
    return not (
        app.registered_callback
        or app.info.callback
        or app.registered_groups
        or len(app.registered_commands) != 1
    )


def _live_app_help(app: typer.Typer) -> str | None:
    """Return the help Typer itself would render for *app* (read at registration).

    A single-command app that would flatten (see :func:`_typer_app_flattens`)
    IS its lone command, so that command's help/docstring is what a native
    ``add_typer`` would have shown. Everything else defers to Typer's own
    resolution chain (``solve_typer_info_help``: app ``help=``, callback
    ``help=``, callback docstring) so this never becomes a second opinion.
    """
    if _typer_app_flattens(app):
        cmd = app.registered_commands[0]
        if isinstance(cmd.help, str) and cmd.help:
            return cmd.help
        doc = inspect.getdoc(cmd.callback) if cmd.callback else None
        if doc:
            return doc
    return typer.main.solve_typer_info_help(TyperInfo(app)) or None


def command_without_completion(app: typer.Typer) -> Any:
    """Return ``typer.main.get_command(app)`` without Typer's completion options.

    Typer appends ``--install-completion`` and ``--show-completion`` as the
    LAST two params of the command of an app built with ``add_completion=True``
    (its default). Those belong to the root ``otto`` app alone, so a sub-app
    converted on its own drops them. Only a trailing pair with exactly those
    names, in Typer's order, goes: a command's own option that shares one
    name stays. (A command that itself declares both, last, in that order,
    would lose them; Typer publishes no way to tell its pair apart.)
    """
    cmd: Any = typer.main.get_command(app)
    if [p.name for p in cmd.params[-2:]] == _completion_param_names():
        del cmd.params[-2:]
    return cmd


@functools.cache
def _completion_param_names() -> list[str]:
    """Return the names of the completion pair Typer appends, in its order, as Typer builds it."""
    return [p.name or "" for p in typer.main.get_install_completion_arguments()]


def resolve_spec_command(spec: CommandSpec) -> Any:
    """Return the vendored-click command/group for *spec*, importing lazily.

    A ``"pkg.mod:attr"`` loader imports its module only now; a function loader
    is wrapped in a throwaway Typer (the ``expose._synthesize_command``
    pattern); a Typer app converts via Typer's own app→click converter.
    """
    loader = spec.loader
    if isinstance(loader, str):
        mod_name, _, attr = loader.partition(":")
        loader = getattr(importlib.import_module(mod_name), attr)
    if isinstance(loader, typer.Typer):
        # Mirror Typer's native flattening rule (see ``_typer_app_flattens``):
        # a single-command, callback-free, subgroup-free app becomes a bare
        # leaf under the spec's own name — exactly what ``add_typer`` produces
        # natively — so ``monitor`` keeps its documented flat ``--live``/
        # ``<source>`` CLI instead of gaining a spurious nested ``monitor``
        # subcommand.
        # Anything richer stays a group (callers branch on ``hasattr(.commands)``).
        if _typer_app_flattens(loader):
            # Suppress the sub-app's own ``--install/--show-completion`` params:
            # completion belongs to the root ``otto`` app, and base's flattened
            # ``monitor`` (from a name-less ``add_typer``) never carried them.
            leaf = command_without_completion(loader)
            leaf.name = spec.name
            return leaf
        converted: Any = typer.main.get_group(loader)
        converted.name = spec.name
        return converted
    tmp = typer.Typer()
    # No self-wrapping here: an async function loader is bridged through
    # run_command by the leaf-invoke wrapper (_wrap_invoke's coroutine-result
    # bridge) when the root dispatch wraps this resolved command — the same
    # contract a registered Typer app's plain ``async def`` leaves get. A
    # resolved-but-unwrapped command invoked outside otto's dispatch fails
    # loudly (un-awaited coroutine) instead of running without the policy.
    tmp.command(spec.name, help=spec.help)(prepare_command_target(loader))
    leaf_converted: Any = typer.main.get_command(tmp)
    return (
        leaf_converted.commands[spec.name]
        if hasattr(leaf_converted, "commands")
        else leaf_converted
    )


# ---------------------------------------------------------------------------
# The entry check: does a registered options class's ``verbs=`` entry name a
# command that accepts registered options?
# ---------------------------------------------------------------------------
#
# These functions locate commands and judge entries; they never decide whether
# a command marks, attaches or completes registered flags (that is
# otto.cli.invoke.leaf_accepts_options and otto.params.covering), so the guard
# against command-name literals in those deciders does not reach them.


def _close(word: str, names: "Iterable[str]") -> str:
    """``"; did you mean 'x'?"`` for the names close to *word*, else ``""``."""
    import difflib

    matches = difflib.get_close_matches(word, list(names))
    return f"; did you mean {', '.join(repr(m) for m in matches)}?" if matches else ""


def _hung_on_run() -> list[str]:
    """Return the names of the commands and groups hung on ``otto run`` itself.

    Read by the one walk of a Typer app's children (:func:`_typer_children`),
    so a sub-app added with no name offers its commands, as Typer merges them.
    """
    from .run import run_app

    return [child.name for child in _typer_children(run_app)]


def _run_entry_error(spec: CommandSpec, name: str) -> str | None:
    """Judge ``"run <name>"`` from the tables: it names a command, and an instruction accepts.

    An instruction is judged by the one reader over its handler, which is
    already loaded and whose markers its built leaf carries (a project
    instruction has none, so the registration's default answers); nothing
    is built. A command hung on ``otto run`` is judged with its group, when
    ``run`` is resolved.
    """
    from ..instruction_catalog import INSTRUCTIONS
    from .invoke import leaf_accepts_options

    hung = _hung_on_run()
    if name in hung:
        return None
    instruction = INSTRUCTIONS.find(name=name)
    if instruction is None:
        known = [*INSTRUCTIONS.names(), *hung]
        return f"otto run has no command {name!r}{_close(name, known)}"
    if leaf_accepts_options(spec, instruction.handler, beneath_group=True):
        return None
    return f"otto run {name} does not accept registered options"


def _accepts_beneath(spec: CommandSpec, command: Any) -> bool:
    """Whether *command* beneath *spec*'s group accepts: a leaf by the reader, a group by a leaf."""
    from .invoke import leaf_accepts_options

    children = getattr(command, "commands", None)
    if children is None:
        return leaf_accepts_options(spec, getattr(command, "callback", None), beneath_group=True)
    return any(_accepts_beneath(spec, child) for child in children.values())


def _beneath_group_error(spec: CommandSpec, name: str, group: Any) -> str | None:
    """Judge *name* beneath the resolved *group* of *spec*: it exists, and it accepts."""
    children = getattr(group, "commands", None)
    if children is None:
        return f"otto {spec.name} is a single command, with no command {name!r} beneath it"
    child = children.get(name)
    if child is None:
        if name in group.list_commands(typer.Context(group)):
            # A child the group builds only when asked (an instruction under
            # `run`, judged from its table above): building it here would
            # import project code.
            return None
        return f"otto {spec.name} has no command {name!r}{_close(name, children)}"
    if _accepts_beneath(spec, child):
        return None
    return f"otto {spec.name} {name} does not accept registered options"


def entry_error(
    registration: "Registration", entry: str, *, group_of: "Callable[[str], Any | None]"
) -> str | None:
    """Return the message for *entry* of *registration* when it is a bad entry, else ``None``.

    The one judgement of an entry, which the startup check, a group's
    resolution and the checker of a late registration all call:

    - the first word must be a registered command (a close match is
      suggested); an entry under a command closed to registered options
      (``otto.params.closed_to_options``) passes;
    - a one-word entry's registration must accept registered options
      (:func:`~otto.cli.invoke.spec_accepts_options`), judged without
      importing the command;
    - ``"run <name>"``: *name* must be an instruction or a command or group
      hung on ``otto run``, and an instruction must accept (its handler read
      by :func:`~otto.cli.invoke.leaf_accepts_options`, nothing built);
    - a two-word entry whose group *group_of* returns (the resolved group;
      ``None`` while it is not resolved) must name a command or sub-group
      beneath it; a command must accept
      (:func:`~otto.cli.invoke.leaf_accepts_options`), and so must at least
      one command under a sub-group. ``run`` is judged so too.

    It imports no command module but :mod:`otto.cli.run`, and that only for
    a ``"run <name>"`` entry. It locates commands; it does not decide whether
    a command marks, attaches or completes registered flags.
    """
    from ..params import _who, closed_to_options
    from .invoke import spec_accepts_options

    who = _who(registration.project)
    said = f"options class {registration.key!r}, registered for {entry!r} by {who}"
    words = entry.split()
    first = words[0]
    if first not in CLI_COMMANDS:
        return f"{said}: otto has no command {first!r}{_close(first, CLI_COMMANDS.names())}"
    if closed_to_options(first):
        return None
    spec = CLI_COMMANDS.peek(name=first)
    if len(words) == 1:
        if spec_accepts_options(spec):
            return None
        accepting = [
            name
            for name in CLI_COMMANDS.names()
            if spec_accepts_options(CLI_COMMANDS.peek(name=name))
        ]
        return (
            f"{said}: otto {first} does not accept registered options; "
            f"the commands that do: {', '.join(accepting)}"
        )
    name = words[1]
    from ..instruction_catalog import INSTRUCTION_GROUP

    if first == INSTRUCTION_GROUP:
        problem = _run_entry_error(spec, name)
        if problem is not None:
            return f"{said}: {problem}"
    group = group_of(first)
    if group is None:
        return None
    problem = _beneath_group_error(spec, name, group)
    return None if problem is None else f"{said}: {problem}"


@dataclass
class _Judged:
    """The entries judged for one bootstrap result, so a second check over it repeats nothing."""

    result: object = None
    """The bootstrap result these sets belong to, compared by identity."""
    pairs: "set[tuple[str, str, bool]]" = field(default_factory=set)
    """``(key, entry, at_resolution)`` for each entry judged: at startup, or with its group."""
    reported: "set[tuple[str, str]]" = field(default_factory=set)
    """``(key, entry)`` for each entry whose error is on the result."""


_JUDGED = _Judged()


def _judged_for(result: "BootstrapResult") -> _Judged:
    """Return the judged record for *result*, emptied when *result* is not the one last checked."""
    if _JUDGED.result is not result:
        _JUDGED.result = result
        _JUDGED.pairs.clear()
        _JUDGED.reported.clear()
    return _JUDGED


def _judge(
    result: "BootstrapResult",
    registration: "Registration",
    entry: str,
    *,
    group_of: "Callable[[str], Any | None]",
) -> None:
    """Append *entry*'s error, if any, to *result* as a load error of the registering project."""
    message = entry_error(registration, entry, group_of=group_of)
    if message is None:
        return
    from ..config.scope import BootstrapError
    from ..params import OptionsRegistrationError

    root = next((p.root for p in result.projects if p.name == registration.project), None)
    _JUDGED.reported.add((registration.key, entry))
    result.errors.append(
        BootstrapError(
            root, source="options", cause=OptionsRegistrationError(message), reason=message
        )
    )


def check_option_entries(result: "BootstrapResult") -> None:
    """Judge every registration's entries at startup, appending each error to *result*.

    Each entry is judged by :func:`entry_error` with no group resolved, so a
    two-word entry is held only to its first word (and a ``"run <name>"`` to
    its name). An error is a ``BootstrapError(source="options")`` attributed
    to the registering project's root (``None`` for none), so the startup
    printer and the dispatch gate judge it as any project load error.

    Once per bootstrap result: an entry already judged for *result* is not
    judged again, so a second ``entry()`` over the same result appends
    nothing twice and still judges a registration made since; another result
    starts again. It locates commands; it does not decide whether a command
    marks, attaches or completes registered flags.
    """
    from ..params import registrations

    judged = _judged_for(result)
    for registration in registrations():
        for entry in registration.entries:
            pair = (registration.key, entry, False)
            if pair in judged.pairs:
                continue
            judged.pairs.add(pair)
            _judge(result, registration, entry, group_of=lambda _: None)


def check_group_entries(result: "BootstrapResult", spec: CommandSpec, group: Any) -> None:
    """Judge the two-word entries under *spec*'s name against its resolved *group*.

    Each error is appended to *result* as :func:`check_option_entries`
    appends one; an entry already judged against the group for *result*, or
    one whose error is already on it, is not judged again. The group of a
    command closed to registered options (``otto.params.closed_to_options``)
    is not judged here. It locates commands; it does not decide whether a
    command marks, attaches or completes registered flags.
    """
    from ..params import closed_to_options, registrations

    if closed_to_options(spec.name):
        return

    judged = _judged_for(result)
    for registration in registrations():
        for entry in registration.entries:
            words = entry.split()
            if words[0] != spec.name or len(words) == 1:
                continue
            pair = (registration.key, entry, True)
            if pair in judged.pairs or (registration.key, entry) in judged.reported:
                continue
            judged.pairs.add(pair)
            _judge(result, registration, entry, group_of=lambda _: group)


def refuse_bad_entries(
    registration: "Registration", *, group_of: "Callable[[str], Any | None]"
) -> None:
    """Refuse a late registration when :func:`entry_error` finds a bad entry in it.

    The command line installs it (``otto.params.set_entry_checker``) once the
    startup check has run, so a registration made later is judged when it
    is made, by the same judgement.

    Raises:
        OptionsRegistrationError: the first message :func:`entry_error`
            gives for the registration's entries.
    """
    from ..params import OptionsRegistrationError

    for entry in registration.entries:
        message = entry_error(registration, entry, group_of=group_of)
        if message is not None:
            raise OptionsRegistrationError(message)


# ---------------------------------------------------------------------------
# Completion-cache walkers: the CLI's own command tree, serialised for the cache
# ---------------------------------------------------------------------------
#
# otto.config writes the completion cache but sits below the CLI and never
# imports it, so the CLI hands these walkers in (otto.cli.main's
# _completion_collectors). They are not public and stay out of __all__.


def collect_current_commands() -> list[dict[str, Any]]:
    """Read the currently-registered instructions with their own options.

    Must be called after :func:`otto.bootstrap.bootstrap` has finished
    populating ``otto.instruction_catalog.INSTRUCTIONS``. A source that never
    loaded simply has an empty registry (no init modules → no
    ``@instruction()`` ran → no entries). Test files are never read here:
    ``otto test`` has no per-test subcommands, and its flags are served from
    the command tree like any other leaf's.

    Each item is ``{"name": str, "options": list[dict], "accepts_options":
    bool}``: every parameter of the instruction's own command (its own
    options class's fields and its plain parameters; a project instruction
    has none) recorded by
    :func:`otto.config.completion_cache.serialize_options`, and the one
    reader's answer for it beneath ``otto run``
    (:func:`otto.cli.invoke.leaf_accepts_options`), which the fast path's
    stub carries. The flags of the classes covering it, a project
    instruction's body classes included, are the cache's
    ``option_candidates``, which its tree node names.
    """
    from ..config.completion_cache import serialize_options
    from ..instruction_catalog import INSTRUCTION_GROUP, INSTRUCTIONS
    from .invoke import leaf_accepts_options
    from .run import build_instruction_app

    spec = CLI_COMMANDS.peek(name=INSTRUCTION_GROUP)
    out: list[dict[str, Any]] = []
    for name, entry in INSTRUCTIONS.items():
        app = build_instruction_app(entry)
        callback = app.registered_commands[0].callback if app.registered_commands else None
        options = serialize_options(callback, command_name=name) if callback else []
        accepts = leaf_accepts_options(spec, callback, beneath_group=True)
        out.append({"name": name, "options": options, "accepts_options": accepts})
    return out


def collect_option_candidates() -> dict[str, Any]:
    """Describe every options class a command may offer, each on its own, for the cache.

    Enumerated from the two tables and from no command, by the one
    enumeration a command's offer reads too: first every project
    instruction (``otto.instruction_catalog.PROJECT_INSTRUCTIONS``, in table
    order) and a use for each of its bodies' classes
    (:func:`otto.project.commands.body_origins`) whose entry is the
    instruction's own command path (``otto.cli.invoke.body_uses``); then
    every registration (``otto.cli.invoke.registered_uses``, in registration
    order). Each is described on its own
    (``otto.cli.invoke.OptionsUse.candidate``), the description completion
    reads for one command too, and one that fails is left out.

    Returns ``{"rows": {key: [row, ...]}, "uses": [{"key", "project",
    "entries"}, ...]}``. Every occurrence is one use, in enumeration order,
    with its own project and entries: nothing is merged and no selection is
    read. A class's rows, one per parameter as a tree node records it
    (:func:`otto.config.completion_tree.param_rows`), are stored once, the
    first time its key is met.
    """
    import logging

    from ..config.completion_tree import param_rows
    from ..errors import is_containable
    from ..instruction_catalog import PROJECT_INSTRUCTIONS, instruction_path
    from ..project.commands import body_origins
    from .invoke import body_uses, registered_uses

    try:
        instructions = list(PROJECT_INSTRUCTIONS.items())
    except BaseException as e:
        if not is_containable(e):
            raise
        logging.getLogger(__name__).debug(f"completion-cache: no instruction bodies: {e!r}")
        instructions = []
    enumerated = [
        use
        for name, instruction in instructions
        for use in body_uses(instruction_path(name), body_origins(instruction))
    ]
    enumerated += registered_uses()
    rows: dict[str, list[dict[str, Any]]] = {}
    uses: list[dict[str, Any]] = []
    for candidate in (use.candidate() for use in enumerated):
        if candidate is None:
            continue
        uses.append(
            {"key": candidate.key, "project": candidate.project, "entries": list(candidate.entries)}
        )
        if candidate.key not in rows:
            rows[candidate.key] = list(param_rows(candidate.params).values())
    return {"rows": rows, "uses": uses}


def _typer_command_name(info: Any) -> str:
    """Return the name a command registered on a Typer app goes by, as Typer names it."""
    return info.name or typer.main.get_command_name(info.callback.__name__)


def _typer_group_name(info: Any) -> str | None:
    """Return the name a sub-app added to a Typer app goes by, or ``None`` when it has none.

    Typer's own answer (``typer.main.solve_typer_info_defaults``, which
    ``typer.main.get_group`` builds the group from): the ``add_typer`` call's
    name first, then the sub-app's registered ``@callback()``'s, then
    ``typer.Typer(name=...)``'s. An empty name is none: Typer merges that
    sub-app's commands into its parent's.
    """
    return typer.main.solve_typer_info_defaults(info).name or None


def _group_callback(sub: typer.Typer, info: Any = None) -> "Callable[..., Any] | None":
    """Return the callback of the group Typer builds from *sub* (added with *info*), if any.

    Typer takes it from the ``add_typer`` call first, then ``sub``'s
    ``@callback()``, then ``typer.Typer(callback=...)``; an unset one is a
    placeholder, not a callable.
    """
    registered = sub.registered_callback
    found = (
        getattr(info, "callback", None),
        None if registered is None else registered.callback,
        sub.info.callback,
    )
    return next((callback for callback in found if callable(callback)), None)


@dataclass(frozen=True)
class _TyperChild:
    """One child of a Typer app, as ``typer.main.get_group`` builds it."""

    name: str
    """The name it goes by beneath the app."""
    info: Any
    """A command's ``CommandInfo``, or a named sub-app's ``TyperInfo``."""
    is_group: bool
    """Whether it is a named sub-app, which Typer keeps a group whatever it holds."""


def _typer_children(app: typer.Typer) -> list[_TyperChild]:
    """Return the children of *app*, in order, as Typer builds its group.

    The one walk of a Typer app's registrations: its commands first, then
    each sub-app it added, a named one (:func:`_typer_group_name`) as a
    group and one with no name as its own children merged in its place, as
    ``typer.main.get_group`` merges them. The entry check (what is hung on
    ``otto run``) and the completion cache's children read it, so they name
    what the built group lists.
    """
    children = [
        _TyperChild(_typer_command_name(info), info, is_group=False)
        for info in app.registered_commands
    ]
    for info in app.registered_groups:
        sub = info.typer_instance
        if sub is None:
            continue
        name = _typer_group_name(info)
        if name is None:
            children.extend(_typer_children(sub))
        else:
            children.append(_TyperChild(name, info, is_group=True))
    return children


def _serialize_cli_children(app: Any, spec: CommandSpec) -> list[dict[str, Any]]:
    """Serialize a third-party Typer group's children for the cache.

    Children reuse the instruction/suite option schema (rebuilt by
    :func:`otto.config.completion_stubs.build_stub_command` on the fast
    path), every parameter recorded, arguments included. Each child records
    ``accepts_options``, the answer for it beneath *spec*'s group: a leaf
    by :func:`otto.cli.invoke.leaf_accepts_options`, a group by whether a
    leaf beneath it accepts (``_accepts_beneath``). Nested groups
    recurse and record their own parameters as ``options`` beside their
    ``commands``. The children are the one walk's (:func:`_typer_children`):
    a sub-app added with a name stays a group whatever it holds (Typer
    flattens only the top-level app, ``_typer_app_flattens``); one added
    with no name has its commands merged into its parent's, as Typer merges
    them.
    """
    from ..config.completion_cache import serialize_options
    from .invoke import leaf_accepts_options

    children: list[dict[str, Any]] = []
    for child in _typer_children(app):
        info = child.info
        if not child.is_group:
            children.append(
                {
                    "name": child.name,
                    "help": info.help or inspect.getdoc(info.callback) or "",
                    "options": serialize_options(info.callback, command_name=child.name),
                    "accepts_options": leaf_accepts_options(
                        spec, info.callback, beneath_group=True
                    ),
                }
            )
            continue
        sub = info.typer_instance
        ghelp = next((h for h in (info.help, sub.info.help) if isinstance(h, str)), "")
        callback = _group_callback(sub, info)
        children.append(
            {
                "name": child.name,
                "help": ghelp,
                "commands": _serialize_cli_children(sub, spec),
                "options": serialize_options(callback, command_name=child.name) if callback else [],
                "accepts_options": _accepts_beneath(spec, typer.main.get_group(sub)),
            }
        )
    return children


def collect_cli_commands() -> list[dict[str, Any]]:
    """Snapshot third-party top-level CLI commands for the completion cache.

    Reads the live :data:`otto.cli.registry.CLI_COMMANDS` registry and
    returns one ``{"name", "help", "lab_free", "accepts_options"}`` dict per
    entry whose
    registering module (``CLI_COMMANDS.origin(name)``) is *not* under
    ``otto.`` — built-in commands re-register
    on every real invocation (bootstrap always runs), so caching them would
    be redundant and risks masking a genuine removal. Third-party commands,
    by contrast, only exist in the registry after a plugin's init module has
    executed, which the completion fast path deliberately skips; caching
    their name/help/``lab_free`` here is what lets them still tab-complete.

    ``accepts_options`` is the one reader's answer for the command
    (:func:`otto.cli.invoke.leaf_accepts_options`). A GROUP entry
    additionally carries ``"commands"`` (recursive child metadata) and its
    own parameters as ``"options"``; a flattening single-command app and a
    function loader carry their command's ``"options"`` — each omitted when
    empty. Serializing children may import a lazy
    ``"pkg.mod:attr"`` loader's module: a slow-path-only, once-per-cache-
    refresh cost, contained per command (a broken loader degrades that entry
    to name+help and real dispatch still reports the import error loudly).
    """
    import logging

    from ..config.completion_cache import serialize_options
    from ..errors import is_containable
    from .invoke import leaf_accepts_options

    log = logging.getLogger(__name__)
    out: list[dict[str, Any]] = []
    for name, spec in CLI_COMMANDS.items():
        if CLI_COMMANDS.origin(name=name).startswith("otto."):
            continue
        entry: dict[str, Any] = {
            "name": name,
            "help": spec.help,
            "lab_free": spec.lab_free,
            "accepts_options": leaf_accepts_options(spec, None, beneath_group=False),
        }
        try:
            loader = spec.loader
            if isinstance(loader, str):
                mod_name, _, attr = loader.partition(":")
                loader = getattr(importlib.import_module(mod_name), attr)
            options: list[dict[str, Any]] = []
            if isinstance(loader, typer.Typer):
                if _typer_app_flattens(loader):
                    cmd_info = loader.registered_commands[0]
                    options = serialize_options(cmd_info.callback, command_name=spec.name)
                else:
                    commands = _serialize_cli_children(loader, spec)
                    if commands:
                        entry["commands"] = commands
                    callback = _group_callback(loader)
                    if callback is not None:
                        options = serialize_options(callback, command_name=spec.name)
            elif callable(loader):
                options = serialize_options(loader, command_name=spec.name)
            if options:
                entry["options"] = options
        # Containment seam: the cache stays name-only, dispatch reports loudly.
        #
        # BaseException, not Exception: this imports a THIRD-PARTY loader module,
        # so a module-level `pytest.importorskip` there raises `Skipped` — not an
        # `Exception` — straight past this seam and out of `entry()`, which
        # reaches `collect_cli_commands()` as a call ARGUMENT, so the
        # `suppress(OSError)` around the cache write never sees it. That
        # tracebacks out of every cache rebuild — root `otto --help` included —
        # and into the shell mid-TAB. See `otto.errors.UNCONTAINABLE`.
        except BaseException as e:
            if not is_containable(e):
                raise
            log.debug(f"completion-cache: no child metadata for {spec.name!r}: {e!r}")
        out.append(entry)
    return out


# otto's own top-level commands, registered beside the registry that holds them
# like every registry's built-ins: importing this module lists all fifteen,
# whether or not anything has imported the command tree (otto.cli.main). Each
# loader is a "module:attr" string, so listing them imports no verb module.
register_builtin_commands()
