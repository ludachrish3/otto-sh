"""The engine: tiers in order, nothing guessed, budgets injected, resolution by the spec's rule."""

import asyncio
import gc
import warnings
from pathlib import Path

import pytest

from otto.host.command_frame import ZephyrFrame
from otto.host.element import Element
from otto.host.embedded_host import EmbeddedHost
from otto.host.errors import UnsupportedOnUserlandError
from otto.host.login_proxy import Cred
from otto.host.options import (
    ConsoleOptions,
    FtpOptions,
    NcOptions,
    SnmpOptions,
    SshOptions,
    TelnetOptions,
)
from otto.host.survey import engine
from otto.host.survey.dial import DialOutcome
from otto.host.survey.inventory import Inventory, Listener, parse_inventory
from otto.host.survey.login import LoginOutcome
from otto.host.survey.report import menu_pin
from otto.host.survey.sweep import SweepRow
from otto.host.survey.verdict import ProtocolVerdict
from otto.host.transfer import nc_scripts
from otto.host.transfer.nc import NcRungToolError, _ask_listener_check, run_port_rung
from otto.host.unix_host import UnixHost
from otto.logger.mode import LogMode
from otto.result import CommandResult
from otto.utils import Status

_FIX = Path(__file__).parent / "_fixtures"
_CREDS = [Cred(login="admin", password="pw")]


def _unix(**extra):
    extra.setdefault("ssh_options", SshOptions(port=1))
    extra.setdefault("telnet_options", TelnetOptions(port=1))
    extra.setdefault("ftp_options", FtpOptions(port=1))
    extra.setdefault("valid_terms", ["ssh", "telnet"])
    extra.setdefault("valid_transfers", ["scp", "sftp", "ftp", "nc", "shell"])
    extra.setdefault("transfer", extra["valid_transfers"][0])
    extra.setdefault("creds", list(_CREDS))
    return UnixHost(
        element=Element(name="u1"), name="u1", ip="127.0.0.1", log=LogMode.QUIET, **extra
    )


class _Userland:
    def __init__(self, *, elevation="sudo", scp="present", nc="present", settled=True):
        self.elevation, self._scp, self._nc, self._settled = elevation, scp, nc, settled
        self.resolved = False

    async def resolve(self):
        self.resolved = True

    def is_settled(self, *, name):
        return self._settled

    def has_applet(self, *, applet):
        return {"scp": self._scp, "nc": self._nc}[applet]


def _row(survey, protocol, port):
    return next(v for v in survey.verdicts if v.protocol == protocol and v.port == port)


async def _no_rung_exec(self, cmd, **kw):
    return CommandResult(status=Status.Success, value="", retcode=0)


@pytest.fixture
def seams(monkeypatch):
    """Scripted tiers. Each key is an engine seam; tests override what they need."""
    calls: dict[str, list] = {
        k: []
        for k in ("own", "term", "ftp", "inventory", "dial", "snmp", "sweep", "console", "sftp")
    }

    async def own(host, timeout, *, who=None):
        calls["own"].append(host.term)
        label = who if who is not None else "login 'admin'"
        return LoginOutcome("supported", f"{label}: session opened")

    async def term(host, protocol, port, *, login, timeout):
        calls["term"].append((protocol, port, login))
        return LoginOutcome("closed", "connection refused")

    async def ftp(host, port, *, login, timeout):
        calls["ftp"].append((port, login))
        return LoginOutcome("supported", "login 'admin': 230 logged in")

    async def inventory(run, *, elevate):
        calls["inventory"].append(elevate)
        return parse_inventory((_FIX / "ss_tulnp.txt").read_text(), elevated=elevate)

    async def dial(ip, port, *, timeout):
        calls["dial"].append((ip, port, timeout))
        return DialOutcome(state="open", service="ssh", banner="SSH-2.0-x\r\n", detail="SSH-2.0-x")

    async def snmp(**kw):
        calls["snmp"].append(kw["port"])
        return ProtocolVerdict(
            protocol="snmp",
            kind="monitor",
            port=kw["port"],
            state="supported",
            tier="dial",
            vantage=kw["vantage"],
            detail="sysUpTime 1 via community 'public'",
        )

    async def sftp(connections, *, host, attempted):
        calls["sftp"].append(host)
        return object()

    monkeypatch.setattr(engine, "_open_own_session", own)
    monkeypatch.setattr(engine, "attempt_term_login", term)
    monkeypatch.setattr(engine, "attempt_ftp_login", ftp)
    monkeypatch.setattr(engine, "run_inventory", inventory)
    monkeypatch.setattr(engine, "dial_direct", dial)
    monkeypatch.setattr(engine, "check_snmp", snmp)
    monkeypatch.setattr(engine, "open_sftp_or_attribute", sftp)
    monkeypatch.setattr(UnixHost, "userland", lambda self: _Userland())
    # The netcat strategy probe runs over `exec`; by default the host has no
    # strategy's tool, so a test about something else meets one footnote.
    monkeypatch.setattr(UnixHost, "exec", _no_rung_exec)
    return calls


@pytest.mark.asyncio
async def test_unix_declared_ports_get_login_inventory_and_session_rows(seams):
    """Mutation: skip the own-session seam and every session row turns no-session."""
    host = _unix(
        ssh_options=SshOptions(port=22),
        ftp_options=FtpOptions(port=21),
        telnet_options=TelnetOptions(port=23),
    )
    s = await engine.run_survey(host)
    assert seams["own"] == ["ssh"]
    assert seams["term"] == [("telnet", 23, None)]
    assert seams["ftp"] == [(21, None)]
    assert _row(s, "ssh", 22).state == "supported"
    assert _row(s, "ssh", 22).tier == "login"
    assert _row(s, "telnet", 23).state == "closed"
    assert _row(s, "ftp", 21).state == "supported"
    assert _row(s, "shell", 22).state == "supported"
    assert _row(s, "shell", 22).tier == "session"
    assert _row(s, "scp", 22).state == "supported"
    assert _row(s, "scp", 22).tier == "userland"
    assert _row(s, "sftp", 22).state == "supported"
    assert _row(s, "nc", 22).state == "supported"
    assert "tftp" in s.not_applicable
    assert "console" in s.not_applicable
    assert s.supported == ["ftp", "nc", "scp", "sftp", "shell", "snmp", "ssh"]
    assert s.working_ports == {}


@pytest.mark.asyncio
async def test_a_term_with_dials_host_false_produces_no_candidate_dial_or_login(seams):
    """A term not reached by dialling the host (like console) is invisible to the survey.

    Registers a throwaway term backend with ``dials_host=False`` on the unix
    family and proves ``run_survey`` manufactures nothing for it: no
    candidate (so no declared port), no dial, no login attempt, and no row.
    """
    from otto.host import connections as conn_mod
    from otto.host.connections import ConnectionManager, register_term_backend

    class _Quiet(ConnectionManager):
        pass

    register_term_backend(
        "quiet-console",
        _Quiet,
        host_families=frozenset({"unix"}),
        authenticates=True,
        dials_host=False,
    )
    try:
        host = _unix(
            ssh_options=SshOptions(port=22),
            ftp_options=FtpOptions(port=21),
            telnet_options=TelnetOptions(port=23),
        )
        s = await engine.run_survey(host)
    finally:
        conn_mod.TERM_BACKENDS.unregister(name="quiet-console")

    assert "quiet-console" not in s.declared_ports
    assert not any(v.protocol == "quiet-console" for v in s.verdicts)
    assert not any(protocol == "quiet-console" for protocol, _port, _login in seams["term"])


@pytest.mark.asyncio
async def test_a_console_own_term_still_opens_its_session_with_no_dial_and_no_port_row(seams):
    """``dials_host=False`` excludes a term from the candidate walk -- but not from OWNING

    the session when it is the host's own term. Before this, a console-term
    host could never open its own session at all: no candidate meant
    ``declared_logins`` never matched ``c.protocol == self.host.term``, so
    ``own_ok`` stayed False and the session/inventory tiers never ran.
    """
    host = _unix(
        term="console",
        valid_terms=["console"],
        console_options=ConsoleOptions(server="test1", port=4001),
    )
    s = await engine.run_survey(host)
    assert seams["own"] == ["console"]
    assert "console" not in s.declared_ports
    assert not any(v.protocol == "console" and v.tier == "dial" for v in s.verdicts)
    row = _row(s, "console", 0)
    assert row.state == "supported"
    assert row.tier == "login"
    assert seams["inventory"], "own_ok=True must still run the inventory tier"
    assert not any("no term logged in" in f for f in s.footnotes)


@pytest.mark.asyncio
async def test_a_console_own_terms_row_never_becomes_a_supported_port_or_a_pin(seams):
    """The synthetic own-term row must not leak into `_resolve` or `menu_pin`.

    `console` is also a registered (embedded-only) transfer backend name, so
    treating its own-session row like any dialled candidate would falsely
    pin ``valid_transfers`` and a ``console_options`` port fragment for a
    port (0) that was never really surveyed.
    """
    host = _unix(
        term="console",
        valid_terms=["console"],
        # sftp needs ssh regardless of term, so under a console term its row
        # reads `no-session`. That is an unknown and pins nothing; it is left
        # out of valid_transfers only to keep this menu to what the survey
        # can confirm.
        valid_transfers=["scp", "ftp", "nc", "shell"],
        console_options=ConsoleOptions(server="test1", port=4001),
    )
    s = await engine.run_survey(host)
    assert "console" not in s.supported
    assert "console" not in s.working_ports
    row = _row(s, "console", 0)
    assert row.state == "supported"
    assert row.tier == "login"
    pin = menu_pin(host.valid_terms, host.valid_transfers, s)
    assert not any(line.startswith("valid_transfers") for line in pin)
    assert not any(line.startswith('"console_options"') for line in pin)


@pytest.mark.asyncio
async def test_no_session_detail_names_a_non_dialling_own_term_that_failed(seams, monkeypatch):
    """A non-dialling own term (console) has no candidate, so it is never in

    ``self.cands`` -- append it explicitly to the tried list or a
    console-term host's own-session failure reads as though console itself
    was never attempted.
    """

    async def own(host, timeout, *, who=None):
        return LoginOutcome("closed", "connection refused")

    monkeypatch.setattr(engine, "_open_own_session", own)
    host = _unix(
        term="console",
        valid_terms=["console"],
        console_options=ConsoleOptions(server="test1", port=4001),
    )
    s = await engine.run_survey(host)
    row = _row(s, "shell", 1)
    assert row.state == "no-session"
    assert row.detail == "needs a term session; tried ssh, telnet, console"


@pytest.mark.asyncio
async def test_a_discovered_ssh_port_is_dialed_then_logged_into_and_becomes_the_working_port(
    seams, monkeypatch
):
    """Declared ssh 22 refused, inventory shows sshd on 2222 only → login on 2222 → pin fragment.

    The carrying term is telnet and it logs in, because spec 7 step 2 only
    runs the inventory when the host's OWN session opened (Ruling 16); ssh is
    the non-own term whose declared port is dead, which is what makes 2222
    worth logging into.
    """

    async def inventory(run, *, elevate):
        return Inventory(
            tool="ss", elevated=True, listeners=[Listener("tcp", "0.0.0.0", 2222, "sshd")]
        )

    async def term(host, protocol, port, *, login, timeout):
        seams["term"].append((protocol, port, login))
        return (
            LoginOutcome("supported", "login 'admin': session opened")
            if port == 2222
            else LoginOutcome("closed", "refused")
        )

    monkeypatch.setattr(engine, "run_inventory", inventory)
    monkeypatch.setattr(engine, "attempt_term_login", term)
    host = _unix(
        ssh_options=SshOptions(port=22),
        telnet_options=TelnetOptions(port=23),
        valid_terms=["telnet", "ssh"],
        term="telnet",
        valid_transfers=["scp"],
    )
    s = await engine.run_survey(host)
    assert seams["dial"] == [
        ("127.0.0.1", 22, engine.DIAL_TIMEOUT_S),  # the declared ssh port, before its login
        ("127.0.0.1", 1, engine.DIAL_TIMEOUT_S),  # and the declared ftp port
        ("127.0.0.1", 2222, engine.DIAL_TIMEOUT_S),
    ]
    assert ("ssh", 2222, None) in seams["term"]
    assert _row(s, "ssh", 22).state == "closed"
    assert _row(s, "ssh", 2222).state == "supported"
    assert s.working_ports == {"ssh": 2222}


