"""A numeric option field takes a number at every entry point (#1001).

Lab data, ``[host_preferences]``, a ``HostOverrides`` value and a direct
construction all reach an options field. They share one type rule,
:func:`otto.host.options.refuse_non_number`, applied before any coercion, so
``down_timeout: false`` can no longer load as ``0.0`` and skip the down phase.
Each case drives the same input through every entry point its table has.
"""

import dataclasses

import pytest

from otto.host import options as rt
from otto.host.element import Element
from otto.host.factory import create_host_from_dict
from otto.host.option_specs import TftpOptionsSpec
from otto.models.settings import SettingsModel

# (lab-data key, runtime class, a float field or None, an int field or None).
# TFTP has no lab-data key on a host yet; its spec is driven directly.
TABLES = [
    ("ssh_options", rt.SshOptions, "connect_timeout", "port"),
    ("telnet_options", rt.TelnetOptions, "write_chunk_delay", "cols"),
    ("console_options", rt.ConsoleOptions, "settle", "write_chunk_size"),
    ("sftp_options", rt.SftpOptions, None, "max_concurrent_transfers"),
    ("scp_options", rt.ScpOptions, None, "block_size"),
    ("ftp_options", rt.FtpOptions, "socket_timeout", "read_speed_limit"),
    ("nc_options", rt.NcOptions, "listener_timeout", "port"),
    ("snmp", rt.SnmpOptions, None, "port"),
    (None, rt.TftpOptions, "timeout", "block_size"),
    ("reboot_options", rt.RebootOptions, "down_timeout", None),
]
# console_options, snmp and tftp are not settable under [host_preferences],
# so those tables have no preferences case.
PREFERENCE_TABLES = {
    "ssh_options",
    "telnet_options",
    "sftp_options",
    "scp_options",
    "ftp_options",
    "nc_options",
    "reboot_options",
}


def _lab_data(key, runtime, field, value):
    if key is None:
        return TftpOptionsSpec.model_validate({field: value}).to_runtime()
    data = {"ip": "10.0.0.1", "creds": [{"login": "admin", "password": "secret"}]}
    data[key] = {field: value}
    return create_host_from_dict(data, element=Element(name="lab"))


def _preferences(key, runtime, field, value):
    return SettingsModel.model_validate(
        {"name": "p", "version": "1.0.0", "host_preferences": {".*": {key: {field: value}}}}
    )


def _override(key, runtime, field, value):
    # A HostOverrides value is a runtime instance, typically derived from the defaults.
    return dataclasses.replace(runtime(), **{field: value})


def _direct(key, runtime, field, value):
    return runtime(**{field: value})


def _cases():
    for key, runtime, float_field, int_field in TABLES:
        for field, kind in ((float_field, float), (int_field, int)):
            if field is None:
                continue
            entries = [("lab", _lab_data), ("override", _override), ("direct", _direct)]
            if key in PREFERENCE_TABLES:
                entries.append(("preferences", _preferences))
            for name, entry in entries:
                yield pytest.param(
                    entry, key, runtime, field, kind, id=f"{runtime.__name__}.{field}-{name}"
                )


CASES = list(_cases())
BAD = [False, True, "5", "5.0", [5]]


@pytest.mark.parametrize("bad", BAD, ids=repr)
@pytest.mark.parametrize(("entry", "key", "runtime", "field", "kind"), CASES)
def test_a_non_number_is_refused_naming_the_field(entry, key, runtime, field, kind, bad):
    with pytest.raises((TypeError, ValueError), match=field):
        entry(key, runtime, field, bad)


@pytest.mark.parametrize(("entry", "key", "runtime", "field", "kind"), CASES)
def test_an_int_is_accepted_everywhere(entry, key, runtime, field, kind):
    entry(key, runtime, field, 7)


INT_CASES = [c for c in CASES if c.values[4] is int]


def _value(result, key, field):
    """Read *field* back from whatever an entry point returned."""
    if isinstance(result, SettingsModel):
        return result.host_preferences[".*"][key][field]
    if hasattr(result, "element"):
        return getattr(getattr(result, key), field)
    return getattr(result, field)


@pytest.mark.parametrize(("entry", "key", "runtime", "field", "kind"), CASES)
def test_a_float_field_takes_a_float_and_an_int_field_refuses_a_fraction(
    entry, key, runtime, field, kind
):
    if kind is float:
        entry(key, runtime, field, 7.5)
    else:
        with pytest.raises((TypeError, ValueError), match=field):
            entry(key, runtime, field, 7.5)


@pytest.mark.parametrize(("entry", "key", "runtime", "field", "kind"), INT_CASES)
def test_an_int_field_takes_a_whole_number_float_as_an_int(entry, key, runtime, field, kind):
    value = _value(entry(key, runtime, field, 2222.0), key, field)
    assert value == 2222
    assert type(value) is int


@pytest.mark.parametrize("bad", [float("inf"), float("nan")], ids=repr)
@pytest.mark.parametrize(("entry", "key", "runtime", "field", "kind"), INT_CASES)
def test_an_int_field_refuses_a_non_finite_float(entry, key, runtime, field, kind, bad):
    with pytest.raises((TypeError, ValueError), match=field):
        entry(key, runtime, field, bad)


# The SSH forwards are nested under ssh_options and are pydantic dataclasses of
# their own, so the tables above do not reach their ports.
FORWARDS = [
    ("local_forwards", rt.LocalPortForward, {"listen_host": "h", "dest_host": "d", "dest_port": 1}),
    (
        "remote_forwards",
        rt.RemotePortForward,
        {"listen_host": "h", "dest_host": "d", "dest_port": 1},
    ),
    ("socks_forwards", rt.SocksForward, {"listen_host": "h"}),
]


def _forward_lab(key, runtime, fields):
    host = _lab_data("ssh_options", rt.SshOptions, key, [fields])
    return getattr(host.ssh_options, key)[0]


def _forward_preferences(key, runtime, fields):
    model = _preferences("ssh_options", rt.SshOptions, key, [fields])
    return model.host_preferences[".*"]["ssh_options"][key][0]


def _forward_direct(key, runtime, fields):
    return runtime(**fields)


FORWARD_CASES = [
    pytest.param(entry, key, runtime, rest, id=f"{runtime.__name__}-{name}")
    for key, runtime, rest in FORWARDS
    for name, entry in (
        ("lab", _forward_lab),
        ("preferences", _forward_preferences),
        ("direct", _forward_direct),
    )
]


@pytest.mark.parametrize("bad", [*BAD, 7.5, float("nan")], ids=repr)
@pytest.mark.parametrize(("entry", "key", "runtime", "rest"), FORWARD_CASES)
def test_a_forward_port_refuses_a_non_number(entry, key, runtime, rest, bad):
    with pytest.raises((TypeError, ValueError), match="listen_port"):
        entry(key, runtime, {**rest, "listen_port": bad})


@pytest.mark.parametrize("good", [1080, 1080.0], ids=repr)
@pytest.mark.parametrize(("entry", "key", "runtime", "rest"), FORWARD_CASES)
def test_a_forward_port_takes_a_whole_number_as_an_int(entry, key, runtime, rest, good):
    forward = entry(key, runtime, {**rest, "listen_port": good})
    port = forward["listen_port"] if isinstance(forward, dict) else forward.listen_port
    assert port == 1080
    assert type(port) is int
