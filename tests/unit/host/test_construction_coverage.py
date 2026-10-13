"""register_host_class refuses a class/spec pair whose fields do not cover each other."""

import dataclasses
from typing import Annotated

import pytest
from pydantic import Field

from otto.host import (
    Consume,
    Pass,
    ProfileFields,
    RuntimeOnly,
    UnixHostSpec,
    register_host_class,
    register_os_profile,
)
from otto.host.login_proxy import Cred
from otto.host.os_profile import HOST_CLASSES
from otto.host.unix_host import UnixHost
from otto.models.base import CredSpec
from otto.registry import FrozenMap, Ref
from otto.testing import assert_host_registrable


@dataclasses.dataclass(slots=True, kw_only=True)
class WidgetHost(UnixHost):
    widget: str = "sprocket"


class WidgetSpec(UnixHostSpec):
    widget: str = "sprocket"


class OrphanSpec(UnixHostSpec):
    gizmo: str = "g"


@dataclasses.dataclass(slots=True, kw_only=True)
class BadSupplierHost(UnixHost):
    extra: str = dataclasses.field(
        default="x",
        metadata={"otto": RuntimeOnly(reason="r", supplied_by="lab")},  # ty: ignore[invalid-argument-type]
    )


@dataclasses.dataclass(slots=True, kw_only=True)
class ReasonlessHost(UnixHost):
    extra: str = dataclasses.field(default="x", metadata={"otto": RuntimeOnly(reason="")})


@dataclasses.dataclass(slots=True, kw_only=True)
class ContradictoryHost(UnixHost):
    widget: str = dataclasses.field(default="s", metadata={"otto": RuntimeOnly(reason="r")})


@dataclasses.dataclass(slots=True, kw_only=True)
class NotRuntimeOnlyHost(UnixHost):
    extra: str = dataclasses.field(default="x", metadata={"otto": "runtime only"})


class ConsumedWidgetSpec(UnixHostSpec):
    widget: Annotated[str, Consume("read before construction")] = "s"


class TwoKindWidgetSpec(UnixHostSpec):
    widget: Annotated[str, Pass(), Consume("read before construction")] = "s"


@dataclasses.dataclass(slots=True, kw_only=True)
class SelfCredHost(UnixHost):
    creds: list[Cred] = dataclasses.field(init=False)

    def __post_init__(self) -> None:
        self.creds = [Cred(login="self", password="s")]
        UnixHost.__post_init__(self)


class SelfCredSpec(UnixHostSpec):
    creds: Annotated[list[CredSpec], Consume("the class sets its own creds")] = Field(
        default_factory=list
    )


def test_a_class_field_the_spec_lacks_is_refused_at_registration():
    with pytest.raises(
        ValueError,
        match=r"WidgetHost\.widget is neither a UnixHostSpec field nor marked RuntimeOnly",
    ):
        register_host_class("t-widget", WidgetHost)


def test_the_paired_spec_registers():
    register_host_class("t-widget", WidgetHost, spec=WidgetSpec)


def test_a_spec_field_the_class_lacks_is_refused():
    with pytest.raises(ValueError, match=r"spec field 'gizmo' has no UnixHost constructor field"):
        register_host_class("t-orphan", UnixHost, spec=OrphanSpec)


def test_an_unknown_supplier_is_refused():
    with pytest.raises(
        ValueError,
        match=r"BadSupplierHost\.extra: RuntimeOnly supplied_by must be None or 'element'",
    ):
        register_host_class("t-supplier", BadSupplierHost)


def test_a_runtime_only_without_a_reason_is_refused():
    with pytest.raises(
        ValueError, match=r"ReasonlessHost\.extra: RuntimeOnly needs a non-empty reason"
    ):
        register_host_class("t-reasonless", ReasonlessHost)


