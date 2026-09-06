"""HostSpec identity-field validation: ``board`` must slug non-empty; ids >= 0.

The element's own rules (a name that slugs non-empty, a non-negative ``id``)
live on :class:`~otto.host.element.Element` since spec 2026-09-05 §2.6 — see
``tests/unit/host/test_element.py``; a host entry carries no element field.
"""

import pytest
from pydantic import ValidationError

from otto.models.host import UnixHostSpec


def _spec(**over):
    base = {
        "ip": "10.0.0.1",
        "creds": [{"login": "admin"}],
    }
    base.update(over)
    return UnixHostSpec.model_validate(base)


def test_board_that_slugs_empty_is_rejected():
    with pytest.raises(ValidationError, match="slug"):
        _spec(board="!!!")


def test_valid_multiword_board_accepted():
    assert _spec(board="Line Card").board == "Line Card"  # raw string preserved on the spec


def test_negative_slot_rejected():
    with pytest.raises(ValidationError, match=r"slot\s+Value error, must be >= 0"):
        _spec(board="blade", slot=-2)


def test_empty_inventory_key_rejected():
    with pytest.raises(
        ValidationError, match=r"inventory\s+Value error, 'inventory' must name a key"
    ):
        _spec(inventory="")
