"""Every option that pins a netcat ladder rung is checked against the ladder table.

The table (``PORT_LADDER``, ``LISTENER_LADDER``, ``CONTROL_VALUES`` in
``nc_scripts``) is the one place the rung order is declared. A ``Literal`` option
field must either offer exactly the table's rungs between the control values, or
be named here as a setting that is not a ladder, so a new field cannot be added
unclassified.
"""

from typing import Any, Literal, get_args, get_origin

import pytest

from otto.host.option_specs import NcOptionsSpec, UserlandOptionsSpec
from otto.host.transfer.nc_scripts import CONTROL_VALUES, LISTENER_LADDER, PORT_LADDER

SPECS = {"NcOptionsSpec": NcOptionsSpec, "UserlandOptionsSpec": UserlandOptionsSpec}

LADDER_PINS = {
    ("NcOptionsSpec", "port_strategy"): PORT_LADDER,
    ("NcOptionsSpec", "listener_check"): LISTENER_LADDER,
}

SETTLED_USERLAND = {
    ("UserlandOptionsSpec", "elevation"),
    ("UserlandOptionsSpec", "sudo_form"),
    ("UserlandOptionsSpec", "base64_flag"),
    ("UserlandOptionsSpec", "stat_size"),
    ("UserlandOptionsSpec", "checksum"),
    ("UserlandOptionsSpec", "timeout_style"),
    ("UserlandOptionsSpec", "applet_base64"),
    ("UserlandOptionsSpec", "applet_nc"),
    ("UserlandOptionsSpec", "applet_poweroff"),
    ("UserlandOptionsSpec", "applet_scp"),
    ("UserlandOptionsSpec", "applet_shutdown"),
    ("UserlandOptionsSpec", "applet_uudecode"),
    ("UserlandOptionsSpec", "applet_uuencode"),
}

NOT_A_LADDER: set[tuple[str, str]] = set()


def _literal_of(annotation: Any) -> tuple[Any, ...] | None:
    """Every ``Literal`` alternative *annotation* offers, across a union, in order.

    ``None`` when it offers none. A union of two ``Literal`` aliases yields both
    sets of alternatives, so a rung added beside an alias is still read.
    """
    if get_origin(annotation) is Literal:
        return get_args(annotation)
    found = [_literal_of(arg) for arg in get_args(annotation)]
    alternatives = [value for each in found if each is not None for value in each]
    return tuple(alternatives) if any(each is not None for each in found) else None


def test_the_reader_sees_every_alternative_of_a_union() -> None:
    port = Literal["auto", "ss"]
    assert _literal_of(port | Literal["lsof"] | None) == ("auto", "ss", "lsof")
    assert _literal_of(int | None) is None


def _literal_fields() -> set[tuple[str, str]]:
    return {
        (spec_name, name)
        for spec_name, spec in SPECS.items()
        for name, field in spec.model_fields.items()
        if _literal_of(field.annotation) is not None
    }


def test_every_literal_option_field_is_classified() -> None:
    named = [*LADDER_PINS, *SETTLED_USERLAND, *NOT_A_LADDER]
    assert len(named) == len(set(named)), "a field is in more than one set"
    assert set(named) == _literal_fields()


@pytest.mark.parametrize(("spec_name", "field"), sorted(LADDER_PINS))
def test_a_ladder_pin_offers_the_rungs_and_the_controls(spec_name: str, field: str) -> None:
    annotation = SPECS[spec_name].model_fields[field].annotation
    rungs = tuple(rung.name for rung in LADDER_PINS[(spec_name, field)])
    first, *rest = CONTROL_VALUES
    assert _literal_of(annotation) == (first, *rungs, *rest)
