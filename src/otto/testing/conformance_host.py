"""Conformance suites for otto's two host-side extension points.

Two of :mod:`otto.testing`'s six helpers live here rather than beside the other
four in :mod:`otto.testing.conformance`, because a host class and a transfer
backend are the only two interfaces whose contract is a CALL SHAPE plus a
DECLARATION — :func:`assert_host_conforms` is the one surface in otto that
reads :class:`~otto.host.capability_grid.HostCapabilities` back against the
behaviour it promises. Everything they need (the ``Host`` protocol, the
capability grid, the dry-run context) is host machinery the other four never
import.

Both helpers collect every violation on one
:class:`~otto.suite.expect.ExpectCollector` and raise a single
``AssertionError``, exactly as the other four do.
"""

import asyncio
import inspect
from collections.abc import Callable, Coroutine, Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from ..config.lab import Lab
from ..context import OttoContext, reset_context, set_context
from ..host.capability_grid import HostCapabilities, UserSupport
from ..host.host import BaseHost, Host
from ..host.transfer.base import BaseFileTransfer, ProgressGranularity
from ..suite.expect import ExpectCollector

_PROBE_USER = "__otto_conformance_probe_user__"
"""The ``user=`` every behavioural probe passes.

Non-empty and whitespace-free, so ``otto.host.host._validate_user`` accepts it
and the probe reaches the family's own answer rather than dying on the
argument. Obviously not a real login, so a family that somehow got as far as
using it fails loudly instead of touching an account.
"""

_PROBE_COMMAND = "true"
"""The command every ``run``/``exec`` probe carries. A dry run issues none."""

_PROBE_SRC = Path("__otto_conformance_probe_file__")
_PROBE_DEST = Path("__otto_conformance_probe_dir__")
"""Transfer probe paths. Neither is opened: a dry run computes destinations and
moves no bytes, and every ``refused`` family answers before even that."""

_USER_VERBS = {
    "run": "run_user",
    "exec": "exec_user",
    "put": "put_user",
    "get": "get_user",
}
"""The four verbs that take ``user=``, mapped to the field declaring how they
answer it. Anything absent from :class:`~otto.host.capability_grid.HostCapabilities`
has no promise to probe."""

_HOST_VERBS = ("run", "exec", "put", "get", "open_session", "login")
"""The verbs whose call shape production depends on, checked structurally."""


@contextmanager
def _dry_run() -> "Iterator[None]":
    """Install a dry-run :class:`~otto.context.OttoContext` for the block.

    A fresh context rather than a copy of whatever is active: the probe wants
    ``dry_run=True`` and nothing else from the caller's environment, and the
    token restores the previous context (including none) on the way out.
    """
    token = set_context(OttoContext(lab=Lab(name="otto-conformance"), dry_run=True))
    try:
        yield
    finally:
        reset_context(token)


def _keyword_names(func: object) -> list[str]:
    """Parameter names *func* accepts by keyword, ``self`` excluded.

    ``**kwargs`` is reported as the literal ``"**"`` so a caller can tell "this
    signature swallows anything" from "this signature names nothing".

    *func* is typed ``object`` because both call sites read it off a CLASS
    (``getattr(Host, verb)``, ``getattr(BaseFileTransfer, meth)``) rather than
    an instance, where nothing statically guarantees the attribute is
    callable. The ``callable()`` check below narrows it for
    :func:`inspect.signature` and gives a caller a clearer error than the
    ``TypeError`` ``inspect.signature`` would raise on a non-callable.
    """
    if not callable(func):
        raise TypeError(f"_keyword_names: {func!r} is not callable")
    names = []
    for name, param in inspect.signature(func).parameters.items():
        if name == "self":
            continue
        if param.kind is inspect.Parameter.VAR_KEYWORD:
            names.append("**")
        elif param.kind in (
            inspect.Parameter.POSITIONAL_OR_KEYWORD,
            inspect.Parameter.KEYWORD_ONLY,
        ):
            names.append(name)
    return names


def _check_signature(
    c: ExpectCollector, label: str, expected_on: object, actual_on: object
) -> None:
    """Expect every keyword *expected_on* names to be one *actual_on* accepts.

    The expected set is READ OFF the reference implementation rather than
    retyped here, so this rule cannot drift from the interface it guards: add a
    parameter to the protocol and every conforming class is asked for it on the
    next run.
    """
    try:
        expected = _keyword_names(expected_on)
        actual = _keyword_names(actual_on)
    except (TypeError, ValueError) as e:
        c.expect(False, f"{label}: signature is unreadable ({type(e).__name__}: {e})")
        return
    if "**" in actual:
        return
    missing = [name for name in expected if name not in actual]
    c.expect(
        not missing,
        f"{label}: does not accept {', '.join(missing)} by keyword; "
        f"production passes {', '.join(expected)}",
    )


