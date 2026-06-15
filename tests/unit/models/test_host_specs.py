import dataclasses
from pathlib import Path

import pytest
from pydantic import ValidationError

from otto.host.command_frame import ZephyrFrame
from otto.host.embedded_filesystem import NoFileSystem
from otto.host.embedded_host import EmbeddedHost
from otto.host.options import TelnetOptions
from otto.host.toolchain import Toolchain
from otto.host.unix_host import UnixHost
from otto.models.host import (
    HOST_SPEC_RUNTIME_PAIRS,
    EmbeddedHostSpec,
    HostSpec,
    ToolchainSpec,
    UnixHostSpec,
)
from otto.storage.factory import create_host_from_dict


def test_toolchain_spec_defaults_match_runtime():
    rt = ToolchainSpec().to_runtime()
    assert isinstance(rt, Toolchain)
    assert rt.sysroot == Path("/")
    assert rt.lcov == Path("usr/bin/lcov")
    assert rt.gcov == Path("usr/bin/gcov")


def test_toolchain_spec_coerces_str_paths():
    rt = ToolchainSpec(sysroot="/opt/arm", gcov="bin/arm-gcov").to_runtime()
    assert rt.sysroot == Path("/opt/arm")
    assert rt.gcov == Path("bin/arm-gcov")
    assert rt.lcov == Path("usr/bin/lcov")  # untouched default


def test_toolchain_spec_forbids_unknown():
    with pytest.raises(ValidationError):
        ToolchainSpec(sysrot="/x")  # typo


def test_hostspec_requires_ip_and_element():
    with pytest.raises(ValidationError) as exc:
        HostSpec(ip="10.0.0.1")  # missing element
    assert "element" in str(exc.value)


def test_hostspec_forbids_unknown_field():
    with pytest.raises(ValidationError) as exc:
        HostSpec(ip="10.0.0.1", element="lab", lab=["x"])  # typo: lab vs labs
    assert "lab" in str(exc.value)


def test_hostspec_accepts_labs_and_coerces_resources_to_set():
    spec = HostSpec(ip="10.0.0.1", element="lab", labs=["a"], resources=["r1", "r1"])
    assert spec.labs == ["a"]
    assert spec.resources == {"r1"}


def test_common_host_kwargs_omits_unset_and_excludes_labs():
    spec = HostSpec(ip="10.0.0.1", element="lab", labs=["a"])
    kw = spec._common_host_kwargs()
    assert "labs" not in kw                  # membership, never a host field
    assert kw["ip"] == "10.0.0.1"
    assert kw["element"] == "lab"
    # unset common fields are omitted so the host class's own default applies
    for absent in ("os_name", "resources", "telnet_options", "snmp", "toolchain"):
        assert absent not in kw


def test_common_host_kwargs_builds_nested_when_set():
    spec = HostSpec(
        ip="10.0.0.1", element="lab",
        resources=["r1"], telnet_options={"port": 99}, toolchain={"sysroot": "/opt"},
    )
    kw = spec._common_host_kwargs()
    assert kw["resources"] == {"r1"}
    assert isinstance(kw["telnet_options"], TelnetOptions) and kw["telnet_options"].port == 99
    assert isinstance(kw["toolchain"], Toolchain) and kw["toolchain"].sysroot == Path("/opt")


def test_unix_spec_requires_creds():
    with pytest.raises(ValidationError) as exc:
        UnixHostSpec(ip="10.0.0.1", element="lab")  # creds required for unix
    assert "creds" in str(exc.value)


def test_unix_spec_builds_unix_host_with_defaults():
    spec = UnixHostSpec(ip="10.0.0.1", element="lab", creds={"u": "p"})
    host = spec.to_host()
    assert isinstance(host, UnixHost)
    assert host.ip == "10.0.0.1"
    assert host.term == "ssh"
    assert host.transfer == "scp"
    assert host.os_type == "unix"
    assert host.ssh_options.port == 22


