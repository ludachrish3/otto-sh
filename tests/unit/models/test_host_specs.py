import dataclasses
from pathlib import Path
from typing import Annotated, ClassVar

import pytest
from pydantic import Field, ValidationError

from otto.host import HostValidationError, host_context
from otto.host.command_frame import ZephyrFrame
from otto.host.construction import Consume
from otto.host.element import Element
from otto.host.embedded_filesystem import NoFileSystem
from otto.host.embedded_host import EmbeddedHost
from otto.host.factory import create_host_from_dict, validate_host_dict
from otto.host.interface import Interface
from otto.host.login_proxy import Cred, via_cred
from otto.host.options import TelnetOptions
from otto.host.os_profile import register_host_class
from otto.host.specs import (
    EmbeddedHostSpec,
    HostSpec,
    ToolchainSpec,
    UnixHostSpec,
)
from otto.host.toolchain import Toolchain
from otto.host.unix_host import UnixHost
from otto.link import LinkImpairer, register_impairer
from otto.logger.mode import LogMode
from otto.models.base import CredSpec
from tests._fixtures.host_build import build_host


def _minimal_unix_kwargs() -> dict:
    """Smallest kwargs that build a valid ``UnixHostSpec``."""
    return {"ip": "10.0.0.1", "creds": [{"login": "u", "password": "p"}]}


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
    with pytest.raises(ValidationError, match=r"sysrot\s+Extra inputs are not permitted"):
        ToolchainSpec(sysrot="/x")  # typo


def test_toolchain_spec_tools_reach_runtime():
    # Kills: adding the spec field but forgetting the to_runtime() mapping —
    # lab.json-declared tools would silently vanish.
    spec = ToolchainSpec(tools=[{"name": "gdb", "source": "/tc/bin/gdb", "dest": "/usr/local/bin"}])
    tc = spec.to_runtime()
    assert tc.tools[0].name == "gdb"
    assert tc.tools[0].user == "root"


def test_toolchain_spec_tool_paths_and_mode_reach_runtime():
    # Kills: dropping any of source/dest/mode on the way across the boundary,
    # and kills leaving them as raw strings (the runtime wants Paths).
    spec = ToolchainSpec(
        tools=[
            {"name": "gdbinit", "source": "/tc/gdbinit", "dest": "/etc", "mode": "644"},
        ]
    )
    tool = spec.to_runtime().tools[0]
    assert (tool.source, tool.dest, tool.mode) == (Path("/tc/gdbinit"), Path("/etc"), "644")


def test_toolchain_spec_declares_no_tools_by_default():
    assert ToolchainSpec().to_runtime().tools == []


def test_toolchain_spec_declares_no_lcov_args_by_default():
    assert ToolchainSpec().to_runtime().lcov_args == []


def test_toolchain_spec_lcov_args_reach_runtime():
    # Kills: adding the spec field but forgetting the to_runtime() mapping —
    # a lab.json-declared lcov argument would silently vanish before it could
    # reach the capture command.
    spec = ToolchainSpec(lcov_args=["--ignore-errors", "source"])
    assert spec.to_runtime().lcov_args == ["--ignore-errors", "source"]


def test_toolchain_spec_rejects_an_entry_holding_a_whole_command_line():
    # One argv token per entry: otto quotes each entry, so a shell-style
    # "--ignore-errors source" would reach lcov as ONE unknown argument.
    with pytest.raises(ValidationError, match=r"'--ignore-errors source' contains whitespace"):
        ToolchainSpec(lcov_args=["--ignore-errors source"])


def test_toolchain_spec_rejects_a_whitespace_entry_naming_the_list_form():
    with pytest.raises(ValidationError, match="each entry is one argument") as exc:
        ToolchainSpec(lcov_args=["--rc", "geninfo_adjust_src_path=/a /b"])
    assert 'write it as ["--ignore-errors", "source"]' in str(exc.value)
    assert "toolchain.lcov_args" in str(exc.value)


def test_toolchain_tool_spec_forbids_unknown():
    with pytest.raises(ValidationError, match=r"dst\s+Extra inputs are not permitted"):
        ToolchainSpec(tools=[{"name": "gdb", "source": "/a", "dst": "/b"}])


def test_hostspec_requires_ip():
    with pytest.raises(ValidationError, match=r"ip\s+Field required") as exc:
        HostSpec()  # missing ip — the only required field a host entry has
    assert "ip" in str(exc.value)


def test_hostspec_forbids_unknown_field():
    # Anchored loc line: a bare substring assert could not discriminate — "lab"
    # is a substring of element="lab"'s input_value echo; only the ^lab$ line
    # pins the unknown key itself.
    with pytest.raises(ValidationError, match=r"(?m)^lab\n\s+Extra inputs are not permitted"):
        HostSpec(ip="10.0.0.1", lab=["x"])


@pytest.mark.parametrize(("raw", "expected"), [(3, 3), ("3", 3), ("R3", "R3")])
def test_hostspec_site_and_rack_coerce_digit_strings(raw, expected):
    hc = host_context(
        {
            "ip": "10.0.0.1",
            "creds": [{"login": "u", "password": "p"}],
            "site": raw,
            "rack": raw,
            "shelf": 2,
        },
        element=Element("lab"),
    )
    assert hc.spec.site == expected
    assert hc.spec.rack == expected
    host = hc.spec.to_host(ready=hc.spec.validate_in(hc.context))
    assert (host.site, host.rack, host.shelf) == (expected, expected, 2)


def test_location_fields_default_none_and_stay_out_of_the_id():
    host = build_host(
        {"ip": "10.0.0.1", "creds": [{"login": "u", "password": "p"}], "board": "cx", "slot": 1}
    )
    assert (host.site, host.rack, host.shelf) == (None, None, None)
    assert host.id == "lab-cx-1"


def test_hostspec_shelf_rejects_negative():
    with pytest.raises(ValidationError, match="shelf"):
        UnixHostSpec(ip="10.0.0.1", creds=[{"login": "u", "password": "p"}], shelf=-1)


