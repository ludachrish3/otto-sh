"""SSH host keys checked by otto host ID at the connect seam, against real asyncssh servers.

Every case runs a real handshake against an asyncssh server on 127.0.0.1
(:func:`tests._fixtures.loopback_servers.ssh_server`) with a generated host
key, and plants the hostile condition in a known-hosts file under
``tmp_path``: a different key recorded for the host ID, a key of another
type, an address-keyed entry, a revocation, an unwritable directory, a
certificate no CA line trusts. The server records every password it is
offered, so a refusal is proven to happen before any credential leaves.

No case touches the user's real ``~/.ssh/known_hosts``: every
``SshOptions`` here names a file under ``tmp_path``.
"""

import contextlib
import logging
import os
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any, ClassVar

import asyncssh
import pytest
import typer
from pydantic import SecretStr

from otto.host import known_hosts
from otto.host.connections import connect_and_describe
from otto.host.element import Element
from otto.host.errors import HostKeyRefusal, HostKeyRefusedError, KnownHostsUnwritableError
from otto.host.factory import create_host_from_dict
from otto.host.host_key_client import HostKeyCheck, HostKeyClient
from otto.host.known_hosts import KnownHosts
from otto.host.options import SshOptions
from otto.host.transport import teardown_ssh_connection
from otto.host.unix_host import UnixHost
from otto.logger.mode import LogMode
from otto.models.base import Cred
from tests._fixtures.loopback_servers import Offered, ssh_server

PASSWORD = "PLANTED-SECRET-7f3a"
"""The lab password: the servers accept it, and a refusal must never send it."""


def _key(alg: str = "ssh-ed25519") -> asyncssh.SSHKey:
    return asyncssh.generate_private_key(alg)


def _pub(key: asyncssh.SSHKey) -> str:
    return key.convert_to_public().export_public_key("openssh").decode().strip()


def _lines(path: Path) -> list[str]:
    return [line for line in path.read_text().splitlines() if line.strip()]


def _options(port: int, known_hosts: Path, **fields: Any) -> SshOptions:
    """Password-only options on *port* that check keys against *known_hosts*."""
    return SshOptions(
        port=port,
        known_hosts=str(known_hosts),
        client_keys=[],
        preferred_auth="password",
        connect_timeout=10.0,
        **fields,
    )


@contextlib.asynccontextmanager
async def _connected(
    port: int, known_hosts: Path, alias: str = "router1", **fields: Any
) -> AsyncIterator[asyncssh.SSHClientConnection]:
    """One connection through the connect seam, torn down when the block ends."""
    conn = await connect_and_describe(
        "t",
        "127.0.0.1",
        {"username": "admin", "tunnel": None},
        _options(port, known_hosts, **fields),
        password=SecretStr(PASSWORD),
        alias=alias,
    )
    try:
        yield conn
    finally:
        await teardown_ssh_connection(conn)


async def _connect_once(port: int, known_hosts: Path, alias: str = "router1", **fields: Any):
    async with _connected(port, known_hosts, alias, **fields):
        pass


def _host_dict(port: int, known_hosts: Path) -> dict[str, Any]:
    return {
        "ip": "127.0.0.1",
        "creds": [{"login": "admin", "password": PASSWORD}],
        "ssh_options": {
            "port": port,
            "known_hosts": str(known_hosts),
            "client_keys": [],
            "preferred_auth": "password",
            "connect_timeout": 10.0,
        },
    }


def _unix_host(element: str, port: int, known_hosts: Path, **fields: Any) -> UnixHost:
    return UnixHost(
        ip="127.0.0.1",
        element=Element(element),
        creds=[Cred(login="admin", password=PASSWORD)],
        ssh_options=_options(port, known_hosts),
        log=LogMode.QUIET,
        **fields,
    )


def _cannot_enforce_permissions() -> bool:
    return hasattr(os, "geteuid") and os.geteuid() == 0


