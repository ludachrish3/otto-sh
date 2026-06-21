"""
Dynamic, class-scoped exposure of ``@cli_exposed`` host methods as ``otto host``
subcommands.

The :class:`HostGroup` (a ``typer.core.TyperGroup``) synthesizes one command per
exposed coroutine method across every registered host class — built-in and
project-registered alike — and filters the visible/dispatchable set to the verbs
defined on the *resolved* host's class (from ``ctx.params['host_id']``). A project
that registers ``MyHost`` with a ``@cli_exposed`` method gets ``otto host <id> <verb>``
with no extra wiring (the same first/third-party symmetry otto's own verbs use).
"""
from __future__ import annotations

import inspect
from collections.abc import Iterable
from pathlib import Path
from typing import TYPE_CHECKING, Annotated, Any, Callable, Optional

import typer

if TYPE_CHECKING:
    from typer.core import TyperGroup


def collect_exposed_methods(cls: type) -> dict[str, str]:
    """Return ``{cli_name: python_attr_name}`` for *cls*'s ``@cli_exposed``
    coroutine methods (as resolved on *cls* — overrides that drop the marker are
    excluded, which is how per-class scoping by definedness falls out).
    """
    out: dict[str, str] = {}
    for attr_name, fn in inspect.getmembers(cls, predicate=inspect.iscoroutinefunction):
        if getattr(fn, "__cli_exposed__", False):
            out[getattr(fn, "__cli_name__", attr_name)] = attr_name
    return out


def _coerce(value: str, annotation: Any) -> Any:
    if annotation is bool:
        return value.lower() in ("1", "true", "yes", "on")
    if annotation is int:
        return int(value)
    if annotation is Path:
        return Path(value)
    return value


def bind_cli_args(method: Callable, raw_args: list[str]) -> list:  # ty: ignore[missing-type-argument]
    """Coerce *raw_args* positionally against *method*'s parameter annotations.

    *method* is the **bound** method (``self`` already excluded by binding).
    """
    params = list(inspect.signature(method).parameters.values())
    return [_coerce(value, p.annotation) for value, p in zip(raw_args, params)]


def _render_result(result: Any) -> None:
    """Render a host-method result and signal failure via exit code.

    The lifecycle/file-op verbs return ``tuple[Status, str]``; query verbs return
    ``bool``/``list``/``str``. Only a failing ``(Status, str)`` tuple is an error.
    """
    import typer
    from rich import print as rprint

    if isinstance(result, tuple) and len(result) == 2 and hasattr(result[0], "is_ok"):
        status, msg = result
        if msg:
            rprint(msg)
        if not status.is_ok:
            raise typer.Exit(1)
        return
    if result is None:
        rprint("[green]done[/green]")
    else:
        rprint(result)


def make_method_command(attr_name: str) -> Callable:  # ty: ignore[missing-type-argument]
    """Build the async Typer command body dispatching to ``host.<attr_name>``."""
    import typer
    from rich import print as rprint

    async def _cmd(
        ctx: typer.Context,
        args: Annotated[Optional[list[str]], typer.Argument(help="Positional arguments for the host method.")] = None,
    ) -> None:
        host = ctx.obj
        method = getattr(host, attr_name, None)
        if method is None or not callable(method):
            rprint(
                f"[red]Error:[/red] host {getattr(host, 'id', '?')!r} does not "
                f"support {attr_name!r}."
            )
            raise typer.Exit(1)
        try:
            result = await method(*bind_cli_args(method, args or []))
        finally:
            await host.close()
        _render_result(result)

    return _cmd


def host_class_for_id(host_id: str | None) -> type | None:
    """Resolve a host ID to its concrete host class, or ``None``.

    Runs during arg parsing / completion (before the group callback populates
    ``ctx.obj``). Calls :func:`get_host`, which builds the host on first access and
    returns the cached instance thereafter — cheap on repeat, but the first call per
    host does build it. Any failure (no lab loaded, unknown id) yields ``None`` → the
    full (unscoped) menu, and the callback then raises its own clean error.
    """
    if not host_id:
        return None
    try:
        from ..configmodule import get_host

        return type(get_host(host_id))
    except Exception:
        return None


def exposed_cli_names(cls: type | None) -> set[str]:
    """Return the set of ``@cli_exposed`` cli-names defined on *cls* (empty for ``None``)."""
    return set(collect_exposed_methods(cls)) if cls is not None else set()


def iter_exposed_verbs() -> Iterable[tuple[str, str, str]]:
    """Yield ``(cli_name, attr_name, help)`` across all registered host classes.

    First registration of a cli-name wins; help comes from ``__cli_help__`` or the
    method docstring's first line.
    """
    from ..host.os_profile import _HOST_CLASSES

    # First-wins per cli_name assumes a consistent attr_name for a given cli_name across
    # classes (true for inherited verbs; only divergent if two classes use the same
    # explicit name= for different attrs — avoid that).
    seen: set[str] = set()
    for cls in _HOST_CLASSES.values():
        for cli_name, attr_name in collect_exposed_methods(cls).items():
            if cli_name in seen:
                continue
            seen.add(cli_name)
            fn = inspect.getattr_static(cls, attr_name, None) or getattr(cls, attr_name)
            help_text = getattr(fn, "__cli_help__", None) or (
                (fn.__doc__ or "").strip().splitlines() or [""]
            )[0]
            yield cli_name, attr_name, help_text


def _synthesize_command(cli_name: str, attr_name: str, help_text: str) -> Any:
    """Build a vendored-click ``Command`` for *cli_name* via a throwaway Typer
    (the Typer-native way to convert a function — no hand-written click types).
    """
    from ..utils import async_typer_command  # noqa: PLC0415

    cmd_fn = make_method_command(attr_name)
    tmp = typer.Typer()
    tmp.command(name=cli_name, help=help_text or None)(async_typer_command(cmd_fn))
    converted: Any = typer.main.get_command(tmp)
    return converted.commands[cli_name] if hasattr(converted, "commands") else converted


def _make_host_group() -> type[TyperGroup]:
    """Build the ``HostGroup`` class lazily (defers ``TyperGroup`` subclassing)."""
    from typer.core import TyperGroup

    class HostGroup(TyperGroup):
        """``otto host`` group: lazily synthesizes dynamic verb commands and scopes
        the visible/dispatchable set to the resolved host's class.
        """

        _dynamic_names: set[str]

        def _ensure_dynamic(self) -> None:
            if not hasattr(self, "_dynamic_names"):
                self._dynamic_names = set()
            for cli_name, attr_name, help_text in iter_exposed_verbs():
                if cli_name in self.commands:
                    continue
                self.add_command(_synthesize_command(cli_name, attr_name, help_text), cli_name)
                self._dynamic_names.add(cli_name)

        def _class_for(self, ctx: Any) -> type | None:
            return host_class_for_id((ctx.params or {}).get("host_id"))

        def list_commands(self, ctx: Any) -> list[str]:
            self._ensure_dynamic()
            cls = self._class_for(ctx)
            allowed = exposed_cli_names(cls) if cls is not None else self._dynamic_names
            return [
                n
                for n in super().list_commands(ctx)
                if n not in self._dynamic_names or n in allowed
            ]

        def get_command(self, ctx: Any, cmd_name: str) -> Any:
            self._ensure_dynamic()
            cls = self._class_for(ctx)
            if cls is not None and cmd_name in self._dynamic_names and cmd_name not in exposed_cli_names(cls):
                return None
            return super().get_command(ctx, cmd_name)

    return HostGroup


HostGroup = _make_host_group()
