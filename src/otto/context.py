"""otto's per-invocation runtime composition root.

Owns the active Lab and the run's policy (:class:`RunPolicy`, from the
``otto.invocation`` leaf, which also keeps each event loop's host registry).
Propagated via a ContextVar so the bare module accessors
(otto.lab.all_hosts/get_host) can stay zero-argument, while explicit passing
(OttoContext methods, open_context) is first-class.
"""

import asyncio
import logging
import re
from collections.abc import AsyncIterator, Awaitable, Callable, Iterator
from contextlib import asynccontextmanager, contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, TypeVar, cast

from .host.host import DEFAULT_COMMAND_TIMEOUT
from .invocation import ContextBinding, HostResolver, RunPolicy, Variant

if TYPE_CHECKING:
    from pathlib import Path

    from _typeshed import DataclassInstance

    from .bootstrap import BootstrapResult
    from .config.lab import Lab
    from .config.repo import Repo
    from .config.scope import ProjectScope
    from .host import Results, UnixHost
    from .host.remote_host import RemoteHost
    from .params import OptionsSource

__all__ = [
    "ContextBinding",
    "HostResolver",
    "OttoContext",
    "ProjectContextView",
    "RunPolicy",
    "Variant",
    "get_context",
    "open_context",
    "reset_context",
    "reset_variant",
    "set_context",
    "set_variant",
    "try_get_context",
    "variant",
]

T = TypeVar("T")

logger = logging.getLogger(__name__)

LIBRARY_LAB_NAME = "<library>"
"""Sentinel ``Lab.name`` for the minimal, host-less context a library caller
gets for free.

``otto.suite.run._session_context`` installs ``OttoContext(lab=Lab(name=LIBRARY_LAB_NAME))``
around a session when no context is already active (e.g. ``run_tests()``
called outside ``async with otto.open_context(...)``). That
Lab carries no hosts, so any ``get_host()`` call inside such a run fails loud —
:meth:`OttoContext.get_host` checks this constant to append a hint pointing at
``open_context`` (see below) ONLY for that sentinel lab; a real, lab-backed
unknown-host error is untouched.

Lives here rather than in ``otto.suite.run`` (where the sentinel is actually
installed) because this is the lower module in the import graph: ``otto.suite``
already imports ``otto.context``, and ``otto.context`` must never import from
``otto.suite`` (that would cycle back through ``otto.suite.run``'s own
``from ..context import ...``). Defining the shared constant on the
already-imported side keeps both directions acyclic.
"""


_active: ContextVar["OttoContext | None"] = ContextVar("otto_context", default=None)


def get_context() -> "OttoContext":
    """Return the active ``OttoContext``, raising ``RuntimeError`` if none is installed."""
    ctx = _active.get()
    if ctx is None:
        raise RuntimeError(
            "No active OttoContext. Inside the CLI this is built by the top-level "
            "callback; in a script wrap your work in `async with otto.open_context(...)`."
        )
    return ctx


def try_get_context() -> "OttoContext | None":
    """Return the active ``OttoContext``, or ``None`` if none is installed."""
    return _active.get()


def set_context(ctx: "OttoContext") -> ContextBinding:
    """Install *ctx*, its run policy and a live resolver over its lab; return the binding.

    Hand the binding to :func:`reset_context`, in the same execution context.
    Installing acquires no cleanup boundary. A hand-built context on a loop you
    drive yourself closes its hosts with :meth:`OttoContext.sweep_loop`; under
    ``open_context``, ``run_command`` or ``otto test`` a boundary holds the
    loop, so that raises and the boundary closes them instead.
    """
    from .invocation import install_policy, install_resolver, install_var

    policy = ctx.policy  # first: a non-context fails before anything is installed
    binding = install_var(_active, ctx)
    install_policy(policy, into=binding)
    install_resolver(_ContextResolver(ctx), into=binding)
    return binding


def reset_context(token: ContextBinding) -> None:
    """Undo the matching :func:`set_context`: resolver, policy, then context, exactly once."""
    from .invocation import reset_binding

    reset_binding(token)


def variant() -> Variant:
    """Return the run's product variant: ``"debug"`` unless the run chose ``"field"``.

    Read off the installed :class:`RunPolicy` (a fresh default when none is
    installed), so ingest, which runs before any context exists, and providers
    read one value: the one :meth:`otto.declared.KindBuilder.build` picks
    between same-name entries by.
    """
    from .invocation import current_policy

    return current_policy().variant


def set_variant(value: Variant) -> ContextBinding:
    """Choose the run's variant before any context exists; return the binding to reset.

    For a script's manual pattern: ``set_variant("field")``, then build and
    :func:`set_context` an :class:`OttoContext`, which copies the variant.

    Raises:
        ValueError: *value* is not ``"debug"`` or ``"field"``.
        RuntimeError: a context is installed. Its policy is what hosts read,
            and a second policy would split it from :attr:`OttoContext.policy`;
            open a nested ``open_context(variant=...)`` to run under another
            variant.
    """
    import dataclasses

    from .invocation import check_variant, current_policy, install_policy

    checked = check_variant(value)
    if _active.get() is not None:
        raise RuntimeError(
            "set_variant cannot change the variant while a context is installed; "
            "run that work inside a nested `async with otto.open_context(variant=...)`"
        )
    return install_policy(dataclasses.replace(current_policy(), variant=checked))


def reset_variant(token: ContextBinding) -> None:
    """Undo the matching :func:`set_variant`, exactly once."""
    from .invocation import reset_binding

    reset_binding(token)


class _ContextResolver:
    """The installed peer-host resolver: the context's lab, read live (a later ``ctx.lab`` too)."""

    def __init__(self, ctx: "OttoContext") -> None:
        self._ctx = ctx

    @property
    def name(self) -> str:
        return self._ctx.lab.name

    @property
    def hosts(self) -> "dict[str, Any]":
        return self._ctx.lab.hosts


def _flags_hiding_every_match(
    matched: "list[Any]",
    *,
    include_containers: bool,
    include_local: bool,
) -> "list[str]":
    """Name the membership flags that hold back EVERY host in *matched*.

    The prediction behind the second half of D6's empty-selection guard (see
    :meth:`OttoContext.all_hosts`): a pattern matched, and the walk is about to
    yield nothing anyway because container hosts and the built-in ``local``
    host are not fleet members unless asked for. Answering "which flag would
    admit them" is what lets the error name the one edit that fixes it.

    Short-circuits on the first host that IS a fleet member — one survivor
    means the walk is not empty, so there is nothing to explain and no reason
    to keep classifying the rest.

    Which flag holds a host out is ``otto.config.fleet._flag_holding_out``'s
    answer: the same rule the walk's yield loop and
    :func:`otto.config.fleet.fleet_of_interest` apply, so this prediction and
    the loop cannot disagree about a host.

    Args:
        matched: The hosts the pattern fullmatched, taken from the same lab
            mapping the walk itself iterates.
        include_containers: The walk's container flag, as passed by the caller.
        include_local: The walk's ``local`` flag, as passed by the caller.

    Returns:
        The flag names — sorted, so the message is stable — that between them
        hid every match. Empty when at least one match survives the flags,
        which is also what an empty *matched* returns: no matches is the OTHER
        failure, and it is already spoken for by the plain D6 message.
    """
    from .config.fleet import _flag_holding_out

    hiding: set[str] = set()
    for host in matched:
        flag = _flag_holding_out(
            host, include_containers=include_containers, include_local=include_local
        )
        if flag is None:
            return []
        hiding.add(flag)
    return sorted(hiding)


