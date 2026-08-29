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
from typing import TYPE_CHECKING, Any, NoReturn, get_type_hints

import typer
from rich.markup import escape
from typing_extensions import override

from ..errors import OttoError
from ..params import build_options, options_params

if TYPE_CHECKING:
    from _typeshed import DataclassInstance
    from typer.core import TyperGroup

    from ..config.lab import Lab
    from ..config.repo import Repo
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
    opts_cls: "type[DataclassInstance]",
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


class LabContextError(OttoError):
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


def print_error(message: object, *, soft_wrap: bool = False) -> None:
    """Print *message* as a user-facing error, with rich markup ESCAPED.

    The one place a CLI command renders a failure, so the escaping happens
    once instead of at a dozen call sites that each had to remember it.
    ``coverage/`` and ``suite/run.py`` reached the same conclusion
    independently and call :func:`rich.markup.escape` inline at ten more
    sites; consolidating those is a separate pass, not a claim to make here.

    Escaping is load-bearing, not hygiene. Rich reads ``[word]`` as a style
    tag and DELETES it, so ``list[str]`` prints as ``list``, an install hint
    like ``otto-sh[monitor]`` prints as ``otto-sh`` — a runnable command for
    the wrong thing — and a pydantic detail like
    ``[type=missing, input_value={}]`` vanishes entirely. Every message that
    reaches here interpolates something a user, a path, or a library supplied,
    so none can be assumed bracket-free. Numeric subscripts (``argv[1]``)
    happen to survive; that is not a rule worth relying on.

    Lives here rather than in ``otto.console`` because every render site is a
    CLI one, and ``otto.cli`` deliberately does not depend on that module.

    *soft_wrap* turns the hard word-wrap off for messages that carry a token
    the reader is meant to COPY. Rich folds at the console width, and it folds
    between words -- so a hint ending ``or: -I firmware-integration`` breaks
    after the ``-I`` at 80 columns and hands the reader a switch they cannot
    paste. Same reasoning, and the same call shape, as ``otto.cli.link._row``
    and its twin ``otto.cli.tunnel._row``: a long line beats a broken argv.

    PASS IT when any part of the message is meant to be copied and re-run --
    a CLI switch, a shell command, a path, a host id. LEAVE IT OFF for prose,
    which is most errors: folding is what a reader wants there, and turning it
    off would leave one long line to scroll. The test is not "is this message
    long?" but "would a line break inside it destroy something the reader has
    to retype?".

    Note it also turns CROPPING off, which is the part that surprises people:
    with ``no_wrap`` alone rich TRUNCATES an over-wide line at the width
    instead of folding it, so the message quietly loses its tail while still
    looking complete.

    Off, this is the byte-identical call ``rich.print`` itself makes:
    ``rich.print`` forwards only ``sep`` and ``end`` to ``Console.print``, and
    that method's own ``soft_wrap`` default is ``None`` (meaning "defer to
    ``Console.soft_wrap``"). Passing ``None`` here is therefore that same call,
    not an approximation of it -- checked against a corpus of otto's real error
    messages at three widths, byte for byte.
    """
    from rich import get_console

    get_console().print(f"[red]{escape(str(message))}[/red]", soft_wrap=soft_wrap or None)


def fail(message: object, code: int = 1, *, soft_wrap: bool = False) -> "NoReturn":
    """Render *message* as a user-facing error and exit with *code*.

    The exiting half of :func:`print_error`: one place that decides what a
    failing command looks like, so a new command cannot accidentally ship an
    unescaped one (``.ast-grep/rules/error-render-through-helper.yml`` keeps
    that true).

    *soft_wrap* is threaded straight through -- see :func:`print_error` for
    when to ask for it. Keyword-only, and defaulted off, so every existing
    ``fail(msg)`` and ``fail(msg, 2)`` call is unchanged in both spelling and
    behavior.
    """
    print_error(message, soft_wrap=soft_wrap)
    # `from None`: typer.Exit is a control-flow signal, not a consequence of
    # whatever was caught. Chaining it would attach a __cause__ that click's
    # standalone mode discards anyway, and one converted site (expose.py) had
    # asked for exactly this suppression explicitly.
    raise typer.Exit(code) from None


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
    probe: bool = False
    """``--probe``: under a dry run, open a connection to each host the command
    names (spec §3). Defaults so a caller that predates the flag still builds;
    the root callback always passes it, and rejects it without ``--dry-run``."""
    include_projects: tuple[str, ...] = ()
    """``-I``: PEP-503-normalized names forced active (spec §2)."""
    exclude_projects: tuple[str, ...] = ()
    """``-E``: PEP-503-normalized names switched off (spec §2)."""


def ensure_cli_session(ctx: typer.Context) -> None:
    """Initialise CLI logging once per invocation (idempotent).

    Split out of :func:`ensure_lab_context` so a soft lab probe (class-scoped
    ``otto host`` menus via :func:`try_ensure_lab`) never touches logging.
    Guarded by ``ctx.meta['_otto_session_ready']``. The banner is not printed
    here — it shows on help screens only, via :func:`ensure_help_banner`.
    """
    meta = ctx.meta
    if meta.get("_otto_session_ready"):
        return
    meta["_otto_session_ready"] = True

    from ..host import HostFilter
    from ..logger import management

    opts: RootOptions = meta["_otto_root_options"]

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
        # Says which of the two dry runs this is. The old wording ("Connections
        # will still be verified") became false the moment the seam landed: a
        # bare dry run now touches NOTHING, and a connection happens only when
        # the operator asks for one with --probe.
        contact = (
            "--probe will open a connection to each named host, and run no command."
            if opts.probe
            else "No device will be contacted."
        )
        logger.info(f"[magenta][DRY RUN] Commands and file transfers will be skipped. {contact}")
    for repo in get_repos():
        logger.debug(f"{repo.sut_dir}: {repo_provenance(repo)}")


PROVENANCE_NOT_READ = "commit not read (the query was declined)"
"""Stand-in logged when a repo's HEAD could not be read. See :func:`repo_provenance`."""


