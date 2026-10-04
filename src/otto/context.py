"""otto's per-invocation runtime composition root.

Owns the active Lab, the per-invocation runtime flags, and one host scope per
event loop. Propagated via a ContextVar so the bare module accessors
(otto.config.all_hosts/get_host) can stay zero-argument, while explicit
passing (OttoContext methods, open_context) is first-class.
"""

import asyncio
import functools
import logging
import re
from collections.abc import AsyncIterator, Awaitable, Callable, Iterator
from contextlib import asynccontextmanager, contextmanager
from contextvars import ContextVar, Token
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Literal, TypeVar, cast

from .host.host import DEFAULT_COMMAND_TIMEOUT

if TYPE_CHECKING:
    from pathlib import Path

    from _typeshed import DataclassInstance

    from .config.lab import Lab
    from .config.scope import ProjectScope
    from .host import Results, UnixHost
    from .host.host import BaseHost
    from .host.remote_host import RemoteHost
    from .params import OptionsSource

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


class HostScope:
    """The hosts whose connections one event loop owns; closes them while that loop runs.

    One per event loop, held by :meth:`OttoContext.scope_for`. A host
    registers itself the first time it touches a connection on a loop (see
    ``BaseHost._claim_loop``), so the scope holds exactly
    what the loop can still close gracefully. The deterministic backstop that
    replaces ``RemoteHost.__del__``: a host created and passed around without
    an explicit ``async with`` is still closed when its loop's scope is swept.
    Registration is deduped by object identity; ``close()`` is idempotent, so
    an early per-host close and the sweep never collide.
    """

    def __init__(self) -> None:
        self._hosts: "list[BaseHost]" = []
        self._sweeping: "list[BaseHost]" = []
        """The hosts the current sweep set out to close, in registration order."""
        self._settled: dict[int, bool] = {}
        """``id(host)`` -> whether its close succeeded, for each close that has finished."""
        self._closed: list[str] = []
        """The ids closed by the ranks the current sweep has finished, in close order."""
        self._rank: "list[BaseHost]" = []
        """The rank the current sweep is closing now."""

    def register(self, host: "BaseHost") -> None:
        """Add *host* to the scope for the sweep, deduplicating by identity."""
        if any(host is h for h in self._hosts):  # dedup by object identity
            return
        self._hosts.append(host)

    def closed_ids(self) -> list[str]:
        """Return the ids of the hosts the current sweep has closed so far, in close order."""
        in_flight = [_host_id(h) for h in self._rank if self._settled.get(id(h)) is True]
        return [*self._closed, *in_flight]

    def unclosed(self) -> "list[BaseHost]":
        """Return the hosts the current sweep set out to close whose close has not finished."""
        return [h for h in self._sweeping if id(h) not in self._settled]

    async def sweep(self) -> list[str]:
        """Close every registered host this loop still owns; return the ids of those that closed.

        Dependency-ranked: a host that another registered host names as its
        ``parent`` (a container's ``docker exec`` channel drains over its
        parent's still-open transport) closes only after its dependents.
        Within a rank closes run concurrently. A failed close is logged as a
        warning naming the host, and never stops the remaining ranks.

        The list is drained first, so a second sweep closes only what
        registered since. Only hosts this loop still owns are closed: one
        that has moved to another loop is that loop's to close, and one its
        own code already closed (and has not used since) has nothing left to
        close. A duck-typed host with no ownership record at all is closed.
        Progress is recorded as each close finishes (:meth:`closed_ids`,
        :meth:`unclosed`), for a caller that cuts the sweep short.
        """
        hosts, self._hosts = self._hosts, []
        loop = asyncio.get_running_loop()
        remaining = [
            h for h in hosts if getattr(h, "_owner_loop", _UNTRACKED) in (loop, _UNTRACKED)
        ]
        self._sweeping = list(remaining)
        self._settled = {}
        self._closed = []
        while remaining:
            parent_ids = {id(getattr(h, "parent", None)) for h in remaining}
            rank = [h for h in remaining if id(h) not in parent_ids]
            if not rank:
                rank = remaining  # parent cycle (impossible by construction): close all, don't spin
            self._rank = rank
            results = await asyncio.gather(
                *(self._close_one(h) for h in rank), return_exceptions=True
            )
            for host, result in zip(rank, results, strict=True):
                if isinstance(result, BaseException):  # a close that raised CancelledError itself
                    self._settled[id(host)] = False
                    _warn_failed(host, result)
            self._closed += [_host_id(h) for h in rank if self._settled.get(id(h)) is True]
            self._rank = []
            done = {id(h) for h in rank}
            remaining = [h for h in remaining if id(h) not in done]
        return self.closed_ids()

    async def _close_one(self, host: "BaseHost") -> None:
        """Close *host* and record the outcome; a failed close is a warning.

        A close cancelled before it finished records nothing, which is what
        leaves the host in :meth:`unclosed`.
        """
        try:
            await host.close()
        except asyncio.CancelledError:
            raise
        except BaseException as exc:  # noqa: BLE001 — a failed close is a warning, whatever it raised
            self._settled[id(host)] = False
            _warn_failed(host, exc)
        else:
            self._settled[id(host)] = True