@pytest.mark.asyncio
async def test_a_discovered_port_for_an_already_supported_protocol_is_listed_not_logged_into(seams):
    """Ruling 15: the dial's answer is kept as a `listening` row; only the login is skipped.

    Mutation: drop the row and `otto host u1 probe` says nothing at all about
    the second sshd, which is the working-but-undeclared evidence discovery
    exists to surface.
    """
    s = await engine.run_survey(_unix(ssh_options=SshOptions(port=22)))
    row = _row(s, "ssh", 2222)
    assert row.state == "listening"
    assert row.tier == "inventory"
    assert row.detail == ("listening on :2222, owner sshd; not tried: ssh already supported on :22")
    assert ("ssh", 2222, None) not in seams["term"]
    assert "ssh" not in s.working_ports


@pytest.mark.asyncio
async def test_a_closed_own_port_runs_no_inventory_and_says_so(seams, monkeypatch):
    """Ruling 16 / spec 7 step 2: no own session, no inventory — whatever shape the failure took.

    Mutation: run the inventory anyway and the survey burns the budget on a
    dead session, replacing this line with `inventory failed: ...`.
    """

    async def own(host, timeout, *, who=None):
        return LoginOutcome("closed", "connection refused")

    monkeypatch.setattr(engine, "_open_own_session", own)
    s = await engine.run_survey(_unix(valid_terms=["ssh"], valid_transfers=["scp", "shell"]))
    assert seams["inventory"] == []
    # The declared ports are still dialed; what must not happen is discovery,
    # which only the inventory can feed.
    assert 2222 not in [port for _ip, port, _timeout in seams["dial"]]
    assert _row(s, "shell", 1).state == "no-session"
    assert "no term logged in: inventory not run" in s.footnotes


@pytest.mark.asyncio
async def test_user_opens_the_own_session_switched_and_marks_the_row(seams, monkeypatch):
    """Ruling 17: --user applies to the host's own term too, and the row says it was forced."""
    entered, asked = [], []

    class _AsUser:
        def __init__(self, user):
            entered.append(user)

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

    async def own(host, timeout, *, who=None):
        asked.append(who)
        return LoginOutcome("supported", f"{who}: session opened")

    monkeypatch.setattr(UnixHost, "as_user", lambda self, user="root", password=None: _AsUser(user))
    monkeypatch.setattr(engine, "_open_own_session", own)
    host = _unix(creds=[Cred(login="admin", password="pw"), Cred(login="root", password="r")])
    s = await engine.run_survey(host, user="root")
    assert asked == ["login 'root' (--user)"]
    assert entered[0] == "root"
    assert _row(s, "ssh", 1).detail == "login 'root' (--user): session opened"


@pytest.mark.asyncio
async def test_a_user_with_no_cred_for_the_own_term_opens_it_unswitched(seams, monkeypatch):
    """The other half of Ruling 17: a cred scoped away from the term leaves the session alone."""
    entered = []

    class _AsUser:
        def __init__(self, user):
            entered.append(user)

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

    monkeypatch.setattr(UnixHost, "as_user", lambda self, user="root", password=None: _AsUser(user))
    host = _unix(
        creds=[
            Cred(login="admin", password="pw"),
            Cred(login="ftpuser", password="f", protocols=["ftp"]),
        ]
    )
    s = await engine.run_survey(host, user="ftpuser")
    assert entered == ["ftpuser"]  # the inventory only, never the own session
    assert _row(s, "ssh", 1).detail == "login 'admin': session opened"


@pytest.mark.asyncio
async def test_a_term_that_logged_in_is_named_when_the_own_term_did_not(seams, monkeypatch):
    """Ruling 18: no silent fallback — the row names the term that worked and the fix."""

    async def own(host, timeout, *, who=None):
        return LoginOutcome("login-failed", "login 'admin': permission denied")

    async def term(host, protocol, port, *, login, timeout):
        seams["term"].append((protocol, port, login))
        return LoginOutcome("supported", "login 'admin': session opened")

    monkeypatch.setattr(engine, "_open_own_session", own)
    monkeypatch.setattr(engine, "attempt_term_login", term)
    s = await engine.run_survey(_unix(valid_transfers=["scp", "shell"]))
    assert _row(s, "shell", 1).state == "no-session"
    assert _row(s, "shell", 1).detail == (
        "needs the host's own term (ssh); telnet logged in — set term=telnet to use it"
    )


@pytest.mark.asyncio
async def test_no_term_logs_in_means_no_session_rows_and_no_inventory(seams, monkeypatch):
    """Mutation: run the inventory anyway and `calls['inventory']` is non-empty."""

    async def own(host, timeout):
        return LoginOutcome("login-failed", "login 'admin': permission denied")

    monkeypatch.setattr(engine, "_open_own_session", own)
    host = _unix(valid_terms=["ssh"], valid_transfers=["scp", "shell"])
    s = await engine.run_survey(host)
    assert seams["inventory"] == []
    assert _row(s, "shell", 1).state == "no-session"
    assert "tried ssh" in _row(s, "shell", 1).detail
    assert "no term logged in: inventory not run" in s.footnotes


@pytest.mark.asyncio
async def test_an_unelevated_inventory_prints_the_owners_incomplete_notice(seams, monkeypatch):
    monkeypatch.setattr(UnixHost, "userland", lambda self: _Userland(elevation="none"))
    s = await engine.run_survey(_unix())
    assert seams["inventory"] == [False]
    prefix = (
        "owners incomplete: inventory ran as 'admin' without root; "
        "run otto host u1 probe --user root"
    )
    assert any(f.startswith(prefix) for f in s.footnotes)


@pytest.mark.parametrize(("elevation", "elevated"), [("su", True), ("none", False)])
@pytest.mark.asyncio
async def test_a_root_login_gets_no_owners_incomplete_notice(
    seams, monkeypatch, elevation, elevated
):
    """Root already sees every owner, so "ran without root" would be false.

    su-only is the shape where a root login elevates with no answer armed (su
    never asks root); ``none`` is the shape where nothing can elevate, so the
    inventory runs unelevated as root. Mutation: drop the root check and the
    ``none`` case prints the notice. The non-root half is
    ``test_an_unelevated_inventory_prints_the_owners_incomplete_notice``.
    """
    monkeypatch.setattr(UnixHost, "userland", lambda self: _Userland(elevation=elevation))
    host = _unix(creds=[Cred(login="root", password="r")])
    assert host.current_user == "root"
    s = await engine.run_survey(host)
    assert seams["inventory"] == [elevated]
    assert not any(f.startswith("owners incomplete") for f in s.footnotes), s.footnotes


@pytest.mark.asyncio
async def test_elevation_is_requested_only_with_a_mechanism_and_a_password(seams, monkeypatch):
    """Mutation: elevate whenever sudo exists, even a passwordless cred otto cannot use."""
    host = _unix(creds=[Cred(login="admin")])
    await engine.run_survey(host)
    assert seams["inventory"] == [False]
    seams["inventory"].clear()
    await engine.run_survey(_unix())
    assert seams["inventory"] == [True]


@pytest.mark.parametrize(
    ("elevation", "creds", "arms_answer", "expected"),
    [
        ("sudo", [Cred(login="admin", password="pw")], True, True),
        ("sudo", [Cred(login="admin")], False, False),
        ("su", [Cred(login="admin"), Cred(login="root", password="r")], True, True),
        ("su", [Cred(login="admin", password="pw")], False, False),
        # su never asks root: a root login arms no answer yet IS elevated.
        ("su", [Cred(login="root", password="r")], False, True),
        ("su", [Cred(login="root")], False, True),
        ("sudo", [Cred(login="root")], False, True),
    ],
    ids=[
        "sudo-pw",
        "sudo-no-pw",
        "su-root-pw",
        "su-no-root-cred",
        "su-as-root",
        "su-as-root-no-pw",
        "sudo-as-root-no-pw",
    ],
)
@pytest.mark.asyncio
async def test_the_inventory_elevates_exactly_when_the_session_can_elevate(
    seams, monkeypatch, elevation, creds, arms_answer, expected
):
    """One decision: the survey elevates when ``_can_elevate`` says so, and only then.

    The survey and the privilege layer are asked the same input. A non-root
    session can elevate exactly when ``_elevate`` arms an answer; a root
    session can elevate with no answer at all. Mutation: restate the decision
    in the engine as "``_elevation_password`` is not None" (what a root su
    session with ``elevation: su`` lost elevation to) and the ``*-as-root``
    su cases go red.
    """
    userland = _Userland(elevation=elevation)
    monkeypatch.setattr(UnixHost, "userland", lambda self: userland)
    monkeypatch.setattr(UnixHost, "_userland", lambda self: userland)
    host = _unix(creds=creds)
    assert bool(host._elevate("id").expects) is arms_answer
    assert host._can_elevate(elevation) is expected
    await engine.run_survey(host)
    assert seams["inventory"] == [expected]


@pytest.mark.asyncio
async def test_user_runs_the_inventory_in_a_switched_session_without_sudo(seams, monkeypatch):
    entered = []

    class _AsUser:
        def __init__(self, user, password=None):
            entered.append(user)

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

    monkeypatch.setattr(
        UnixHost, "as_user", lambda self, user="root", password=None: _AsUser(user, password)
    )
    s = await engine.run_survey(
        _unix(creds=[Cred(login="admin", password="pw"), Cred(login="root", password="r")]),
        user="root",
    )
    assert entered == ["root", "root"]
    assert seams["inventory"] == [False]
    assert not any(f.startswith("owners incomplete") for f in s.footnotes)
    assert s.user == "root"


@pytest.mark.asyncio
async def test_user_applies_only_to_the_protocols_its_cred_is_scoped_to(seams, monkeypatch):
    """Ruling 12/spec 4.2: --user replaces the pick only where its cred applies."""

    class _AsUser:
        def __init__(self, user, password=None):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

    monkeypatch.setattr(
        UnixHost, "as_user", lambda self, user="root", password=None: _AsUser(user, password)
    )
    host = _unix(
        creds=[
            Cred(login="admin", password="pw"),
            Cred(login="ftpuser", password="fp", protocols=["ftp"]),
        ],
        valid_terms=["telnet", "ssh"],
        term="telnet",
        valid_transfers=["ftp"],
        ssh_options=SshOptions(port=22),
        telnet_options=TelnetOptions(port=23),
        ftp_options=FtpOptions(port=21),
    )
    await engine.run_survey(host, user="ftpuser")
    assert ("ssh", 22, None) in seams["term"]
    assert (21, "ftpuser") in seams["ftp"]


@pytest.mark.asyncio
async def test_a_userland_elevation_refusal_propagates_out_of_run_survey(seams, monkeypatch):
    """The engine turns it into no row and no footnote: it leaves ``run_survey`` as an error."""

    async def inventory(run, *, elevate):
        raise UnsupportedOnUserlandError("no sudo and no su on this userland")

    monkeypatch.setattr(engine, "run_inventory", inventory)
    with pytest.raises(UnsupportedOnUserlandError):
        await engine.run_survey(_unix())


@pytest.mark.asyncio
async def test_other_listeners_and_loopback_land_in_footnotes_not_rows(seams):
    s = await engine.run_survey(
        _unix(ssh_options=SshOptions(port=22), ftp_options=FtpOptions(port=21))
    )
    assert "2 loopback-only listeners not shown" in s.footnotes
    assert s.other_listeners == []  # every fixture listener is a declared port or a discovered ssh
    assert seams["snmp"] == [
        161
    ]  # udp :161 owner snmpd → the snmp check on the declared default port