def _kwargs(entry, element=None):
    """The validated spec, and the constructor kwargs the stock builder makes of its record."""
    hc = host_context(entry, element=element if element is not None else Element("lab"))
    return hc.spec, hc.spec.build_kwargs(hc.spec.validate_in(hc.context))


def test_build_kwargs_omits_unset_fields_and_supplies_the_element():
    element = Element("lab")
    _, kw = _kwargs({"ip": "10.0.0.1"}, element)
    assert kw["ip"] == "10.0.0.1"
    assert kw["element"] is element
    # unset fields are omitted so the host class's own default applies
    for absent in ("os_name", "metadata", "telnet_options", "snmp", "toolchain"):
        assert absent not in kw


def test_build_kwargs_builds_nested_fields_when_set():
    spec, kw = _kwargs(
        {
            "ip": "10.0.0.1",
            "metadata": {"owner": "infra"},
            "telnet_options": {"port": 99},
            "toolchain": {"sysroot": "/opt"},
        }
    )
    assert kw["metadata"] == {"owner": "infra"}
    assert kw["metadata"] is not spec.metadata  # copied, never aliased
    assert isinstance(kw["telnet_options"], TelnetOptions)
    assert kw["telnet_options"].port == 99
    assert isinstance(kw["toolchain"], Toolchain)
    assert kw["toolchain"].sysroot == Path("/opt")


def test_unix_spec_without_creds_builds_a_credless_host():
    """``creds`` is optional: a console, or a host whose creds come from elsewhere.

    The runtime already connects such a host loginless, so the spec must not
    refuse what the host can do.
    """
    hc = host_context({"ip": "10.0.0.1"}, element=Element("lab"))
    assert hc.spec.creds == []
    host = hc.spec.to_host(ready=hc.spec.validate_in(hc.context))
    assert isinstance(host, UnixHost)
    assert host.creds == []
    assert host.default_cred is None


@dataclasses.dataclass(slots=True, kw_only=True)
class _DeviceDefaultHost(UnixHost):
    """A custom host class whose own creds default is the device's account."""

    creds: list[Cred] = dataclasses.field(
        default_factory=lambda: [Cred(login="device", password="d")]
    )


def test_a_credless_spec_keeps_a_host_subclass_s_own_creds_default():
    """Omitted creds fall through to the TARGET class's default, as every other
    unset field does: here the device's account rather than UnixHost's ``[]``."""
    register_host_class("t-device-default", _DeviceDefaultHost)
    host = build_host({"ip": "10.0.0.1", "os_type": "t-device-default"})
    assert isinstance(host, _DeviceDefaultHost)
    assert [c.login for c in host.creds] == ["device"]


def test_explicit_creds_still_beat_a_host_subclass_s_default():
    register_host_class("t-device-default", _DeviceDefaultHost)
    host = build_host(
        {
            "ip": "10.0.0.1",
            "creds": [{"login": "lab", "password": "p"}],
            "os_type": "t-device-default",
        }
    )
    assert [c.login for c in host.creds] == ["lab"]


class _ConstructorDefaultHost(UnixHost):
    """An undecorated host class whose own constructor defaults creds.

    Its dataclass field is UnixHost's (defaulting to ``[]``); the signature's
    own default is the one that applies.
    """

    def __init__(self, creds: list[Cred] | None = None, **kwargs) -> None:
        if creds is None:
            creds = [Cred(login="device", password="d")]
        UnixHost.__init__(self, creds=creds, **kwargs)


@dataclasses.dataclass(slots=True, kw_only=True)
class _SelfCredHost(UnixHost):
    """A host class that sets creds itself: ``field(init=False)``, filled in ``__post_init__``."""

    creds: list[Cred] = dataclasses.field(init=False)

    def __post_init__(self) -> None:
        self.creds = [Cred(login="self", password="s")]
        UnixHost.__post_init__(self)


class _SelfCredSpec(UnixHostSpec):
    """The paired spec of a class that sets its own creds: the entry's creds are never passed."""

    creds: Annotated[list[CredSpec], Consume("the class sets its own creds")] = Field(
        default_factory=list
    )


def test_a_credless_spec_keeps_a_host_constructor_s_creds_default():
    """The constructor's contract decides, not the dataclass field: a default
    in a hand-written ``__init__`` applies when the entry omits creds."""
    register_host_class("t-constructor-default", _ConstructorDefaultHost)
    host = build_host({"ip": "10.0.0.1", "os_type": "t-constructor-default"})
    assert isinstance(host, _ConstructorDefaultHost)
    assert [c.login for c in host.creds] == ["device"]


def test_explicit_creds_still_beat_a_host_constructor_s_default():
    register_host_class("t-constructor-default", _ConstructorDefaultHost)
    host = build_host(
        {
            "ip": "10.0.0.1",
            "creds": [{"login": "lab", "password": "p"}],
            "os_type": "t-constructor-default",
        }
    )
    assert [c.login for c in host.creds] == ["lab"]


def test_a_credless_spec_builds_a_host_that_sets_its_own_creds():
    """A constructor with no ``creds`` parameter at all is passed none."""
    register_host_class("t-self-cred", _SelfCredHost, spec=_SelfCredSpec)
    host = build_host({"ip": "10.0.0.1", "os_type": "t-self-cred"})
    assert isinstance(host, _SelfCredHost)
    assert [c.login for c in host.creds] == ["self"]


class _PassthroughHost(UnixHost):
    """A wrapper ``__init__`` that forwards everything: its signature names no ``creds``."""

    def __init__(self, *args, **kwargs) -> None:
        UnixHost.__init__(self, *args, **kwargs)


class _PassthroughSelfCredHost(_SelfCredHost):
    """The same passthrough over a class whose creds field is ``init=False``."""

    def __init__(self, *args, **kwargs) -> None:
        _SelfCredHost.__init__(self, *args, **kwargs)


def test_a_credless_spec_builds_a_passthrough_host_credless():
    """Behind ``**kwargs`` the dataclass field speaks: UnixHost's creds defaults to ``[]``."""
    register_host_class("t-passthrough", _PassthroughHost)
    host = build_host({"ip": "10.0.0.1", "os_type": "t-passthrough"})
    assert isinstance(host, _PassthroughHost)
    assert host.creds == []


