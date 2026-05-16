import asyncio
import dataclasses
import re
from dataclasses import dataclass, field
from typing import (
    TYPE_CHECKING,
    Any,
    Awaitable,
    Callable,
    Generator,
    Optional,
    TypeVar,
)

from ..logger import getOttoLogger
from ..utils import CommandStatus, Status
from .lab import Lab
from .repo import Repo

if TYPE_CHECKING:
    from ..host import RemoteHost, RunResult
    from ..host.options import (
        FtpOptions,
        NcOptions,
        ScpOptions,
        SftpOptions,
        SshOptions,
        TelnetOptions,
    )
    from ..reservations import ReservationBackend, ResolvedIdentity

T = TypeVar("T")

logger = getOttoLogger()


# TODO: use the dataclass.asdict function can turn dataclasses into dicts of just their defined values
@dataclass(
    frozen=True,
)
class ConfigModule():
    repos: list[Repo]
    """Repos under test."""

    lab: Lab
    """All lab information, like hosts, version information, etc."""

    reservation_backend: Optional['ReservationBackend'] = None
    """Configured reservation backend, or ``None`` if the repo has no
    ``[reservations]`` settings wired up yet."""

    identity: Optional['ResolvedIdentity'] = field(default=None)
    """Effective reservation identity for this invocation (set by the top-level
    CLI callback after parsing ``--as-user``)."""

    skip_reservation_check: bool = False
    """``True`` when ``-R``/``--skip-reservation-check`` is on the command line."""

    def logRepoCommits(self):

        for repo in self.repos:
            logger.debug(f"{repo.sutDir}: {repo.commit}")


@dataclass(
    init=False,
)
class ConfigModuleManager():

    _configModule: ConfigModule

    @property
    def configModule(self) -> ConfigModule:
        return self._configModule

    @configModule.setter
    def configModule(self, configModule: ConfigModule):
        self._configModule = configModule


_manager = ConfigModuleManager()


def getConfigModule():

    global _manager
    return _manager.configModule


def tryGetConfigModule() -> Optional[ConfigModule]:
    """Return the active ConfigModule, or None if none has been set.

    Unlike :func:`getConfigModule`, this does not raise when the singleton
    is uninitialized.  Used by code paths (e.g. the reservation ``gate``)
    that must be callable from unit tests which invoke subcommand apps
    directly without going through the top-level ``main`` callback.
    """
    global _manager
    return getattr(_manager, '_configModule', None)


def setConfigModule(
    configModule: Optional[ConfigModule] = None,
    lab: Optional[Lab] = None,
    repos: Optional[list[Repo]] = None,
    reservation_backend: Optional['ReservationBackend'] = None,
    identity: Optional['ResolvedIdentity'] = None,
    skip_reservation_check: bool = False,
):

    global _manager

    if      lab is not None \
        and repos is not None:
        configModule = ConfigModule(
            lab=lab,
            repos=repos,
            reservation_backend=reservation_backend,
            identity=identity,
            skip_reservation_check=skip_reservation_check,
        )

    if configModule is None:
        raise ValueError("Invalid ConfigModule. Either a ConfigModule object or a set of OttoEnv "
                         "and Lab objects must be provided.")

    _manager.configModule = configModule

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
    :meth:`RemoteHost.__post_init__` and therefore constructs a *fresh*
    :class:`ConnectionManager` with the override options wired in from
    the start. This is required because protocol options shape the
    connection itself (key algorithms, hop wiring, etc.) and cannot be
    swapped on an already-open connection. The original *host* and any
    connection it owns are untouched; the override copy will open its
    own connection on first use.

    When no overrides are supplied, the original *host* is returned
    unchanged so identity (``host is host``) is preserved for non-override
    callers.
    """
    overrides: dict[str, Any] = {
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
    if not overrides:
        return host
    return dataclasses.replace(host, **overrides)


def getHost(
    name: str,
) -> 'RemoteHost':

    configModule = getConfigModule()
    hosts = configModule.lab.hosts
    if name not in hosts:
        raise ValueError(f'Attempted to retrieve a host named "{name}", but no such host exists in {configModule.lab}')
    return configModule.lab.hosts[name]

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

    This is the *fleet* generator: by default it yields only real
    :class:`RemoteHost` instances and skips :class:`DockerContainerHost`
    entries, since containers aren't operated on as part of the host
    fleet (e.g. ``otto monitor``, coverage collection). Containers remain
    reachable for targeted use via tab completion and ``get_host`` —
    neither of which goes through this generator. Pass
    ``include_containers=True`` to yield container hosts as well.

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
            untouched. When no overrides are passed, the stored
            instances are yielded as-is so identity is preserved. Hop
            resolution is internal and is *not* affected by overrides.

    Yields:
        RemoteHost: Each matching host from the lab configuration.

    Examples:
        >>> import re
        >>> # assuming hosts: carrot_seed, tomato_seed, pepper_seed
        >>> seeds = list(all_hosts(re.compile(r"tomato")))  # doctest: +SKIP
    """
    from ..host.dockerHost import DockerContainerHost

    configModule = getConfigModule()
    for host in configModule.lab.hosts.values():
        if pattern is not None and not pattern.search(host.id):
            continue
        if not include_containers and isinstance(host, DockerContainerHost):
            continue
        yield _apply_option_overrides(
            host,
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
        method: Unbound async method (e.g. ``RemoteHost.oneshot``).
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
        >>> from otto.host import RemoteHost
        >>> results = await do_for_all_hosts(  # doctest: +SKIP
        ...     RemoteHost.oneshot, "uname -a",
        ...     pattern=re.compile(r"router"),
        ... )
    """
    hosts = list(all_hosts(
        pattern=pattern,
        include_containers=include_containers,
        ssh_options=ssh_options,
        telnet_options=telnet_options,
        sftp_options=sftp_options,
        scp_options=scp_options,
        ftp_options=ftp_options,
        nc_options=nc_options,
    ))

    if concurrent:
        results = await asyncio.gather(
            *(method(host, *args, **kwargs) for host in hosts),
            return_exceptions=True,
        )
        return dict(zip([h.id for h in hosts], results))

    out: dict[str, T | BaseException] = {}
    for host in hosts:
        try:
            out[host.id] = await method(host, *args, **kwargs)
        except BaseException as exc:
            out[host.id] = exc
    return out


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
    """Run commands on every matching host via :meth:`RemoteHost.run`.

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
    from ..host import RemoteHost

    cmd_list: list[str] = [cmds] if isinstance(cmds, str) else cmds

    async def _run_list(
        host: 'RemoteHost',
    ) -> 'RunResult':
        return await host.run(cmd_list, timeout=timeout)

    return await do_for_all_hosts(
        _run_list,
        pattern=pattern,
        concurrent=concurrent,
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
) -> 'RemoteHost':
    """Return the host registered under *host_id* in the active lab.

    Args:
        host_id: Unique host id (as produced by ``RemoteHost.id``).
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

    configModule = getConfigModule()
    host = configModule.lab.hosts[host_id]
    return _apply_option_overrides(
        host,
        ssh_options=ssh_options,
        telnet_options=telnet_options,
        sftp_options=sftp_options,
        scp_options=scp_options,
        ftp_options=ftp_options,
        nc_options=nc_options,
    )
