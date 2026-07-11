"""Dynamic, class-scoped exposure of ``@cli_exposed`` host methods as ``otto host`` subcommands.

The :class:`HostGroup` (a ``typer.core.TyperGroup``) synthesizes one command per
exposed coroutine method across every registered host class — built-in and
project-registered alike — and filters the visible/dispatchable set to the verbs
defined on the *resolved* host's class (from ``ctx.params['host_id']``). A project
that registers ``MyHost`` with a ``@cli_exposed`` method gets ``otto host <id> <verb>``
with no extra wiring (the same first/third-party symmetry otto's own verbs use).
"""

import inspect
from collections.abc import Callable, Iterable
from typing import TYPE_CHECKING, Any

import typer
from typing_extensions import override

if TYPE_CHECKING:
    from typer.core import TyperGroup


def collect_exposed_methods(cls: type) -> dict[str, str]:
    """Return ``{cli_name: python_attr_name}`` for *cls*'s ``@cli_exposed`` coroutine methods.

    As resolved on *cls* — overrides that drop the marker are
    excluded, which is how per-class scoping by definedness falls out.
    """
    out: dict[str, str] = {}
    for attr_name, fn in inspect.getmembers(cls, predicate=inspect.iscoroutinefunction):
        if getattr(fn, "__cli_exposed__", False):
            out[getattr(fn, "__cli_name__", attr_name)] = attr_name
    return out


def _render_result(result: Any, success: str | None = None) -> None:
    """Render a host-verb result and signal failure via exit code.

    First-party verbs return the ``otto.result`` family (exit code comes from
    ``result.exit_code``); ``None`` means side-effect-only success. Any other
    value is the documented third-party fallback: printed as-is, exit 0.
    """
    from rich import print as rprint

    from otto.result import CommandResult, Result, Results

    if isinstance(result, Result):
        is_command = isinstance(result, (CommandResult, Results))
        if result.is_ok:
            if is_command:
                pass  # command output already streamed during execution
            elif success:
                rprint(f"[green]{success}[/green]")
            elif isinstance(result.value, dict):
                for src, entry in result.value.items():
                    rprint(f"{src} -> {entry.value}")
            elif isinstance(result.value, list):
                for item in result.value:
                    rprint(item)
            elif result.value is not None:
                rprint(result.value)
            return
        if result.msg:
            rprint(f"[red]{result.msg}[/red]")
        if isinstance(result.value, dict):
            for entry in result.value.values():
                if isinstance(entry, Result) and not entry.is_ok and entry.msg:
                    rprint(f"[red]{entry.msg}[/red]")
        elif isinstance(result, Results):
            for entry in result:
                if not entry.is_ok and entry.msg:
                    rprint(f"[red]{entry.msg}[/red]")
        raise typer.Exit(result.exit_code)

    if result is None:
        rprint(f"[green]{success}[/green]" if success else "[green]done[/green]")
        return

    rprint(result)  # documented third-party plain-value fallback, exit 0