def repo_provenance(repo: "Repo") -> str:
    """Return *repo*'s commit SHA, or a stand-in saying it could not be read.

    **A log line must never be able to fail the invocation it describes.**
    ``Repo.commit`` shells out to git, so it can come back declined, and
    reading a declined result's payload raises. The caller is a
    ``logger.debug`` f-string, which is evaluated whatever the log level — so
    before this function existed, a declined provenance query aborted the
    command with a traceback from a line whose only job was to write one
    sentence into ``verbose.log``.

    That is the shape the dry-run sweep found eight times over in
    ``otto.docker``: an eager ``.value`` inside a log string, surfacing a WRONG
    STORY (here: "git was not run") in place of the real one (here: nothing,
    because the line does not matter). Naming the error at the read is right
    for a parser and wrong for a narrator.

    This is the SECOND of two independent defences, and the other one is why
    the ``except`` arm below does not fire today:
    :meth:`~otto.config.repo.Repo.run_git_command` is exempt from the dry-run
    decline (it reads the local checkout's HEAD and contacts no device), so
    the provenance query succeeds and a dry run logs the same real SHA a live
    run logs. Either defence alone stops the traceback; both are kept because
    they fail for different reasons — the exemption could be dropped in a
    refactor, and a future provenance source could decline for a reason that
    has nothing to do with dry runs.

    A NAMED arm above nothing wider, deliberately. Anything that is not a
    decline — a git binary that is missing, a permission error — still
    propagates, because those are real failures of a real query and this
    function has no business swallowing them.
    """
    from ..result import CommandNotRunError

    try:
        return str(repo.commit)
    except CommandNotRunError:
        return PROVENANCE_NOT_READ


def build_lab_from_repos(repos: "list[Repo]", labnames: "str | list[str]") -> "Lab":
    """Aggregate the repos' lab configuration and load the named lab(s).

    The lab-construction slice of :func:`ensure_lab_context`, factored out so
    completion (``otto.cli.remote_completion``) builds the identical lab the
    real command would — same sources, preference merge, and host-source
    backend selection. Raises :class:`LabContextError` when the host source is
    unavailable.

    Every repo's ``[[lab.sources]]`` entries aggregate, in OTTO_SUT_DIRS
    order, into one composite: ALL declared sources are live, and a later
    source's host record overrides an earlier one's. No repo's declaration
    shadows another's.

    THE one place the process inventory is resolved (spec 2026-08-28
    host-inventory §8): built here, once, and handed to the load so every
    source sees the same one. Building it does no I/O, so a lab with no
    referenced entry never touches the inventory — but a BROKEN declaration
    is loud here rather than silently "no inventory", which is how a typo'd
    ``[inventory]`` would otherwise surface as an unexplained
    "no inventory is configured" against the first referencing host.
    """
    from ..config import load_lab

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

    from ..labs import LabRepositoryError, build_lab_sources

    try:
        lab_repository = build_lab_sources(repos)
    except (ValueError, LabRepositoryError) as e:
        raise LabContextError(
            f"[bold red]Host source unavailable:[/bold red] {escape(str(e))}",
            exit_code=1,
        ) from e

    # Function-local: `otto.inventory` pulls ~77 otto modules, and this module
    # is on the budgeted CLI surfaces (scripts/import_budget.py). The edge is
    # real and declared in tach.toml; only the timing is deferred.
    from ..inventory import InventoryError, build_inventory

    try:
        inventory = build_inventory(repos)
    except InventoryError as e:
        raise LabContextError(
            f"[bold red]Inventory unavailable:[/bold red] {escape(str(e))}", exit_code=1
        ) from e

    return load_lab(
        labnames,
        preferences=merged_host_preferences,
        repository=lab_repository,
        inventory=inventory,
    )


def ensure_lab_context(ctx: typer.Context) -> "OttoContext":
    """Load the lab, build reservation state, and install the runtime context (idempotent).

    Enforces ``--lab``, builds the lab repository, loads the lab, synthesizes
    docker placeholder hosts, resolves reservation state (stashed on
    ``ctx.meta['otto_reservation']``), and installs an ``OttoContext`` via
    ``set_cli_context``. Guarded by ``ctx.meta['_otto_lab_ready']`` so repeated calls
    are cheap. No banner, no logging init, no output dir — those belong to
    :func:`ensure_cli_session` / :func:`command_preamble`.
    """
    from ..context import get_context

    meta = ctx.meta
    if meta.get("_otto_lab_ready"):
        return get_context()

    opts: RootOptions = meta["_otto_root_options"]

    from ..config import get_repos

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

    lab = build_lab_from_repos(repos, opts.labs)

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
            f"[bold red]Reservation backend unavailable:[/bold red] {escape(str(e))}\n"
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

    # Install the runtime context: lab + dry_run flag + the per-invocation
    # project switches (-I/-E), which every activation question reads back
    # through otto.config.scope.active. Token kept module-side in otto.context;
    # entry()'s finally calls reset_cli_context().
    from ..context import OttoContext, set_cli_context

    set_cli_context(
        OttoContext(
            lab=lab,
            dry_run=opts.dry_run,
            include_projects=opts.include_projects,
            exclude_projects=opts.exclude_projects,
        )
    )
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


def validate_project_switches(ctx: typer.Context) -> None:
    """Exit 2 when ``-I``/``-E`` names a repo discovery never found (spec §2).

    Runs at first use (the preamble / the ``--show-lab`` branch) rather than in
    the root callback, because the check needs the discovered repo set and the
    root callback also runs for ``--help``, where there is no invocation to
    validate. It claims no saving on user code: ``entry()`` has already called
    ``bootstrap()`` before argv parsing on every non-completion invocation, and
    the gate on the very next line calls it again. The early return below is
    just "nothing was asked for, so look nothing up".

    The name is normalized on BOTH sides — the switch values arrive
    PEP-503-normalized from the root callback, so comparing them against a raw
    ``repo.name`` would reject the only spelling the CLI can produce.

    Printed manually + ``typer.Exit(2)`` rather than raised as a
    ``click.UsageError``: a real one raised after parse escapes Typer 0.26's
    vendored click fork uncaught, which is why ``ensure_lab_context``
    hand-writes its missing-``--lab`` usage text too.
    """
    opts: "RootOptions | None" = ctx.meta.get("_otto_root_options")
    if opts is None or not (opts.include_projects or opts.exclude_projects):
        return
    import difflib

    from ..bootstrap import bootstrap
    from ..models.dependencies import normalize_name

    known = {normalize_name(repo.name) for repo in bootstrap().repos}
    for value in (*opts.include_projects, *opts.exclude_projects):
        if value in known:
            continue
        close = difflib.get_close_matches(value, sorted(known), n=1)
        hint = f" — did you mean {close[0]!r}?" if close else ""
        fail(f"no project {value!r}{hint}", 2)