def test_a_credless_spec_keeps_a_passthrough_host_s_own_creds():
    """An ``init=False`` field behind the passthrough is not passed: the class sets it."""
    register_host_class("t-passthrough-self-cred", _PassthroughSelfCredHost, spec=_SelfCredSpec)
    host = build_host({"ip": "10.0.0.1", "os_type": "t-passthrough-self-cred"})
    assert isinstance(host, _PassthroughSelfCredHost)
    assert [c.login for c in host.creds] == ["self"]


def test_unix_spec_builds_unix_host_with_defaults():
    host = build_host({"ip": "10.0.0.1", "creds": [{"login": "u", "password": "p"}]})
    assert isinstance(host, UnixHost)
    assert host.ip == "10.0.0.1"
    assert host.term == "ssh"
    assert host.transfer == "scp"
    assert host.os_type == "unix"
    assert host.ssh_options.port == 22


def test_hostspec_log_default_is_normal_on_spec_and_runtime():
    hc = host_context(_minimal_unix_kwargs(), element=Element("lab"))
    assert hc.spec.log is LogMode.NORMAL
    assert hc.spec.to_host(ready=hc.spec.validate_in(hc.context)).log is LogMode.NORMAL


def test_hostspec_log_rejects_bool_and_names_the_modes():
    # The v1 ``log = true/false`` back-compat coercion is gone (spec §10): a
    # bool now errors, and the error teaches the vocabulary that replaced it.
    for value in (False, True):
        with pytest.raises(
            ValidationError, match=r"Input should be 'normal', 'quiet' or 'never'"
        ) as exc:
            UnixHostSpec.model_validate(_minimal_unix_kwargs() | {"log": value})
        assert "log" in str(exc.value)


def test_hostspec_log_accepts_logmode_string():
    spec = UnixHostSpec.model_validate(_minimal_unix_kwargs() | {"log": "quiet"})
    assert spec.log is LogMode.QUIET


def test_unix_spec_builds_nested_options_and_snmp():
    host = build_host(
        {
            "ip": "10.0.0.1",
            "creds": [{"login": "u", "password": "p"}],
            "ssh_options": {"port": 2222, "extra": {"x": 1}},
            "snmp": {"oids": ["1.3.6.1.2.1.1.3.0"], "port": 16101},
            "metadata": {"owner": "infra"},
        }
    )
    assert host.ssh_options.port == 2222
    assert host.ssh_options.extra == {"x": 1}
    assert host.snmp is not None
    assert host.snmp.oids == ("1.3.6.1.2.1.1.3.0",)
    assert host.metadata == {"owner": "infra"}


def test_unix_spec_rejects_embedded_only_field():
    with pytest.raises(ValidationError, match=r"filesystem\s+Extra inputs are not permitted"):
        UnixHostSpec(
            ip="1.1.1.1",
            creds=[{"login": "u", "password": "p"}],
            filesystem="littlefs",
        )


def test_embedded_spec_builds_with_command_frame():
    host = build_host({"ip": "192.0.2.1", "os_type": "embedded", "command_frame": "zephyr"})
    assert isinstance(host, EmbeddedHost)
    assert host.os_type == "embedded"
    assert isinstance(host.command_frame, ZephyrFrame)


def test_embedded_spec_absent_filesystem_keeps_runtime_default():
    host = build_host({"ip": "192.0.2.1", "os_type": "embedded", "command_frame": "zephyr"})
    assert isinstance(host.filesystem, NoFileSystem)  # EmbeddedHost default


def test_embedded_spec_rejects_unknown_filesystem():
    # Now caught at validate-time by the field_validator, not at to_host().
    with pytest.raises(ValidationError, match="is not a registered filesystem") as exc:
        EmbeddedHostSpec(ip="192.0.2.1", filesystem="bogusfs")
    assert "bogusfs" in str(exc.value)


def test_embedded_spec_accepts_registered_filesystem():
    # A registered filesystem name validates (resolved to an instance at build).
    hc = host_context(
        {
            "ip": "192.0.2.1",
            "os_type": "embedded",
            "command_frame": "zephyr",
            "filesystem": "none",
        },
        element=Element("lab"),
    )
    assert hc.spec.filesystem == "none"
    # the validated name still resolves to its instance through build_filesystem
    assert isinstance(
        hc.spec.to_host(ready=hc.spec.validate_in(hc.context)).filesystem, NoFileSystem
    )


def test_hostspec_rejects_unregistered_command_frame():
    with pytest.raises(ValidationError, match="is not a registered frame") as exc:
        UnixHostSpec(
            ip="10.0.0.1",
            creds=[{"login": "u", "password": "p"}],
            command_frame="nonesuch",
        )
    assert "nonesuch" in str(exc.value)


def test_embedded_spec_rejects_unix_only_field():
    with pytest.raises(ValidationError, match=r"docker_capable\s+Extra inputs are not permitted"):
        EmbeddedHostSpec(ip="192.0.2.1", docker_capable=True)


def test_host_resources_reach_the_runtime_host_as_a_frozenset_copy():
    """Spec 2026-08-28 three-level-reservations §2, §3: a host declares its slot.

    The negative is anchored on the validator's exact sentence, not a loose
    ``resources.*non-empty``: the failure text carries this test's locals, so a
    wide pattern is satisfiable by the test's own names.
    """
    host = build_host({**_minimal_unix_kwargs(), "resources": ["slot-1", "slot-1"]})
    assert host.resources == frozenset({"slot-1"})
    assert isinstance(host.resources, frozenset)

    bare = build_host(_minimal_unix_kwargs(), element=Element("x"))
    assert bare.resources == frozenset()

    with pytest.raises(ValidationError, match=r"resources must be non-empty strings"):
        UnixHostSpec(**_minimal_unix_kwargs(), resources=[" "])


# The BaseHost contract sweep lived here until spec 2026-09-09 made BaseHost a
# dataclass; its hazard (a bare annotation with no runtime field) is now
# structurally impossible and `tests/unit/host/test_field_homes.py` holds the
# new shape.