# ---------------------------------------------------------------------------
# First use, and the entry it writes
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_first_use_records_by_host_id(tmp_path):
    """A host with no entry connects and its key is recorded under the host ID,
    in the ``[id]:port`` form for a non-22 port, never under the address."""
    key = _key()
    kh = tmp_path / "known_hosts"
    offered = Offered()
    async with ssh_server(offered, host_keys=[key], accepted_password=PASSWORD) as port:
        await _connect_once(port, kh)
    assert _lines(kh) == [f"[router1]:{port} {_pub(key)}"]
    assert "127.0.0.1" not in kh.read_text()
    assert len(offered.passwords) == 1


@pytest.mark.asyncio
async def test_changed_key_refused_before_password_sent(tmp_path):
    """The hostile condition: the address now answers with a different key.
    The connection is refused naming the host and the fix, and the server
    never sees the password."""
    kh = tmp_path / "known_hosts"
    offered = Offered()
    async with ssh_server(offered, host_keys=[_key()], accepted_password=PASSWORD) as port:
        kh.write_text(f"[router1]:{port} {_pub(_key())}\n")
        before = kh.read_bytes()
        with pytest.raises(HostKeyRefusedError, match="otto host router1 forget-key") as info:
            await _connect_once(port, kh)
    assert offered.passwords == []
    assert kh.read_bytes() == before
    refused = info.value
    assert refused.host_id == "router1"
    assert refused.address == "127.0.0.1"
    assert refused.path == str(kh)
    assert refused.hint == "otto host router1 forget-key"
    assert refused.kind is HostKeyRefusal.CHANGED
    assert str(kh) in str(refused)
    assert "127.0.0.1" in str(refused)


@pytest.mark.asyncio
async def test_address_entry_with_other_key_is_ignored(tmp_path):
    """Entries the user's own ``ssh`` wrote under the address (plain and
    bracketed) hold another key; otto neither refuses over them nor edits them."""
    key = _key()
    kh = tmp_path / "known_hosts"
    offered = Offered()
    async with ssh_server(offered, host_keys=[key], accepted_password=PASSWORD) as port:
        address_lines = [f"127.0.0.1 {_pub(_key())}", f"[127.0.0.1]:{port} {_pub(_key())}"]
        kh.write_text("".join(f"{line}\n" for line in address_lines))
        await _connect_once(port, kh)
    assert _lines(kh) == [*address_lines, f"[router1]:{port} {_pub(key)}"]


@pytest.mark.asyncio
async def test_alias_is_host_id_under_shared_preferences_selector(tmp_path):
    """Two hosts whose ssh_options come from one ``".*"`` selector share every
    option value; each still records under its own host ID, never the
    selector's options or the address."""
    key = _key()
    kh = tmp_path / "known_hosts"
    offered = Offered()
    async with ssh_server(offered, host_keys=[key], accepted_password=PASSWORD) as port:
        preferences = {".*": {"ssh_options": _host_dict(port, kh)["ssh_options"]}}
        hosts = [
            create_host_from_dict(
                {"ip": "127.0.0.1", "creds": [{"login": "admin", "password": PASSWORD}]},
                preferences=preferences,
                element=Element(element),
            )
            for element in ("router1", "router2")
        ]
        assert hosts[0].ssh_options == hosts[1].ssh_options
        for host in hosts:
            try:
                await host.connections.ssh()
            finally:
                await host.close()
    assert sorted(_lines(kh)) == [f"[router1]:{port} {_pub(key)}", f"[router2]:{port} {_pub(key)}"]


# ---------------------------------------------------------------------------
# Hops
# ---------------------------------------------------------------------------


@contextlib.asynccontextmanager
async def _hop_pair(
    hop_offered: Offered, target_offered: Offered, hop_key: Any, target_key: Any
) -> AsyncIterator[list[int]]:
    """A forwarding hop server and a target server; yields ``[hop_port, target_port]``."""
    async with (
        ssh_server(
            hop_offered, host_keys=[hop_key], accepted_password=PASSWORD, forwarding=True
        ) as hop_port,
        ssh_server(target_offered, host_keys=[target_key], accepted_password=PASSWORD) as port,
    ):
        yield [hop_port, port]