@pytest.mark.asyncio
async def test_two_supported_undeclared_ports_produce_no_pin_and_a_footnote(seams, monkeypatch):
    async def inventory(run, *, elevate):
        return Inventory(
            tool="ss",
            elevated=True,
            listeners=[
                Listener("tcp", "0.0.0.0", 2222, "sshd"),
                Listener("tcp", "0.0.0.0", 2223, "sshd"),
            ],
        )

    async def term(host, protocol, port, *, login, timeout):
        return (
            LoginOutcome("supported", "ok")
            if port in (2222, 2223)
            else LoginOutcome("closed", "refused")
        )

    monkeypatch.setattr(engine, "run_inventory", inventory)
    monkeypatch.setattr(engine, "attempt_term_login", term)
    s = await engine.run_survey(
        _unix(
            ssh_options=SshOptions(port=22),
            telnet_options=TelnetOptions(port=23),
            valid_terms=["telnet", "ssh"],
            term="telnet",
            valid_transfers=["shell"],
        )
    )
    assert s.working_ports == {}
    assert "ssh: supported on 2222 and 2223; choose one in ssh_options.port" in s.footnotes


@pytest.mark.asyncio
async def test_a_spent_budget_marks_the_rest_timeout_and_runs_nothing_further(seams, monkeypatch):
    """Mutation: inherit the 90 s budget and this test cannot finish."""
    monkeypatch.setattr(engine, "SURVEY_BUDGET_S", 0.05)

    async def own(host, timeout):
        await asyncio.sleep(0.2)
        return LoginOutcome("supported", "late")

    monkeypatch.setattr(engine, "_open_own_session", own)
    s = await engine.run_survey(_unix(valid_terms=["ssh", "telnet"], valid_transfers=["shell"]))
    assert seams["term"] == []
    assert seams["inventory"] == []
    assert all(v.state == "timeout" for v in s.verdicts if v.protocol in ("ssh", "telnet"))
    assert any("survey budget exhausted" in v.detail for v in s.verdicts)


@pytest.mark.asyncio
async def test_a_scan_port_the_dial_refused_is_not_an_other_listener(seams, monkeypatch):
    """Ruling 27, as narrowed by Ruling 29: an OPEN dial, or an inventory witness.

    `--scan-ports 9000` on a unix host goes to the dial tier, and nothing
    else ever looked at 9000 -- no inventory row, no owner, no banner. Listing
    it as `other listeners: 9000/tcp unknown` off a REFUSED connect is a
    listener fabricated out of nothing anyone observed (mutation: restore the
    old non-open branch and the entry comes back).
    """

    async def refused(ip, port, *, timeout):
        seams["dial"].append((ip, port, timeout))
        return DialOutcome(state="closed", detail="connection refused")

    monkeypatch.setattr(engine, "dial_direct", refused)
    s = await engine.run_survey(
        _unix(ssh_options=SshOptions(port=22), ftp_options=FtpOptions(port=21)),
        scan_ports="9000",
    )
    assert (("127.0.0.1", 9000, engine.DIAL_TIMEOUT_S)) in seams["dial"]
    assert not any(e.startswith("9000/") for e in s.other_listeners), s.other_listeners
    assert not any(v.port == 9000 for v in s.verdicts), s.verdicts
    # The contrast that makes the rule a rule (Ruling 29): the SAME refused
    # dial on a port the INVENTORY saw does stay, annotated. The operator's
    # extra has no such witness, and that is the whole difference.
    assert "2222/tcp sshd (closed from controller)" in s.other_listeners, s.other_listeners


@pytest.mark.asyncio
async def test_a_listener_the_inventory_saw_is_listed_whatever_its_dial_says(seams, monkeypatch):
    """Ruling 29: who SAW the listener decides what a refused dial means.

    The fixture's inventory reports an undeclared tcp listener; the dial from
    outside refuses it. The host's own socket table has already established
    that something is bound there, so the refusal is a fact about the PATH — a
    firewall, a bind on another interface — not evidence the listener is
    absent. `other_listeners` is the only place an undeclared listener is ever
    reported, so dropping it loses the observation entirely (mutation: return
    early for every non-open dial and the entry disappears).

    The entry carries what the dial said, and from where, so the reader is
    never left to assume the port was reachable.
    """

    async def refused(ip, port, *, timeout):
        seams["dial"].append((ip, port, timeout))
        return DialOutcome(state="closed", detail="connection refused")

    monkeypatch.setattr(engine, "dial_direct", refused)
    s = await engine.run_survey(
        _unix(ssh_options=SshOptions(port=1), ftp_options=FtpOptions(port=2))
    )
    # ss_tulnp.txt's undeclared tcp listeners, none of which is a declared port here.
    assert s.other_listeners, s.other_listeners
    assert all(
        entry.endswith("(closed from controller)") for entry in s.other_listeners if "/tcp" in entry
    ), s.other_listeners


@pytest.mark.asyncio
async def test_a_budget_spent_discovery_dial_lists_no_listener(seams, monkeypatch):
    """A dial the budget never let run cannot report a listener either.

    `_bounded` answers `None` on a spent budget, and the old branch turned
    that `None` into `other listeners: 9000/tcp unknown` -- a listener
    reported for a port the survey never contacted at all. The real budget is
    used (`SURVEY_BUDGET_S`), not a stubbed `_bounded`, so the `None` arrives
    the way it does in production.
    """
    monkeypatch.setattr(engine, "SURVEY_BUDGET_S", 0.0)
    s = await engine.run_survey(_unix(), scan_ports="9000")
    assert seams["dial"] == [], seams["dial"]
    assert s.other_listeners == [], s.other_listeners


@pytest.mark.asyncio
async def test_bad_scan_ports_is_refused_before_any_contact(seams):
    with pytest.raises(ValueError, match="--scan-ports"):
        await engine.run_survey(_unix(), scan_ports="70000")
    assert seams["own"] == []


@pytest.mark.asyncio
async def test_docker_rows_are_not_checkable_with_the_stated_reason():
    from otto.host.docker_host import DockerContainerHost

    host = DockerContainerHost.__new__(
        DockerContainerHost
    )  # the survey reads only the class; nothing is contacted
    s = await engine.run_survey(host)
    assert s.verdicts
    assert all(v.state == "not-checkable" for v in s.verdicts)
    assert all(
        v.detail == "containers have no generic probe; docker exec is the only transport"
        for v in s.verdicts
    )


@pytest.mark.asyncio
async def test_local_states_its_shell_row_and_never_calls_it_not_applicable(monkeypatch):
    """The machine otto runs on reaches itself over its own shell; listeners are a footnote.

    Mutation: build `not_applicable` from the registries alone and `shell`
    appears as supported AND as not-applicable in the same report.
    """
    from otto.host.local_host import LocalHost

    async def own(host, timeout, *, who=None):
        return LoginOutcome("supported", "login <local>: session opened")

    async def inventory(run, *, elevate):
        return Inventory(
            tool="ss",
            elevated=False,
            listeners=[
                Listener("tcp", "0.0.0.0", 8080, "nginx"),
                Listener("udp", "0.0.0.0", 161, None),
            ],
            loopback_dropped=1,
        )

    monkeypatch.setattr(engine, "_open_own_session", own)
    monkeypatch.setattr(engine, "run_inventory", inventory)
    s = await engine.run_survey(LocalHost())
    assert s.supported == ["shell"]
    assert "shell" not in s.not_applicable
    assert "ssh" in s.not_applicable  # the network backends really are not applicable
    row = _row(s, "shell", 0)
    assert row.state == "supported"
    assert row.tier == "session"
    assert row.vantage == "local"
    assert s.other_listeners == ["8080/tcp nginx", "161/udp unknown"]
    assert "1 loopback-only listeners not shown" in s.footnotes


@pytest.mark.asyncio
async def test_a_dead_declared_ftp_port_behind_a_hop_is_closed_and_never_logged_into(
    seams, monkeypatch
):
    """Ruling 21: a declared candidate is dialed first, so a dead port cannot read login-failed.

    Over a hop nothing of otto's touches the socket -- the connect is a
    forward the hop performs -- so aioftp sees an EOF behind a local end that
    connected fine and the classifier's ConnectionError arm calls it a refused
    password. The dial is what tells "nothing is listening" from "the password
    was wrong", and it also keeps otto off a dead port entirely.

    Mutation: drop the pre-dial and the ftp seam is called, producing
    `login-failed` with an empty reason for a port with nothing behind it.
    """

    async def via_hop(run, hop_id, ip, port, *, timeout, userland):
        seams["dial"].append((ip, port, timeout))
        return DialOutcome(state="closed", detail="connection refused (from hop test4)")

    async def own(host, timeout, *, who=None):
        # No own session, so no inventory: its declared row for :21 would say
        # `closed` too, from the stronger `inventory` tier, and mask the dial's.
        return LoginOutcome("closed", "connection refused (via hop)")

    monkeypatch.setattr(engine, "_open_own_session", own)
    monkeypatch.setattr(engine, "dial_via_hop", via_hop)
    monkeypatch.setattr(UnixHost, "hop_host", lambda self: self)
    host = _unix(
        hop="test4",
        ssh_options=SshOptions(port=22),
        telnet_options=TelnetOptions(port=23),
        ftp_options=FtpOptions(port=21),
    )
    s = await engine.run_survey(host)
    row = _row(s, "ftp", 21)
    assert row.state == "closed"
    assert row.tier == "dial"
    assert row.vantage == "hop:test4"
    assert row.detail == "connection refused (from hop test4)"
    assert seams["ftp"] == []  # no login is tried on a port the dial did not find open
    assert seams["term"] == []  # nor on the non-own telnet, refused the same way


@pytest.mark.asyncio
async def test_an_open_declared_port_still_goes_to_the_login_tier(seams):
    """The other half of Ruling 21: an `open` dial changes nothing about the login."""
    host = _unix(
        ssh_options=SshOptions(port=22),
        telnet_options=TelnetOptions(port=23),
        valid_terms=["telnet", "ssh"],
        term="telnet",
        valid_transfers=["ftp"],
        ftp_options=FtpOptions(port=21),
    )
    s = await engine.run_survey(host)
    assert ("127.0.0.1", 22, engine.DIAL_TIMEOUT_S) in seams["dial"]
    assert ("ssh", 22, None) in seams["term"]
    assert (21, None) in seams["ftp"]
    assert _row(s, "ssh", 22).tier == "login"  # the login row outranks the dial


@pytest.mark.asyncio
async def test_the_declared_snmp_candidate_is_never_dialed_over_tcp(seams):
    """snmp is UDP: `check_snmp` is its check, and a TCP connect to :161 would prove nothing."""
    s = await engine.run_survey(
        _unix(ssh_options=SshOptions(port=22), ftp_options=FtpOptions(port=21))
    )
    assert all(port != 161 for _ip, port, _timeout in seams["dial"])
    assert seams["snmp"] == [161]
    assert _row(s, "snmp", 161).state == "supported"


@pytest.mark.asyncio
async def test_the_snmp_row_reads_controller_even_behind_a_hop(seams, monkeypatch):
    """Ruling 24: pysnmp GETs from wherever otto runs, never through a hop session.

    Unlike a login or a dial, the snmp check is never routed over the hop's
    session -- ``check_snmp`` calls ``otto.snmp.snmp_get`` directly, a local
    UDP call -- so its row must read ``vantage="controller"`` even when every
    other row on this same hopped host reads ``hop:test4``.

    Mutation: put ``vantage=_vantage(host)`` back inside ``_snmp_row`` and
    this fails with ``vantage == "hop:test4"`` instead.
    """

    async def via_hop(run, hop_id, ip, port, *, timeout, userland):
        seams["dial"].append((ip, port, timeout))
        return DialOutcome(state="open", service=None, banner="", detail="open")

    monkeypatch.setattr(engine, "dial_via_hop", via_hop)
    monkeypatch.setattr(UnixHost, "hop_host", lambda self: self)
    host = _unix(
        hop="test4",
        ssh_options=SshOptions(port=22),
        telnet_options=TelnetOptions(port=23),
        ftp_options=FtpOptions(port=21),
        snmp=SnmpOptions(port=1161),
    )
    s = await engine.run_survey(host)
    # 161 is the fixture's discovered udp listener (owner snmpd); 1161 is the
    # DECLARED endpoint, which Ruling 26 queries unconditionally -- before
    # that, a declared port with nothing bound inside the guest was never
    # asked at all.
    assert seams["snmp"] == [161, 1161], seams["snmp"]
    row = _row(s, "snmp", 161)
    assert row.state == "supported"
    assert row.vantage == "controller"
    assert _row(s, "telnet", 23).vantage == "hop:test4"  # the contrast: a real hop-routed row