def test_class_metadata_that_is_not_a_runtime_only_is_refused():
    with pytest.raises(
        ValueError, match=r"NotRuntimeOnlyHost\.extra: its 'otto' metadata must be a RuntimeOnly"
    ):
        register_host_class("t-notruntimeonly", NotRuntimeOnlyHost)


def test_a_spec_field_with_two_kinds_is_refused_at_registration():
    with pytest.raises(
        ValueError, match=r"TwoKindWidgetSpec\.widget declares two field kinds"
    ) as exc:
        register_host_class("t-twokind", WidgetHost, spec=TwoKindWidgetSpec)
    assert "is neither" not in str(exc.value)  # the spec declares it: one gap, said once


@dataclasses.dataclass(slots=True, kw_only=True)
class NotRuntimeOnlyWidgetHost(UnixHost):
    widget: str = dataclasses.field(default="s", metadata={"otto": "runtime only"})


def test_metadata_that_is_not_a_runtime_only_on_a_spec_field_is_reported_once():
    with pytest.raises(
        ValueError,
        match=r"NotRuntimeOnlyWidgetHost\.widget: its 'otto' metadata must be a RuntimeOnly",
    ) as exc:
        register_host_class("t-notro-widget", NotRuntimeOnlyWidgetHost, spec=WidgetSpec)
    assert "marked RuntimeOnly" not in str(exc.value)


@pytest.mark.parametrize("spec", [WidgetSpec, ConsumedWidgetSpec], ids=["pass", "consume"])
def test_a_runtime_only_field_the_spec_declares_is_refused_whatever_its_kind(spec):
    with pytest.raises(
        ValueError,
        match=rf"ContradictoryHost\.widget is marked RuntimeOnly but {spec.__name__} declares it",
    ):
        register_host_class("t-contra", ContradictoryHost, spec=spec)


def test_an_init_false_override_names_the_consume_fix():
    """Name the Consume fix for the documented init=False override, then accept the paired spec."""
    with pytest.raises(
        ValueError,
        match=r"spec field 'creds' has no SelfCredHost constructor field.*Consume\(reason\)",
    ):
        register_host_class("t-selfcred", SelfCredHost)
    register_host_class("t-selfcred", SelfCredHost, spec=SelfCredSpec)


@pytest.mark.parametrize(
    ("cls_name", "message"),
    [
        ("WidgetHost", r"WidgetHost\.widget is neither"),
        ("ReasonlessHost", r"RuntimeOnly needs a non-empty reason"),
        ("ContradictoryHost", r"ContradictoryHost\.widget is marked RuntimeOnly"),
    ],
)
def test_a_pair_given_by_reference_is_refused_at_its_first_get(cls_name, message):
    spec = "WidgetSpec" if cls_name == "ContradictoryHost" else None
    register_host_class(
        "t-ref",
        Ref(f"{__name__}:{cls_name}"),
        spec=Ref(f"{__name__}:{spec}") if spec else Ref("otto.host.specs:UnixHostSpec"),
    )
    with pytest.raises(ValueError, match=message):
        HOST_CLASSES.get("t-ref")


def test_assert_host_registrable_reports_the_coverage_gap():
    with pytest.raises(AssertionError, match=r"WidgetHost\.widget is neither"):
        assert_host_registrable(WidgetHost)
    assert_host_registrable(WidgetHost, spec=WidgetSpec)


def test_a_profile_default_naming_products_is_refused():
    with pytest.raises(ValueError, match=r"\['products'\]"):
        register_os_profile("t-products", "unix", {"products": []})


def test_a_profile_default_naming_inventory_is_refused():
    with pytest.raises(ValueError, match=r"\['inventory'\]"):
        register_os_profile("t-inventory", "unix", {"inventory": "k"})


def test_a_host_class_s_own_profile_uses_the_same_vocabulary():
    with pytest.raises(ValueError, match=r"\['products'\]"):
        register_host_class(
            "t-ownprofile",
            WidgetHost,
            spec=WidgetSpec,
            profile=ProfileFields(FrozenMap.freeze_json({"products": []})),
        )
