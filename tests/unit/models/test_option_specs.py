import dataclasses

import pytest
from pydantic import ValidationError

from otto.host.options import (
    FtpOptions,
    LocalPortForward,
    NcOptions,
    RemotePortForward,
    ScpOptions,
    SftpOptions,
    SnmpOptions,
    SocksForward,
    SshOptions,
    TelnetOptions,
    TftpOptions,
)
from otto.models.base import OttoModel
from otto.models.options import (
    OPTION_SPEC_RUNTIME_PAIRS,
    FtpOptionsSpec,
    NcOptionsSpec,
    ScpOptionsSpec,
    SftpOptionsSpec,
    SnmpOptionsSpec,
    SshOptionsSpec,
    TelnetOptionsSpec,
    TftpOptionsSpec,
)


class _Sample(OttoModel):
    x: int = 1


def test_otto_model_forbids_unknown_fields():
    with pytest.raises(ValidationError) as exc:
        _Sample(x=1, nope=2)
    # extra='forbid' surfaces the offending key
    assert "nope" in str(exc.value)


def test_otto_model_accepts_known_fields():
    assert _Sample(x=5).x == 5


def test_local_port_forward_from_dict_and_positional():
    fwd = LocalPortForward(
        listen_host="127.0.0.1", listen_port=8080, dest_host="10.0.0.1", dest_port=80
    )
    assert fwd == LocalPortForward("127.0.0.1", 8080, "10.0.0.1", 80)


def test_remote_port_forward_from_dict_and_positional():
    fwd = RemotePortForward(
        listen_host="0.0.0.0", listen_port=2222, dest_host="127.0.0.1", dest_port=22
    )
    assert fwd == RemotePortForward("0.0.0.0", 2222, "127.0.0.1", 22)


def test_socks_forward_from_dict_and_positional():
    assert SocksForward(listen_host="127.0.0.1", listen_port=1080) == SocksForward(
        "127.0.0.1", 1080
    )


def test_forward_rejects_unknown_key():
    with pytest.raises(ValidationError):
        LocalPortForward(
            **{  # type: ignore[call-arg]
                "listen_host": "x",
                "listen_port": 1,
                "dest_host": "y",
                "dest_port": 2,
                "bogus": 3,
            }
        )


def test_ssh_spec_defaults_match_runtime_defaults():
    rt_obj = SshOptionsSpec().to_runtime()
    assert isinstance(rt_obj, SshOptions)
    assert rt_obj.port == 22
    assert rt_obj.known_hosts is None
    assert rt_obj.agent_forwarding is False


def test_ssh_spec_builds_forwards_and_extra():
    spec = SshOptionsSpec(
        port=2222,
        connect_timeout=5.0,
        local_forwards=[
            {
                "listen_host": "127.0.0.1",
                "listen_port": 8080,
                "dest_host": "10.0.0.1",
                "dest_port": 80,
            }
        ],
        extra={"rekey_bytes": 1000000},
    )
    rt_obj = spec.to_runtime()
    assert rt_obj.port == 2222
    assert rt_obj.connect_timeout == 5.0
    assert rt_obj.local_forwards[0].dest_port == 80
    assert rt_obj.extra == {"rekey_bytes": 1000000}


def test_ssh_spec_rejects_unknown_top_level_key():
    with pytest.raises(ValidationError) as exc:
        SshOptionsSpec(connet_timeout=5.0)  # typo
    assert "connet_timeout" in str(exc.value)


def test_ssh_spec_has_no_post_connect_field():
    assert "post_connect" not in SshOptionsSpec.model_fields


def test_telnet_spec_defaults_match_runtime():
    rt_obj = TelnetOptionsSpec().to_runtime()
    assert isinstance(rt_obj, TelnetOptions)
    assert rt_obj.port == 23
    assert rt_obj.cols == 400
    assert rt_obj.login_prompt == b":"


def test_telnet_spec_encodes_login_prompt_from_str():
    rt_obj = TelnetOptionsSpec(login_prompt="Password:").to_runtime()
    assert rt_obj.login_prompt == b"Password:"


