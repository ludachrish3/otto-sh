"""HostSpec identity-field validation: element/board must slug non-empty; ids >= 0."""

import pytest
from pydantic import ValidationError

from otto.models.host import UnixHostSpec


def _spec(**over):
    base = {
        "ip": "10.0.0.1",
        "element": "server",
        "creds": [{"login": "admin"}],
    }
    base.update(over)
    return UnixHostSpec.model_validate(base)


def test_valid_multiword_element_accepted():
    spec = _spec(element="Lab X Server")
    assert spec.element == "Lab X Server"  # raw string preserved on the spec


def test_element_that_slugs_empty_is_rejected():
    with pytest.raises(ValidationError, match="slug"):
        _spec(element="___")


def test_board_that_slugs_empty_is_rejected():
    with pytest.raises(ValidationError, match="slug"):
        _spec(element="server", board="!!!")


def test_negative_element_id_rejected():
    with pytest.raises(ValidationError):
        _spec(element_id=-1)


def test_negative_slot_rejected():
    with pytest.raises(ValidationError):
        _spec(board="blade", slot=-2)
