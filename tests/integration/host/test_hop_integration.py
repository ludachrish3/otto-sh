"""
Integration tests for multi-hop SSH connectivity.

These tests require all three Vagrant VMs to be running::

    vagrant up test1 test2 test3

Topology
--------
- otto (dev VM, 10.10.200.100)
- test1 / carrot (10.10.200.11) — SSH hop
- test2 / tomato (10.10.200.12) — intermediate hop or target
- test3 / pepper (10.10.200.13) — final target for 2-hop chains

Run hop tests::

    pytest -m hops

Skip hop tests::

    pytest -m "not hops"
"""

from pathlib import Path

import pytest
import pytest_asyncio

from otto.configmodule.lab import Lab
from otto.context import OttoContext, set_context
from otto.host import UnixHost
from otto.host.login_proxy import Cred
from otto.logger.mode import LogMode
from otto.utils import Status
from tests.conftest import host_data
from tests.integration.host._transfer_retry import transfer_with_retry

pytestmark = [pytest.mark.timeout(30)]


# ---------------------------------------------------------------------------
# Lab setup — the config module must be populated so that hop resolution
# (configmodule.get_host) can find the hop hosts by ID.
# ---------------------------------------------------------------------------


def _build_host(ne: str, **overrides) -> UnixHost:
    data = host_data(ne)
    return UnixHost(
        ip=data["ip"],
        element=data["element"],
        creds=[Cred(**c) for c in data["creds"]],
        board=data.get("board"),
        is_virtual=data.get("is_virtual", False),
        term=overrides.get("term", data.get("term", "ssh")),
        transfer=overrides.get("transfer", data.get("transfer", "scp")),
        log=LogMode.QUIET,
    )


@pytest.fixture(autouse=True, scope="module")
def _load_lab():
    """Populate the active OttoContext with all lab hosts so hop resolution works.

    Snapshots/restores the contextvar so the module's lab doesn't leak past the
    module (the function-scoped _reset_otto_context preserves it *within* each
    test rather than forcing None).
    """
    from otto.context import _active

    lab = Lab(name="hops_test")
    for ne in ("carrot", "tomato", "pepper"):
        lab.add_host(_build_host(ne))
    snapshot = _active.get()
    set_context(OttoContext(lab=lab))
    yield
    _active.set(snapshot)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def single_hop_ssh():
    """Target reached through one SSH hop: otto -> carrot -> tomato (SSH)."""
    data = host_data("tomato")
    h = UnixHost(
        ip=data["ip"],
        element=data["element"],
        creds=[Cred(**c) for c in data["creds"]],
        board=data.get("board"),
        is_virtual=True,
        term="ssh",
        transfer="scp",
        hop="carrot_seed",
        log=LogMode.QUIET,
    )
    yield h
    await h.close()


@pytest_asyncio.fixture
async def single_hop_telnet():
    """Target reached via SSH hop, using telnet to the target: otto -> carrot -> tomato (telnet)."""
    data = host_data("tomato")
    h = UnixHost(
        ip=data["ip"],
        element=data["element"],
        creds=[Cred(**c) for c in data["creds"]],
        board=data.get("board"),
        is_virtual=True,
        term="telnet",
        transfer="ftp",
        hop="carrot_seed",
        log=LogMode.QUIET,
    )
    yield h
    await h.close()


