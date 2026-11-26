"""The survey engine: candidates, the tiers in order, one budget, discovery, resolution.

Family dispatch is by class: unix runs every tier; embedded swaps the
in-session inventory for the external sweep; local runs the inventory on
this machine and dials nothing; containers answer not-checkable. Every
check is bounded twice -- its own timeout and the survey's remaining
budget -- and a spent budget marks what is left ``timeout`` without running it.
"""

import asyncio
import time
from collections.abc import Callable, Coroutine
from dataclasses import dataclass, field
from functools import partial
from typing import TYPE_CHECKING, Any, Literal, TypeVar, cast

from ...errors import OttoError
from ...logger.mode import LogMode
from ...models.base import cred_identity
from ...result import CommandResult
from ...utils import Status
from ..connections import TERM_BACKENDS, TermMetadata
from ..login_proxy import cred_for
from ..options import NcOptions, SnmpOptions
from ..transfer.nc import (
    NcListenerMeasurement,
    available_rungs,
    run_port_rung,
    scan_start,
    what_the_count_says,
)
from ..transfer.nc_scripts import (
    CONTROL_AUTO,
    CONTROL_CUSTOM,
    LISTENER_LADDER,
    LISTENER_OPTION,
    PORT_LADDER,
    PORT_OPTION,
    ListenerRung,
)
from ..transfer.registry import TRANSFER_BACKENDS, connection_protocol
from ..transfer.sftp import open_sftp_or_attribute
from ..userland import APPLET_PRESENT, applet_capability
from .dial import DialOutcome, dial_direct, dial_via_hop
from .inventory import Inventory, Listener, run_inventory
from .login import (
    LoginOutcome,
    attempt_console_open,
    attempt_ftp_login,
    attempt_term_login,
    classify_login_error,
    session_login,
)
from .owners import DEFAULT_PORTS, SSH_SWEEP_PORTS, SWEEP_ALTERNATES, protocol_for_owner
from .snmp_check import check_snmp
from .sweep import SweepRow, parse_scan_ports, sweep_port_set, sweep_ports
from .verdict import (
    DIAL_TIMEOUT_S,
    INVENTORY_TIMEOUT_S,
    LOGIN_MARGIN_S,
    LOGIN_TIMEOUT_S,
    SNMP_RETRIES,
    SNMP_TIMEOUT_S,
    SURVEY_BUDGET_S,
    SWEEP_CONCURRENCY,
    SWEEP_DIAL_TIMEOUT_S,
    Candidate,
    Kind,
    ProtocolVerdict,
    State,
    Tier,
    merge_verdicts,
)

if TYPE_CHECKING:
    from ..host import BaseHost
    from ..remote_host import RemoteHost
    from ..unix_host import UnixHost

T = TypeVar("T")

_SESSION_TRANSFERS = frozenset({"scp", "sftp", "shell", "nc"})
_LOGIN_TERMS = frozenset({"ssh", "telnet"})
_LOGIN_PROTOCOLS = _LOGIN_TERMS | {"ftp"}
_DOCKER_REASON = "containers have no generic probe; docker exec is the only transport"
_TFTP_REASON = "tftp backend not implemented"
_DIAL_STATE: dict[str, State] = {
    "closed": "closed",
    "timeout": "timeout",
    "not-checkable": "not-checkable",
}
"""How a dial that ANSWERED reads as a verdict; ``open`` is never a verdict state.

``not-checkable`` is spelled here for the sweep and discovery paths; the
declared pre-dial no longer ends on it (Ruling 25) -- it falls through to the
login tier, whose own outcome becomes the row.
"""


@dataclass
class NcLadderResult:
    """What the survey found for one netcat strategy (``port_strategy`` or ``listener_check``).

    Attributes:
        state: ``measured`` when a strategy was run and worked, ``declared``
            when the host entry pins one (it is not run), ``unmeasured`` when
            no measurement succeeded: nothing could be run, or every strategy
            whose tool is on the host failed.
        rung: The strategy that worked (``measured``) or the entry's value
            (``declared``); ``None`` when ``unmeasured``.
        note: One sentence saying why the strategy was not measured.
    """

    state: Literal["measured", "declared", "unmeasured"]
    rung: "str | None" = None
    note: str = ""


@dataclass(frozen=True)
class NcFailedStrategy:
    """A netcat strategy whose tool is on the host and whose script failed in the probe.

    Attributes:
        ladder: ``port`` (``nc_options.port_strategy``) or ``listener``
            (``nc_options.listener_check``).
        strategy: The strategy's name, as the option spells it.
        reason: What went wrong, a timeout included.
    """

    ladder: Literal["port", "listener"]
    strategy: str
    reason: str


@dataclass
class NcRungs:
    """The netcat strategies the survey measured on the host.

    Attributes:
        port: The free-port strategy (``nc_options.port_strategy``).
        listener: The listener check (``nc_options.listener_check``).
        failed: Each strategy met on the way whose tool is on the host and
            whose script failed, in the order they were tried.
        listener_left: One sentence when the probe opened a listener with
            no lifetime cap and could not confirm it ended; empty otherwise.
    """

    port: NcLadderResult
    listener: NcLadderResult
    failed: list[NcFailedStrategy] = field(default_factory=list)
    listener_left: str = ""


@dataclass(slots=True)
class Survey:
    """The whole survey result: rows plus the footnotes and pins the report renders."""

    verdicts: list[ProtocolVerdict] = field(default_factory=list)
    not_applicable: list[str] = field(default_factory=list)
    footnotes: list[str] = field(default_factory=list)
    other_listeners: list[str] = field(default_factory=list)
    declared_ports: dict[str, int] = field(default_factory=dict)
    working_ports: dict[str, int] = field(default_factory=dict)
    supported: list[str] = field(default_factory=list)
    user: "str | None" = None
    terminal_mappings: dict[str, str] = field(default_factory=dict)
    """Per term a shell session opened on: how its terminal maps line endings.

    The first session on a term is the one recorded. An observation only:
    there is no host field to pin it in, because the handshake measures it on
    every session at no cost, and a stale pin would corrupt output silently."""
    nc_rungs: "NcRungs | None" = None
    """The netcat strategies measured on the host; ``None`` where no unix session opened."""


class _Deadline:
    """One survey-wide budget; ``remaining()`` is what every check is bounded by."""

    def __init__(self, budget_s: float) -> None:
        self._end = time.monotonic() + budget_s

    def remaining(self) -> float:
        return max(0.0, self._end - time.monotonic())

    @property
    def spent(self) -> bool:
        return self.remaining() <= 0.0


def _verdict(c: Candidate, state: State, tier: Tier, vantage: str, detail: str) -> ProtocolVerdict:
    return ProtocolVerdict(
        protocol=c.protocol,
        kind=c.kind,
        port=c.port,
        state=state,
        tier=tier,
        vantage=vantage,
        detail=detail,
    )


async def _bounded(
    deadline: _Deadline, timeout: float, coro: "Coroutine[Any, Any, T]"
) -> "T | None":
    """Await *coro* bounded by *timeout* and the remaining budget; ``None`` when spent."""
    grant = min(timeout, deadline.remaining())
    if grant <= 0.0:
        coro.close()
        return None
    try:
        return await asyncio.wait_for(coro, grant)
    except (TimeoutError, asyncio.TimeoutError):
        return None


def _own_login_name(host: "BaseHost") -> str:
    """``login 'admin'`` for a remote host; ``login <local>`` for the machine otto runs on."""
    manager = getattr(host, "connections", None)
    return f"login {manager.login_target!r}" if manager is not None else "login <local>"