def test_telnet_spec_accepts_encoding_false():
    rt_obj = TelnetOptionsSpec(encoding=False).to_runtime()
    assert rt_obj.encoding is False


def test_sftp_spec_defaults_and_extra():
    rt_obj = SftpOptionsSpec(extra={"block_size": 32768}).to_runtime()
    assert isinstance(rt_obj, SftpOptions)
    assert rt_obj.env is None
    assert rt_obj.extra == {"block_size": 32768}


def test_scp_spec_defaults_match_runtime():
    rt_obj = ScpOptionsSpec().to_runtime()
    assert isinstance(rt_obj, ScpOptions)
    assert rt_obj.recurse is True
    assert rt_obj.block_size == 16384


def test_ftp_spec_coerces_passive_commands_to_tuple():
    rt_obj = FtpOptionsSpec(passive_commands=["pasv"]).to_runtime()
    assert isinstance(rt_obj, FtpOptions)
    assert rt_obj.passive_commands == ("pasv",)


def test_ftp_spec_defaults_match_runtime():
    rt_obj = FtpOptionsSpec().to_runtime()
    assert rt_obj.port == 21
    assert rt_obj.passive_commands == ("epsv", "pasv")


def test_nc_spec_defaults_match_runtime():
    rt_obj = NcOptionsSpec().to_runtime()
    assert isinstance(rt_obj, NcOptions)
    assert rt_obj.exec_name == "nc"
    assert rt_obj.port == 9000
    assert rt_obj.port_strategy == "auto"


def test_nc_spec_rejects_unknown_key():
    with pytest.raises(ValidationError):
        NcOptionsSpec(extra={"x": 1})  # otto-owned: no passthrough


def test_snmp_spec_coerces_oids_to_tuple():
    rt_obj = SnmpOptionsSpec(oids=["1.3.6.1.2.1.1.3.0"]).to_runtime()
    assert isinstance(rt_obj, SnmpOptions)
    assert rt_obj.oids == ("1.3.6.1.2.1.1.3.0",)


def test_snmp_spec_defaults_and_address():
    rt_obj = SnmpOptionsSpec(address="10.0.0.9").to_runtime()
    assert rt_obj.community == "public"
    assert rt_obj.port == 161
    assert rt_obj.version == "2c"
    assert rt_obj.address == "10.0.0.9"


def test_snmp_spec_rejects_bad_version():
    with pytest.raises(ValidationError):
        SnmpOptionsSpec(version="3")


def test_tftp_spec_defaults_match_runtime():
    rt_obj = TftpOptionsSpec(server_ip="10.0.0.2").to_runtime()
    assert isinstance(rt_obj, TftpOptions)
    assert rt_obj.port == 69
    assert rt_obj.block_size == 512
    assert rt_obj.server_ip == "10.0.0.2"


@pytest.mark.parametrize("spec_cls,runtime_cls", OPTION_SPEC_RUNTIME_PAIRS)
def test_spec_fields_subset_of_runtime(spec_cls, runtime_cls):
    spec_fields = set(spec_cls.model_fields)
    runtime_fields = {f.name for f in dataclasses.fields(runtime_cls)}
    missing = spec_fields - runtime_fields
    assert not missing, (
        f"{spec_cls.__name__} has fields absent from {runtime_cls.__name__}: {sorted(missing)}"
    )


# The forward value objects collapsed into frozen pydantic dataclasses
# (no separate *Spec), so OPTION_SPEC_RUNTIME_PAIRS now contains only the
# fully-defaulted option-table specs — all of which build a runtime with no args.
_DEFAULT_CONSTRUCTIBLE_PAIRS = OPTION_SPEC_RUNTIME_PAIRS


@pytest.mark.parametrize("spec_cls,runtime_cls", _DEFAULT_CONSTRUCTIBLE_PAIRS)
def test_default_spec_builds_runtime(spec_cls, runtime_cls):
    assert isinstance(spec_cls().to_runtime(), runtime_cls)
