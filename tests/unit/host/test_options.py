"""Tests for the Options dataclasses and their JSON/factory integration."""

import dataclasses

import pytest
from pydantic import ValidationError

from otto.host.element import Element
from otto.host.factory import create_host_from_dict
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
    UserlandOptions,
)


def test_local_port_forward_validates_and_is_frozen():
    # positional construction still works (back-compat)
    fwd = LocalPortForward("localhost", 8080, "web", 80)
    assert fwd.listen_port == 8080

    # frozen: a frozen pydantic dataclass raises FrozenInstanceError on assignment
    with pytest.raises(dataclasses.FrozenInstanceError):
        fwd.listen_port = 9090  # type: ignore[misc]

    # validates: a non-numeric port is rejected. Built positionally, so pydantic
    # reports the location as the argument index — "1" is listen_port.
    with pytest.raises(ValidationError, match=r"(?m)^1\n\s+Input should be a valid integer"):
        LocalPortForward("localhost", "not-a-port", "web", 80)


# ---------------------------------------------------------------------------
# SshOptions
# ---------------------------------------------------------------------------


class TestSshOptions:
    def test_default_kwargs_send_the_port_and_the_check_by_host_id(self):
        """asyncssh's own file check (by name AND address) is never handed the
        path: it gets otto's check for the host ID, its client object, the
        host ID as the alias it judges certificates under, and otto's choice
        of host-key algorithms."""
        from otto.host import known_hosts
        from otto.host.host_key_client import HostKeyCheck

        kw = SshOptions()._kwargs(alias="router1")
        assert sorted(kw) == [
            "client_factory",
            "host_key_alias",
            "known_hosts",
            "port",
            "server_host_key_algs",
        ]
        assert kw["port"] == 22
        assert kw["host_key_alias"] == "router1"
        check = kw["known_hosts"]
        assert isinstance(check, HostKeyCheck)
        assert check.alias == "router1"
        assert check.port == 22
        assert check.path == known_hosts.known_hosts_path(SshOptions())
        assert kw == HostKeyCheck.for_options(SshOptions(), "router1").connect_kwargs() | {
            "port": 22
        }

    def test_a_configured_host_key_algorithm_list_wins_over_otto_s_choice(self):
        kw = SshOptions(server_host_key_algs=["ssh-rsa"])._kwargs(alias="router1")
        assert kw["server_host_key_algs"] == ["ssh-rsa"]
        assert kw["known_hosts"].chooses_algs is False

    def test_kwargs_with_checking_off_send_no_check_at_all(self):
        """``verify_host_key = false``: asyncssh gets ``known_hosts=None`` and
        nothing that would read or write the file."""
        assert SshOptions(verify_host_key=False)._kwargs(alias="router1") == {
            "port": 22,
            "known_hosts": None,
        }

    def test_host_key_checking_is_on_by_default_against_the_users_file(self):
        opts = SshOptions()
        assert opts.known_hosts == "~/.ssh/known_hosts"
        assert opts.verify_host_key is True

    def test_none_known_hosts_is_refused(self):
        """``None`` used to turn checking off; it is now refused, naming the switch."""
        with pytest.raises(ValueError, match="verify_host_key = false"):
            SshOptions(known_hosts=None)  # type: ignore[arg-type]

    def test_the_lab_spec_refuses_a_null_known_hosts_with_the_same_message(self):
        from otto.host.option_specs import SshOptionsSpec

        with pytest.raises(ValidationError, match="verify_host_key = false"):
            SshOptionsSpec.model_validate({"known_hosts": None})

    def test_the_lab_spec_carries_both_host_key_settings(self):
        from otto.host.option_specs import SshOptionsSpec

        spec = SshOptionsSpec.model_validate(
            {"known_hosts": "/srv/ci/known_hosts", "verify_host_key": False}
        )
        runtime = spec.to_runtime()
        assert runtime.known_hosts == "/srv/ci/known_hosts"
        assert runtime.verify_host_key is False

    def test_curated_fields_included_only_when_set(self):
        kw = SshOptions(port=2222, connect_timeout=5.0, agent_forwarding=True)._kwargs(alias="h1")
        assert kw["port"] == 2222
        assert kw["connect_timeout"] == 5.0
        assert kw["agent_forwarding"] is True
        # Unset fields stay absent so asyncssh's defaults apply.
        assert "keepalive_interval" not in kw
        assert "client_keys" not in kw

    def test_extra_forwards_arbitrary_kwargs(self):
        opts = SshOptions(extra={"config": ["/tmp/ssh_config"], "proxy_command": "pc"})
        kw = opts._kwargs(alias="h1")
        assert kw["config"] == ["/tmp/ssh_config"]
        assert kw["proxy_command"] == "pc"

    def test_extra_cannot_name_a_curated_field(self):
        """One value, one source: a field's name in ``extra`` is refused when the
        options are built (tests/unit/host/test_options_extra_shadowing.py has
        the rule across every class and the lab spec)."""
        with pytest.raises(ValueError, match="known_hosts"):
            SshOptions(port=22, extra={"known_hosts": "/etc/ssh/known_hosts"})

    @pytest.mark.parametrize("key", ["username", "tunnel"])
    def test_extra_overriding_an_identity_key_warns_once_and_names_the_key(self, caplog, key):
        """M6: before the options layer existed, duplicating username/tunnel as
        a kwarg was a TypeError; extra now overrides them silently. A warning
        naming the key keeps the old loudness without the crash, and prints
        only the key name, never the value. (``password`` is refused outright:
        tests/unit/host/test_options_extra_shadowing.py.)"""
        import logging

        opts = SshOptions(extra={key: "s3cret-value"})
        with caplog.at_level(logging.WARNING, logger="otto.host.options"):
            opts._kwargs(alias="h1")
        assert key in caplog.text
        assert "s3cret-value" not in caplog.text

    def test_extra_without_an_identity_key_is_silent(self, caplog):
        import logging

        opts = SshOptions(extra={"config": ["/tmp/ssh_config"]})
        with caplog.at_level(logging.WARNING, logger="otto.host.options"):
            opts._kwargs(alias="h1")
        assert caplog.text == ""

    def test_kex_and_mac_lists_reach_asyncssh_only_when_set(self):
        """A legacy sshd needs SHA-1-era key exchange and MACs listed by name.

        Both are asyncssh kwargs; both were reachable only through ``extra``
        before they were curated. Unset means asyncssh's own default list.
        """
        assert "kex_algs" not in SshOptions()._kwargs(alias="h1")
        assert "mac_algs" not in SshOptions()._kwargs(alias="h1")
        kw = SshOptions(
            kex_algs=["diffie-hellman-group14-sha1", "diffie-hellman-group1-sha1"],
            mac_algs=["hmac-sha1", "hmac-md5"],
        )._kwargs(alias="h1")
        assert kw["kex_algs"] == ["diffie-hellman-group14-sha1", "diffie-hellman-group1-sha1"]
        assert kw["mac_algs"] == ["hmac-sha1", "hmac-md5"]


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
            local_forwards=[LocalPortForward("localhost", 8080, "web", 80)],
            remote_forwards=[RemotePortForward("", 9000, "localhost", 22)],
            socks_forwards=[SocksForward("localhost", 1080)],
        )
        conn = FakeConn()
        await opts._apply_post_connect(conn)  # type: ignore[arg-type]
        assert conn.local == [("localhost", 8080, "web", 80)]
        assert conn.remote == [("", 9000, "localhost", 22)]
        assert conn.socks == [("localhost", 1080)]

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
        assert kw["port"] == 23
        assert kw["encoding"] is False
        assert kw["cols"] == 400
        assert kw["rows"] == 24

    def test_extra_merges(self):
        kw = TelnetOptions(extra={"term": "vt100"})._open_kwargs()
        assert kw["term"] == "vt100"

    def test_connect_timeout_bounds_the_tcp_connect(self):
        assert "connect_timeout" not in TelnetOptions()._open_kwargs()
        assert TelnetOptions(connect_timeout=2.5)._open_kwargs()["connect_timeout"] == 2.5

    def test_single_client_console_defaults_false(self):
        from otto.host.options import TelnetOptions

        assert TelnetOptions().single_client_console is False

    def test_single_client_console_can_be_set(self):
        from otto.host.options import TelnetOptions

        assert TelnetOptions(single_client_console=True).single_client_console is True


