"""The dashboard request bodies are OttoModel (extra='forbid')."""

import pytest
from pydantic import ValidationError

from otto.monitor.server import _EventBody, _EventUpdateBody


class TestEventBodies:
    def test_event_body_defaults(self):
        b = _EventBody(label="deploy")
        assert b.color == "#888888"
        assert b.dash == "dash"

    def test_event_body_requires_label(self):
        with pytest.raises(ValidationError):
            _EventBody()  # label has no default

    def test_event_body_rejects_unknown_field(self):
        with pytest.raises(ValidationError):
            _EventBody(label="deploy", colour="#fff")  # typo'd 'color'

    def test_update_body_all_optional(self):
        b = _EventUpdateBody()
        assert b.label is None
        assert b.color is None
        assert b.dash is None

    def test_update_body_rejects_unknown_field(self):
        with pytest.raises(ValidationError):
            _EventUpdateBody(dashh="dot")