@pytest.mark.asyncio
async def test_a_dial_that_could_not_run_does_not_block_the_login(seams, monkeypatch):
    """Ruling 25: `not-checkable` is not an answer, so it must not end the candidate.

    Behind a hop with no dial tool — a BusyBox build without `nc`, or with an
    `nc` that has no `-z` — every non-own term and ftp read
    `not-checkable: hop <id> offers no dial tool` and was never logged into,
    losing the survey's authoritative tier to a tool gap on the HOP rather
    than to anything about the host. The login now proceeds and its own
    outcome is the row (mutation: end on `not-checkable` again and both rows
    go back to tier `dial`).
    """

    async def unusable(ip, port, *, timeout):
        seams["dial"].append((ip, port, timeout))
        return DialOutcome(state="not-checkable", detail="hop carrot offers no dial tool")

    monkeypatch.setattr(engine, "dial_direct", unusable)
    s = await engine.run_survey(
        _unix(
            ssh_options=SshOptions(port=22),
            telnet_options=TelnetOptions(port=23),
            ftp_options=FtpOptions(port=21),
        )
    )
    assert seams["term"] == [("telnet", 23, None)], seams["term"]
    assert seams["ftp"] == [(21, None)], seams["ftp"]
    assert _row(s, "ftp", 21).tier == "login"
    assert _row(s, "ftp", 21).state == "supported"
    telnet = next(v for v in s.verdicts if v.protocol == "telnet" and v.tier == "login")
    assert telnet.state == "closed", telnet


@pytest.mark.asyncio
async def test_a_closed_declared_dial_still_ends_the_candidate_with_no_login(seams, monkeypatch):
    """The half of Ruling 21 that stands: an ANSWERED dial is the row, and no login runs.

    Mutation: let `closed` fall through too and a dead port reads
    `login-failed` — an answer that drifts and pins — from a connect nothing
    was listening behind.
    """

    async def refused(ip, port, *, timeout):
        seams["dial"].append((ip, port, timeout))
        return DialOutcome(state="closed", detail="connection refused")

    monkeypatch.setattr(engine, "dial_direct", refused)
    s = await engine.run_survey(
        _unix(
            ssh_options=SshOptions(port=22),
            telnet_options=TelnetOptions(port=23),
            ftp_options=FtpOptions(port=21),
        )
    )
    assert seams["term"] == [], seams["term"]
    assert seams["ftp"] == [], seams["ftp"]
    assert not any(v.tier == "login" and v.protocol in ("telnet", "ftp") for v in s.verdicts)


@pytest.mark.asyncio
async def test_snmp_is_still_checked_when_the_hosts_own_session_never_opened(seams, monkeypatch):
    """Ruling 26: the GET is snmp's dial, and it does not ride the host's session.

    With no term session there is no inventory, so before this the unix arm
    emitted NO snmp row at all -- for a check that is a controller-issued UDP
    call and could have run regardless. Mutation: move the call back inside
    `discovery` and this host reports nothing about snmp.
    """

    async def refused(host, timeout, *, who=None):
        seams["own"].append(host.term)
        return LoginOutcome("closed", "connection refused")

    monkeypatch.setattr(engine, "_open_own_session", refused)
    s = await engine.run_survey(_unix(snmp=SnmpOptions(port=161)))
    assert seams["inventory"] == []
    assert seams["snmp"] == [161], seams["snmp"]
    row = _row(s, "snmp", 161)
    assert (row.state, row.tier, row.vantage) == ("supported", "dial", "controller"), row


@pytest.mark.asyncio
async def test_a_relayed_snmp_endpoint_gets_no_inventory_row_but_is_still_queried(seams):
    """Ruling 26: a relay endpoint is not described by the GUEST's socket table.

    `snmp.address` is documented as the endpoint reachable from the otto host
    -- typically the local end of a UDP relay, not the device's own address.
    Reading `nothing bound on :161` off the guest's own `ss` output and
    printing `closed` for an agent `otto monitor` polls successfully is a
    verdict fabricated from looking at the wrong machine (mutation: drop the
    relay guard and the `closed` inventory row comes back and outranks
    nothing, but states a dead agent).
    """
    host = _unix(
        ssh_options=SshOptions(port=22),
        ftp_options=FtpOptions(port=21),
        snmp=SnmpOptions(address="10.0.0.9", port=161),
    )
    s = await engine.run_survey(host)
    assert seams["snmp"] == [161], seams["snmp"]
    rows = [v for v in s.verdicts if v.protocol == "snmp"]
    assert [v.tier for v in rows] == ["dial"], rows
    assert all("nothing bound" not in v.detail for v in rows), rows


def _embedded(**extra):
    extra.setdefault("telnet_options", TelnetOptions(port=1))
    return EmbeddedHost(
        element=Element(name="z1"),
        name="z1",
        ip="127.0.0.1",
        creds=[],
        command_frame=ZephyrFrame(),
        log=LogMode.QUIET,
        **extra,
    )


@pytest.mark.asyncio
async def test_embedded_sweeps_the_bounded_set_and_promotes_a_telnet_banner(monkeypatch):
    calls: dict[str, list] = {"sweep": [], "console": [], "snmp": []}

    async def telnet_ok():
        return object()

    async def sweep(ports, dial, *, concurrency):
        calls["sweep"].append((list(ports), concurrency))
        rows = []
        for p in ports:
            if p == 2323:
                rows.append(
                    SweepRow(
                        p,
                        DialOutcome(
                            state="open", service="telnet", banner="\xff\xfd\x18", detail="IAC"
                        ),
                    )
                )
            elif p == 22:
                rows.append(
                    SweepRow(
                        p,
                        DialOutcome(
                            state="open",
                            service="ssh",
                            banner="SSH-2.0-dropbear\r\n",
                            detail="SSH-2.0-dropbear",
                        ),
                    )
                )
            elif p == 8023:
                rows.append(
                    SweepRow(p, DialOutcome(state="open", detail="open, no banner within 0.5s"))
                )
            else:
                rows.append(
                    SweepRow(p, DialOutcome(state="timeout", detail="no connect within 0.5s"))
                )
        return rows

    async def console(host, port, *, timeout):
        calls["console"].append(port)
        return LoginOutcome("supported", "console connected")

    async def snmp(**kw):
        calls["snmp"].append(kw["port"])
        return ProtocolVerdict(
            protocol="snmp",
            kind="monitor",
            port=kw["port"],
            state="timeout",
            tier="dial",
            vantage=kw["vantage"],
            detail="no reply",
        )

    host = _embedded(telnet_options=TelnetOptions(port=2325))
    monkeypatch.setattr(type(host.connections), "telnet", lambda self: telnet_ok())
    monkeypatch.setattr(engine, "sweep_ports", sweep)
    monkeypatch.setattr(engine, "attempt_console_open", console)
    monkeypatch.setattr(engine, "check_snmp", snmp)
    s = await engine.run_survey(host, scan_ports="9000")
    # Ruling 27: 2325 is the declared console port and the sweep never dials
    # it. Its verdict is the host's own connection's (the login row asserted
    # below), a sweep row on it could never have survived merge_verdicts, and
    # the connect it costs is a second client on a single-client console.
    # Mutation: put 2325 back in the list and this is the only test that says so.
    assert calls["sweep"] == [([23, 2323, 8023, 22, 2222, 9000], engine.SWEEP_CONCURRENCY)]
    assert 2325 not in calls["sweep"][0][0]
    assert calls["console"] == [2323]
    assert _row(s, "telnet", 2325).state == "supported"
    assert _row(s, "telnet", 2325).tier == "login"
    assert _row(s, "telnet", 2323).state == "supported"
    assert _row(s, "console", 2325).state == "supported"
    assert _row(s, "tftp", 69).state == "not-checkable"
    assert "22/tcp SSH-2.0-dropbear" in s.other_listeners
    assert "8023/tcp unknown (no banner)" in s.other_listeners
    assert sorted(calls["snmp"]) == [161, 1161]
    assert s.working_ports == {}  # the declared console port won


@pytest.mark.asyncio
async def test_an_embedded_console_transfer_row_stays_supported(monkeypatch):
    """Only a console TERM row is kept out of ``supported``; the TRANSFER row is not.

    ``console`` names both a (non-dialling) term backend and the embedded
    transfer backend. The resolver's skip is for the term's own-session row
    only -- an embedded host's supported console transfer must still land in
    ``survey.supported``, or the report reads a working transfer as absent.
    """

    async def telnet_ok():
        return object()

    async def sweep(ports, dial, *, concurrency):
        return [SweepRow(p, DialOutcome(state="timeout")) for p in ports]

    async def snmp(**kw):
        return ProtocolVerdict(
            protocol="snmp",
            kind="monitor",
            port=kw["port"],
            state="timeout",
            tier="dial",
            vantage=kw["vantage"],
            detail="no reply",
        )

    host = _embedded(telnet_options=TelnetOptions(port=2325))
    monkeypatch.setattr(type(host.connections), "telnet", lambda self: telnet_ok())
    monkeypatch.setattr(engine, "sweep_ports", sweep)
    monkeypatch.setattr(engine, "check_snmp", snmp)
    s = await engine.run_survey(host)
    row = _row(s, "console", 2325)
    assert (row.kind, row.state) == ("transfer", "supported")
    assert "console" in s.supported
    pin = menu_pin(host.valid_terms, host.valid_transfers, s)
    assert not any(line.startswith("valid_transfers") for line in pin)


@pytest.mark.asyncio
async def test_embedded_behind_a_hop_without_a_relay_does_not_dial_snmp_blindly(monkeypatch):
    async def telnet_ok():
        return object()

    async def sweep(ports, dial, *, concurrency):
        return [SweepRow(p, DialOutcome(state="timeout")) for p in ports]

    host = _embedded(hop="test4", telnet_options=TelnetOptions(port=23))
    monkeypatch.setattr(type(host.connections), "telnet", lambda self: telnet_ok())
    monkeypatch.setattr(engine, "sweep_ports", sweep)
    monkeypatch.setattr(EmbeddedHost, "hop_host", lambda self: None)
    s = await engine.run_survey(host)
    assert _row(s, "snmp", 161).state == "not-checkable"
    assert "relay" in _row(s, "snmp", 161).detail
    assert _row(s, "telnet", 23).vantage == "hop:test4"


@pytest.mark.asyncio
async def test_each_term_a_session_opened_on_reports_its_terminal_mapping(seams, monkeypatch):
    """The own session and every other term's login session report what their handshake saw.

    Mutation: drop the engine's note of the mapping and the survey carries none.
    """

    async def own(host, timeout, *, who=None):
        return LoginOutcome("supported", "login 'admin': session opened", terminal_mapping="onlcr")

    async def term(host, protocol, port, *, login, timeout):
        return LoginOutcome(
            "supported", "login 'admin': session opened", terminal_mapping="unknown('X')"
        )

    monkeypatch.setattr(engine, "_open_own_session", own)
    monkeypatch.setattr(engine, "attempt_term_login", term)
    host = _unix(ssh_options=SshOptions(port=22), telnet_options=TelnetOptions(port=23))
    s = await engine.run_survey(host)
    assert s.terminal_mappings == {"ssh": "onlcr", "telnet": "unknown('X')"}


@pytest.mark.asyncio
async def test_a_term_whose_login_opened_no_session_reports_no_mapping(seams):
    """The default `term` seam refuses telnet: no session opened, so no mapping is claimed."""
    host = _unix(ssh_options=SshOptions(port=22), telnet_options=TelnetOptions(port=23))
    s = await engine.run_survey(host)
    assert "telnet" not in s.terminal_mappings


@pytest.mark.asyncio
async def test_the_own_session_reads_the_hosts_mapping_after_it_opens(monkeypatch):
    """`_open_own_session` reports the default session's mapping, measured by its handshake."""

    async def ok(self, *_a, **_k):
        return None

    monkeypatch.setattr(UnixHost, "run", ok)
    monkeypatch.setattr(UnixHost, "terminal_mapping", property(lambda self: "identity"))
    out = await engine._open_own_session(_unix(), 5.0)
    assert out.terminal_mapping == "identity"


