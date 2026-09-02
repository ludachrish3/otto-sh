"""The CLI command registry: how commands — first- and third-party — join ``otto``.

A :class:`CommandSpec` describes one top-level command or group: its name, a
loader (a live Typer app, a plain/async function, or a lazy ``"pkg.mod:attr"``
string imported only on dispatch), the help line shown by ``otto --help``
*without* importing the module, and dispatch metadata (``lab_free``,
``output_dir``, ``gate``, ``dry_run_preview``). First-party subcommands and
third-party plugins register through the same :func:`register_cli_command`.
"""

import importlib
import inspect
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

import typer
from typer.models import TyperInfo

from ..registry import Registry, caller_module
from .invoke import prepare_command_target


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

    NOT set for ``test``: every ``otto test <Suite>`` leaf is sync, because
    ``pytest.main`` is."""

    origin: str = ""
    """Module that registered the command (auto-captured) — used in collisions."""


CLI_COMMANDS: Registry[CommandSpec] = Registry(
    "CLI command",
    register_hint="otto.register_cli_command()",
    # register_cli_command has no overwrite parameter — the default
    # "Pass overwrite=True…" sentence would point at a knob that does not exist.
    collision_hint="CLI command names cannot be overwritten; pick a unique name.",
)
"""Every registered top-level ``otto`` command or group, keyed by CLI name."""


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
    origin: str | None = None,
) -> None:
    """Register a top-level ``otto`` command or group.

    *loader* is a ``typer.Typer`` app (group), a plain/async function (leaf
    command), or a ``"pkg.mod:attr"`` string resolved lazily on dispatch.
    Name collisions raise immediately, naming both registering modules —
    there is deliberately no overwrite escape hatch for CLI commands.

    *dry_run_preview* opts every leaf under this command out of the
    ``--dry-run`` seam default (see :attr:`CommandSpec.dry_run_preview`).

    *origin* names the registering module; ``None`` (the default) captures
    the caller's, which is right for every direct call. A WRAPPER that
    registers on someone else's behalf must pass the real registrant — the
    same seam :meth:`Registry.register <otto.registry.Registry.register>`
    exposes, and for the same reason: ``@cli_command`` registers from inside
    this module, and a frame-captured origin of ``otto.cli.registry`` made
    the completion cache classify every decorated third-party leaf as a
    BUILT-IN and silently drop it from warm root help.
    """
    if help is None and isinstance(loader, typer.Typer):
        # A live app already carries its Typer-native help — read it once here
        # so the spec (the single source of truth for root help AND the
        # completion cache) inherits it instead of rendering a placeholder.
        # Lazy "pkg.mod:attr" loaders have nothing to read without importing;
        # that is what explicit help= is for.
        help = _live_app_help(loader)  # noqa: A001 — mirrors typer's own `help=` keyword
    if origin is None:
        origin = caller_module()
    spec = CommandSpec(
        name=name,
        loader=loader,
        help=help,
        lab_free=lab_free,
        output_dir=output_dir,
        gate=gate,
        async_leaves=async_leaves,
        dry_run_preview=dry_run_preview,
        origin=origin,
    )
    CLI_COMMANDS.register(name, spec, origin=origin)


def cli_command(
    *,
    options: type | None = None,
    name: str | None = None,
    help: str | None = None,  # noqa: A002 — mirrors typer's own `help=` keyword
    lab_free: bool = False,
    output_dir: bool = True,
    gate: bool = True,
    dry_run_preview: bool = False,
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
    interruptible than a sync one (see :func:`~otto.cli.run.instruction`).
    """

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
        target = prepare_command_target(func, options)
        cmd_name = name or getattr(func, "__name__", repr(func)).replace("_", "-")
        doc_line = ((func.__doc__ or "").strip().splitlines() or [""])[0]
        # Register the prepared callable itself — resolve_spec_command's
        # function-loader branch wraps it in a throwaway Typer on dispatch
        # (same as expose._synthesize_command), so it always resolves to a
        # leaf command, never a same-named nested group.
        #
        # origin= is the module APPLYING the decorator, captured here because
        # the register_cli_command call below runs in THIS module's frame — a
        # frame-captured origin would read "otto.cli.registry", the cache
        # collector would classify a third-party leaf as a built-in, and warm
        # root help would silently drop it (while direct registrations, whose
        # frame IS the plugin module, survived).
        register_cli_command(
            cmd_name,
            target,
            help=help or (doc_line or None),
            lab_free=lab_free,
            output_dir=output_dir,
            gate=gate,
            dry_run_preview=dry_run_preview,
            origin=caller_module(),
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
            add_completion = loader._add_completion  # noqa: SLF001 — Typer flag we toggle for conversion
            loader._add_completion = False  # noqa: SLF001
            try:
                leaf: Any = typer.main.get_command(loader)
            finally:
                loader._add_completion = add_completion  # noqa: SLF001
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