def _hopped_target(kh: Path, hop_port: int, port: int) -> UnixHost:
    from otto.labs.loader import Lab

    hop = _unix_host("hop1", hop_port, kh)
    target = _unix_host("router1", port, kh, hop="hop1")
    lab = Lab(name="host_key_hops")
    lab.add_host(hop)
    lab.add_host(target)
    return target


@pytest.mark.asyncio
async def test_hop_changed_key_names_the_hop(tmp_path):
    """A hop whose key changed is refused by the hop's ID, not the target's,
    and neither the hop nor the target sees a password."""
    kh = tmp_path / "known_hosts"
    hop_offered, target_offered = Offered(), Offered()
    async with _hop_pair(hop_offered, target_offered, _key(), _key()) as (hp, port):
        kh.write_text(f"[hop1]:{hp} {_pub(_key())}\n")
        target = _hopped_target(kh, hp, port)
        try:
            with pytest.raises(HostKeyRefusedError, match="otto host hop1 forget-key") as info:
                await target.connections.ssh()
        finally:
            await target.close()
    assert info.value.host_id == "hop1"
    assert "router1" not in str(info.value)
    assert hop_offered.passwords == []
    assert target_offered.passwords == []


@pytest.mark.asyncio
async def test_non_default_port_through_hop_records_bracket_form(tmp_path):
    """Through a hop, the target's peer address is the hop's; the target's
    key is still recorded as ``[router1]:port`` and the hop's as
    ``[hop1]:port``, and no entry names an address."""
    hop_key, target_key = _key(), _key()
    kh = tmp_path / "known_hosts"
    hop_offered, target_offered = Offered(), Offered()
    async with _hop_pair(hop_offered, target_offered, hop_key, target_key) as (hp, port):
        target = _hopped_target(kh, hp, port)
        try:
            await target.connections.ssh()
        finally:
            await target.close()
    assert sorted(_lines(kh)) == sorted(
        [f"[hop1]:{hp} {_pub(hop_key)}", f"[router1]:{port} {_pub(target_key)}"]
    )
    assert "127.0.0.1" not in kh.read_text()


# ---------------------------------------------------------------------------
# Changed keys in other shapes
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_key_type_rotation_maps_to_changed_message(tmp_path):
    """The host switched key type: the entry holds an ecdsa key, the server has
    only an ed25519 one. otto offers the recorded type first and every
    default type after it, so the handshake reaches the new key, which the
    entry does not hold: the changed-key refusal, through asyncssh's
    ``HostKeyNotVerifiable``, before any password."""
    kh = tmp_path / "known_hosts"
    offered = Offered()
    async with ssh_server(offered, host_keys=[_key()], accepted_password=PASSWORD) as port:
        kh.write_text(f"[router1]:{port} {_pub(_key('ecdsa-sha2-nistp256'))}\n")
        with pytest.raises(HostKeyRefusedError, match="otto host router1 forget-key") as info:
            await _connect_once(port, kh)
    assert offered.passwords == []
    assert info.value.kind is HostKeyRefusal.CHANGED
    assert isinstance(info.value.__cause__, asyncssh.HostKeyNotVerifiable)


@pytest.mark.asyncio
async def test_a_failed_key_exchange_with_an_entry_is_not_a_host_key_refusal(tmp_path):
    """A configured algorithm list the server cannot meet fails the key
    exchange. The host has an entry, but nothing about its key was judged:
    the failure stays asyncssh's own, never the changed-key refusal."""
    key = _key()
    kh = tmp_path / "known_hosts"
    offered = Offered()
    async with ssh_server(offered, host_keys=[key], accepted_password=PASSWORD) as port:
        kh.write_text(f"[router1]:{port} {_pub(key)}\n")
        with pytest.raises(asyncssh.KeyExchangeFailed):
            await _connect_once(port, kh, server_host_key_algs=["ssh-rsa"])
    assert offered.passwords == []


