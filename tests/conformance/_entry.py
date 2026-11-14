"""How a contract's command reaches a cell's host: the ENTRY, and the route it must take.

A command contract asserts one property of ``host.run``, ``host.exec`` and
``host.exec`` forced onto the pooled shell alike -- three entry points that
must agree on the same input (``AGENTS.md``, "ONE DECISION, ONE PLACE"). So
the entry is part of the CELL a contract is parametrised over, and every
command a contract or control issues goes through :func:`issue`, which also
asserts the route the call took (:func:`check_route`).

THE EXPECTED ROUTE IS READ OFF THE LAB ENTRY'S FACTS, never off the host's own
resolver (``SessionManager.exec_route``). A prediction computed by the code
under test agrees with that code by construction, so it could never go red;
the cell's ``family``, ``term`` and ``proxied_login`` are facts the venue read
from the entry it built the host from, and the table in
:func:`expected_route` is this suite's independent statement of what otto
should do with them.

WHICH CONTRACTS ARE COMMAND CONTRACTS is declared on the contract itself, by
``@pytest.mark.entries("run", "exec", "exec-shell")``, and read here by AST
(:func:`entries_declared`) the way ``tests/conformance/_controls.py`` reads
``positive_control`` and ``tests/conformance/_observable.py`` reads
``observable``: the matrix's rows are derived without collecting anything.
:func:`entries_declared` is the ONLY reader of that marker.

A marked contract does not run over the full cell space times its entries:
the entry, not the transfer, is what varies a command, so the space is first
PROJECTED to one cell per ``(element, term)`` (:func:`project`) and then
crossed with the entries the contract declares and the cell admits
(:func:`space_for`). An unmarked contract keeps the full, entry-less space.
"""

import ast
import contextlib
import dataclasses
from collections.abc import AsyncIterator, Callable, Iterator
from contextlib import AbstractAsyncContextManager
from typing import Any

from otto.host.embedded_host import EmbeddedHost
from otto.host.exec_route import ExecRoute
from otto.host.host import BaseHost
from otto.host.local_host import LocalHost
from otto.result import CommandResult
from tests._fixtures.paths import CHECKOUT_ROOT
from tests._fixtures.session_setup_hooks import noop_session_setup as _temporary_noop_hook
from tests.conformance._controls import mark_string_arguments, walk_test_functions
from tests.conformance._resolved import ResolvedCell
from tests.conformance._sample import cell_label

RUN, EXEC, EXEC_SHELL = "run", "exec", "exec-shell"
"""The three ways a command is issued: ``run``, ``exec``, and ``exec`` forced
onto the pooled shell by a registered no-op ``session_setup`` hook."""

ENTRIES_MARK = "entries"
"""``@pytest.mark.entries("run", "exec", "exec-shell")`` on a command contract."""

ALL_ENTRIES: list[str] = [RUN, EXEC, EXEC_SHELL]

UNIX, EMBEDDED, LOCAL_FAMILY = "unix", "embedded", "local"
"""The values :attr:`ResolvedCell.family` takes."""

NOOP_SETUP_HOOK = "conformance-exec-shell-noop"
"""The name PREFIX of the one session-setup hook this suite registers (each context
adds a unique suffix). Declaring any hook sends ``exec`` to the pooled shell;
this one does nothing else."""

_BUSYBOX_ARTIFACT = "busybox-artifact"
_SSH, _TELNET, _CONSOLE = "ssh", "telnet", "console"


def entries_declared(contract: str) -> "list[str]":
    """The entries *contract*'s ``@pytest.mark.entries(...)`` names; ``[]`` if unmarked.

    *contract* is a nodeid (``path::name``, an optional ``[param]`` ignored),
    relative to the checkout. A nodeid whose file is not in this checkout
    (a fabricated contract in a unit test) carries no marker and answers
    ``[]``. A marker naming an entry this module does not know RAISES: a
    misspelt entry would otherwise silently drop that leg of the contract.
    """
    base = contract.split("[", 1)[0]
    path = CHECKOUT_ROOT / base.split("::", 1)[0]
    if not path.is_file():
        return []
    for nodeid, node in walk_test_functions(path):
        if nodeid == base:
            return entries_of_node(node)
    return []


