"""``otto --show-lab``: the lab as a readable tree, not a Python repr (#685).

The library answers what each host and link IS (:func:`otto.labs.overview.lab_overview`,
plain values); the CLI draws the tree (:func:`otto.cli.show_lab.render_lab`). Both
run against a hand-built lab: no host is contacted.
"""

from pathlib import Path

from rich.console import Console

from otto.host.element import Element
from otto.host.local_host import LocalHost
from otto.host.login_proxy import Cred
from otto.host.product import ShellProduct
from otto.host.unix_host import UnixHost
from otto.labs.loader import Lab
from otto.labs.overview import HostOverview, LinkOverview, lab_overview
from otto.link.model import Link, LinkEndpoint
from otto.logger.mode import LogMode
from otto.result import Result
from otto.utils import Status


class _Agent(ShellProduct):
    async def install(self, host):
        return Result(Status.Success)

    async def uninstall(self, host):
        return Result(Status.Success)

    async def is_installed(self, host):
        return False


def _unix(element: str, ip: str, **kw) -> UnixHost:
    return UnixHost(
        ip=ip,
        element=Element(element),
        creds=[Cred(login="admin", password="secret")],
        log=LogMode.QUIET,
        **kw,
    )


def _lab() -> Lab:
    lab = Lab(name="bench")
    router = _unix("router1", "10.0.0.1", term="ssh", transfer="sftp")
    router.products = [
        _Agent(artifact=Path("/b/agent.tar.gz")),
        _Agent(artifact=Path("/b/kcov.ko")),
    ]
    lab.add_host(router)
    lab.add_host(_unix("dut1", "10.0.0.2", term="telnet", transfer="nc", hop=router.id))
    lab.add_host(LocalHost())
    lab.links.append(
        Link(a=LinkEndpoint(router.id, "eth1"), b=LinkEndpoint("dut1"), protocol="udp", name="ab")
    )
    return lab


def test_the_overview_names_every_host_and_link_as_plain_values() -> None:
    lab = _lab()
    overview = lab_overview(lab)
    assert overview.name == "bench"
    by_id = {h.id: h for h in overview.hosts}
    assert list(by_id) == list(lab.hosts)  # lab order
    router = by_id["router1"]
    assert router == HostOverview(
        id="router1",
        kind="UnixHost",
        address="10.0.0.1",
        transports=["ssh", "sftp"],
        products=["agent.tar.gz", "kcov.ko"],
    )
    assert by_id["dut1"].address == "10.0.0.2 (via router1)"
    assert by_id["dut1"].transports == ["telnet", "nc"]
    assert by_id["local"].kind == "LocalHost"
    assert by_id["local"].address == "this machine"
    assert overview.links == [LinkOverview(id="ab", a="router1:eth1", b="dut1", protocol="udp")]


def _render(width: int = 80, depth: int = 0) -> str:
    from otto.cli.show_lab import render_lab

    console = Console(width=width, record=True, color_system=None)
    render_lab(_lab(), depth=depth, console=console)
    return console.export_text()


def test_the_tree_shows_the_lab_its_hosts_and_links() -> None:
    text = _render()
    # No Python repr.
    assert "Lab(" not in text
    assert "name=" not in text
    for expected in (
        "bench",
        "router1",
        "UnixHost",
        "10.0.0.1",
        "ssh",
        "sftp",
        "agent.tar.gz, kcov.ko",
        "dut1",
        "10.0.0.2 (via router1)",
        "local",
        "LocalHost",
        "this machine",
        "ab",
        "router1:eth1",
        "udp",
    ):
        assert expected in text, (expected, text)


def test_the_tree_fits_80_columns() -> None:
    text = _render(width=80)
    assert max(len(line) for line in text.splitlines()) <= 80, text


def test_lab_depth_limits_how_far_the_tree_opens() -> None:
    shallow = _render(depth=1)
    assert "router1" in shallow
    assert "10.0.0.1" not in shallow  # host details sit one level further down
    assert "10.0.0.1" in _render(depth=0)
    # A closed links branch says how many it holds rather than sitting empty.
    assert "links (1)" in shallow
    assert "router1:eth1" not in shallow


def test_a_lab_with_no_links_has_no_links_branch() -> None:
    from otto.cli.show_lab import render_lab

    lab = Lab(name="solo")
    lab.add_host(LocalHost())
    console = Console(width=80, record=True, color_system=None)
    render_lab(lab, depth=0, console=console)
    text = console.export_text()
    assert "links" not in text
    assert "local" in text
