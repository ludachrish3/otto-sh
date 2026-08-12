"""otto's per-invocation runtime composition root.

Owns the active Lab, the per-invocation runtime flags, and the host lifecycle
scope. Propagated via a ContextVar so the bare module accessors
(otto.config.all_hosts/get_host) can stay zero-argument, while explicit
passing (OttoContext methods, open_context) is first-class.
"""

import asyncio
import logging
import re
from collections.abc import AsyncIterator, Awaitable, Callable, Iterator
from contextlib import asynccontextmanager
from contextvars import ContextVar, Token
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, TypeVar, cast

from typing_extensions import Self

if TYPE_CHECKING:
    from pathlib import Path

    from .config.lab import Lab
    from .host import Results, UnixHost
    from .host.remote_host import RemoteHost

T = TypeVar("T")

logger = logging.getLogger(__name__)

LIBRARY_LAB_NAME = "<library>"
"""Sentinel ``Lab.name`` for the minimal, host-less context a library caller
gets for free.

``otto.suite.run._session_context`` installs ``OttoContext(lab=Lab(name=LIBRARY_LAB_NAME))``
around a session when no context is already active (e.g. ``run_suite()``/
``run_selection()`` called outside ``async with otto.open_context(...)``). That
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

    def rebuild_connections(self) -> None:
        """Drop per-loop connection state on every registered host.

        For hosts opened inside an inner pytest session (``otto test`` /
        ``run_suite``): their transports are bound to pytest's now-closed
        event loops, and no later loop can drive them — a cross-loop close
        only raises into the sweep's failure logging. Rebuilding (the same
        ``rebuild_connections`` pattern ``otto test --cov`` already uses to
        refresh hosts after ``pytest.main()`` returns) abandons the dead
        per-loop state so the post-run sweep closes only what the CURRENT
        loop actually owns. Real remote cleanup for suite-opened hosts
        belongs to the suite's own fixtures, on the loop that opened them.
        Hosts without the hook (fakes, minimal BaseHosts) are left as-is.
        """
        for host in self._hosts:
            rebuild = getattr(host, "rebuild_connections", None)
            if rebuild is not None:
                rebuild()

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *exc: object) -> None:
        # Close on the Host *contract* (idempotent close()), not the
        # RemoteHost-private ``_connected``: DockerContainerHost / LocalHost are
        # BaseHosts without ``_connected``, so treat a missing attr as "needs
        # closing" (close() no-ops when nothing is open).
        # Drain the list first: the lifecycle wrapper enters/exits this scope
        # once per asyncio.run, and a command may run several (suite pre/post
        # phases), so a swept host must not be re-closed by the next cycle.
        hosts, self._hosts = self._hosts, []
        remaining = [h for h in hosts if getattr(h, "_connected", True)]
        # Dependency-ranked sweep (chaos spec): a host that another registered
        # host names as its ``parent`` (DockerContainerHost documents
        # close-before-parent — its docker exec channel drains over the
        # parent's still-open transport) closes only after its dependents.
        # Within a rank closes run concurrently; failures are logged per host
        # — never silently swallowed — and never stop the remaining ranks.
        while remaining:
            parent_ids = {id(getattr(h, "parent", None)) for h in remaining}
            rank = [h for h in remaining if id(h) not in parent_ids]
            if not rank:
                rank = remaining  # parent cycle (impossible by construction): close all, don't spin
            results = await asyncio.gather(*(h.close() for h in rank), return_exceptions=True)
            for host, result in zip(rank, results, strict=True):
                if isinstance(result, BaseException):
                    logger.warning(
                        f"otto: closing host {getattr(host, 'id', host)!r} failed "
                        f"during scope sweep: {result!r}"
                    )
            closed = {id(h) for h in rank}
            remaining = [h for h in remaining if id(h) not in closed]


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


def reset_cli_context() -> None:
    """Undo :func:`set_cli_context` if it ran; safe to call unconditionally."""
    global _cli_token  # noqa: PLW0603 — module-level singleton/cache
    if _cli_token is not None:
        reset_context(_cli_token)
        _cli_token = None


# Deferred to here (rather than the top-of-file imports) on purpose: importing
# otto.host at module scope pulls in otto.host.interact, which imports
# try_get_context from this module at ITS module scope. Doing so before
# try_get_context is defined above would raise ImportError on a fresh
# `import otto.context` (circular import). Only a plain value is needed here,
# so the deferred position is enough — no need to push this to TYPE_CHECKING.
from .host.host import DEFAULT_COMMAND_TIMEOUT  # noqa: E402


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
        from .config.fleet import _apply_option_overrides

        host = self.lab.resolve_handle(host_id)
        if host is None:
            # The sentinel LIBRARY_LAB_NAME lab is what run_suite()/run_selection()
            # install for a library caller with no active context (see
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
        self.scope.register(resolved)
        return cast("UnixHost", resolved)

    def all_hosts(
        self,
        pattern: "re.Pattern[str] | None" = None,
        *,
        include_containers: bool = False,
        include_local: bool = False,
        **overrides: Any,
    ) -> "Iterator[RemoteHost]":
        """Yield all hosts in the lab, optionally filtered by *pattern* and keyword overrides.

        The built-in ``local`` host (the machine otto runs on, injected by
        ``load_lab`` for targeted ``otto host local`` use) is NOT part of the
        fleet: deploy/monitor/coverage sweeps must never silently operate on
        the runner itself, and it is not a ``RemoteHost``. Pass
        ``include_local=True`` to opt it in; ``get_host("local")`` always
        resolves it.
        """
        from .config.fleet import _apply_option_overrides
        from .host.docker_host import DockerContainerHost
        from .host.local_host import LocalHost

        for host in self.lab.hosts.values():
            if pattern is not None and not pattern.search(host.id):
                continue
            if not include_containers and isinstance(host, DockerContainerHost):
                continue
            if not include_local and isinstance(host, LocalHost):
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
        **kwargs: Any,
    ) -> "dict[str, T | BaseException]":
        """Call *method* on every matching host and return a ``{host_id: result}`` mapping.

        When *concurrent* is ``True`` (default), all calls are gathered in
        parallel via ``asyncio.gather``; exceptions from individual hosts are
        captured as values rather than propagated. When ``False``, hosts are
        called sequentially and exceptions are likewise captured. Fleet
        membership follows :meth:`all_hosts` — the built-in ``local`` host is
        excluded unless ``include_local=True``.
        """
        hosts = list(
            self.all_hosts(
                pattern=pattern,
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
            userland_options=userland_options,
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
    from .config import load_lab
    from .config.lab import Lab

    resolved_lab = lab if isinstance(lab, Lab) else load_lab(lab, search_paths or [])
    ctx = OttoContext(lab=resolved_lab, dry_run=dry_run, log_command_output=log_command_output)
    token = set_context(ctx)
    try:
        async with ctx.scope:
            yield ctx
    finally:
        reset_context(token)
