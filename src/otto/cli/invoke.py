"""Shared command-wrapper machinery: OttoContext injection + options expansion.

The plumbing behind both ``@instruction`` (``otto run`` subcommands) and
``@cli_command`` (top-level commands): a parameter annotated ``OttoContext``
is stripped from the CLI signature and supplied at call time from the active
context, and an *options* dataclass parameter is expanded into individual CLI
flags. Factored out of ``cli/run.py`` so both decorators share one
implementation.
"""

import dataclasses
import functools
import inspect
from collections.abc import Callable
from logging import getLogger
from pathlib import Path
from typing import TYPE_CHECKING, Any, get_type_hints

import typer
from typing_extensions import override

from ..params import build_options, options_params

if TYPE_CHECKING:
    from typer.core import TyperGroup

    from ..context import OttoContext
    from ..registry import Registry
    from .registry import CommandSpec


def _ctx_param_name(func: Callable[..., Any]) -> str | None:
    """Return the name of any parameter annotated as OttoContext, or None."""
    from ..context import OttoContext

    hints = get_type_hints(func)
    for name, hint in hints.items():
        if hint is OttoContext:
            return name
    return None


def _inject_ctx(func: Callable[..., Any], ctx_name: str) -> Callable[..., Any]:
    """Wrap *func* so the OttoContext param is supplied from the active context.

    Supplied at call time and hidden from the Typer-facing signature.
    """
    from ..context import get_context

    sig = inspect.signature(func)
    exposed = [p for n, p in sig.parameters.items() if n != ctx_name]

    @functools.wraps(func)
    async def wrapper(**kw: Any) -> Any:
        kw[ctx_name] = get_context()
        return await func(**kw)

    # Drop ctx_name from __annotations__ too so get_type_hints() on the
    # wrapper doesn't see it (important when _wrap_with_options composes on top).
    wrapper.__annotations__ = {k: v for k, v in func.__annotations__.items() if k != ctx_name}
    wrapper.__signature__ = inspect.Signature(exposed)  # ty: ignore[unresolved-attribute]
    return wrapper


def _wrap_with_options(
    func: Callable[..., Any],
    opts_cls: type,
) -> Callable[..., Any]:
    """Build a wrapper that expands an options dataclass into CLI parameters.

    The wrapper:
    1. Accepts the expanded dataclass fields as keyword arguments.
    2. Constructs the dataclass instance from those kwargs.
    3. Forwards it to *func* in the position of the original options parameter.
    """
    sig = inspect.signature(func)
    hints = get_type_hints(func, include_extras=True)

    # Find the parameter annotated with the options class
    opts_param_name: str | None = None
    for name, hint in hints.items():
        if hint is opts_cls:
            opts_param_name = name
            break

    if opts_param_name is None:
        raise TypeError(
            f"instruction {getattr(func, '__name__', repr(func))!r} declares options={opts_cls.__name__} "  # noqa: E501 — long error message f-string
            f"but has no parameter annotated as {opts_cls.__name__}"
        )

    # Build new parameter list: replace the opts param with expanded fields
    opts_field_names = {f.name for f in dataclasses.fields(opts_cls)}
    expanded = options_params(opts_cls)

    new_params: list[inspect.Parameter] = []
    for p in sig.parameters.values():
        if p.name == opts_param_name:
            new_params.extend(expanded)
        else:
            # Ensure all params are KEYWORD_ONLY for a consistent Typer signature
            kw_only_p = (
                p
                if p.kind == inspect.Parameter.KEYWORD_ONLY
                else p.replace(kind=inspect.Parameter.KEYWORD_ONLY)
            )
            new_params.append(kw_only_p)

    @functools.wraps(func)
    async def wrapper(**kw: Any) -> Any:
        # Split kwargs: dataclass fields vs. remaining params
        opts_kw = {k: kw.pop(k) for k in list(kw) if k in opts_field_names}
        opts_instance = build_options(opts_cls, opts_kw)
        kw[opts_param_name] = opts_instance
        return await func(**kw)

    wrapper.__signature__ = inspect.Signature(new_params)  # ty: ignore[unresolved-attribute]
    return wrapper