@pytest_asyncio.fixture
async def two_hop_ssh():
    """Target reached through two SSH hops: otto -> carrot -> tomato -> pepper.

    The intermediate hop (tomato) must itself have a hop configured so that
    the recursive tunnel factory chains them.
    """
    # Reconfigure tomato in the lab with a hop through carrot
    lab = Lab(name="hops_test_2hop")
    lab.add_host(_build_host("carrot"))
    tomato_data = host_data("tomato")
    tomato_with_hop = UnixHost(
        ip=tomato_data["ip"],
        element=tomato_data["element"],
        creds=[Cred(**c) for c in tomato_data["creds"]],
        board=tomato_data.get("board"),
        is_virtual=True,
        term="ssh",
        transfer="scp",
        hop="carrot_seed",
        log=LogMode.QUIET,
    )
    lab.add_host(tomato_with_hop)
    lab.add_host(_build_host("pepper"))
    set_context(OttoContext(lab=lab))

    pepper_data = host_data("pepper")
    h = UnixHost(
        ip=pepper_data["ip"],
        element=pepper_data["element"],
        creds=[Cred(**c) for c in pepper_data["creds"]],
        board=pepper_data.get("board"),
        is_virtual=True,
        term="ssh",
        transfer="scp",
        hop="tomato_seed",
        log=LogMode.QUIET,
    )
    yield h
    await h.close()

    # Restore the single-hop lab for subsequent tests
    lab = Lab(name="hops_test")
    for ne in ("carrot", "tomato", "pepper"):
        lab.add_host(_build_host(ne))
    set_context(OttoContext(lab=lab))


# ---------------------------------------------------------------------------
# Single-hop SSH tests
# ---------------------------------------------------------------------------


class TestSingleHopSsh:
    @pytest.mark.asyncio
    @pytest.mark.hops
    async def test_echo_through_hop(self, single_hop_ssh: UnixHost):
        result = (await single_hop_ssh.run("echo hello_through_hop")).only
        assert result.status == Status.Success
        assert "hello_through_hop" in result.value

    @pytest.mark.asyncio
    @pytest.mark.hops
    async def test_hostname_through_hop(self, single_hop_ssh: UnixHost):
        result = (await single_hop_ssh.run("hostname")).only
        assert result.status == Status.Success
        # Should be test2's hostname, not test1 (the hop)
        assert "test2" in result.value

    @pytest.mark.asyncio
    @pytest.mark.hops
    async def test_multiple_commands_through_hop(self, single_hop_ssh: UnixHost):
        result = await single_hop_ssh.run(["echo first", "echo second"])
        assert result.status == Status.Success
        assert "first" in result[0].value
        assert "second" in result[1].value

    @pytest.mark.asyncio
    @pytest.mark.hops
    async def test_state_persists_through_hop(self, single_hop_ssh: UnixHost):
        await single_hop_ssh.run("export HOP_VAR=works")
        result = (await single_hop_ssh.run("echo $HOP_VAR")).only
        assert result.status == Status.Success
        assert "works" in result.value


# ---------------------------------------------------------------------------
# Single-hop telnet target tests
# ---------------------------------------------------------------------------


class TestSingleHopTelnet:
    @pytest.mark.asyncio
    @pytest.mark.hops
    async def test_telnet_through_ssh_hop(self, single_hop_telnet: UnixHost):
        """Reach a telnet target through an SSH hop (port forwarding)."""
        result = (await single_hop_telnet.run("echo telnet_via_hop")).only
        assert result.status == Status.Success
        assert "telnet_via_hop" in result.value

    @pytest.mark.asyncio
    @pytest.mark.hops
    async def test_telnet_hostname_through_hop(self, single_hop_telnet: UnixHost):
        result = (await single_hop_telnet.run("hostname")).only
        assert result.status == Status.Success
        assert "test2" in result.value


# ---------------------------------------------------------------------------
# File transfer through single hop
#
# NOTE: Transfers traverse an SSH hop via asyncssh. When the intermediate hop
# or the target SSH daemon stalls mid-protocol, an ``await`` on the transfer
# can hang indefinitely — kernel TCP keepalive on the SSH socket won't fire
# for hours. get/put are wrapped in ``transfer_with_retry`` so each
# attempt is bounded by ``asyncio.wait_for`` and retried once before failing.
# ---------------------------------------------------------------------------