def test_unix_spec_builds_nested_options_and_snmp():
    spec = UnixHostSpec(
        ip="10.0.0.1", element="lab", creds={"u": "p"},
        ssh_options={"port": 2222, "extra": {"x": 1}},
        snmp={"oids": ["1.3.6.1.2.1.1.3.0"], "port": 16101},
        resources=["r1"], labs=["veggies"],
    )
    host = spec.to_host()
    assert host.ssh_options.port == 2222
    assert host.ssh_options.extra == {"x": 1}
    assert host.snmp is not None and host.snmp.oids == ("1.3.6.1.2.1.1.3.0",)
    assert host.resources == {"r1"}


def test_unix_spec_rejects_embedded_only_field():
    with pytest.raises(ValidationError):
        UnixHostSpec(ip="1.1.1.1", element="lab", creds={"u": "p"}, filesystem="littlefs")


def test_embedded_spec_builds_with_command_frame():
    spec = EmbeddedHostSpec(ip="192.0.2.1", element="dut", command_frame="zephyr")
    host = spec.to_host()
    assert isinstance(host, EmbeddedHost)
    assert host.os_type == "embedded"
    assert isinstance(host.command_frame, ZephyrFrame)


def test_embedded_spec_absent_filesystem_keeps_runtime_default():
    spec = EmbeddedHostSpec(ip="192.0.2.1", element="dut", command_frame="zephyr")
    host = spec.to_host()
    assert isinstance(host.filesystem, NoFileSystem)  # EmbeddedHost default


def test_embedded_spec_rejects_unknown_filesystem():
    # Now caught at validate-time by the field_validator, not at to_host().
    with pytest.raises(ValidationError) as exc:
        EmbeddedHostSpec(ip="192.0.2.1", element="dut", filesystem="bogusfs")
    assert "bogusfs" in str(exc.value)


def test_embedded_spec_accepts_registered_filesystem():
    # A registered filesystem name validates (resolved to an instance at build).
    spec = EmbeddedHostSpec(
        ip="192.0.2.1", element="dut", command_frame="zephyr", filesystem="none",
    )
    assert spec.filesystem == "none"
    # the validated name still resolves to its instance through build_filesystem
    assert isinstance(spec.to_host().filesystem, NoFileSystem)


def test_hostspec_rejects_unregistered_command_frame():
    with pytest.raises(ValidationError) as exc:
        UnixHostSpec(
            ip="10.0.0.1", element="lab", creds={"u": "p"},
            command_frame="nonesuch",
        )
    assert "nonesuch" in str(exc.value)


def test_embedded_spec_rejects_unix_only_field():
    with pytest.raises(ValidationError):
        EmbeddedHostSpec(ip="192.0.2.1", element="dut", docker_capable=True)


@pytest.mark.parametrize("spec_cls,runtime_cls", HOST_SPEC_RUNTIME_PAIRS)
def test_host_spec_fields_match_runtime_init(spec_cls, runtime_cls):
    """Bidirectional: every spec field maps to a constructor param AND every
    public init field of the runtime class is exposed by the spec. ``labs`` is
    the only allowed spec-only field (lab membership, not a host arg).
    """
    spec_fields = set(spec_cls.model_fields) - {"labs"}
    init_fields = {
        f.name for f in dataclasses.fields(runtime_cls)
        if f.init and not f.name.startswith("_")
    }
    assert spec_fields == init_fields, (
        f"{spec_cls.__name__} <-> {runtime_cls.__name__} field mismatch — "
        f"spec-only={sorted(spec_fields - init_fields)}, "
        f"runtime-only (spec forgot)={sorted(init_fields - spec_fields)}"
    )


def test_hostspec_interfaces_default_empty_and_passes_to_host():
    spec = UnixHostSpec(ip="10.0.0.1", element="lab", creds={"u": "p"})
    assert spec.interfaces == {}
    assert spec.to_host().interfaces == {}