def test_every_registered_pair_passes_the_coverage_rule():
    from otto.host.construction import check_coverage
    from otto.host.os_profile import build_host_class, registered_host_specs

    for name, spec_cls in registered_host_specs().items():
        check_coverage(name, build_host_class(name), spec_cls)


def test_spec_inventory_key_never_reaches_the_host():
    """``inventory`` is spec-only (the loader's join key, §5) — it is ``Consume``, so the
    coverage rule never asks the host for it, and this pins the property that matters
    directly: it must never become a host attribute.
    """
    # The pipeline refuses an unresolved inventory key, so the spec that carries one is
    # validated directly, in the context of the same entry without it.
    hc = host_context(
        {"ip": "10.0.0.1", "creds": [{"login": "u", "password": "p"}]}, element=Element("lab")
    )
    spec = UnixHostSpec(ip="10.0.0.1", creds=[{"login": "u", "password": "p"}], inventory="k")
    host = spec.to_host(ready=spec.validate_in(hc.context))
    assert not hasattr(host, "inventory")


def test_hostspec_interfaces_default_empty_and_passes_to_host():
    hc = host_context(
        {"ip": "10.0.0.1", "creds": [{"login": "u", "password": "p"}]}, element=Element("lab")
    )
    assert hc.spec.interfaces == {}
    assert hc.spec.to_host(ready=hc.spec.validate_in(hc.context)).interfaces == {}


def test_hostspec_interfaces_resolve_on_built_host():
    host = build_host(
        {
            "ip": "10.0.0.1",
            "creds": [{"login": "u", "password": "p"}],
            "interfaces": {"mgmt": "10.9.9.9"},
        }
    )
    assert host.interfaces == {"mgmt": Interface(ip="10.9.9.9")}
    assert host.address_for("mgmt") == "10.9.9.9"


def test_hostspec_interfaces_accepts_ipv6():
    spec = HostSpec(ip="10.0.0.1", interfaces={"v6": "2001:db8::1"})
    assert spec.interfaces["v6"].ip == "2001:db8::1"


def test_hostspec_interfaces_rejects_non_ip_value():
    with pytest.raises(ValidationError, match="is not a valid IP") as exc:
        HostSpec(ip="10.0.0.1", interfaces={"mgmt": "not-an-ip"})
    assert "mgmt" in str(exc.value)


def test_unix_to_host_matches_factory():
    d = {
        "ip": "10.10.200.11",
        "os_type": "unix",
        "board": "qemu",
        "term": "ssh",
        "transfer": "scp",
        "is_virtual": True,
        "creds": [{"login": "vagrant", "password": "vagrant"}],
        "metadata": {"owner": "infra"},
        "ssh_options": {"port": 2200},
    }
    hc = host_context(dict(d), element=Element("test1"))
    spec_host = hc.spec.to_host(ready=hc.spec.validate_in(hc.context))
    factory_host = create_host_from_dict(d, element=Element("test1"))
    for attr in (
        "ip",
        "element",
        "os_type",
        "os_name",
        "os_version",
        "board",
        "term",
        "transfer",
        "is_virtual",
        "creds",
        "metadata",
        "name",
        "hop",
    ):
        assert getattr(spec_host, attr) == getattr(factory_host, attr), attr
    assert spec_host.ssh_options.port == factory_host.ssh_options.port == 2200


def test_embedded_to_host_matches_factory():
    d = {
        "ip": "192.0.2.1",
        "os_type": "embedded",
        "command_frame": "zephyr",
        "telnet_options": {"port": 9023},
    }
    hc = host_context(dict(d), element=Element("dut"))
    spec_host = hc.spec.to_host(ready=hc.spec.validate_in(hc.context))
    factory_host = create_host_from_dict(d, element=Element("dut"))
    assert type(spec_host) is type(factory_host)
    assert spec_host.telnet_options.port == factory_host.telnet_options.port == 9023
    assert type(spec_host.command_frame) is type(factory_host.command_frame)


def test_unix_spec_accepts_command_frame_string():
    from otto.host.command_frame import BashFrame

    host = build_host(
        {"ip": "10.0.0.1", "creds": [{"login": "u", "password": "p"}], "command_frame": "bash"}
    )
    assert isinstance(host.command_frame, BashFrame)


def test_unix_spec_omits_command_frame_when_unset():
    entry = {"ip": "10.0.0.1", "creds": [{"login": "u", "password": "p"}]}
    # unset -> not passed -> UnixHost default (None -> SessionManager BashFrame)
    _, kw = _kwargs(dict(entry))
    assert "command_frame" not in kw
    assert build_host(entry).command_frame is None


def test_spec_power_control_coerces_through_to_host():
    """A lab-data ``[power]`` table on the spec builds a runtime controller."""
    from otto.host.power import CommandPowerController

    host = build_host(
        {
            "ip": "10.0.0.1",
            "creds": [{"login": "u", "password": "p"}],
            "power_control": {
                "type": "command",
                "on_cmd": "on {name}",
                "off_cmd": "off {name}",
                "controller": "hyp",
            },
        }
    )
    assert isinstance(host.power_control, CommandPowerController)
    assert host.power_control.config.controller == "hyp"


def test_spec_unset_power_control_defaults_none():
    """Unset power_control falls through to the runtime host default; products is
    not a spec field, so the host keeps its own empty default.
    """
    host = build_host(
        {"ip": "10.0.0.1", "creds": [{"login": "u", "password": "p"}]}, element=Element("x")
    )
    assert host.power_control is None
    assert host.products == []


def test_spec_rejects_products_as_lab_data():
    """``products`` is project-logic-applied, not lab data — lab.json must not
    declare it (extra='forbid' rejects the key).
    """
    with pytest.raises(ValidationError, match=r"products\s+Extra inputs are not permitted"):
        UnixHostSpec(ip="10.0.0.1", creds=[{"login": "u", "password": "p"}], products=[])


