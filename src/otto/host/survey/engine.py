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
from typing import TYPE_CHECKING, Any, TypeVar

from ...logger.mode import LogMode
from ..connections import TERM_BACKENDS, TermBackend
from ..login_proxy import cred_for, cred_identity
from ..transfer.registry import TRANSFER_BACKENDS
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
)
from .owners import DEFAULT_PORTS, SSH_SWEEP_PORTS, SWEEP_ALTERNATES, protocol_for_owner
from .snmp_check import check_snmp
from .sweep import SweepRow, parse_scan_ports, sweep_port_set, sweep_ports
from .verdict import (
    DIAL_TIMEOUT_S,
    INVENTORY_TIMEOUT_S,
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
    from ...result import CommandResult
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


async def _open_own_session(
    host: "BaseHost", timeout: float, *, who: "str | None" = None
) -> LoginOutcome:
    """Open the host's own persistent session; that opening is the declared term's login.

    *who* names the identity when ``--user`` forced one, so the row says the
    login was asked for rather than picked; otherwise the session's own cred.
    """
    who = who or _own_login_name(host)
    try:
        await asyncio.wait_for(host.run("true", timeout=timeout, log=LogMode.NEVER), timeout)
    except Exception as exc:  # noqa: BLE001 — every wire failure becomes a classified verdict
        return classify_login_error(exc, who=who)
    return LoginOutcome("supported", f"{who}: session opened")


async def _run_only(host: "BaseHost", cmd: str, *, sudo: bool = False) -> "CommandResult":
    return (await host.run(cmd, sudo=sudo, timeout=INVENTORY_TIMEOUT_S, log=LogMode.NEVER)).only


def dialling_terms() -> dict[str, TermBackend]:
    """Return registered term backends reached by dialling the HOST's own address.

    A console term is addressed at its console SERVER, not the host, so it has
    no port on the host's own address for a port survey to dial — every survey
    read of ``TERM_BACKENDS`` routes through here instead, so a term like it
    never manufactures a phantom candidate, dial or login attempt against the
    host.
    """
    return {n: b for n, b in TERM_BACKENDS.items() if b.dials_host}


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
    for name, cls in TRANSFER_BACKENDS.items():
        if family in cls.host_families:
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
    if name in ("scp", "sftp"):
        return host.ssh_options.port
    if name in ("shell", "nc"):
        return host.ssh_options.port if host.term == "ssh" else host.telnet_options.port
    return DEFAULT_PORTS.get(name, 0)


def _has_password(host: "UnixHost", mechanism: str) -> bool:
    target = host.current_user if mechanism == "sudo" else "root"
    cred = cred_for(host.creds, target, host.term)
    return cred is not None and cred.password is not None


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
            host.address_for(host.snmp.address or host.ip),
            host.snmp.port,
            host.snmp.community,
            host.snmp.version,
        )
    if host.hop is None:
        return (host.ip, DEFAULT_PORTS["snmp"], "public", "2c")
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
        if c.protocol == "ftp":
            return await _bounded(
                self.deadline,
                LOGIN_TIMEOUT_S,
                attempt_ftp_login(
                    self.host, c.port, login=self._login_for("ftp"), timeout=LOGIN_TIMEOUT_S
                ),
            )
        return await _bounded(
            self.deadline,
            LOGIN_TIMEOUT_S,
            attempt_term_login(
                self.host,
                c.protocol,
                c.port,
                login=self._login_for(c.protocol),
                timeout=LOGIN_TIMEOUT_S,
            ),
        )

    async def _own_session(self) -> "LoginOutcome | None":
        """Open the host's own session, switched to ``--user`` when it applies to the term.

        The session tier and the inventory both ride on THIS session, so when
        the operator named a login it has to be the one that opens -- and the
        row has to say the login was forced rather than picked.
        """
        host, user = self.host, self.user
        cred = cred_for(host.creds, user, host.term) if user is not None else None
        if cred is None or user is None:
            return await _bounded(
                self.deadline, LOGIN_TIMEOUT_S, _open_own_session(host, LOGIN_TIMEOUT_S)
            )
        who = f"login {cred_identity(cred.login, cred.protocols)!r} (--user)"
        async with host.as_user(user):
            return await _bounded(
                self.deadline, LOGIN_TIMEOUT_S, _open_own_session(host, LOGIN_TIMEOUT_S, who=who)
            )

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
        if not ul.is_settled(applet_capability(c.protocol)):
            return _verdict(
                c, "not-checkable", "userland", self.session_vantage, "userland probe unsettled"
            )
        if ul.has_applet(c.protocol) == APPLET_PRESENT:
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
            async with host.as_user(self.user):
                inv = await _bounded(
                    self.deadline, INVENTORY_TIMEOUT_S * 2 + 1.0, run_inventory(run, elevate=False)
                )
        else:
            mech = self.userland.elevation if self.userland is not None else "none"
            elevate = mech in ("sudo", "su") and _has_password(host, mech)
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

    def _inventory_footnotes(self, inv: Inventory, *, elevation_refused: bool) -> None:
        """In the Interfaces order: owners-incomplete, loopback-dropped, then the failures."""
        host = self.host
        if not inv.elevated and self.user is None:
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
        login = self._login_for(got.service)
        if got.service == "ftp":
            out = await _bounded(
                self.deadline,
                LOGIN_TIMEOUT_S,
                attempt_ftp_login(self.host, port, login=login, timeout=LOGIN_TIMEOUT_S),
            )
        else:
            out = await _bounded(
                self.deadline,
                LOGIN_TIMEOUT_S,
                attempt_term_login(
                    self.host, got.service, port, login=login, timeout=LOGIN_TIMEOUT_S
                ),
            )
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
    names = set(dialling_terms()) | {n for n, _ in TRANSFER_BACKENDS.items()}
    survey.not_applicable = sorted(names - answered)
    return survey


async def _survey_local(host: "BaseHost") -> Survey:
    deadline = _Deadline(SURVEY_BUDGET_S)
    survey = Survey()
    got = await _bounded(deadline, LOGIN_TIMEOUT_S, _open_own_session(host, LOGIN_TIMEOUT_S))
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
    for name, cls in TRANSFER_BACKENDS.items():
        if "unix" in cls.host_families:
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
    named.extend((n, "transfer") for n, _ in TRANSFER_BACKENDS.items())
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