def _shell_login_budget(host: "BaseHost", term: "str | None" = None) -> float:
    """Return the budget for one shell login on *host* over *term*, never under ``LOGIN_TIMEOUT_S``.

    A login that never yields a shell is reported only once the session has
    spent its whole ``open_budget``, which grows with the host's
    ``init_timeout`` and *term*'s connect waits (*term* defaults to the
    host's own). Anything shorter would cut the attempt off first and turn a
    refused password into ``timeout``, a state the drift table and the pin
    discard. ``LOGIN_MARGIN_S`` is added per attempt, for the waits the
    session leaves unbounded.
    """
    mgr = host._session_mgr  # noqa: SLF001 — intra-package read of the host's own session manager
    return max(LOGIN_TIMEOUT_S, mgr.open_budget(term=term) + 2 * LOGIN_MARGIN_S)


async def _open_own_session(
    host: "BaseHost", timeout: float, *, who: "str | None" = None
) -> LoginOutcome:
    """Open the host's own persistent session; that opening is the declared term's login.

    *who* names the identity when ``--user`` forced one, so the row says the
    login was asked for rather than picked; otherwise the session's own cred.
    """
    return await session_login(host, who=who or _own_login_name(host), timeout=timeout)


async def _run_only(host: "BaseHost", cmd: str, *, sudo: bool = False) -> "CommandResult":
    return (await host.run(cmd, sudo=sudo, timeout=INVENTORY_TIMEOUT_S, log=LogMode.NEVER)).only


async def _exec_only(host: "BaseHost", cmd: str, *, user: "str | None") -> "CommandResult":
    """Run *cmd* the way a netcat transfer runs its control scripts: ``host.exec``.

    Never ``host.run``: over ssh that is a different shell and ``PATH``.
    """
    return await host.exec(cmd, timeout=INVENTORY_TIMEOUT_S, log=LogMode.NEVER, user=user)


class _BudgetSpent(Exception):  # noqa: N818 — a private signal, not an error a caller sees
    """The survey's budget ran out while the netcat strategies were being measured."""


_NcRunner = Callable[[str], Coroutine[Any, Any, "CommandResult"]]
_NC_BUDGET_SPENT = "the survey budget ran out before it ran"


def _nc_unmeasured(note: str) -> NcRungs:
    """Both netcat strategies unmeasured, for one reason."""
    return NcRungs(NcLadderResult("unmeasured", note=note), NcLadderResult("unmeasured", note=note))


def _nc_declared(value: str) -> "NcLadderResult | None":
    """Return a ladder the entry pins (any value but ``auto``) as ``declared``; ``None`` if not."""
    return None if value == CONTROL_AUTO else NcLadderResult("declared", value)


def _nc_error(exc: Exception) -> str:
    """Word a runner's error: otto's own by its text, any other by its type and text."""
    if isinstance(exc, OttoError):
        return str(exc)
    text = str(exc)
    return f"{type(exc).__name__}: {text}" if text else type(exc).__name__


_NC_NO_TOOL_REMEDY = {
    PORT_OPTION: f'set nc_options.{PORT_OPTION} to "{CONTROL_CUSTOM}" with a port_cmd that'
    " prints a free port",
    LISTENER_OPTION: f'set nc_options.{LISTENER_OPTION} to "{CONTROL_CUSTOM}" with a'
    " listener_cmd that exits 0 when {port} is listening",
}
"""What a user can do when no strategy's tool is on the host: the ``custom`` contract."""

_NC_NO_MEASUREMENT = "the nc transfer registered for this host cannot open a listener for the probe"


def _nc_no_backend(exc: Exception) -> str:
    """Word why the listener checks were not measured: the netcat backend could not be had."""
    return f"otto could not set up netcat for this host: {_nc_error(exc)}"


def _nc_no_port(why: str) -> str:
    """Word why the listener checks were not measured: no free port to open a listener on."""
    return f"there was no free port to open a listener on: {why}"


def _nc_not_counted(into: NcListenerMeasurement, *, counted_late: bool) -> str:
    """Word no check counting the probe's listener within the readiness wait.

    *counted_late* is whether a check counted it after the wait, in the
    complete round: the listener then started late, which is known, and
    every check being blind is not.
    """
    port = into.port
    said = (
        f"no listener check counted the listener otto opened on :{port} within {into.waited_s:g} s"
    )
    if counted_late:
        return (
            f"{said}, but a check counted it after that, so it took longer than that to start."
            " A transfer waits the same time for its listener, so it would fail the same way."
            " Run the probe again to see whether it starts in time"
        )
    if into.was_up:
        return (
            f"{said}, and it was up when otto closed it. Either every check is blind to this"
            " netcat, or the listener took longer than that to start. Run the probe again; if"
            f" no check counts it again, {_NC_NO_TOOL_REMEDY[LISTENER_OPTION]}"
        )
    return (
        f"{said}, and otto could not tell whether it started. Run '{into.bare_command}' on"
        " the host to see whether it starts, or run the probe again"
    )


def _nc_ended_early(into: NcListenerMeasurement) -> str:
    """Word a probe listener that ended before otto closed it; *into*'s words say how."""
    port = into.port
    if not into.spawned:
        return f"otto could not start a listener on :{port}: {into.ending}. Run the probe again"
    if into.lost:
        return (
            f"otto lost the listener it opened on :{port} before it closed it: {into.ending}."
            " Run the probe again"
        )
    said = f"the listener otto opened on :{port} ended before otto closed it: {into.ending}"
    if into.netcat_could_not_run:
        return said
    if into.exit_status == 0:
        return (
            f"{said}. Either another process connected to it before otto did, or this netcat"
            " does not stay listening when its input is empty. Run"
            f" '{into.bare_command} < /dev/null' on the host to see whether it stays"
            " listening, or run the probe again"
        )
    return (
        f"{said}. Either this netcat refuses that command, or another process took the port"
        f" in the same moment. Run '{into.bare_command}' on the host to see its message,"
        " or run the probe again"
    )


def _nc_lost_in_the_close(into: NcListenerMeasurement) -> str:
    """Word a probe listener whose command raised or gave no exit status while otto closed it."""
    return (
        f"otto lost the listener it opened on :{into.port} while it closed it: {into.ending}."
        " Run the probe again"
    )


def _nc_budget_spent_measuring(port: int) -> str:
    """Word the survey's budget ending while the probe's listener was open."""
    return (
        f"the survey budget ran out while otto measured the listener it opened on :{port}."
        " Run the probe again"
    )


def _nc_runner_failed(exc: Exception) -> str:
    """Word a runner error that ended the listener measurement."""
    cause = _nc_error(exc).rstrip(".")
    return f"the listener checks could not be run: {cause}. Run the probe again"


def _nc_not_seen(port: int) -> str:
    """Word a listener check that counted none on the port the probe's listener held."""
    return f"did not see the listener otto opened on :{port}"


def _nc_no_answer(port: int) -> str:
    """Word a listener check that gave no answer to read."""
    return f"no answer about :{port}; it timed out or printed nothing"


def _nc_counted_many(count: int, port: int) -> str:
    """Word a listener check that counted more than the one listener the probe opened."""
    return (
        f"counted {count} listeners on :{port}, where otto opened one. Either the check"
        " miscounts this netcat, so a transfer would refuse the port as shared, or another"
        " process holds the same port. Running the probe again may tell them apart; a"
        " repeated result is not proof of either"
    )


def _nc_listener_left(port: int) -> str:
    """Word an unconfirmed end of the probe's listener on *port* that no lifetime cap bounds."""
    return (
        f"otto could not confirm the listener it opened on :{port} ended. No lifetime cap"
        " could be installed, so it may stay until it is ended on the host"
    )


def _no_rung_note(available: frozenset[str], ladder: "tuple[Any, ...]", option: str) -> str:
    """Why a walked ladder found no strategy: none had its tool, or every one that did failed."""
    if any(rung.name in available for rung in ladder):
        return "every strategy whose tool is on the host failed"
    return f"no strategy's tool is on the host; {_NC_NO_TOOL_REMEDY[option]}"


async def _finished(step: "Callable[[], Coroutine[Any, Any, None]]") -> bool:
    """Run *step* and return True, so a bounded caller tells a finished step from a cut one.

    *step* makes its coroutine only once this one runs, so a caller that
    closes this one unstarted leaves no coroutine of the step unawaited.
    """
    await step()
    return True