# ---------------------------------------------------------------------------
# FtpOptions
# ---------------------------------------------------------------------------


class TestFtpOptions:
    def test_defaults_produce_empty_client_kwargs(self):
        # aioftp.Client() with no args is the historical behavior.
        assert FtpOptions()._client_kwargs() == {}
        assert FtpOptions().port == 21

    def test_ftps_kwargs(self):
        kw = FtpOptions(ssl=True, encoding="latin-1", socket_timeout=10.0)._client_kwargs()
        assert kw["ssl"] is True
        assert kw["encoding"] == "latin-1"
        assert kw["socket_timeout"] == 10.0


# ---------------------------------------------------------------------------
# SftpOptions / ScpOptions
# ---------------------------------------------------------------------------


class TestSftpScpOptions:
    def test_sftp_defaults_empty(self):
        assert SftpOptions()._kwargs() == {}

    def test_scp_defaults(self):
        kw = ScpOptions()._kwargs()
        assert kw == {"preserve": False, "recurse": True, "block_size": 16384}

    def test_scp_tuned_block_size(self):
        kw = ScpOptions(block_size=65536, preserve=True)._kwargs()
        assert kw["block_size"] == 65536
        assert kw["preserve"] is True

    def test_the_transfer_bound_is_otto_s_and_never_reaches_asyncssh(self):
        assert ScpOptions().max_concurrent_transfers is None
        assert SftpOptions().max_concurrent_transfers is None
        assert "max_concurrent_transfers" not in ScpOptions(max_concurrent_transfers=3)._kwargs()
        assert "max_concurrent_transfers" not in SftpOptions(max_concurrent_transfers=3)._kwargs()


