"""otto's per-invocation runtime composition root.

Owns the active Lab, the per-invocation runtime flags, and the host lifecycle
scope. Propagated via a ContextVar so the bare module accessors
(otto.configmodule.all_hosts/get_host) can stay zero-argument, while explicit
passing (OttoContext methods, open_context) is first-class.
"""

import asyncio
import re
from collections.abc import AsyncIterator, Awaitable, Callable, Iterator
from contextlib import asynccontextmanager
from contextvars import ContextVar, Token
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, TypeVar, cast

from typing_extensions import Self

if TYPE_CHECKING:
    from pathlib import Path

    from .configmodule.lab import Lab
    from .host import Results, UnixHost
    from .host.remote_host import RemoteHost

T = TypeVar("T")


class HostScope:
    """Owns hosts handed out during a command; closes any still-connected on exit.

    The deterministic backstop that replaces RemoteHost.__del__: a host created
    and passed around without an explicit ``async with`` is still closed when
    the scope exits. Registration is deduped by object identity; close() is
    assumed idempotent so an early per-host close and the sweep never collide.
    """

    def __init__(self) -> None:
        self._hosts: "list[RemoteHost]" = []

    def register(self, host: "RemoteHost") -> None:
        """Add *host* to the scope for deferred close on exit, deduplicating by identity."""
        if any(host is h for h in self._hosts):  # dedup by object identity
            return
        self._hosts.append(host)

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *exc: object) -> None:
        # Close on the Host *contract* (idempotent close()), not the
        # RemoteHost-private ``_connected``: DockerContainerHost / LocalHost are
        # BaseHosts without ``_connected``, so treat a missing attr as "needs
        # closing" (close() no-ops when nothing is open).
        await asyncio.gather(
            *(h.close() for h in self._hosts if getattr(h, "_connected", True)),
            return_exceptions=True,
        )


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


def reset_context(token: "Token[OttoContext | None]") -> None:
    """Restore the context ContextVar to the value it held before the matching ``set_context``."""
    _active.reset(token)


@dataclass
class OttoContext:
    """The active per-invocation runtime: chosen lab, runtime flags, and host lifecycle scope."""

    lab: "Lab"
    dry_run: bool = False
    log_command_output: bool = True
    output_dir: "Path | None" = None
    scope: HostScope = field(default_factory=HostScope)

    def get_host(self, host_id: str, **overrides: Any) -> "UnixHost":
        """Look up *host_id* in the active lab, apply any keyword overrides, and register it."""
        from .configmodule.configmodule import _apply_option_overrides

        try:
            host = self.lab.hosts[host_id]
        except KeyError:
            raise KeyError(
                f"No host {host_id!r} in lab {self.lab.name!r}. Available: {sorted(self.lab.hosts)}"
            ) from None
        resolved = _apply_option_overrides(cast("Any", host), **overrides)
        self.scope.register(resolved)
        return cast("UnixHost", resolved)

    def all_hosts(
        self,
        pattern: "re.Pattern[str] | None" = None,
        *,
        include_containers: bool = False,
        **overrides: Any,
    ) -> "Iterator[RemoteHost]":
        """Yield all hosts in the lab, optionally filtered by *pattern* and keyword overrides."""
        from .configmodule.configmodule import _apply_option_overrides
        from .host.docker_host import DockerContainerHost

        for host in self.lab.hosts.values():
            if pattern is not None and not pattern.search(host.id):
                continue
            if not include_containers and isinstance(host, DockerContainerHost):
                continue
            resolved = _apply_option_overrides(cast("Any", host), **overrides)
            self.scope.register(resolved)
            yield resolved

    async def do_for_all_hosts(  # noqa: PLR0913 — wide host-dispatch API
        self,
        method: "Callable[..., Awaitable[T]]",
        *args: Any,
        pattern: "re.Pattern[str] | None" = None,
        concurrent: bool = True,
        include_containers: bool = False,
        term: "str | None" = None,
        transfer: "str | None" = None,
        ssh_options: "Any" = None,
        telnet_options: "Any" = None,
        sftp_options: "Any" = None,
        scp_options: "Any" = None,
        ftp_options: "Any" = None,
        nc_options: "Any" = None,
        **kwargs: Any,
    ) -> "dict[str, T | BaseException]":
        """Call *method* on every matching host and return a ``{host_id: result}`` mapping.

        When *concurrent* is ``True`` (default), all calls are gathered in
        parallel via ``asyncio.gather``; exceptions from individual hosts are
        captured as values rather than propagated. When ``False``, hosts are
        called sequentially and exceptions are likewise captured.
        """
        hosts = list(
            self.all_hosts(
                pattern=pattern,
                include_containers=include_containers,
                term=term,
                transfer=transfer,
                ssh_options=ssh_options,
                telnet_options=telnet_options,
                sftp_options=sftp_options,
                scp_options=scp_options,
                ftp_options=ftp_options,
                nc_options=nc_options,
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
        timeout: float | None = None,
        *,
        include_containers: bool = False,
        term: "str | None" = None,
        transfer: "str | None" = None,
        ssh_options: "Any" = None,
        telnet_options: "Any" = None,
        sftp_options: "Any" = None,
        scp_options: "Any" = None,
        ftp_options: "Any" = None,
        nc_options: "Any" = None,
    ) -> "dict[str, Results | BaseException]":
        """Run one or more shell commands on every matching host and return a results mapping.

        Accepts a single command string or a list of commands executed in
        sequence on each host. Delegates concurrency and filtering to
        ``do_for_all_hosts``; exceptions from individual hosts are captured as
        values rather than propagated.
        """
        cmd_list = [cmds] if isinstance(cmds, str) else cmds

        async def _run_list(host: "UnixHost") -> "Results":
            return await host.run(cmd_list, timeout=timeout)

        return await self.do_for_all_hosts(
            _run_list,
            pattern=pattern,
            concurrent=concurrent,
            include_containers=include_containers,
            term=term,
            transfer=transfer,
            ssh_options=ssh_options,
            telnet_options=telnet_options,
            sftp_options=sftp_options,
            scp_options=scp_options,
            ftp_options=ftp_options,
            nc_options=nc_options,
        )


@asynccontextmanager
async def open_context(
    *,
    lab: "Lab | str | list[str]",
    dry_run: bool = False,
    log_command_output: bool = True,
    search_paths: "list[Path] | None" = None,
) -> "AsyncIterator[OttoContext]":
    """Build, install, and tear down an OttoContext for library / script use.

    Pass a Lab, or a lab name / list of names to load via load_lab. On exit the
    host scope closes any still-connected hosts and the contextvar is reset.
    Does NOT run a reservation check — that is a CLI concern; a script that wants
    one calls otto.reservations.check_reservations explicitly.
    """
    from .bootstrap import bootstrap

    bootstrap()  # composition root — idempotent; registers user init-module components
    from .configmodule import load_lab
    from .configmodule.lab import Lab

    resolved_lab = lab if isinstance(lab, Lab) else load_lab(lab, search_paths or [])
    ctx = OttoContext(lab=resolved_lab, dry_run=dry_run, log_command_output=log_command_output)
    token = set_context(ctx)
    try:
        async with ctx.scope:
            yield ctx
    finally:
        reset_context(token)
