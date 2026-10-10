"""What this run does, and who installed it: the run's policy, peer-host resolver and bindings.

A leaf. It imports no otto module at runtime (its annotations name host
types under ``TYPE_CHECKING`` only), so the host layer reads the run's policy
and resolves a peer host through it without importing :mod:`otto.context`.
:class:`otto.context.OttoContext` composes these; the public names are
declared at :mod:`otto.context`, and this module has no public path.

It also holds each event loop's host registry and the cleanup boundaries
that decide when that loop's hosts close.

Readers import it inside the function that reads it, as they import
``otto.context``: a value bound at import would dodge the tests' patches and
go stale across runs.
"""

import logging
from collections.abc import Awaitable, Callable
from contextvars import ContextVar, Token
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Literal, Protocol, TypeVar

if TYPE_CHECKING:
    import asyncio
    from collections.abc import Mapping
    from pathlib import Path

    from .host import Host

T = TypeVar("T")

Variant = Literal["debug", "field"]
"""The product variant a run selects, ``"debug"`` or ``"field"``."""

VARIANTS: tuple[Variant, ...] = ("debug", "field")
"""The two product variants a run can select (``--field``/``--debug``)."""


def check_variant(value: object) -> Variant:
    """Return *value* as a :data:`~otto.context.Variant`, else raise ``ValueError`` naming both."""
    for variant in VARIANTS:
        if value == variant:
            return variant
    raise ValueError(f"variant must be 'debug' or 'field', got {value!r}")


@dataclass
class RunPolicy:
    """What this run does, read live by every host when it acts.

    Mutable on purpose: ``SuppressCommandOutput()`` flips
    :attr:`~otto.context.RunPolicy.log_command_output` on the installed
    object, and the CLI stamps :attr:`~otto.context.RunPolicy.output_dir` once
    the run's directory exists. A host never captures a value at construction,
    because a host outlives the context it was built under. The variant is
    validated when the policy is built; change it by building a new policy
    (:func:`otto.context.set_variant`, ``open_context(variant=...)``), never by
    assigning it.
    """

    dry_run: bool = False
    """Whether the run declines every device-changing command (``--dry-run``)."""
    log_command_output: bool = True
    """Whether hosts log each command and its output."""
    output_dir: "Path | None" = None
    """The run's output directory, the base of the session log and retrieved logs."""
    variant: Variant = "debug"
    """The product variant ingest builds and providers read."""
    teardown_deadline: float = 10.0
    """Seconds a graceful teardown may take before it is abandoned.

    Preparation fills it from ``OTTO_TEARDOWN_DEADLINE`` once discovery has
    run; with no policy installed, readers see this default.
    """

    def __post_init__(self) -> None:
        check_variant(self.variant)


class HostResolver(Protocol):
    """Where a host looks up a peer (a hop, a power controller) it holds no reference to.

    :class:`~otto.config.lab.Lab` satisfies it. A host's own lab
    back-reference always wins; the installed resolver is the fallback.
    """

    @property
    def name(self) -> str:
        """The lab's name, for the error that names where a peer was not found."""
        ...

    @property
    def hosts(self) -> "Mapping[str, Host]":
        """The hosts a peer is looked up in, by id, read live."""
        ...


class ContextBinding:
    """What an install changed, for its one matching reset.

    Returned by :func:`otto.context.set_context` (the context, its policy and
    its resolver) and :func:`otto.context.set_variant` (one policy), and handed
    back to :func:`otto.context.reset_context` / :func:`otto.context.reset_variant`.
    It resets in reverse order, exactly once, in the execution context that
    made it.
    """

    def __init__(self) -> None:
        self._installed: list[tuple[ContextVar[Any], Token[Any]]] = []
        self._reset = False


_POLICY: ContextVar["RunPolicy | None"] = ContextVar("otto_run_policy", default=None)
_RESOLVER: ContextVar["HostResolver | None"] = ContextVar("otto_host_resolver", default=None)


def current_policy() -> RunPolicy:
    """Return the installed policy, or a fresh default one; never a shared default."""
    policy = _POLICY.get()
    return RunPolicy() if policy is None else policy


def installed_policy() -> "RunPolicy | None":
    """Return the installed policy, or ``None``: the writers' accessor (nothing to change)."""
    return _POLICY.get()