def fail_loud_on_bootstrap_errors(ctx: "typer.Context | None" = None) -> None:
    """Exit(1) when bootstrap contained an ACTIVE repo's error — shared loud gate.

    The per-error ``warning:`` lines were already printed by ``entry()`` at
    startup; print ONLY the framed summary here (don't re-print each error
    in red) — the summary points back at those warnings. Used by the leaf
    preamble AND the root ``--show-lab``/``--list-hosts`` branch, so anything
    that inspects the registered world fails the same way.

    With *ctx*, an error attributable to a repo that is inactive for this
    invocation demotes to one warning line (spec §3): a broken sibling must not
    fail a run it is not part of. Inactivity here is the PRE-LAB projection —
    the explicit switches plus lab-name inference
    (:func:`otto.config.scope.inactive_before_lab`) — because this gate is what
    protects lab construction from a half-registered world and so has to run
    before it; a repo that is inactive only because it is host-starved
    therefore keeps its errors fatal. Attribution is by ``sut_dir``: an error
    belonging to no discovered repo (a ``settings.toml`` that failed to PARSE,
    so no :class:`~otto.config.repo.Repo` object exists) can never be judged
    inactive and stays fatal. Without *ctx* — library callers, tests — every
    error is fatal, unchanged.
    """
    from ..bootstrap import bootstrap

    result = bootstrap()
    if not result.errors:
        return

    fatal = list(result.errors)
    opts: "RootOptions | None" = ctx.meta.get("_otto_root_options") if ctx is not None else None
    if opts is not None:
        from ..config.scope import inactive_before_lab
        from ..models.dependencies import normalize_name

        by_dir = {str(repo.sut_dir): repo for repo in result.repos}
        fatal = []
        for err in result.errors:
            repo = by_dir.get(str(err.sut_dir))
            if repo is None:
                fatal.append(err)  # discovery-phase error: nothing to attribute
                continue
            name = normalize_name(repo.name)
            # Include is tested FIRST, where :func:`otto.config.scope.active`
            # tests exclude first. The two agree only because the tuples are
            # disjoint, which the root callback guarantees:
            # ``_refuse_contradictory_switches`` exits 2 on any overlap before
            # RootOptions is built. A library caller hand-building RootOptions
            # with a name in both would get "fatal" here and "inactive" there;
            # the CLI cannot produce that input.
            if name in opts.include_projects:
                # -I beats every inference: the operator said this repo is part
                # of the run, so its failure is the run's failure.
                fatal.append(err)
                continue
            if name in opts.exclude_projects:
                reason = f"--exclude-projects {name}"
            elif inactive_before_lab(repo.project_scope, opts.labs):
                reason = f"not applicable to lab(s) [{', '.join(opts.labs or ())}]"
            else:
                fatal.append(err)
                continue
            # PRINTED, not logged. This gate runs before `ensure_cli_session`
            # at BOTH call sites, so `init_cli_logging` has not yet put a
            # console handler on the `otto` logger — and the library-citizen
            # NullHandler otto attaches at import counts to
            # `logging.callHandlers`, which defeats `logging.lastResort`. A
            # `getLogger().warning` here emitted NOTHING, leaving the operator
            # with entry()'s scary "failed to load" line and no sign of the
            # decision taken on their behalf.
            #
            # stderr via typer.echo, matching `_emit_bootstrap_findings` — the
            # site that printed the startup `warning:` line this one explains,
            # so a redirect cannot separate them. Deliberately NOT rich.print:
            # the run CONTINUES past here, so a diagnostic on stdout would land
            # inside a successful command's output, and rich would eat the
            # reason's `[unix]` as a style tag (leaving `lab(s) )`) — the exact
            # damage `print_error` exists to prevent.
            typer.echo(
                f"warning: repo {repo.name!r} failed to load, but is inactive "
                f"for this run ({reason}) — continuing without it",
                err=True,
            )

    if fatal:
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

        rprint(f"[bold red]{escape(outcome.warning)}[/bold red]")


def ensure_lab_session(ctx: typer.Context, spec: "CommandSpec") -> None:
    """Run the lab-requiring slice of the leaf-invoke preamble: session, lab, output dir.

    Shared by :func:`command_preamble` (every ordinary, non-``lab_free``
    command) and any ``lab_free``-registered command whose ONE branch still
    needs a lab — e.g. ``otto monitor --live`` (:mod:`otto.cli.monitor`),
    which pulls this in itself the same loud way ``otto reservation check``
    pulls in :func:`ensure_lab_context`, mirroring the per-branch
    ``spec.gate`` pattern those commands already use for the reservation
    gate. Idempotent (``ensure_cli_session``/``ensure_lab_context`` each
    guard their own re-entry), except for the output-dir creation, which a
    caller should therefore invoke at most once per command invocation.

    Does not touch the reservation gate itself — callers decide when (or
    whether) to call :func:`present_reservation_gate`.
    """
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


