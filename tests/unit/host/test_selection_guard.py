"""The selection rule, checked at both entry points with the same input (#1295).

For a menu capability (``term``, ``transfer``, ``impairer``) a list the user
writes is theirs, and with no preference and no pin its first entry is the
selection; a default pin (a profile's or a host class's) yields to it. Every
case here goes through the two drivers, :func:`_via_lab` (a lab entry merged
with its os profile, then the spec build) and :func:`_via_constructor`
(``UnixHost(...)`` and its siblings), so a profile, host class or capability
added later that breaks the rule fails here. Nothing here opens a connection.
"""

import dataclasses
import itertools
from typing import Any

import pytest

from otto.config.project import Project
from otto.host import RemoteHost
from otto.host.capability import (
    MenuCapability,
    is_default_selection,
    menu_capabilities,
    menu_capability,
)
from otto.host.connections import TERM_BACKENDS
from otto.host.element import Element
from otto.host.login_proxy import Cred
from otto.host.options import ConsoleOptions
from otto.host.os_profile import (
    HOST_CLASSES,
    ProfileContext,
    copy_host,
    register_host_class,
    register_os_profile,
    registered_profile_names,
    resolve_os_profile,
)
from otto.host.transfer.registry import TRANSFER_BACKENDS
from otto.host.unix_host import UnixHost
from otto.link.impairer import IMPAIRERS
from otto.logger.mode import LogMode
from otto.registry import resolved
from tests._fixtures.host_build import build_host
from tests._fixtures.lab_projection import register_fixture_host_extensions
from tests._fixtures.paths import TESTS_ROOT

_BED_PROFILES = ProfileContext.from_projects(
    projects=[Project(root=TESTS_ROOT / "project1"), Project(root=TESTS_ROOT / "project3")]
)
"""The bed projects' ``[os_profiles]`` tables (``zephyr-*``, each pinning ``transfer``)."""

_CONSOLE = {"server": "hub", "port": 4001}
"""The least a ``console`` term needs to validate, on both entry points."""


def _family_of_profile(name: str, profiles: ProfileContext) -> str:
    profile = resolve_os_profile(name=name, data=profiles)
    return resolved(HOST_CLASSES.get(profile.base).spec)._host_family


def _profile_defaults(name: str, profiles: ProfileContext) -> dict[str, Any]:
    return resolve_os_profile(name=name, data=profiles).fields.defaults.thaw_json()


def _class_family(cls: type) -> str:
    for name in HOST_CLASSES.names():
        entry = HOST_CLASSES.get(name)
        if resolved(entry.cls) is cls:
            return resolved(entry.spec)._host_family
    raise LookupError(f"{cls.__name__} is not a registered host class")


def _vocabulary(family: str, cap: MenuCapability) -> list[str]:
    """Every name the capability may take on *family*, from its registry."""
    if cap.pin_field == "term":
        return sorted(n for n, e in TERM_BACKENDS.raw_items() if family in e.metadata.host_families)
    if cap.pin_field == "transfer":
        return sorted(
            n for n, e in TRANSFER_BACKENDS.raw_items() if family in e.metadata.host_families
        )
    if cap.pin_field == "impairer":
        return sorted(
            n for n in IMPAIRERS.names() if family in resolved(IMPAIRERS.get(n).cls).host_families
        )
    raise AssertionError(f"no vocabulary for capability {cap.pin_field!r}")


def _lists(vocabulary: list[str], default: list[str]) -> list[list[str]]:
    """Every single entry, every ordered pair, and the default forwards and reversed."""
    out = [[v] for v in vocabulary]
    out += [list(p) for p in itertools.permutations(vocabulary, 2)]
    for extra in (list(default), list(reversed(default))):
        if extra not in out:
            out.append(extra)
    return out


def _profile_cases() -> list[tuple[str, MenuCapability]]:
    return [
        (name, cap)
        for name in registered_profile_names(data=_BED_PROFILES)
        for cap in menu_capabilities(_family_of_profile(name, _BED_PROFILES))
    ]


def _host_classes() -> list[type]:
    return list(dict.fromkeys(resolved(HOST_CLASSES.get(n).cls) for n in HOST_CLASSES.names()))


def _class_cases() -> list[tuple[type, MenuCapability]]:
    return [(cls, cap) for cls in _host_classes() for cap in menu_capabilities(_class_family(cls))]