@dataclass(init=False)
class OttoContext:
    """The active per-invocation runtime: chosen lab and run policy."""

    lab: "Lab"
    policy: "RunPolicy"
    """The run's policy, read live by every host; the same object :func:`set_context` installs."""
    cov_decision: "bool | None" = None
    """Whether coverage is on for this run, once decided; ``None`` until then.

    Read :attr:`cov`, not this. ``otto test`` is the one writer: it stamps its
    resolved ``--cov``/``--no-cov``/auto decision here for the length of the
    run (:func:`otto.suite.run.run_tests`).
    Left ``None``, the first read of :attr:`cov` detects the answer and
    stores it here.
    """
    include_projects: tuple[str, ...] = ()
    """Repo names forced ACTIVE this invocation (``-I``), PEP-503-normalized ON READ.

    Populated from the root CLI callback via ``RootOptions``; empty for
    library contexts. Read only through :func:`otto.config.scope.active` —
    nothing else may re-derive activation from these tuples.

    Nothing normalizes on WRITE. The constructor stores what it is given, so
    any caller may store whatever spelling it holds and the stored tuple is
    NOT guaranteed normalized;
    :func:`otto.config.scope.active` normalizes both the stored values and the
    queried name before comparing. The invariant is therefore enforced where it
    is read rather than merely asked for here — a docstring-only version would
    let ``exclude_projects=("My_Repo",)`` be silently ignored, which is an
    explicit switch failing OPEN.
    """

    exclude_projects: tuple[str, ...] = ()
    """Repo names forced INACTIVE this invocation (``-E``), PEP-503-normalized ON READ.

    Same write/read contract as :attr:`include_projects`. Read by
    :func:`otto.config.scope.active` for the verdict, by
    :func:`otto.config.scope.switched_off` for attribution, and on the fleet
    side by :meth:`otto.context.OttoContext.admissible_ids` (through
    :func:`otto.config.scope.scoped_ids`), which leaves a switched-off repo's
    hosts out of play.
    """

    verb: "str | None" = field(default=None, init=False)
    """The verb this invocation dispatched (``"run"`` or ``"test"``), once bound.

    ``None`` until :meth:`bind_verb_options` runs — a library context that
    never dispatches a verb keeps this ``None`` forever, and :meth:`options`
    reports that plainly rather than guessing.
    """

    _verb_options: "dict[type, Any]" = field(default_factory=dict, init=False, repr=False)
    """This invocation's built instance of each options class registered for :attr:`verb`."""

    _verb_option_kwargs: "dict[str, Any]" = field(default_factory=dict, init=False, repr=False)
    """The flat parsed kwargs :meth:`bind_verb_options` was last called with.

    Kept so :meth:`verb_option_source` can replay them for a class that was
    not itself part of the bound verb's build — e.g. a project instruction
    body building its own options class that shares fields with a bound one.
    """

    _scopes_refusal: "BaseException | None" = field(
        default=None, init=False, repr=False, compare=False
    )
    """What :meth:`_resolve_scopes` raised, re-raised on every later read.

    Cached beside the verdicts, so a refused read never re-runs the load-error
    classifier on every walk.
    """

    _bootstrap_result: "BootstrapResult | None" = field(
        default=None, init=False, repr=False, compare=False
    )
    """The composition root's result the run already holds, as given to the constructor."""

    _bootstrap_refusal: "BaseException | None" = field(
        default=None, init=False, repr=False, compare=False
    )
    """What reading the composition root raised, re-raised on every later read."""

    _scope_verdicts: "dict[str, ProjectScope] | None" = field(
        default=None, init=False, repr=False, compare=False
    )
    """The repos' scope verdicts, once resolved."""

    def __init__(
        self,
        lab: "Lab",
        *,
        cov_decision: "bool | None" = None,
        include_projects: tuple[str, ...] = (),
        exclude_projects: tuple[str, ...] = (),
        policy: "RunPolicy | None" = None,
        bootstrap: "BootstrapResult | None" = None,
    ) -> None:
        """Build a context over *lab*; the run flags come only from *policy*.

        Without *policy*, a fresh :class:`RunPolicy` with the default flags
        (not dry-run, command output logged, no output directory) copies the
        variant and teardown deadline of
        :func:`otto.invocation.current_policy` (the two values that were
        ambient before contexts carried a policy); it never copies another
        context's flags. With *policy*, that object is used as given: to run
        a hand-built context dry, pass ``policy=RunPolicy(dry_run=True)``.
        *bootstrap* is the composition root's result this run already holds
        (the CLI and ``open_context`` pass theirs); without it, the first read
        of :attr:`repos`, :attr:`ordered_repos` or the scope verdicts calls
        ``otto.bootstrap.bootstrap()`` once, and its outcome, a refusal
        included, is kept for the context's life.
        ``dataclasses.replace`` is not supported on a context.
        """
        from .invocation import RunPolicy, current_policy

        if policy is None:
            ambient = current_policy()
            policy = RunPolicy(variant=ambient.variant, teardown_deadline=ambient.teardown_deadline)
        self.lab = lab
        self.policy = policy
        self.cov_decision = cov_decision
        self.include_projects = include_projects
        self.exclude_projects = exclude_projects
        self.verb = None
        self._verb_options = {}
        self._verb_option_kwargs = {}
        self._scopes_refusal = None
        self._bootstrap_result = bootstrap
        self._bootstrap_refusal = None
        self._scope_verdicts = None

    @property
    def dry_run(self) -> bool:
        """Whether this run declines device-changing commands, read live off :attr:`policy`.

        Read-only. Set it on the policy (``ctx.policy.dry_run = True``), or build
        the context with ``policy=RunPolicy(dry_run=True)``.
        """
        return self.policy.dry_run

    @property
    def log_command_output(self) -> bool:
        """Whether hosts log each command and its output, read live off :attr:`policy`.

        Read-only. Set it on the policy (``ctx.policy.log_command_output = False``),
        or suppress it for a block with :class:`otto.host.host.SuppressCommandOutput`.
        """
        return self.policy.log_command_output

    @property
    def output_dir(self) -> "Path | None":
        """The run's output directory, read live off :attr:`policy`.

        Read-only. Set it on the policy (``ctx.policy.output_dir = path``), or
        build the context with ``policy=RunPolicy(output_dir=path)``.
        """
        return self.policy.output_dir

    def bind_verb_options(self, verb: str, kwargs: "dict[str, Any]") -> None:
        """Build every options class registered for *verb* from the parsed *kwargs*.

        Each registered class is constructed with
        ``OptionsSource.from_kwargs(kwargs).build(cls)``, which raises
        ``otto.params.OptionsValidationError`` (not a pydantic
        ``ValidationError``) on a bad value; the CLI translates that to a
        usage error at its boundary (``otto.cli.invoke.usage_error_from``)
        before any command body runs. Calling this a second time replaces the
        earlier binding outright — nothing is merged across calls.
        """
        from .params import OptionsSource, verb_option_classes

        source = OptionsSource.from_kwargs(kwargs)
        built = {
            origin.cls: source.build(cast("type[DataclassInstance]", origin.cls))
            for origin in verb_option_classes(verb)
        }
        self.verb, self._verb_options, self._verb_option_kwargs = verb, built, dict(kwargs)

    @contextmanager
    def verb_binding_preserved(self) -> "Iterator[None]":
        """Restore this context's verb binding, exactly as it was, when the block exits.

        For a caller that binds a verb's options on a context it does not own
        -- :func:`otto.suite.run.run_tests` binds ``test`` on the caller's
        active context -- and must hand it back unchanged: the bound verb, its
        built instances and its parsed flags all come back, whether the block
        returns or raises. Nothing is merged; a binding made inside the block
        is simply discarded.
        """
        saved = (self.verb, dict(self._verb_options), dict(self._verb_option_kwargs))
        try:
            yield
        finally:
            self.verb, self._verb_options, self._verb_option_kwargs = saved

    def options(self, cls: "type[T]") -> "T":
        """Return this invocation's instance of the registered options class *cls*.

        Raises:
            otto.params.OptionsNotAvailableError: *cls* is not registered at
                all; no verb is bound in this context yet; *cls* is
                registered for a verb other than the one bound here; or it
                was registered for that verb after the verb was bound.
        """
        from .params import OptionsNotAvailableError, verbs_for

        if cls in self._verb_options:
            return cast("T", self._verb_options[cls])
        verbs = verbs_for(cls)
        if verbs is None:
            raise OptionsNotAvailableError(
                f"{cls.__qualname__} is not registered; declare it with "
                "@otto.options(verbs=[...]) in an init module"
            )
        if self.verb is None:
            raise OptionsNotAvailableError(
                f"no verb's options are bound in this context, so {cls.__qualname__} has no value"
            )
        if self.verb in verbs:
            raise OptionsNotAvailableError(
                f"{cls.__qualname__} was registered after otto {self.verb} bound its options"
            )
        raise OptionsNotAvailableError(
            f"{cls.__qualname__} is registered for {', '.join(verbs)}, not {self.verb}"
        )

    def verb_option_source(self) -> "OptionsSource":
        """Return the parsed flags of the bound verb, for building a body's options class.

        With nothing bound, returns ``OptionsSource.from_kwargs({})`` — every
        class built from it takes its own defaults, rather than this raising
        for a context that never dispatched a verb.
        """
        from .params import OptionsSource

        return OptionsSource.from_kwargs(self._verb_option_kwargs)

    @property
    def cov(self) -> bool:
        """Whether this lab is in coverage mode — informational, never an action.

        ``otto test``'s decision when it made one (:attr:`cov_decision`).
        Otherwise it is detected on first read with ``otto test``'s auto rule:
        some product on a ``[coverage].hosts`` host is an instrumented build
        AND a ``[coverage]`` table is configured. That is the same local
        artifact scan ``otto test`` runs, and no host is contacted. The answer
        is cached for this context. A test, a fixture or an instruction reads
        it to avoid destroying counters that a coverage run still needs, e.g.
        to leave ``.gcda`` files in place instead of uninstalling a product.
        Nothing cleans or collects coverage because of it.

        Lazy on purpose: a command that never asks pays for neither the scan
        nor the coverage import.
        """
        if self.cov_decision is None:
            self.cov_decision = self._detect_cov()
        return self.cov_decision

    def _detect_cov(self) -> bool:
        """Apply ``otto test``'s auto rule to this context's lab; ``False`` if it cannot.

        Never raises for a misconfiguration. A caller that only asked a
        question must not die of it, so a broken ``[coverage].hosts`` selector
        is one warning and ``False``, exactly as it is for an auto
        ``otto test``. So is a repo that fails to load: the walk that finds the
        coverage hosts refuses with :class:`otto.session.RepoLoadError`, and
        turning coverage off is the narrowing direction, never a widening. The
        sentinel library lab and unreachable repos have no coverage
        configuration to find and answer ``False`` quietly.
        """
        if self.lab.name == LIBRARY_LAB_NAME:
            return False
        try:
            repos = self.repos
        except Exception as exc:  # noqa: BLE001 — no repos reachable ⇒ no [coverage] ⇒ off
            logger.debug(f"otto: coverage detection unavailable ({exc!r}); ctx.cov is False")
            return False
        from .bootstrap import ProjectScopeError
        from .config.coverage_settings import (
            CoverageConfigError,
            get_cov_config,
            load_hosts_pattern,
        )
        from .config.scope import EmptySelectionError
        from .session.errors import RepoLoadError

        cov_config = get_cov_config(repos)
        if not cov_config:
            return False
        try:
            pattern = load_hosts_pattern(cov_config)
            hosts = list(self.all_hosts(pattern=pattern, include_containers=True))
        except (EmptySelectionError, CoverageConfigError, ProjectScopeError, RepoLoadError) as exc:
            from rich.markup import escape as escape_markup

            # escape_markup: the message quotes a literal bracket ("[coverage].hosts",
            # or the user's own regex) and the console handler renders markup.
            reason = escape_markup(str(exc))
            logger.warning(f"otto: coverage detection failed, ctx.cov is False: {reason}")
            return False
        # The verdict otto.coverage.instrumentation.detect() reports as "yes" —
        # asked of the products directly, because this layer sits beneath the
        # coverage pipeline (tach) and needs no report, only the answer.
        return any(product.instrumented() is True for host in hosts for product in host.products)

    def get_host(self, host_id: str, **overrides: Any) -> "UnixHost":
        """Look up *host_id* in the active lab and apply any keyword overrides.

        Handing a host out registers nothing: the host registers with the
        event loop it connects on, and that loop's cleanup closes it.
        """
        from .config.fleet import _apply_option_overrides

        host = self.lab.hosts.get(host_id)
        if host is None:
            # The sentinel LIBRARY_LAB_NAME lab is what run_tests() installs
            # for a library caller with no active context (see
            # otto.suite.run._session_context) — it never carries hosts, so
            # get_host() always fails here. Point a caller who hits this at the
            # real fix (open_context) rather than leaving them staring at an
            # empty "Available: []". A normal, lab-backed miss is untouched.
            breadcrumb = (
                " — no lab is loaded; run inside 'async with otto.open_context(lab=...)'"
                if self.lab.name == LIBRARY_LAB_NAME
                else ""
            )
            raise KeyError(
                f"No host {host_id!r} in lab {self.lab.name!r}. "
                f"Available: {sorted(self.lab.hosts)}{breadcrumb}"
            )
        resolved = _apply_option_overrides(cast("Any", host), **overrides)
        return cast("UnixHost", resolved)

    async def sweep_loop(
        self, loop: asyncio.AbstractEventLoop, *, label: str, deadline: float | None = None
    ) -> list[str]:
        """Close every host registered on *loop* now, while it runs; for a loop you drive yourself.

        Meant for a script that drives its own loop with a hand-built context
        and no cleanup boundary. Inside ``open_context``, ``run_command`` or
        ``otto test`` the boundary closes the loop's hosts instead, so this
        raises there. *label* names the loop in the debug line. Returns the
        ids of the hosts that closed; a failed close is a warning.

        *deadline* bounds the sweep in seconds (``None`` waits for every
        close). On expiry the sweep is cancelled, and after a short grace
        cancelled again; a warning names the hosts not closed, whose
        connections are dropped so each reconnects on its next use.

        Raises:
            RuntimeError: *loop* is not the running loop; a cleanup boundary
                holds it; it is already being swept; or its runner shut it down.
        """
        if loop is not asyncio.get_running_loop():
            raise RuntimeError(
                f"sweep_loop can only sweep the running loop, but {label} is not the "
                "loop running here; await it on that loop, before the loop closes"
            )
        from .invocation import sweep_unheld

        return await sweep_unheld(loop, label=label, deadline=deadline)

    def _bootstrap_snapshot(self) -> "BootstrapResult":
        """Return the one result this context reads its repos and load errors from.

        Read on first use when the constructor was given none; a refusal is
        cached and re-raised on every later read.
        """
        if self._bootstrap_refusal is not None:
            raise self._bootstrap_refusal
        if self._bootstrap_result is None:
            from .bootstrap import bootstrap  # function-scope: the composition root

            try:
                self._bootstrap_result = bootstrap()
            except Exception as exc:  # cached: an unavailable bootstrap stays unavailable
                self._bootstrap_refusal = exc
                raise
        return self._bootstrap_result

    @property
    def repos(self) -> "list[Repo]":
        """Every repo this run parsed, a dependency-skipped one included.

        A fresh list on every read: changing it changes nothing here. A list
        of repos is not deeply immutable; what is promised is the membership
        and the order. With no SUT directories it is empty ("known empty").

        Raises:
            Exception: whatever discovery raised, when the repos cannot be
                read ("unavailable"); the same exception on every read.
        """
        return list(self._bootstrap_snapshot().repos)

    @property
    def ordered_repos(self) -> "list[Repo]":
        """The repos a fleet-wide walk visits, in dependency order, skipped repos absent.

        A fresh list on every read, from the same result as :attr:`repos`.
        """
        return list(self._bootstrap_snapshot().ordered_repos)

    def _resolve_scopes(self) -> "dict[str, ProjectScope]":
        """Resolve the per-repo scope verdicts once and cache them; read them with ``scopes_of``.

        Each repo's resolved fleet of interest for this run, keyed by
        ``Repo.name``. Display and abort data: ``status --full`` renders it
        and :func:`~otto.config.scope.require_current_scope` refuses on it.
        Fleet iteration does NOT read the stored
        :attr:`~otto.config.scope.ProjectScope.universe`; it re-asks
        :func:`~otto.config.scope.repo_targets` per host through
        :func:`~otto.config.scope.scoped_ids`, so a container registered after
        the verdicts were first resolved is still scoped correctly.

        Lazy, not computed at construction, because resolving them needs the
        repos, which come from :attr:`repos`'s one bootstrap result. A context
        is built in plenty of places that never walk a fleet (library
        FD-model callers, the ``LIBRARY_LAB_NAME`` sentinel session, unit
        tests holding a hand-built ``Lab``), and making every one of them pay
        for, or fail on, a composition root it never asked for would be the
        wrong trade.

        The verdicts cover every repo whose settings parsed, a repo the
        dependency pass skipped included: a skip changes what a repo
        registers, never what it declared, so it cannot make a declaration
        vanish and widen the walk. Two worlds answer an empty mapping, which
        feeds the whole-lab fallback: the ``LIBRARY_LAB_NAME`` sentinel lab,
        and a run with no SUT directories at all (no repos, so nothing
        declared and nothing to narrow).

        A walk never widens because the repos could not be read:

        * a load error that :func:`otto.session.check_repos` would treat as
          fatal (an unparseable ``settings.toml``, or any error of an active
          repo) raises :class:`otto.session.RepoLoadError`, decided by the
          same classifier, so a hand-built context that never ran
          ``check_repos`` refuses where the CLI would have. A switched-off or
          lab-inactive repo's error only demotes it, and its declaration
          still counts;
        * an environment failure (``OTTO_SUT_DIRS`` naming a directory that
          does not exist) propagates as raised.

        A refusal is cached: every later read re-raises the same exception
        without running the classifier or ``bootstrap()`` again.

        Raises:
            otto.session.RepoLoadError: A load error that stops a run.
        """
        if self._scopes_refusal is not None:
            raise self._scopes_refusal
        if self._scope_verdicts is None:
            if self.lab.name == LIBRARY_LAB_NAME:
                self._scope_verdicts = {}
            else:
                try:
                    self._scope_verdicts = self._resolve_verdicts()
                except Exception as exc:  # cached: a refused read must not re-run the classifier
                    self._scopes_refusal = exc
                    raise
        return self._scope_verdicts

    def _resolve_verdicts(self) -> "dict[str, ProjectScope]":
        """Resolve every parsed repo's verdict, after refusing a load error that is fatal here."""
        from .config.scope import _not_switched_off, resolve_scopes
        from .host.builtin_hosts import BUILTIN_LOCAL_HOST_ID
        from .session.errors import RepoLoadError
        from .session.projects import classify_load_errors

        result = self._bootstrap_snapshot()
        verdicts = classify_load_errors(
            result,
            list(self.lab.component_names),
            include=list(self.include_projects),
            exclude=list(self.exclude_projects),
        )
        if verdicts.fatal:
            raise RepoLoadError(verdicts.fatal, verdicts.demoted)
        scopes = resolve_scopes(
            result.repos,  # every parsed repo: a dependency skip never hides a declaration
            self.lab.component_names,
            self.lab.hosts,
            # `local` is the runner, never fleet — and its `source_lab` is
            # stamped with whichever component the CLI listed first, so keying
            # membership on that stamp would make `-l a+b` and `-l b+a` resolve
            # differently. `include_local=True` remains the walk's own opt-in.
            exclude_ids=frozenset({BUILTIN_LOCAL_HOST_ID}),
        )
        declared = [scope for scope in scopes.values() if scope.declared]
        if declared:
            # Once per context, and only when something actually narrowed: the
            # fallback is not news, and a line printed on every run is a line
            # nobody reads on the run that mattered.
            # A repo switched off with -E puts no host in play, so its
            # universe is not counted (``scoped_ids`` drops it the same way).
            active = _not_switched_off(declared, self.exclude_projects)
            union = frozenset().union(*(scope.universe for scope in active))
            excluded = sum(1 for scope in scopes.values() if scope.excluded)
            logger.info(
                f"fleet of interest: {len(union)} of {len(self.lab.hosts)} lab hosts "
                f"({len(scopes)} repo(s), {excluded} excluded)"
            )
        return scopes

    def admissible_ids(
        self, owner: "str | None" = None, *, require_nonempty: bool = True
    ) -> "set[str]":
        """Host ids this run's fleet walks may reach, re-derived live for *owner*.

        The one place the two fleet surfaces get their base set from, so
        widening one cannot silently leave the other behind.

        Public since spec 2026-08-28 three-level-reservations §5: the
        reservation gate reads the same set every fleet walk starts from — one
        definition, two readers. A repo in :attr:`exclude_projects` contributes
        no host to it, so ``-E`` narrows both readers at once.

        ``require_nonempty=False`` is the RESERVATION readers' spelling —
        :meth:`otto.reservations.check.ReservationGate.evaluate` (through
        :func:`otto.config.fleet.get_hosts_in_play`), ``otto reservation
        check``, and the completer's gate. They ask which hosts are IN PLAY
        (spec §5) and nothing more: an empty declared fleet means nothing is in
        play, which is a legal answer — the requirement narrows to the
        lab-level set — not a condition to abort on. The refusal stays with the
        WALK, which is the surface that would silently touch nothing; a run
        that walks still aborts with the same fleet-shaped message, just after
        the gate rather than inside it. The unknown-*owner* refusal lives in
        :func:`~otto.config.scope.scoped_ids` and fires either way: a caller's
        typo would fall back to the whole lab, which is the silent widening
        this scoping exists to prevent.

        Args:
            owner: ``Repo.name`` whose universe bounds the walk, or ``None``
                (the default) for the union across declaring repos.
            require_nonempty: Refuse an empty base set (the default, for every
                fleet walk). ``False`` returns it as the answer it is.

        Returns:
            The admissible ids — possibly empty when *require_nonempty* is
            ``False``.

        Raises:
            otto.bootstrap.ProjectScopeError: Nothing is admissible while some
                repo declared a ``[project]`` scope (spec §10 row 5) — framed
                against *owner* when the walk was bound to one, so a repo whose
                own fleet is empty is not reported as the whole fleet's;
                suppressed by ``require_nonempty=False``. Also when *owner*
                names a repo this run never resolved, which no flag suppresses.
            otto.session.RepoLoadError: The scope verdicts refused: a repo failed
                to load in a way that stops a run. No flag suppresses it.
        """
        from .config.scope import require_nonempty_fleet, scoped_ids

        scopes = self._resolve_scopes()
        admissible = scoped_ids(
            self.lab.hosts, scopes, owner, exclude_projects=self.exclude_projects
        )
        if require_nonempty:
            require_nonempty_fleet(
                scopes, admissible, owner, exclude_projects=self.exclude_projects
            )
        return admissible

    def all_hosts(
        self,
        pattern: "re.Pattern[str] | None" = None,
        *,
        include_containers: bool = False,
        include_local: bool = False,
        _scope_owner: "str | None" = None,
        **overrides: Any,
    ) -> "Iterator[RemoteHost]":
        """Yield this run's fleet of interest, optionally narrowed by *pattern*.

        The base set is the ambient project universe (spec §6) — the hosts some
        repo's ``[project]`` declaration admits, re-derived live — and *not*
        the whole loaded lab. When no repo declares ``[project]``, that IS the
        whole loaded lab, so product-less projects are untouched.

        *pattern* selects a SUBSET of that base set with ``re.fullmatch``, never
        ``re.search`` (D6): ``sensor`` no longer selects ``sensor-1``; write
        ``sensor.*``. A pattern that fullmatches none of a NON-EMPTY base set
        raises :class:`~otto.config.scope.EmptySelectionError` rather than
        yielding nothing — a silently empty sweep is the one failure worse than
        a crash.

        A pattern that DID match and whose every match the two membership flags
        below then removed raises the same class, with the other of its two
        messages: it names the flag rather than the regex, because widening a
        regex that already matched is the wrong edit and the natural first
        guess. The base-set count both messages quote is taken before those
        flags, so the *denominator* still describes the whole walkable fleet.

        The built-in ``local`` host (the machine otto runs on, injected by
        ``load_lab`` for targeted ``otto host local`` use) is NOT part of the
        fleet: deploy/monitor/coverage sweeps must never silently operate on
        the runner itself, and it is not a ``RemoteHost``. Pass
        ``include_local=True`` to opt it in; ``get_host("local")`` always
        resolves it. Both flags apply AFTER scoping, which is what keeps
        ``include_local=True`` working under a declaration. Their exclusions
        stay silent when *pattern* is ``None``: an unnarrowed walk over a fleet
        that happens to hold only containers is the long-standing "empty lab,
        empty walk" case, not a selection anyone typed and got wrong.

        Args:
            pattern: Compiled regex fullmatched against each host's ``id``.
                ``None`` (the default) yields the whole base set.
            include_containers: Also yield
                :class:`~otto.host.docker_host.DockerContainerHost` entries.
            include_local: Also yield the built-in ``local`` host.
            _scope_owner: Internal. ``Repo.name`` whose universe bounds this
                walk; the repo-scoped context view supplies it, and a plain
                context leaves it ``None`` for the union. Underscored because
                it is a seam between context objects, not a call-site knob —
                which host set a walk gets must come from WHICH OBJECT the call
                goes through, never from an argument each call site remembers
                (spec §7). It also cannot be spelled ``owner``: that name is
                already a *method* kwarg forwarded to owner-accepting host verbs.
            **overrides: Per-call protocol/option overrides; see
                ``otto.config.fleet._apply_option_overrides``.

        Yields:
            RemoteHost: Each selected host. Nothing registers until it
            connects; then it registers with the event loop it connects on,
            and that loop's cleanup closes it.

        Raises:
            otto.config.scope.EmptySelectionError: *pattern* is not ``None`` and
                either fullmatches nothing in the base set, or fullmatches only
                hosts ``include_containers``/``include_local`` hold out of the
                walk. The instance's ``excluded_by`` tells the two apart.
            otto.bootstrap.ProjectScopeError: The base set is empty while some
                repo declared a ``[project]`` scope.
        """
        from .config.fleet import _apply_option_overrides, _flag_holding_out
        from .config.scope import EmptySelectionError

        # Computed here rather than in a wrapper: this is a generator, so the
        # body runs at first `next()` — which is when the walk actually happens
        # and therefore when the lab's host mapping is the one being walked.
        selected = self.admissible_ids(_scope_owner)
        if pattern is not None and selected:
            # `and selected`: with an EMPTY base set the pattern is not what
            # went wrong, and blaming it would send the reader to fix a regex
            # over a lab that holds nothing to select. That case is already
            # spoken for — loudly by `admissible_ids` when a repo declared a
            # scope, and deliberately silently otherwise, which is the
            # long-standing "an empty lab yields an empty walk" behavior every
            # lab-less CLI path (e.g. `otto cov get` reaching its own
            # validation) still depends on.
            base_size = len(selected)
            matched = {host_id for host_id in selected if pattern.fullmatch(host_id)}
            if not matched:
                raise EmptySelectionError(pattern.pattern, base_size)
            # The other way a selection ends up empty, and the one the count
            # above cannot see: the pattern matched, and the membership flags
            # below then removed every match. Reading the survivors off
            # `self.lab.hosts.values()` — the SAME mapping the yield loop walks,
            # in the same order, through the same `_flag_holding_out` rule — is
            # what keeps this prediction and that loop from ever disagreeing
            # about who is a fleet member.
            hidden_by = _flags_hiding_every_match(
                [host for host in self.lab.hosts.values() if host.id in matched],
                include_containers=include_containers,
                include_local=include_local,
            )
            if hidden_by:
                raise EmptySelectionError(
                    pattern.pattern,
                    base_size,
                    excluded_by=hidden_by,
                    matched_size=len(matched),
                )
            selected = matched
        for host in self.lab.hosts.values():
            if host.id not in selected:
                continue
            held_out_by = _flag_holding_out(
                host, include_containers=include_containers, include_local=include_local
            )
            if held_out_by is not None:
                continue
            yield _apply_option_overrides(cast("Any", host), **overrides)

    async def do_for_all_hosts(  # noqa: PLR0913 — wide host-dispatch API
        self,
        method: "Callable[..., Awaitable[T]]",
        *args: Any,
        pattern: "re.Pattern[str] | None" = None,
        concurrent: bool = True,
        include_containers: bool = False,
        include_local: bool = False,
        term: "str | None" = None,
        transfer: "str | None" = None,
        ssh_options: "Any" = None,
        telnet_options: "Any" = None,
        sftp_options: "Any" = None,
        scp_options: "Any" = None,
        ftp_options: "Any" = None,
        nc_options: "Any" = None,
        userland_options: "Any" = None,
        _scope_owner: "str | None" = None,
        **kwargs: Any,
    ) -> "dict[str, T | BaseException]":
        """Call *method* on every matching host and return a ``{host_id: result}`` mapping.

        When *concurrent* is ``True`` (default), all calls are gathered in
        parallel via ``asyncio.gather``; exceptions from individual hosts are
        captured as values rather than propagated. When ``False``, hosts are
        called sequentially and exceptions are likewise captured. Fleet
        membership follows :meth:`all_hosts` in full — the ambient project
        universe, ``pattern`` as a fullmatch within it, its empty-selection
        guard, and the ``local`` exclusion unless ``include_local=True``.

        ``_scope_owner`` is the same internal seam :meth:`all_hosts` documents
        and is forwarded verbatim. It is spelled with a leading underscore
        partly to stay out of ``**kwargs``, which is where a plain ``owner=``
        belongs: that one is forwarded to *method* for owner-accepting host
        verbs, and the two must never be the same word.
        """
        hosts = list(
            self.all_hosts(
                pattern=pattern,
                include_containers=include_containers,
                include_local=include_local,
                _scope_owner=_scope_owner,
                term=term,
                transfer=transfer,
                ssh_options=ssh_options,
                telnet_options=telnet_options,
                sftp_options=sftp_options,
                scp_options=scp_options,
                ftp_options=ftp_options,
                nc_options=nc_options,
                userland_options=userland_options,
            )
        )
        if concurrent:
            results = await asyncio.gather(
                *(method(h, *args, **kwargs) for h in hosts),
                return_exceptions=True,
            )
            return dict(zip([h.id for h in hosts], results, strict=True))
        out: dict[str, T | BaseException] = {}
        for h in hosts:
            try:
                out[h.id] = await method(h, *args, **kwargs)
            except BaseException as exc:  # noqa: PERF203,BLE001 — collect-results, intentionally catches all
                out[h.id] = exc
        return out

    async def run_on_all_hosts(  # noqa: PLR0913 — wide host-dispatch API
        self,
        cmds: "list[str] | str",
        pattern: "re.Pattern[str] | None" = None,
        concurrent: bool = True,
        timeout: float = DEFAULT_COMMAND_TIMEOUT,
        *,
        _scope_owner: "str | None" = None,
        include_containers: bool = False,
        term: "str | None" = None,
        transfer: "str | None" = None,
        ssh_options: "Any" = None,
        telnet_options: "Any" = None,
        sftp_options: "Any" = None,
        scp_options: "Any" = None,
        ftp_options: "Any" = None,
        nc_options: "Any" = None,
        userland_options: "Any" = None,
    ) -> "dict[str, Results | BaseException]":
        """Run one or more shell commands on every matching host and return a results mapping.

        Accepts a single command string or a list of commands executed in
        sequence on each host. Delegates concurrency and filtering to
        ``do_for_all_hosts``; exceptions from individual hosts are captured as
        values rather than propagated.

        ``_scope_owner`` is the internal seam :meth:`all_hosts` documents,
        forwarded verbatim so :class:`ProjectContextView` can bound this
        surface too. It narrows the FLEET and stamps nothing — ``host.run``
        takes no ``owner``, and a shell command belongs to whoever ran it.
        """
        cmd_list = [cmds] if isinstance(cmds, str) else cmds

        async def _run_list(host: "UnixHost") -> "Results":
            return await host.run(cmd_list, timeout=timeout)

        return await self.do_for_all_hosts(
            _run_list,
            pattern=pattern,
            concurrent=concurrent,
            _scope_owner=_scope_owner,
            include_containers=include_containers,
            term=term,
            transfer=transfer,
            ssh_options=ssh_options,
            telnet_options=telnet_options,
            sftp_options=sftp_options,
            scp_options=scp_options,
            ftp_options=ftp_options,
            nc_options=nc_options,
            userland_options=userland_options,
        )

    def for_repo(self, repo_name: str) -> "ProjectContextView":
        """Return this same context seen from *repo_name*'s side (spec §7).

        A facade, not a copy — see :class:`ProjectContextView`. Cheap enough to
        build per call; :func:`otto.project.actions.actions_for` builds one for
        every ``ProjectActions`` it constructs.

        Args:
            repo_name: ``Repo.name``. Not validated here: a name this run never
                resolved is refused by the first walk that goes through the
                view (:func:`otto.config.scope.scoped_ids`), where the resolved
                set is known and the message can list it. Validating at
                construction would make a view unbuildable in the contexts that
                legitimately have no scopes at all — a library context, a run
                with no SUT directories — which are exactly the ones that walk
                the whole lab by design. A bootstrap that cannot be read is not
                one of them: it refuses at the view's first walk instead.

        Returns:
            The repo-scoped view.
        """
        return ProjectContextView(self, repo_name)