class TestShellLoginBudget:
    """A refused telnet password must reach the row as login-failed, never timeout (#732).

    The session reports a shell that never became ready only after spending
    its whole ``open_budget``; a survey budget under that cuts the attempt
    off first.
    """

    def test_the_default_budget_outlasts_a_failed_open(self):
        host = _unix()
        assert engine._shell_login_budget(host) > host._session_mgr.open_budget()

    def test_the_budget_follows_the_host_init_timeout(self):
        assert engine._shell_login_budget(_unix(init_timeout=30.0)) > 2 * 30.0

    def test_a_telnet_login_from_an_ssh_host_counts_telnet_connect_waits(self):
        # The survey logs in over each declared term on a copy of the host,
        # so the budget is sized for the term it dials, not the host's own.
        host = _unix(term="ssh", telnet_options=TelnetOptions(port=1, echo_negotiation_timeout=9.0))
        assert engine._shell_login_budget(host, "telnet") > host._session_mgr.open_budget(
            term="telnet"
        )
        assert (
            host._session_mgr.open_budget(term="telnet")
            >= host._session_mgr.open_budget() + 2 * 9.0
        )

    def test_a_fast_host_keeps_the_floor(self):
        host = _unix(init_timeout=0.1, ssh_options=SshOptions(port=1))
        assert engine._shell_login_budget(host, "ssh") == engine.LOGIN_TIMEOUT_S


@pytest.mark.asyncio
async def test_every_shell_login_the_survey_makes_outlasts_a_failed_open(seams, monkeypatch):
    """Own session, declared port and discovered port all get the same sized budget (#732).

    Checked at the call sites, not only the helper: a discovered port once
    kept the flat ``LOGIN_TIMEOUT_S`` while declared ones moved on.
    """
    budgets: dict[str, float] = {}

    async def own(host, timeout, *, who=None):
        budgets["own"] = timeout
        return LoginOutcome("supported", "login 'admin': session opened")

    async def term(host, protocol, port, *, login, timeout):
        budgets[f"{protocol}:{port}"] = timeout
        return LoginOutcome("closed", "refused")

    async def inventory(run, *, elevate):
        return Inventory(
            tool="ss", elevated=True, listeners=[Listener("tcp", "0.0.0.0", 2222, "sshd")]
        )

    monkeypatch.setattr(engine, "_open_own_session", own)
    monkeypatch.setattr(engine, "attempt_term_login", term)
    monkeypatch.setattr(engine, "run_inventory", inventory)
    host = _unix(
        ssh_options=SshOptions(port=22),
        telnet_options=TelnetOptions(port=23),
        valid_terms=["telnet", "ssh"],
        term="telnet",
        valid_transfers=["scp"],
        init_timeout=30.0,
    )
    await engine.run_survey(host)
    mgr = host._session_mgr
    assert set(budgets) >= {"own", "ssh:22", "ssh:2222"}, budgets
    # Each login against its own term's open: telnet for the host's own
    # session, ssh for the declared and discovered ssh ports.
    assert budgets["own"] > mgr.open_budget(), budgets
    assert all(budgets[k] > mgr.open_budget(term="ssh") for k in ("ssh:22", "ssh:2222")), budgets


@pytest.mark.asyncio
async def test_an_ssh_host_sizes_its_telnet_logins_for_telnet(seams, monkeypatch):
    """A telnet port on an ssh host gets telnet's budget, declared or discovered (#732).

    Telnet's connect waits are the larger ones, so a budget sized for the
    host's own term (ssh) would cut a telnet login short.
    """
    budgets: dict[str, float] = {}

    async def term(host, protocol, port, *, login, timeout):
        budgets[f"{protocol}:{port}"] = timeout
        return LoginOutcome("closed", "refused")

    async def inventory(run, *, elevate):
        return Inventory(
            tool="ss", elevated=True, listeners=[Listener("tcp", "0.0.0.0", 2323, "telnetd")]
        )

    async def dial(ip, port, *, timeout):
        return DialOutcome(state="open", service="telnet", banner="\xff\xfd\x18", detail="IAC")

    monkeypatch.setattr(engine, "attempt_term_login", term)
    monkeypatch.setattr(engine, "run_inventory", inventory)
    monkeypatch.setattr(engine, "dial_direct", dial)
    host = _unix(
        ssh_options=SshOptions(port=22),
        telnet_options=TelnetOptions(port=23),
        valid_terms=["ssh", "telnet"],
        term="ssh",
        valid_transfers=["scp"],
        init_timeout=30.0,
    )
    await engine.run_survey(host)
    telnet_open = host._session_mgr.open_budget(term="telnet")
    assert set(budgets) >= {"telnet:23", "telnet:2323"}, budgets
    assert all(budgets[k] > telnet_open for k in ("telnet:23", "telnet:2323")), budgets


# --- The netcat strategies: the probe measures each ladder's rungs. ---------

_PORTS = {r.name: r for r in nc_scripts.PORT_LADDER}
_LISTENERS = {r.name: r for r in nc_scripts.LISTENER_LADDER}
_AVAILABILITY = nc_scripts.availability_probe()
_BASE = NcOptions().port
_FREE = 9100
"""The port every scripted port strategy answers, so the probe's listener is opened on it."""


def _port_script(name: str, base: int = _BASE) -> str:
    return _PORTS[name].scripts(base, "")[0]


def _listener_script(name: str, port: int = _FREE) -> str:
    return _LISTENERS[name].script(port)


def _ok(value: str) -> CommandResult:
    return CommandResult(status=Status.Success, value=value, retcode=0)


def _sentinel() -> CommandResult:
    return CommandResult(status=Status.Failed, value=nc_scripts.TOOL_FAILED, retcode=1)


class _FakeNc:
    """The host's netcat backend, as the survey sees it.

    ``_measure_listener_checks`` types the listener, then asks each check once
    through the survey's runner and the backend's own ``_ask_listener_check``,
    so the scripts the test answered are what it counts, and records a
    listener that was up and ended with status 0; a test that needs another
    ending sets ``fill``, which then fills ``into`` in its place.
    ``_port_by_strategy`` runs the pinned strategy's first script from the
    default base, through the survey's runner.
    """

    def __init__(self) -> None:
        self.users: list = []
        self.measured: list[tuple[int, list[str], int]] = []
        self.strategies: list[str] = []
        self.fill = None
        self.made = 0
        self.capped = True

    async def _port_by_strategy(self, strategy, run):
        self.strategies.append(strategy)
        if strategy == "custom":
            return _FREE
        return await run_port_rung(_PORTS[strategy], run, base_port=_BASE, reserved="")

    def _measure_listener_checks(self, run, checks, into, *, cap_s):
        # Recorded when the survey makes the step, not when it runs it.
        self.made += 1
        return self._measure(run, checks, into, cap_s)

    async def _measure(self, run, checks, into, cap_s):
        self.measured.append((into.port, [c.name for c in checks], cap_s))
        into.capped = self.capped
        into.bare_command = f"nc -l -p {into.port}"
        into.spawned = True
        if self.fill is not None:
            await self.fill(run, checks, into)
            return
        for check in checks:
            into.answers[check.name] = await _ask_listener_check(check, run, into.port)
        counts = [n for n in into.answers.values() if isinstance(n, int)]
        into.timed_out = not any(n >= 1 for n in counts)
        into.was_up = into.end_confirmed = True
        into.exit_status, into.ending = 0, "the listener command exited with status 0"


@pytest.fixture
def nc_backend(monkeypatch):
    """The fake netcat backend ``_nc_backend_for`` hands the survey; it records the user."""
    backend = _FakeNc()

    def backend_for(self, user):
        backend.users.append(user)
        return backend

    monkeypatch.setattr(UnixHost, "_nc_backend_for", backend_for)
    return backend


@pytest.fixture
def scripted(seams, nc_backend, monkeypatch):
    """A host whose ``exec`` answers only the table's scripts; ``run`` must never see one.

    Yields the answer map (script -> result) the test fills and the list of
    every script ``exec`` received, in order. A script nobody answered fails
    the test: the probe runs nothing the table did not build. The port scan
    starts at ``nc_options.port`` exactly, so each port script is known.
    """
    answers: dict[str, CommandResult] = {}
    ran: list[str] = []
    run_cmds: list = []

    async def exec_(self, cmd, **kw):
        ran.append(cmd)
        assert cmd in answers, f"a script the table did not build reached exec: {cmd!r}"
        return answers[cmd]

    async def run(self, cmds, **kw):
        run_cmds.append(cmds)
        raise AssertionError(f"a script reached run: {cmds!r}")

    monkeypatch.setattr(UnixHost, "exec", exec_)
    monkeypatch.setattr(UnixHost, "run", run)
    monkeypatch.setattr(engine, "scan_start", lambda base: base)
    yield answers, ran
    assert run_cmds == []


@pytest.mark.asyncio
async def test_ss_works_for_both_ladders(scripted):
    answers, ran = scripted
    answers[_AVAILABILITY] = _ok("ss netstat python proc")
    answers[_port_script("ss")] = _ok(str(_FREE))
    for name in _LISTENERS:
        answers[_listener_script(name)] = _ok("1")
    s = await engine.run_survey(_unix())
    assert s.nc_rungs == engine.NcRungs(
        port=engine.NcLadderResult("measured", "ss"),
        listener=engine.NcLadderResult("measured", "ss"),
    )
    assert ran == [_AVAILABILITY, _port_script("ss"), *map(_listener_script, _LISTENERS)]


@pytest.mark.asyncio
async def test_a_present_rung_that_fails_is_named_and_the_next_one_measured(scripted):
    answers, _ran = scripted
    answers[_AVAILABILITY] = _ok("ss netstat proc")
    answers[_port_script("ss")] = _sentinel()
    answers[_port_script("netstat")] = _ok(str(_FREE))
    for name in _LISTENERS:
        answers[_listener_script(name)] = _ok("1")
    s = await engine.run_survey(_unix())
    assert s.nc_rungs is not None
    assert s.nc_rungs.port == engine.NcLadderResult("measured", "netstat")
    assert s.nc_rungs.listener == engine.NcLadderResult("measured", "ss")
    assert [f"{f.ladder} {f.strategy}" for f in s.nc_rungs.failed] == ["port ss"], s.nc_rungs.failed


@pytest.mark.asyncio
async def test_a_rung_that_is_not_available_is_passed_over_in_silence(scripted, nc_backend):
    answers, ran = scripted
    answers[_AVAILABILITY] = _ok("python proc")
    answers[_port_script("python")] = _ok(str(_FREE))
    answers[_listener_script("proc")] = _ok("1")
    s = await engine.run_survey(_unix())
    assert s.nc_rungs == engine.NcRungs(
        port=engine.NcLadderResult("measured", "python"),
        listener=engine.NcLadderResult("measured", "proc"),
    )
    assert ran == [_AVAILABILITY, _port_script("python"), _listener_script("proc")]
    assert [checks for _port, checks, _cap in nc_backend.measured] == [["proc"]]


async def _measured(scripted, nc_backend, fill, *, available="ss netstat"):
    """Survey a host whose port ladder finds :data:`_FREE` and whose measurement *fill* fills."""
    answers, _ran = scripted
    answers[_AVAILABILITY] = _ok(available)
    answers[_port_script("ss")] = _ok(str(_FREE))
    nc_backend.fill = fill
    s = await engine.run_survey(_unix())
    assert s.nc_rungs is not None
    return s.nc_rungs


def _answering(**answers):
    """A fill whose complete round gave *answers*, with a confirmed end."""

    async def fill(run, checks, into):
        into.answers.update(answers)
        into.was_up = into.end_confirmed = True
        into.exit_status = 0

    return fill


def _failed(rungs) -> list[tuple[str, str]]:
    return [(f.ladder, f.strategy) for f in rungs.failed]


@pytest.mark.asyncio
async def test_a_check_that_counts_one_is_measured(scripted, nc_backend):
    rungs = await _measured(scripted, nc_backend, _answering(ss=1, netstat=0))
    assert rungs.listener == engine.NcLadderResult("measured", "ss")
    assert rungs.failed == []
    assert rungs.listener_left == ""


@pytest.mark.asyncio
async def test_a_check_that_counts_none_while_another_counts_it_did_not_see_the_listener(
    scripted, nc_backend
):
    rungs = await _measured(scripted, nc_backend, _answering(ss=0, netstat=1))
    assert rungs.listener == engine.NcLadderResult("measured", "netstat")
    assert _failed(rungs) == [("listener", "ss")]
    assert rungs.failed[0].reason.startswith(f"did not see the listener otto opened on :{_FREE}")