def _via_lab(
    os_type: str,
    *,
    profiles: ProfileContext,
    selections: dict | None = None,
    **fields: Any,
) -> RemoteHost:
    """Build through the lab path: the entry merged with its profile, then the spec build."""
    entry: dict[str, Any] = {
        "os_type": os_type,
        "ip": "10.0.0.1",
        "creds": [{"login": "u", "password": "p"}],
        "console_options": dict(_CONSOLE),
    }
    if _family_of_profile(
        os_type, profiles
    ) == "embedded" and "command_frame" not in _profile_defaults(os_type, profiles):
        entry["command_frame"] = "zephyr"
    return build_host(
        entry | fields, element=Element(name="g"), selections=selections, profiles=profiles
    )


def _via_constructor(cls: type, **fields: Any) -> RemoteHost:
    """Build by calling the host class directly, with the same least options as the lab path."""
    base: dict[str, Any] = {
        "ip": "10.0.0.1",
        "element": Element(name="g"),
        "creds": [Cred(login="u", password="p")],
        "console_options": ConsoleOptions(**_CONSOLE),
        "log": LogMode.QUIET,
    }
    if _class_family(cls) == "embedded" and "command_frame" not in fields:
        base["command_frame"] = "zephyr"
    return cls(**(base | fields))


def _assert_first_entry_selected(host: RemoteHost, cap: MenuCapability, lst: list[str]) -> None:
    assert getattr(host, cap.pin_field) == lst[0], (cap.pin_field, lst)
    assert is_default_selection(getattr(host, cap.pin_field)), (cap.pin_field, lst)
    assert getattr(host, cap.menu_field) == lst, (cap.menu_field, lst)


def _check_lab_profile(os_type: str, cap: MenuCapability, profiles: ProfileContext) -> int:
    lists = _lab_lists(os_type, cap, profiles)
    for lst in lists:
        host = _via_lab(os_type, profiles=profiles, **{cap.menu_field: list(lst)})
        _assert_first_entry_selected(host, cap, lst)
    return len(lists)


def _check_constructor(cls: type, cap: MenuCapability) -> int:
    lists = _lists(_vocabulary(_class_family(cls), cap), cap.default_menu)
    for lst in lists:
        host = _via_constructor(cls, **{cap.menu_field: list(lst)})
        _assert_first_entry_selected(host, cap, lst)
    return len(lists)


def _id(case: tuple) -> str:
    head, cap = case
    return f"{head if isinstance(head, str) else head.__name__}-{cap.pin_field}"


# --- a written list selects its first entry, both entry points ----------------


@pytest.mark.parametrize(
    ("os_type", "cap"), _profile_cases(), ids=[_id(c) for c in _profile_cases()]
)
def test_a_written_list_selects_its_first_entry_on_the_lab_path(os_type, cap):
    register_fixture_host_extensions()  # the bed's zephyr-2.7 names the zephyr-inline frame
    assert _check_lab_profile(os_type, cap, _BED_PROFILES) > 0


@pytest.mark.parametrize(("cls", "cap"), _class_cases(), ids=[_id(c) for c in _class_cases()])
def test_a_passed_list_selects_its_first_entry_in_the_constructor(cls, cap):
    assert _check_constructor(cls, cap) > 0


def _no_list_expectation(name: str, cap: MenuCapability, profiles: ProfileContext) -> str:
    """What an entry with no list and no pin selects: the profile's pin, else the list head."""
    defaults = _profile_defaults(name, profiles)
    if cap.pin_field in defaults:
        return defaults[cap.pin_field]
    if cap.menu_field in defaults:
        return defaults[cap.menu_field][0]
    host_cls = resolved(HOST_CLASSES.get(resolve_os_profile(name=name, data=profiles).base).cls)
    return {f.name: f for f in dataclasses.fields(host_cls)}[cap.menu_field].default_factory()[0]


@pytest.mark.parametrize(
    ("os_type", "cap"), _profile_cases(), ids=[_id(c) for c in _profile_cases()]
)
def test_no_written_list_selects_the_profile_s_pin_or_list_head_on_the_lab_path(os_type, cap):
    register_fixture_host_extensions()
    host = _via_lab(os_type, profiles=_BED_PROFILES)
    assert getattr(host, cap.pin_field) == _no_list_expectation(os_type, cap, _BED_PROFILES)