_OWNER_MISMATCH = """\
a fleet walk through repo '{repo}'s context view was passed owner={passed!r}.

A repo-scoped view supplies owner='{repo}' itself. Naming a different one asks
a repo-scoped object to act for another repo, which is the cross-repo bleed the
view exists to prevent -- and rewriting it to '{repo}' silently, as the safe
direction invites, would leave the caller believing they had acted on
{passed!r} while a healthy-looking results mapping said nothing.

Drop the owner= and let the view supply it, walk through the plain context if
the sweep really is host-global, or pass with_owner=False for a host verb that
takes no owner at all."""
"""The view's refusal for a walk that names an owner other than its own repo.

Narrow by design: an owner EQUAL to the view's repo is redundant, not wrong,
and passes through -- ``super().install()`` from a subclass that still spells
the old argument must keep working. What is refused is the disagreement.
"""


class ProjectContextView:
    """One repo's face on the live context: its fleet, and its owner stamp (spec §7).

    Returned by :meth:`OttoContext.for_repo` and handed to every
    :class:`~otto.project.actions.ProjectActions` by
    :func:`~otto.project.actions.actions_for`, so a repo's ``install()`` reads
    ``self.ctx.do_for_all_hosts(_dispatch_install)`` — no ``owner=``, no
    universe plumbing.

    THE POINT IS WHERE THE SCOPE COMES FROM. Both narrowings — which hosts a
    walk may reach, and which owner's products the host verb touches — are
    properties of the OBJECT the call goes through, never of arguments each
    call site has to remember. An argument is forgettable exactly once per new
    call site, and the failure mode of forgetting it is a walk that silently
    reaches further than it should: repo A's uninstall taking repo B's products
    off a host they share. Through this view that mistake is unspellable.

    A FACADE OVER THE SAME CONTEXT, not a copy. Everything else —
    :meth:`~OttoContext.get_host`, the runtime flags, links, options — is
    delegated live by ``__getattr__``, so an ``output_dir`` set after the
    view was built still arrives. A host handed out here registers with the
    event loop it connects on, and that loop's cleanup closes it.

    ``get_host`` is deliberately NOT narrowed (§6): explicit targeting beats
    scoping, and a repo naming a jump host it does not own must still reach it.

    Owner-less host verbs stay dispatchable through
    :meth:`do_for_all_hosts` by passing ``with_owner=False``. That opt-out is
    explicit rather than inferred from the callable's signature: signature
    sniffing would silently skip the stamp for anything it could not read
    (``functools.partial``, ``*args`` wrappers, a C-implemented double), which
    turns an owner-scoping failure into the quietest possible bug. Its cost is
    that dispatching an owner-less callable and forgetting the flag raises
    ``TypeError`` at the host — loud, per-host, and captured in the results
    mapping like any other host failure.
    """

    def __init__(self, ctx: "OttoContext", repo_name: str) -> None:
        self._ctx = ctx
        """The live context every unbound attribute is read from."""

        self._repo_name = repo_name
        """The repo this view acts for — the walk's bound universe and its owner stamp."""

    def __getattr__(self, name: str) -> Any:
        """Delegate anything this view does not override to the live context.

        ``__getattr__`` runs only after normal lookup fails, so ``_ctx`` (bound
        in ``__init__``) resolves from the instance dict and cannot recurse
        here.
        """
        return getattr(self._ctx, name)

    def all_hosts(self, *args: Any, **kwargs: Any) -> "Iterator[RemoteHost]":
        """Yield this repo's fleet of interest — :meth:`OttoContext.all_hosts`, bound.

        Every argument is forwarded unchanged; only the base set differs.
        Overriding this surface separately from :meth:`do_for_all_hosts` is
        load-bearing rather than tidy: ``status``/``is_clean``/``owns_products``
        read the iteration surface directly, and a view that bound only its
        dispatch seam would answer those about the whole union.
        """
        return self._ctx.all_hosts(*args, _scope_owner=self._repo_name, **kwargs)

    async def do_for_all_hosts(
        self,
        method: "Callable[..., Awaitable[T]]",
        *args: Any,
        with_owner: bool = True,
        **kwargs: Any,
    ) -> "dict[str, T | BaseException]":
        """Call *method* on this repo's fleet, with ``owner=`` supplied for it.

        Args:
            method: The dispatch helper, called as ``method(host, ...)``.
            *args: Positional arguments forwarded to *method*.
            with_owner: Whether to supply ``owner=<repo>`` to *method*. Pass
                ``False`` for a host verb that takes no owner — see the class
                docstring for why this is a flag and not a signature check.
            **kwargs: Forwarded to :meth:`OttoContext.do_for_all_hosts`, which
                splits its own walk knobs out and hands the rest to *method*.
                An ``owner`` here must equal this view's repo; see Raises.

        Returns:
            ``{host_id: result | exception}``, exactly as the context's own
            dispatch returns.

        Raises:
            otto.bootstrap.ProjectScopeError: *kwargs* names an ``owner`` other
                than this view's repo. Raised BEFORE the walk, because it is
                the caller's mistake and not a host's — nothing is contacted,
                and it never lands in the results mapping as a per-host value.
        """
        if with_owner:
            if "owner" in kwargs and kwargs["owner"] != self._repo_name:
                from .bootstrap import ProjectScopeError  # function-scope: import-light

                raise ProjectScopeError(
                    "",  # a caller mistake, so there is no settings.toml to send them to
                    _OWNER_MISMATCH.format(repo=self._repo_name, passed=kwargs["owner"]),
                )
            kwargs["owner"] = self._repo_name
        return await self._ctx.do_for_all_hosts(
            method, *args, _scope_owner=self._repo_name, **kwargs
        )

    async def run_on_all_hosts(
        self, *args: Any, **kwargs: Any
    ) -> "dict[str, Results | BaseException]":
        """Run commands on this repo's fleet — :meth:`OttoContext.run_on_all_hosts`, bound.

        Bound rather than delegated, because delegation would leave one
        unscoped fleet walk reachable on an object whose whole purpose is to
        bound them — and it is the surface a subclass reaches for when no host
        verb fits. No owner is stamped: ``host.run`` takes none.
        """
        return await self._ctx.run_on_all_hosts(*args, _scope_owner=self._repo_name, **kwargs)