def installed_resolver() -> "HostResolver | None":
    """Return the installed peer-host resolver, or ``None``."""
    return _RESOLVER.get()


def install_var(
    var: "ContextVar[T]", value: T, *, into: "ContextBinding | None" = None
) -> ContextBinding:
    """Set *var* to *value* and record the token in *into* (a new binding when ``None``).

    Raises:
        RuntimeError: *into* was already reset; an install recorded there
            could never be undone.
    """
    binding = ContextBinding() if into is None else into
    if binding._reset:  # noqa: SLF001 — the binding is this module's own
        raise RuntimeError("this context binding was already reset; it takes no further installs")
    binding._installed.append((var, var.set(value)))  # noqa: SLF001 — the binding is this module's own
    return binding


def install_policy(policy: RunPolicy, *, into: "ContextBinding | None" = None) -> ContextBinding:
    """Install *policy* as the run's policy."""
    return install_var(_POLICY, policy, into=into)


def install_resolver(
    resolver: HostResolver, *, into: "ContextBinding | None" = None
) -> ContextBinding:
    """Install *resolver* as the run's peer-host resolver."""
    return install_var(_RESOLVER, resolver, into=into)


def reset_binding(binding: ContextBinding) -> None:
    """Undo *binding*'s installs in reverse order, exactly once.

    Raises:
        RuntimeError: *binding* was already reset.
        ValueError: called in another execution context than the one that
            made *binding* (``ContextVar.reset``'s own refusal); the binding
            stays resettable where it was made.
    """
    if binding._reset:  # noqa: SLF001 — the binding is this module's own
        raise RuntimeError("this context binding was already reset")
    for var, token in reversed(binding._installed):  # noqa: SLF001 — the binding is this module's own
        var.reset(token)
    binding._reset = True  # noqa: SLF001 — the binding is this module's own


# ---------------------------------------------------------------------------
# Each event loop's host registry, and the cleanup boundaries that drain it
# ---------------------------------------------------------------------------

_SWEEP_GRACE = 0.25
"""Seconds an expired sweep waits after each cancel for the closes to unwind."""

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class HostRegistration:
    """One host's entry in its loop's registry, built by the host.

    The registry never reads host internals: everything it needs is here.
    """

    key: int
    """The host's identity, ``id(host)``; a loop holds one record per key."""
    display_id: str
    """The host's id, for the sweep's log lines."""
    depends_on: list[int]
    """The keys this host must close before, such as a docker exec channel's parent transport."""
    generation: int
    """The host's connection epoch when it registered."""
    close: "Callable[[], Awaitable[None]]"
    """Closes the host; awaited on the registry's loop."""
    owned_by: "Callable[[asyncio.AbstractEventLoop], bool]"
    """Whether the given loop still owns the host's connection."""
    abandon: "Callable[[asyncio.AbstractEventLoop, int], bool]"
    """Drops the host's connections with no I/O, if its generation and owner still match.

    Returns whether it dropped anything: ``False`` for a stale abandonment.
    """


@dataclass(frozen=True)
class RegistrySnapshot:
    """One loop's registry as the test suite's guards read it."""

    loop: "asyncio.AbstractEventLoop"
    held: int
    state: str
    closed: bool
    display_ids: list[str]


class _Registry:
    """One loop's registrations, its held-boundary count and its drain state."""

    def __init__(self) -> None:
        self.records: dict[int, HostRegistration] = {}
        self.state: Literal["open", "draining", "shut"] = "open"
        self.held = 0
        self.changed: "asyncio.Event | None" = None
        self.waiters: "list[asyncio.Future[None]]" = []

    def poke(self) -> None:
        """Wake a running drain so it recomputes what it can close."""
        if self.changed is not None:
            self.changed.set()


_REGISTRIES: "dict[asyncio.AbstractEventLoop, _Registry]" = {}
_SWEEPING: ContextVar[bool] = ContextVar("otto_in_host_sweep", default=False)


def _forget_if_idle(loop: "asyncio.AbstractEventLoop", registry: _Registry) -> None:
    """Forget *loop*'s open registry once nothing is held, registered or waiting on it.

    Called after a sweep, so a finished command or context leaves no entry
    behind; the next use of the loop makes a fresh one. A shut registry is
    kept, so its loop keeps refusing boundaries until it closes; the next
    registration, boundary acquisition or shutdown on any loop then prunes it.
    """
    if registry.state != "open" or registry.held or registry.records or registry.waiters:
        return
    if _REGISTRIES.get(loop) is registry:
        _forget_registry(loop)


