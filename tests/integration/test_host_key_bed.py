"""SSH host keys checked by host ID against the real lab VMs (test1, test2).

The unit tests (tests/unit/host/test_host_key_client.py) drive asyncssh
servers on loopback; these prove the same behavior against the bed's real
OpenSSH servers, including a target reached through a hop, whose peer
address is the hop's.

Every case writes a known-hosts file of its own under ``tmp_path``, named by
``ssh_options.known_hosts`` on every host it builds, hop included. None of
them reads or writes the user's real ``~/.ssh/known_hosts``.

Topology: otto (dev VM) -> test1 (10.10.200.11) -> test2 (10.10.200.12).
"""

from pathlib import Path

import asyncssh
import pytest

from otto.host.errors import HostKeyRefusal, HostKeyRefusedError
from otto.host.login_proxy import Cred
from otto.host.options import SshOptions
from otto.host.unix_host import UnixHost
from otto.labs.loader import Lab
from otto.logger.mode import LogMode
from otto.utils import Status
from tests._fixtures.labdata import element_for, host_data

pytestmark = [pytest.mark.timeout(60)]


def _pub(key: asyncssh.SSHKey) -> str:
    return key.export_public_key("openssh").decode().strip()


def _lines(path: Path) -> list[str]:
    return [line for line in path.read_text().splitlines() if line.strip()]


def _other_key() -> str:
    """A key no bed host holds: the planted 'changed' key."""
    return _pub(asyncssh.generate_private_key("ssh-ed25519").convert_to_public())


def _bed_host(
    ne: str, known_hosts: Path, *, hop: str | None = None, verify_host_key: bool = True
) -> UnixHost:
    """*ne* from the unix lab data, checking keys against *known_hosts*."""
    data = host_data(ne)
    return UnixHost(
        ip=data["ip"],
        element=element_for(ne),
        creds=[Cred(**c) for c in data["creds"]],
        board=data.get("board"),
        is_virtual=data.get("is_virtual", False),
        term="ssh",
        transfer="scp",
        hop=hop,
        ssh_options=SshOptions(known_hosts=str(known_hosts), verify_host_key=verify_host_key),
        log=LogMode.QUIET,
    )


def _hopped_test2(known_hosts: Path) -> UnixHost:
    """test2 reached through test1, both checking keys against *known_hosts*."""
    lab = Lab(name="host_key_bed")
    lab.add_host(_bed_host("test1", known_hosts))
    target = _bed_host("test2", known_hosts, hop="test1")
    lab.add_host(target)
    return target


@pytest.mark.asyncio
async def test_first_use_records_the_bed_hosts_key_under_its_id(tmp_path):
    """A first connection to test1 records ``test1 <its real key>`` (port 22:
    the bare ID), and a second connection reads it without writing."""
    kh = tmp_path / "known_hosts"
    host = _bed_host("test1", kh)
    try:
        conn = await host.connections.ssh()
        server_key = conn.get_server_host_key()
        assert server_key is not None
    finally:
        await host.close()
    assert _lines(kh) == [f"test1 {_pub(server_key)}"]
    before = kh.read_bytes()

    again = _bed_host("test1", kh)
    try:
        result = await again.exec("true")
    finally:
        await again.close()
    assert result.status == Status.Success, result
    assert kh.read_bytes() == before


@pytest.mark.asyncio
@pytest.mark.hops
async def test_hopped_target_records_by_id_not_by_the_hops_address(tmp_path):
    """Through test1, test2's peer address is test1's; both keys are still
    recorded under their own IDs, and no entry names an address."""
    kh = tmp_path / "known_hosts"
    target = _hopped_test2(kh)
    try:
        result = await target.exec("true")
    finally:
        await target.close()
    assert result.status == Status.Success, result
    names = sorted(line.split(" ", 1)[0] for line in _lines(kh))
    assert names == ["test1", "test2"]
    for ne in ("test1", "test2"):
        assert host_data(ne)["ip"] not in kh.read_text()


@pytest.mark.asyncio
@pytest.mark.hops
async def test_a_changed_target_key_names_the_target_and_a_changed_hop_key_names_the_hop(
    tmp_path,
):
    """The hostile condition on real servers: a different key recorded for the
    target refuses the target; one recorded for the hop refuses the hop."""
    target_changed = tmp_path / "target" / "known_hosts"
    target_changed.parent.mkdir()
    target_changed.write_text(f"test2 {_other_key()}\n")
    target = _hopped_test2(target_changed)
    try:
        with pytest.raises(HostKeyRefusedError, match="otto host test2 forget-key") as info:
            await target.exec("true")
    finally:
        await target.close()
    assert info.value.host_id == "test2"

    hop_changed = tmp_path / "hop" / "known_hosts"
    hop_changed.parent.mkdir()
    hop_changed.write_text(f"test1 {_other_key()}\n")
    target = _hopped_test2(hop_changed)
    try:
        with pytest.raises(HostKeyRefusedError, match="otto host test1 forget-key") as info:
            await target.exec("true")
    finally:
        await target.close()
    assert info.value.host_id == "test1"


@pytest.mark.asyncio
async def test_a_key_type_the_host_does_not_hold_is_refused_as_a_changed_key(tmp_path):
    """The rotation case on a real sshd: test1's entry holds an
    ecdsa-sha2-nistp521 key, a type its sshd does not serve. otto offers that
    type first and every default type after it, so the handshake completes
    with a key test1 does hold, which the entry does not: the changed-key
    refusal with the forget-key hint, not a failed key exchange."""
    kh = tmp_path / "known_hosts"
    planted = asyncssh.generate_private_key("ecdsa-sha2-nistp521").convert_to_public()
    kh.write_text(f"test1 {_pub(planted)}\n")
    before = kh.read_bytes()
    host = _bed_host("test1", kh)
    try:
        with pytest.raises(HostKeyRefusedError, match="otto host test1 forget-key") as info:
            await host.exec("true")
    finally:
        await host.close()
    assert info.value.kind is HostKeyRefusal.CHANGED
    assert kh.read_bytes() == before


@pytest.mark.asyncio
async def test_checking_off_connects_over_a_changed_key_and_leaves_the_file(tmp_path):
    """``verify_host_key = false``: a planted wrong key does not refuse, and the
    file is left byte-for-byte as it was, with no lock file beside it."""
    kh = tmp_path / "known_hosts"
    kh.write_text(f"test1 {_other_key()}\n")
    before = kh.read_bytes()
    host = _bed_host("test1", kh, verify_host_key=False)
    try:
        result = await host.exec("true")
    finally:
        await host.close()
    assert result.status == Status.Success, result
    assert kh.read_bytes() == before
    assert sorted(p.name for p in tmp_path.iterdir()) == ["known_hosts"]
