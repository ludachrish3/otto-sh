"""Tests for the Options dataclasses and their JSON/factory integration."""

import pytest

from otto.host.options import (
    FtpOptions,
    LocalPortForward,
    NcOptions,
    RemotePortForward,
    ScpOptions,
    SftpOptions,
    SocksForward,
    SshOptions,
    TelnetOptions,
)
from otto.storage.factory import create_host_from_dict


# ---------------------------------------------------------------------------
# SshOptions
# ---------------------------------------------------------------------------


class TestSshOptions:

    def test_defaults_reproduce_historical_kwargs(self):
        # Historical behavior: port 22, host-key verification off.
        assert SshOptions()._kwargs() == {'port': 22, 'known_hosts': None}

    def test_curated_fields_included_only_when_set(self):
        kw = SshOptions(port=2222, connect_timeout=5.0, agent_forwarding=True)._kwargs()
        assert kw['port'] == 2222
        assert kw['connect_timeout'] == 5.0
        assert kw['agent_forwarding'] is True
        # Unset fields stay absent so asyncssh's defaults apply.
        assert 'keepalive_interval' not in kw
        assert 'client_keys' not in kw

    def test_extra_forwards_arbitrary_kwargs(self):
        opts = SshOptions(extra={'config': ['/tmp/ssh_config'], 'proxy_command': 'pc'})
        kw = opts._kwargs()
        assert kw['config'] == ['/tmp/ssh_config']
        assert kw['proxy_command'] == 'pc'

    def test_extra_overrides_curated_fields(self):
        opts = SshOptions(port=22, extra={'known_hosts': '/etc/ssh/known_hosts'})
        kw = opts._kwargs()
        assert kw['known_hosts'] == '/etc/ssh/known_hosts'


class TestSshPostConnect:

    @pytest.mark.asyncio
    async def test_applies_local_and_remote_forwards(self):
        class FakeConn:
            def __init__(self):
                self.local = []
                self.remote = []
                self.socks = []

            async def forward_local_port(self, lh, lp, dh, dp):
                self.local.append((lh, lp, dh, dp))

            async def forward_remote_port(self, lh, lp, dh, dp):
                self.remote.append((lh, lp, dh, dp))

            async def forward_socks(self, lh, lp):
                self.socks.append((lh, lp))

        opts = SshOptions(
            local_forwards=[LocalPortForward('localhost', 8080, 'web', 80)],
            remote_forwards=[RemotePortForward('', 9000, 'localhost', 22)],
            socks_forwards=[SocksForward('localhost', 1080)],
        )
        conn = FakeConn()
        await opts._apply_post_connect(conn)  # type: ignore[arg-type]
        assert conn.local == [('localhost', 8080, 'web', 80)]
        assert conn.remote == [('', 9000, 'localhost', 22)]
        assert conn.socks == [('localhost', 1080)]

    @pytest.mark.asyncio
    async def test_runs_post_connect_hook(self):
        calls = []

        async def hook(conn):
            calls.append(conn)

        opts = SshOptions(post_connect=hook)
        sentinel = object()
        await opts._apply_post_connect(sentinel)  # type: ignore[arg-type]
        assert calls == [sentinel]


# ---------------------------------------------------------------------------
# TelnetOptions
# ---------------------------------------------------------------------------


class TestTelnetOptions:

    def test_defaults_reproduce_historical_open_kwargs(self):
        kw = TelnetOptions()._open_kwargs()
        assert kw['port'] == 23
        assert kw['encoding'] is False
        assert kw['cols'] == 400
        assert kw['rows'] == 24

    def test_extra_merges(self):
        kw = TelnetOptions(extra={'term': 'vt100'})._open_kwargs()
        assert kw['term'] == 'vt100'


# ---------------------------------------------------------------------------
# FtpOptions
# ---------------------------------------------------------------------------


class TestFtpOptions:

    def test_defaults_produce_empty_client_kwargs(self):
        # aioftp.Client() with no args is the historical behavior.
        assert FtpOptions()._client_kwargs() == {}
        assert FtpOptions().port == 21

    def test_ftps_kwargs(self):
        kw = FtpOptions(ssl=True, encoding='latin-1', socket_timeout=10.0)._client_kwargs()
        assert kw['ssl'] is True
        assert kw['encoding'] == 'latin-1'
        assert kw['socket_timeout'] == 10.0


# ---------------------------------------------------------------------------
# SftpOptions / ScpOptions
# ---------------------------------------------------------------------------