def _await_probe(coro_fn: "Callable[[], Coroutine[Any, Any, object]]") -> "Exception | None":
    """Run one probe under dry-run and return what it raised, or ``None``.

    ``asyncio.run`` needs a thread with no running loop, which is why both
    helpers are documented as synchronous-test helpers. Catches
    ``Exception``, not ``BaseException`` -- a probe's outcome is the
    measurement, but ``KeyboardInterrupt``/``SystemExit`` are the caller
    asking the process to stop, not something a conforming class can be
    blamed for; folding them into a violation string would swallow the
    interrupt instead of propagating it.
    """

    async def _run() -> None:
        await coro_fn()

    try:
        with _dry_run():
            asyncio.run(_run())
    except Exception as e:  # noqa: BLE001 — the probe's outcome IS the measurement
        return e
    return None


def _refuse_inside_a_running_loop() -> None:
    """Raise before any probe when this thread already has a running event loop.

    ``asyncio.run`` refuses to nest, and the resulting ``RuntimeError`` would
    otherwise be caught per verb by :func:`_await_probe` and reported four times
    over as a violation of the caller's class — blaming their host for the way
    their test is declared. Named here instead, once, as what it is.
    """
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return
    raise RuntimeError(
        "assert_host_conforms(instance=...) must be called from a SYNCHRONOUS "
        "test: its probes drive their own event loop, and this thread already "
        "has one running (an `async def` test, e.g. under "
        "`@pytest.mark.asyncio`). Call it from a plain `def test_...`; nothing "
        "about it needs to await."
    )


def _probe(instance: object, verb: str) -> "Exception | None":
    """Call *verb* on *instance* with ``user=`` set, under a dry run."""
    call = getattr(instance, verb)
    if verb in ("run", "exec"):
        return _await_probe(lambda: call(_PROBE_COMMAND, user=_PROBE_USER))
    return _await_probe(lambda: call([_PROBE_SRC], _PROBE_DEST, user=_PROBE_USER))


def assert_host_conforms(cls: type, *, instance: "Host | None" = None) -> None:
    """Assert *cls* satisfies the :class:`~otto.host.host.Host` contract it declares.

    Structural rules always run: *cls* is a
    :class:`~otto.host.host.BaseHost` subclass carrying a
    :class:`~otto.host.capability_grid.HostCapabilities` declaration (which
    :func:`~otto.host.os_profile.register_host_class` also demands), and its
    ``run``/``exec``/``put``/``get``/``open_session``/``login`` accept, by
    keyword, every parameter the ``Host`` protocol names — read off the
    protocol's own signatures at call time, so adding a parameter there asks
    every conforming class for it rather than silently exempting them.

    Behavioural rules run when *instance* is supplied, and they are the point:
    they check the DECLARATION against what the class does. Each verb is called
    once with ``user=`` set, inside a dry-run
    :class:`~otto.context.OttoContext`, so nothing connects and no bytes move.
    A verb declared :attr:`~otto.host.capability_grid.UserSupport.refused` must
    raise :exc:`NotImplementedError` — under a dry run too, which is what
    forces the refusal above the dry-run arm rather than behind it. A verb
    declared anything else must not.

    THE DECLARATION IS PER CLASS; BEHAVIOUR CAN BE PER INSTANCE.
    :class:`~otto.host.capability_grid.HostCapabilities` has one row per family
    and no dimension for a host's own configuration, so a class whose answer
    depends on how the instance is configured will conform on one instance and
    report a violation on another — both truthfully. otto's own ``unix`` row is
    the in-tree example: it declares ``exec_user=authenticate``, which holds
    over ``term="ssh"``, while a ``term="telnet"`` ``UnixHost`` refuses (telnet
    has no stateless exec channel to authenticate on) and is reported here. A
    violation on such a class is a statement about THAT INSTANCE's
    configuration, not a defect in the class; probe the configuration your
    declaration speaks for, and read the note on your family's row for the
    conditions it carries.

    Construct *instance* the way otto's own suite does: a host is built from
    lab data or directly (``UnixHost(ip=..., element=..., creds=[...])``,
    ``ZephyrHost(ip=..., element=...)``, ``LocalHost()``,
    ``DockerContainerHost(parent=..., container_id=..., ...)``), and the
    dry-run context this helper installs keeps every probe off the wire. Closing
    the instance afterwards is the caller's job.

    Raises:
        AssertionError: once, listing every violation found.
        RuntimeError: called with an *instance* from an ``async def`` test. The
            probes drive their own event loop, so call this from a plain
            synchronous test.
    """
    c = ExpectCollector()
    c.expect(
        isinstance(cls, type) and issubclass(cls, BaseHost),
        f"Host: {cls!r} must be a subclass of otto.host.host.BaseHost",
    )
    capabilities = getattr(cls, "capabilities", None)
    declared = isinstance(capabilities, HostCapabilities)
    c.expect(
        declared,
        f"Host: {getattr(cls, '__name__', cls)!r} must declare a HostCapabilities in "
        f"`capabilities` saying what its verbs promise, got {capabilities!r}",
    )
    for verb in _HOST_VERBS:
        actual = getattr(cls, verb, None)
        if actual is None:
            c.expect(False, f"Host.{verb}: missing")
            continue
        c.expect(
            inspect.iscoroutinefunction(actual),
            f"Host.{verb}: must be an async def — production awaits it",
        )
        _check_signature(c, f"Host.{verb}", getattr(Host, verb), actual)
    if instance is not None and declared:
        _expect_declaration_holds(c, capabilities, instance)
    c.raise_if_failures()