def _listener_verdict(
    checks: "list[ListenerRung]",
    into: NcListenerMeasurement,
    failed: list[NcFailedStrategy],
) -> NcLadderResult:
    """Turn what one listener measurement recorded into the listener ladder's result.

    Read in this order, each leaving the ladder unmeasured whatever
    ``answers`` hold: ``ended_early`` (an exit status is the most specific
    fact), then ``timed_out``, then :attr:`NcListenerMeasurement.lost` (the
    command raised or gave no exit status during the close). When none is
    set, the backend guarantees ``answers`` is a complete round, one per
    check. Then the first check in ladder order whose count reads as one is
    measured, the ones before it are named in *failed*, and the ones after it
    are not reported.
    """
    port = into.port
    if into.ended_early:
        return NcLadderResult("unmeasured", note=_nc_ended_early(into))
    if into.timed_out:
        late = any(
            isinstance(n, int) and what_the_count_says(n) == "one" for n in into.answers.values()
        )
        return NcLadderResult("unmeasured", note=_nc_not_counted(into, counted_late=late))
    if into.lost:
        return NcLadderResult("unmeasured", note=_nc_lost_in_the_close(into))
    for rung in checks:
        answer = into.answers.get(rung.name)
        if isinstance(answer, Exception):
            failed.append(NcFailedStrategy("listener", rung.name, _nc_error(answer)))
            continue
        says = what_the_count_says(answer)
        if says == "one":
            return NcLadderResult("measured", rung.name)
        if says == "no answer":
            reason = _nc_no_answer(port)
        elif says == "none":
            reason = _nc_not_seen(port)
        else:
            reason = _nc_counted_many(cast("int", answer), port)
        failed.append(NcFailedStrategy("listener", rung.name, reason))
    note = _no_rung_note(frozenset(r.name for r in checks), LISTENER_LADDER, LISTENER_OPTION)
    return NcLadderResult("unmeasured", note=note)


def dialling_terms() -> dict[str, TermMetadata]:
    """Return registered term backends reached by dialling the HOST's own address.

    A console term is addressed at its console SERVER, not the host, so it has
    no port on the host's own address for a port survey to dial — every survey
    read of ``TERM_BACKENDS`` routes through here instead, so a term like it
    never manufactures a phantom candidate, dial or login attempt against the
    host.
    """
    return {n: e.metadata for n, e in TERM_BACKENDS.raw_items() if e.metadata.dials_host}


def _family_candidates(
    family: str, declared: "Callable[[str, Kind], int]"
) -> "tuple[list[Candidate], list[str]]":
    """Split registered protocols into this family's candidates and the not-applicable names."""
    cands: list[Candidate] = []
    other: list[str] = []
    for name, backend in dialling_terms().items():
        if family in backend.host_families:
            cands.append(Candidate(name, "term", declared(name, "term"), declared=True))
        else:
            other.append(name)
    for name, entry in TRANSFER_BACKENDS.raw_items():
        if family in entry.metadata.host_families:
            cands.append(Candidate(name, "transfer", declared(name, "transfer"), declared=True))
        else:
            other.append(name)
    cands.append(Candidate("snmp", "monitor", declared("snmp", "monitor"), declared=True))
    return cands, sorted(other)


def _vantage(host: "RemoteHost") -> str:
    return f"hop:{host.hop}" if host.hop else "controller"


def _declared_port_unix(host: "UnixHost", name: str, _kind: Kind) -> int:
    if name == "ssh":
        return host.ssh_options.port
    if name == "telnet":
        return host.telnet_options.port
    if name == "ftp":
        return host.ftp_options.port
    if name == "snmp":
        return host.snmp.port if host.snmp is not None else DEFAULT_PORTS["snmp"]
    if connection_protocol(name) == "ssh":
        return host.ssh_options.port
    if name in ("shell", "nc"):
        return host.ssh_options.port if host.term == "ssh" else host.telnet_options.port
    return DEFAULT_PORTS.get(name, 0)


async def _hop_run(hop: "RemoteHost", timeout: float, cmd: str) -> "CommandResult":
    return (await hop.run(cmd, timeout=timeout + 2.0, log=LogMode.NEVER)).only


def dial_for(
    host: "RemoteHost", *, timeout: float
) -> "Callable[[int], Coroutine[Any, Any, DialOutcome]]":
    """Return the dial *host*'s connect path makes, bound to its vantage and *timeout*.

    Every dial in the survey goes through this: the declared pre-dial, the
    discovery dial, and the embedded sweep, which takes it as its dial
    callable. It is public because an observer of the survey -- a bed test
    asserting what the hop's dial really says about a port -- has to make the
    SAME dial the engine makes, and a hand-rolled copy of hop resolution, the
    hop's userland and the hop-side command budget can drift from this one
    silently. One binding, used by the engine itself, cannot.

    A coroutine, not a bare awaitable: :func:`_bounded` closes the one it is
    handed when the budget is already spent, and the sweep takes the callable
    as its ``Awaitable``-returning dial, which this satisfies.
    """

    async def dial(port: int) -> DialOutcome:
        return await _dial(host, port, timeout=timeout)

    return dial


async def _dial(host: "RemoteHost", port: int, *, timeout: float) -> DialOutcome:
    """Dial from the connect path's vantage: the hop when there is one."""
    if host.hop is None:
        return await dial_direct(host.ip, port, timeout=timeout)
    hop = host.hop_host()
    if hop is None:
        return DialOutcome(state="not-checkable", detail=f"hop {host.hop} not resolvable")
    userland = None
    resolver = getattr(hop, "userland", None)
    if callable(resolver):
        userland = resolver()
    if userland is not None:
        await userland.resolve()
    run = partial(_hop_run, hop, timeout)
    return await dial_via_hop(run, host.hop, host.ip, port, timeout=timeout, userland=userland)


def _snmp_target(host: "RemoteHost") -> "tuple[str, int, str, str] | None":
    """(address, port, community, version) from the host's block, or a direct host's defaults."""
    if host.snmp is not None:
        return (
            host.address_for(name_or_literal=host.snmp.address or host.ip),
            host.snmp.port,
            host.snmp.community,
            host.snmp.version,
        )
    if host.hop is None:
        defaults = SnmpOptions()
        return (host.ip, defaults.port, defaults.community, defaults.version)
    return None


async def _snmp_row(
    port: int,
    deadline: _Deadline,
    *,
    address: str,
    community: str,
    version: str,
) -> ProtocolVerdict:
    """One snmp GET, always labelled ``vantage="controller"``.

    Ruling 24: pysnmp issues the GET itself, a local UDP call from wherever
    otto is running -- never routed through a hop session the way a dial or
    a login is -- so the row's vantage names where the check ran, not the
    path to the target address. A hopped host's declared relay endpoint
    (``_snmp_target``) is still reached correctly; only the label changes.
    """
    got = await _bounded(
        deadline,
        SNMP_TIMEOUT_S * (SNMP_RETRIES + 1) + 1.0,
        check_snmp(
            address=address,
            port=port,
            community=community,
            version=version,
            vantage="controller",
            timeout=SNMP_TIMEOUT_S,
            retries=SNMP_RETRIES,
        ),
    )
    if got is None:
        return ProtocolVerdict(
            protocol="snmp",
            kind="monitor",
            port=port,
            state="timeout",
            tier="dial",
            vantage="controller",
            detail="survey budget exhausted",
        )
    return got


