"""One pipeline for the factory, the doctor and identity."""

import pytest
from pydantic import ValidationError

from otto.host import Element, HostValidationError, UnixHostSpec, register_host_class
from otto.host.factory import create_host_from_dict, host_identity, validate_host_dict
from otto.host.os_profile import OsProfile, ProfileContext, ProfileFields
from otto.host.unix_host import UnixHost
from otto.registry import FrozenMap

_UNIX = {"ip": "10.0.0.1", "creds": [{"login": "u", "password": "p"}]}
_CONSOLE_NC = {
    **_UNIX,
    "name": "cons1",
    "valid_terms": ["ssh", "console"],
    "console_options": {"server": "test1", "port": 4001},
    "transfer": "nc",
}


def test_an_unknown_os_type_has_one_text_on_every_route():
    texts = set()
    for call in (
        lambda d: validate_host_dict(d, element=Element("e")),
        lambda d: host_identity(d, Element("e")),
        lambda d: create_host_from_dict(d, element=Element("e")),
    ):
        with pytest.raises(ValueError, match=r"Unknown os_type profile 'windows'") as exc:
            call({**_UNIX, "os_type": "windows"})
        texts.add(str(exc.value))
    assert len(texts) == 1


def test_a_malformed_board_is_a_validation_error_not_an_attribute_error():
    with pytest.raises(ValidationError, match="board"):
        host_identity({**_UNIX, "board": 5}, Element("e"))


@pytest.mark.parametrize("slot", ["03", 3.0, 3])
def test_a_lax_slot_gives_the_built_host_s_id(slot):
    entry = {**_UNIX, "board": "cpu", "slot": slot}
    built = create_host_from_dict(dict(entry), element=Element("e"))
    assert host_identity(dict(entry), Element("e")).id == built.id == "e-cpu-3"


def test_flat_preferences_are_refused_with_the_table_shape():
    """Review Focus 3: a flat {capability: [...]} table used to be silently ignored."""
    with pytest.raises(
        ValueError, match=r"preferences\['term'\] must be a table .* keyed by a host-id selector"
    ):
        create_host_from_dict(dict(_UNIX), {"term": ["telnet"]}, element=Element("e"))


def test_check_in_failure_surfaces_on_all_three_routes():
    class Picky(UnixHostSpec):
        def check_in(self, ctx):
            raise KeyError("needs a widget")

    register_host_class("t-picky", UnixHost, spec=Picky)
    entry = {**_UNIX, "os_type": "t-picky"}
    for call in (
        lambda: validate_host_dict(dict(entry), element=Element("e")),
        lambda: host_identity(dict(entry), Element("e")),
        lambda: create_host_from_dict(dict(entry), element=Element("e")),
    ):
        with pytest.raises(
            HostValidationError, match=r"host 'e': check_in failed: KeyError: 'needs a widget'"
        ) as exc:
            call()
        assert isinstance(exc.value.__cause__, KeyError)


def test_check_in_raising_host_validation_error_is_not_rewrapped():
    class Own(UnixHostSpec):
        def check_in(self, ctx):
            raise HostValidationError(f"host {ctx.host_id!r}: widget missing")

    register_host_class("t-own", UnixHost, spec=Own)
    with pytest.raises(HostValidationError, match=r"widget missing") as exc:
        validate_host_dict({**_UNIX, "os_type": "t-own"}, element=Element("e"))
    assert str(exc.value) == "host 'e': widget missing"


def test_check_in_validation_error_never_echoes_input():
    from pydantic import BaseModel

    class M(BaseModel):
        n: int

    class Leaky(UnixHostSpec):
        def check_in(self, ctx):
            M.model_validate({"n": "s3cret-value"})

    register_host_class("t-leaky", UnixHost, spec=Leaky)
    with pytest.raises(HostValidationError, match=r"host 'e': check_in failed") as exc:
        validate_host_dict({**_UNIX, "os_type": "t-leaky"}, element=Element("e"))
    assert "s3cret-value" not in str(exc.value)


def test_check_in_runs_once_and_only_after_structural_and_stock_contextual_validation_pass():
    seen = []

    class Order(UnixHostSpec):
        def check_in(self, ctx):
            seen.append(ctx.host_id)

    register_host_class("t-order", UnixHost, spec=Order)
    with pytest.raises(ValidationError, match=r"ip\s+Field required"):
        validate_host_dict({"os_type": "t-order"}, element=Element("e"))  # no ip: structural fails
    # A stock contextual failure: the console preference meets the nc pin.
    with pytest.raises(HostValidationError, match=r"cons1.*second command channel"):
        create_host_from_dict(
            {**_CONSOLE_NC, "os_type": "t-order"},
            {".*": {"term": ["console"]}},
            element=Element("e"),
        )
    assert seen == []
    validate_host_dict({**_UNIX, "os_type": "t-order"}, element=Element("e"))
    assert seen == ["e"]


def test_host_context_none_preferences_is_buildable_not_a_probe():
    from otto.host import host_context

    hc = host_context(dict(_UNIX), element=Element("e"))
    assert hc.context.preferences == {}
    assert hc.spec.validate_in(hc.context).selections is not None


def test_a_factory_build_keeps_its_data_profile_prompts():
    alpha = OsProfile("alpha", "unix", ProfileFields(login_prompt="alpha-login: "))
    data = ProfileContext(profiles=FrozenMap({"alpha": alpha}), owners=FrozenMap({"alpha": "r"}))
    host = create_host_from_dict({**_UNIX, "os_type": "alpha"}, element=Element("e"), profiles=data)
    assert host._profile_fields is not None
    assert host._profile_fields.login_prompt == "alpha-login: "