def prepare_command_target(
    func: Callable[..., Any], options_cls: type | None = None
) -> Callable[..., Any]:
    """Apply otto's CLI wrappers to *func*: OttoContext injection + options expansion.

    The shared machinery behind ``@instruction`` and ``@cli_command``: a
    parameter annotated ``OttoContext`` is stripped from the CLI signature and
    injected at call time; an *options_cls* dataclass parameter is expanded
    into individual CLI flags.

    Idempotent by contract, not coincidence: a callable this function already
    wrapped is returned unchanged (sentinel attribute). The dispatch path
    prepares twice — ``@cli_command`` at decoration, then
    ``resolve_spec_command``'s function-loader branch, which serves every
    function loader and can't know one was pre-prepared. Without the sentinel
    that was safe only because ``_inject_ctx`` happens to strip the ctx
    annotation that triggers it.
    """
    if getattr(func, "__otto_cli_prepared__", False):
        return func
    ctx_name = _ctx_param_name(func)
    target: Callable[..., Any] = func
    if ctx_name is not None:
        target = _inject_ctx(func, ctx_name)
    if options_cls is not None and dataclasses.is_dataclass(options_cls):
        target = _wrap_with_options(target, options_cls)
    if target is not func:
        target.__otto_cli_prepared__ = True  # ty: ignore[unresolved-attribute]
    return target


# ---------------------------------------------------------------------------
# Leaf-invoke preamble: lazy lab loading, session setup, output dir, gate
# ---------------------------------------------------------------------------


class LabContextError(Exception):
    """A lab-context failure carrying its user-facing message + exit code.

    :func:`ensure_lab_context` raises this instead of printing directly, so a
    soft probe (:func:`try_ensure_lab`, used by the class-scoped ``otto host``
    menu) can swallow it silently. The *loud* callers — :func:`command_preamble`
    and the root ``--show-lab`` / ``--list-hosts`` branch — catch it and print
    (the ``rich`` flag chooses ``rich.print`` vs a plain stderr ``typer.echo``)
    before re-raising ``typer.Exit`` with the stored ``exit_code``.
    """

    def __init__(self, message: str, exit_code: int, *, rich: bool = True) -> None:
        """Store *message*, *exit_code*, and whether to print with rich markup."""
        super().__init__(message)
        self.message = message
        self.exit_code = exit_code
        self.rich = rich


def report_lab_context_error(err: "LabContextError") -> None:
    """Print *err*'s message the loud way, then raise ``typer.Exit`` with its code.

    Shared by the loud lab-context callers so the exact user-facing text and
    exit codes live in one place. Rich messages go through ``rich.print``;
    non-rich messages (the plain ``Missing option '--lab'`` usage error) go to
    stderr via ``typer.echo`` to match click's own usage-error stream.
    """
    if err.rich:
        from rich import print as rprint

        rprint(err.message)
    else:
        typer.echo(err.message, err=True)
    raise typer.Exit(code=err.exit_code)


@dataclasses.dataclass(frozen=True)
class RootOptions:
    """The root-callback options the preamble needs, stashed on ``ctx.meta``.

    The root callback shrinks to recording these; the lab-loading /
    session-setup work reads them back lazily from ``ctx.meta`` at the moment a
    real (non-help) command invocation begins.
    """

    labs: "list[str] | None"
    xdir: Path
    log_days: int
    log_level: str
    rich_log_file: bool
    show_time: bool
    dry_run: bool
    as_user: "str | None"
    skip_reservation_check: bool


def ensure_cli_session(ctx: typer.Context) -> None:
    """Print the banner and initialise CLI logging once per invocation (idempotent).

    Split out of :func:`ensure_lab_context` so a soft lab probe (class-scoped
    ``otto host`` menus via :func:`try_ensure_lab`) never prints the banner or
    touches logging. Guarded by ``ctx.meta['_otto_session_ready']``.
    """
    meta = ctx.meta
    if meta.get("_otto_session_ready"):
        return
    meta["_otto_session_ready"] = True

    from rich import print as rprint
    from rich.align import Align

    from ..host import HostFilter
    from ..logger import management
    from .banner import banner

    opts: RootOptions = meta["_otto_root_options"]

    rprint(Align.center(banner))

    management.init_cli_logging(
        xdir=opts.xdir,
        log_level=opts.log_level,
        keep_days=opts.log_days,
        show_time=opts.show_time,
        rich_log_file=opts.rich_log_file,
    )
    management.attach_console_suppress_filter(HostFilter())

    # Stash the product / external logger prefixes (init roots, libs sub-packages,
    # explicit [logging] capture) so the per-subcommand create_output_dir attaches
    # the shared QueueHandler to them once it exists. Done here (after
    # init_cli_logging set the log level) so capture honours the verbose floor.
    from ..config import get_repos

    prefixes: set[str] = set()
    for repo in get_repos():
        prefixes |= repo.product_log_prefixes()
    management.set_capture_prefixes(prefixes)

    logger = getLogger(__name__)
    if opts.dry_run:
        logger.info(
            "[magenta][DRY RUN] Commands and file transfers will be skipped. "
            "Connections will still be verified."
        )
    for repo in get_repos():
        logger.debug(f"{repo.sut_dir}: {repo.commit}")