def _resolve(survey: Survey) -> None:
    dialling = dialling_terms()
    by_proto: dict[str, list[int]] = {}
    for v in survey.verdicts:
        if v.state != "supported":
            continue
        if v.kind == "term" and v.protocol not in dialling:
            # A non-dialling term (dials_host=False, e.g. console) has no
            # port on the host's own address -- its login row stays in
            # survey.verdicts so the report still shows it, but it never
            # becomes a "supported" port or a working-port pin; there is no
            # port for a menu_pin *_options fragment to name. Keyed on the
            # row's kind, not its name: "console" is also the embedded
            # transfer backend, and that transfer row is a real supported
            # protocol on the declared console port.
            continue
        by_proto.setdefault(v.protocol, []).append(v.port)
    survey.supported = sorted(by_proto)
    for proto, ports in by_proto.items():
        declared = survey.declared_ports.get(proto)
        if declared in ports:
            continue
        undeclared = sorted(set(ports))
        if len(undeclared) == 1:
            survey.working_ports[proto] = undeclared[0]
        else:
            key = "snmp" if proto == "snmp" else f"{proto}_options"
            joined = " and ".join(map(str, undeclared))
            survey.footnotes.append(f"{proto}: supported on {joined}; choose one in {key}.port")


class _UnixSurvey:
    """The unix arm: seven steps in the spec's order, one per method, one budget."""

    def __init__(self, host: "UnixHost", *, user: "str | None", extra_ports: list[int]) -> None:
        self.host = host
        self.user = user
        self.extra_ports = extra_ports
        self.deadline = _Deadline(SURVEY_BUDGET_S)
        self.survey = Survey(user=user)
        self.rows: list[ProtocolVerdict] = []
        self.session_vantage = f"session:{host.term}"
        self.cands, self.survey.not_applicable = _family_candidates(
            "unix", lambda n, k: _declared_port_unix(host, n, k)
        )
        self.survey.declared_ports = {c.protocol: c.port for c in self.cands}
        self.own_ok = False
        self.userland: "Any | None" = None
        self.declared_supported: set[str] = set()

    async def run(self) -> Survey:
        await self.declared_logins()
        inventory: "Inventory | None" = None
        if self.own_ok:
            await self.session_tier()
            # Frozen before discovery: a port discovered for a protocol whose
            # DECLARED port already answered is not a second working port.
            self.declared_supported = {
                v.protocol
                for v in self.rows
                if v.state == "supported" and v.protocol in _LOGIN_PROTOCOLS
            }
            inventory = await self.inventory_tier()
            if inventory is not None:
                await self.discovery(inventory)
        else:
            # Spec 7 step 2: no own session, no inventory -- it would run over
            # the session that just failed to open.
            self.no_session_rows()
        await self.snmp_tier()
        await self.extra_dials()
        if self.own_ok:
            # Last: it reads only the nc row, and a host whose exec hangs, or
            # whose listener is slow to end, must not starve the older tiers
            # of the budget.
            self.survey.nc_rungs = await self.nc_rung_tier()
        self.survey.verdicts = merge_verdicts(self.rows)
        _resolve(self.survey)
        return self.survey

    def _login_for(self, protocol: str) -> "str | None":
        """``--user`` for the protocols its cred applies to; the default pick otherwise."""
        user = self.user
        if user is None:
            return None
        if cred_for(self.host.creds, user, None) is None:
            # The login names no cred at all: pass it through so the attempt itself
            # reports not-checkable with "no cred for login", never guessed here.
            return user
        return user if cred_for(self.host.creds, user, protocol) is not None else None

    def _note_mapping(self, term: str, got: "LoginOutcome | None") -> None:
        """Keep the terminal mapping of the first shell session *term* opened."""
        if got is not None and got.terminal_mapping is not None:
            self.survey.terminal_mappings.setdefault(term, got.terminal_mapping)

    def _budget_row(self, c: Candidate, tier: Tier) -> None:
        self.rows.append(
            _verdict(c, "timeout", tier, _vantage(self.host), "survey budget exhausted")
        )

    async def declared_logins(self) -> None:
        if self.host.term not in dialling_terms() and self.host.term in TERM_BACKENDS:
            # A non-dialling own term (dials_host=False, e.g. console) never
            # becomes a candidate -- dialling_terms() excludes it, so it has
            # no port on the host's own address to dial. It still owns the
            # session, so it runs the same own-term path as ssh/telnet below,
            # just with no candidate, no dial, and no declared-port row.
            own = Candidate(
                self.host.term, "term", DEFAULT_PORTS.get(self.host.term, 0), declared=True
            )
            if self.deadline.spent:
                self._budget_row(own, "login")
            else:
                got = await self._own_session()
                self.own_ok = got is not None and got.state == "supported"
                self._note_mapping(self.host.term, got)
                if got is None:
                    self._budget_row(own, "login")
                else:
                    self.rows.append(
                        _verdict(own, got.state, "login", _vantage(self.host), got.detail)
                    )
        for c in self.cands:
            if c.kind != "term" and c.protocol != "ftp":
                continue
            if self.deadline.spent:
                # The own term opens its session; everything else is dialed
                # first, so that is the tier this row never got to.
                self._budget_row(c, "login" if c.protocol == self.host.term else "dial")
                continue
            if c.protocol == self.host.term:
                got = await self._own_session()
                self.own_ok = got is not None and got.state == "supported"
            else:
                if not await self._declared_dial(c):
                    continue
                got = await self._declared_login(c)
            self._note_mapping(c.protocol, got)
            if got is None:
                self._budget_row(c, "login")
            else:
                self.rows.append(_verdict(c, got.state, "login", _vantage(self.host), got.detail))

    async def _declared_dial(self, c: Candidate) -> bool:
        """Dial *c*'s declared port; ``True`` when it is open and a login may be tried.

        A DECLARED candidate goes straight to the login tier, so the login
        classifier's premise -- that a dial has already found the service --
        is only true if the survey makes it true. It matters most through a
        hop, where nothing of otto's touches the socket: the connect is a
        forward the hop performs, the local end of it succeeds, and the EOF
        that arrives when there is nothing behind it reads as a refused
        password. That fabricates ``login-failed`` -- an ANSWER, which drifts
        and pins -- for a port with no service at all.

        It is also lockout hygiene: the spec never retries a refused login,
        and never tries one on a port nothing is listening on.

        A dial that could not RUN is a different thing from a dial that
        answered (Ruling 25, narrowing Ruling 21): behind a hop with no dial
        tool -- a BusyBox build without ``nc``, or with an ``nc`` that has no
        ``-z`` -- the dial is ``not-checkable``, and ending there made every
        non-own term and ftp on that host read
        ``not-checkable: hop <id> offers no dial tool`` for a service that
        would have logged in, losing the survey's authoritative tier to a
        tool gap on the HOP. So a ``not-checkable`` dial falls through and
        the login's own outcome is the row; ``closed`` and ``timeout`` are
        answers, and those still end it with no login attempted. The lockout
        rule is about refused CREDS, not unreachable dials.
        """
        got = await _bounded(
            self.deadline,
            DIAL_TIMEOUT_S + 1.0,
            dial_for(self.host, timeout=DIAL_TIMEOUT_S)(c.port),
        )
        if got is None:
            self._budget_row(c, "dial")
            return False
        if got.state in ("open", "not-checkable"):
            return True
        state = _DIAL_STATE.get(got.state)
        if state is None:
            msg = f"dial state {got.state!r} has no verdict spelling in _DIAL_STATE"
            raise ValueError(msg)
        self.rows.append(_verdict(c, state, "dial", _vantage(self.host), got.detail))
        return False

    async def _declared_login(self, c: Candidate) -> "LoginOutcome | None":
        """One login attempt on a declared port the dial found open."""
        return await self._candidate_login(c.protocol, c.port)

    async def _candidate_login(self, protocol: str, port: int) -> "LoginOutcome | None":
        """One login attempt over *protocol* on *port*, declared or discovered alike.

        ``None`` when the survey's own budget ran out first.
        """
        login = self._login_for(protocol)
        if protocol == "ftp":
            return await _bounded(
                self.deadline,
                LOGIN_TIMEOUT_S,
                attempt_ftp_login(self.host, port, login=login, timeout=LOGIN_TIMEOUT_S),
            )
        budget = _shell_login_budget(self.host, protocol)
        return await _bounded(
            self.deadline,
            budget,
            attempt_term_login(self.host, protocol, port, login=login, timeout=budget),
        )

    async def _own_session(self) -> "LoginOutcome | None":
        """Open the host's own session, switched to ``--user`` when it applies to the term.

        The session tier and the inventory both ride on THIS session, so when
        the operator named a login it has to be the one that opens -- and the
        row has to say the login was forced rather than picked.
        """
        host, user = self.host, self.user
        cred = cred_for(host.creds, user, host.term) if user is not None else None
        budget = _shell_login_budget(host)
        if cred is None or user is None:
            return await _bounded(self.deadline, budget, _open_own_session(host, budget))
        who = f"login {cred_identity(cred.login, cred.protocols)!r} (--user)"
        async with host.as_user(user=user):
            return await _bounded(self.deadline, budget, _open_own_session(host, budget, who=who))

    def _no_session_detail(self) -> str:
        """Name the term that DID log in, if one did: the session tier never falls back to it.

        The login tier logs in on a COPY and tears it down, so a term that
        answered has left no session to carry scp/nc/sftp/shell. Saying which
        one answered, and the one-word change that would use it, is the whole
        remedy the operator needs.
        """
        other = next(
            (
                v.protocol
                for v in self.rows
                if v.tier == "login"
                and v.state == "supported"
                and v.protocol in _LOGIN_TERMS
                and v.protocol != self.host.term
            ),
            None,
        )
        if other is not None:
            return (
                f"needs the host's own term ({self.host.term}); {other} logged in"
                f" — set term={other} to use it"
            )
        names = [c.protocol for c in self.cands if c.kind == "term"]
        if self.host.term not in names:
            # The own term is non-dialling (dials_host=False, e.g. console):
            # it has no candidate, so it is never in `names` on its own --
            # name it explicitly or a console-term host's own-session
            # failure reads as though its own term was never tried at all.
            names.append(self.host.term)
        return f"needs a term session; tried {', '.join(names)}"

    def no_session_rows(self) -> None:
        detail = self._no_session_detail()
        for c in self.cands:
            if c.protocol in _SESSION_TRANSFERS:
                self.rows.append(_verdict(c, "no-session", "session", self.session_vantage, detail))
            elif c.protocol == "tftp":
                self.rows.append(
                    _verdict(c, "not-checkable", "session", self.session_vantage, _TFTP_REASON)
                )
        self.survey.footnotes.append("no term logged in: inventory not run")

    async def session_tier(self) -> None:
        self.userland = self.host.userland()
        if self.userland is not None:
            await self.userland.resolve()
        for c in self.cands:
            if c.protocol == "shell":
                self.rows.append(
                    _verdict(
                        c, "supported", "session", self.session_vantage, "the carrying session"
                    )
                )
            elif c.protocol in ("scp", "nc"):
                self.rows.append(self._applet_row(c))
            elif c.protocol == "sftp":
                self.rows.append(await self._sftp_row(c))
            elif c.protocol == "tftp":
                self.rows.append(
                    _verdict(c, "not-checkable", "session", self.session_vantage, _TFTP_REASON)
                )

    def _applet_row(self, c: Candidate) -> ProtocolVerdict:
        ul = self.userland
        if ul is None:
            return _verdict(
                c,
                "not-checkable",
                "userland",
                self.session_vantage,
                "no userland resolver on this host",
            )
        if not ul.is_settled(name=applet_capability(c.protocol)):
            return _verdict(
                c, "not-checkable", "userland", self.session_vantage, "userland probe unsettled"
            )
        if ul.has_applet(applet=c.protocol) == APPLET_PRESENT:
            return _verdict(
                c, "supported", "userland", self.session_vantage, f"{c.protocol} applet present"
            )
        return _verdict(
            c,
            "service-mismatch",
            "userland",
            self.session_vantage,
            f"{c.protocol} binary absent on the host",
        )

    async def _sftp_row(self, c: Candidate) -> ProtocolVerdict:
        if self.host.term != "ssh":
            return _verdict(
                c,
                "no-session",
                "session",
                self.session_vantage,
                f"needs ssh; the session is {self.host.term}",
            )
        try:
            got = await _bounded(
                self.deadline,
                LOGIN_TIMEOUT_S,
                open_sftp_or_attribute(
                    self.host.connections, host=self.host.name, attempted="probe"
                ),
            )
        except Exception as exc:  # noqa: BLE001 — a refused subsystem is a verdict, not a crash
            return _verdict(
                c,
                "service-mismatch",
                "session",
                self.session_vantage,
                f"sftp subsystem refused: {type(exc).__name__}: {exc}",
            )
        if got is None:
            return _verdict(
                c, "timeout", "session", self.session_vantage, "survey budget exhausted"
            )
        return _verdict(c, "supported", "session", self.session_vantage, "subsystem opened")

    def _snmp_is_the_hosts_own_address(self) -> bool:
        """Whether the snmp endpoint is this host itself rather than a relay.

        ``snmp.address``/``port`` are documented as the endpoint reachable
        from the OTTO HOST -- "for an embedded device behind a hop this is
        typically the local end of a UDP relay on the hop host, not the
        device's own address" (lab-config). When it names a relay, the
        guest's own socket table says nothing whatever about the agent, so
        an inventory row built from it ("nothing bound on :161") is a verdict
        read off the wrong machine.
        """
        target = _snmp_target(self.host)
        return target is not None and target[0] == self.host.ip

    async def snmp_tier(self) -> None:
        """Issue the declared snmp GET, whatever the session and the inventory did.

        Ruling 26. snmp is a candidate on every family (spec 5.1) and the GET
        IS its dial: ``check_snmp`` is a controller-issued UDP call that never
        rides the host's session, so a term that would not log in, or an
        inventory that errored, says nothing about whether the agent answers.
        Before this it was reached only through ``discovery`` -- which runs
        only when an inventory ran -- so a host whose session failed got NO
        snmp row at all.
        """
        c = next(c for c in self.cands if c.protocol == "snmp")
        target = _snmp_target(self.host)
        if target is None:
            self.rows.append(
                _verdict(
                    c,
                    "not-checkable",
                    "dial",
                    _vantage(self.host),
                    "snmp needs a declared relay endpoint behind a hop",
                )
            )
            return
        address, port, community, version = target
        self.rows.append(
            await _snmp_row(
                port, self.deadline, address=address, community=community, version=version
            )
        )

    async def inventory_tier(self) -> "Inventory | None":
        host = self.host
        run = partial(_run_only, host)
        refused = False
        if self.user is not None:
            async with host.as_user(user=self.user):
                inv = await _bounded(
                    self.deadline, INVENTORY_TIMEOUT_S * 2 + 1.0, run_inventory(run, elevate=False)
                )
        else:
            mech = self.userland.elevation if self.userland is not None else "none"
            # Elevate only when the session can: a mechanism exists, and the
            # session is already root or otto will answer the prompt.
            elevate = host._can_elevate(mech)  # noqa: SLF001 — the elevation seam's own decision, not a copy
            inv = await _bounded(
                self.deadline, INVENTORY_TIMEOUT_S * 2 + 1.0, run_inventory(run, elevate=elevate)
            )
            refused = bool(inv is not None and elevate and not inv.elevated and not inv.error)
        if inv is None:
            self.survey.footnotes.append("inventory: survey budget exhausted before it ran")
            return None
        if inv.error:
            self.survey.footnotes.append(inv.error)
            return None
        self._inventory_footnotes(inv, elevation_refused=refused)
        self._declared_inventory_rows(inv)
        return inv

    async def nc_rung_tier(self) -> NcRungs:
        """Measure which netcat strategies work on the host, by running the ladders' own scripts.

        Runs only when the session is not a console and, while
        ``nc_options.exec_name`` is the default, when the session tier found
        ``nc`` supported. A strategy the entry pins is reported ``declared``
        and not run, whatever fails afterwards; with both pinned no command
        runs. The ``auto`` port ladder is walked in its table order and stops
        at the first strategy that works; one whose tool is present and whose
        script failed, by any error of the runner, is named in ``failed``.

        The listener checks are measured against a listener the probe opens:
        one netcat listener on a free port, counted by each check whose tool
        is on the host and then closed (:meth:`_listener_ladder`). When the
        probe could not confirm that listener ended and no lifetime cap ends
        it, ``listener_left`` says so.
        """
        host = self.host
        if host.term == "console":
            return _nc_unmeasured("the session is a console, where netcat cannot be used")
        opts = host.nc_options
        if opts.exec_name == NcOptions().exec_name and not any(
            v.protocol == "nc" and v.state == "supported" for v in self.rows
        ):
            return _nc_unmeasured("nc is not supported on this host")

        async def run(cmd: str) -> "CommandResult":
            got = await _bounded(
                self.deadline, INVENTORY_TIMEOUT_S, _exec_only(host, cmd, user=self.user)
            )
            if got is not None:
                return got
            if self.deadline.spent:
                raise _BudgetSpent
            # This script's own bound ran out, not the survey's: the strategy
            # running it failed, and the walk goes on to the next one.
            return CommandResult(
                status=Status.Error,
                command=cmd,
                value=f"timed out after {INVENTORY_TIMEOUT_S:g}s",
                timed_out=True,
            )

        declared_port = _nc_declared(opts.port_strategy)
        declared_listener = _nc_declared(opts.listener_check)
        if declared_port is not None and declared_listener is not None:
            return NcRungs(declared_port, declared_listener)

        def unmeasured(note: str, failed: "list[NcFailedStrategy] | None" = None) -> NcRungs:
            """Report the ladders still to be measured as ``unmeasured``; a declared pin stays."""
            missing = NcLadderResult("unmeasured", note=note)
            return NcRungs(declared_port or missing, declared_listener or missing, failed or [])

        try:
            available = await available_rungs(run)
        except _BudgetSpent:
            return unmeasured(_NC_BUDGET_SPENT)
        except Exception as exc:  # noqa: BLE001 — a raw runner error must not lose the survey's rows
            return unmeasured(f"the check for each strategy's tool failed: {_nc_error(exc)}")
        failed: list[NcFailedStrategy] = []
        found: "int | None" = None
        if declared_port is None:
            try:
                port, found = await self._port_ladder(run, available, failed)
            except _BudgetSpent:
                return unmeasured(_NC_BUDGET_SPENT, failed)
        else:
            port = declared_port
        if declared_listener is not None:
            return NcRungs(port, declared_listener, failed)
        listener, left = await self._listener_ladder(run, available, failed, port, found)
        return NcRungs(port, listener, failed, left)

    async def _port_ladder(
        self, run: "_NcRunner", available: frozenset[str], failed: list[NcFailedStrategy]
    ) -> "tuple[NcLadderResult, int | None]":
        """Walk the port ladder in its order; the first strategy that works is measured.

        The scan starts where a transfer's does (:func:`scan_start`), so two
        probes of one host rarely open their listeners on the same port.

        Returns:
            What the ladder found, and the port the measured strategy returned
            (``None`` when none worked).
        """
        base_port = scan_start(self.host.nc_options.port)
        for rung in PORT_LADDER:
            if rung.name not in available:
                continue
            try:
                found = await run_port_rung(rung, run, base_port=base_port, reserved="")
            except _BudgetSpent:
                raise
            except Exception as exc:  # noqa: BLE001 — any runner error fails this strategy only
                failed.append(NcFailedStrategy("port", rung.name, _nc_error(exc)))
                continue
            return NcLadderResult("measured", rung.name), found
        note = _no_rung_note(available, PORT_LADDER, PORT_OPTION)
        return NcLadderResult("unmeasured", note=note), None

    async def _listener_ladder(
        self,
        run: "_NcRunner",
        available: frozenset[str],
        failed: list[NcFailedStrategy],
        port: NcLadderResult,
        found: "int | None",
    ) -> "tuple[NcLadderResult, str]":
        """Measure the listener checks against one listener the probe opens, counts and closes.

        The listener's port is *found* when the port ladder was walked; with
        a pinned port strategy, that strategy is asked once for one. The
        backend's ``_measure_listener_checks`` runs as one step bounded by what
        is left of the survey's budget, and what it recorded is the verdict
        (:meth:`_listener_verdict`).

        Args:
            run: The survey's runner for each control command.
            available: The strategies whose tool is on the host.
            failed: Where a failed check is named.
            port: What the port ladder found, or the declared pin.
            found: The port the port ladder's measured strategy returned.

        Returns:
            The listener ladder's result, and ``NcRungs.listener_left``.
        """
        checks = [rung for rung in LISTENER_LADDER if rung.name in available]
        if not checks:
            note = _no_rung_note(available, LISTENER_LADDER, LISTENER_OPTION)
            return NcLadderResult("unmeasured", note=note), ""
        if port.state == "unmeasured":
            return NcLadderResult("unmeasured", note=_nc_no_port(port.note)), ""
        try:
            backend: Any = self.host._nc_backend_for(self.user)  # noqa: SLF001 — the survey's own host
        except Exception as exc:  # noqa: BLE001 — a backend that cannot be built must not lose the survey's rows
            return NcLadderResult("unmeasured", note=_nc_no_backend(exc)), ""
        measure = getattr(backend, "_measure_listener_checks", None)
        if measure is None:
            return NcLadderResult("unmeasured", note=_NC_NO_MEASUREMENT), ""
        if found is None:
            found, why = await self._pinned_port(backend, run)
            if found is None:
                return NcLadderResult("unmeasured", note=why), ""
        cap_s = int(SURVEY_BUDGET_S)
        into = NcListenerMeasurement(found)
        cut = await self._measure(_finished(partial(measure, run, checks, into, cap_s=cap_s)), into)
        left = ""
        # A capped listener ends by itself within the cap, so there is nothing
        # for the user to do and nothing is said. Only one that may stay is named.
        if into.spawned and not into.end_confirmed and not into.capped:
            left = _nc_listener_left(found)
        if cut is not None:
            return NcLadderResult("unmeasured", note=cut), left
        return _listener_verdict(checks, into, failed), left

    async def _pinned_port(self, backend: Any, run: "_NcRunner") -> "tuple[int | None, str]":
        """Ask the pinned port strategy once, through *backend*, for the probe's port.

        Returns:
            The port and ``""``; or ``None`` and the note that says why there is none.
        """
        by_strategy = getattr(backend, "_port_by_strategy", None)
        if by_strategy is None:
            return None, _NC_NO_MEASUREMENT
        try:
            return int(await by_strategy(self.host.nc_options.port_strategy, run)), ""
        except _BudgetSpent:
            return None, _NC_BUDGET_SPENT
        except Exception as exc:  # noqa: BLE001 — the pinned strategy's error is the note
            return None, _nc_no_port(_nc_error(exc))

    async def _measure(
        self, step: "Coroutine[Any, Any, bool]", into: NcListenerMeasurement
    ) -> "str | None":
        """Run the measurement *step*, which fills *into*, bounded by what is left of the budget.

        Returns:
            ``None`` when the step finished; otherwise the note that says why
            the listener checks were not measured. A budget that ran out once
            the listener command was typed is worded as such, not as one that
            ran out before the step ran.
        """
        try:
            finished = await _bounded(self.deadline, self.deadline.remaining(), step)
        except _BudgetSpent:
            finished = False
        except Exception as exc:  # noqa: BLE001 — a raw runner error must not lose the survey's rows
            return _nc_runner_failed(exc)
        if finished:
            return None
        # Only a finished step returns True; with this grant, None is the budget.
        return _nc_budget_spent_measuring(into.port) if into.spawned else _NC_BUDGET_SPENT

    def _inventory_footnotes(self, inv: Inventory, *, elevation_refused: bool) -> None:
        """In the Interfaces order: owners-incomplete, loopback-dropped, then the failures."""
        host = self.host
        # A root session already sees every owner: there is nothing to elevate to.
        as_root = host._acts_as_root()  # noqa: SLF001 — the elevation seam's own identity rule
        if not inv.elevated and self.user is None and not as_root:
            self.survey.footnotes.append(
                f"owners incomplete: inventory ran as {host.current_user!r} without root; "
                f"run otto host {host.id} probe --user root for owner names"
            )
        if inv.loopback_dropped:
            self.survey.footnotes.append(
                f"{inv.loopback_dropped} loopback-only listeners not shown"
            )
        if elevation_refused:
            self.survey.footnotes.append("elevation refused: inventory fell back to a plain run")
        if inv.unparsed:
            self.survey.footnotes.append(
                f"inventory: unparsed output from {inv.tool}, {inv.unparsed} lines ignored"
            )

    def _declared_inventory_rows(self, inv: Inventory) -> None:
        how = f"inventory: {inv.tool}, {'elevated' if inv.elevated else 'unelevated'}"
        own_snmp = self._snmp_is_the_hosts_own_address()
        for c in self.cands:
            if not (c.kind == "term" or c.protocol in ("ftp", "snmp")):
                continue
            if c.protocol == "snmp" and not own_snmp:
                # Ruling 26: the endpoint is a relay, so this guest's socket
                # table is not evidence about it either way. The GET's own
                # verdict (snmp_tier) is the row.
                continue
            transport = "udp" if c.protocol == "snmp" else "tcp"
            hit = next(
                (lis for lis in inv.listeners if lis.transport == transport and lis.port == c.port),
                None,
            )
            if hit is None:
                self.rows.append(
                    _verdict(
                        c,
                        "closed",
                        "inventory",
                        self.session_vantage,
                        f"nothing bound on :{c.port} ({how})",
                    )
                )
            else:
                self.rows.append(
                    _verdict(
                        c,
                        "listening",
                        "inventory",
                        self.session_vantage,
                        f"listening on :{c.port}, owner {hit.owner or 'unknown'}",
                    )
                )

    async def discovery(self, inv: Inventory) -> None:
        declared_tcp = {c.port for c in self.cands if c.kind == "term" or c.protocol == "ftp"}
        snmp_port = self.survey.declared_ports["snmp"]
        for lis in inv.listeners:
            if lis.transport == "tcp":
                if lis.port not in declared_tcp:
                    await self._discover_tcp(lis.port, owner=lis.owner, seen_by_inventory=True)
            elif lis.port == snmp_port or protocol_for_owner(lis.owner) == "snmp":
                await self._discover_snmp(lis)
            else:
                self.survey.other_listeners.append(f"{lis.port}/udp {lis.owner or 'unknown'}")

    async def _discover_snmp(self, lis: "Listener") -> None:
        """Query an snmp-shaped UDP listener the inventory found on an UNDECLARED port.

        The declared endpoint has already been queried by :meth:`snmp_tier`,
        from the controller, so it is not queried again here. A second port is
        only worth a GET when the agent is the host's own address: a relay
        endpoint is reached at the relay, and this listener is inside the
        guest, so nothing connects the two.
        """
        target = _snmp_target(self.host)
        if lis.port == self.survey.declared_ports["snmp"] or target is None:
            return
        address, _p, community, version = target
        if address != self.host.ip:
            self.survey.other_listeners.append(f"{lis.port}/udp {lis.owner or 'unknown'}")
            return
        self.rows.append(
            await _snmp_row(
                lis.port, self.deadline, address=address, community=community, version=version
            )
        )

    async def _discover_tcp(
        self, port: int, *, owner: "str | None", seen_by_inventory: bool
    ) -> None:
        """Dial one undeclared port and report it as what the evidence supports.

        Who SAW the listener decides what a non-open dial means (Ruling 29,
        correcting Ruling 27's wording):

        * ``seen_by_inventory`` -- the host's own socket table already
          established that something is bound there, and the dial is a second,
          weaker observation from outside. A refusal is then a fact ABOUT THE
          PATH (a firewall, a bind on another interface), not evidence the
          listener is absent, so the entry stays and carries what the dial
          said. Dropping it would lose the only place an undeclared listener
          is ever reported.
        * an operator's ``--scan-ports`` extra -- nothing looked at the port
          but this dial. A refused connect then supports nothing at all, and
          ``other listeners: 9000/tcp unknown`` was a listener fabricated out
          of thin air; a budget-spent dial (``got is None``) listed a port the
          survey never contacted. Those stay unlisted.

        The embedded sweep keeps the same rule for the same reason: it has no
        inventory behind it, so ``open`` is all it can support.
        """
        got = await _bounded(
            self.deadline, DIAL_TIMEOUT_S + 1.0, dial_for(self.host, timeout=DIAL_TIMEOUT_S)(port)
        )
        if got is None or got.state != "open":
            if seen_by_inventory:
                said = "not dialed: survey budget exhausted" if got is None else got.state
                self.survey.other_listeners.append(
                    f"{port}/tcp {owner or 'unknown'} ({said} from {_vantage(self.host)})"
                )
            return
        if got.service not in _LOGIN_PROTOCOLS:
            has_banner = got.banner.strip()
            label = got.banner.strip().splitlines()[0][:40] if has_banner else (owner or "unknown")
            self.survey.other_listeners.append(f"{port}/tcp {label}")
            return
        if got.service in self.declared_supported:
            # The declared port already answered, so this one cannot become the
            # working port and is not logged into -- but the listener is real and
            # the dial named it, so it is reported rather than dropped.
            declared = self.survey.declared_ports.get(got.service, 0)
            self.rows.append(
                ProtocolVerdict(
                    protocol=got.service,
                    kind="term" if got.service in _LOGIN_TERMS else "transfer",
                    port=port,
                    state="listening",
                    tier="inventory",
                    vantage=self.session_vantage,
                    detail=(
                        f"listening on :{port}, owner {owner or 'unknown'}; "
                        f"not tried: {got.service} already supported on :{declared}"
                    ),
                )
            )
            return
        c = Candidate(
            got.service, "term" if got.service in _LOGIN_TERMS else "transfer", port, declared=False
        )
        out = await self._candidate_login(got.service, port)
        if got.service != "ftp":
            self._note_mapping(got.service, out)
        if out is None:
            self._budget_row(c, "login")
        else:
            self.rows.append(
                _verdict(
                    c,
                    out.state,
                    "login",
                    _vantage(self.host),
                    f"{out.detail}; banner {got.detail!r}",
                )
            )

    async def extra_dials(self) -> None:
        known = {v.port for v in self.rows}
        for port in self.extra_ports:
            if port not in known:
                await self._discover_tcp(port, owner=None, seen_by_inventory=False)