class TestFileTransferThroughHop:
    @pytest.mark.asyncio
    @pytest.mark.hops
    async def test_scp_get_through_hop(self, single_hop_ssh: UnixHost, tmp_path: Path):
        """Download a file from the target through an SSH hop via SCP."""
        result = (await single_hop_ssh.run("hostname")).only
        expected = result.value.strip()

        res = await transfer_with_retry(
            lambda: single_hop_ssh.get([Path("/etc/hostname")], tmp_path)
        )
        assert res.status == Status.Success, f"SCP get failed: {res.msg}"
        assert (tmp_path / "hostname").read_text().strip() == expected

    @pytest.mark.asyncio
    @pytest.mark.hops
    async def test_scp_put_through_hop(self, single_hop_ssh: UnixHost, tmp_path: Path):
        """Upload a file to the target through an SSH hop via SCP."""
        content = "hop_transfer_test"
        src = tmp_path / "hop_upload.txt"
        src.write_text(content)
        remote_path = "/tmp/hop_upload.txt"

        res = await transfer_with_retry(lambda: single_hop_ssh.put([src], Path("/tmp")))
        assert res.status == Status.Success, f"SCP put failed: {res.msg}"

        result = (await single_hop_ssh.run(f"cat {remote_path}")).only
        assert content in result.value
        await single_hop_ssh.run(f"rm -f {remote_path}")

    @pytest.mark.asyncio
    @pytest.mark.hops
    async def test_sftp_get_through_hop(self, tmp_path: Path):
        """Download a file through an SSH hop via SFTP."""
        data = host_data("tomato")
        h = UnixHost(
            ip=data["ip"],
            element=data["element"],
            creds=[Cred(**c) for c in data["creds"]],
            board=data.get("board"),
            is_virtual=True,
            term="ssh",
            transfer="sftp",
            hop="carrot_seed",
            log=LogMode.QUIET,
        )
        try:
            result = (await h.run("hostname")).only
            expected = result.value.strip()

            res = await transfer_with_retry(lambda: h.get([Path("/etc/hostname")], tmp_path))
            assert res.status == Status.Success, f"SFTP get failed: {res.msg}"
            assert (tmp_path / "hostname").read_text().strip() == expected
        finally:
            await h.close()

    @pytest.mark.asyncio
    @pytest.mark.hops
    async def test_ftp_put_through_hop(self, tmp_path: Path):
        """Upload a file through an SSH hop via FTP (port-forwarded)."""
        data = host_data("tomato")
        h = UnixHost(
            ip=data["ip"],
            element=data["element"],
            creds=[Cred(**c) for c in data["creds"]],
            board=data.get("board"),
            is_virtual=True,
            term="ssh",
            transfer="ftp",
            hop="carrot_seed",
            log=LogMode.QUIET,
        )
        try:
            content = "ftp_hop_test"
            src = tmp_path / "ftp_hop_upload.txt"
            src.write_text(content)
            remote_path = "/tmp/ftp_hop_upload.txt"

            res = await transfer_with_retry(lambda: h.put([src], Path("/tmp")))
            assert res.status == Status.Success, f"FTP put failed: {res.msg}"

            result = (await h.run(f"cat {remote_path}")).only
            assert content in result.value
            await h.run(f"rm -f {remote_path}")
        finally:
            await h.close()

    @pytest.mark.asyncio
    @pytest.mark.hops
    async def test_ftp_get_through_hop(self, tmp_path: Path):
        """Download a file through an SSH hop via FTP (port-forwarded)."""
        data = host_data("tomato")
        h = UnixHost(
            ip=data["ip"],
            element=data["element"],
            creds=[Cred(**c) for c in data["creds"]],
            board=data.get("board"),
            is_virtual=True,
            term="ssh",
            transfer="ftp",
            hop="carrot_seed",
            log=LogMode.QUIET,
        )
        try:
            result = (await h.run("hostname")).only
            expected = result.value.strip()

            res = await transfer_with_retry(lambda: h.get([Path("/etc/hostname")], tmp_path))
            assert res.status == Status.Success, f"FTP get failed: {res.msg}"
            assert (tmp_path / "hostname").read_text().strip() == expected
        finally:
            await h.close()

    @pytest.mark.asyncio
    @pytest.mark.hops
    @pytest.mark.retry(3)
    async def test_nc_put_through_hop(self, tmp_path: Path):
        """Upload a file through an SSH hop via netcat (port-forwarded)."""
        data = host_data("tomato")
        h = UnixHost(
            ip=data["ip"],
            element=data["element"],
            creds=[Cred(**c) for c in data["creds"]],
            board=data.get("board"),
            is_virtual=True,
            term="ssh",
            transfer="nc",
            hop="carrot_seed",
            log=LogMode.QUIET,
        )
        try:
            content = "nc_hop_put_test"
            src = tmp_path / "nc_hop_upload.txt"
            src.write_text(content)
            remote_path = "/tmp/nc_hop_upload.txt"

            res = await transfer_with_retry(lambda: h.put([src], Path("/tmp")))
            assert res.status == Status.Success, f"NC put failed: {res.msg}"

            # Verify via SSH session (switch to scp for the read-back)
            result = (await h.run(f"cat {remote_path}")).only
            assert content in result.value
            await h.run(f"rm -f {remote_path}")
        finally:
            await h.close()

    @pytest.mark.asyncio
    @pytest.mark.hops
    @pytest.mark.retry(3)
    async def test_nc_get_through_hop(self, tmp_path: Path):
        """Download a file through an SSH hop via netcat (reversed-listener)."""
        data = host_data("tomato")
        h = UnixHost(
            ip=data["ip"],
            element=data["element"],
            creds=[Cred(**c) for c in data["creds"]],
            board=data.get("board"),
            is_virtual=True,
            term="ssh",
            transfer="nc",
            hop="carrot_seed",
            log=LogMode.QUIET,
        )
        try:
            result = (await h.run("hostname")).only
            expected = result.value.strip()

            res = await transfer_with_retry(lambda: h.get([Path("/etc/hostname")], tmp_path))
            assert res.status == Status.Success, f"NC get failed: {res.msg}"
            assert (tmp_path / "hostname").read_text().strip() == expected
        finally:
            await h.close()