def _expect_declaration_holds(
    c: ExpectCollector,
    capabilities: HostCapabilities,
    instance: object,
) -> None:
    """Probe each ``user=``-taking verb against what *capabilities* promises for it."""
    _refuse_inside_a_running_loop()
    for verb, field_name in _USER_VERBS.items():
        support = getattr(capabilities, field_name)
        raised = _probe(instance, verb)
        if support is UserSupport.refused:
            c.expect(
                isinstance(raised, NotImplementedError),
                f"Host.{verb}: declared {field_name}={support.value!r}, so "
                f"{verb}(user=...) must raise NotImplementedError under a dry run "
                f"too — the refusal has to sit above the dry-run arm; got "
                f"{_outcome(raised)}",
            )
        elif isinstance(raised, NotImplementedError):
            c.expect(
                False,
                f"Host.{verb}: declared {field_name}={support.value!r}, but "
                f"{verb}(user=...) raised NotImplementedError: {raised}",
            )
        else:
            c.expect(
                raised is None,
                f"Host.{verb}: declared {field_name}={support.value!r}, but a dry-run "
                f"{verb}(user=...) raised {_outcome(raised)}",
            )


def _outcome(raised: "Exception | None") -> str:
    """Render a probe's outcome for a failure message."""
    if raised is None:
        return "no exception"
    return f"{type(raised).__name__}: {raised}"


def assert_transfer_backend_conforms(cls: "type[BaseFileTransfer]") -> None:
    """Assert *cls* satisfies the :class:`~otto.host.transfer.BaseFileTransfer` contract.

    Structural throughout — a transfer backend is constructed only through
    ``create(ctx)`` with a live host's context, so there is nothing to probe
    without a lab. The rules are the ones
    :func:`~otto.host.transfer.register_transfer_backend` enforces at
    registration plus the call shape the host relies on afterwards:

    * a non-empty ``frozenset[str]`` in
      :attr:`~otto.host.transfer.BaseFileTransfer.host_families` — a backend
      applicable to no family can never validate against a host;
    * a :class:`~otto.host.transfer.base.ProgressGranularity` in
      :attr:`~otto.host.transfer.BaseFileTransfer.progress_granularity`;
    * ``put_files``/``get_files`` (what the host calls) and
      ``_run_put``/``_run_get`` (what the base class calls) accepting every
      keyword ``BaseFileTransfer``'s own definitions name, read off those
      definitions rather than retyped here;
    * an overriding ``create`` classmethod — the base's raises, so a backend
      that inherits it cannot be built from a
      :class:`~otto.host.transfer.TransferContext`;
    * nothing left abstract, so the class can be instantiated at all.

    Raises:
        AssertionError: once, listing every violation found.
    """
    c = ExpectCollector()
    c.expect(
        isinstance(cls, type) and issubclass(cls, BaseFileTransfer),
        f"BaseFileTransfer: {cls!r} must be a subclass of otto.host.transfer.BaseFileTransfer",
    )
    families = getattr(cls, "host_families", None)
    c.expect(
        isinstance(families, frozenset)
        and bool(families)
        and all(isinstance(f, str) for f in families),
        f"BaseFileTransfer: host_families must be a non-empty frozenset[str] naming the "
        f"host families this backend serves, got {families!r}",
    )
    granularity = getattr(cls, "progress_granularity", None)
    c.expect(
        isinstance(granularity, ProgressGranularity),
        f"BaseFileTransfer: progress_granularity must be a ProgressGranularity saying what "
        f"this backend promises the progress bar, got {granularity!r}",
    )
    for meth in ("put_files", "get_files", "_run_put", "_run_get"):
        actual = getattr(cls, meth, None)
        if actual is None:
            c.expect(False, f"BaseFileTransfer.{meth}: missing")
            continue
        c.expect(
            inspect.iscoroutinefunction(actual),
            f"BaseFileTransfer.{meth}: must be an async def — the base class awaits it",
        )
        _check_signature(c, f"BaseFileTransfer.{meth}", getattr(BaseFileTransfer, meth), actual)
    create = getattr(cls, "create", None)
    c.expect(
        inspect.ismethod(create),
        f"BaseFileTransfer.create: must be a classmethod taking a TransferContext, got {create!r}",
    )
    c.expect(
        inspect.ismethod(create) and create.__func__ is not BaseFileTransfer.create.__func__,
        "BaseFileTransfer.create: must be overridden — the base implementation raises "
        "NotImplementedError, so a host could never build this backend",
    )
    left_abstract = sorted(getattr(cls, "__abstractmethods__", ()))
    c.expect(
        not left_abstract,
        f"BaseFileTransfer: leaves {', '.join(left_abstract)} abstract, so it cannot be "
        f"instantiated",
    )
    c.raise_if_failures()