async def _survey_embedded(host: "RemoteHost", *, extra_ports: list[int]) -> Survey:
    deadline = _Deadline(SURVEY_BUDGET_S)
    survey = Survey()
    rows: list[ProtocolVerdict] = []
    console_port = host.telnet_options.port
    cands, survey.not_applicable = _family_candidates(
        "embedded",
        lambda n, _k: (
            console_port
            if n in ("telnet", "console")
            else (
                host.snmp.port if n == "snmp" and host.snmp is not None else DEFAULT_PORTS.get(n, 0)
            )
        ),
    )
    survey.declared_ports = {c.protocol: c.port for c in cands}
    vantage = _vantage(host)
    # Login tier: the host's OWN console -- never a second client on a single-client console.
    telnet_c = next(c for c in cands if c.protocol == "telnet")
    try:
        got = await _bounded(deadline, LOGIN_TIMEOUT_S, host.connections.telnet())
        outcome = LoginOutcome("supported", "console connected") if got is not None else None
    except Exception as exc:  # noqa: BLE001 — classified, never guessed
        outcome = classify_login_error(exc, who="console")
    if outcome is None:
        rows.append(_verdict(telnet_c, "timeout", "login", vantage, "survey budget exhausted"))
    else:
        rows.append(_verdict(telnet_c, outcome.state, "login", vantage, outcome.detail))
    for c in cands:
        if c.protocol == "console":
            state = outcome.state if outcome is not None else "timeout"
            if outcome is not None and outcome.state == "supported":
                detail = "the console session"
            else:
                detail = outcome.detail if outcome is not None else "survey budget exhausted"
            rows.append(_verdict(c, state, "session", vantage, detail))
        elif c.protocol == "tftp":
            rows.append(_verdict(c, "not-checkable", "session", vantage, _TFTP_REASON))
    # Inventory tier: the external sweep -- never the host's own console port.
    #
    # Ruling 27. The declared console's verdict is the login row above, from
    # the host's OWN connection, and login (tier 4) beats inventory (tier 2)
    # for every state -- so a sweep row on that port was discarded 100% of the
    # time. What it cost was real: one extra TCP client on a single-client
    # RTOS console while the survey's own client is attached, which is the
    # exact shape that has taken this lab's Zephyr consoles out for a guest's
    # lifetime (tests/firmware/zephyr/README.md, #260). Zero information for a
    # connect otto cannot afford.
    ports = [
        p
        for p in sweep_port_set(
            [console_port],
            [DEFAULT_PORTS["telnet"]],
            SWEEP_ALTERNATES["telnet"] + SSH_SWEEP_PORTS,
            extra_ports,
        )
        if p != console_port
    ]
    swept = await _bounded(
        deadline,
        SWEEP_DIAL_TIMEOUT_S * len(ports) + 2.0,
        sweep_ports(
            ports,
            dial_for(host, timeout=SWEEP_DIAL_TIMEOUT_S),
            concurrency=SWEEP_CONCURRENCY,
        ),
    )
    for row in swept or []:
        await _embedded_sweep_row(host, row, rows, survey, deadline)
    # snmp.
    snmp_c = next(c for c in cands if c.protocol == "snmp")
    target = _snmp_target(host)
    if target is None:
        rows.append(
            _verdict(
                snmp_c,
                "not-checkable",
                "dial",
                vantage,
                "snmp needs a declared relay endpoint behind a hop",
            )
        )
    else:
        address, port, community, version = target
        snmp_ports = [port] if host.snmp is not None else [port, *SWEEP_ALTERNATES["snmp"]]
        rows.extend(
            [
                await _snmp_row(p, deadline, address=address, community=community, version=version)
                for p in snmp_ports
            ]
        )
    survey.verdicts = merge_verdicts(rows)
    _resolve(survey)
    return survey