def register(loop: "asyncio.AbstractEventLoop", record: HostRegistration) -> None:
    """Record *record* in *loop*'s registry, replacing the host's earlier one.

    Every registration first forgets the registries of loops that have
    closed (it closes nothing; :func:`abandon_closed_loops` is the backstop
    that drops what they held).
    """
    _prune_closed_loops(loop)
    registry = _REGISTRIES.setdefault(loop, _Registry())
    registry.records[record.key] = record
    registry.poke()


def _prune_closed_loops(loop: "asyncio.AbstractEventLoop") -> None:
    """Forget the registries of closed loops other than *loop*, closing nothing.

    Run wherever a registry is made: a registration, a boundary acquisition
    and a runner's shutdown. So a shut registry outlives its loop's close
    only until the next of them, and a hostless suite of short runner loops
    keeps at most one closed loop's registry.
    """
    for dead in [lp for lp in _REGISTRIES if lp.is_closed() and lp is not loop]:
        _forget_registry(dead)


def unregister(loop: "asyncio.AbstractEventLoop", key: int, generation: int) -> None:
    """Remove *key*'s record from *loop*'s registry, only if it still carries *generation*."""
    registry = _REGISTRIES.get(loop)
    if registry is None:
        return
    record = registry.records.get(key)
    if record is None or record.generation != generation:
        return
    del registry.records[key]
    registry.poke()


class Boundary:
    """A held claim on a loop's cleanup: the loop's hosts close when the last one is released.

    ``open_context``, ``run_command`` and each pytest runner loop hold one.
    """

    def __init__(self, loop: "asyncio.AbstractEventLoop", deadline: "float | None") -> None:
        self.loop = loop
        """The loop whose hosts this boundary keeps open."""
        self.deadline = deadline
        """Seconds the sweep at the last release may take (``None`` waits for every close)."""
        self._released = False

    async def release(self, *, label: str) -> list[str]:
        """Give the boundary back; the last release closes the loop's hosts and returns their ids.

        The count drops when this coroutine starts, before its first await, so
        a release cancelled during its sweep still counts. Once the loop's
        runner has shut it down, a release sweeps nothing. The last release
        forgets the loop's registry once it is empty.

        Raises:
            RuntimeError: this boundary was already released.
        """
        if self._released:
            raise RuntimeError("this cleanup boundary was already released")
        self._released = True
        registry = _REGISTRIES.get(self.loop)
        if registry is None:
            return []
        registry.held -= 1
        if registry.held or registry.state != "open" or self.loop.is_closed():
            _forget_if_idle(self.loop, registry)
            return []
        try:
            return await _drain(
                self.loop, registry, label=label, deadline=self.deadline, shut=False
            )
        finally:
            _forget_if_idle(self.loop, registry)


def refuse_inside_a_sweep(what: str) -> None:
    """Raise if called from inside a host's close during a sweep (``open_context``'s first step).

    A close that opened a cleanup boundary would wait on the sweep that
    waits on it.

    Raises:
        RuntimeError: called from inside a sweep's close.
    """
    if _SWEEPING.get():
        raise RuntimeError(
            f"{what} was called from inside a host sweep on this loop; a close cannot "
            "open a cleanup boundary while the sweep waits on that close"
        )


def _acquirable(loop: "asyncio.AbstractEventLoop", *, what: str) -> _Registry:
    refuse_inside_a_sweep(what)
    if loop.is_closed():
        raise RuntimeError(f"{what}: the event loop is closed")
    _prune_closed_loops(loop)
    registry = _REGISTRIES.setdefault(loop, _Registry())
    if registry.state == "shut":
        raise RuntimeError(f"{what}: this loop's runner has shut its host registry down")
    return registry


def acquire_boundary(loop: "asyncio.AbstractEventLoop", *, deadline: "float | None") -> Boundary:
    """Hold *loop*'s cleanup now; for a loop that is not running yet or has just started.

    Raises:
        RuntimeError: *loop* is closing its hosts, was shut down by its
            runner, or is closed; or this is called from inside a sweep.
    """
    registry = _acquirable(loop, what="acquire_boundary")
    if registry.state == "draining":
        raise RuntimeError(
            "acquire_boundary: this loop is closing its hosts; only a waiting acquisition "
            "(open_context) can enter now"
        )
    registry.held += 1
    return Boundary(loop, deadline)