class TestMenuValidation:
    def test_unix_default_menus(self):
        spec = UnixHostSpec(ip="10.0.0.1", creds=[{"login": "u", "password": "p"}])
        assert spec.valid_terms == ["ssh", "telnet"]
        assert spec.valid_transfers == ["scp", "sftp", "ftp", "nc"]
        assert spec.term is None
        assert spec.transfer is None

    def test_embedded_default_menus(self):
        spec = EmbeddedHostSpec(
            ip="10.0.0.1",
        )
        assert spec.valid_terms == ["telnet"]
        assert spec.valid_transfers == ["console"]

    def test_scalar_coerces_to_one_element_menu(self):
        spec = UnixHostSpec(
            ip="10.0.0.1",
            creds=[{"login": "u", "password": "p"}],
            valid_terms="ssh",
            valid_transfers="scp",
        )
        assert spec.valid_terms == ["ssh"]
        assert spec.valid_transfers == ["scp"]

    def test_list_menu_preserved_in_order(self):
        spec = UnixHostSpec(
            ip="10.0.0.1",
            creds=[{"login": "u", "password": "p"}],
            valid_transfers=["nc", "scp"],
        )
        assert spec.valid_transfers == ["nc", "scp"]

    def test_unknown_term_in_menu_raises(self):
        with pytest.raises(ValueError, match="not a registered term backend"):
            UnixHostSpec(
                ip="1.1.1.1",
                creds=[{"login": "u", "password": "p"}],
                valid_terms=["bogus"],
            )

    def test_unknown_unix_transfer_in_menu_raises(self):
        with pytest.raises(ValueError, match="not a registered transfer backend"):
            UnixHostSpec(
                ip="1.1.1.1",
                creds=[{"login": "u", "password": "p"}],
                valid_transfers=["bogus"],
            )

    def test_unix_rejects_embedded_only_transfer_in_menu(self):
        with pytest.raises(ValueError, match="not valid on a unix host"):
            UnixHostSpec(
                ip="1.1.1.1",
                creds=[{"login": "u", "password": "p"}],
                valid_transfers=["console"],
            )

    def test_embedded_rejects_unix_only_transfer_in_menu(self):
        with pytest.raises(ValueError, match="not valid on an embedded host"):
            EmbeddedHostSpec(ip="1.1.1.1", valid_transfers=["scp"])

    def test_empty_menu_rejected(self):
        with pytest.raises(ValueError, match="must be a non-empty"):
            UnixHostSpec(
                ip="1.1.1.1",
                creds=[{"login": "u", "password": "p"}],
                valid_transfers=[],
            )

    def test_embedded_rejects_unix_only_term_in_menu(self):
        with pytest.raises(ValueError, match=r"term 'ssh' is not valid on an embedded host"):
            EmbeddedHostSpec(ip="1.1.1.1", command_frame="zephyr", valid_terms=["ssh"])

    def test_unix_accepts_telnet_term(self):
        spec = UnixHostSpec(
            ip="1.1.1.1",
            creds=[{"login": "root", "password": "x"}],
            valid_terms=["telnet"],
        )
        assert spec.valid_terms == ["telnet"]

    def test_embedded_accepts_telnet_term(self):
        spec = EmbeddedHostSpec(ip="1.1.1.1", command_frame="zephyr", valid_terms=["telnet"])
        assert spec.valid_terms == ["telnet"]


class TestPreferenceResolution:
    def test_preference_in_menu_becomes_active(self):
        entry = {
            "ip": "1.1.1.1",
            "creds": [{"login": "root", "password": "x"}],
            "valid_transfers": ["scp", "sftp"],
        }
        host = build_host(entry, selections={"transfer": ["sftp"]})
        assert host.transfer == "sftp"

    def test_preference_out_of_menu_is_skipped(self):
        entry = {
            "ip": "1.1.1.1",
            "creds": [{"login": "root", "password": "x"}],
            "valid_transfers": ["scp", "nc"],
        }
        host = build_host(entry, selections={"transfer": ["sftp", "nc"]})
        # sftp not in menu -> skipped; nc is the first preference in the menu
        assert host.transfer == "nc"

    def test_preference_beats_pin(self):
        # Product preference now wins over the lab pin when the preference is in menu.
        entry = {
            "ip": "1.1.1.1",
            "creds": [{"login": "root", "password": "x"}],
            "valid_transfers": ["scp", "sftp"],
            "transfer": "scp",
        }
        host = build_host(entry, selections={"transfer": ["sftp"]})
        assert host.transfer == "sftp"

    def test_pin_still_validated_when_preference_overrides(self):
        # A bad lab pin is refused at validation, so a preference never gets to override it.
        import pytest

        with pytest.raises(ValidationError, match="transfer 'nc' is not in"):
            UnixHostSpec(
                ip="1.1.1.1",
                creds=[{"login": "root", "password": "x"}],
                valid_transfers=["scp", "sftp"],
                transfer="nc",
            )

    def test_no_preference_uses_menu_first(self):
        host = build_host(
            {
                "ip": "1.1.1.1",
                "creds": [{"login": "root", "password": "x"}],
                "valid_terms": ["telnet", "ssh"],
            }
        )
        assert host.term == "telnet"