def prepared_policy(
    *,
    variant: "Variant | None" = None,
    dry_run: bool = False,
    log_command_output: bool = True,
    output_dir: "Path | None" = None,
) -> RunPolicy:
    """Build the policy a run prepares before its lab: flags given, variant given or inherited.

    Shared by :func:`open_context` and the library ``run_tests``. The
    teardown deadline is the default 10 seconds, never an inherited one; the
    caller fills it from discovery with :func:`fill_teardown_deadline`.
    Raises ``ValueError`` for an unknown *variant*, before anything is
    installed.
    """
    from .invocation import RunPolicy, check_variant, current_policy

    return RunPolicy(
        dry_run=dry_run,
        log_command_output=log_command_output,
        output_dir=output_dir,
        variant=current_policy().variant if variant is None else check_variant(variant),
    )


def fill_teardown_deadline(policy: "RunPolicy") -> None:
    """Set *policy*'s teardown deadline to the one discovery parsed (``OTTO_TEARDOWN_DEADLINE``).

    When discovery fails, *policy*'s deadline is left as it is: a policy from
    :func:`prepared_policy` keeps the default 10 s. It never reads another
    policy's deadline. ``open_context`` calls it once its policy is
    installed, and the library ``run_tests`` before it installs its sentinel
    context.
    """
    from .bootstrap import discovered_teardown_deadline  # function-scope: the composition root

    discovered = discovered_teardown_deadline()
    if discovered is not None:
        policy.teardown_deadline = discovered