def refuse_inactive_instruction(inner_ctx: typer.Context) -> None:
    """Refuse dispatching a repo-owned instruction whose repo is inactive (spec §5).

    Runs in the leaf preamble AFTER the lab session exists, so the verdict can
    come from :func:`otto.config.scope.active` — the one authority — rather than
    from a pre-lab projection of it. Ordering is load-bearing, not cosmetic:
    before the session there are no ``ctx.scopes`` verdicts at all, and a
    missing verdict resolves ACTIVE, so a gate hoisted above it would refuse
    nothing except an explicit ``-E``.

    Help and completion never reach here (click exits during parse), so every
    registered instruction stays listed. That is deliberate: activation is
    per-invocation, and hiding entries would make ``-I`` undiscoverable —
    the reader would have no way to learn the name the hint tells them to pass.

    Exit 1, not 2: the command line is well-formed; the configuration excludes
    it. A usage error would tell the reader to fix their typing, which is the
    wrong place to look.
    """
    # Climb to the node whose PARENT is the ``run`` group -- for an ordinary
    # instruction that is the leaf itself, and for one registered as a
    # sub-group (``add_typer``) it is the group, whose name is the registry
    # key. Anything else -- ``otto host <id> get``, ``otto link impair`` --
    # exits at the root with ``parent is None`` and is left alone; this gate
    # is about instruction dispatch, not about every command in the CLI.
    node = inner_ctx
    while node.parent is not None and getattr(node.parent.command, "name", None) != "run":
        node = node.parent
    if node.parent is None:
        return  # not dispatched through `otto run`

    from ..instructions import INSTRUCTIONS

    # `or ""`: click types `info_name` as `str | None`, and an unnamed node owns
    # nothing -- "" is never a registered instruction, so the one lookup below
    # covers both "no name" and "not an instruction" without a second branch.
    name = node.info_name or ""
    if name not in INSTRUCTIONS:
        return  # a statically-declared `run` child; nobody owns it
    owner = INSTRUCTIONS.get(name).registered_by
    if owner is None:
        return  # first-party (or hand-registered) -- never refused

    from ..config.scope import active, switched_off
    from ..context import get_context

    ctx = get_context()
    if active(owner, ctx):
        return

    from ..models.dependencies import normalize_name

    switch_name = normalize_name(owner)
    if switched_off(owner, ctx):
        detail = f"which was switched off for this run (--exclude-projects {switch_name})"
        hint = f"  activate it: remove --exclude-projects {switch_name}    or: -I {switch_name}"
    else:
        # Not switched off, yet not active: `active()` reaches that verdict ONLY
        # by finding an unusable ProjectScope under the repo's declared name, so
        # the lookup cannot miss. Subscripting rather than `.get(...) or <blank>`
        # keeps it that way -- a blanked-out sentence would be a plausible,
        # content-free error, which is worse than the KeyError it replaced.
        scope = ctx.scopes[owner]
        loaded = ", ".join(scope.loaded_labs)
        if scope.excluded:
            # No loaded lab applies. The fix is a different `-l`, so the hint
            # names the patterns the repo actually declared.
            patterns = ", ".join(scope.lab_patterns) or "(none)"
            detail = (
                f"which is inactive for the loaded lab(s) [{loaded}] (lab_patterns: {patterns})"
            )
            wanted = " / -l ".join(sorted(scope.lab_patterns)) or "<a matching lab>"
            hint = f"  activate it: -l {wanted}    or: -I {switch_name}"
        else:
            # Labs match, hosts do not. Different cause, different fix -- the
            # same distinction `otto.config.scope._unusable_scope_message`
            # draws for the walks, so the two surfaces stay readable together.
            patterns = ", ".join(scope.host_patterns) or "(none)"
            detail = (
                f"which is inactive: its [project] host_patterns ({patterns}) "
                f"match no host in the loaded lab(s) [{loaded}]"
            )
            hint = f"  activate it: widen host_patterns, or: -I {switch_name}"
    # Through `fail`, never a hand-rolled `[red]` f-string: the message
    # interpolates a lab list and the literal table name `[project]`, and rich
    # DELETES `[word]` as markup unless it is escaped -- the demotion warning
    # a few functions up was observed rendering "lab(s) [unix]" as "lab(s) )"
    # for exactly this reason. `print_error` escapes; the
    # `error-render-through-helper` ast-grep rule keeps this route the only one.
    # soft_wrap: the hint's whole job is to hand over a runnable switch. Rich
    # folds between WORDS at the console width, so at 80 columns an ordinary
    # repo name splits `or: -I firmware-integration` across the fold and the
    # token cannot be pasted. The detail line above is prose and would be fine
    # folded; the hint is not, and they share one render.
    fail(f"{name!r} belongs to repo {owner!r}, {detail}\n{hint}", soft_wrap=True)


def refuse_unsatisfied_dependencies() -> None:
    """Refuse when an ACTIVE repo declares a Python requirement this env lacks (spec §3).

    Runs in the leaf preamble AFTER the lab session exists, for the same reason
    :func:`refuse_inactive_instruction` does and no other: severity here is an
    ACTIVATION question, and :func:`otto.config.scope.active` -- the one
    authority -- needs the lab verdicts the session installs. The spec asked for
    this check inside ``bootstrap()``, which runs before any of that exists;
    those two requirements cannot both hold, and the authority is the half worth
    keeping. The pre-lab projection :func:`otto.config.scope.inactive_before_lab`
    would have refused a HOST-STARVED repo -- one whose labs match but whose
    ``host_patterns`` select nothing -- because that shape is undetectable
    before the lab is built. Here it is simply inactive, and warns.

    Two consequences of the position, both deliberate:

    * **Library callers are not checked.** ``open_context`` and anything else
      calling ``bootstrap()`` directly never reach this gate. It is a CLI
      refusal, not a composition-root one.
    * **An EAGER import failure never gets here.** ``bootstrap()``'s per-repo
      init loop runs first, so a repo whose init module imports a missing
      package at module scope is already a ``BootstrapError`` and
      :func:`fail_loud_on_bootstrap_errors` reports it generically. Only the
      LAZY shape -- the import inside an instruction body, the one that would
      otherwise fail mid-run with hosts already touched -- is what this gate
      exists to pre-empt.

    Called only from ``command_preamble``'s non-``lab_free`` branch, which is
    also what keeps ``otto env sync`` -- the verb the refusal names -- outside
    the gate that would otherwise refuse to let you run the fix.
    """
    from ..bootstrap import bootstrap
    from ..config.scope import active
    from ..context import get_context
    from ..env.preflight import preflight

    result = preflight(bootstrap().ordered_repos)
    # PRINTED to stderr, not logged and not through rich -- the same call and
    # the same three reasons as the demotion warning in
    # `fail_loud_on_bootstrap_errors`: the run CONTINUES past here so stdout
    # belongs to the command, rich would eat a bracketed token, and the
    # library-citizen NullHandler defeats `logging.lastResort` for a logger
    # with no console handler yet.
    for warning in result.warnings:
        typer.echo(f"warning: {warning}", err=True)
    if not result.unsatisfied:
        return

    ctx = get_context()
    blocking = []
    for bad in result.unsatisfied:
        if active(bad.repo, ctx):
            blocking.append(bad)
            continue
        typer.echo(
            f"warning: repo {bad.repo!r} requires {bad.requirement!r} — not satisfied "
            f"in this environment (found: {bad.found}), but {bad.repo} is inactive for "
            f"this run — continuing without it",
            err=True,
        )
    if not blocking:
        return

    lines = [
        f"error: repo {bad.repo!r} requires {bad.requirement!r} — not satisfied in "
        f"this environment (found: {bad.found})"
        for bad in blocking
    ]
    # `env sync` ALWAYS, and always second: it is the verb that fixes this
    # whatever the cause, and it is the one an operator who has never built an
    # orchestration venv needs to be told about. The direct install is third
    # because it is for the operator who manages the environment by hand --
    # correct, but it leaves otto's record of the env untouched.
    lines.append("  fix: otto env sync")
    installs = " ".join(f"{bad.requirement!r}" for bad in blocking)
    lines.append(f"  or:  uv pip install {installs}")
    # Through `fail`, never a hand-rolled `[red]` f-string. A requirement
    # carries brackets whenever it names an extra (`otto-sh[monitor] >= 1`) and
    # rich DELETES `[word]` as markup -- printing a runnable command for the
    # wrong package. `print_error` escapes; the `error-render-through-helper`
    # ast-grep rule keeps this route the only one. soft_wrap because both fix
    # lines are meant to be PASTED, and rich folds between words at the console
    # width.
    fail("\n".join(lines), 1, soft_wrap=True)


