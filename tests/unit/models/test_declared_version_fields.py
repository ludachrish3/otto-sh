"""The two pydantic version fields mirror their declared lists (dump spec §13.1).

``MonitorExport.format`` and ``ReservationFile.version`` keep pydantic's
native ``Literal``. Their modules load on budgeted CLI surfaces, so they do
not import ``otto.models.formats``. Instead, this file holds each field's
``Literal`` arguments equal to the declared read list, as
``test_export_format_mirror.py`` does for the browser. A version added to or
dropped from either side fails here.

The differential compares each field with a ``Literal`` over the declared
list, built with the same config, on every validation path:
``model_validate`` and ``model_validate_json``, each lax and strict. It
compares the verdict, the value and type returned, the error type and
message, and the JSON Schema. Re-spelling a field as anything that judges
differently fails here. For example, a before-validator feeding an ``int``
field refuses ``True`` and ``1.0`` under strict validation, while ``Literal``
accepts both as ``1``.
"""

import json
import typing
from typing import Literal

import pytest
from pydantic import ValidationError, create_model

from otto.models import formats
from otto.models.monitor import MonitorExport
from otto.models.settings import ReservationFile

FIELDS = [
    pytest.param(
        MonitorExport, "format", "MONITOR_EXPORT_READ_VERSIONS", {"sessions": []}, id="export"
    ),
    pytest.param(
        ReservationFile,
        "version",
        "RESERVATIONS_READ_VERSIONS",
        {"reservations": []},
        id="reservations",
    ),
]
PATHS = [
    pytest.param("python", False, id="python-lax"),
    pytest.param("python", True, id="python-strict"),
    pytest.param("json", False, id="json-lax"),
    pytest.param("json", True, id="json-strict"),
]
INPUTS = [0, 1, 2, 3, "1", True, False, 1.0, 1.5, None, [1], {"v": 1}]

TODAY = {
    "1": ("accepted", "int", 1),
    "True": ("accepted", "int", 1),
    "1.0": ("accepted", "int", 1),
    "'1'": ("refused", "literal_error", "Input should be 1"),
    "2": ("refused", "literal_error", "Input should be 1"),
    "0": ("refused", "literal_error", "Input should be 1"),
}
"""Today's verdicts with read list ``[1]``, the same on all four paths for both fields."""


def _judge(model, field, payload, path, strict):
    """Return the verdict on *payload*: the value accepted with its type, or the field's error."""
    try:
        if path == "json":
            accepted = model.model_validate_json(json.dumps(payload), strict=strict)
        else:
            accepted = model.model_validate(payload, strict=strict)
    except ValidationError as err:
        (first,) = [e for e in err.errors() if e["loc"] == (field,)]
        return ("refused", first["type"], first["msg"])
    value = getattr(accepted, field)
    return ("accepted", type(value).__name__, value)


def _reference(model, field, constant):
    allowed = tuple(getattr(formats, constant))
    return create_model(
        "Reference", __config__=model.model_config, **{field: (Literal[allowed], ...)}
    )


@pytest.mark.parametrize(("model", "field", "constant", "rest"), FIELDS)
def test_the_field_literal_is_the_declared_read_list(model, field, constant, rest):
    annotation = model.model_fields[field].annotation
    assert (typing.get_origin(annotation), typing.get_args(annotation)) == (
        Literal,
        tuple(getattr(formats, constant)),
    )


@pytest.mark.parametrize(("path", "strict"), PATHS)
@pytest.mark.parametrize(("model", "field", "constant", "rest"), FIELDS)
def test_the_field_judges_every_input_as_a_literal_over_the_declared_list(
    model, field, constant, rest, path, strict
):
    reference = _reference(model, field, constant)
    assert {repr(v): _judge(model, field, {field: v, **rest}, path, strict) for v in INPUTS} == {
        repr(v): _judge(reference, field, {field: v}, path, strict) for v in INPUTS
    }


@pytest.mark.parametrize(("model", "field", "constant", "rest"), FIELDS)
def test_the_field_schema_is_a_literal_over_the_declared_list(model, field, constant, rest):
    reference = _reference(model, field, constant)
    assert (
        model.model_json_schema()["properties"][field]
        == reference.model_json_schema()["properties"][field]
    )


@pytest.mark.parametrize(("model", "field", "constant", "rest"), FIELDS)
def test_todays_verdicts_hold_on_every_validation_path(model, field, constant, rest):
    for path, strict in [("python", False), ("python", True), ("json", False), ("json", True)]:
        verdicts = {
            repr(v): _judge(model, field, {field: v, **rest}, path, strict)
            for v in [1, True, 1.0, "1", 2, 0]
        }
        assert verdicts == TODAY, (path, strict)