async def _embedded_sweep_row(
    host: "RemoteHost",
    row: SweepRow,
    rows: list[ProtocolVerdict],
    survey: Survey,
    deadline: _Deadline,
) -> None:
    """Turn one swept port into a row, a login, or nothing.

    The console port never reaches here (the sweep does not dial it), and a
    non-open dial is neither a verdict nor a listener on this family either
    (Ruling 27): the sweep states what it FOUND.
    """
    o = row.outcome
    vantage = _vantage(host)
    if o.state != "open":
        return
    if o.service == "telnet":
        login = await _bounded(
            deadline, LOGIN_TIMEOUT_S, attempt_console_open(host, row.port, timeout=LOGIN_TIMEOUT_S)
        )
        state, detail = (
            (login.state, login.detail)
            if login is not None
            else ("timeout", "survey budget exhausted")
        )
        rows.append(
            ProtocolVerdict(
                protocol="telnet",
                kind="term",
                port=row.port,
                state=state,
                tier="login",
                vantage=vantage,
                detail=detail,
            )
        )
        return
    label = o.banner.strip().splitlines()[0][:40] if o.banner.strip() else "unknown (no banner)"
    survey.other_listeners.append(f"{row.port}/tcp {label}")


def _local_not_applicable(survey: Survey) -> Survey:
    """Fill ``not_applicable`` with every registered protocol this survey did NOT answer for.

    Computed from the emitted rows rather than from the registries alone: the
    local arm states a ``shell`` verdict, and a protocol cannot be supported
    and not-applicable in the same report.
    """
    answered = {v.protocol for v in survey.verdicts}
    names = set(dialling_terms()) | set(TRANSFER_BACKENDS.names())
    survey.not_applicable = sorted(names - answered)
    return survey


