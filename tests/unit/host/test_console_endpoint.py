"""``RemoteHost.console_endpoint``: the server host record feeds the hop machinery."""

import pytest

from otto.config.lab import Lab
from otto.host.element import Element
from otto.host.embedded_host import ZephyrHost
from otto.host.errors import ConsoleError
from otto.host.login_proxy import Cred
from otto.host.options import ConsoleOptions
from otto.host.unix_host import UnixHost
from otto.logger.mode import LogMode


def _lab(**server_extra):
    lab = Lab(name="t")
    server = UnixHost(
        ip="10.10.200.11",
        element=Element("test1"),
        creds=[Cred(login="vagrant", password="vagrant")],
        log=LogMode.QUIET,
        **server_extra,
    )
    lab.add_host(server)
    return lab


def _console_host(lab, *, element="test2", ip="10.10.200.12", **opts):
    dev = UnixHost(
        ip=ip,
        element=Element(element),
        creds=[Cred(login="test", password="Password1")],
        log=LogMode.QUIET,
        valid_terms=["console"],
        term="console",
        console_options=ConsoleOptions(**opts),
    )
    lab.add_host(dev)
    return dev


@pytest.fixture
def ssh_connects(monkeypatch):
    """Record every hop-tunnel SSH connect as ``(tag, host, username)``; no network."""
    seen = []

    async def fake(tag, host, kwargs, options, *, apply_post_connect=True):
        seen.append((tag, host, kwargs["username"]))
        return object()

    monkeypatch.setattr("otto.host.remote_host.connect_and_describe", fake)
    return seen


@pytest.mark.asyncio
async def test_ssh_dial_tunnels_into_the_server(ssh_connects):
    lab = _lab()
    dev = _console_host(lab, server="test1", port=4001)
    ep = dev.console_endpoint()
    assert ep.server_ip == "10.10.200.11"
    assert ep.hop is not None
    await ep.hop.get_tunnel()
    # INTO the server, as the server's own ssh cred.
    assert ssh_connects == [("test2 via test1", "10.10.200.11", "vagrant")]


def test_direct_dial_with_a_hopless_server_has_no_hop():
    lab = _lab()
    dev = _console_host(lab, server="test1", port=4001, dial="direct")
    assert dev.console_endpoint().hop is None


@pytest.mark.asyncio
async def test_direct_dial_through_a_hopped_server_tunnels_into_the_servers_hop(ssh_connects):
    lab = _lab(hop="jump")
    lab.add_host(
        UnixHost(
            ip="10.10.200.1",
            element=Element("jump"),
            creds=[Cred(login="jumper", password="pw")],
            log=LogMode.QUIET,
        )
    )
    dev = _console_host(lab, server="test1", port=4001, dial="direct")
    ep = dev.console_endpoint()
    assert ep.server_ip == "10.10.200.11"
    assert ep.hop is not None
    # A fresh transport owned by the console endpoint, not the server's own.
    assert ep.hop is not lab.hosts["test1"]._connections._hop
    await ep.hop.get_tunnel()
    # Through the server's HOP (the forward then dials the server's ip), never
    # into the server itself.
    assert ssh_connects == [("test1 via jump", "10.10.200.1", "jumper")]


def test_the_hosts_own_hop_is_never_consulted():
    lab = _lab()
    dev = UnixHost(
        ip="10.10.200.12",
        element=Element("test2"),
        creds=[Cred(login="test", password="Password1")],
        log=LogMode.QUIET,
        hop="nowhere",
        valid_terms=["console"],
        term="console",
        console_options=ConsoleOptions(server="test1", port=4001, dial="direct"),
    )
    lab.add_host(dev)
    assert dev.console_endpoint().hop is None


def test_a_console_server_that_is_the_host_itself_is_refused():
    lab = _lab()
    dev = _console_host(lab, element="test1b", ip="10.10.200.11", server="test1b", port=4001)
    with pytest.raises(ConsoleError, match=r"test1b: console test1b:4001 .*its own console server"):
        dev.console_endpoint()


def test_an_unknown_server_names_the_lab():
    lab = _lab()
    dev = _console_host(lab, server="ghost", port=4001)
    with pytest.raises(KeyError, match="console server 'ghost' not in lab") as excinfo:
        dev.console_endpoint()
    assert "test2" in str(excinfo.value)


@pytest.mark.asyncio
async def test_the_hop_cycle_guard_fires_for_a_console_server_chain():
    """The console server's own hop chain loops: the shared cycle check still fires."""
    lab = _lab(hop="test9")
    lab.add_host(
        UnixHost(
            ip="10.10.200.19",
            element=Element("test9"),
            creds=[Cred(login="vagrant", password="vagrant")],
            log=LogMode.QUIET,
            hop="test1",
        )
    )
    dev = _console_host(lab, server="test1", port=4001)
    ep = dev.console_endpoint()
    assert ep.hop is not None
    with pytest.raises(ValueError, match="Circular hop detected"):
        await ep.hop.get_tunnel()


def test_embedded_console_forces_login_off():
    z = ZephyrHost(
        ip="127.0.0.1",
        element=Element("z"),
        valid_terms=["console"],
        term="console",
        console_options=ConsoleOptions(server="test4", port=2323, login=True),
    )
    assert z._connections.console_options.login is False


def test_unix_console_options_carry_the_os_profile_prompts():
    lab = _lab()
    dev = _console_host(lab, server="test1", port=4001)
    opts = dev._connections.console_options
    assert opts.login_prompt is not None
    assert opts.password_prompt is not None