def ensure_help_banner(ctx: typer.Context) -> None:
    """Print the banner before rendered help text, once per invocation (idempotent).

    Independent of :func:`ensure_cli_session`: every help screen shows the
    banner regardless of which command it belongs to, while real command
    execution never shows it. Guarded by ``ctx.meta['_otto_help_banner_shown']``
    — ``ctx.meta`` is shared by reference down the whole context chain, so one
    guard covers the root context and every leaf/group context beneath it.
    """
    meta = ctx.meta
    if meta.get("_otto_help_banner_shown"):
        return
    meta["_otto_help_banner_shown"] = True

    from .banner import print_banner

    print_banner()


def command_preamble(ctx: typer.Context) -> None:
    """Run once when a real (non-help) command invocation starts.

    Order: ``-I``/``-E`` names are validated → an active repo's bootstrap
    errors fail loud → lab-free commands skip the lab slice → CLI session
    (logging) → lab context → per-command output dir → an inactive repo's
    instruction is refused → an active repo's unsatisfied Python dependencies
    are refused → reservation gate → the dry-run seam. ``--help`` paths never
    reach this function: click's help option exits during leaf parse, before
    ``Command.invoke``.

    The seam is LAST on purpose. Everything above it is the validating half of
    the dry-run contract (spec §1: arguments coerce, the lab loads, references
    resolve, the gate speaks), and a dry run has to pay all of it before it is
    allowed to claim the command "would run" — otherwise ``-n`` degrades into
    print-and-exit and a typo'd host reference exits 0. The seam adds only the
    stop, and it applies to ``lab_free`` commands too: ``lab_free`` means "I
    drive the lifecycle myself", which is exactly the command that could still
    reach a device (``otto monitor --live``) with nobody watching.
    """
    meta = ctx.meta
    if meta.get("_otto_preamble_done"):
        return
    meta["_otto_preamble_done"] = True

    # Order matters: a typo'd -I/-E name is reported as a typo, rather than
    # being silently absorbed into a demotion decision the gate makes next.
    validate_project_switches(ctx)
    fail_loud_on_bootstrap_errors(ctx)

    spec: CommandSpec = meta["_otto_command_spec"]
    if not spec.lab_free:
        ensure_lab_session(ctx, spec)
        # After the session (the lab verdicts it installs ARE the input) and
        # before the gate: a run that is being refused must not first be
        # warned about the reservations it would have needed.
        refuse_inactive_instruction(ctx)
        # After the ownership gate, which refuses the thing the operator TYPED
        # and so owns the more specific sentence; before the reservation gate,
        # for the reason stated one line up.
        refuse_unsatisfied_dependencies()
        if spec.gate:
            present_reservation_gate(ctx)

    stop_at_dry_run_seam(ctx, spec)


# ---------------------------------------------------------------------------
# The --dry-run seam: validate, print what would run, stop before the body
# ---------------------------------------------------------------------------

DRY_RUN_PREVIEW_ATTR = "__cli_dry_run_preview__"
"""Per-leaf opt-out marker read off the resolved command's callback.

Stamped by ``@cli_exposed(dry_run_preview=True)`` (host verbs) and by
``otto.suite.register`` (suite leaves); read here the same way
``ensure_lab_session`` reads ``__cli_output_dir__``, so a third-party leaf
opts in through exactly the mechanism otto's own leaves use.
"""

DRY_RUN_REFS_ATTR = "__otto_dry_run_refs__"
"""Per-leaf hook naming the lab references a dry run must still resolve.

A callable ``(ctx) -> Iterable[LabReference]``. The seam runs it BEFORE it
prints anything, so an unknown host/link/tunnel reference fails the way it
always did instead of being echoed back as if it existed. It exists because
reference resolution lives in command BODIES, and the seam's whole job is not
running those -- the hook lets a leaf lend the seam the same resolver its body
would have used (``otto host`` lends :func:`~otto.cli.host.resolve_cli_host`),
so there is one authority rather than a mirrored copy that can drift.
"""


@dataclasses.dataclass(frozen=True)
class LabReference:
    """One lab entity a command names, resolved before the dry run reports it."""

    kind: str
    """What kind of entity this is -- ``"host"``, ``"link"`` or ``"tunnel"``."""

    name: str
    """The resolved identifier, as the block should print it."""

    host_ids: "list[str]" = dataclasses.field(default_factory=list)
    """Lab host ids this reference names, for ``--dry-run --probe`` to dial."""

    term: "str | None" = None
    """Per-invocation terminal-protocol override applying to :attr:`host_ids`
    (``otto host --term``), or ``None`` for the host's configured default.

    Carried on the reference so ``--probe`` dials the transport the command
    would actually have used. Without it the probe re-fetches the lab-default
    instance and can report a host reachable over SSH for an invocation that
    was going to use telnet -- a true statement about the wrong question."""

    transfer: "str | None" = None
    """Per-invocation file-transfer override applying to :attr:`host_ids`
    (``otto host --transfer``), or ``None``. Same reason as :attr:`term`, and it
    is not cosmetic: ``transfer='ftp'`` makes one probe open the FTP control
    channel as well as the term channel."""


def dry_run_requested(ctx: typer.Context) -> bool:
    """Whether this invocation is a dry run.

    Prefers the root callback's recorded options (the flag the user actually
    typed) and falls back to the active :class:`~otto.context.OttoContext` —
    which is what a sub-app driven without the root callback (unit tests, an
    embedder) installs, and what ``ensure_lab_context`` derives from the flag
    anyway, so the two can only ever agree on the real dispatch path.

    Read with ``getattr``, not attribute access: ``_otto_root_options`` is a
    plain ``ctx.meta`` slot, and several preamble tests park a bare sentinel
    there. A seam that raised ``AttributeError`` on an unfamiliar stash would
    fail the invocation at the one line whose job is to answer a yes/no.
    """
    flag = getattr(ctx.meta.get("_otto_root_options"), "dry_run", None)
    if flag is not None:
        return bool(flag)
    from ..context import try_get_context

    active = try_get_context()
    return bool(active is not None and active.dry_run)