async def acquire_boundary_waiting(
    loop: "asyncio.AbstractEventLoop", *, deadline: "float | None"
) -> Boundary:
    """Hold *loop*'s cleanup, waiting out a sweep in progress; ``open_context``'s entry point.

    Waiters are admitted together, in arrival order, when the sweep ends,
    each counted as held at that moment. A waiter cancelled after its grant
    gives it back as an ordinary release; one cancelled before leaves the
    queue.

    Raises:
        RuntimeError: as :func:`acquire_boundary`, except that a sweep in
            progress is waited for.
    """
    import asyncio

    registry = _acquirable(loop, what="acquire_boundary_waiting")
    if registry.state == "open":
        registry.held += 1
        return Boundary(loop, deadline)
    waiter = loop.create_future()
    registry.waiters.append(waiter)
    try:
        await waiter
    except asyncio.CancelledError:
        if waiter.done() and not waiter.cancelled() and waiter.exception() is None:
            await Boundary(loop, deadline).release(label="a cancelled wait for the loop's sweep")
        elif waiter in registry.waiters:
            registry.waiters.remove(waiter)
        raise
    return Boundary(loop, deadline)


async def sweep_unheld(
    loop: "asyncio.AbstractEventLoop", *, label: str, deadline: "float | None"
) -> list[str]:
    """Close every host registered on *loop* now: the engine of ``OttoContext.sweep_loop``.

    Raises:
        RuntimeError: a cleanup boundary holds *loop* (its last release closes
            these hosts), *loop* is already closing its hosts, or its runner
            shut it down.
    """
    registry = _REGISTRIES.get(loop)
    if registry is None:
        return []
    if registry.state == "shut":
        raise RuntimeError(
            f"sweep_loop cannot sweep {label}: its runner has shut that loop's host registry down"
        )
    if registry.state == "draining" or _SWEEPING.get():
        raise RuntimeError(
            f"sweep_loop cannot sweep {label}: that loop is already closing its hosts"
        )
    if registry.held:
        raise RuntimeError(
            f"sweep_loop cannot sweep {label} while a cleanup boundary holds it: open_context, "
            "run_command or the test runner closes that loop's hosts when its boundary is released"
        )
    try:
        return await _drain(loop, registry, label=label, deadline=deadline, shut=False)
    finally:
        _forget_if_idle(loop, registry)


async def shut_down(
    loop: "asyncio.AbstractEventLoop", *, label: str, deadline: "float | None"
) -> list[str]:
    """Close every host registered on *loop* whatever the count, then refuse new boundaries.

    A pytest runner's finalizer runs it on the runner's loop before the runner
    closes that loop. The registry stays, shut, until the loop closes; the
    next registration, boundary acquisition or shutdown on any loop then
    prunes it.
    """
    _prune_closed_loops(loop)
    registry = _REGISTRIES.setdefault(loop, _Registry())
    if registry.state == "shut":
        return []
    if registry.state == "draining":
        raise RuntimeError(f"cannot shut {label} down while it is closing its hosts")
    return await _drain(loop, registry, label=label, deadline=deadline, shut=True)