# ---------------------------------------------------------------------------
# Two-hop SSH chain: otto -> carrot -> tomato -> pepper
#
# NOTE: The two-hop chain multiplies the odds of an asyncssh stall since any
# of the three SSH daemons can pause mid-protocol. Transfers are wrapped in
# ``transfer_with_retry`` for the same reason as the single-hop class above.
# ---------------------------------------------------------------------------


class TestTwoHopChain:
    @pytest.mark.asyncio
    @pytest.mark.hops
    async def test_echo_through_two_hops(self, two_hop_ssh: UnixHost):
        result = (await two_hop_ssh.run("echo two_hop_success")).only
        assert result.status == Status.Success
        assert "two_hop_success" in result.value

    @pytest.mark.asyncio
    @pytest.mark.hops
    async def test_hostname_through_two_hops(self, two_hop_ssh: UnixHost):
        """Command should run on test3 (pepper), not the intermediate hops."""
        result = (await two_hop_ssh.run("hostname")).only
        assert result.status == Status.Success
        assert "test3" in result.value

    @pytest.mark.asyncio
    @pytest.mark.hops
    async def test_scp_get_through_two_hops(self, two_hop_ssh: UnixHost, tmp_path: Path):
        """Download a file through a 2-hop SSH chain."""
        result = (await two_hop_ssh.run("hostname")).only
        expected = result.value.strip()

        res = await transfer_with_retry(lambda: two_hop_ssh.get([Path("/etc/hostname")], tmp_path))
        assert res.status == Status.Success, f"SCP get through 2 hops failed: {res.msg}"
        assert (tmp_path / "hostname").read_text().strip() == expected

    @pytest.mark.asyncio
    @pytest.mark.hops
    async def test_scp_put_through_two_hops(self, two_hop_ssh: UnixHost, tmp_path: Path):
        """Upload a file through a 2-hop SSH chain."""
        content = "two_hop_upload_test"
        src = tmp_path / "two_hop_upload.txt"
        src.write_text(content)
        remote_path = "/tmp/two_hop_upload.txt"

        res = await transfer_with_retry(lambda: two_hop_ssh.put([src], Path("/tmp")))
        assert res.status == Status.Success, f"SCP put through 2 hops failed: {res.msg}"

        result = (await two_hop_ssh.run(f"cat {remote_path}")).only
        assert content in result.value
        await two_hop_ssh.run(f"rm -f {remote_path}")
