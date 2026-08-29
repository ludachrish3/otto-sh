"""Fleet host access and host-dispatch helpers for the active lab."""

import dataclasses
import logging
import re
from collections.abc import Awaitable, Callable, Generator
from typing import (
    TYPE_CHECKING,
    Any,
    TypeVar,
    cast,
)

from ..host.host import DEFAULT_COMMAND_TIMEOUT
from .lab import Lab

if TYPE_CHECKING:
    from ..host import Results, UnixHost
    from ..host.options import (
        FtpOptions,
        NcOptions,
        ScpOptions,
        SftpOptions,
        SshOptions,
        TelnetOptions,
        UserlandOptions,
    )
    from ..host.remote_host import RemoteHost

T = TypeVar("T")

logger = logging.getLogger(__name__)


def _apply_option_overrides(
    host: "RemoteHost",
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
) -> "RemoteHost":
    """Return a copy of *host* with the given ``*_options`` fields replaced.

    Each non-``None`` argument **replaces** the corresponding field on the
    returned copy wholesale; the caller is responsible for constructing
    the full options instance they want.

    The copy is built via :func:`dataclasses.replace`, which re-runs
    ``__post_init__`` and therefore constructs a *fresh*
    :class:`ConnectionManager` with the override options wired in from
    the start. This is required because protocol options shape the
    connection itself (key algorithms, hop wiring, etc.) and cannot be
    swapped on an already-open connection. The original *host* and any
    connection it owns are untouched; the override copy will open its
    own connection on first use.

    Override keys that don't correspond to a field on *host* are silently
    dropped — e.g. ``ssh_options`` is ignored for an :class:`EmbeddedHost`,
    which only carries ``telnet_options``. This lets fleet callers pass
    SSH-shaped overrides without erroring on embedded hosts that simply
    don't speak SSH.

    When no applicable overrides are supplied, the original *host* is
    returned unchanged so identity (``host is host``) is preserved for
    non-override callers.

    ``term`` / ``transfer`` switch the host's *active* protocol. Each is
    validated against the host's menu (``valid_terms`` / ``valid_transfers``)
    by the copy's ``__post_init__`` — a value outside the menu raises a
    fail-loud ``ValueError`` naming the menu — and the copy's connection /
    file-transfer backend is rebuilt for the chosen protocol via the registry
    ``create()`` seam. Switching to a value not in the menu is rejected; to
    select a custom backend it must be listed in the host's menu. This is the
    only supported way to change a host's active protocol.
    """
    candidates: dict[str, Any] = {
        k: v
        for k, v in (
            ("term", term),
            ("transfer", transfer),
            ("ssh_options", ssh_options),
            ("telnet_options", telnet_options),
            ("sftp_options", sftp_options),
            ("scp_options", scp_options),
            ("ftp_options", ftp_options),
            ("nc_options", nc_options),
            ("userland_options", userland_options),
        )
        if v is not None
    }
    if not candidates:
        return host
    # RemoteHost subclasses (UnixHost, EmbeddedHost) are all dataclasses,
    # but RemoteHost itself isn't decorated — cast around the type checker.
    host_any = cast("Any", host)
    host_fields = {f.name for f in dataclasses.fields(host_any)}
    overrides = {k: v for k, v in candidates.items() if k in host_fields}
    if not overrides:
        return host
    return cast("RemoteHost", dataclasses.replace(host_any, **overrides))


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
    ``[project]`` universes the run's repos declared (spec §6), re-derived
    live per walk. When no repo declares ``[project]`` it *is* the whole
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
            ``_apply_option_overrides``.
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
        ``.*`` is what makes this a prefix (see :doc:`/library/index` for a
        runnable, in-memory example)::

            import re

            matched = list(all_hosts(re.compile(r"test2.*")))
    """
    from ..context import get_context

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
            ``_apply_option_overrides``.
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
                pattern=re.compile(r"router"),
            )
    """
    from ..context import get_context

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
            ``_apply_option_overrides``.
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
    from ..context import get_context

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
            ``_apply_option_overrides``.
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
    from ..context import get_context

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


def get_lab() -> Lab:
    """Return the active lab from the current OttoContext."""
    from ..context import get_context

    return get_context().lab


def get_hosts_in_play() -> set[str]:
    """Return this run's hosts in play — the RESERVATION readers' entry; walks must not use it.

    Named for the contract rather than for the mechanism, and deliberately NOT
    re-exported from :mod:`otto.config`: it bakes in ``require_nonempty=False``,
    so ``for hid in get_hosts_in_play(): get_host(hid)`` is a walk that does
    nothing at all on an empty declared fleet — the exact silent emptiness
    :func:`~otto.config.scope.require_nonempty_fleet` exists to make loud.
    A fleet WALK calls :meth:`otto.context.OttoContext.admissible_ids`
    directly, with the refusal on.

    Here rather than at the call site because not every reader may import
    ``otto.context``: ``tach.toml`` confines ``otto.reservations`` to a short
    list that does not include it, and the reservation gate needs exactly this
    set to scope its requirement (spec 2026-08-28 three-level-reservations §5).
    Lazy for the same reason :func:`get_lab` is — context imports config back.

    An empty declared fleet reads as ZERO hosts in play — the requirement is
    the lab-level set alone — and never as an abort; a run that goes on to walk
    its fleet still refuses that fleet with the same message.

    The built-in ``local`` host is never in play: otto can always run on the
    machine it is running on, so a reservation standing between a user and
    ``otto host local <verb>`` costs them a run and buys nobody a slot. The
    subtraction is here rather than in :func:`~otto.config.scope.scoped_ids`
    because scoping answers a different question — ``include_local=True`` is a
    fleet WALK's own opt-in and keeps working untouched. ``is_builtin_host``
    rather than an ``id != "local"`` test: a lab may declare its OWN ``local``
    entry (``load_lab`` then injects nothing), and that host's ``resources``
    are as real as any other's.
    """
    from ..context import get_context
    from ..host.builtin_hosts import is_builtin_host

    ctx = get_context()
    in_play = ctx.admissible_ids(require_nonempty=False)
    return {hid for hid in in_play if not is_builtin_host(ctx.lab.hosts.get(hid))}