def test_hostspec_interfaces_resolve_on_built_host():
    spec = UnixHostSpec(
        ip="10.0.0.1", element="lab", creds={"u": "p"},
        interfaces={"mgmt": "10.9.9.9"},
    )
    host = spec.to_host()
    assert host.interfaces == {"mgmt": "10.9.9.9"}
    assert host.address_for("mgmt") == "10.9.9.9"


def test_hostspec_interfaces_accepts_ipv6():
    spec = HostSpec(ip="10.0.0.1", element="lab", interfaces={"v6": "2001:db8::1"})
    assert spec.interfaces["v6"] == "2001:db8::1"


def test_hostspec_interfaces_rejects_non_ip_value():
    with pytest.raises(ValidationError) as exc:
        HostSpec(ip="10.0.0.1", element="lab", interfaces={"mgmt": "not-an-ip"})
    assert "mgmt" in str(exc.value)


def test_unix_to_host_matches_factory():
    d = {
        "ip": "10.10.200.11", "element": "carrot", "os_type": "unix",
        "board": "seed", "term": "ssh", "transfer": "scp", "is_virtual": True,
        "creds": {"vagrant": "vagrant"}, "resources": ["carrot"], "labs": ["veggies"],
        "ssh_options": {"port": 2200},
    }
    spec_host = UnixHostSpec.model_validate(d).to_host()
    factory_host = create_host_from_dict(d)
    for attr in ("ip", "element", "os_type", "os_name", "os_version", "board",
                 "term", "transfer", "is_virtual", "creds", "resources", "name",
                 "hop", "user"):
        assert getattr(spec_host, attr) == getattr(factory_host, attr), attr
    assert spec_host.ssh_options.port == factory_host.ssh_options.port == 2200


def test_embedded_to_host_matches_factory():
    d = {
        "ip": "192.0.2.1", "element": "dut", "os_type": "embedded",
        "command_frame": "zephyr", "telnet_options": {"port": 9023},
    }
    spec_host = EmbeddedHostSpec.model_validate(d).to_host()
    factory_host = create_host_from_dict(d)
    assert type(spec_host) is type(factory_host)
    assert spec_host.telnet_options.port == factory_host.telnet_options.port == 9023
    assert type(spec_host.command_frame) is type(factory_host.command_frame)


def test_unix_spec_accepts_command_frame_string():
    from otto.host.command_frame import BashFrame
    spec = UnixHostSpec(
        ip="10.0.0.1", element="lab", creds={"u": "p"}, command_frame="bash",
    )
    host = spec.to_host()
    assert isinstance(host.command_frame, BashFrame)


def test_unix_spec_omits_command_frame_when_unset():
    spec = UnixHostSpec(ip="10.0.0.1", element="lab", creds={"u": "p"})
    # unset -> not passed -> UnixHost default (None -> SessionManager BashFrame)
    assert "command_frame" not in spec._common_host_kwargs()
    assert spec.to_host().command_frame is None


def test_registered_pairs_drift_guard():
    """Every registered (host_class, spec) pair has matching field sets — the
    same bidirectional check as HOST_SPEC_RUNTIME_PAIRS, but sourced from the
    live registry so it covers built-ins registered through register_host_class.
    """
    from otto.host.os_profile import _HOST_CLASSES, _HOST_SPECS
    for name, spec_cls in _HOST_SPECS.items():
        runtime_cls = _HOST_CLASSES[name]
        spec_fields = set(spec_cls.model_fields) - {"labs"}
        init_fields = {
            f.name for f in dataclasses.fields(runtime_cls)
            if f.init and not f.name.startswith("_")
        }
        assert spec_fields == init_fields, (
            f"{name}: {spec_cls.__name__} <-> {runtime_cls.__name__} mismatch — "
            f"spec-only={sorted(spec_fields - init_fields)}, "
            f"runtime-only={sorted(init_fields - spec_fields)}"
        )