def make_method_command(
    attr_name: str, sample_func: Callable[..., Any], cli_name: str | None = None
) -> Callable[..., Any]:
    """Build the async Typer command body dispatching to ``host.<attr_name>``.

    *sample_func* is the unbound method used to derive the CLI signature
    (via :func:`~otto.cli.param_synth.build_cli_binding`); the bound method on the
    resolved host is what actually runs.

    *cli_name* is the verb as the user types it (e.g. ``"login"``).  When
    omitted it falls back to *attr_name* so callers that only know the Python
    name still produce a useful message.
    """
    from rich import print as rprint

    from .param_synth import build_cli_binding

    binding = build_cli_binding(sample_func)
    verb = cli_name if cli_name is not None else attr_name

    async def _cmd(ctx: typer.Context, **kw: Any) -> None:
        from .host import resolve_cli_host

        host = resolve_cli_host(ctx)
        method = getattr(host, attr_name, None)
        if method is None or not callable(method):
            rprint(
                f"[red]Error:[/red] host {getattr(host, 'id', '?')!r} does not support {verb!r}."
            )
            raise typer.Exit(1)
        call_kw = dict(binding.excluded)
        for name, value in kw.items():
            conv = binding.converters.get(name)
            call_kw[name] = conv(value) if conv is not None else value
        # Filter only excluded-default keys to the params the concrete method accepts;
        # CLI-sourced keys (kw) are always kept so unexpected ones raise a loud TypeError.
        # The binding is built from the first-registered sample_func; a different
        # host class may implement the same verb without some internal params
        # (e.g. DockerContainerHost.put has no show_progress).
        try:
            method_sig = inspect.signature(method)
        except (ValueError, TypeError):
            method_sig = None
        if method_sig is not None and any(
            p.kind == inspect.Parameter.VAR_KEYWORD for p in method_sig.parameters.values()
        ):
            pass  # **kwargs — forward everything
        elif method_sig is not None:
            accepted = set(method_sig.parameters)
            call_kw = {k: v for k, v in call_kw.items() if k in kw or k in accepted}
        success = getattr(method, "__cli_success__", None)
        try:
            result = await method(**call_kw)
        except NotImplementedError as e:
            rprint(
                f"[red]Error:[/red] host {getattr(host, 'id', '?')!r} does not "
                f"support {verb!r}: {e}"
            )
            raise typer.Exit(1) from None
        finally:
            await host.close()
        _render_result(result, success)

    ctx_param = inspect.Parameter(
        "ctx", inspect.Parameter.POSITIONAL_OR_KEYWORD, annotation=typer.Context
    )
    _cmd.__signature__ = inspect.Signature(  # ty: ignore[unresolved-attribute]
        [
            ctx_param,
            *(p.replace(kind=inspect.Parameter.KEYWORD_ONLY) for p in binding.params),
        ]
    )
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
        from ..config import get_host

        return type(get_host(host_id))
    except Exception:  # noqa: BLE001 — completion fallback: no lab loaded / unknown id → return None for full menu
        return None


def exposed_cli_names(cls: type | None) -> set[str]:
    """Return the set of ``@cli_exposed`` cli-names defined on *cls* (empty for ``None``)."""
    return set(collect_exposed_methods(cls)) if cls is not None else set()


def iter_exposed_verbs() -> Iterable[tuple[str, str, str, Callable[..., Any]]]:
    """Yield ``(cli_name, attr_name, help, sample_func)`` across all registered host classes.

    First registration of a cli-name wins; help comes from ``__cli_help__`` or the
    method docstring's first line.  ``sample_func`` is the unbound method used to
    derive the CLI signature via :func:`~otto.cli.param_synth.build_cli_binding`.
    """
    from ..host.os_profile import HOST_CLASSES

    # First-wins per cli_name assumes a consistent attr_name for a given cli_name across
    # classes (true for inherited verbs; only divergent if two classes use the same
    # explicit name= for different attrs — avoid that).
    seen: set[str] = set()
    for _name, cls in HOST_CLASSES.items():  # noqa: PERF102 — Registry has no .values(), only .items()
        for cli_name, attr_name in collect_exposed_methods(cls).items():
            if cli_name in seen:
                continue
            seen.add(cli_name)
            fn = inspect.getattr_static(cls, attr_name, None) or getattr(cls, attr_name)
            help_text = (
                getattr(fn, "__cli_help__", None)
                or ((fn.__doc__ or "").strip().splitlines() or [""])[0]
            )
            yield cli_name, attr_name, help_text, fn


def _synthesize_command(
    cli_name: str, attr_name: str, help_text: str, sample_func: Callable[..., Any]
) -> Any:
    """Build a vendored-click ``Command`` for *cli_name* via a throwaway Typer.

    The Typer-native way to convert a function — no hand-written click types.
    """
    from ..utils import async_typer_command

    cmd_fn = make_method_command(attr_name, sample_func, cli_name)
    # Propagate the verb's per-invocation output-dir preference onto the command
    # callback so the leaf-invoke preamble (which reads `__cli_output_dir__` off
    # `ctx.command.callback`) honours read-only verbs (exists/ls/…) that opt out.
    # functools.wraps in async_typer_command carries the marker through, but set
    # it on cmd_fn BEFORE wrapping so the wrapper inherits it.
    cmd_fn.__cli_output_dir__ = getattr(sample_func, "__cli_output_dir__", True)  # ty: ignore[unresolved-attribute]
    tmp = typer.Typer()
    tmp.command(name=cli_name, help=help_text or None)(async_typer_command(cmd_fn))
    converted: Any = typer.main.get_command(tmp)
    return converted.commands[cli_name] if hasattr(converted, "commands") else converted