def entries_of_node(node: "ast.FunctionDef | ast.AsyncFunctionDef") -> "list[str]":
    """The arguments of *node*'s ``@pytest.mark.entries(...)`` decorator, in order."""
    for decorator in node.decorator_list:
        names = mark_string_arguments(decorator, ENTRIES_MARK)
        if names is None:
            continue
        unknown = [name for name in names if name not in ALL_ENTRIES]
        if not names or unknown:
            raise ValueError(
                f"{node.name}: @pytest.mark.{ENTRIES_MARK} must name string literals among "
                f"{ALL_ENTRIES}; unreadable or unknown: {unknown or names}"
            )
        return names
    return []


#: Why :func:`entries_of` withholds each entry it withholds from some cells, in a
#: reader's words: the command contract page prints it beside the hosts the entry
#: is not checked on (``scripts/render_support_matrix.py``, ``not_checked``). It
#: sits next to the rule so a change to one is a change in front of the other.
ENTRY_REASON = {
    EXEC: (
        "there the applets are put on the `PATH` of the `run` session only, which "
        "a one-off `exec` process never sees"
    ),
    EXEC_SHELL: (
        "only there does `exec` otherwise get an SSH channel of its own, which "
        "`exec-shell` sends through a shell session instead"
    ),
}


def entries_of(resolved: ResolvedCell) -> "list[str]":
    """The entries *resolved*'s host can be reached by, in :data:`ALL_ENTRIES` order.

    A BusyBox-artifact cell is ``run`` only: its applets are put on ``PATH``
    inside the ``run`` session, which an ``exec`` subprocess never sees.
    ``exec-shell`` needs a raw channel to force off, so only an ``ssh`` cell
    whose login is not proxied has one (a proxied login is on the pooled shell
    already). :data:`ENTRY_REASON` says each of these to a reader.
    """
    if resolved.kind == _BUSYBOX_ARTIFACT:
        return [RUN]
    if _has_raw_channel_to_force_off(resolved):
        return [RUN, EXEC, EXEC_SHELL]
    return [RUN, EXEC]


def _has_raw_channel_to_force_off(resolved: ResolvedCell) -> bool:
    return resolved.family == UNIX and resolved.cell.term == _SSH and not resolved.proxied_login


def expected_route(resolved: ResolvedCell) -> str:
    """The route a command issued through *resolved*'s entry must report.

    From the cell's facts alone; see the module docstring for why never from
    the host's own resolver.
    """
    entry = resolved.entry
    if entry == RUN:
        return ExecRoute.DEFAULT_SESSION.value
    if entry not in entries_of(resolved):
        raise ValueError(f"{cell_label(resolved)} has no {entry!r} entry")
    if entry == EXEC_SHELL:
        return ExecRoute.POOLED_SHELL.value
    if resolved.family == LOCAL_FAMILY:
        return ExecRoute.LOCAL_SUBPROCESS.value
    if resolved.family == EMBEDDED or resolved.cell.term == _CONSOLE:
        return ExecRoute.DEFAULT_SESSION.value
    if resolved.cell.term == _SSH and not resolved.proxied_login:
        return ExecRoute.SSH_CHANNEL.value
    if resolved.cell.term in {_SSH, _TELNET}:
        return ExecRoute.POOLED_SHELL.value
    raise ValueError(
        f"no expected exec route for family {resolved.family!r} on term {resolved.cell.term!r}"
    )


def project(space: "list[ResolvedCell]") -> "list[ResolvedCell]":
    """The first cell of *space* per ``(element, term)``, in *space*'s order."""
    seen: set[tuple[str, str]] = set()
    projected: list[ResolvedCell] = []
    for resolved in space:
        key = (resolved.cell.element, resolved.cell.term)
        if key not in seen:
            seen.add(key)
            projected.append(resolved)
    return projected


def with_entry(resolved: ResolvedCell, entry: str) -> ResolvedCell:
    """*resolved*, reached by *entry*."""
    return dataclasses.replace(resolved, entry=entry)