@pytest.mark.asyncio
@pytest.mark.parametrize("form", ["bare", "port"])
async def test_revoked_key_refused(form, tmp_path):
    """A key revoked for the host ID, under either entry form, is refused and
    not recorded, before any password. The message says it is revoked and
    does not advise forget-key, which keeps ``@revoked`` lines."""
    key = _key()
    kh = tmp_path / "known_hosts"
    offered = Offered()
    async with ssh_server(offered, host_keys=[key], accepted_password=PASSWORD) as port:
        name = "router1" if form == "bare" else f"[router1]:{port}"
        kh.write_text(f"@revoked {name} {_pub(key)}\n")
        before = kh.read_bytes()
        with pytest.raises(HostKeyRefusedError, match="revoked") as info:
            await _connect_once(port, kh)
    assert offered.passwords == []
    assert kh.read_bytes() == before
    assert info.value.kind is HostKeyRefusal.REVOKED
    assert f"The key is revoked in {kh}" in str(info.value)
    assert "forget-key" not in str(info.value)


@pytest.mark.asyncio
async def test_revoked_key_beside_a_recorded_key_gets_no_forget_advice(tmp_path):
    """The host ID has a recorded key AND the offered key is revoked. Forgetting
    the entry would not help (the ``@revoked`` line stays), so the refusal is
    the revoked one, not the changed one."""
    key = _key()
    kh = tmp_path / "known_hosts"
    offered = Offered()
    async with ssh_server(offered, host_keys=[key], accepted_password=PASSWORD) as port:
        kh.write_text(f"[router1]:{port} {_pub(_key())}\n@revoked router1 {_pub(key)}\n")
        with pytest.raises(HostKeyRefusedError) as info:
            await _connect_once(port, kh)
    assert offered.passwords == []
    assert info.value.kind is HostKeyRefusal.REVOKED
    assert "forget-key" not in str(info.value)


@pytest.mark.asyncio
async def test_a_host_id_containing_revoked_still_gets_the_changed_key_refusal(tmp_path):
    """The revoked verdict is asyncssh's reason, read as a prefix: a host whose
    ID happens to contain "revoked" and whose key changed is refused as a
    changed key, with the forget-key advice."""
    kh = tmp_path / "known_hosts"
    offered = Offered()
    async with ssh_server(offered, host_keys=[_key()], accepted_password=PASSWORD) as port:
        kh.write_text(f"[revoked-box]:{port} {_pub(_key())}\n")
        with pytest.raises(HostKeyRefusedError) as info:
            await _connect_once(port, kh, alias="revoked-box")
    assert offered.passwords == []
    assert info.value.kind is HostKeyRefusal.CHANGED
    assert "otto host revoked-box forget-key" in str(info.value)


@pytest.mark.asyncio
async def test_a_key_revoked_between_handshake_and_auth_is_refused_not_recorded(
    tmp_path, monkeypatch
):
    """The handshake found no entry and accepted the key for first use; before
    ``begin_auth`` records it, the key is revoked. The record judges revocations
    too, so the connection is refused as revoked and no password is sent."""
    key = _key()
    kh = tmp_path / "known_hosts"
    revocation = f"@revoked router1 {_pub(key)}"
    real_locked = KnownHosts._locked

    def locked_after_a_revocation(self: KnownHosts):
        with real_locked(self):
            with kh.open("a") as out:
                out.write(f"{revocation}\n")
            yield

    monkeypatch.setattr(KnownHosts, "_locked", contextlib.contextmanager(locked_after_a_revocation))
    offered = Offered()
    async with ssh_server(offered, host_keys=[key], accepted_password=PASSWORD) as port:
        with pytest.raises(HostKeyRefusedError) as info:
            await _connect_once(port, kh)
    assert offered.passwords == []
    assert info.value.kind is HostKeyRefusal.REVOKED
    assert "forget-key" not in str(info.value)
    assert _lines(kh) == [revocation]


