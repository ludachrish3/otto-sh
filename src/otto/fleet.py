"""Fleet host access and host-dispatch helpers for the active lab."""

import logging
import re
from collections.abc import Awaitable, Callable, Generator
from typing import (
    TYPE_CHECKING,
    Any,
    TypeVar,
)

from .host.host import DEFAULT_COMMAND_TIMEOUT

if TYPE_CHECKING:
    from .config.project import Project
    from .host.options import (
        FtpOptions,
        NcOptions,
        ScpOptions,
        SftpOptions,
        SshOptions,
        TelnetOptions,
        UserlandOptions,
    )
    from .host.remote_host import RemoteHost
    from .host.unix_host import UnixHost
    from .labs.loader import Lab
    from .result import Results

T = TypeVar("T")

logger = logging.getLogger(__name__)


def all_hosts(  # noqa: PLR0913 — wide host-dispatch API (mirrors do_for_all_hosts)
    pattern: re.Pattern[str] | None = None,
    *,
    include_containers: bool = False,
    include_local: bool = False,
    term: str | None = None,
    transfer: str | None = None,
    ssh_options: "SshOptions | None" = None,
    telnet_options: "TelnetOptions | None" = None,
    sftp_options: "SftpOptions | None" = None,
    scp_options: "ScpOptions | None" = None,
    ftp_options: "FtpOptions | None" = None,
    nc_options: "NcOptions | None" = None,
    userland_options: "UserlandOptions | None" = None,
) -> Generator["RemoteHost", Any, Any]:
    """Yield this run's fleet of interest, optionally narrowed by regex.

    This is the *fleet* generator: it yields every network-reached
    :class:`~otto.host.remote_host.RemoteHost` the active project universe
    admits — both :class:`~otto.host.unix_host.UnixHost`
    (SSH/telnet to a shell) and :class:`~otto.host.embedded_host.EmbeddedHost` (telnet to an RTOS
    console). :class:`~otto.host.docker_host.DockerContainerHost` entries are skipped by default
    because containers aren't operated on as part of the host fleet
    (e.g. ``otto monitor``, coverage collection); containers remain
    reachable for targeted use via tab completion and ``get_host``.
    Pass ``include_containers=True`` to yield container hosts as well.

    The base set is NOT the whole loaded lab: it is the union of the
    ``[project]`` universes the run's projects declared (spec §6), re-derived
    live per walk. When no project declares ``[project]`` it *is* the whole
    loaded lab, so product-less projects see no change. Everything here
    delegates to :meth:`otto.context.OttoContext.all_hosts`, which owns the
    rules and their two loud failures — see it for the empty-fleet and
    empty-selection errors.

    The built-in ``local`` host (the machine otto itself runs on, injected
    into every lab for targeted ``otto host local`` use) is likewise NOT part
    of the fleet: a deploy/monitor/coverage sweep must never silently operate
    on the runner. It stays reachable via ``get_host("local")``; pass
    ``include_local=True`` to opt it into fleet iteration.

    Args:
        pattern: Compiled regex matched against each host's ``id`` via
            ``pattern.fullmatch()`` — never ``search`` (D6), so ``sensor``
            does not select ``sensor-1``; write ``sensor.*``. A pattern that
            fullmatches none of the base set raises
            :class:`~otto.config.scope.EmptySelectionError` instead of
            yielding nothing, as does one whose every match
            ``include_containers``/``include_local`` then holds back — same
            class, and it names the flag instead of the regex. When *None*
            (the default), the whole base set is yielded.
        include_containers: When ``True``, also yield
            :class:`~otto.host.docker_host.DockerContainerHost` entries. Defaults to ``False``.
        term, transfer: optional active-protocol override; see
            ``otto.run_context._apply_option_overrides``.
        ssh_options, telnet_options, sftp_options, scp_options,
        ftp_options, nc_options, userland_options: Optional per-call overrides. When
            supplied, each yielded host is a fresh
            :func:`dataclasses.replace`-style copy whose corresponding
            ``*_options`` field is replaced by the caller's instance
            (wholesale replacement, not per-key merge). The new host has
            a fresh :class:`~otto.host.connections.ConnectionManager` constructed with the
            override options, so the override values shape whichever
            connection opens first. Stored hosts in ``lab.hosts`` are
            untouched. Override keys that don't correspond to a field on
            a given host are silently dropped — e.g. ``ssh_options`` is
            ignored for an :class:`~otto.host.embedded_host.EmbeddedHost`, which only carries
            ``telnet_options``. When no applicable overrides remain, the
            stored instance is yielded as-is so identity is preserved.
            Hop resolution is internal and is *not* affected by overrides.

    Yields:
        RemoteHost: Each matching :class:`~otto.host.unix_host.UnixHost` or
        :class:`~otto.host.embedded_host.EmbeddedHost` from the lab configuration.

    Examples:
        Narrow the fleet by id pattern — a FULL match, so the trailing
        ``.*`` is what makes this a prefix (see :doc:`/extend/python-library` for a
        runnable, in-memory example)::

            import re

            matched = list(all_hosts(re.compile(r"test2.*")))
    """
    from .run_context import get_context

    yield from get_context().all_hosts(
        pattern,
        include_containers=include_containers,
        include_local=include_local,
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


async def do_for_all_hosts(  # noqa: PLR0913 — wide host-dispatch API
    method: Callable[..., Awaitable[T]],
    *args: Any,
    pattern: re.Pattern[str] | None = None,
    concurrent: bool = True,
    include_containers: bool = False,
    include_local: bool = False,
    term: str | None = None,
    transfer: str | None = None,
    ssh_options: "SshOptions | None" = None,
    telnet_options: "TelnetOptions | None" = None,
    sftp_options: "SftpOptions | None" = None,
    scp_options: "ScpOptions | None" = None,
    ftp_options: "FtpOptions | None" = None,
    nc_options: "NcOptions | None" = None,
    userland_options: "UserlandOptions | None" = None,
    **kwargs: Any,
) -> dict[str, T | BaseException]:
    """Call an async host method on every matching host.

    Args:
        method: Unbound async method (e.g. ``UnixHost.exec``).
        *args: Positional arguments forwarded to *method* after the host.
        pattern: Compiled regex filter passed to :func:`all_hosts`.
        concurrent: When ``True`` (default), run all calls via
            ``asyncio.gather`` with ``return_exceptions=True``.
            When ``False``, execute serially.
        include_containers: Forwarded to :func:`all_hosts`. When
            ``False`` (default), container hosts are excluded.
        include_local: Forwarded to :func:`all_hosts`. When ``False``
            (default), the built-in ``local`` runner host is excluded.
        term, transfer: optional active-protocol override; see
            ``otto.run_context._apply_option_overrides``.
        ssh_options, telnet_options, sftp_options, scp_options,
        ftp_options, nc_options, userland_options: Optional per-call overrides
            forwarded to :func:`all_hosts`. See its docstring for
            semantics.
        **kwargs: Keyword arguments forwarded to *method*.

    Returns:
        A dict keyed by host ID.  Values are the return of *method*,
        or a :class:`BaseException` if that host's call failed.

    Examples:
        Call an unbound async method on every matching host::

            import re
            from otto.host import UnixHost

            results = await do_for_all_hosts(
                UnixHost.exec,
                "uname -a",
                pattern=re.compile(r"router.*"),
            )
    """
    from .run_context import get_context

    return await get_context().do_for_all_hosts(
        method,
        *args,
        pattern=pattern,
        concurrent=concurrent,
        include_containers=include_containers,
        include_local=include_local,
        term=term,
        transfer=transfer,
        ssh_options=ssh_options,
        telnet_options=telnet_options,
        sftp_options=sftp_options,
        scp_options=scp_options,
        ftp_options=ftp_options,
        nc_options=nc_options,
        userland_options=userland_options,
        **kwargs,
    )


async def run_on_all_hosts(  # noqa: PLR0913 — wide host-dispatch API
    cmds: list[str] | str,
    pattern: re.Pattern[str] | None = None,
    concurrent: bool = True,
    timeout: float = DEFAULT_COMMAND_TIMEOUT,
    *,
    include_containers: bool = False,
    term: str | None = None,
    transfer: str | None = None,
    ssh_options: "SshOptions | None" = None,
    telnet_options: "TelnetOptions | None" = None,
    sftp_options: "SftpOptions | None" = None,
    scp_options: "ScpOptions | None" = None,
    ftp_options: "FtpOptions | None" = None,
    nc_options: "NcOptions | None" = None,
    userland_options: "UserlandOptions | None" = None,
) -> "dict[str, Results | BaseException]":
    """Run commands on every matching host via :meth:`~otto.host.host.BaseHost.run`.

    Convenience wrapper around :func:`do_for_all_hosts` for the most
    common use case.

    Args:
        cmds: Command string or list of command strings.
        pattern: Compiled regex filter passed to :func:`all_hosts`.
        concurrent: When ``True`` (default), run all calls via
            ``asyncio.gather``.  When ``False``, execute serially.
        timeout: Per-host timeout forwarded to ``run``. Defaults to
            :data:`~otto.host.host.DEFAULT_COMMAND_TIMEOUT`.
        include_containers: Forwarded to :func:`do_for_all_hosts`. When
            ``False`` (default), container hosts are excluded.
        term, transfer: optional active-protocol override; see
            ``otto.run_context._apply_option_overrides``.
        ssh_options, telnet_options, sftp_options, scp_options,
        ftp_options, nc_options, userland_options: Optional per-call overrides
            forwarded to :func:`do_for_all_hosts`.

    Returns:
        A dict keyed by host ID.  Values are :class:`~otto.result.Results` instances,
        or a :class:`BaseException` if that host's call failed.

    Examples:
        Run a command on every matching host::

            results = await run_on_all_hosts("uname -a")
    """
    from .run_context import get_context

    return await get_context().run_on_all_hosts(
        cmds,
        pattern=pattern,
        concurrent=concurrent,
        timeout=timeout,
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


def get_host(
    host_id: str,
    *,
    term: str | None = None,
    transfer: str | None = None,
    ssh_options: "SshOptions | None" = None,
    telnet_options: "TelnetOptions | None" = None,
    sftp_options: "SftpOptions | None" = None,
    scp_options: "ScpOptions | None" = None,
    ftp_options: "FtpOptions | None" = None,
    nc_options: "NcOptions | None" = None,
    userland_options: "UserlandOptions | None" = None,
) -> "UnixHost":
    """Return the host registered under *host_id* in the active lab.

    Args:
        host_id: Unique host id (as produced by ``UnixHost.id``).
        term, transfer: optional active-protocol override; see
            ``otto.run_context._apply_option_overrides``.
        ssh_options, telnet_options, sftp_options, scp_options,
        ftp_options, nc_options, userland_options: Optional per-call overrides.
            Each non-``None`` argument **replaces** the corresponding
            ``*_options`` field on a returned copy wholesale; the copy is
            built via :func:`dataclasses.replace` so the new host's
            :class:`~otto.host.connections.ConnectionManager` is constructed with the override
            options from the start. The stored host (and any connection
            it owns) is untouched. With no overrides, the stored
            instance is returned unchanged so
            ``get_host('x') is get_host('x')`` still holds. Hop
            resolution is internal and is *not* affected by overrides.
    """
    from .run_context import get_context

    return get_context().get_host(
        host_id,
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


def get_lab() -> "Lab":
    """Return the active lab from the current OttoContext."""
    from .run_context import get_context

    return get_context().lab


def get_hosts_in_play() -> set[str]:
    """Return this run's hosts in play — the RESERVATION readers' entry; walks must not use it.

    Named for the contract rather than for the mechanism, and deliberately NOT
    re-exported from :mod:`otto.lab` or :mod:`otto`: it bakes in ``require_nonempty=False``,
    so ``for hid in get_hosts_in_play(): get_host(hid)`` is a walk that does
    nothing at all on an empty declared fleet — the exact silent emptiness
    :func:`~otto.config.scope.require_nonempty_fleet` exists to make loud.
    A fleet WALK calls :meth:`otto.context.OttoContext.admissible_ids`
    directly, with the refusal on.

    The active context's answer to the reservation gate's own question
    (``otto.reservations.check._hosts_in_play``, spec 2026-08-28
    three-level-reservations §5), so ``otto reservation check``, the
    completer's gate and the gate itself cannot disagree about a host. An
    empty declared fleet reads as ZERO hosts in play, never as an abort, and
    the built-in ``local`` host is never in play.
    """
    from .reservations.check import _hosts_in_play
    from .run_context import get_context

    return _hosts_in_play(get_context())


_UNKNOWN_QUERY_OWNER = """\
fleet_of_interest() was bound to project '{owner}', but none of the projects
it was given is named '{owner}'.

    projects given: {known}

Answering with the whole lab would be the silent widening project scoping
exists to prevent. Pass the project's name exactly as the project declares it
(owner is not normalized), or owner=None for the union of the declaring
projects."""
"""The query's unknown-owner refusal. Not the walk's message: the walk's speaks
of "this run's resolved projects" and of a fallback, and a query has neither. It
names what was passed, so a caller can tell a typo from a project it forgot."""


def fleet_of_interest(
    lab: "Lab",
    projects: "list[Project]",
    *,
    owner: str | None = None,
    exclude_projects: list[str] | None = None,
    include_containers: bool = False,
    include_local: bool = False,
) -> list[str]:
    """Return the ids of the hosts a fleet walk would reach, without walking or connecting.

    The question a reader asks before a run: which of this lab's hosts are the
    project's fleet of interest. The answer is the ids
    :meth:`otto.context.OttoContext.all_hosts` yields with the same flags, for
    the same lab, projects and ``-E`` set, in the lab's order; with *owner* set,
    the ids ``ctx.for_project(owner).all_hosts()`` yields. It is the walk's own
    computation, not a copy of it: the admissible set comes from
    :func:`otto.config.scope.scoped_ids`, which the walk's base set
    (:meth:`otto.context.OttoContext.admissible_ids`) reads too, and the
    membership flags from the one rule the walk applies.

    Three differences are deliberate:

    * **An empty declared fleet** is an answer, ``[]``. The walk refuses it,
      because a walk that touches nothing is a silent failure; a query that
      returns an empty list is not silent.
    * **An unknown owner** is refused even when *projects* is empty. A context
      that could not reach its projects falls back to the whole lab; a caller
      that passed *projects* has reached them, so the whole lab would widen.
    * **The library sentinel lab** (``otto.run_context.LIBRARY_LAB_NAME``) gets no
      special case. A context on it walks its whole lab; this function scopes
      the lab it is given.

    Pure: no I/O, no ``bootstrap()``, no lab loading, no read of the active
    context. The caller passes the projects: usually ``get_projects()``, or a
    hand-built ``[Project(root=...)]``. When project names are unique their order
    does not matter; when two projects share a name the last one wins, as in the
    walk. There is no ``pattern`` parameter: narrowing a list is the caller's
    job.

    Args:
        lab: The loaded lab. Its ``hosts`` are read as they stand at the call.
        projects: The projects whose ``[project]`` declarations scope the lab.
        owner: A ``Project.name`` exactly as declared (it is never normalized),
            for that one project's fleet. ``None`` (the default) answers the
            union of the declaring projects, or the whole lab when none declares.
        exclude_projects: Project names switched off for the union, compared
            PEP 503-normalized, as ``--exclude-projects`` is. Ignored when
            *owner* is set, as in the walk. A name that matches no project is
            ignored: validating it is the CLI's job.
        include_containers: Also answer the container hosts the scope admits.
        include_local: Also answer the built-in ``local`` host when the scope
            admits it. With no declaration it always does; under one it
            depends on ``local``'s stamped ``source_lab``. The flag never adds
            a host the scope excluded.

    Returns:
        The host ids, in the lab's order. Possibly empty.

    Raises:
        otto.bootstrap.ProjectScopeError: *owner* is not ``None`` and names no
            project in *projects*, including when *projects* is empty.

    >>> import re
    >>> import types
    >>> from otto.fleet import fleet_of_interest
    >>> from otto.labs.loader import Lab
    >>> from otto.config.scope import ProjectScopeConfig
    >>> hosts = {
    ...     "sensor-1": types.SimpleNamespace(source_lab="bench"),
    ...     "gw-1": types.SimpleNamespace(source_lab="bench"),
    ... }
    >>> lab = Lab(name="bench", hosts=hosts)
    >>> sensors = types.SimpleNamespace(  # only .name/.project_scope/.root are read
    ...     name="sensors",
    ...     root="/projects/sensors",
    ...     project_scope=ProjectScopeConfig([re.compile("bench")], [re.compile("sensor-.*")]),
    ... )
    >>> fleet_of_interest(lab, [sensors])
    ['sensor-1']
    >>> fleet_of_interest(lab, [])  # nothing declared: the whole lab
    ['sensor-1', 'gw-1']
    """
    # function-scope: keeps this module import-light
    from .config.scope import ProjectScopeError, resolve_scopes, scoped_ids
    from .host.builtin_hosts import BUILTIN_LOCAL_HOST_ID, _flag_holding_out

    # The verdicts the walk's context resolves (``scopes_of(ctx)``), with
    # the same ``local`` exclusion, so the two read identical data.
    scopes = resolve_scopes(
        projects,
        lab.component_names,
        lab.hosts,
        exclude_ids=frozenset({BUILTIN_LOCAL_HOST_ID}),
    )
    if owner is not None and owner not in scopes:
        # Checked here, before ``scoped_ids``: its own refusal stands down over
        # empty scopes, a leniency for a context that could not reach its
        # projects. This caller handed them over.
        raise ProjectScopeError(
            "",  # no project, so no settings.toml to send the reader to
            _UNKNOWN_QUERY_OWNER.format(owner=owner, known=", ".join(sorted(scopes)) or "(none)"),
        )
    admissible = scoped_ids(
        lab.hosts, scopes, owner, exclude_projects=tuple(exclude_projects or ())
    )
    return [
        host_id
        for host_id, host in lab.hosts.items()
        if host_id in admissible
        and _flag_holding_out(
            host, include_containers=include_containers, include_local=include_local, owner=owner
        )
        is None
    ]