def space_for(contract: str, space: "list[ResolvedCell]") -> "list[ResolvedCell]":
    """The cells *contract* is parametrised over, before the draw.

    Unmarked: *space* itself, entry-less. Marked: :func:`project` of *space*,
    each crossed with the declared entries the cell admits, entry-major
    within a cell so the order is a pure function of *space*.
    """
    declared = entries_declared(contract)
    if not declared:
        return list(space)
    return [
        with_entry(resolved, entry)
        for resolved in project(space)
        for entry in declared
        if entry in entries_of(resolved)
    ]


def opened(resolved: ResolvedCell) -> "AbstractAsyncContextManager[BaseHost]":
    """Open *resolved*'s host the way its entry needs: hooked for ``exec-shell``."""
    if resolved.entry == EXEC_SHELL:
        if resolved.open_shell_host is None:
            raise RuntimeError(
                f"{cell_label(resolved)} is reached by exec-shell but its venue "
                f"set no open_shell_host"
            )
        return resolved.open_shell_host()
    return resolved.open_host()


async def issue(host: BaseHost, resolved: ResolvedCell, cmd: str, **kwargs: Any) -> CommandResult:
    """Issue *cmd* through *resolved*'s entry, then assert the route it took."""
    if resolved.entry in {EXEC, EXEC_SHELL}:
        result = await host.exec(cmd, **kwargs)
    else:
        result = (await host.run(cmd, **kwargs)).only
    check_route(resolved, result)
    return result


def check_route(resolved: ResolvedCell, result: CommandResult) -> None:
    """Assert *result* ran on the route *resolved*'s entry must take."""
    expected = expected_route(resolved)
    assert result.route == expected, (
        f"{cell_label(resolved)}: the command ran on route {result.route!r}, expected {expected!r}"
    )


@contextlib.contextmanager
def noop_session_setup() -> Iterator[str]:
    """Register a no-op hook for the life of one hooked host; yield its name.

    :func:`tests._fixtures.session_setup_hooks.noop_session_setup`, under a
    name of this context's own, so two overlapping hooked hosts never share
    (and never remove) one registry entry.
    """
    with _temporary_noop_hook(NOOP_SETUP_HOOK) as name:
        yield name


def hooked(
    build: "Callable[[str], AbstractAsyncContextManager[BaseHost]]",
) -> "Callable[[], AbstractAsyncContextManager[BaseHost]]":
    """Wrap an opener factory *build(session_setup)* into a hooked opener.

    *build* takes the hook name and returns an async context manager over the
    host built with that ``session_setup``; the hook is registered before
    the host is built (construction checks the registry) and unregistered
    after it closes.
    """

    @contextlib.asynccontextmanager
    async def opener() -> AsyncIterator[BaseHost]:
        with noop_session_setup() as hook:
            async with build(hook) as host:
                yield host

    return opener


def family_of(host: BaseHost) -> str:
    """The family *host* was BUILT as, from its class: the venues' one reading of it."""
    if isinstance(host, LocalHost):
        return LOCAL_FAMILY
    if isinstance(host, EmbeddedHost):
        return EMBEDDED
    return UNIX


def proxied_on(entry: "dict[str, Any]", term: str, *, family: str) -> bool:
    """Whether *entry*'s creds make a login over *term* a proxied one.

    Read off the LAB ENTRY itself, never through otto's own cred resolver,
    so the route a venue expects is independent of the code that picks the
    route: the login over *term* is the first cred that applies to it (no
    ``protocols``, or ``protocols`` naming *term*;
    docs/configuration/host-sources.md, "Scoping a cred to a protocol"), and
    it is proxied when that cred names a ``proxy``.

    A local or embedded host cannot proxy a login, so it answers False
    without reading anything.

    Raises:
        LookupError: *entry* is a unix host's and declares no ``creds``, so
            the fact this reads is missing rather than False.
        ValueError: *family* is none this suite builds.
    """
    if family in (LOCAL_FAMILY, EMBEDDED):
        return False
    if family != UNIX:
        raise ValueError(f"unknown family {family!r}: cannot tell whether its login is proxied")
    creds = entry.get("creds")
    if creds is None:
        raise LookupError(f"lab entry {entry.get('name', entry)!r} declares no creds")
    pick = next((cred for cred in creds if term in cred.get("protocols", [term])), None)
    return pick is not None and "proxy" in pick
