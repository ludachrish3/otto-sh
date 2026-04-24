import asyncio
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
) -> Generator['RemoteHost', Any, Any]:
    """Yield every host in the active lab, optionally filtered by regex.

    Args:
        pattern: Compiled regex matched against each host's ``id`` via
            ``pattern.search()``.  When *None* (the default), all hosts
            are yielded.

    Yields:
        RemoteHost: Each matching host from the lab configuration.

    Examples:
        >>> import re
        >>> # assuming hosts: carrot_seed, tomato_seed, pepper_seed
        >>> seeds = list(all_hosts(re.compile(r"tomato")))  # doctest: +SKIP
    """
    configModule = getConfigModule()
    for host in configModule.lab.hosts.values():
        if pattern is not None and not pattern.search(host.id):
            continue
        yield host

async def do_for_all_hosts(
    method: Callable[..., Awaitable[T]],
    *args: Any,
    pattern: re.Pattern[str] | None = None,
    concurrent: bool = True,
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
    hosts = list(all_hosts(pattern=pattern))

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
    )


def get_host(
    host_id: str,
) -> 'RemoteHost':

    configModule = getConfigModule()
    return configModule.lab.hosts[host_id]