def ensure_lab_context(ctx: typer.Context) -> "OttoContext":
    """Load the lab, build reservation state, and install the runtime context (idempotent).

    Enforces ``--lab``, builds the lab repository, loads the lab, synthesizes
    docker placeholder hosts, resolves reservation state (stashed on
    ``ctx.meta['otto_reservation']``), and installs an ``OttoContext`` via
    ``set_context``. Guarded by ``ctx.meta['_otto_lab_ready']`` so repeated calls
    are cheap. No banner, no logging init, no output dir — those belong to
    :func:`ensure_cli_session` / :func:`command_preamble`.
    """
    from ..context import get_context

    meta = ctx.meta
    if meta.get("_otto_lab_ready"):
        return get_context()

    opts: RootOptions = meta["_otto_root_options"]

    from ..config import get_repos, load_lab

    # `--lab` is no longer a hard-required Typer option (so lab-free subcommands
    # can run without it); enforce it here — before any lab side effects — for
    # everything that does need a lab.
    if not opts.labs:
        # Raise (don't print): loud callers report via report_lab_context_error;
        # the soft HostGroup probe swallows it silently. `rich=False` keeps the
        # plain "Missing option '--lab'" usage text on stderr — matching click's
        # own usage-error stream. (A *real* click.UsageError would escape Typer
        # 0.26's vendored click fork uncaught, hence the manual message.)
        raise LabContextError(
            "Error: Missing option '--lab' / '-l' (env var: 'OTTO_LAB').",
            exit_code=2,
            rich=False,
        )

    repos = get_repos()

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

    from ..labs import LabRepositoryError, build_lab_repository

    try:
        lab_repository = build_lab_repository(
            lab_settings, lab_repo_dir, search_paths=lab_search_paths
        )
    except (ValueError, LabRepositoryError) as e:
        raise LabContextError(
            f"[bold red]Host source unavailable:[/bold red] {e}", exit_code=1
        ) from e

    lab = load_lab(opts.labs, preferences=merged_host_preferences, repository=lab_repository)

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
        build_reservation_gate,
    )

    try:
        reservation_gate = build_reservation_gate(
            repos,
            as_user=opts.as_user,
            skip_reservation_check=opts.skip_reservation_check,
            cwd_fallback=Path.cwd(),
        )
    except ReservationBackendError as e:
        raise LabContextError(
            f"[bold red]Reservation backend unavailable:[/bold red] {e}\n"
            f"Pass [bold]--skip-reservation-check[/bold] / [bold]-R[/bold] to proceed without the check.",  # noqa: E501 — long rich markup string
            exit_code=1,
        ) from e

    identity = reservation_gate.identity
    if identity is not None and identity.source == "--as-user":
        getLogger(__name__).info(
            f"[bold magenta][reservations] acting as {identity.username!r}"
            f" (--as-user)[/bold magenta]"
        )

    meta["otto_reservation"] = reservation_gate

    # Install the runtime context: lab + dry_run flag.
    from ..context import OttoContext, set_context

    set_context(OttoContext(lab=lab, dry_run=opts.dry_run))
    meta["_otto_lab_ready"] = True
    return get_context()


def try_ensure_lab(ctx: typer.Context) -> "OttoContext | None":
    """Soft variant of :func:`ensure_lab_context`: return None instead of raising.

    Used by ``HostGroup`` class-scoping — a soft probe where any failure (no
    ``--lab``, unknown lab, broken backend) simply means "no class scoping
    available", falling back to the full unscoped verb menu.
    """
    try:
        return ensure_lab_context(ctx)
    except Exception:  # noqa: BLE001 — soft scoping probe: ANY failure (incl. typer.Exit, an Exception subclass) → no scoping
        return None