@pytest.mark.asyncio
async def test_a_check_that_counts_two_or_more_names_both_causes(scripted, nc_backend):
    rungs = await _measured(scripted, nc_backend, _answering(ss=2, netstat=1))
    assert rungs.listener == engine.NcLadderResult("measured", "netstat")
    assert _failed(rungs) == [("listener", "ss")]
    reason = rungs.failed[0].reason
    assert reason.startswith(f"counted 2 listeners on :{_FREE}, where otto opened one."), reason
    assert "Either the check miscounts this netcat" in reason


@pytest.mark.asyncio
async def test_a_check_whose_tool_failed_or_gave_no_answer_is_failed_as_before(
    scripted, nc_backend
):
    tool = NcRungToolError("ss: the tool is missing or failed")
    rungs = await _measured(
        scripted, nc_backend, _answering(ss=tool, netstat=None, proc=1), available="ss netstat proc"
    )
    assert rungs.listener == engine.NcLadderResult("measured", "proc")
    assert _failed(rungs) == [("listener", "ss"), ("listener", "netstat")]
    assert rungs.failed[0].reason == "ss: the tool is missing or failed"
    assert rungs.failed[1].reason.startswith(f"no answer about :{_FREE}")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("was_up", "late", "opening"),
    [
        (True, False, "and it was up when otto closed it."),
        (False, False, "and otto could not tell whether it started."),
        (True, True, "but a check counted it after that, so it took longer than that to start."),
    ],
    ids=["was-up", "unknown", "late"],
)
async def test_no_check_counting_before_the_readiness_timeout_leaves_the_ladder_unmeasured(
    scripted, nc_backend, was_up, late, opening
):
    """Each case says what was seen and what to do; a late count is not called blindness."""

    async def fill(run, checks, into):
        # The complete round still ran; it is not the verdict.
        into.answers.update(ss=1 if late else 0, netstat=0)
        into.timed_out, into.was_up, into.end_confirmed, into.exit_status = True, was_up, True, 0
        into.waited_s = 5.0

    rungs = await _measured(scripted, nc_backend, fill)
    assert rungs.listener.state == "unmeasured"
    note = rungs.listener.note
    said = f"no listener check counted the listener otto opened on :{_FREE} within 5 s"
    assert note.startswith(f"{said}, "), note
    assert opening in note, note
    if late:
        action = "Run the probe again to see whether it starts in time"
        assert "blind" not in note, note
    elif was_up:
        action = "Run the probe again; if no check counts it again, set nc_options.listener_check"
    else:
        action = f"Run 'nc -l -p {_FREE}' on the host to see whether it starts, or run the probe"
    assert action in note, note
    assert rungs.failed == []


@pytest.mark.asyncio
async def test_a_listener_command_that_raised_leaves_the_ladder_unmeasured_with_its_cause(
    scripted, nc_backend
):
    async def fill(run, checks, into):
        into.answers["ss"] = 0  # a partial round: never a failed check
        into.ended_early = True
        into.ending = "the 'nc' command failed: channel closed"

    nc_backend.capped = False
    rungs = await _measured(scripted, nc_backend, fill)
    assert rungs.listener.state == "unmeasured"
    assert rungs.listener.note == (
        f"otto lost the listener it opened on :{_FREE} before it closed it:"
        " the 'nc' command failed: channel closed. Run the probe again"
    )
    assert "status" not in rungs.listener.note
    assert rungs.failed == []
    assert rungs.listener_left.startswith(
        f"otto could not confirm the listener it opened on :{_FREE}"
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "status", [127, 126, 1, 0], ids=["not-found", "not-runnable", "other", "zero"]
)
async def test_a_listener_that_ended_before_the_close_names_its_exit_status(
    scripted, nc_backend, status
):
    ending = {
        127: "'nc' exited with status 127, so it was not found on the host"
        " (nc_options.exec_name names the netcat to run)",
        126: "'nc' exited with status 126, so it was found but could not be run"
        " (nc_options.exec_name names the netcat to run)",
        1: "the listener command exited with status 1",
        0: "the listener command exited with status 0",
    }[status]

    async def fill(run, checks, into):
        into.answers["ss"] = 0
        into.ended_early = into.end_confirmed = True
        into.exit_status, into.ending = status, ending

    rungs = await _measured(scripted, nc_backend, fill)
    assert rungs.listener.state == "unmeasured"
    note = rungs.listener.note
    assert note.startswith(
        f"the listener otto opened on :{_FREE} ended before otto closed it: {ending}"
    ), note
    refused = f"{ending}. Either this netcat refuses that command"
    empty_input = f"{ending}. Either another process connected to it before otto did"
    assert (refused in note) == (status == 1), note
    assert (empty_input in note) == (status == 0), note
    if status == 0:
        assert f"Run 'nc -l -p {_FREE} < /dev/null' on the host" in note, note
    if status == 1:
        assert f"Run 'nc -l -p {_FREE}' on the host to see its message" in note, note
    assert "-l -p PORT" not in note, note
    if status in (126, 127):
        assert note.endswith(ending), note
    assert rungs.failed == []
    assert rungs.listener_left == ""


@pytest.mark.asyncio
async def test_two_or_more_is_not_measured_again_on_another_port(scripted, nc_backend):
    rungs = await _measured(scripted, nc_backend, _answering(ss=2), available="ss")
    assert len(nc_backend.measured) == 1
    assert rungs.listener.state == "unmeasured"
    assert _failed(rungs) == [("listener", "ss")]
    assert rungs.failed[0].reason.startswith("counted 2 listeners")


@pytest.mark.asyncio
async def test_auto_auto_measures_on_the_port_the_ladder_found(scripted, nc_backend):
    """The listener is opened on the port the measured strategy printed."""
    answers, ran = scripted
    answers[_AVAILABILITY] = _ok("ss netstat")
    answers[_port_script("ss")] = _ok("41000")
    answers[_listener_script("ss", 41000)] = _ok("1")
    answers[_listener_script("netstat", 41000)] = _ok("0")
    s = await engine.run_survey(_unix())
    assert s.nc_rungs is not None
    assert s.nc_rungs.listener == engine.NcLadderResult("measured", "ss")
    assert nc_backend.measured == [(41000, ["ss", "netstat"], int(engine.SURVEY_BUDGET_S))]
    assert nc_backend.strategies == []
    assert ran == [
        _AVAILABILITY,
        _port_script("ss"),
        _listener_script("ss", 41000),
        _listener_script("netstat", 41000),
    ]


@pytest.mark.asyncio
async def test_a_pinned_port_strategy_is_run_once_for_the_port_and_reported_as_declared(
    scripted, nc_backend
):
    answers, ran = scripted
    answers[_AVAILABILITY] = _ok("ss netstat")
    answers[_port_script("netstat")] = _ok(str(_FREE))
    answers[_listener_script("ss")] = _ok("1")
    answers[_listener_script("netstat")] = _ok("1")
    host = _unix(nc_options=NcOptions(port_strategy="netstat"))
    s = await engine.run_survey(host)
    assert s.nc_rungs is not None
    assert s.nc_rungs.port == engine.NcLadderResult("declared", "netstat")
    assert s.nc_rungs.listener == engine.NcLadderResult("measured", "ss")
    assert nc_backend.strategies == ["netstat"]
    assert ran == [
        _AVAILABILITY,
        _port_script("netstat"),
        _listener_script("ss"),
        _listener_script("netstat"),
    ]


@pytest.mark.asyncio
async def test_a_pinned_custom_port_strategy_is_asked_once_for_the_port(scripted, nc_backend):
    answers, _ran = scripted
    answers[_AVAILABILITY] = _ok("ss")
    answers[_listener_script("ss")] = _ok("1")
    host = _unix(nc_options=NcOptions(port_strategy="custom", port_cmd="echo 9100"))
    s = await engine.run_survey(host)
    assert s.nc_rungs is not None
    assert s.nc_rungs.port == engine.NcLadderResult("declared", "custom")
    assert s.nc_rungs.listener == engine.NcLadderResult("measured", "ss")
    assert nc_backend.strategies == ["custom"]
    assert [port for port, _checks, _cap in nc_backend.measured] == [_FREE]


@pytest.mark.asyncio
async def test_a_pinned_listener_check_opens_no_listener(scripted, nc_backend):
    answers, ran = scripted
    answers[_AVAILABILITY] = _ok("ss")
    answers[_port_script("ss")] = _ok(str(_FREE))
    host = _unix(nc_options=NcOptions(listener_check="custom", listener_cmd="true"))
    s = await engine.run_survey(host)
    assert s.nc_rungs is not None
    assert s.nc_rungs.port == engine.NcLadderResult("measured", "ss")
    assert s.nc_rungs.listener == engine.NcLadderResult("declared", "custom")
    assert ran == [_AVAILABILITY, _port_script("ss")]
    assert nc_backend.users == nc_backend.measured == []


@pytest.mark.asyncio
async def test_both_pinned_runs_nothing(scripted, nc_backend):
    _answers, ran = scripted
    host = _unix(nc_options=NcOptions(port_strategy="netstat", listener_check="proc"))
    s = await engine.run_survey(host)
    assert s.nc_rungs == engine.NcRungs(
        port=engine.NcLadderResult("declared", "netstat"),
        listener=engine.NcLadderResult("declared", "proc"),
    )
    assert ran == []
    assert nc_backend.users == []


@pytest.mark.asyncio
async def test_the_port_ladder_scans_from_the_random_start(scripted, monkeypatch, nc_backend):
    answers, ran = scripted
    starts: list[int] = []

    def start(base):
        starts.append(base)
        return base + 7

    monkeypatch.setattr(engine, "scan_start", start)
    answers[_AVAILABILITY] = _ok("ss")
    answers[_port_script("ss", _BASE + 7)] = _ok(str(_FREE))
    answers[_listener_script("ss")] = _ok("1")
    s = await engine.run_survey(_unix())
    assert s.nc_rungs is not None
    assert s.nc_rungs.port == engine.NcLadderResult("measured", "ss")
    assert starts == [_BASE]
    assert ran[1] == _port_script("ss", _BASE + 7)


@pytest.mark.asyncio
@pytest.mark.parametrize("pinned", [False, True], ids=["ladder-found-none", "pinned-raised"])
async def test_no_port_leaves_the_listener_ladder_unmeasured_quoting_the_error(
    scripted, nc_backend, pinned
):
    answers, _ran = scripted
    answers[_AVAILABILITY] = _ok("ss netstat")
    answers[_port_script("ss")] = _sentinel()
    answers[_port_script("netstat")] = _sentinel()
    opts = NcOptions(port_strategy="netstat") if pinned else NcOptions()
    s = await engine.run_survey(_unix(nc_options=opts))
    assert s.nc_rungs is not None
    note = s.nc_rungs.listener.note
    assert s.nc_rungs.listener.state == "unmeasured"
    assert note.startswith("there was no free port to open a listener on: "), note
    if pinned:
        assert s.nc_rungs.port == engine.NcLadderResult("declared", "netstat")
        assert "netstat" in note, note
    else:
        assert s.nc_rungs.port.state == "unmeasured"
        assert note.endswith(s.nc_rungs.port.note), note
    assert nc_backend.measured == []


@pytest.mark.asyncio
async def test_another_exec_name_is_measured_when_the_nc_row_is_not_supported(
    scripted, monkeypatch, nc_backend
):
    answers, _ran = scripted
    monkeypatch.setattr(UnixHost, "userland", lambda self: _Userland(nc="absent"))
    answers[_AVAILABILITY] = _ok("ss")
    answers[_port_script("ss")] = _ok(str(_FREE))
    answers[_listener_script("ss")] = _ok("1")
    s = await engine.run_survey(_unix(nc_options=NcOptions(exec_name="ncat")))
    assert s.nc_rungs is not None
    assert s.nc_rungs.listener == engine.NcLadderResult("measured", "ss")
    assert len(nc_backend.measured) == 1


