"""Structural validation refuses an unknown loader or power type, and a pin outside its menu.

Each was accepted by ``model_validate`` and only failed when the host was built
(or never, for the doctor and host summaries, which build nothing).
"""

import pytest
from pydantic import ValidationError

from otto.host import EmbeddedHostSpec, UnixHostSpec
from otto.host.binary_loader import LOADER_CLASSES
from otto.host.embedded_filesystem import FILESYSTEM_CLASSES
from otto.host.power import POWER_CONTROLLERS

_EMBEDDED = {"ip": "192.0.2.1", "command_frame": "zephyr"}
_UNIX = {"ip": "10.0.0.1"}


def test_an_unregistered_loader_is_refused():
    with pytest.raises(
        ValidationError, match=r"loader 'nope' is not a registered binary loader\. Known: llext-hex"
    ):
        EmbeddedHostSpec.model_validate({**_EMBEDDED, "loader": "nope"})


def test_a_registered_loader_validates():
    assert (
        EmbeddedHostSpec.model_validate({**_EMBEDDED, "loader": "llext-hex"}).loader == "llext-hex"
    )


@pytest.mark.parametrize("value", ["nope", {"type": "nope", "on_cmd": "x", "off_cmd": "y"}])
def test_an_unregistered_power_type_is_refused(value):
    with pytest.raises(
        ValidationError,
        match=r"power_control 'nope' is not a registered power controller\. Known: .*command",
    ):
        UnixHostSpec.model_validate({**_UNIX, "power_control": value})


@pytest.mark.parametrize("value", [{"on_cmd": "x"}, {"type": 5}])
def test_a_power_table_without_a_string_type_is_refused(value):
    """A table fails at its ``type`` alone, whether it is missing or not a string."""
    with pytest.raises(ValidationError, match=r"power_control\.table\.type") as exc:
        UnixHostSpec.model_validate({**_UNIX, "power_control": value})
    assert exc.value.error_count() == 1


@pytest.mark.parametrize(
    ("spec_cls", "base", "field", "value", "registry"),
    [
        (UnixHostSpec, _UNIX, "power_control", "nope", POWER_CONTROLLERS),
        (UnixHostSpec, _UNIX, "power_control", {"type": "nope"}, POWER_CONTROLLERS),
        (EmbeddedHostSpec, _EMBEDDED, "loader", "nope", LOADER_CLASSES),
        (EmbeddedHostSpec, _EMBEDDED, "filesystem", "nope", FILESYSTEM_CLASSES),
    ],
    ids=["power-name", "power-table", "loader", "filesystem"],
)
def test_an_empty_registry_is_named_as_none(spec_cls, base, field, value, registry):
    for name in registry.names():
        registry.unregister(name)
    with pytest.raises(ValidationError, match=r"'nope' is not a registered .*\. Known: <none>"):
        spec_cls.model_validate({**base, field: value})


def test_a_registered_power_type_is_not_parsed_structurally():
    """The table's body is the controller's to parse, at contextual validation."""
    table = {"type": "command", "on_cmd": 5}
    spec = UnixHostSpec.model_validate({**_UNIX, "power_control": table})
    assert spec.model_dump()["power_control"] == table


@pytest.mark.parametrize(
    ("spec_cls", "base", "extra", "message"),
    [
        (
            UnixHostSpec,
            _UNIX,
            {"term": "telnet", "valid_terms": ["ssh"]},
            r"term 'telnet' is not in this host's term menu \['ssh'\]",
        ),
        (
            UnixHostSpec,
            _UNIX,
            {"transfer": "sftp", "valid_transfers": ["scp"]},
            r"transfer 'sftp' is not in this host's transfer menu \['scp'\]",
        ),
        (
            UnixHostSpec,
            _UNIX,
            {"impairer": "other"},
            r"impairer 'other' is not in this host's impairer menu \['netem'\]",
        ),
        (
            EmbeddedHostSpec,
            _EMBEDDED,
            {"term": "ssh"},
            r"term 'ssh' is not in this host's term menu \['telnet'\]",
        ),
        (
            EmbeddedHostSpec,
            _EMBEDDED,
            {"transfer": "tftp"},
            r"transfer 'tftp' is not in this host's transfer menu \['console'\]",
        ),
    ],
)
def test_a_pin_outside_its_menu_is_refused(spec_cls, base, extra, message):
    with pytest.raises(ValidationError, match=message):
        spec_cls.model_validate({**base, **extra})


def test_a_pin_inside_its_menu_validates():
    spec = EmbeddedHostSpec.model_validate(
        {**_EMBEDDED, "transfer": "tftp", "valid_transfers": ["console", "tftp"]}
    )
    assert spec.transfer == "tftp"