@pytest.mark.parametrize(("cls", "cap"), _class_cases(), ids=[_id(c) for c in _class_cases()])
def test_no_passed_list_selects_the_class_default_in_the_constructor(cls, cap):
    host = _via_constructor(cls)
    default = {f.name: f for f in dataclasses.fields(cls)}[cap.pin_field].default
    assert getattr(host, cap.pin_field) == default
    assert is_default_selection(getattr(host, cap.pin_field))


def test_the_no_list_cases_include_a_pinning_profile_of_each_kind():
    expected = {
        "busybox": "shell",  # the built-in profile's list head
        "zephyr-3.7": "console",  # a bed project profile's transfer pin
    }
    for name, transfer in expected.items():
        assert (
            _no_list_expectation(
                name,
                menu_capability(family=_family_of_profile(name, _BED_PROFILES), name="transfer"),
                _BED_PROFILES,
            )
            == transfer
        )
        register_fixture_host_extensions()
        assert _via_lab(name, profiles=_BED_PROFILES).transfer == transfer
    register_os_profile(
        "t-guard-pins-mid",
        "unix",
        defaults={"transfer": "sftp", "valid_transfers": ["scp", "sftp"]},
    )
    host = _via_lab("t-guard-pins-mid", profiles=ProfileContext.empty())
    assert host.transfer == "sftp"
    assert host.valid_transfers == ["scp", "sftp"]


_PINS_ALL = "t-guard-pins-all"


def _register_pins_all() -> None:
    """Register a unix code profile pinning all three capabilities, each with its list."""
    register_os_profile(
        _PINS_ALL,
        "unix",
        defaults={
            "term": "telnet",
            "valid_terms": ["telnet", "ssh", "console"],
            "transfer": "sftp",
            "valid_transfers": ["sftp", "scp", "ftp", "nc", "shell"],
            "impairer": "netem",
            "valid_impairers": ["netem"],
        },
    )


def _lab_lists(name: str, cap: MenuCapability, profiles: ProfileContext) -> list[list[str]]:
    family = _family_of_profile(name, profiles)
    default = _profile_defaults(name, profiles).get(cap.menu_field, cap.default_menu)
    return _lists(_vocabulary(family, cap), default)


def test_a_registered_profile_that_pins_all_three_yields_to_a_written_list():
    _register_pins_all()  # undone by the root conftest's _isolate_registries
    caps = menu_capabilities("unix")
    assert [c.pin_field for c in caps] == ["term", "transfer", "impairer"]
    for cap in caps:
        assert _check_lab_profile(_PINS_ALL, cap, ProfileContext.empty()) > 0


def test_the_guard_covers_enough_cases_to_mean_something():
    register_fixture_host_extensions()
    _register_pins_all()
    profile_cases = _profile_cases()
    assert len(profile_cases) >= 12
    lab_lists = sum(len(_lab_lists(n, cap, _BED_PROFILES)) for n, cap in profile_cases)
    lab_lists += sum(
        len(_lab_lists(_PINS_ALL, cap, ProfileContext.empty())) for cap in menu_capabilities("unix")
    )
    ctor_lists = sum(
        len(_lists(_vocabulary(_class_family(cls), cap), cap.default_menu))
        for cls, cap in _class_cases()
    )
    assert lab_lists >= 120
    assert ctor_lists >= 50


# --- the invariant: a class default heads its own default list ----------------


@pytest.mark.parametrize(("cls", "cap"), _class_cases(), ids=[_id(c) for c in _class_cases()])
def test_every_marked_class_default_heads_its_own_default_list(cls, cap):
    fields = {f.name: f for f in dataclasses.fields(cls)}
    default = fields[cap.pin_field].default
    assert is_default_selection(default)
    assert default == fields[cap.menu_field].default_factory()[0]


# --- pins, refusals, the console and copies, both entry points ----------------

_ROUTES = ("lab", "constructor")


def _unix(route: str, **fields: Any) -> RemoteHost:
    if route == "lab":
        return _via_lab("unix", profiles=ProfileContext.empty(), **fields)
    return _via_constructor(UnixHost, **fields)


@pytest.mark.parametrize("route", _ROUTES)
def test_a_pin_with_no_list_is_kept_unmarked(route):
    host = _unix(route, transfer="sftp")
    assert host.transfer == "sftp"
    assert not is_default_selection(host.transfer)