def _make_host_group() -> "type[TyperGroup]":
    """Build the ``HostGroup`` class lazily (defers ``TyperGroup`` subclassing)."""
    from typer.core import TyperGroup

    class HostGroup(TyperGroup):
        """``otto host`` group: lazily synthesizes dynamic verb commands.

        Scopes the visible/dispatchable set to the resolved host's class.
        """

        _dynamic_names: set[str]

        def _ensure_dynamic(self) -> None:
            if not hasattr(self, "_dynamic_names"):
                self._dynamic_names = set()
            for cli_name, attr_name, help_text, sample_func in iter_exposed_verbs():
                if cli_name in self.commands:
                    continue
                self.add_command(
                    _synthesize_command(cli_name, attr_name, help_text, sample_func), cli_name
                )
                self._dynamic_names.add(cli_name)

        def _class_for(self, ctx: Any) -> type | None:
            # During shell completion (``resilient_parsing``) skip resolving the
            # host's class: ``host_class_for_id`` calls ``get_host``, which loads the
            # lab and constructs the host just to scope the menu. Returning ``None``
            # offers the full unscoped verb list — correct for completion — without
            # paying that cost. Verbs are synthesized live either way, so nothing
            # goes stale.
            if getattr(ctx, "resilient_parsing", False):
                return None
            # No host id to scope by (e.g. `otto host --help`, `otto host <TAB>`):
            # skip the lab probe entirely. Probing with no id can only ever return
            # None anyway, and doing so on a help path used to trigger a full lab
            # load (with OTTO_LAB set) or spam the "Missing option '--lab'" message
            # once per probe. Full unscoped menu, zero lab work.
            host_id = (ctx.params or {}).get("host_id")
            if not host_id:
                return None
            # Real dispatch with an id: the lab loads lazily (leaf-invoke preamble),
            # which runs AFTER this parse-time scoping. Ensure it here as a soft
            # probe so ``host_class_for_id`` → ``get_host`` can resolve the concrete
            # class. A failed probe (no --lab / broken backend) is harmless: the
            # call below then returns None (full menu), and the leaf raises its own
            # clean error.
            from .invoke import try_ensure_lab

            try_ensure_lab(ctx)
            return host_class_for_id(host_id)

        @override
        def list_commands(self, ctx: Any) -> list[str]:
            self._ensure_dynamic()
            cls = self._class_for(ctx)
            allowed = exposed_cli_names(cls) if cls is not None else self._dynamic_names
            return [
                n
                for n in super().list_commands(ctx)
                if n not in self._dynamic_names or n in allowed
            ]

        def _class_command(self, cls: type, cmd_name: str, attr_name: str) -> Any:
            """Build (and cache) the verb's command from *cls*'s own method.

            A verb name shared across classes can carry a different signature per
            class. Cached per ``(cls, cmd_name)``.
            """
            cache = getattr(self, "_class_cmd_cache", None)
            if cache is None:
                cache = self._class_cmd_cache = {}
            key = (cls, cmd_name)
            if key not in cache:
                fn = inspect.getattr_static(cls, attr_name, None) or getattr(cls, attr_name)
                help_text = (
                    getattr(fn, "__cli_help__", None)
                    or ((fn.__doc__ or "").strip().splitlines() or [""])[0]
                )
                cache[key] = _synthesize_command(cmd_name, attr_name, help_text, fn)
            return cache[key]

        @override
        def get_command(self, ctx: Any, cmd_name: str) -> Any:
            self._ensure_dynamic()
            cls = self._class_for(ctx)
            if cls is None:
                # Completion / unresolved host → the unscoped global command.
                return super().get_command(ctx, cmd_name)
            verbs = collect_exposed_methods(cls)
            if cmd_name in self._dynamic_names and cmd_name not in verbs:
                return None  # dynamic verb not exposed on this host class
            if cmd_name in verbs:
                return self._class_command(cls, cmd_name, verbs[cmd_name])
            return super().get_command(ctx, cmd_name)  # static (non-dynamic) commands

    return HostGroup


HostGroup = _make_host_group()