@pytest.mark.asyncio
async def test_the_default_exec_name_still_needs_the_nc_row(scripted, monkeypatch, nc_backend):
    _answers, ran = scripted
    monkeypatch.setattr(UnixHost, "userland", lambda self: _Userland(nc="absent"))
    s = await engine.run_survey(_unix())
    assert ran == []
    assert s.nc_rungs is not None
    assert s.nc_rungs.port.state == s.nc_rungs.listener.state == "unmeasured"
    assert s.nc_rungs.port.note == "nc is not supported on this host"
    assert nc_backend.users == []


async def _survey_with_backend(scripted, monkeypatch, backend_for, opts):
    """Survey a host whose ss port script fails and netstat's works, with *backend_for* patched."""
    answers, _ran = scripted
    answers[_AVAILABILITY] = _ok("ss netstat")
    answers[_port_script("ss")] = _sentinel()
    answers[_port_script("netstat")] = _ok(str(_FREE))
    answers[_listener_script("ss")] = _ok("1")
    answers[_listener_script("netstat")] = _ok("1")
    if backend_for is not None:
        monkeypatch.setattr(UnixHost, "_nc_backend_for", backend_for)
    s = await engine.run_survey(_unix(nc_options=opts))
    assert s.nc_rungs is not None
    return s


def _raise_build(self, user):
    raise ValueError("max_concurrent_transfers must be at least 1")


@pytest.mark.asyncio
async def test_a_backend_build_that_raises_leaves_the_port_result_and_every_row_alone(
    scripted, monkeypatch
):
    works = await _survey_with_backend(scripted, monkeypatch, None, NcOptions())
    broken = await _survey_with_backend(scripted, monkeypatch, _raise_build, NcOptions())
    assert works.nc_rungs.listener == engine.NcLadderResult("measured", "ss")
    assert _failed(works.nc_rungs) == [("port", "ss")]
    assert broken.nc_rungs.port == works.nc_rungs.port
    assert broken.nc_rungs.failed == works.nc_rungs.failed
    assert broken.verdicts == works.verdicts
    assert broken.nc_rungs.listener.state == "unmeasured"
    assert broken.nc_rungs.listener.note.startswith(
        "otto could not set up netcat for this host: ValueError: max_concurrent"
    ), broken.nc_rungs.listener.note


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "opts", [NcOptions(), NcOptions(port_strategy="netstat")], ids=["auto-port", "pinned-port"]
)
async def test_a_replacement_backend_with_neither_method_does_the_same(scripted, monkeypatch, opts):
    works = await _survey_with_backend(scripted, monkeypatch, None, opts)
    neither = lambda self, user: object()  # noqa: E731 — a registered backend with no method
    broken = await _survey_with_backend(scripted, monkeypatch, neither, opts)
    assert works.nc_rungs.listener == engine.NcLadderResult("measured", "ss")
    assert broken.nc_rungs.port == works.nc_rungs.port
    assert broken.nc_rungs.failed == works.nc_rungs.failed
    assert broken.verdicts == works.verdicts
    assert broken.nc_rungs.listener.state == "unmeasured"
    assert broken.nc_rungs.listener.note.startswith("the nc transfer registered for this host")


@pytest.mark.asyncio
@pytest.mark.parametrize("capped", [True, False], ids=["capped", "uncapped"])
@pytest.mark.parametrize("way", ["bounded-none", "budget-raised"])
async def test_the_budget_running_out_mid_measurement_reports_budget_spent_and_the_port(
    scripted, monkeypatch, nc_backend, way, capped
):
    """Ended once the listener was typed: its own wording, and a line only for no lifetime cap."""
    nc_backend.capped = capped
    answers, _ran = scripted
    answers[_AVAILABILITY] = _ok("ss")
    answers[_port_script("ss")] = _ok(str(_FREE))
    if way == "bounded-none":
        # How ``asyncio.wait_for`` ends a step whose grant ran out, with no clock.
        async def fill(run, checks, into):
            raise asyncio.TimeoutError

        nc_backend.fill = fill
    else:
        real_exec = UnixHost.exec

        async def exec_(self, cmd, **kw):
            if cmd == _listener_script("ss"):
                raise engine._BudgetSpent
            return await real_exec(self, cmd, **kw)

        monkeypatch.setattr(UnixHost, "exec", exec_)
    s = await engine.run_survey(_unix())
    assert s.nc_rungs is not None
    assert s.nc_rungs.port == engine.NcLadderResult("measured", "ss")
    note = s.nc_rungs.listener.note
    assert s.nc_rungs.listener.state == "unmeasured"
    assert note == engine._nc_budget_spent_measuring(_FREE), note
    assert note.startswith(
        f"the survey budget ran out while otto measured the listener it opened on :{_FREE}."
    ), note
    assert note != engine._NC_BUDGET_SPENT
    left = s.nc_rungs.listener_left
    if capped:
        assert left == "", left
    else:
        assert left.startswith(f"otto could not confirm the listener it opened on :{_FREE}"), left
        assert "No lifetime cap could be installed" in left, left


@pytest.mark.asyncio
async def test_a_measurement_that_finishes_as_the_deadline_reads_spent_is_judged_by_its_result(
    scripted, monkeypatch, nc_backend
):
    spent: list[bool] = []
    real_remaining = engine._Deadline.remaining
    monkeypatch.setattr(
        engine._Deadline, "remaining", lambda self: 0.0 if spent else real_remaining(self)
    )
    answering = _answering(ss=1)

    async def fill(run, checks, into):
        await answering(run, checks, into)
        spent.append(True)  # the clock reads spent from here on

    rungs = await _measured(scripted, nc_backend, fill)
    assert rungs.listener == engine.NcLadderResult("measured", "ss")
    assert rungs.listener_left == ""


@pytest.mark.asyncio
async def test_a_runner_error_from_the_measurement_is_reported_and_the_port_ladder_kept(
    scripted, monkeypatch
):
    """An error that is not the check's own ends the measurement, so no further check is asked.

    It is the session failing while otto's listener is up, so the listener is
    closed at once rather than asking the next check through a runner that
    just failed; the port ladder still fails one strategy per runner error.
    """
    answers, _ran = scripted
    answers[_AVAILABILITY] = _ok("ss netstat")
    answers[_port_script("ss")] = _ok(str(_FREE))
    answers[_listener_script("netstat")] = _ok("1")
    _raising_exec(scripted, monkeypatch, _listener_script("ss"), OSError("Connection refused"))
    s = await engine.run_survey(_unix())
    assert s.nc_rungs is not None
    assert s.nc_rungs.port == engine.NcLadderResult("measured", "ss")
    assert s.nc_rungs.listener.state == "unmeasured"
    assert s.nc_rungs.listener.note.startswith(
        "the listener checks could not be run: OSError: Connection refused."
    ), s.nc_rungs.listener.note
    assert s.nc_rungs.failed == []
    assert _row(s, "ssh", 1).state == "supported"


@pytest.mark.asyncio
async def test_a_cancellation_from_outside_the_measurement_is_not_caught(scripted, nc_backend):
    async def fill(run, checks, into):
        raise asyncio.CancelledError

    with pytest.raises(asyncio.CancelledError):
        await _measured(scripted, nc_backend, fill)


@pytest.mark.asyncio
async def test_a_listener_lost_while_otto_closed_it_leaves_the_ladder_unmeasured_with_its_cause(
    scripted, nc_backend
):
    """Counted, then the command raised during the close: no verdict, and the end is unconfirmed."""

    async def fill(run, checks, into):
        into.answers.update(ss=1, netstat=1)
        into.counted_early["ss"] = 1
        into.was_up = True
        into.ending = "the 'nc' command failed: channel closed"

    nc_backend.capped = False
    rungs = await _measured(scripted, nc_backend, fill)
    assert rungs.listener.state == "unmeasured"
    assert rungs.listener.note == (
        f"otto lost the listener it opened on :{_FREE} while it closed it:"
        " the 'nc' command failed: channel closed. Run the probe again"
    )
    assert "status" not in rungs.listener.note
    assert rungs.failed == []
    assert rungs.listener_left.startswith(
        f"otto could not confirm the listener it opened on :{_FREE}"
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("capped", [True, False], ids=["capped", "uncapped"])
async def test_a_listener_still_running_after_the_close_keeps_the_checks_answers(
    scripted, nc_backend, capped
):
    """No end within the close's bound: the checks' answers stand.

    A listener that carries the lifetime cap ends by itself, so nothing more
    is said: this is every probe of a host whose high ports the controller
    cannot reach. One with no cap may stay, and the listener line names it.
    """

    async def fill(run, checks, into):
        into.answers.update(ss=1, netstat=1)
        into.was_up = True

    nc_backend.capped = capped
    rungs = await _measured(scripted, nc_backend, fill)
    assert rungs.listener == engine.NcLadderResult("measured", "ss")
    if capped:
        assert rungs.listener_left == ""
    else:
        assert rungs.listener_left == (
            f"otto could not confirm the listener it opened on :{_FREE} ended. No lifetime cap"
            " could be installed, so it may stay until it is ended on the host"
        )


@pytest.mark.asyncio
async def test_a_listener_never_typed_is_not_called_opened_or_left(scripted, nc_backend):
    """A session that never opened: otto started nothing, so nothing is said to be left."""

    async def fill(run, checks, into):
        into.spawned = False
        into.ended_early = True
        into.ending = "the 'nc' command failed: the session did not open"

    rungs = await _measured(scripted, nc_backend, fill)
    assert rungs.listener.note == (
        f"otto could not start a listener on :{_FREE}:"
        " the 'nc' command failed: the session did not open. Run the probe again"
    )
    assert rungs.listener_left == ""


@pytest.mark.asyncio
async def test_an_exit_status_outranks_a_timed_out_count(scripted, nc_backend):
    """Both recorded: the exit status is the more specific fact, so it is the note."""

    async def fill(run, checks, into):
        into.timed_out = into.ended_early = into.end_confirmed = True
        into.exit_status, into.ending = 127, "'nc' exited with status 127"

    rungs = await _measured(scripted, nc_backend, fill)
    assert rungs.listener.note.startswith(
        f"the listener otto opened on :{_FREE} ended before otto closed it: 'nc' exited"
    ), rungs.listener.note


@pytest.mark.asyncio
async def test_no_budget_left_for_the_measurement_never_makes_its_step(
    scripted, monkeypatch, nc_backend
):
    """The step is made only once it runs, so a spent budget leaves no coroutine unawaited."""
    spent: list[bool] = []
    real_remaining = engine._Deadline.remaining
    monkeypatch.setattr(
        engine._Deadline, "remaining", lambda self: 0.0 if spent else real_remaining(self)
    )
    real_port = nc_backend._port_by_strategy

    async def port_then_spent(strategy, run):
        port = await real_port(strategy, run)
        spent.append(True)  # the clock reads spent from here on
        return port

    nc_backend._port_by_strategy = port_then_spent
    answers, _ran = scripted
    answers[_AVAILABILITY] = _ok("ss netstat")
    answers[_port_script("netstat")] = _ok(str(_FREE))
    with warnings.catch_warnings(record=True) as seen:
        warnings.simplefilter("always")
        s = await engine.run_survey(_unix(nc_options=NcOptions(port_strategy="netstat")))
        gc.collect()
    assert nc_backend.made == 0
    assert s.nc_rungs is not None
    assert s.nc_rungs.listener == engine.NcLadderResult("unmeasured", note=engine._NC_BUDGET_SPENT)
    assert not [w for w in seen if "never awaited" in str(w.message)], [
        str(w.message) for w in seen
    ]


def test_a_runner_error_that_ends_in_a_period_is_not_doubled():
    note = engine._nc_runner_failed(OSError("Connection refused."))
    assert (
        note
        == "the listener checks could not be run: OSError: Connection refused. Run the probe again"
    )


@pytest.mark.asyncio
async def test_a_console_session_measures_nothing(scripted, nc_backend):
    _answers, ran = scripted
    host = _unix(
        term="console",
        valid_terms=["console"],
        valid_transfers=["scp", "ftp", "nc", "shell"],
        console_options=ConsoleOptions(server="test1", port=4001),
    )
    s = await engine.run_survey(host)
    assert ran == []
    assert s.nc_rungs is not None
    assert s.nc_rungs.port.state == s.nc_rungs.listener.state == "unmeasured"
    assert "console" in s.nc_rungs.port.note
    assert nc_backend.users == nc_backend.measured == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "listeners",
    [None, [Listener("udp", "0.0.0.0", 161, "snmpd")]],
    ids=["no-inventory", "no-tcp-listener"],
)
async def test_the_listener_checks_are_measured_whatever_the_inventory_found(
    scripted, monkeypatch, listeners
):
    answers, ran = scripted
    answers[_AVAILABILITY] = _ok("ss")
    answers[_port_script("ss")] = _ok(str(_FREE))
    answers[_listener_script("ss")] = _ok("1")

    async def inventory(run, *, elevate):
        if listeners is None:
            return Inventory(tool="", elevated=False, error="inventory failed: boom")
        return Inventory(tool="ss", elevated=elevate, listeners=listeners)

    monkeypatch.setattr(engine, "run_inventory", inventory)
    s = await engine.run_survey(_unix())
    assert s.nc_rungs is not None
    assert s.nc_rungs.port == engine.NcLadderResult("measured", "ss")
    assert s.nc_rungs.listener == engine.NcLadderResult("measured", "ss")
    assert ran == [_AVAILABILITY, _port_script("ss"), _listener_script("ss")]