async def _drain(
    loop: "asyncio.AbstractEventLoop",
    registry: _Registry,
    *,
    label: str,
    deadline: "float | None",
    shut: bool,
) -> list[str]:
    """Close the registry's hosts in dependency order, bounded by *deadline*; one per loop.

    The drain ends when the registry is empty: a host registered after the
    sweep's last close, before the drain ends, is closed by the same drain.

    A cancelled caller does not reopen the loop early. The drain keeps closing
    under its own deadline, and only then lets the waiters in and re-raises. A
    second cancellation, an interrupt, the sweep being cancelled from outside,
    or the sweep failing cuts it short: the closes get two bounded graces, then
    every outstanding record is abandoned at its generation before anyone is
    admitted. An interrupt (``KeyboardInterrupt``, ``SystemExit``) is what
    propagates over a cancellation.
    """
    import asyncio
    import contextvars

    if asyncio.get_running_loop() is not loop:
        raise RuntimeError(f"{label} can only be swept on its own loop, while that loop runs")
    registry.state = "draining"  # before the first await: one sweep per loop
    changed = registry.changed = asyncio.Event()
    closed: list[str] = []
    in_flight: "dict[int, tuple[asyncio.Task[bool], HostRegistration]]" = {}
    started = loop.time()
    sweep_context = contextvars.copy_context()
    sweep_context.run(_SWEEPING.set, True)  # the closes it spawns inherit it

    def remaining() -> "float | None":
        return None if deadline is None else max(0.0, deadline - (loop.time() - started))

    warned: "set[tuple[int, int]]" = set()  # records whose ownership check already warned

    def start_body() -> "asyncio.Task[Any]":
        return sweep_context.run(
            loop.create_task, _sweep(loop, registry, changed, closed, in_flight, warned)
        )

    body = start_body()

    async def until_empty() -> None:
        nonlocal body
        while True:
            await asyncio.wait({body}, timeout=remaining())
            if not body.done() or body.cancelled() or body.exception() is not None:
                return
            # The body returned with the registry empty, but a host may have registered
            # since, before this drain resumed: with time left, the same drain closes it.
            if not registry.records or remaining() == 0.0:
                return
            body = start_body()

    interrupted: "BaseException | None" = None
    # A further cancel or an interrupt ended the drain, not its deadline.
    cut_short_by_caller = False
    try:
        await until_empty()
    except asyncio.CancelledError as exc:
        # The caller was cancelled. Admission stays closed while old closes still
        # have authority: the drain finishes, bounded by its own deadline, first.
        interrupted = exc
        try:
            await until_empty()
        except asyncio.CancelledError:  # a further cancel: cut short now
            cut_short_by_caller = True
        except BaseException as exc:  # noqa: BLE001 — KeyboardInterrupt, SystemExit: cut short now
            interrupted = _outranking(interrupted, exc)
            cut_short_by_caller = True
    except BaseException as exc:  # noqa: BLE001 — KeyboardInterrupt, SystemExit: cut short now
        interrupted = exc
        cut_short_by_caller = True
    failed = body.done() and not body.cancelled() and body.exception() is not None
    # Records left after a body that returned mean the deadline ran out first.
    expired = not body.done() or body.cancelled() or failed or bool(registry.records)
    if expired:
        # The closes the expiry interrupts: those still running as it starts.
        interrupting = [(task, r) for task, r in in_flight.values() if not task.done()]
        try:
            await _cut_short([body, *(task for task, _ in in_flight.values())])
        except BaseException as exc:  # noqa: BLE001 — a cancel during the graces: still abandon
            interrupted = _outranking(interrupted, exc)
        # The closes this drain cut short and that did not finish: cancelled, still
        # running, or raising on the way out. One may have removed its own record
        # then, but its connections are still half-closed.
        cut = [r for task, r in interrupting if not _close_finished(task)]
        if failed:
            why = "failed"
        elif cut_short_by_caller:
            why = "was cut short"
        else:
            why = "ran past its deadline"
        # Abandonment clears each owner and bumps each generation, so a close that
        # still runs can no longer touch a connection: only now may anyone enter.
        _abandon_outstanding(
            loop,
            registry,
            cut,
            label=label,
            why=why,
            elapsed=loop.time() - started,
        )
    _end_drain(registry, shut=shut)
    if interrupted is not None:
        raise interrupted
    if failed:
        body.result()  # a defect in the sweep itself surfaces, never hides
    if closed:
        noun = "host" if len(closed) == 1 else "hosts"
        logger.debug(f"closed {len(closed)} {noun} at end of {label}: {', '.join(closed)}")
    return list(closed)


def _outranking(held: "BaseException | None", new: BaseException) -> BaseException:
    """Pick the exception a drain re-raises: an interrupt outranks a cancellation."""
    import asyncio

    if held is None or (
        isinstance(held, asyncio.CancelledError) and not isinstance(new, asyncio.CancelledError)
    ):
        return new
    return held


