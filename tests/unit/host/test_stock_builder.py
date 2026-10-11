"""One stock builder walks the spec's fields by kind; a custom field needs no override."""

import dataclasses
from typing import Annotated

import pytest
from pydantic import PositiveInt, ValidationError

from otto.host import (
    Convert,
    Element,
    HostValidationError,
    Omit,
    UnixHostSpec,
    host_context,
    register_host_class,
)
from otto.host.factory import create_host_from_dict
from otto.host.login_proxy import Cred
from otto.host.os_profile import OsProfile, ProfileContext, ProfileFields
from otto.host.unix_host import UnixHost
from otto.registry import FrozenMap
from tests._fixtures.host_build import build_host


@dataclasses.dataclass(slots=True, kw_only=True)
class GadgetHost(UnixHost):
    # Defaults unlike the spec's, so an omitted field visibly keeps the CLASS default.
    widget: str = "cog-default"
    buffer_bytes: int = 512


class GadgetSpec(UnixHostSpec):
    widget: str = "sprocket"
    buffer_bytes: Annotated[PositiveInt, Convert(lambda kib, build: kib * 1024)] = 1


def test_a_custom_plain_field_reaches_the_constructor():
    register_host_class("t-gadget", GadgetHost, spec=GadgetSpec)
    host = build_host({"ip": "10.0.0.1", "os_type": "t-gadget", "widget": "cog", "buffer_bytes": 4})
    assert (host.widget, host.buffer_bytes) == ("cog", 4096)


def test_an_omitted_custom_field_keeps_the_class_default():
    register_host_class("t-gadget", GadgetHost, spec=GadgetSpec)
    host = build_host({"ip": "10.0.0.1", "os_type": "t-gadget"})
    assert (host.widget, host.buffer_bytes) == ("cog-default", 512)


@pytest.mark.parametrize("bad", [0, -1, "big"])
def test_a_bad_converted_input_is_refused_at_validation_not_in_the_conversion(bad):
    register_host_class("t-gadget", GadgetHost, spec=GadgetSpec)
    with pytest.raises(ValidationError, match="buffer_bytes"):
        host_context(
            {"ip": "10.0.0.1", "os_type": "t-gadget", "buffer_bytes": bad}, element=Element("lab")
        )


@dataclasses.dataclass(slots=True, kw_only=True)
class SerialHost(UnixHost):
    serial: str


class OptionalSerialSpec(UnixHostSpec):
    serial: str = "default"


class OmittingSerialSpec(UnixHostSpec):
    serial: Annotated[str, Convert(lambda value, build: Omit)]


def test_an_optional_pass_cannot_supply_a_required_parameter():
    with pytest.raises(ValueError, match=r"SerialHost requires 'serial', which nothing supplies"):
        register_host_class("t-serial", SerialHost, spec=OptionalSerialSpec)


def test_omit_for_a_required_parameter_is_a_validation_error_naming_the_field():
    register_host_class("t-omit", SerialHost, spec=OmittingSerialSpec)
    with pytest.raises(
        HostValidationError,
        match=r"host 'lab': field 'serial' is required by SerialHost but its conversion "
        r"returned Omit",
    ):
        build_host({"ip": "10.0.0.1", "os_type": "t-omit", "serial": "x"})


def test_build_kwargs_consumes_the_record():
    hc = host_context({"ip": "10.0.0.1"}, element=Element("lab"))
    ready = hc.spec.validate_in(hc.context)
    hc.spec.build_kwargs(ready)
    with pytest.raises(HostValidationError, match="record already consumed"):
        hc.spec.to_host(ready=ready)


def test_a_credless_unix_entry_builds_with_the_constructor_s_default():
    assert build_host({"ip": "10.0.0.1"}).creds == []


def test_unixhost_takes_creds_positionally_but_no_longer_requires_them():
    assert UnixHost("10.0.0.1", element=Element("lab")).creds == []
    cred = Cred(login="u", password="p")
    assert UnixHost("10.0.0.1", [cred], element=Element("lab")).creds == [cred]


class _OverrideSpec(UnixHostSpec):
    def to_host(self, *, ready):
        return ready.cls(**self.build_kwargs(ready))


def test_an_override_through_build_kwargs_keeps_its_data_profile_prompts():
    """The factory's own constructing_with scope covers an override that skips the stock to_host."""
    register_host_class("t-override", UnixHost, spec=_OverrideSpec)
    alpha = OsProfile("alpha", "t-override", ProfileFields(login_prompt="alpha-login: "))
    data = ProfileContext(profiles=FrozenMap({"alpha": alpha}), owners=FrozenMap({"alpha": "r"}))
    host = create_host_from_dict(
        {"ip": "10.0.0.1", "os_type": "alpha"}, element=Element("lab"), profiles=data
    )
    assert host._profile_fields is not None
    assert host._profile_fields.login_prompt == "alpha-login: "