@asynccontextmanager
async def open_context(
    *,
    lab: "Lab | str | list[str]",
    include_projects: "list[str] | None" = None,
    exclude_projects: "list[str] | None" = None,
    variant: Variant | None = None,
    dry_run: bool = False,
    log_command_output: bool = True,
    holder: "str | None" = None,
    skip_reservation_check: bool = False,
) -> "AsyncIterator[OttoContext]":
    """Prepare a run exactly as ``otto --lab ...`` does, install its context, and tear it down.

    The same decisions in the same order as the CLI preamble, from
    :mod:`otto.session`: the project switches are validated
    (:func:`~otto.session.select_projects`); a broken ACTIVE repo refuses
    the run (:func:`~otto.session.check_repos`), and an inactive repo's load
    error is logged as a warning; the lab is built from the repos'
    ``[[lab.sources]]``, ``[host_preferences]``, inventory and declared
    containers (:func:`~otto.session.build_lab`), or a
    :class:`~otto.config.lab.Lab` you pass is used as given; once the context
    is installed, an active repo's unmet Python requirement refuses
    (:func:`~otto.session.check_dependencies`); last, the reservation gate
    runs (:meth:`~otto.reservations.check.ReservationGate.evaluate`, built by
    :func:`~otto.reservations.build_reservation_gate` from the repos'
    ``[reservations]``), so a lab whose resources you do not hold refuses.

    *variant* mirrors ``otto --field/--debug``: it is installed on the run's
    policy (:attr:`OttoContext.policy`) before the lab is built, so a product
    declared once per variant resolves to the chosen entry, and it stays for
    the body, where providers read it. ``None`` inherits the variant already
    current (``"debug"`` unless the caller chose one). A nested
    ``open_context(variant=...)`` inside another context is allowed: it is
    how a script runs work under another variant, since :func:`set_variant`
    refuses while a context is installed. Every exit restores the outer
    policy, a refusal during setup included. A :class:`~otto.config.lab.Lab`
    you pass keeps the products it was built with, so build it under the same
    variant (:func:`set_variant` before :func:`~otto.session.build_lab`);
    containers started in the body (``compose_up``) are ingested under the
    variant you pass.

    *lab* is a lab name, a ``+``-joined combination, a list of either (each
    item split like a repeated ``--lab``), or a
    :class:`~otto.config.lab.Lab`. It installs no logging (see
    :func:`otto.session.install_logging`).

    On exit it releases its cleanup boundary on the running loop, and the
    last release closes the hosts that connected on that loop. A nested
    ``open_context``'s exit closes nothing while the outer one holds the
    loop; under ``otto test`` the runner's loop holds it until that runner
    shuts down (see the host-scopes cookbook). Then it restores the outer
    context and policy.

    *holder* mirrors ``--holder`` (check reservations as that user; ``None``
    or ``""`` means the invoking user). *skip_reservation_check* mirrors
    ``-R``: no backend is built and a warning says the check was skipped.
    ``dry_run`` is gated too, as the CLI gates before its dry-run seam.

    Raises:
        ValueError: a malformed lab string, such as ``""`` or ``"a++b"`` (an
            empty ``+``-segment), or a *variant* other than ``"debug"`` or
            ``"field"`` (raised before anything else runs).
        otto.session.ProjectSelectionError: an unknown project name, or one
            in both lists.
        otto.session.RepoLoadError: an active repo failed to load.
        otto.session.LabBuildError: no lab, an unknown lab, a bad lab source,
            or a broken inventory declaration.
        otto.labs.errors.LabRepositoryError: lab data that fails only at
            load time (malformed lab data, composite conflicts).
        otto.session.DependencyRefusedError: an active repo's Python
            requirement is not met.
        ValueError: a misconfigured ``[reservations]`` table (no
            ``[reservations.json] path``, an unknown backend name), raised by
            the backend factory.
        otto.reservations.check.MissingReservationError: the lab needs a resource
            you do not hold; its ``.report`` lists each one and its holders.
        otto.reservations.check.ReservationBackendError: the configured backend
            cannot be built or queried.
        RuntimeError: entered from inside a host's close while its loop is
            being swept (refused before any of the steps above).
    """
    from .invocation import install_policy, refuse_inside_a_sweep, reset_binding

    # First, so an invalid variant refuses before anything needs undoing.
    policy = prepared_policy(
        variant=variant, dry_run=dry_run, log_command_output=log_command_output
    )
    # Before bootstrap, the lab build and the reservation gate: a host's close
    # during a sweep cannot open a context, so it pays for none of them.
    refuse_inside_a_sweep("open_context")
    policy_binding = install_policy(policy)  # ingest reads the variant while the lab is built
    try:
        from .bootstrap import bootstrap
        from .config.lab import Lab, split_lab_names
        from .session import build_lab, check_dependencies, check_repos, select_projects

        result = bootstrap()  # composition root — idempotent; registers user init components
        fill_teardown_deadline(policy)  # after discovery; bounds the closing sweep on exit
        selection = select_projects(result.repos, include_projects or [], exclude_projects or [])
        if isinstance(lab, Lab):
            labs = list(lab.component_names)
        elif isinstance(lab, str):
            labs = split_lab_names(lab)
        else:
            # Each item splits on `+`, as each repeated `--lab` value does.
            labs = [name for item in lab for name in split_lab_names(item)]
        for demoted in check_repos(result, labs, selection).demoted:
            logger.warning("%s", demoted.message)
        # A `Lab` object is used as given: `build_lab` (and the `otto.inventory`
        # import it makes, ~77 modules) never runs for it.
        resolved_lab = lab if isinstance(lab, Lab) else build_lab(result.repos, labs)
        # As the CLI does: the gate is built after the lab and before the
        # context, so an unbuildable backend refuses with nothing to undo.
        from pathlib import Path

        from .reservations import build_reservation_gate

        gate = build_reservation_gate(
            result.repos,
            holder=holder,
            skip_reservation_check=skip_reservation_check,
            cwd_fallback=Path.cwd(),
        )
        if holder and gate.identity is not None:
            logger.info("reservations: acting as %r (holder=)", gate.identity.username)
        ctx = OttoContext(
            lab=resolved_lab,
            include_projects=tuple(selection.include),
            exclude_projects=tuple(selection.exclude),
            policy=policy,
            bootstrap=result,
        )
        binding = set_context(ctx)
        try:
            for warning in check_dependencies(ctx):
                logger.warning("%s", warning)
            # Last, as the CLI presents its gate after the dependency refusal;
            # dry_run is gated too, because the CLI gates before its dry-run seam.
            gate.evaluate()
            from .invocation import acquire_boundary_waiting

            # Waits out a sweep in progress on this loop.
            boundary = await acquire_boundary_waiting(
                asyncio.get_running_loop(), deadline=policy.teardown_deadline
            )
        except BaseException:
            reset_context(binding)
            raise
        try:
            yield ctx
        finally:
            try:
                await boundary.release(label="open_context")
            finally:
                reset_context(binding)
    finally:
        reset_binding(policy_binding)