def probe_requested(ctx: typer.Context) -> bool:
    """Whether this dry run may open connections (``--probe``, spec §3).

    Read ONLY from the root callback's recorded options — no
    :class:`~otto.context.OttoContext` fallback, unlike
    :func:`dry_run_requested`. ``dry_run`` lives on the context because the
    LIBRARY layer branches on it at every device boundary; ``--probe`` is a CLI
    presentation choice that nothing below the seam consults, and putting it on
    the context would invite exactly that. A sub-app driven without the root
    callback therefore never probes, which is the safe answer.
    """
    return bool(getattr(ctx.meta.get("_otto_root_options"), "probe", False))


def _leaf_declares_preview(ctx: typer.Context) -> bool:
    """Whether the resolved leaf stamped itself as owning its own dry-run preview."""
    return bool(getattr(getattr(ctx.command, "callback", None), DRY_RUN_PREVIEW_ATTR, False))


def resolve_dry_run_references(ctx: typer.Context) -> "list[LabReference]":
    """Resolve the lab references the leaf names, or return an empty list.

    Any failure propagates: a leaf's resolver raises/exits exactly as its body
    would have, so ``otto host nosuchbox exec 'uptime' -n`` still exits
    non-zero with the resolution error.
    """
    resolver = getattr(getattr(ctx.command, "callback", None), DRY_RUN_REFS_ATTR, None)
    if resolver is None:
        return []
    return list(resolver(ctx))


def _param_words(ctx: typer.Context) -> "list[str]":
    """Echo one context's non-default parameters back as command-line words."""
    import shlex

    words: list[str] = []
    for param in getattr(ctx.command, "params", ()):
        name = getattr(param, "name", None)
        if name is None or name not in ctx.params:
            continue
        value = ctx.params[name]
        if value is None or value == param.default:
            continue
        opts = [o for o in (getattr(param, "opts", None) or []) if o.startswith("-")]
        items = list(value) if isinstance(value, (list, tuple)) else [value]
        for item in items:
            if not opts:  # a positional argument: the value IS the word
                words.append(shlex.quote(str(item)))
            elif isinstance(item, bool):
                if item:
                    words.append(max(opts, key=len))
            else:
                words.extend((max(opts, key=len), shlex.quote(str(item))))
    return words


def would_run_line(ctx: typer.Context) -> str:
    """Rebuild the invocation from the parsed context chain, root command first.

    Reconstructed from what click PARSED rather than from ``sys.argv``, so it
    reflects the values the command would actually have received (env-var
    defaults included) and stays correct under any runner. The root context's
    own options are omitted: ``--lab`` is what the lab line reports, and
    echoing ``--dry-run`` back at someone who just typed it is noise.
    """
    chain: list[typer.Context] = []
    node: Any = ctx
    while node is not None:
        chain.append(node)
        node = node.parent
    chain.reverse()

    words: list[str] = []
    for depth, node in enumerate(chain):
        if node.info_name:
            words.append(node.info_name)
        if depth:
            words.extend(_param_words(node))
    return " ".join(words)


def _lab_line(references: "list[LabReference]") -> str:
    """Describe the lab the dry run validated against, and what resolved in it."""
    from ..context import try_get_context

    active = try_get_context()
    lab = getattr(active, "lab", None) if active is not None else None
    if lab is None:
        line = "lab: not loaded (lab-free command)"
    else:
        count = len(getattr(lab, "hosts", ()) or ())
        line = f"lab: {lab.name} ({count} host{'' if count == 1 else 's'})"
    if references:
        resolved = ", ".join(f"{ref.kind} {ref.name!r}" for ref in references)
        line = f"{line}; references resolve: {resolved}"
    return line


def print_dry_run_block(
    ctx: typer.Context,
    references: "list[LabReference] | None" = None,
    contacted: bool = False,
) -> None:
    """Print the dry run's product: what would run, and what it was checked against.

    Printed straight to the console, never through a logger. A dry run whose
    output is empty is a bug (spec §2), so the announcement must not be
    foldable by ``LogMode.QUIET``, a log level, or a capture filter — the
    payload is what gets suppressed under a dry run, never the announcement.

    *contacted* is ``True`` when ``--probe`` actually dialed at least one host,
    and it selects the headline. The default headline ends "and no device was
    contacted" — printing that immediately below a table reporting a host
    reachable would be a plain falsehood, and the one thing this contract
    cannot ship is a dry run that says something untrue about device contact.
    """
    from rich import print as rprint

    from ..utils import DRY_RUN_HEADLINE, DRY_RUN_HEADLINE_PROBED

    headline = DRY_RUN_HEADLINE_PROBED if contacted else DRY_RUN_HEADLINE
    rprint(f"[magenta]{escape(headline)}[/magenta]")
    rprint(f"  would run: {escape(would_run_line(ctx))}")
    rprint(f"  {escape(_lab_line(references or []))}")


def stop_at_dry_run_seam(ctx: typer.Context, spec: "CommandSpec") -> None:
    """Under ``--dry-run``, print what would run and exit 0 before the body.

    The default for every registered command, first- and third-party alike:
    zero author effort, and safe when the author never thought about dry runs
    at all. A command buys depth deliberately — ``dry_run_preview=True`` at
    registration (the whole group) or on the leaf's own ``@cli_exposed``
    stamp — and then owns its ``is_dry_run()`` branch.

    A no-op when this is not a dry run, which is every non-``-n`` invocation.

    ``--probe`` (spec §3) is the one thing that happens ahead of the preview
    check: the reachability table is printed for a previewing command
    (``link``/``tunnel``) and a seam-stopped one alike, and in both cases
    BEFORE the thing it informs. Without the flag not a single transport is
    opened here, which is what keeps every guard Tasks 5-5c added — "a dry run
    makes no device contact" — true by default.
    """
    if not dry_run_requested(ctx):
        return

    contacted = False
    references: "list[LabReference] | None" = None
    if probe_requested(ctx):
        from .probe import print_probe_report, probe_contacted, run_probe

        # Resolution first, and its failure still wins: dialing a host set
        # derived from an unresolvable reference would be acting on a
        # reference the command could not use.
        references = resolve_dry_run_references(ctx)
        results = run_probe(references)
        print_probe_report(results)
        # `probe_contacted`, not `bool(results)`: a result set that is entirely
        # `not probed`, or entirely LocalHost, opened no socket, and the probed
        # headline would then assert a connection nobody made.
        contacted = probe_contacted(results)

    if spec.dry_run_preview or _leaf_declares_preview(ctx):
        return
    # Before the print, not after: the resolution error is the answer when a
    # reference does not exist, and a block claiming a bad command "would run"
    # is exactly the fabrication this whole contract exists to remove.
    if references is None:
        references = resolve_dry_run_references(ctx)
    print_dry_run_block(ctx, references, contacted=contacted)
    raise typer.Exit(0)


