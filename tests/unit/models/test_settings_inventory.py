"""[inventory] on both settings models; cache_ttl grammar (spec §8, §9.5)."""

from datetime import timedelta

import pytest
from pydantic import ValidationError

from otto.models.inventory import parse_cache_ttl
from otto.models.settings import InventoryConfigSpec, SettingsModel, UserSettingsModel


def test_settings_model_accepts_inventory():
    m = SettingsModel.model_validate(
        {"name": "x", "version": "0.1.0", "inventory": {"backend": "json", "path": "i.json"}}
    )
    assert m.inventory is not None
    assert m.inventory.backend == "json"
    assert m.inventory.model_extra == {"path": "i.json"}
    assert SettingsModel.model_validate({"name": "x", "version": "0.1.0"}).inventory is None


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("0", timedelta(0)),
        ("30m", timedelta(minutes=30)),
        ("24h", timedelta(hours=24)),
        ("2d", timedelta(days=2)),
    ],
)
def test_cache_ttl_grammar(text, expected):
    assert parse_cache_ttl(text) == expected
    assert InventoryConfigSpec(backend="json", cache_ttl=text).cache_ttl == text


_GRAMMAR = r"cache_ttl must be '0' or <n>m / <n>h / <n>d"
_RANGE = r"cache_ttl '1000000000d' is out of range: days=1000000000"


@pytest.mark.parametrize(
    ("bad", "message"),
    [
        ("", _GRAMMAR),
        ("24", _GRAMMAR),
        ("1w", _GRAMMAR),
        ("-1h", _GRAMMAR),
        ("1.5h", _GRAMMAR),
        ("h", _GRAMMAR),
        ("01h", _GRAMMAR),
        ("0h", _GRAMMAR),
        ("1 h", _GRAMMAR),
        ("24H", _GRAMMAR),
        # `$` matches before a trailing newline; `\Z` does not.
        ("24h\n", _GRAMMAR),
        # In the grammar, out of timedelta's range: must still be a ValueError,
        # or it sails through every caller's `except ValueError`.
        ("1000000000d", _RANGE),
    ],
)
def test_cache_ttl_rejects(bad, message):
    # Anchored on the guard's own sentence, not the field name: a bare "cache_ttl"
    # match is satisfied by any pydantic dump that happens to echo the field.
    with pytest.raises(ValueError, match=message):
        parse_cache_ttl(bad)
    with pytest.raises(ValidationError, match=message):
        InventoryConfigSpec(backend="json", cache_ttl=bad)


def test_the_largest_in_range_ttl_still_parses():
    """Anti-vacuity for the range guard: the boundary is timedelta's, not an arbitrary cap."""
    assert parse_cache_ttl("999999999d") == timedelta(days=999999999)


def test_cache_ttl_defaults_to_a_day():
    assert InventoryConfigSpec(backend="json").cache_ttl == "24h"
    assert parse_cache_ttl(InventoryConfigSpec(backend="json").cache_ttl) == timedelta(hours=24)


def test_backend_is_required():
    with pytest.raises(ValidationError, match=r"backend\n\s+Field required"):
        InventoryConfigSpec()


def test_user_settings_model_forbids_repo_only_tables():
    assert UserSettingsModel().inventory is None
    model = UserSettingsModel.model_validate({"inventory": {"backend": "json", "path": "i.json"}})
    assert model.inventory is not None
    assert model.inventory.backend == "json"
    with pytest.raises(ValidationError, match=r"reservations\n\s+Extra inputs are not permitted"):
        UserSettingsModel.model_validate({"reservations": {"backend": "none"}})