def test_a_first_use_key_that_cannot_be_written_whole_stops_authentication(tmp_path, monkeypatch):
    """``begin_auth`` raises when the record's write makes no progress, which
    is how asyncssh is told to abort before the first credential."""
    kh = tmp_path / "known_hosts"
    client = HostKeyClient(KnownHosts(kh), "router1", 22)
    assert client.validate_host_public_key("router1", "127.0.0.1", 22, _key().convert_to_public())

    def stuck_write(fd: int, data: bytes) -> int:
        return 0

    monkeypatch.setattr(os, "write", stuck_write)
    with pytest.raises(KnownHostsUnwritableError):
        client.begin_auth("admin")


def _cert_server_keys(ca: asyncssh.SSHKey, host_key: asyncssh.SSHKey) -> list[Any]:
    """A host key with a certificate for principal ``router1``: asyncssh serves both
    the certificate and the plain key."""
    cert = ca.generate_host_certificate(host_key, "router1", principals=["router1"])
    return [(host_key, cert)]


@pytest.mark.asyncio
async def test_cert_trusted_by_ca_line_connects_and_records_nothing(tmp_path):
    """With an ``@cert-authority`` line for the host ID, the certificate (its
    principals naming the host ID) is what the handshake checks, and nothing
    is recorded."""
    ca, host_key = _key(), _key()
    kh = tmp_path / "known_hosts"
    offered = Offered()
    server_keys = _cert_server_keys(ca, host_key)
    async with ssh_server(offered, host_keys=server_keys, accepted_password=PASSWORD) as port:
        kh.write_text(f"@cert-authority [router1]:{port} {_pub(ca)}\n")
        before = kh.read_bytes()
        await _connect_once(port, kh)
    assert kh.read_bytes() == before
    assert len(offered.passwords) == 1


@pytest.mark.asyncio
async def test_cert_and_plain_key_without_ca_line_records_the_plain_key(tmp_path):
    """No CA line trusts the host's certificate, so otto offers no certificate
    algorithm: the handshake uses the plain key, a first use, which is
    recorded. The certificate is never recorded."""
    ca, host_key = _key(), _key()
    kh = tmp_path / "known_hosts"
    offered = Offered()
    server_keys = _cert_server_keys(ca, host_key)
    async with ssh_server(offered, host_keys=server_keys, accepted_password=PASSWORD) as port:
        await _connect_once(port, kh)
    assert _lines(kh) == [f"[router1]:{port} {_pub(host_key)}"]
    assert len(offered.passwords) == 1


# ---------------------------------------------------------------------------
# The file: off, unwritable, forget
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_verify_off_touches_no_file(tmp_path):
    """``verify_host_key = false``: a planted different key does not refuse, the
    file is not rewritten, no lock file appears, and a file in a missing
    directory is never created."""
    kh = tmp_path / "known_hosts"
    absent = tmp_path / "absent" / "known_hosts"
    offered = Offered()
    async with ssh_server(offered, host_keys=[_key()], accepted_password=PASSWORD) as port:
        kh.write_text(f"[router1]:{port} {_pub(_key())}\n")
        before = kh.read_bytes()
        await _connect_once(port, kh, verify_host_key=False)
        await _connect_once(port, absent, verify_host_key=False)
    assert kh.read_bytes() == before
    assert sorted(p.name for p in tmp_path.iterdir()) == ["known_hosts"]
    assert len(offered.passwords) == 2