_UNTRACKED = object()
"""Stands in for ``_owner_loop`` on a duck-typed host that records no owning loop."""

_CANCEL_GRACE = 0.25
"""Seconds a timed-out sweep waits after each cancel for the closes to unwind."""


def _host_id(host: "BaseHost") -> str:
    return getattr(host, "id", repr(host))


def _warn_failed(host: "BaseHost", exc: BaseException) -> None:
    logger.warning(f"otto: closing host {_host_id(host)!r} failed during scope sweep: {exc!r}")


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


def set_context(ctx: "OttoContext") -> "Token[OttoContext | None]":
    """Install *ctx* as the active context and return the reset token."""
    return _active.set(ctx)


Variant = Literal["debug", "field"]
VARIANTS: tuple[Variant, ...] = ("debug", "field")
"""The two product variants a run can select (``--field``/``--debug``)."""

_variant: ContextVar[Variant] = ContextVar("otto_variant", default="debug")


def variant() -> Variant:
    """Return the run's product variant — ``"debug"`` unless root ``--field`` chose field.

    Its own ContextVar rather than a field on :class:`OttoContext`: a lab is
    ingested — every declared entry built, every provider run — inside the
    CLI's lab load, before an ``OttoContext`` exists, and the variant must be
    readable there. Read by :meth:`otto.declared.KindRegistry.build` to pick
    between same-name entries, and by providers that return per-variant
    instances. A library caller that never set it reads ``"debug"``.
    """
    return _variant.get()


def set_variant(value: Variant) -> "Token[Variant]":
    """Set the run's variant; the root CLI callback's one write, before any lab loads."""
    if value not in VARIANTS:
        raise ValueError(f"variant must be 'debug' or 'field', got {value!r}")
    return _variant.set(value)


def reset_context(token: "Token[OttoContext | None]") -> None:
    """Restore the context ContextVar to the value it held before the matching ``set_context``."""
    _active.reset(token)


def reset_variant(token: "Token[Variant]") -> None:
    """Restore the variant ContextVar to the value it held before the matching ``set_variant``."""
    _variant.reset(token)


_cli_token: "Token[OttoContext | None] | None" = None


def set_cli_context(ctx: "OttoContext") -> None:
    """Install *ctx* as the CLI invocation's context, remembering the reset token.

    The CLI installs the context from deep inside the Typer callback
    (``cli.invoke.ensure_lab_context``) while the natural reset point is the
    console-script entry's ``finally`` — the two can't share a stack frame, so
    the token lives module-side. One CLI invocation per process; tests that
    drive the app via CliRunner are covered by the autouse ContextVar
    snapshot fixture in tests/conftest.py either way.
    """
    global _cli_token  # noqa: PLW0603 — module-level singleton/cache
    _cli_token = set_context(ctx)