@pytest.mark.asyncio
@pytest.mark.parametrize("ports", [[1], [1, 8080]], ids=["own-port-only", "another-listener"])
async def test_no_inventory_listener_is_the_port_asked_about(scripted, monkeypatch, ports):
    """A dropbear host lists only its sshd, another lists more: no inventory port is asked."""
    answers, ran = scripted
    answers[_AVAILABILITY] = _ok("ss")
    answers[_port_script("ss")] = _ok(str(_FREE))
    answers[_listener_script("ss")] = _ok("1")

    async def inventory(run, *, elevate):
        return Inventory(
            tool="ss",
            elevated=elevate,
            listeners=[Listener("tcp", "0.0.0.0", p, "") for p in ports],
        )

    monkeypatch.setattr(engine, "run_inventory", inventory)
    s = await engine.run_survey(_unix())
    assert s.nc_rungs is not None
    assert s.nc_rungs.listener == engine.NcLadderResult("measured", "ss")
    assert ran[-1] == _listener_script("ss")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "answer",
    [_ok("ss bogus"), CommandResult(status=Status.Failed, value="", retcode=-1, timed_out=True)],
    ids=["not-a-rung", "timed-out"],
)
async def test_an_availability_probe_that_cannot_answer_leaves_both_unmeasured(scripted, answer):
    answers, ran = scripted
    answers[_AVAILABILITY] = answer
    s = await engine.run_survey(_unix())
    assert s.nc_rungs is not None
    assert s.nc_rungs.port.state == s.nc_rungs.listener.state == "unmeasured"
    assert s.nc_rungs.port.note == s.nc_rungs.listener.note != ""
    assert ran == [_AVAILABILITY]


@pytest.mark.asyncio
async def test_every_script_the_probe_runs_is_one_the_table_built(scripted):
    """Every rung fails but the last: each script that reached exec is a table builder's output."""
    answers, ran = scripted
    answers[_AVAILABILITY] = _ok("ss netstat python proc")
    for name, rung in _PORTS.items():
        for script in rung.scripts(_BASE, ""):
            answers[script] = _ok(str(_FREE)) if name == "proc" else _sentinel()
    for name in _LISTENERS:
        answers[_listener_script(name)] = _ok("1") if name == "proc" else _ok("0")
    built = set(answers)
    s = await engine.run_survey(_unix())
    assert s.nc_rungs is not None
    assert s.nc_rungs.port == engine.NcLadderResult("measured", "proc")
    assert s.nc_rungs.listener == engine.NcLadderResult("measured", "proc")
    assert set(ran) == built
    assert [f"{f.ladder} {f.strategy}" for f in s.nc_rungs.failed] == [
        "port ss",
        "port netstat",
        "port python",
        "listener ss",
        "listener netstat",
    ]


@pytest.mark.asyncio
async def test_a_strategy_that_hangs_is_one_failed_strategy_and_the_walk_goes_on(
    scripted, monkeypatch
):
    """A hung ss port scan times out on its own bound: netstat is measured, the listeners walked.

    Mutation: read every ``None`` from ``_bounded`` as a spent survey budget
    and both ladders read "the survey budget ran out" after one hang.
    """
    answers, ran = scripted
    monkeypatch.setattr(engine, "INVENTORY_TIMEOUT_S", 0.05)
    answers[_AVAILABILITY] = _ok("ss netstat")
    answers[_port_script("netstat")] = _ok(str(_FREE))
    answers[_listener_script("ss")] = _ok("1")
    answers[_listener_script("netstat")] = _ok("0")
    hang = _port_script("ss")
    answers[hang] = _ok("never read")
    real_exec = UnixHost.exec

    async def exec_(self, cmd, **kw):
        if cmd == hang:
            ran.append(cmd)
            await asyncio.sleep(3600)
        return await real_exec(self, cmd, **kw)

    monkeypatch.setattr(UnixHost, "exec", exec_)
    s = await engine.run_survey(_unix())
    assert s.nc_rungs is not None
    assert s.nc_rungs.port == engine.NcLadderResult("measured", "netstat")
    assert s.nc_rungs.listener == engine.NcLadderResult("measured", "ss")
    assert [(f.ladder, f.strategy) for f in s.nc_rungs.failed] == [("port", "ss")]
    assert "timed out" in s.nc_rungs.failed[0].reason, s.nc_rungs.failed
    assert ran == [
        _AVAILABILITY,
        hang,
        _port_script("netstat"),
        _listener_script("ss"),
        _listener_script("netstat"),
    ]


@pytest.mark.asyncio
async def test_with_no_strategys_tool_each_ladder_says_how_to_pin_custom(scripted, nc_backend):
    answers, ran = scripted
    answers[_AVAILABILITY] = _ok("")
    s = await engine.run_survey(_unix())
    assert s.nc_rungs is not None
    assert s.nc_rungs.port.note == (
        "no strategy's tool is on the host; set nc_options.port_strategy to"
        ' "custom" with a port_cmd that prints a free port'
    )
    assert s.nc_rungs.listener.note == (
        "no strategy's tool is on the host; set nc_options.listener_check to"
        ' "custom" with a listener_cmd that exits 0 when {port} is listening'
    )
    assert ran == [_AVAILABILITY]
    assert nc_backend.measured == []


def _raising_exec(scripted, monkeypatch, failing: str, error: BaseException) -> None:
    """``exec`` raises *error* for the one script *failing*, as a runner error arrives raw."""
    _answers, ran = scripted
    real_exec = UnixHost.exec

    async def exec_(self, cmd, **kw):
        if cmd == failing:
            ran.append(cmd)
            raise error
        return await real_exec(self, cmd, **kw)

    monkeypatch.setattr(UnixHost, "exec", exec_)


@pytest.mark.asyncio
async def test_a_runner_error_in_the_availability_probe_leaves_the_ladders_unmeasured(
    scripted, monkeypatch
):
    """A refused exec channel arrives as a raw ``OSError``: the survey keeps its other rows."""
    _answers, ran = scripted
    _raising_exec(scripted, monkeypatch, _AVAILABILITY, OSError("Connection refused"))
    s = await engine.run_survey(_unix())
    assert s.nc_rungs is not None
    assert s.nc_rungs.port.state == s.nc_rungs.listener.state == "unmeasured"
    assert "OSError" in s.nc_rungs.port.note, s.nc_rungs.port.note
    assert _row(s, "ssh", 1).state == "supported"
    assert ran == [_AVAILABILITY]


@pytest.mark.asyncio
async def test_a_runner_error_in_a_port_script_is_one_failed_strategy(scripted, monkeypatch):
    answers, _ran = scripted
    answers[_AVAILABILITY] = _ok("ss netstat")
    answers[_port_script("netstat")] = _ok(str(_FREE))
    answers[_listener_script("ss")] = _ok("1")
    answers[_listener_script("netstat")] = _ok("1")
    _raising_exec(scripted, monkeypatch, _port_script("ss"), OSError("Connection refused"))
    s = await engine.run_survey(_unix())
    assert s.nc_rungs is not None
    assert s.nc_rungs.port == engine.NcLadderResult("measured", "netstat")
    assert s.nc_rungs.listener == engine.NcLadderResult("measured", "ss")
    assert [(f.ladder, f.strategy) for f in s.nc_rungs.failed] == [("port", "ss")]
    assert "OSError" in s.nc_rungs.failed[0].reason, s.nc_rungs.failed
    assert _row(s, "ssh", 1).state == "supported"


@pytest.mark.asyncio
async def test_a_failed_availability_probe_keeps_a_declared_pin(scripted):
    answers, ran = scripted
    answers[_AVAILABILITY] = CommandResult(
        status=Status.Failed, value="", retcode=-1, timed_out=True
    )
    s = await engine.run_survey(_unix(nc_options=NcOptions(port_strategy="netstat")))
    assert s.nc_rungs is not None
    assert s.nc_rungs.port == engine.NcLadderResult("declared", "netstat")
    assert s.nc_rungs.listener.state == "unmeasured"
    assert ran == [_AVAILABILITY]


@pytest.mark.asyncio
async def test_a_budget_spent_on_the_port_ladder_keeps_a_declared_listener_pin(
    scripted, monkeypatch
):
    answers, _ran = scripted
    answers[_AVAILABILITY] = _ok("ss")
    hang = _port_script("ss")
    real_exec = UnixHost.exec

    async def exec_(self, cmd, **kw):
        if cmd == hang:
            raise engine._BudgetSpent
        return await real_exec(self, cmd, **kw)

    monkeypatch.setattr(UnixHost, "exec", exec_)
    s = await engine.run_survey(_unix(nc_options=NcOptions(listener_check="netstat")))
    assert s.nc_rungs is not None
    assert s.nc_rungs.port.state == "unmeasured"
    assert s.nc_rungs.listener == engine.NcLadderResult("declared", "netstat")


@pytest.mark.asyncio
async def test_the_strategy_tier_runs_after_every_older_tier(scripted, monkeypatch):
    """Discovery, the SNMP tier and the extra dials all finish before the first strategy script."""
    answers, _ran = scripted
    answers[_AVAILABILITY] = _ok("ss")
    answers[_port_script("ss")] = _ok(str(_FREE))
    answers[_listener_script("ss")] = _ok("1")
    order: list[str] = []
    real_exec, real_dial, real_snmp = UnixHost.exec, engine.dial_direct, engine.check_snmp

    async def exec_(self, cmd, **kw):
        order.append("exec")
        return await real_exec(self, cmd, **kw)

    async def dial(ip, port, *, timeout):
        order.append(f"dial:{port}")
        return await real_dial(ip, port, timeout=timeout)

    async def snmp(**kw):
        order.append("snmp")
        return await real_snmp(**kw)

    monkeypatch.setattr(UnixHost, "exec", exec_)
    monkeypatch.setattr(engine, "dial_direct", dial)
    monkeypatch.setattr(engine, "check_snmp", snmp)
    await engine.run_survey(_unix(), scan_ports="9000")
    first = order.index("exec")
    assert {"snmp", "dial:2222", "dial:9000"} <= set(order[:first]), order
    assert all(step == "exec" for step in order[first:]), order


@pytest.mark.asyncio
async def test_under_user_the_backend_asked_for_is_that_users_and_no_pin_is_offered(
    scripted, monkeypatch, nc_backend
):
    answers, _ran = scripted
    answers[_AVAILABILITY] = _ok("ss")
    answers[_port_script("ss")] = _ok(str(_FREE))
    answers[_listener_script("ss")] = _ok("1")

    class _AsUser:
        def __init__(self, user):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

    monkeypatch.setattr(UnixHost, "as_user", lambda self, user="root", password=None: _AsUser(user))
    host = _unix(creds=[Cred(login="admin", password="pw"), Cred(login="root", password="r")])
    s = await engine.run_survey(host, user="root")
    assert s.user == "root"
    assert s.nc_rungs is not None
    assert s.nc_rungs.port == engine.NcLadderResult("measured", "ss")
    assert s.nc_rungs.listener == engine.NcLadderResult("measured", "ss")
    assert nc_backend.users == ["root"]
    pin = menu_pin(host.valid_terms, host.valid_transfers, s)
    assert not any("port_strategy" in line or "listener_check" in line for line in pin), pin