async def _survey_local(host: "BaseHost") -> Survey:
    deadline = _Deadline(SURVEY_BUDGET_S)
    survey = Survey()
    budget = _shell_login_budget(host)
    got = await _bounded(deadline, budget, _open_own_session(host, budget))
    if got is None or got.state != "supported":
        survey.footnotes.append("no local session: inventory not run")
        return _local_not_applicable(survey)
    # The machine otto runs on reaches itself over its own shell, and that is
    # the one protocol row the local arm can state.
    survey.verdicts = [
        ProtocolVerdict(
            protocol="shell",
            kind="transfer",
            port=0,
            state="supported",
            tier="session",
            vantage="local",
            detail="the carrying session",
        )
    ]
    survey.supported = ["shell"]
    inv = await _bounded(
        deadline, INVENTORY_TIMEOUT_S + 1.0, run_inventory(partial(_run_only, host), elevate=False)
    )
    if inv is None or inv.error:
        survey.footnotes.append(
            inv.error if inv is not None else "inventory: survey budget exhausted before it ran"
        )
        return _local_not_applicable(survey)
    survey.other_listeners = [
        f"{lis.port}/{lis.transport} {lis.owner or 'unknown'}" for lis in inv.listeners
    ]
    if inv.loopback_dropped:
        survey.footnotes.append(f"{inv.loopback_dropped} loopback-only listeners not shown")
    if not inv.elevated:
        survey.footnotes.append(
            "owners incomplete: inventory ran as the invoking user without root"
        )
    return _local_not_applicable(survey)