class TestImpairerValidation:
    def test_default_menu_is_netem(self) -> None:
        spec = UnixHostSpec(ip="1.1.1.1", creds=[{"login": "u", "password": "p"}])
        assert spec.valid_impairers == ["netem"]
        assert spec.impairer is None  # pin unset; resolved at to_host

    def test_scalar_impairer_coerces_to_one_element_menu(self) -> None:
        spec = UnixHostSpec(
            ip="1.1.1.1",
            creds=[{"login": "u", "password": "p"}],
            valid_impairers="netem",
        )
        assert spec.valid_impairers == ["netem"]

    def test_unknown_impairer_in_menu_raises(self) -> None:
        with pytest.raises(ValueError, match="not a registered impairer"):
            UnixHostSpec(
                ip="1.1.1.1",
                creds=[{"login": "u", "password": "p"}],
                valid_impairers=["bogus"],
            )

    def test_family_inapplicable_impairer_rejected(self) -> None:
        class _EmbeddedOnly(LinkImpairer):
            host_families: ClassVar[frozenset[str]] = frozenset({"embedded"})

        register_impairer("embedded-only", _EmbeddedOnly)
        entry = {
            "ip": "1.1.1.1",
            "creds": [{"login": "u", "password": "p"}],
            "valid_impairers": ["embedded-only"],
        }
        UnixHostSpec.model_validate(entry)  # structural: membership only
        with pytest.raises(HostValidationError, match="not valid on a unix host"):
            validate_host_dict(entry, element=Element("e"))

    def test_empty_menu_rejected(self) -> None:
        with pytest.raises(ValueError, match="must be a non-empty"):
            UnixHostSpec(
                ip="1.1.1.1",
                creds=[{"login": "u", "password": "p"}],
                valid_impairers=[],
            )

    def test_pin_out_of_menu_rejected_at_validation(self) -> None:
        with pytest.raises(ValidationError, match="impairer 'fake' is not in"):
            UnixHostSpec(
                ip="1.1.1.1",
                creds=[{"login": "u", "password": "p"}],
                impairer="fake",
            )

    def test_to_host_resolves_family_default(self) -> None:
        host = build_host({"ip": "1.1.1.1", "creds": [{"login": "u", "password": "p"}]})
        assert host.impairer == "netem"

    def test_preference_beats_default(self) -> None:
        class _Fake(LinkImpairer):
            host_families: ClassVar[frozenset[str]] = frozenset({"unix"})

        register_impairer("fake", _Fake)
        entry = {
            "ip": "1.1.1.1",
            "creds": [{"login": "u", "password": "p"}],
            "valid_impairers": ["netem", "fake"],
        }
        host = build_host(entry, selections={"impairer": ["fake"]})
        assert host.impairer == "fake"


CRED_BASE = {"ip": "10.0.0.1"}


def _cred_spec(creds, **extra):
    return UnixHostSpec.model_validate({**CRED_BASE, "creds": creds, **extra})


def _cred_host(creds, **extra):
    return build_host({**CRED_BASE, "creds": creds, **extra})


class TestCredSpec:
    def test_creds_list_minimal(self):
        host = _cred_host([{"login": "admin", "password": "pw"}])
        assert host.creds[0].login == "admin"
        assert host.creds[0].password == "pw"

    def test_creds_proxied_entry_roundtrip(self):
        host = _cred_host(
            [
                {"login": "admin", "password": "pw"},
                {"login": "mysql", "proxy": "su", "via": "admin", "params": {"svc": "db"}},
            ]
        )
        mysql = host.creds[1]
        assert (mysql.proxy, mysql.via, mysql.params) == ("su", "admin", {"svc": "db"})

    def test_creds_legacy_dict_rejected_with_migration_hint(self):
        with pytest.raises(ValidationError, match="list of cred objects"):
            _cred_spec({"admin": "pw"})

    def test_creds_duplicate_login_rejected(self):
        with pytest.raises(ValidationError, match="duplicate cred entries"):
            _cred_spec([{"login": "a", "password": "x"}, {"login": "a", "password": "y"}])

    def test_creds_via_requires_proxy(self):
        with pytest.raises(ValidationError, match="require 'proxy'"):
            _cred_spec([{"login": "a", "password": "x"}, {"login": "b", "via": "a"}])

    def test_creds_via_must_name_listed_login(self):
        with pytest.raises(ValidationError, match="unknown 'via'"):
            _cred_spec(
                [
                    {"login": "a", "password": "x"},
                    {"login": "b", "proxy": "su", "via": "ghost"},
                ]
            )

    def test_creds_via_cycle_rejected(self):
        with pytest.raises(ValidationError, match="chain"):
            _cred_spec(
                [
                    {"login": "a", "proxy": "su", "via": "b"},
                    {"login": "b", "proxy": "su", "via": "a"},
                ]
            )

    def test_creds_proxy_name_checked_against_registry(self):
        with pytest.raises(ValidationError, match="not a registered login proxy"):
            _cred_spec(
                [
                    {"login": "a", "password": "x"},
                    {"login": "b", "proxy": "nope", "via": "a"},
                ]
            )

    def test_a_user_key_is_refused_with_the_migration_message(self):
        with pytest.raises(ValidationError, match=r"user was removed: order creds instead"):
            _cred_spec([{"login": "a", "password": "x"}], user="a")

    def test_the_user_migration_message_itself_never_echoes_the_value(self):
        with pytest.raises(ValidationError, match=r"user was removed") as e:
            _cred_spec([{"login": "a", "password": "x"}], user="hunter2-looking-value")
        assert "hunter2" not in e.value.errors(include_input=False)[0]["msg"]

    def test_creds_optional_on_unix_host(self):
        assert UnixHostSpec.model_validate(CRED_BASE).creds == []

    def test_creds_protocols_roundtrip_and_default_to_unscoped(self):
        host = _cred_host(
            [
                {"login": "admin", "password": "pw"},
                {"login": "ftpuser", "password": "fp", "protocols": ["ftp"]},
            ]
        )
        creds = host.creds
        assert creds[0].protocols == []
        assert creds[1].protocols == ["ftp"]
        assert isinstance(creds[1].protocols, list)

    def test_via_cred_resolves_directly_on_credspec_objects(self):
        # Proves the HasVia Protocol is satisfied structurally by CredSpec,
        # not just Cred: via_cred is shared logic (spec cred-scope §3.3).
        admin_spec = CredSpec(login="admin", password="pw")
        root_spec = CredSpec(login="root", proxy="su", via="admin", protocols=["telnet"])
        assert via_cred([admin_spec, root_spec], root_spec) is admin_spec

    def test_creds_protocols_must_name_an_authenticating_backend(self):
        with pytest.raises(
            ValidationError, match=r"'nope' is not an authenticating protocol.*ftp, ssh, telnet"
        ):
            _cred_spec([{"login": "a", "password": "x", "protocols": ["nope"]}])

    def test_creds_protocols_refuses_a_riding_transfer(self):
        """scp inherits ssh's identity; scoping to it would be a statement with no effect."""
        with pytest.raises(ValidationError, match=r"'scp' is not an authenticating protocol"):
            _cred_spec([{"login": "a", "password": "x", "protocols": ["scp"]}])

    def test_creds_same_login_different_scope_is_two_creds(self):
        spec = _cred_spec(
            [
                {"login": "admin", "password": "unix"},
                {"login": "admin", "password": "ftp-pw", "protocols": ["ftp"]},
            ]
        )
        assert [c.password for c in spec.creds] == ["unix", "ftp-pw"]

    def test_creds_duplicate_login_and_scope_rejected_by_identity(self):
        with pytest.raises(ValidationError, match=r"duplicate cred entries: \['admin \[ftp\]'\]"):
            _cred_spec(
                [
                    {"login": "admin", "password": "x", "protocols": ["ftp"]},
                    {"login": "admin", "password": "y", "protocols": ["ftp"]},
                ]
            )

    def test_creds_via_may_name_a_same_scope_login(self):
        spec = _cred_spec(
            [
                {"login": "admin", "password": "x", "protocols": ["telnet"]},
                {"login": "root", "proxy": "su", "via": "admin", "protocols": ["telnet"]},
            ],
            term="telnet",
        )
        assert spec.creds[1].via == "admin"

    def test_creds_via_naming_only_a_foreign_scope_login_is_unknown(self):
        with pytest.raises(ValidationError, match=r"cred 'root': unknown 'via' 'admin'"):
            _cred_spec(
                [
                    {"login": "admin", "password": "x", "protocols": ["ftp"]},
                    {"login": "root", "proxy": "su", "via": "admin", "protocols": ["telnet"]},
                ],
                term="telnet",
            )

    def test_every_cred_scoped_away_from_the_term_is_rejected_where_that_term_is_used(self):
        """A host whose term can authenticate as nobody fails at load, not at connect."""
        entry = {
            **CRED_BASE,
            "creds": [{"login": "admin", "password": "x", "protocols": ["telnet"]}],
        }
        UnixHostSpec.model_validate(entry)  # structural: no selection is known yet
        with pytest.raises(
            HostValidationError,
            match=r"no cred can log in over term 'ssh': every cred is scoped elsewhere "
            r"\(admin \[telnet\]\)",
        ):
            create_host_from_dict(dict(entry), element=Element("lab"))  # the resolver picks ssh
        validate_host_dict(dict(entry), element=Element("lab"))  # the telnet witness passes

    def test_a_chain_that_terminates_only_in_a_foreign_scope_is_rejected(self):
        """`root` is unscoped, so ssh and telnet pick it too — and there it has no direct end.

        The chain resolves for ftp alone (`ftpuser` is the only proxy-less
        entry and it is ftp-scoped); over the term it terminates nowhere,
        which used to load fine and raise mid-connection.
        """
        with pytest.raises(ValidationError, match=r"unresolvable via-chain"):
            _cred_spec(
                [
                    {"login": "root", "proxy": "su"},
                    {"login": "ftpuser", "password": "y", "protocols": ["ftp"]},
                ]
            )

    def test_a_scoped_chain_is_resolved_under_every_protocol_it_is_scoped_to(self):
        """Scoped to ftp AND telnet, resolvable for ftp only: the telnet half is the defect."""
        with pytest.raises(ValidationError, match=r"unresolvable via-chain over 'telnet'"):
            _cred_spec(
                [
                    {"login": "ftpadmin", "password": "x", "protocols": ["ftp"]},
                    {"login": "root", "proxy": "su", "protocols": ["ftp", "telnet"]},
                ],
                term="telnet",
            )