@pytest.mark.asyncio
@pytest.mark.skipif(_cannot_enforce_permissions(), reason="root ignores directory permissions")
async def test_unwritable_refuses_only_when_recording(tmp_path):
    """A read-only pre-seeded file whose entry matches keeps working; a host
    that must be recorded there is refused, naming both fixes, before any
    password."""
    key = _key()
    ro = tmp_path / "ro"
    ro.mkdir()
    kh = ro / "known_hosts"
    offered = Offered()
    async with ssh_server(offered, host_keys=[key], accepted_password=PASSWORD) as port:
        kh.write_text(f"[router1]:{port} {_pub(key)}\n")
        kh.chmod(0o400)
        ro.chmod(0o500)
        try:
            await _connect_once(port, kh)
            assert len(offered.passwords) == 1
            with pytest.raises(HostKeyRefusedError, match="verify_host_key = false") as info:
                await _connect_once(port, kh, alias="router2")
        finally:
            ro.chmod(0o700)
            kh.chmod(0o600)
    assert len(offered.passwords) == 1
    assert info.value.host_id == "router2"
    assert info.value.kind is HostKeyRefusal.FILE
    assert "forget-key" not in str(info.value)
    assert _lines(kh) == [f"[router1]:{port} {_pub(key)}"]


@pytest.mark.asyncio
async def test_a_garbled_file_refuses_the_connection_naming_the_file(tmp_path):
    """A known-hosts file asyncssh cannot parse: choosing the algorithms falls
    back to asyncssh's default (no ``server_host_key_algs``), and the
    handshake's own read refuses the connection as a file problem, naming
    the file, before any password."""
    kh = tmp_path / "known_hosts"
    offered = Offered()
    async with ssh_server(offered, host_keys=[_key()], accepted_password=PASSWORD) as port:
        kh.write_text(f"[router1]:{port}\n")  # a truncated entry: no key at all
        assert "server_host_key_algs" not in _options(port, kh)._kwargs(alias="router1")
        with pytest.raises(HostKeyRefusedError) as info:
            await _connect_once(port, kh)
    assert offered.passwords == []
    assert info.value.kind is HostKeyRefusal.FILE
    assert str(kh) in str(info.value)
    assert "forget-key" not in str(info.value)


@pytest.mark.asyncio
async def test_refuse_then_forget_then_connect_records_new_key(tmp_path):
    """The recovery the refusal names: forget the host's entries, and the next
    connection records the new key."""
    new_key = _key()
    kh = tmp_path / "known_hosts"
    offered = Offered()
    async with ssh_server(offered, host_keys=[new_key], accepted_password=PASSWORD) as port:
        kh.write_text(f"[router1]:{port} {_pub(_key())}\n")
        with pytest.raises(HostKeyRefusedError):
            await _connect_once(port, kh)
        assert KnownHosts(kh).forget("router1", [port]).removed == 1
        await _connect_once(port, kh)
    assert _lines(kh) == [f"[router1]:{port} {_pub(new_key)}"]
    assert len(offered.passwords) == 1


# ---------------------------------------------------------------------------
# One message, every surface; and the silent opt-out
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_survey_and_cli_show_the_same_message(tmp_path, monkeypatch):
    """The survey's login row and a host verb's error line carry the one
    refusal message, the one :class:`HostKeyRefusedError` builds."""
    from otto.cli.expose import make_method_command
    from otto.host.survey.login import attempt_term_login

    kh = tmp_path / "known_hosts"
    offered = Offered()
    shown: list[str] = []

    def record_fail(message: object, code: int = 1, **_: object):
        shown.append(str(message))
        raise typer.Exit(code)

    monkeypatch.setattr("otto.cli.expose.fail", record_fail)
    async with ssh_server(offered, host_keys=[_key()], accepted_password=PASSWORD) as port:
        kh.write_text(f"[router1]:{port} {_pub(_key())}\n")
        host = _unix_host("router1", port, kh)

        outcome = await attempt_term_login(host, "ssh", port, login=None, timeout=20.0)

        class _Ctx:
            obj = host
            meta: ClassVar[dict] = {}

        cmd = make_method_command("exec", UnixHost.exec)
        with pytest.raises(typer.Exit) as excinfo:
            await cmd(_Ctx(), cmd="true")
    assert excinfo.value.exit_code == 1
    assert offered.passwords == []

    assert outcome.state == "login-failed"
    assert len(shown) == 1
    marker = "SSH host key for router1"
    survey = outcome.detail[outcome.detail.index(marker) :]
    cli = shown[0][shown[0].index(marker) :]
    assert survey == cli
    assert "otto host router1 forget-key" in cli
    assert shown[0].startswith("host 'router1' exec: ")


