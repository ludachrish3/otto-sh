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

from .invoke import RENDER_POLICY_KEY, RenderPolicy, fail

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
    from .param_synth import build_cli_binding

    binding = build_cli_binding(sample_func)
    verb = cli_name if cli_name is not None else attr_name

    async def _cmd(ctx: typer.Context, **kw: Any) -> Any:
        from .host import resolve_cli_host

        host = resolve_cli_host(ctx)
        method = getattr(host, attr_name, None)
        if method is None or not callable(method):
            fail(f"host {getattr(host, 'id', '?')!r} does not support {verb!r}.")
        call_kw = dict(binding.excluded)
        for name, value in kw.items():
            conv = binding.converters.get(name)
            call_kw[name] = conv(value) if conv is not None else value
        # Filter only excluded-default keys to the params the concrete method accepts;
        # CLI-sourced keys (kw) are always kept so unexpected ones raise a loud TypeError.
        # The binding is built from the first-registered sample_func; a different
        # host class may implement the same verb without some internal params
        # (DockerContainerHost.put lacked show_progress until 2026-09-03; the
        # filter stays because the binding is still per-verb, not per-class).
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
        # Import and label bound before the try: nothing evaluated inside the
        # finally may raise ahead of the teardown guard and mask the verb's
        # own failure.
        from ..host.connections import teardown_step

        host_label = str(getattr(host, "id", "host"))
        try:
            result = await method(**call_kw)
        except NotImplementedError as e:
            fail(f"host {getattr(host, 'id', '?')!r} does not support {verb!r}: {e}")
        finally:
            with teardown_step(host_label, "post-verb host close"):
                await host.close()
        # Presentation is per-invocation state: `success` comes off the RESOLVED
        # host's bound method (`__cli_success__` is per-class — "Module loaded."
        # vs "Binary loaded." for the same verb name), so it cannot be a static
        # marker. Install it on ctx.meta (shared by-reference down the context
        # chain) for the leaf-invoke wrapper's `render_leaf_value`, and return
        # the raw result for it to render. `none_message=success or "done"`
        # preserves the host verbs' historical None rendering.
        ctx.meta[RENDER_POLICY_KEY] = RenderPolicy(success=success, none_message=success or "done")
        return result

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


def host_dry_run_references(ctx: typer.Context) -> "list[Any]":
    """Resolve the host an ``otto host`` invocation names, for the dry-run seam.

    Lent to :func:`~otto.cli.invoke.stop_at_dry_run_seam` through the
    ``__otto_dry_run_refs__`` marker, and deliberately the SAME call the verb
    body makes (:func:`~otto.cli.host.resolve_cli_host`) rather than a
    lookalike: an unknown id, an unreachable ``--hop`` id, an unknown
    ``--term``/``--transfer`` all fail here exactly as they fail in a real run,
    so ``-n`` validates for real instead of echoing a command back at someone
    whose host does not exist. Resolution builds the host object; it opens no
    transport and issues no command, which is what makes it legal under a dry
    run at all.

    The ``--term``/``--transfer`` overrides travel WITH the reference. They have
    to: :func:`resolve_cli_host` applies them by building an override copy that
    lives on ``ctx.obj`` and not in the lab, so a consumer that re-fetched the
    id alone (``--dry-run --probe``) would dial the lab default and report
    reachability for a transport this invocation is not going to use. Read back
    out of the same ``_otto_host_request`` stash the resolver itself reads, so
    there is still one authority for what the invocation asked for.
    """
    from .host import resolve_cli_host
    from .invoke import LabReference

    host_id = str(getattr(resolve_cli_host(ctx), "id", "") or "")
    request = getattr(ctx, "meta", None) or {}
    overrides = request.get("_otto_host_request") or {}
    return [
        LabReference(
            kind="host",
            name=host_id,
            host_ids=[host_id],
            term=overrides.get("term") or None,
            transfer=overrides.get("transfer") or None,
        )
    ]


def _synthesize_command(
    cli_name: str, attr_name: str, help_text: str, sample_func: Callable[..., Any]
) -> Any:
    """Build a vendored-click ``Command`` for *cli_name* via a throwaway Typer.

    The Typer-native way to convert a function — no hand-written click types.
    The async body runs under the command lifecycle via the leaf-invoke
    wrapper's coroutine bridge (``cli/invoke._wrap_invoke``), which wraps
    these synthesized verbs as ``HostGroup`` resolves them.
    """
    cmd_fn = make_method_command(attr_name, sample_func, cli_name)
    # Propagate the verb's per-invocation output-dir preference onto the command
    # callback so the leaf-invoke preamble (which reads `__cli_output_dir__` off
    # `ctx.command.callback`) honours read-only verbs (exists/ls/…) that opt out.
    # Typer's own callback shim functools-wraps cmd_fn, carrying the marker
    # through to the resolved command's callback.
    cmd_fn.__cli_output_dir__ = getattr(sample_func, "__cli_output_dir__", True)  # ty: ignore[unresolved-attribute]
    # Same idiom, same reader, for the dry-run seam: whether this verb owns its
    # own preview, and how to resolve the host it names before the seam reports
    # it. Without the first line every host verb would be seam-stopped
    # regardless of what its author declared; without the second, `-n` would
    # exit 0 on a host that does not exist.
    from .invoke import DRY_RUN_PREVIEW_ATTR, DRY_RUN_REFS_ATTR

    setattr(cmd_fn, DRY_RUN_PREVIEW_ATTR, getattr(sample_func, DRY_RUN_PREVIEW_ATTR, False))
    setattr(cmd_fn, DRY_RUN_REFS_ATTR, host_dry_run_references)
    tmp = typer.Typer()
    tmp.command(name=cli_name, help=help_text or None)(cmd_fn)
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