@pytest.mark.parametrize("route", _ROUTES)
def test_a_pin_outside_the_list_is_refused(route):
    with pytest.raises(ValueError, match="transfer 'sftp' is not in this host's transfer menu"):
        _unix(route, transfer="sftp", valid_transfers=["scp", "nc"])


@pytest.mark.parametrize("route", _ROUTES)
def test_an_empty_list_is_refused(route):
    with pytest.raises(ValueError, match="valid_transfers must be a non-empty list"):
        _unix(route, valid_transfers=[])


@pytest.mark.parametrize("route", _ROUTES)
def test_a_console_term_skips_nc_at_the_head_of_the_list(route):
    host = _unix(route, valid_terms=["console"], valid_transfers=["nc", "shell"])
    assert host.term == "console"
    assert host.transfer == "shell"
    assert host.valid_transfers == ["nc", "shell"]


@pytest.mark.parametrize("route", _ROUTES)
def test_a_console_term_refuses_an_nc_pin(route):
    with pytest.raises(ValueError, match="cannot run on a console term"):
        _unix(route, valid_terms=["console"], transfer="nc")


@pytest.mark.parametrize("route", _ROUTES)
def test_a_console_term_refuses_a_list_of_only_nc(route):
    with pytest.raises(ValueError, match="leaves a console term no transfer"):
        _unix(route, valid_terms=["console"], valid_transfers=["nc"])


@pytest.mark.parametrize("route", _ROUTES)
def test_an_explicit_pin_equal_to_the_class_default_is_a_pin(route):
    host = _unix(route, transfer="scp", valid_transfers=["nc", "scp"])
    assert host.transfer == "scp"
    assert not is_default_selection(host.transfer)
    with pytest.raises(ValueError, match="transfer 'scp' is not in this host's transfer menu"):
        _unix(route, transfer="scp", valid_transfers=["nc", "sftp"])


@pytest.mark.parametrize("route", _ROUTES)
def test_a_copy_after_a_default_selection_reselects(route):
    host = _unix(route)
    assert copy_host(host, valid_transfers=["nc", "sftp"]).transfer == "nc"
    assert dataclasses.replace(host, valid_transfers=["nc", "sftp"]).transfer == "nc"
    kept = copy_host(host, shell_history=not host.shell_history)
    assert kept.transfer == host.transfer
    assert is_default_selection(kept.transfer)


@pytest.mark.parametrize("route", _ROUTES)
def test_a_copy_after_an_explicit_pin_keeps_it(route):
    host = _unix(route, transfer="scp")
    assert copy_host(host, valid_transfers=["nc", "scp"]).transfer == "scp"
    with pytest.raises(ValueError, match="transfer 'scp' is not in this host's transfer menu"):
        copy_host(host, valid_transfers=["nc"])


def test_a_preference_beats_a_pin_on_the_lab_path():
    host = _via_lab(
        "unix",
        profiles=ProfileContext.empty(),
        selections={"transfer": ["nc"]},
        transfer="scp",
        valid_transfers=["scp", "nc"],
    )
    assert host.transfer == "nc"
    assert not is_default_selection(host.transfer)
    # A preference's win is the user's choice: a copy keeps it as a pin.
    with pytest.raises(ValueError, match="transfer 'nc' is not in this host's transfer menu"):
        copy_host(host, valid_transfers=["scp"])


# --- a host class's own default list, both entry points -----------------------


@dataclasses.dataclass(slots=True, kw_only=True)
class _FtpFirstHost(UnixHost):
    """A custom class that changes its default by redeclaring only the list."""

    valid_transfers: list[str] = dataclasses.field(default_factory=lambda: ["ftp", "scp"])


@pytest.mark.parametrize("route", _ROUTES)
def test_a_registered_class_s_redeclared_list_is_its_default(route):
    register_host_class("t-guard-ftp-first", _FtpFirstHost)  # undone by _isolate_registries
    if route == "lab":
        host = _via_lab("t-guard-ftp-first", profiles=ProfileContext.empty())
    else:
        host = _via_constructor(_FtpFirstHost)
    assert type(host) is _FtpFirstHost
    assert host.valid_transfers == ["ftp", "scp"]
    assert host.transfer == "ftp"
    assert is_default_selection(host.transfer)