def _survey_docker(_host_cls: type) -> Survey:
    survey = Survey()
    verdicts: list[ProtocolVerdict] = []
    for name, backend in dialling_terms().items():
        if "unix" in backend.host_families:
            verdicts.append(
                ProtocolVerdict(
                    protocol=name,
                    kind="term",
                    port=0,
                    state="not-checkable",
                    tier="dial",
                    vantage="controller",
                    detail=_DOCKER_REASON,
                )
            )
    for name, entry in TRANSFER_BACKENDS.raw_items():
        if "unix" in entry.metadata.host_families:
            verdicts.append(
                ProtocolVerdict(
                    protocol=name,
                    kind="transfer",
                    port=0,
                    state="not-checkable",
                    tier="dial",
                    vantage="controller",
                    detail=_DOCKER_REASON,
                )
            )
    survey.verdicts = merge_verdicts(verdicts)
    return survey


def _with_user(survey: Survey, user: "str | None") -> Survey:
    """Carry ``--user`` into the report on the arms that do not build it themselves."""
    survey.user = user
    return survey


async def run_survey(
    host: "BaseHost", *, user: "str | None" = None, scan_ports: "str | None" = None
) -> Survey:
    """Survey *host*: every registered protocol, on the port it really listens on."""
    extra = parse_scan_ports(scan_ports)
    from ..docker_host import DockerContainerHost
    from ..embedded_host import EmbeddedHost
    from ..local_host import LocalHost
    from ..unix_host import UnixHost

    if isinstance(host, EmbeddedHost):
        return _with_user(await _survey_embedded(host, extra_ports=extra), user)
    if isinstance(host, UnixHost):
        return await _UnixSurvey(host, user=user, extra_ports=extra).run()
    if isinstance(host, LocalHost):
        return _with_user(await _survey_local(host), user)
    if isinstance(host, DockerContainerHost):
        return _with_user(_survey_docker(type(host)), user)
    survey = Survey(user=user)
    reason = f"no survey for {type(host).__name__}"
    named: list[tuple[str, Kind]] = [(n, "term") for n in dialling_terms()]
    named.extend((n, "transfer") for n in TRANSFER_BACKENDS.names())
    for name, kind in named:
        survey.verdicts.append(
            ProtocolVerdict(
                protocol=name,
                kind=kind,
                port=0,
                state="not-checkable",
                tier="dial",
                vantage="controller",
                detail=reason,
            )
        )
    return survey