DRY_RUN_DECLINE = "dry run: this command was not run"
"""Per-RESULT decline announcement, distinct from ``DRY_RUN_HEADLINE``.

The run-level headline makes a device-contact claim; this one is printed for a
single declined result, possibly after ``--probe`` already dialed, so it says
only what that result knows. See ``_render_dry_run_decline``.
"""

RENDER_POLICY_KEY = "_otto_render_policy"


@dataclasses.dataclass(frozen=True)
class RenderPolicy:
    """Per-invocation presentation a leaf installs on ``ctx.meta`` for the wrapper."""

    success: str | None = None
    """Message rendered (green) for an ok, non-command Result."""

    none_message: str | None = None
    """Message rendered (green) when the leaf returns None; None = silent."""


def _payload_or_decline(value: Any) -> Any:
    """Read a Result's payload, or report the decline and yield ``None``.

    A :class:`~otto.result.NotRunResult` raises on ``.value`` — deliberately,
    at the line that mistook a non-measurement for a measurement. That is
    right for a PARSER and wrong for a RENDERER: an error thrown at the print
    statement reports the mistake at a line that made none, which is the same
    misdirection the raising property exists to prevent, pointed the other
    way. So the renderer names the decline and prints nothing else.
    """
    from ..result import CommandNotRunError

    try:
        return value.value
    except CommandNotRunError as e:
        print_error(e)
        return None


def _render_dry_run_decline(value: Any) -> None:
    """Announce a result a dry run declined to produce, and never parse it.

    Exit code 0 is the point. ``NotRunResult.exit_code`` is 255 (ssh's
    "never connected"), which is the right answer to a LIBRARY caller asking
    whether the command ran, and the wrong answer to a USER who asked for a
    dry run and got exactly what they asked for. The seam stops most bodies
    long before here; this covers a command that opted into a preview and
    handed its decline back for rendering.

    The msg-less fallback deliberately does NOT reuse ``DRY_RUN_HEADLINE``.
    That constant ends "and no device was contacted" — a claim about the whole
    invocation, which a per-RESULT announcement is in no position to make, and
    which ``--dry-run --probe`` can falsify outright (spec §3): the preview path
    reaches this printer *after* the probe may have dialed. Saying only what
    this one result knows removes the claim instead of trying to keep it
    accurate from a function that cannot see the probe.
    """
    from rich import print as rprint

    if value.msg:
        rprint(f"[magenta]{escape(str(value.msg))}[/magenta]")
        return
    command = str(getattr(value, "command", "") or "")
    detail = f" ({escape(command)})" if command else ""
    rprint(f"[magenta]{escape(DRY_RUN_DECLINE)}{detail}[/magenta]")


def render_leaf_value(value: Any, policy: "RenderPolicy | None" = None) -> None:
    """Render a leaf command's return value and signal failure via exit code.

    Implements the documented "Return values" contract
    (``docs/library/extending-cli.md``) for every registered command and
    instruction: an ``otto.result`` family value derives the process exit code
    from its own ``exit_code`` (the ssh-like rules); any other non-``None``
    value is printed as-is, exit 0. ``None`` renders nothing by default —
    every side-effect-only first-party leaf returns it, so a leaf that wants a
    completion message must say so via :class:`RenderPolicy` (installed on
    ``ctx.meta[RENDER_POLICY_KEY]``, e.g. by the ``otto host`` verb bodies).

    A ``Status.NotRun`` result is a dry run's decline, not a failure: it is
    announced, never parsed, and never turned into a non-zero exit (the
    library-facing 255 answers a different question than the user's).
    """
    from rich import print as rprint

    from ..result import CommandResult, Result, Results
    from ..utils import Status

    success = policy.success if policy else None

    if isinstance(value, Result):
        if value.status is Status.NotRun:
            _render_dry_run_decline(value)
            return
        is_command = isinstance(value, (CommandResult, Results))
        if value.is_ok:
            if is_command:
                pass  # command output already streamed during execution
            elif success:
                rprint(f"[green]{success}[/green]")
            else:
                payload = _payload_or_decline(value)
                if isinstance(payload, dict):
                    for src, entry in payload.items():
                        rprint(f"{src} -> {_payload_or_decline(entry)}")
                elif isinstance(payload, list):
                    for item in payload:
                        rprint(item)
                elif payload is not None:
                    rprint(payload)
            return
        if value.msg:
            print_error(value.msg)
        payload = _payload_or_decline(value)
        if isinstance(payload, dict):
            for entry in payload.values():
                if isinstance(entry, Result) and not entry.is_ok and entry.msg:
                    print_error(entry.msg)
        elif isinstance(value, Results):
            for entry in value:
                if not entry.is_ok and entry.msg:
                    print_error(entry.msg)
        raise typer.Exit(value.exit_code)

    if value is None:
        if policy is not None and policy.none_message is not None:
            rprint(f"[green]{policy.none_message}[/green]")
        return

    rprint(value)  # documented third-party plain-value fallback, exit 0