# ---------------------------------------------------------------------------
# NcOptions
# ---------------------------------------------------------------------------


class TestNcOptions:
    def test_defaults_match_legacy_remotehost_fields(self):
        opts = NcOptions()
        assert opts.exec_name == "nc"
        assert opts.port == 9000
        assert opts.port_strategy == "auto"
        assert opts.port_cmd is None
        assert opts.listener_check == "auto"
        assert opts.listener_cmd is None


# ---------------------------------------------------------------------------
# lab.json deserialization
# ---------------------------------------------------------------------------


class TestCreateHostFromDict:
    def _minimal(self, **extra):
        data = {
            "ip": "10.0.0.1",
            "creds": [{"login": "admin", "password": "secret"}],
        }
        data.update(extra)
        return data

    def test_minimal_dict_uses_default_options(self):
        host = create_host_from_dict(self._minimal(), element=Element("lab"))
        assert host.ssh_options == SshOptions()
        assert host.telnet_options == TelnetOptions()
        assert host.ftp_options == FtpOptions()
        assert host.nc_options == NcOptions()
        # All-None is "probe everything", which is what a host that says
        # nothing about its userland must get.
        assert host.userland_options == UserlandOptions()

    def test_userland_options_from_dict(self):
        """Lab data's ``userland_options`` reaches the runtime host as runtime options.

        Two separate claims, because the spec-side wiring has two halves that
        fail independently: ``HostSpec`` has to DECLARE the field (without it
        the table is an extra key and validation rejects the host outright),
        and ``to_host`` has to convert it (without that the host keeps the
        all-probe default and the declaration is silently discarded).

        The ``isinstance`` is not decoration. ``UserlandOptionsSpec`` carries
        the same attribute names, so a ``to_host`` that forwarded the SPEC
        instead of calling ``to_runtime()`` would satisfy every value
        assertion below while handing :class:`~otto.host.userland.Userland` a
        pydantic model where it expects a dataclass.
        """
        host = create_host_from_dict(
            self._minimal(
                userland_options={
                    "elevation": "su",
                    "timeout_style": "dash-t",
                }
            ),
            element=Element("lab"),
        )
        assert isinstance(host.userland_options, UserlandOptions)
        assert host.userland_options.elevation == "su"
        assert host.userland_options.timeout_style == "dash-t"
        # Declaring two answers must not fabricate the other three: an
        # undeclared capability stays None so the device is still asked.
        assert host.userland_options.base64_flag is None

    def test_ssh_options_from_dict(self):
        host = create_host_from_dict(
            self._minimal(
                ssh_options={
                    "port": 2222,
                    "connect_timeout": 5.0,
                    "extra": {"config": ["/tmp/ssh_config"]},
                }
            ),
            element=Element("lab"),
        )
        assert host.ssh_options.port == 2222
        assert host.ssh_options.connect_timeout == 5.0
        assert host.ssh_options.extra == {"config": ["/tmp/ssh_config"]}

    def test_ssh_options_structured_forwards_from_dict(self):
        host = create_host_from_dict(
            self._minimal(
                ssh_options={
                    "local_forwards": [
                        {
                            "listen_host": "localhost",
                            "listen_port": 8080,
                            "dest_host": "web.internal",
                            "dest_port": 80,
                        },
                    ],
                    "remote_forwards": [
                        {
                            "listen_host": "",
                            "listen_port": 9000,
                            "dest_host": "localhost",
                            "dest_port": 22,
                        },
                    ],
                    "socks_forwards": [
                        {"listen_host": "localhost", "listen_port": 1080},
                    ],
                }
            ),
            element=Element("lab"),
        )
        assert host.ssh_options.local_forwards == [
            LocalPortForward("localhost", 8080, "web.internal", 80),
        ]
        assert host.ssh_options.remote_forwards == [
            RemotePortForward("", 9000, "localhost", 22),
        ]
        assert host.ssh_options.socks_forwards == [SocksForward("localhost", 1080)]

    def test_telnet_options_login_prompt_string_to_bytes(self):
        host = create_host_from_dict(
            self._minimal(
                telnet_options={
                    "port": 2323,
                    "cols": 200,
                    "login_prompt": ">",
                }
            ),
            element=Element("lab"),
        )
        assert host.telnet_options.port == 2323
        assert host.telnet_options.cols == 200
        assert host.telnet_options.login_prompt == b">"

    def test_ftp_options_passive_commands_list_to_tuple(self):
        host = create_host_from_dict(
            self._minimal(
                ftp_options={
                    "port": 2121,
                    "passive_commands": ["pasv"],
                }
            ),
            element=Element("lab"),
        )
        assert host.ftp_options.port == 2121
        assert host.ftp_options.passive_commands == ("pasv",)

    def test_nc_options_from_dict(self):
        host = create_host_from_dict(
            self._minimal(
                nc_options={
                    "exec_name": "ncat",
                    "port": 9500,
                    "port_strategy": "ss",
                }
            ),
            element=Element("lab"),
        )
        assert host.nc_options.exec_name == "ncat"
        assert host.nc_options.port == 9500
        assert host.nc_options.port_strategy == "ss"

    def test_scp_and_sftp_options_from_dict(self):
        host = create_host_from_dict(
            self._minimal(
                scp_options={"block_size": 65536, "preserve": True},
                sftp_options={"env": {"FOO": "bar"}},
            ),
            element=Element("lab"),
        )
        assert host.scp_options.block_size == 65536
        assert host.scp_options.preserve is True
        assert host.sftp_options.env == {"FOO": "bar"}

    def test_post_connect_rejected_in_dict(self):
        # JSON can't carry callables; ``post_connect`` is a runtime-only field
        # the SshOptionsSpec deliberately omits. Under the Phase A 2b spec path
        # (``extra='forbid'``) such a misplaced key now raises a pydantic
        # ValidationError instead of being silently dropped.
        with pytest.raises(
            ValidationError,
            match=r"(?m)^ssh_options\.post_connect\n\s+Extra inputs are not permitted",
        ):
            create_host_from_dict(
                self._minimal(
                    ssh_options={
                        "port": 2222,
                        "post_connect": "not_a_callable",
                    }
                ),
                element=Element("lab"),
            )
