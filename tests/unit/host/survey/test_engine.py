"""The engine: tiers in order, nothing guessed, budgets injected, resolution by the spec's rule."""

import asyncio
from pathlib import Path

import pytest

from otto.host.command_frame import ZephyrFrame
from otto.host.element import Element
from otto.host.embedded_host import EmbeddedHost
from otto.host.errors import UnsupportedOnUserlandError
from otto.host.login_proxy import Cred
from otto.host.options import ConsoleOptions, FtpOptions, SnmpOptions, SshOptions, TelnetOptions
from otto.host.survey import engine
from otto.host.survey.dial import DialOutcome
from otto.host.survey.inventory import Inventory, Listener, parse_inventory
from otto.host.survey.login import LoginOutcome
from otto.host.survey.report import menu_pin
from otto.host.survey.sweep import SweepRow
from otto.host.survey.verdict import ProtocolVerdict
from otto.host.unix_host import UnixHost
from otto.logger.mode import LogMode

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
    return UnixHost(element=Element("u1"), name="u1", ip="127.0.0.1", log=LogMode.QUIET, **extra)


class _Userland:
    def __init__(self, *, elevation="sudo", scp="present", nc="present", settled=True):
        self.elevation, self._scp, self._nc, self._settled = elevation, scp, nc, settled
        self.resolved = False

    async def resolve(self):
        self.resolved = True

    def is_settled(self, _name):
        return self._settled

    def has_applet(self, name):
        return {"scp": self._scp, "nc": self._nc}[name]


def _row(survey, protocol, port):
    return next(v for v in survey.verdicts if v.protocol == protocol and v.port == port)


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
        conn_mod.TERM_BACKENDS.unregister("quiet-console")

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
        # sftp needs ssh regardless of term, so it never becomes "supported"
        # under a console term -- leaving it in valid_transfers would fire a
        # genuine (unrelated) drift pin and mask the one this test is for.
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


@pytest.mark.asyncio
async def test_elevation_is_requested_only_with_a_mechanism_and_a_password(seams, monkeypatch):
    """Mutation: elevate whenever sudo exists, even a passwordless cred otto cannot use."""
    host = _unix(creds=[Cred(login="admin")])
    await engine.run_survey(host)
    assert seams["inventory"] == [False]
    seams["inventory"].clear()
    await engine.run_survey(_unix())
    assert seams["inventory"] == [True]


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
        element=Element("z1"),
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