class TestInterfaceSpec:
    """interfaces: dict[netdev -> InterfaceSpec], with bare-string shorthand."""

    def _host(self, interfaces: object) -> dict:
        return {
            "ip": "192.0.2.1",
            "creds": [{"login": "u", "password": "p"}],
            "interfaces": interfaces,
        }

    def test_object_form_parses(self):
        spec = UnixHostSpec.model_validate(self._host({"eth1": {"ip": "10.0.0.5"}}))
        assert spec.interfaces["eth1"].ip == "10.0.0.5"

    def test_string_shorthand_coerces(self):
        spec = UnixHostSpec.model_validate(self._host({"eth1": "10.0.0.5"}))
        assert spec.interfaces["eth1"].ip == "10.0.0.5"

    def test_bad_ip_rejected(self):
        with pytest.raises(ValidationError, match="not a valid IP"):
            UnixHostSpec.model_validate(self._host({"eth1": {"ip": "not-an-ip"}}))

    def test_unknown_interface_key_rejected(self):
        with pytest.raises(
            ValidationError, match=r"interfaces\.eth1\.mac\s+Extra inputs are not permitted"
        ):
            UnixHostSpec.model_validate(self._host({"eth1": {"ip": "10.0.0.5", "mac": "x"}}))

    def test_runtime_host_gets_interface_objects(self):
        host = build_host(self._host({"eth1": "10.0.0.5"}), element=Element("x"))
        assert host.interfaces["eth1"] == Interface(ip="10.0.0.5")

    def test_subnet_optional_and_parsed(self):
        spec = UnixHostSpec.model_validate(
            self._host({"eth1": {"ip": "192.168.1.11", "subnet": "192.168.1.0/24"}})
        )
        assert spec.interfaces["eth1"].subnet == "192.168.1.0/24"

    def test_subnet_defaults_to_none(self):
        spec = UnixHostSpec.model_validate(self._host({"eth1": {"ip": "10.0.0.5"}}))
        assert spec.interfaces["eth1"].subnet is None

    def test_malformed_subnet_rejected(self):
        with pytest.raises(ValidationError, match="CIDR"):
            UnixHostSpec.model_validate(
                self._host({"eth1": {"ip": "10.0.0.5", "subnet": "not-a-network"}})
            )

    def test_subnet_with_host_bits_rejected(self):
        # The interface's OWN address is not its subnet: declare the network.
        with pytest.raises(ValidationError, match="CIDR"):
            UnixHostSpec.model_validate(
                self._host({"eth1": {"ip": "192.168.1.11", "subnet": "192.168.1.11/24"}})
            )

    def test_ip_outside_declared_subnet_rejected(self):
        with pytest.raises(ValidationError, match="not inside"):
            UnixHostSpec.model_validate(
                self._host({"eth1": {"ip": "10.0.0.5", "subnet": "192.168.1.0/24"}})
            )

    def test_runtime_interface_carries_subnet(self):
        host = build_host(
            self._host({"eth1": {"ip": "192.168.1.11", "subnet": "192.168.1.0/24"}}),
            element=Element("x"),
        )
        assert host.interfaces["eth1"] == Interface(ip="192.168.1.11", subnet="192.168.1.0/24")