def fail_loud_on_bootstrap_errors() -> None:
    """Exit(1) when bootstrap contained any repo error — shared loud gate.

    The per-error ``warning:`` lines were already printed by ``entry()`` at
    startup; print ONLY the framed summary here (don't re-print each error
    in red) — the summary points back at those warnings. Used by the leaf
    preamble AND the root ``--show-lab``/``--list-hosts`` branch, so anything
    that inspects the registered world fails the same way.
    """
    from ..bootstrap import bootstrap

    if bootstrap().errors:
        from rich import print as rprint

        rprint("[red]Cannot run commands while a repo fails to load (see warnings above).[/red]")
        raise typer.Exit(1)


def present_reservation_gate(ctx: typer.Context) -> None:
    """Evaluate the active reservation gate (if any) and present its warning.

    Reads ``ctx.meta["otto_reservation"]`` — a no-op when absent (e.g. a
    lab-free command, or a test that never populated it) — and calls
    :meth:`~otto.reservations.check.ReservationGate.evaluate`. ``evaluate()``
    returns a :class:`~otto.reservations.check.ReservationGateResult` whose
    ``warning`` is deliberately plain text (the library has no Typer/rich
    dependency); this function OWNS the presentation of that text — it is
    the single place that wraps it in ``[bold red]...[/bold red]`` markup.
    Both CLI call sites (``command_preamble`` here and the live branch of
    ``otto monitor``) delegate to this one function rather than composing
    the markup themselves.

    ``MissingReservationError`` (raised by ``evaluate()`` when a required
    resource isn't held) is not caught here — it propagates to the caller
    unchanged, exactly as it did before this adapter existed.
    """
    res = ctx.meta.get("otto_reservation")
    if res is None:
        return
    outcome = res.evaluate()
    if outcome.warning:
        from rich import print as rprint

        rprint(f"[bold red]{outcome.warning}[/bold red]")


def command_preamble(ctx: typer.Context) -> None:
    """Run once when a real (non-help) command invocation starts.

    Order: bootstrap errors fail loud → lab-free commands are done → CLI
    session (banner/logging) → lab context → per-command output dir →
    reservation gate. ``--help`` paths never reach this function: click's
    help option exits during leaf parse, before ``Command.invoke``.
    """
    meta = ctx.meta
    if meta.get("_otto_preamble_done"):
        return
    meta["_otto_preamble_done"] = True

    fail_loud_on_bootstrap_errors()

    spec: CommandSpec = meta["_otto_command_spec"]
    if spec.lab_free:
        return

    ensure_cli_session(ctx)
    try:
        ensure_lab_context(ctx)
    except LabContextError as e:
        report_lab_context_error(e)

    leaf_wants_dir = bool(getattr(ctx.command.callback, "__cli_output_dir__", True))
    if spec.output_dir and leaf_wants_dir:
        from ..context import get_context
        from ..logger import management

        # A flattened single-command group (e.g. ``monitor``) IS the group-level
        # command: its leaf name equals ``spec.name``, so there is no meaningful
        # sub-name — pass None to keep the base ``monitor/<TS>`` dir (not
        # ``monitor/<TS>_monitor``). Real sub-groups (run/test/host) keep their
        # ``<name>/<TS>_<sub>`` layout since ``ctx.command.name`` differs.
        leaf_name = ctx.command.name
        sub = None if leaf_name == spec.name else (leaf_name or spec.name)
        get_context().output_dir = management.create_output_dir(spec.name, sub)
    if spec.gate:
        present_reservation_gate(ctx)


def _wrap_invoke(cmd: Any, spec: "CommandSpec") -> Any:
    """Wrap a single leaf command's ``invoke`` with the preamble (idempotent)."""
    if getattr(cmd, "_otto_preambled", False):
        return cmd
    cmd._otto_preambled = True  # noqa: SLF001 — own marker attribute on the command object
    original_invoke = cmd.invoke

    def _invoke_with_preamble(inner_ctx: Any) -> Any:
        # Restamp on the leaf's own (inner) ctx: ctx.meta is shared by-reference
        # down the click context chain, but the spec must reflect THIS leaf.
        inner_ctx.meta["_otto_command_spec"] = spec
        command_preamble(inner_ctx)
        return original_invoke(inner_ctx)

    cmd.invoke = _invoke_with_preamble
    return cmd