def _require_async_leaf(cmd: Any, spec: "CommandSpec") -> None:
    """Refuse a sync leaf under a command whose lane demands coroutines.

    Only a coroutine reaches the bridge below, so a sync leaf runs with no
    host-scope entry and no interrupt policy. ``@instruction`` checks this at
    its own decorator, but that is the SUGAR: a directly-registered
    ``InstructionEntry``, an ``@run_app.command()``, and a sub-group added
    with ``add_typer`` all reach ``otto run`` without passing it.

    Checked HERE — at invocation — rather than where commands resolve, and
    that placement is the whole point. ``TyperGroup.format_commands`` resolves
    every child to render a help table, so a resolve-time check makes
    ``otto run --help`` traceback for a user whose plugin ships one bad leaf,
    hiding the very list that would identify it. Completion has the same
    problem and would additionally false-positive on the synthesized stubs the
    cache serves. Invocation happens on exactly one path, reaches every leaf
    however it was registered, and cannot fire on a read-only one.

    One ``__wrapped__`` level, matching the group-callback guard: that is the
    function typer will actually call.
    """
    callback = getattr(cmd, "callback", None)
    if callback is None:
        return
    if inspect.iscoroutinefunction(getattr(callback, "__wrapped__", callback)):
        return
    raise TypeError(
        f"{spec.name} command {getattr(cmd, 'name', '?')!r} is a plain `def`: only a "
        "coroutine reaches otto's lifecycle bridge, so its hosts are never swept and "
        "an interrupt never becomes a clean exit. Write it as `async def` — a body "
        "with nothing to await is still correct."
    )


def _wrap_invoke(cmd: Any, spec: "CommandSpec") -> Any:
    """Wrap a single leaf command's ``invoke``: preamble + async-leaf bridge (idempotent)."""
    if getattr(cmd, "_otto_preambled", False):
        return cmd
    cmd._otto_preambled = True  # noqa: SLF001 — own marker attribute on the command object
    original_invoke = cmd.invoke

    def _invoke_with_preamble(inner_ctx: Any) -> Any:
        # Before the preamble, so a refused command creates no output dir and
        # loads no lab.
        if spec.async_leaves:
            _require_async_leaf(cmd, spec)
        # Restamp on the leaf's own (inner) ctx: ctx.meta is shared by-reference
        # down the click context chain, but the spec must reflect THIS leaf.
        inner_ctx.meta["_otto_command_spec"] = spec
        command_preamble(inner_ctx)
        result = original_invoke(inner_ctx)
        # The lifecycle bridge (wave 2 of the command-lifecycle-uniformity
        # spec): a plain ``async def`` leaf — typer never awaits callbacks, so
        # its invoke returns the coroutine object — runs under the full
        # command policy (host-scope entry, two-stage interrupts, teardown
        # deadline) with REGISTRATION as the only opt-in. Detection is on the
        # invoke RESULT, not the callback: typer wraps every callback in its
        # own sync shim, so ``iscoroutinefunction(cmd.callback)`` is always
        # False, while the coroutine itself passes through untouched.
        # Naturally idempotent: a leaf that self-bridges (``run_command``
        # inside a sync wrapper — the retired ``@async_typer_command``
        # migration pattern) returns a plain value and is skipped — no double
        # ``asyncio.run`` is reachable.
        if inspect.iscoroutine(result):
            from ..lifecycle import run_command

            result = run_command(result)
        # Rendering happens AFTER the bridge (on the awaited value), never on
        # --help paths (help exits during parse, before ``Command.invoke``),
        # and a leaf that raises (typer.Exit, SystemExit(128+n) from the
        # policy) never reaches it.
        render_leaf_value(result, inner_ctx.meta.get(RENDER_POLICY_KEY))
        return result

    cmd.invoke = _invoke_with_preamble
    return cmd


def _wrap_get_help(cmd: Any) -> None:
    """Wrap *cmd*'s ``get_help`` to print the banner first (idempotent).

    Covers every way help text gets rendered: the eager ``--help``/``-h``
    option, ``no_args_is_help``, and otto's own manual ``rprint(ctx.get_help())``
    call sites (e.g. ``otto host``'s class-scoped menu, bare ``otto monitor``)
    — they all resolve through ``ctx.get_help()`` → ``ctx.command.get_help(ctx)``.
    """
    if getattr(cmd, "_otto_help_wrapped", False):
        return
    cmd._otto_help_wrapped = True  # noqa: SLF001 — own marker attribute on the command object
    original_get_help = cmd.get_help

    def _get_help_with_banner(inner_ctx: Any) -> str:
        ensure_help_banner(inner_ctx)
        return original_get_help(inner_ctx)

    cmd.get_help = _get_help_with_banner


def wrap_leaf_callbacks(cmd: Any, spec: "CommandSpec") -> Any:
    """Wrap every leaf command under *cmd* so its invoke runs the preamble first.

    Wrapping ``Command.invoke`` (not the callback) means the preamble runs
    only on real execution: a ``--help`` on the leaf exits during parse and
    never reaches ``invoke``. Groups recurse into their static subcommands AND
    wrap their ``get_command`` so lazily-synthesized subcommands (e.g. the
    dynamic ``otto host <verb>`` commands) are wrapped on resolution too.
    Already-wrapped commands are skipped (resolution results are cached).
    Also wraps every node's ``get_help`` (see ``_wrap_get_help``) so any
    help screen reached through this tree shows the banner.
    """
    if getattr(cmd, "_otto_preambled", False):
        return cmd
    _wrap_get_help(cmd)
    if not hasattr(cmd, "commands"):
        return _wrap_invoke(cmd, spec)

    cmd._otto_preambled = True  # noqa: SLF001 — own marker attribute on the command object

    # A GROUP's own callback can never reach the lifecycle bridge: the
    # vendored click fork runs it via an unbound class call and DISCARDS its
    # return value (TyperGroup.invoke → super().invoke), so an ``async def``
    # group callback would silently no-op with exit 0 — the exact failure
    # class the bridge exists to kill. Reject it loudly at wrap time. One
    # ``__wrapped__`` level only (typer's get_callback update_wrapper's the
    # registered function): that is exactly what typer will call
    # synchronously, so the check cannot false-positive on a user's own
    # sync-bridging decorator.
    group_cb = getattr(cmd, "callback", None)
    if group_cb is not None and inspect.iscoroutinefunction(
        getattr(group_cb, "__wrapped__", group_cb)
    ):
        raise TypeError(
            f"async group callback on command group {cmd.name!r}: typer discards a "
            "group callback's return value, so it runs outside otto's lifecycle "
            "bridge and would silently do nothing — write group/root callbacks "
            "as plain `def` (only leaf commands may be `async def`)"
        )

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
    ``suite_app`` usable standalone without going through the full ``otto``
    root app (unit tests drive them through the same seam via
    ``tests/_fixtures/dispatch.DispatchRunner``; note a plain ``async def``
    leaf needs that wrapper — bare, it fails loudly with an un-awaited
    coroutine) while ``otto run smoke`` / ``otto test TestX`` still get the
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