class TestConsoleRules:
    def _entry(self, **extra):
        return {"ip": "10.0.0.9", "creds": [{"login": "u", "password": "p"}], **extra}

    def test_console_term_requires_a_server(self):
        entry = self._entry(valid_terms=["console"], console_options={"port": 4001})
        with pytest.raises(HostValidationError, match=r"console_options\.server"):
            validate_host_dict(entry, element=Element("e"))

    def test_console_term_requires_a_port(self):
        entry = self._entry(valid_terms=["console"], console_options={"server": "test1"})
        with pytest.raises(HostValidationError, match=r"console_options\.port"):
            validate_host_dict(entry, element=Element("e"))

    def test_console_options_are_inert_on_an_ssh_host(self):
        # Review Focus 5: pre-declared console data on a host that runs ssh
        # today must not be validated against anything.
        hc = host_context(
            self._entry(valid_terms=["ssh", "console"], console_options={"server": "nope"}),
            element=Element("e"),
        )
        assert hc.spec.console_options.server == "nope"
        host = hc.spec.to_host(ready=hc.spec.validate_in(hc.context))
        assert host.term == "ssh"
        assert host.console_options.server == "nope"

    def test_console_options_reach_the_runtime(self):
        entry = self._entry(
            valid_terms=["console"],
            console_options={"server": "test1", "port": 4001, "dial": "direct"},
        )
        host = build_host(entry, element=Element("e"))
        assert host.console_options.port == 4001
        assert host.console_options.dial == "direct"

    def test_console_options_pass_through_on_a_console_free_menu(self):
        # No term-menu validation ever touches console_options here: "console"
        # is not even in valid_terms, so the passthrough and inert paths hold
        # regardless of which term the host actually runs.
        hc = host_context(
            self._entry(valid_terms=["ssh"], console_options={"server": "nope", "port": 0}),
            element=Element("e"),
        )
        assert hc.spec.console_options.server == "nope"
        assert hc.spec.console_options.port == 0
        host = hc.spec.to_host(ready=hc.spec.validate_in(hc.context))
        assert host.console_options.server == "nope"
        assert host.console_options.port == 0


class TestConsoleRefusesNc:
    """A console serves one client, and nc's remote listener needs a second one.

    An explicit nc pin, or a menu of nothing but nc, fails when the lab loads.
    An nc *preference* is skipped, so the next kind on the menu is chosen.
    """

    _CONSOLE: ClassVar[dict] = {
        "valid_terms": ["console"],
        "console_options": {"server": "test1", "port": 4001},
    }

    def _entry(self, **extra):
        return {
            "ip": "10.0.0.9",
            "name": "cons1",
            "creds": [{"login": "u", "password": "p"}],
            **extra,
        }

    def test_an_nc_pin_on_a_console_is_a_config_error(self):
        with pytest.raises(
            HostValidationError,
            match=r"cons1.*nc.*second command channel.*single-client console",
        ):
            validate_host_dict(self._entry(transfer="nc", **self._CONSOLE), element=Element("e"))

    def test_a_menu_of_only_nc_on_a_console_is_a_config_error(self):
        with pytest.raises(
            HostValidationError,
            match=r"cons1.*nc.*second command channel.*single-client console",
        ):
            validate_host_dict(
                self._entry(valid_transfers=["nc"], **self._CONSOLE), element=Element("e")
            )

    def test_a_menu_with_nc_and_scp_on_a_console_resolves_scp(self):
        host = build_host(
            self._entry(valid_transfers=["nc", "scp"], **self._CONSOLE), element=Element("e")
        )
        assert host.transfer == "scp"
        assert host.valid_transfers == ["nc", "scp"]

    def test_an_nc_preference_on_a_console_falls_through_to_scp(self):
        host = build_host(
            self._entry(**self._CONSOLE),
            element=Element("e"),
            selections={"transfer": ["nc", "scp"]},
        )
        assert host.transfer == "scp"

    def test_an_nc_preference_on_an_ssh_host_still_resolves_nc(self):
        host = build_host(
            self._entry(), element=Element("e"), selections={"transfer": ["nc", "scp"]}
        )
        assert host.term == "ssh"
        assert host.transfer == "nc"

    def test_an_nc_pin_on_an_ssh_host_is_accepted(self):
        host = build_host(self._entry(transfer="nc"), element=Element("e"))
        assert host.transfer == "nc"

    def test_a_console_chosen_by_preference_refuses_an_nc_pin(self):
        # The term preference is invisible to the structural validator, so the nc
        # pin only meets the console in contextual validation — still a config error.
        hc = host_context(
            self._entry(
                valid_terms=["ssh", "console"],
                console_options={"server": "test1", "port": 4001},
                transfer="nc",
            ),
            element=Element("e"),
            preferences={".*": {"term": ["console"]}},
        )
        with pytest.raises(HostValidationError, match=r"cons1.*second command channel"):
            hc.spec.validate_in(hc.context)

    def test_a_console_chosen_by_preference_with_only_nc_is_a_config_error(self):
        hc = host_context(
            self._entry(
                valid_terms=["ssh", "console"],
                console_options={"server": "test1", "port": 4001},
                valid_transfers=["nc"],
            ),
            element=Element("e"),
            preferences={".*": {"term": ["console"]}},
        )
        with pytest.raises(HostValidationError, match=r"cons1.*second command channel"):
            hc.spec.validate_in(hc.context)