@pytest.mark.asyncio
@pytest.mark.parametrize("selector", [".*", "router1"])
async def test_opt_out_connects_silently(selector, caplog, tmp_path):
    """``verify_host_key = false`` from the project-wide or the per-host
    selector: a host whose recorded key differs connects, nothing is logged
    at WARNING or above, and the file is neither read for a refusal nor
    written."""
    kh = tmp_path / "known_hosts"
    offered = Offered()
    async with ssh_server(offered, host_keys=[_key()], accepted_password=PASSWORD) as port:
        kh.write_text(f"[router1]:{port} {_pub(_key())}\n")
        before = kh.read_bytes()
        host = create_host_from_dict(
            _host_dict(port, kh),
            preferences={selector: {"ssh_options": {"verify_host_key": False}}},
            element=Element("router1"),
        )
        assert host.ssh_options.verify_host_key is False
        with caplog.at_level(logging.DEBUG):
            try:
                await host.connections.ssh()
            finally:
                await host.close()
    assert [r for r in caplog.records if r.levelno >= logging.WARNING] == []
    assert kh.read_bytes() == before
    assert sorted(p.name for p in tmp_path.iterdir()) == ["known_hosts"]
    assert len(offered.passwords) == 1


def test_the_suite_never_resolves_the_real_known_hosts_file(tmp_path_factory):
    """The root conftest's ``_hermetic_known_hosts`` is in force: a default
    ``SshOptions`` resolves to a per-worker file under pytest's basetemp,
    never the real ``~/.ssh/known_hosts``, while a file a test names is still
    that file."""
    resolved = known_hosts.known_hosts_path(SshOptions())
    assert resolved.is_relative_to(tmp_path_factory.getbasetemp())
    assert not resolved.is_relative_to(Path.home() / ".ssh")
    named = Path("/srv/lab/known_hosts")
    assert known_hosts.known_hosts_path(SshOptions(known_hosts=str(named))) == named


# ---------------------------------------------------------------------------
# Concurrent first contact: asyncssh's read of the file is stale by key exchange
# ---------------------------------------------------------------------------


def _record_after_the_read(monkeypatch, kh: Path, key: asyncssh.SSHKey) -> None:
    """Record *key* right after asyncssh reads the check, as a sibling connection would.

    asyncssh calls the ``known_hosts`` callable once, when the connection opens,
    and keeps that answer; the key exchange comes later. A sibling's first-use
    record landing in between is what concurrent first contact produces.
    """
    real_call = HostKeyCheck.__call__

    def read_then_sibling_records(self, host, addr, port):
        found = real_call(self, host, addr, port)
        KnownHosts(kh).record(self.alias, self.port, key.convert_to_public())
        return found

    monkeypatch.setattr(HostKeyCheck, "__call__", read_then_sibling_records)


@pytest.mark.asyncio
async def test_a_key_recorded_by_a_sibling_after_the_read_is_trusted(tmp_path, monkeypatch):
    """The file holds exactly the key presented, so it is trusted, not a changed key."""
    key = _key()
    kh = tmp_path / "known_hosts"
    _record_after_the_read(monkeypatch, kh, key)
    offered = Offered()
    async with ssh_server(offered, host_keys=[key], accepted_password=PASSWORD) as port:
        await _connect_once(port, kh)
    assert _lines(kh) == [f"[router1]:{port} {_pub(key)}"]
    assert len(offered.passwords) == 1


@pytest.mark.asyncio
async def test_a_different_key_recorded_after_the_read_is_refused_as_changed(tmp_path, monkeypatch):
    """The companion: what landed is another key, so the presented one is changed."""
    kh = tmp_path / "known_hosts"
    other = _key()
    _record_after_the_read(monkeypatch, kh, other)
    offered = Offered()
    async with ssh_server(offered, host_keys=[_key()], accepted_password=PASSWORD) as port:
        with pytest.raises(HostKeyRefusedError) as info:
            await _connect_once(port, kh)
    assert info.value.kind is HostKeyRefusal.CHANGED
    assert offered.passwords == []
    assert _lines(kh) == [f"[router1]:{port} {_pub(other)}"]


