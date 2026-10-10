"""Conformance cases for the host-class records and the code OS profiles."""

import functools
import sys

import pytest

from otto.host.os_profile import (
    HOST_CLASSES,
    OS_PROFILES,
    HostClassEntry,
    OsProfile,
    ProfileFields,
    register_host_class,
    register_os_profile,
)
from otto.host.unix_host import UnixHost
from otto.models.host import UnixHostSpec
from otto.registry import FrozenMap, Ref
from tests.unit.registry import conformance

COVERS = [
    "otto.host.os_profile:HOST_CLASSES",
    "otto.host.os_profile:OS_PROFILES",
]

_SCRATCH = "case_host_classes_mod"

_SCRATCH_SOURCE = """\
from otto.host.unix_host import UnixHost


class CaseHost(UnixHost):
    pass


class NotAHost:
    pass
"""


@pytest.fixture
def scratch(tmp_path, monkeypatch) -> str:
    """An importable module NOT yet imported, so the lazy case proves nothing imports it early."""
    (tmp_path / f"{_SCRATCH}.py").write_text(_SCRATCH_SOURCE)
    monkeypatch.syspath_prepend(str(tmp_path))
    monkeypatch.delitem(sys.modules, _SCRATCH, raising=False)
    return _SCRATCH


def _host_class(i: int) -> type:
    """A distinct unix host class per *i*, so each record compares unequal."""
    return type(f"CaseHost{i}", (UnixHost,), {})


def test_host_classes_raw_case(scratch):
    conformance.assert_raw_registry(
        HOST_CLASSES,
        make=lambda i: ("case-host", HostClassEntry(cls=_host_class(i), spec=UnixHostSpec)),
        invalid=("case-host", HostClassEntry(cls=object, spec=UnixHostSpec)),  # type: ignore[arg-type]
        lazy=(
            "case-host",
            HostClassEntry(
                cls=Ref(f"{scratch}:CaseHost"), spec=Ref("otto.models.host:UnixHostSpec")
            ),
        ),
        wrongly_typed_lazy=(
            "case-host",
            HostClassEntry(
                cls=Ref(f"{scratch}:NotAHost"), spec=Ref("otto.models.host:UnixHostSpec")
            ),
        ),
    )


def test_a_host_class_record_refuses_defaults_its_class_does_not_have():
    with pytest.raises(ValueError, match="unknown default field"):
        HOST_CLASSES.register(
            "case-host",
            HostClassEntry(
                cls=_host_class(0),
                spec=UnixHostSpec,
                profile=ProfileFields(FrozenMap({"osTyp": "unix"})),
            ),
        )
    assert "case-host" not in HOST_CLASSES


def test_os_profiles_raw_case():
    conformance.assert_raw_registry(
        OS_PROFILES,
        make=lambda i: (
            "case-profile",
            OsProfile("case-profile", "unix", ProfileFields(login_prompt=f"login{i}: ?$")),
        ),
        invalid=("case-profile", OsProfile("case-profile", "no-such-base")),
    )


def test_register_host_class_matches_the_raw_path():
    cls = _host_class(1)
    conformance.assert_wrapper_matches_raw(
        HOST_CLASSES,
        via_wrapper=functools.partial(register_host_class, "case-host", cls, spec=UnixHostSpec),
        record_for=lambda: HostClassEntry(cls=cls, spec=UnixHostSpec),
    )


def test_register_os_profile_matches_the_raw_path():
    conformance.assert_wrapper_matches_raw(
        OS_PROFILES,
        via_wrapper=functools.partial(
            register_os_profile, "case-profile", "unix", {"has_bash": False}
        ),
        record_for=lambda: OsProfile(
            "case-profile", "unix", ProfileFields(FrozenMap({"has_bash": False}))
        ),
    )


def test_a_code_profile_record_named_for_another_key_is_refused():
    with pytest.raises(ValueError, match="the profile record is named 'b'"):
        OS_PROFILES.register("a", OsProfile("b", "unix"))
    assert "a" not in OS_PROFILES