def wrap_leaf_callbacks(cmd: Any, spec: "CommandSpec") -> Any:
    """Wrap every leaf command under *cmd* so its invoke runs the preamble first.

    Wrapping ``Command.invoke`` (not the callback) means the preamble runs
    only on real execution: a ``--help`` on the leaf exits during parse and
    never reaches ``invoke``. Groups recurse into their static subcommands AND
    wrap their ``get_command`` so lazily-synthesized subcommands (e.g. the
    dynamic ``otto host <verb>`` commands) are wrapped on resolution too.
    Already-wrapped commands are skipped (resolution results are cached).
    """
    if getattr(cmd, "_otto_preambled", False):
        return cmd
    if not hasattr(cmd, "commands"):
        return _wrap_invoke(cmd, spec)

    cmd._otto_preambled = True  # noqa: SLF001 — own marker attribute on the command object
    for sub in cmd.commands.values():
        wrap_leaf_callbacks(sub, spec)

    # Lazy groups (HostGroup) synthesize subcommands in get_command rather than
    # populating .commands up front — wrap the returned command as it resolves.
    original_get_command = cmd.get_command

    def _get_command_wrapped(gc_ctx: Any, name: str) -> Any:
        sub = original_get_command(gc_ctx, name)
        if sub is not None:
            wrap_leaf_callbacks(sub, spec)
        return sub

    cmd.get_command = _get_command_wrapped
    return cmd


# ---------------------------------------------------------------------------
# Shared lazy child-group factory — the "one attachment idiom" for
# registry-backed Typer groups (root CLI_COMMANDS, run/instructions,
# test/suites all resolve their children this way).
# ---------------------------------------------------------------------------


def make_registry_group(child_registry: "Registry[Any]") -> "type[TyperGroup]":
    """Build a TyperGroup class whose children come from *child_registry*.

    Children (suite/instruction sub-apps) convert lazily on first access.
    This follows the same idiom as ``HostGroup`` (``cli/expose.py``): the
    group only resolves children on demand — it does NOT itself wrap them
    with the leaf-invoke preamble. ``main.py``'s root dispatch wraps the
    WHOLE resolved group (and therefore every child it lazily resolves, via
    ``wrap_leaf_callbacks``'s ``get_command`` recursion) with the preamble
    for the top-level spec (``"run"`` / ``"test"``). This keeps ``run_app`` /
    ``suite_app`` usable standalone (e.g. in unit tests that invoke them
    directly via ``CliRunner`` without going through the full ``otto`` root
    app) while ``otto run smoke`` / ``otto test TestX`` still get the
    preamble when dispatched for real.
    """
    from typer.core import TyperGroup

    class RegistryBackedGroup(TyperGroup):
        """Group whose subcommands resolve from a component registry."""

        _child_cache: dict[str, Any]

        @override
        def list_commands(self, ctx: Any) -> list[str]:
            static = super().list_commands(ctx)
            return static + [n for n in child_registry.names() if n not in static]

        @override
        def get_command(self, ctx: Any, cmd_name: str) -> Any:
            static = super().get_command(ctx, cmd_name)
            if static is not None:
                return static
            if cmd_name not in child_registry:
                return None
            # Converted-child cache with NO invalidation: fine for the CLI's
            # one-shot process lifetime, but a same-file suite re-registration
            # (sanctioned: overwrite=True within one module) that happens
            # AFTER this group already converted the child would keep serving
            # the earlier conversion. If long-lived embedders ever hit that,
            # key the cache on the registry entry (or clear it on register).
            cache = getattr(self, "_child_cache", None) or {}
            self._child_cache = cache
            if cmd_name not in cache:
                entry = child_registry.get(cmd_name)
                converted: Any = typer.main.get_command(entry.sub_app)
                cache[cmd_name] = (
                    converted.commands[cmd_name]
                    if hasattr(converted, "commands") and cmd_name in converted.commands
                    else converted
                )
            return cache[cmd_name]

    return RegistryBackedGroup