async def _sweep(
    loop: "asyncio.AbstractEventLoop",
    registry: _Registry,
    changed: "asyncio.Event",
    closed: list[str],
    in_flight: "dict[int, tuple[asyncio.Task[bool], HostRegistration]]",
    warned: "set[tuple[int, int]]",
) -> None:
    """Start every outstanding close nothing outstanding depends on; recompute on each change."""
    import asyncio

    records = registry.records
    while True:
        unowned = [
            k for k, r in records.items() if k not in in_flight and not _owned(loop, r, warned)
        ]
        for key in unowned:
            del records[key]  # moved to another loop, or closed by its own code
        if not records and not in_flight:
            return
        depended = {dep for key, r in records.items() for dep in r.depends_on if dep != key}
        ready = [r for k, r in records.items() if k not in in_flight and k not in depended]
        if not ready and not in_flight:
            ready = [r for k, r in records.items() if k not in in_flight]  # a cycle: close all
        for record in ready:
            task = loop.create_task(_close_one(registry, record, closed))
            in_flight[record.key] = (task, record)
        if not in_flight:
            continue
        changed.clear()
        woken = loop.create_task(changed.wait())
        try:
            await asyncio.wait(
                {woken, *(task for task, _ in in_flight.values())},
                return_when=asyncio.FIRST_COMPLETED,
            )
        finally:
            woken.cancel()
        for key, (task, record) in [(k, v) for k, v in in_flight.items() if v[0].done()]:
            del in_flight[key]
            if task.cancelled():  # the close raised CancelledError by itself: a failed close
                logger.warning(
                    f"otto: closing host {record.display_id!r} failed during the loop's sweep: "
                    "CancelledError()"
                )
                _remove_if_current(registry, record)


def _owned(
    loop: "asyncio.AbstractEventLoop",
    record: HostRegistration,
    warned: "set[tuple[int, int]] | None" = None,
) -> bool:
    """Ask *record* whether *loop* owns its host; a failing check is a warning and counts as owned.

    Counting it as owned keeps the record in the sweep, which then closes or abandons it.
    A record already in *warned* (its key and generation) fails quietly: a sweep asks
    again on every recompute, and one warning per record says it.
    """
    try:
        return record.owned_by(loop)
    except Exception as exc:  # noqa: BLE001 — one host's defect never stops the sweep
        if warned is None or (record.key, record.generation) not in warned:
            logger.warning(
                f"otto: the ownership check of host {record.display_id!r} failed: {exc!r}"
            )
        if warned is not None:
            warned.add((record.key, record.generation))
        return True


async def _close_one(registry: _Registry, record: HostRegistration, closed: list[str]) -> bool:
    """Close one record and return whether its close finished; a failure is a warning.

    Completion, failed or not, removes the record at its generation. The
    result tells an expiry which interrupted closes still need abandoning: a
    close that raised did not finish.

    A record that a newer registration of its host replaced, or that left the
    registry, before this close started is not closed: the host's close acts
    on whatever generation is current when it starts, which is the
    replacement's. The sweep closes the replacement as its own record.
    """
    import asyncio

    current = registry.records.get(record.key)
    if current is None or current.generation != record.generation:
        return True  # nothing of this record's is left to close
    finished = False
    try:
        await record.close()
    except asyncio.CancelledError:
        raise
    except BaseException as exc:  # noqa: BLE001 — a failed close is a warning, whatever it raised
        logger.warning(
            f"otto: closing host {record.display_id!r} failed during the loop's sweep: {exc!r}"
        )
    else:
        finished = True
        closed.append(record.display_id)
    _remove_if_current(registry, record)
    return finished


def _close_finished(task: "asyncio.Task[bool]") -> bool:
    """Whether a close task ended by finishing its close (see :func:`_close_one`)."""
    return task.done() and not task.cancelled() and task.exception() is None and task.result()


def _remove_if_current(registry: _Registry, record: HostRegistration) -> None:
    current = registry.records.get(record.key)
    if current is not None and current.generation == record.generation:
        del registry.records[record.key]
    registry.poke()


async def _cut_short(tasks: "list[asyncio.Task[Any]]") -> None:
    """Cancel an expired sweep's tasks twice, a bounded grace each; leave refusers to finish."""
    import asyncio

    for _ in range(2):
        pending = [t for t in tasks if not t.done()]
        if not pending:
            return
        for task in pending:
            task.cancel()
        await asyncio.wait(pending, timeout=_SWEEP_GRACE)
    for task in tasks:
        if not task.done():
            task.add_done_callback(lambda t: t.cancelled() or t.exception())