class TestSftpScpOptions:

    def test_sftp_defaults_empty(self):
        assert SftpOptions()._kwargs() == {}

    def test_scp_defaults(self):
        kw = ScpOptions()._kwargs()
        assert kw == {'preserve': False, 'recurse': True, 'block_size': 16384}

    def test_scp_tuned_block_size(self):
        kw = ScpOptions(block_size=65536, preserve=True)._kwargs()
        assert kw['block_size'] == 65536
        assert kw['preserve'] is True


# ---------------------------------------------------------------------------
# NcOptions
# ---------------------------------------------------------------------------


class TestNcOptions:

    def test_defaults_match_legacy_remotehost_fields(self):
        opts = NcOptions()
        assert opts.exec_name == 'nc'
        assert opts.port == 9000
        assert opts.port_strategy == 'auto'
        assert opts.port_cmd is None
        assert opts.listener_check == 'auto'
        assert opts.listener_cmd is None


# ---------------------------------------------------------------------------
# hosts.json deserialization
# ---------------------------------------------------------------------------


class TestCreateHostFromDict:

    def _minimal(self, **extra):
        data = {
            'ip': '10.0.0.1',
            'creds': {'admin': 'secret'},
            'ne': 'lab',
        }
        data.update(extra)
        return data

    def test_minimal_dict_uses_default_options(self):
        host = create_host_from_dict(self._minimal())
        assert host.ssh_options == SshOptions()
        assert host.telnet_options == TelnetOptions()
        assert host.ftp_options == FtpOptions()
        assert host.nc_options == NcOptions()

    def test_ssh_options_from_dict(self):
        host = create_host_from_dict(self._minimal(ssh_options={
            'port': 2222,
            'connect_timeout': 5.0,
            'extra': {'config': ['/tmp/ssh_config']},
        }))
        assert host.ssh_options.port == 2222
        assert host.ssh_options.connect_timeout == 5.0
        assert host.ssh_options.extra == {'config': ['/tmp/ssh_config']}

    def test_ssh_options_structured_forwards_from_dict(self):
        host = create_host_from_dict(self._minimal(ssh_options={
            'local_forwards': [
                {'listen_host': 'localhost', 'listen_port': 8080,
                 'dest_host': 'web.internal', 'dest_port': 80},
            ],
            'remote_forwards': [
                {'listen_host': '', 'listen_port': 9000,
                 'dest_host': 'localhost', 'dest_port': 22},
            ],
            'socks_forwards': [
                {'listen_host': 'localhost', 'listen_port': 1080},
            ],
        }))
        assert host.ssh_options.local_forwards == [
            LocalPortForward('localhost', 8080, 'web.internal', 80),
        ]
        assert host.ssh_options.remote_forwards == [
            RemotePortForward('', 9000, 'localhost', 22),
        ]
        assert host.ssh_options.socks_forwards == [SocksForward('localhost', 1080)]

    def test_telnet_options_login_prompt_string_to_bytes(self):
        host = create_host_from_dict(self._minimal(telnet_options={
            'port': 2323,
            'cols': 200,
            'login_prompt': '>',
        }))
        assert host.telnet_options.port == 2323
        assert host.telnet_options.cols == 200
        assert host.telnet_options.login_prompt == b'>'

    def test_ftp_options_passive_commands_list_to_tuple(self):
        host = create_host_from_dict(self._minimal(ftp_options={
            'port': 2121,
            'passive_commands': ['pasv'],
        }))
        assert host.ftp_options.port == 2121
        assert host.ftp_options.passive_commands == ('pasv',)

    def test_nc_options_from_dict(self):
        host = create_host_from_dict(self._minimal(nc_options={
            'exec_name': 'ncat',
            'port': 9500,
            'port_strategy': 'ss',
        }))
        assert host.nc_options.exec_name == 'ncat'
        assert host.nc_options.port == 9500
        assert host.nc_options.port_strategy == 'ss'

    def test_scp_and_sftp_options_from_dict(self):
        host = create_host_from_dict(self._minimal(
            scp_options={'block_size': 65536, 'preserve': True},
            sftp_options={'env': {'FOO': 'bar'}},
        ))
        assert host.scp_options.block_size == 65536
        assert host.scp_options.preserve is True
        assert host.sftp_options.env == {'FOO': 'bar'}

    def test_post_connect_ignored_in_dict(self):
        # JSON can't carry callables; factory must drop the key cleanly.
        host = create_host_from_dict(self._minimal(ssh_options={
            'port': 2222,
            'post_connect': 'not_a_callable',
        }))
        assert host.ssh_options.port == 2222
        assert host.ssh_options.post_connect is None