_variant_token: "Token[Variant] | None" = None


def set_cli_variant(value: Variant) -> None:
    """Set the CLI invocation's variant, remembering the reset token.

    The root callback writes the variant; the console-script entry's
    ``finally`` (:func:`reset_cli_context`) undoes it. They share no stack
    frame, so the token lives module-side, exactly as :func:`set_cli_context`
    keeps the context's.
    """
    global _variant_token  # noqa: PLW0603 — module-level singleton/cache
    _variant_token = set_variant(value)


def reset_cli_context() -> None:
    """Undo :func:`set_cli_context` and :func:`set_cli_variant` if they ran; safe to call always."""
    global _cli_token, _variant_token  # noqa: PLW0603 — module-level singleton/cache
    try:
        if _cli_token is not None:
            reset_context(_cli_token)
            _cli_token = None
    finally:
        # Each reset stands on its own: the second runs even if the first throws.
        if _variant_token is not None:
            reset_variant(_variant_token)
            _variant_token = None


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
    from .host.docker_host import DockerContainerHost
    from .host.local_host import LocalHost

    hiding: set[str] = set()
    for host in matched:
        if not include_containers and isinstance(host, DockerContainerHost):
            hiding.add("include_containers")
        elif not include_local and isinstance(host, LocalHost):
            hiding.add("include_local")
        else:
            return []
    return sorted(hiding)


