import dataclasses
import re
from typing import (
    TYPE_CHECKING,
    Any,
    Awaitable,
    Callable,
    Generator,
    TypeVar,
    cast,
)

from ..logger import getOttoLogger
from .lab import Lab

if TYPE_CHECKING:
    from ..host import RunResult, UnixHost
    from ..host.options import (
        FtpOptions,
        NcOptions,
        ScpOptions,
        SftpOptions,
        SshOptions,
        TelnetOptions,
    )
    from ..host.remoteHost import RemoteHost

T = TypeVar("T")

logger = getOttoLogger()



def _apply_option_overrides(
    host: 'RemoteHost',
    *,
    ssh_options: 'SshOptions | None' = None,
    telnet_options: 'TelnetOptions | None' = None,
    sftp_options: 'SftpOptions | None' = None,
    scp_options: 'ScpOptions | None' = None,
    ftp_options: 'FtpOptions | None' = None,
    nc_options: 'NcOptions | None' = None,
) -> 'RemoteHost':
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
    """
    candidates: dict[str, Any] = {
        k: v
        for k, v in (
            ('ssh_options', ssh_options),
            ('telnet_options', telnet_options),
            ('sftp_options', sftp_options),
            ('scp_options', scp_options),
            ('ftp_options', ftp_options),
            ('nc_options', nc_options),
        )
        if v is not None
    }
    if not candidates:
        return host
    # RemoteHost subclasses (UnixHost, EmbeddedHost) are all dataclasses,
    # but RemoteHost itself isn't decorated — cast around the type checker.
    host_any = cast(Any, host)
    host_fields = {f.name for f in dataclasses.fields(host_any)}
    overrides = {k: v for k, v in candidates.items() if k in host_fields}
    if not overrides:
        return host
    return cast('RemoteHost', dataclasses.replace(host_any, **overrides))


def all_hosts(
    pattern: re.Pattern[str] | None = None,
    *,
    include_containers: bool = False,
    ssh_options: 'SshOptions | None' = None,
    telnet_options: 'TelnetOptions | None' = None,
    sftp_options: 'SftpOptions | None' = None,
    scp_options: 'ScpOptions | None' = None,
    ftp_options: 'FtpOptions | None' = None,
    nc_options: 'NcOptions | None' = None,
) -> Generator['RemoteHost', Any, Any]:
    """Yield the active lab's real remote hosts, optionally filtered by regex.

    This is the *fleet* generator: it yields every network-reached
    :class:`RemoteHost` in the active lab — both :class:`UnixHost`
    (SSH/telnet to a shell) and :class:`EmbeddedHost` (telnet to an RTOS
    console). :class:`DockerContainerHost` entries are skipped by default
    because containers aren't operated on as part of the host fleet
    (e.g. ``otto monitor``, coverage collection); containers remain
    reachable for targeted use via tab completion and ``get_host``.
    Pass ``include_containers=True`` to yield container hosts as well.

    Args:
        pattern: Compiled regex matched against each host's ``id`` via
            ``pattern.search()``.  When *None* (the default), all hosts
            are yielded.
        include_containers: When ``True``, also yield
            :class:`DockerContainerHost` entries. Defaults to ``False``.
        ssh_options, telnet_options, sftp_options, scp_options,
        ftp_options, nc_options: Optional per-call option overrides. When
            supplied, each yielded host is a fresh
            :func:`dataclasses.replace`-style copy whose corresponding
            ``*_options`` field is replaced by the caller's instance
            (wholesale replacement, not per-key merge). The new host has
            a fresh :class:`ConnectionManager` constructed with the
            override options, so the override values shape whichever
            connection opens first. Stored hosts in ``lab.hosts`` are
            untouched. Override keys that don't correspond to a field on
            a given host are silently dropped — e.g. ``ssh_options`` is
            ignored for an :class:`EmbeddedHost`, which only carries
            ``telnet_options``. When no applicable overrides remain, the
            stored instance is yielded as-is so identity is preserved.
            Hop resolution is internal and is *not* affected by overrides.

    Yields:
        RemoteHost: Each matching :class:`UnixHost` or
        :class:`EmbeddedHost` from the lab configuration.

    Examples:
        >>> import re
        >>> # assuming hosts: carrot_seed, tomato_seed, pepper_seed
        >>> seeds = list(all_hosts(re.compile(r"tomato")))  # doctest: +SKIP
    """
    from ..context import get_context
    yield from get_context().all_hosts(
        pattern,
        include_containers=include_containers,
        ssh_options=ssh_options,
        telnet_options=telnet_options,
        sftp_options=sftp_options,
        scp_options=scp_options,
        ftp_options=ftp_options,
        nc_options=nc_options,
    )

async def do_for_all_hosts(
    method: Callable[..., Awaitable[T]],
    *args: Any,
    pattern: re.Pattern[str] | None = None,
    concurrent: bool = True,
    include_containers: bool = False,
    ssh_options: 'SshOptions | None' = None,
    telnet_options: 'TelnetOptions | None' = None,
    sftp_options: 'SftpOptions | None' = None,
    scp_options: 'ScpOptions | None' = None,
    ftp_options: 'FtpOptions | None' = None,
    nc_options: 'NcOptions | None' = None,
    **kwargs: Any,
) -> dict[str, T | BaseException]:
    """Call an async host method on every matching host.

    Args:
        method: Unbound async method (e.g. ``UnixHost.oneshot``).
        *args: Positional arguments forwarded to *method* after the host.
        pattern: Compiled regex filter passed to :func:`all_hosts`.
        concurrent: When ``True`` (default), run all calls via
            ``asyncio.gather`` with ``return_exceptions=True``.
            When ``False``, execute serially.
        include_containers: Forwarded to :func:`all_hosts`. When
            ``False`` (default), container hosts are excluded.
        ssh_options, telnet_options, sftp_options, scp_options,
        ftp_options, nc_options: Optional per-call option overrides
            forwarded to :func:`all_hosts`. See its docstring for
            semantics.
        **kwargs: Keyword arguments forwarded to *method*.

    Returns:
        A dict keyed by host ID.  Values are the return of *method*,
        or a :class:`BaseException` if that host's call failed.

    Examples:
        >>> import re
        >>> from otto.host import UnixHost
        >>> results = await do_for_all_hosts(  # doctest: +SKIP
        ...     UnixHost.oneshot, "uname -a",
        ...     pattern=re.compile(r"router"),
        ... )
    """
    from ..context import get_context
    return await get_context().do_for_all_hosts(
        method,
        *args,
        pattern=pattern,
        concurrent=concurrent,
        include_containers=include_containers,
        ssh_options=ssh_options,
        telnet_options=telnet_options,
        sftp_options=sftp_options,
        scp_options=scp_options,
        ftp_options=ftp_options,
        nc_options=nc_options,
        **kwargs,
    )


async def run_on_all_hosts(
    cmds: list[str] | str,
    pattern: re.Pattern[str] | None = None,
    concurrent: bool = True,
    timeout: float | None = None,
    *,
    include_containers: bool = False,
    ssh_options: 'SshOptions | None' = None,
    telnet_options: 'TelnetOptions | None' = None,
    sftp_options: 'SftpOptions | None' = None,
    scp_options: 'ScpOptions | None' = None,
    ftp_options: 'FtpOptions | None' = None,
    nc_options: 'NcOptions | None' = None,
) -> 'dict[str, RunResult | BaseException]':
    """Run commands on every matching host via :meth:`UnixHost.run`.

    Convenience wrapper around :func:`do_for_all_hosts` for the most
    common use case.

    Args:
        cmds: Command string or list of command strings.
        pattern: Compiled regex filter passed to :func:`all_hosts`.
        concurrent: When ``True`` (default), run all calls via
            ``asyncio.gather``.  When ``False``, execute serially.
        timeout: Per-host timeout forwarded to ``run``.
        include_containers: Forwarded to :func:`do_for_all_hosts`. When
            ``False`` (default), container hosts are excluded.
        ssh_options, telnet_options, sftp_options, scp_options,
        ftp_options, nc_options: Optional per-call option overrides
            forwarded to :func:`do_for_all_hosts`.

    Returns:
        A dict keyed by host ID.  Values are :class:`RunResult` instances,
        or a :class:`BaseException` if that host's call failed.

    Examples:
        >>> results = await run_on_all_hosts("uname -a")  # doctest: +SKIP
    """
    from ..context import get_context
    return await get_context().run_on_all_hosts(
        cmds,
        pattern=pattern,
        concurrent=concurrent,
        timeout=timeout,
        include_containers=include_containers,
        ssh_options=ssh_options,
        telnet_options=telnet_options,
        sftp_options=sftp_options,
        scp_options=scp_options,
        ftp_options=ftp_options,
        nc_options=nc_options,
    )


def get_host(
    host_id: str,
    *,
    ssh_options: 'SshOptions | None' = None,
    telnet_options: 'TelnetOptions | None' = None,
    sftp_options: 'SftpOptions | None' = None,
    scp_options: 'ScpOptions | None' = None,
    ftp_options: 'FtpOptions | None' = None,
    nc_options: 'NcOptions | None' = None,
) -> 'UnixHost':
    """Return the host registered under *host_id* in the active lab.

    Args:
        host_id: Unique host id (as produced by ``UnixHost.id``).
        ssh_options, telnet_options, sftp_options, scp_options,
        ftp_options, nc_options: Optional per-call option overrides.
            Each non-``None`` argument **replaces** the corresponding
            ``*_options`` field on a returned copy wholesale; the copy is
            built via :func:`dataclasses.replace` so the new host's
            :class:`ConnectionManager` is constructed with the override
            options from the start. The stored host (and any connection
            it owns) is untouched. With no overrides, the stored
            instance is returned unchanged so
            ``get_host('x') is get_host('x')`` still holds. Hop
            resolution is internal and is *not* affected by overrides.
    """

    from ..context import get_context
    return get_context().get_host(
        host_id,
        ssh_options=ssh_options,
        telnet_options=telnet_options,
        sftp_options=sftp_options,
        scp_options=scp_options,
        ftp_options=ftp_options,
        nc_options=nc_options,
    )


def get_lab() -> Lab:
    """Return the active lab from the current OttoContext."""
    from ..context import get_context
    return get_context().lab