@pytest.mark.asyncio
async def test_a_key_revoked_after_the_read_is_refused_as_revoked(tmp_path, monkeypatch):
    """A revocation landing after asyncssh's read is labelled revoked, not changed."""
    key = _key()
    kh = tmp_path / "known_hosts"
    revocation = f"@revoked router1 {_pub(key)}"
    real_call = HostKeyCheck.__call__

    def read_then_revoked(self, host, addr, port):
        found = real_call(self, host, addr, port)
        kh.write_text(f"{revocation}\n")
        return found

    monkeypatch.setattr(HostKeyCheck, "__call__", read_then_revoked)
    offered = Offered()
    async with ssh_server(offered, host_keys=[key], accepted_password=PASSWORD) as port:
        with pytest.raises(HostKeyRefusedError) as info:
            await _connect_once(port, kh)
    assert info.value.kind is HostKeyRefusal.REVOKED
    assert offered.passwords == []
    assert _lines(kh) == [revocation]


@pytest.mark.asyncio
async def test_concurrent_first_contact_all_connect_and_record_one_line(tmp_path):
    """Eight connections to one never-seen host at once: all connect, one line recorded."""
    import asyncio

    key = _key()
    kh = tmp_path / "known_hosts"
    async with ssh_server(Offered(), host_keys=[key], accepted_password=PASSWORD) as port:
        await asyncio.gather(*(_connect_once(port, kh) for _ in range(8)))
    assert _lines(kh) == [f"[router1]:{port} {_pub(key)}"]


@pytest.mark.asyncio
async def test_a_changed_key_stays_changed_when_forget_key_runs_before_the_label(
    tmp_path, monkeypatch
):
    """The label is the hook's verdict, carried in asyncssh's reason: a
    ``forget-key`` between the refusal and its labelling cannot turn it into
    an untrusted-certificate refusal."""
    kh = tmp_path / "known_hosts"
    real_validate = HostKeyClient.validate_host_public_key

    def refuse_then_forget(self, host, addr, port, key):
        accepted = real_validate(self, host, addr, port, key)
        KnownHosts(kh).forget(self._alias, [self._port])
        return accepted

    monkeypatch.setattr(HostKeyClient, "validate_host_public_key", refuse_then_forget)
    offered = Offered()
    async with ssh_server(offered, host_keys=[_key()], accepted_password=PASSWORD) as port:
        kh.write_text(f"[router1]:{port} {_pub(_key())}\n")
        with pytest.raises(HostKeyRefusedError) as info:
            await _connect_once(port, kh)
    assert info.value.kind is HostKeyRefusal.CHANGED
    assert offered.passwords == []
    assert _lines(kh) == []


@pytest.mark.asyncio
async def test_a_host_known_by_its_ca_refuses_a_plain_key_as_not_certified(tmp_path):
    """An ``@cert-authority`` line for the host ID means only the CA's
    certificates are trusted for it: a server offering a plain key alone is
    refused as not certified, with advice that is not forget-key (which keeps
    the CA line); nothing is recorded, no password is sent."""
    ca = _key()
    kh = tmp_path / "known_hosts"
    offered = Offered()
    async with ssh_server(offered, host_keys=[_key()], accepted_password=PASSWORD) as port:
        kh.write_text(f"@cert-authority [router1]:{port} {_pub(ca)}\n")
        before = kh.read_bytes()
        with pytest.raises(HostKeyRefusedError) as info:
            await _connect_once(port, kh)
    assert info.value.kind is HostKeyRefusal.NOT_CERTIFIED
    assert "forget-key" not in str(info.value)
    assert "@cert-authority line" in str(info.value)
    assert offered.passwords == []
    assert kh.read_bytes() == before