@dataclass
class OttoContext:
    """The active per-invocation runtime: chosen lab, runtime flags, and per-loop host scopes."""

    lab: "Lab"
    dry_run: bool = False
    log_command_output: bool = True
    output_dir: "Path | None" = None
    cov_decision: "bool | None" = None
    """Whether coverage is on for this run, once decided; ``None`` until then.

    Read :attr:`cov`, not this. ``otto test`` is the one writer: it stamps its
    resolved ``--cov``/``--no-cov``/auto decision here for the length of the
    run (:func:`otto.suite.run.run_tests`).
    Left ``None``, the first read of :attr:`cov` detects the answer and
    stores it here.
    """
    _loop_scopes: "dict[asyncio.AbstractEventLoop, HostScope]" = field(
        default_factory=dict, init=False, repr=False, compare=False
    )
    """One :class:`HostScope` per event loop that has owned a host connection."""

    include_projects: tuple[str, ...] = ()
    """Repo names forced ACTIVE this invocation (``-I``), PEP-503-normalized ON READ.

    Populated from the root CLI callback via ``RootOptions``; empty for
    library contexts. Read only through :func:`otto.config.scope.active` —
    nothing else may re-derive activation from these tuples.

    Nothing normalizes on WRITE. This dataclass stays plain (no
    ``__post_init__``), so any caller may store whatever spelling it holds and
    the stored tuple is NOT guaranteed normalized;
    :func:`otto.config.scope.active` normalizes both the stored values and the
    queried name before comparing. The invariant is therefore enforced where it
    is read rather than merely asked for here — a docstring-only version would
    let ``exclude_projects=("My_Repo",)`` be silently ignored, which is an
    explicit switch failing OPEN.
    """

    exclude_projects: tuple[str, ...] = ()
    """Repo names forced INACTIVE this invocation (``-E``), PEP-503-normalized ON READ.

    Same write/read contract as :attr:`include_projects`. Read by
    :func:`otto.config.scope.active` for the verdict and by
    :func:`otto.config.scope.switched_off` for attribution.
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
        ``otto test``. The sentinel library lab and unreachable repos have no
        coverage configuration to find and answer ``False`` quietly.
        """
        if self.lab.name == LIBRARY_LAB_NAME:
            return False
        try:
            from .config import get_repos

            repos = get_repos()
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

        cov_config = get_cov_config(repos)
        if not cov_config:
            return False
        try:
            pattern = load_hosts_pattern(cov_config)
            hosts = list(self.all_hosts(pattern=pattern, include_containers=True))
        except (EmptySelectionError, CoverageConfigError, ProjectScopeError) as exc:
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

        Handing a host out registers nothing: the host joins the scope of
        whichever event loop it first connects on (:meth:`scope_for`).
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

    def scope_for(self, loop: asyncio.AbstractEventLoop) -> HostScope:
        """Return the host scope of *loop*: the hosts whose connections that loop owns.

        Entries for loops that have closed are pruned on the way, so a
        long-lived context that sees many short loops (a script calling
        ``asyncio.run`` repeatedly) keeps one entry per live loop. Pruning
        only forgets the entry: a host still owned by that dead loop drops
        its connection state on its next use or ``close()``.
        """
        for dead in [lp for lp in self._loop_scopes if lp.is_closed() and lp is not loop]:
            del self._loop_scopes[dead]
        return self._loop_scopes.setdefault(loop, HostScope())

    async def sweep_loop(
        self, loop: asyncio.AbstractEventLoop, *, label: str, deadline: float | None = None
    ) -> list[str]:
        """Close every host *loop* owns, while it still runs; log one debug line naming them.

        Awaited on *loop* itself, before it closes: the command lifecycle
        does so at the end of each command, ``open_context`` on exit, and
        ``otto test`` at the end of each pytest-asyncio loop. *label* names
        the loop in the debug line, for example ``closed 2 hosts at end of
        TestRouter's loop: dut1, dut2``. Returns the ids of the hosts that
        closed; a failed close is a warning, not a raise.

        *deadline* bounds the sweep in seconds; the callers in otto pass the
        teardown deadline (``OTTO_TEARDOWN_DEADLINE``), and ``None`` waits
        for every close. When the deadline expires the sweep is cancelled,
        and after a short grace cancelled again, however its closes' own
        cleanup is going. A warning then gives the time taken and names the
        hosts not closed, whose connection state is dropped unclosed so each
        reconnects on its next use. The return value still lists the hosts
        that did close.

        Raises:
            RuntimeError: *loop* is not the running loop. A host's close is
                network work that only its owning loop can drive.
        """
        if loop is not asyncio.get_running_loop():
            raise RuntimeError(
                f"sweep_loop can only sweep the running loop, but {label} is not the "
                "loop running here; await it on that loop, before the loop closes"
            )
        scope = self._loop_scopes.pop(loop, None)
        if scope is None:
            return []
        started = loop.time()
        task = loop.create_task(scope.sweep())
        try:
            await asyncio.wait({task}, timeout=deadline)
            if not task.done():
                await self._cut_short(task)
        except BaseException:
            task.cancel()  # our own caller gave up; the sweep must not outlive it
            raise
        if task.done() and not task.cancelled():
            closed = task.result()
        else:
            closed = scope.closed_ids()
            self._abandon_unclosed(scope, loop, label=label, elapsed=loop.time() - started)
        if closed:
            noun = "host" if len(closed) == 1 else "hosts"
            logger.debug(f"closed {len(closed)} {noun} at end of {label}: {', '.join(closed)}")
        return closed

    @staticmethod
    async def _cut_short(task: "asyncio.Task[list[str]]") -> None:
        """Cancel a sweep that ran past its deadline, waiting only a bounded grace per cancel.

        A cancelled close can keep running in its own cleanup, for example a
        transport's close in a ``finally``; the second cancel interrupts that.
        A task that still refuses is left to finish on its own, with its
        outcome retrieved when it does, so nothing waits on it unboundedly.
        """
        for _ in range(2):
            task.cancel()
            await asyncio.wait({task}, timeout=_CANCEL_GRACE)
            if task.done():
                return
        task.add_done_callback(lambda t: t.cancelled() or t.exception())

    @staticmethod
    def _abandon_unclosed(
        scope: HostScope, loop: asyncio.AbstractEventLoop, *, label: str, elapsed: float
    ) -> None:
        """Drop the connections of the hosts a cut-short sweep of *loop* left unclosed."""
        stuck = [h for h in scope.unclosed() if getattr(h, "_owner_loop", None) in (None, loop)]
        for host in stuck:
            if hasattr(host, "_owner_loop"):  # a duck-typed host keeps no state to drop
                host._drop_dead_connections()  # noqa: SLF001 — the context is the owner's other half
                host._owner_loop = None  # noqa: SLF001 — the context is the owner's other half
        ids = ", ".join(_host_id(h) for h in stuck) or "none"
        logger.warning(
            f"otto: closing hosts at end of {label} ran past its deadline; gave up after "
            f"{elapsed:.1f}s and dropped the connections of: {ids}"
        )

    def abandon_closed_loops(self) -> list[str]:
        """Drop the connections of hosts whose loop closed unswept (the backstop).

        A loop that closed before anything swept it leaves connections no
        loop can close gracefully. Their state is dropped, as a host's next
        use would drop it anyway, and the hosts are unowned again. Hosts
        that have since moved to another loop are left alone. Returns the ids
        abandoned, logged at debug level.
        """
        abandoned: list[str] = []
        for loop in [lp for lp in self._loop_scopes if lp.is_closed()]:
            for host in self._loop_scopes.pop(loop)._hosts:  # noqa: SLF001 — HostScope is this module's own
                if host._owner_loop is not loop:  # noqa: SLF001 — the context is the owner's other half
                    continue
                host._drop_dead_connections()  # noqa: SLF001 — the context is the owner's other half
                host._owner_loop = None  # noqa: SLF001 — the context is the owner's other half
                abandoned.append(host.id)
        if abandoned:
            noun = "host" if len(abandoned) == 1 else "hosts"
            logger.debug(
                f"abandoned {len(abandoned)} {noun} left on closed loops: {', '.join(abandoned)}"
            )
        return abandoned

    @functools.cached_property
    def scopes(self) -> "dict[str, ProjectScope]":
        """Each repo's resolved fleet of interest for this run (spec §5), computed once.

        Display and abort data — ``status --full`` renders it and
        :func:`~otto.config.scope.require_current_scope` refuses on it. Fleet
        iteration does NOT read the stored :attr:`~otto.config.scope.ProjectScope.universe`; it
        re-asks :func:`~otto.config.scope.repo_targets` per host through
        :func:`~otto.config.scope.scoped_ids`, so a container registered after
        this property was first touched is still scoped correctly.

        Lazy, not computed in ``__post_init__``, because resolving it needs the
        repos — and reaching them runs ``bootstrap()``. A context is built in
        plenty of places that never walk a fleet (library FD-model callers, the
        ``LIBRARY_LAB_NAME`` sentinel session, unit tests holding a hand-built
        ``Lab``), and making every one of them pay for — or fail on — a
        composition root they never asked for would be the wrong trade.

        An empty mapping is the honest answer whenever the repos cannot be
        reached, and it feeds the whole-lab fallback (§6): no declarations, no
        narrowing, today's behavior. That covers the sentinel lab explicitly
        and any bootstrap failure by catching it — a fleet walk must not be the
        surface that first reports a broken composition root, which the CLI
        entry already does with a message built for it.
        """
        from .config.scope import resolve_scopes
        from .host.builtin_hosts import BUILTIN_LOCAL_HOST_ID

        if self.lab.name == LIBRARY_LAB_NAME:
            return {}
        try:
            from .config import get_ordered_repos

            repos = get_ordered_repos()
        except Exception as exc:  # noqa: BLE001 — no repos reachable ⇒ no declarations ⇒ fallback
            logger.debug(f"otto: fleet scoping unavailable ({exc!r}); walking the whole lab")
            return {}
        scopes = resolve_scopes(
            repos,
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
            union = frozenset().union(*(scope.universe for scope in declared))
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
        definition, two readers.

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
        """
        from .config.scope import require_nonempty_fleet, scoped_ids

        admissible = scoped_ids(self.lab.hosts, self.scopes, owner)
        if require_nonempty:
            require_nonempty_fleet(self.scopes, admissible, owner)
        return admissible

    def _admissible_ids(self, owner: "str | None") -> "set[str]":
        """Alias of :meth:`admissible_ids` kept for one release; new code calls the public name."""
        return self.admissible_ids(owner)

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
            RemoteHost: Each selected host. Nothing registers until it connects
            (see :meth:`scope_for`).

        Raises:
            otto.config.scope.EmptySelectionError: *pattern* is not ``None`` and
                either fullmatches nothing in the base set, or fullmatches only
                hosts ``include_containers``/``include_local`` hold out of the
                walk. The instance's ``excluded_by`` tells the two apart.
            otto.bootstrap.ProjectScopeError: The base set is empty while some
                repo declared a ``[project]`` scope.
        """
        from .config.fleet import _apply_option_overrides
        from .config.scope import EmptySelectionError
        from .host.docker_host import DockerContainerHost
        from .host.local_host import LocalHost

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
            # in the same order — is what keeps this prediction and that loop
            # from ever disagreeing about who is a fleet member.
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
            if not include_containers and isinstance(host, DockerContainerHost):
                continue
            if not include_local and isinstance(host, LocalHost):
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
                legitimately have no scopes at all — a library context, an
                unavailable bootstrap — which are exactly the ones that walk
                the whole lab by design.

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
    :meth:`~OttoContext.get_host`, the runtime flags, the per-loop host
    scopes, links, options — is delegated live by ``__getattr__``, so an
    ``output_dir`` set after the view was built still arrives, and a host
    handed out here registers, once it connects, into the same per-loop
    :class:`HostScope` that closes it.

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

    *variant* mirrors ``otto --field/--debug``: it is applied with
    :func:`otto.context.set_variant` before the lab is built, so a product
    declared once per variant resolves to the chosen entry, and it stays set
    for the body, where providers read it. ``None`` keeps the variant already
    active (``"debug"`` unless the caller set one). Every exit restores the
    previous variant, a refusal during setup included. A
    :class:`~otto.config.lab.Lab` you pass keeps the products it was built
    with, so build it under the same variant (:func:`set_variant` before
    :func:`~otto.session.build_lab`); containers started in the body
    (``compose_up``) are ingested under the variant you pass.

    *lab* is a lab name, a ``+``-joined combination, a list of either (each
    item split like a repeated ``--lab``), or a
    :class:`~otto.config.lab.Lab`. On exit, the running loop's host scope
    closes the hosts that connected on it (:meth:`OttoContext.sweep_loop`) and
    the contextvar is reset. It installs no logging (see
    :func:`otto.session.install_logging`).

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
    """
    # First, so an invalid value refuses before anything needs undoing.
    variant_token = None if variant is None else set_variant(variant)
    try:
        from .bootstrap import bootstrap
        from .config import get_env
        from .config.lab import Lab, split_lab_names
        from .session import build_lab, check_dependencies, check_repos, select_projects

        result = bootstrap()  # composition root — idempotent; registers user init components
        deadline = get_env().teardown_deadline  # bounds the closing sweep on exit
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
            dry_run=dry_run,
            log_command_output=log_command_output,
            include_projects=tuple(selection.include),
            exclude_projects=tuple(selection.exclude),
        )
        token = set_context(ctx)
        try:
            for warning in check_dependencies(ctx):
                logger.warning("%s", warning)
            # Last, as the CLI presents its gate after the dependency refusal;
            # dry_run is gated too, because the CLI gates before its dry-run seam.
            gate.evaluate()
        except BaseException:
            reset_context(token)
            raise
        try:
            yield ctx
        finally:
            try:
                await ctx.sweep_loop(
                    asyncio.get_running_loop(), label="open_context", deadline=deadline
                )
            finally:
                reset_context(token)
    finally:
        if variant_token is not None:
            reset_variant(variant_token)
