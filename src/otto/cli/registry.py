"""The CLI command registry: how commands — first- and third-party — join ``otto``.

A :class:`CommandSpec` describes one top-level command or group: its name, a
loader (a live Typer app, a plain/async function, or a lazy ``"pkg.mod:attr"``
string imported only on dispatch), the help line shown by ``otto --help``
*without* importing the module, and dispatch metadata (``lab_free``,
``output_dir``, ``gate``). First-party subcommands and third-party plugins
register through the same :func:`register_cli_command`.
"""

import importlib
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

import typer

from ..registry import Registry, caller_module
from ..utils import async_typer_command
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
) -> None:
    """Register a top-level ``otto`` command or group.

    *loader* is a ``typer.Typer`` app (group), a plain/async function (leaf
    command), or a ``"pkg.mod:attr"`` string resolved lazily on dispatch.
    Name collisions raise immediately, naming both registering modules —
    there is deliberately no overwrite escape hatch for CLI commands.
    """
    origin = caller_module()
    spec = CommandSpec(
        name=name,
        loader=loader,
        help=help,
        lab_free=lab_free,
        output_dir=output_dir,
        gate=gate,
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
) -> Callable[..., Any]:
    """Register an async function as a top-level ``otto`` command.

    The ergonomics match ``@instruction``: an ``OttoContext``-annotated
    parameter is injected (hidden from the CLI), and ``options=`` expands a
    pydantic-dataclass into flags. The command name defaults to the function
    name with underscores dashed.
    """

    def decorator(func: Callable[..., Any]) -> Callable[..., Any]:
        target = prepare_command_target(func, options)
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
        )
        return func

    return decorator


def _typer_app_flattens(app: typer.Typer) -> bool:
    """Whether Typer's native ``get_command`` would flatten *app* to a bare leaf.

    Mirrors the predicate inside ``typer.main.get_command``: an app with no
    callback (root or ``info``), no sub-groups, and exactly one registered
    command collapses into that single command rather than a group. ``monitor``
    (one ``@monitor_app.command()``, no callback) is the motivating case — its
    documented flat CLI (``otto monitor --file …``) depends on this.
    """
    return not (
        app.registered_callback
        or app.info.callback
        or app.registered_groups
        or len(app.registered_commands) != 1
    )


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
        # natively — so ``monitor`` keeps its documented flat ``--file`` CLI
        # instead of gaining a spurious nested ``monitor`` subcommand.
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
    tmp.command(spec.name, help=spec.help)(async_typer_command(prepare_command_target(loader)))
    leaf_converted: Any = typer.main.get_command(tmp)
    return (
        leaf_converted.commands[spec.name]
        if hasattr(leaf_converted, "commands")
        else leaf_converted
    )
