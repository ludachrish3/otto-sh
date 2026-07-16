"""Keys starting with '_' are stripped before HostSpec validation (JSON comments)."""

import pytest
from pydantic import ValidationError

from otto.models.host import UnixHostSpec

MINIMAL = {
    "ip": "192.0.2.1",
    "element": "example-device",
    "os_type": "unix",
    "creds": [{"login": "u", "password": "p"}],
}


def test_underscore_key_is_ignored() -> None:
    spec = UnixHostSpec.model_validate(
        MINIMAL | {"_comment": "see docs/guide/setup/host-database.md"}
    )
    assert spec.element == "example-device"


def test_multiple_underscore_keys_are_ignored() -> None:
    spec = UnixHostSpec.model_validate(MINIMAL | {"_comment": "a", "_todo": "b"})
    assert spec.ip == "192.0.2.1"


def test_typoed_key_still_fails_loud() -> None:
    with pytest.raises(ValidationError, match="ipp"):
        UnixHostSpec.model_validate(MINIMAL | {"ipp": "10.0.0.1"})