def _abandon_outstanding(
    loop: "asyncio.AbstractEventLoop",
    registry: _Registry,
    cut: "list[HostRegistration]",
    *,
    label: str,
    why: str,
    elapsed: float,
) -> None:
    """In one step with no await: abandon every outstanding record, and leave none.

    *cut* are the records of the closes the drain cut short. One whose close
    already removed it is abandoned too, at its own generation: a host that
    reconnected since ignores it. One a newer registration of the same host
    replaced is left to that registration's abandonment. The warning names
    only the hosts whose abandon dropped something, never a stale no-op.
    """
    stuck = list(registry.records.values())
    stuck += [r for r in cut if r.key not in registry.records]
    dropped = [record.display_id for record in stuck if _abandon_one(loop, record)]
    registry.records.clear()
    ids = ", ".join(dropped) or "none"
    logger.warning(
        f"otto: closing hosts at end of {label} {why}; gave up after "
        f"{elapsed:.1f}s and dropped the connections of: {ids}"
    )


def _abandon_one(loop: "asyncio.AbstractEventLoop", record: HostRegistration) -> bool:
    """Abandon one record and return whether it dropped anything; a failure is a warning."""
    try:
        return record.abandon(loop, record.generation)
    except Exception as exc:  # noqa: BLE001 — one host's abandon never stops the others'
        logger.warning(f"otto: abandoning host {record.display_id!r} failed: {exc!r}")
        return False


def _end_drain(registry: _Registry, *, shut: bool) -> None:
    """End a drain: back to open (or shut); admit every waiter at once, in arrival order."""
    registry.state = "shut" if shut else "open"
    registry.changed = None
    waiters, registry.waiters = registry.waiters, []
    for waiter in waiters:
        if waiter.done():
            continue
        if shut:
            waiter.set_exception(
                RuntimeError("this loop's runner shut its host registry down during the wait")
            )
        else:
            registry.held += 1
            waiter.set_result(None)


def abandon_closed_loops() -> list[str]:
    """Drop the connections of hosts whose loop closed unswept; the test suite's backstop.

    Hosts that have since moved to another loop are left alone. A host whose
    ownership check or abandon raises is a warning, never a stop. Returns the
    ids whose connections were dropped (a stale abandonment drops nothing),
    logged at debug level.
    """
    abandoned: list[str] = []
    for loop in [lp for lp in _REGISTRIES if lp.is_closed()]:
        abandoned.extend(
            record.display_id
            for record in _forget_registry(loop).records.values()
            if _owned(loop, record) and _abandon_one(loop, record)
        )
    if abandoned:
        noun = "host" if len(abandoned) == 1 else "hosts"
        logger.debug(
            f"abandoned {len(abandoned)} {noun} left on closed loops: {', '.join(abandoned)}"
        )
    return abandoned


def forget_closed_loops() -> None:
    """Forget closed loops' registries without abandoning anything (the test guard's cleanup)."""
    for loop in [lp for lp in _REGISTRIES if lp.is_closed()]:
        _forget_registry(loop)


_FORGET_OBSERVER: "Callable[[RegistrySnapshot], None] | None" = None
"""Test support: told of every registry forgotten while it still holds records.

The test suite installs one so a host left on a wider-scoped pytest loop is
reported even when a later registration's pruning forgets that loop."""


def _forget_registry(loop: "asyncio.AbstractEventLoop") -> _Registry:
    """Remove *loop*'s registry and return it, first telling the observer if it held records.

    The only code that removes a registry (registration pruning, an idle
    registry after a sweep, :func:`forget_closed_loops`,
    :func:`abandon_closed_loops`), so the observer sees every registry that
    disappears with hosts still in it.
    """
    registry = _REGISTRIES.pop(loop)
    if registry.records and _FORGET_OBSERVER is not None:
        _FORGET_OBSERVER(_snapshot(loop, registry))
    return registry


def _snapshot(loop: "asyncio.AbstractEventLoop", registry: _Registry) -> RegistrySnapshot:
    return RegistrySnapshot(
        loop,
        registry.held,
        registry.state,
        loop.is_closed(),
        [x.display_id for x in registry.records.values()],
    )


def open_registrations() -> list[RegistrySnapshot]:
    """Every open loop's registry that still holds records, for the test suite's orphan guard."""
    return [_snapshot(lp, r) for lp, r in _REGISTRIES.items() if not lp.is_closed() and r.records]


def all_registrations() -> list[RegistrySnapshot]:
    """Every registry that still holds records, closed loops included, for the session-end check."""
    return [_snapshot(lp, r) for lp, r in _REGISTRIES.items() if r.records]
