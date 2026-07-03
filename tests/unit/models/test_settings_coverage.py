"""Typed [coverage] settings: tiers, colors, exclusions."""

import pytest
from pydantic import ValidationError

from otto.models.settings import SettingsModel

BASE = {"name": "demo", "version": "1.0.0"}


def _settings(coverage: dict) -> SettingsModel:
    return SettingsModel.model_validate({**BASE, "coverage": coverage})


def test_empty_coverage_still_valid() -> None:
    s = SettingsModel.model_validate(BASE)
    assert s.coverage.tiers == {}


def test_legacy_keys_survive_typing() -> None:
    s = _settings(
        {
            "hosts": "cov_.*",
            "gcda_remote_dir": "/tmp/gcda",
            "embedded": {"extension": "cov_ext", "builds": {"3.7": {"build_dir": "b"}}},
        }
    )
    assert s.coverage.hosts == "cov_.*"
    assert s.coverage.embedded["builds"]["3.7"]["build_dir"] == "b"


def test_tiers_parse_with_defaults() -> None:
    s = _settings(
        {
            "tiers": {
                "system": {"kind": "e2e", "precedence": 1},
                "unit": {"kind": "unit", "precedence": 2, "harvest_dirs": ["build"]},
                "manual": {"kind": "manual", "precedence": 3, "max_age": "180d"},
            }
        }
    )
    assert s.coverage.tiers["system"].kind == "e2e"
    assert s.coverage.tiers["unit"].harvest_dirs[0].name == "build"
    assert s.coverage.tiers["manual"].max_age == "180d"


def test_bad_color_rejected() -> None:
    with pytest.raises(ValidationError, match="color"):
        _settings({"tiers": {"system": {"kind": "e2e", "precedence": 1, "color": "nope"}}})


def test_good_colors_accepted() -> None:
    s = _settings({"tiers": {"system": {"kind": "e2e", "precedence": 1, "color": "#112233"}}})
    assert s.coverage.tiers["system"].color == "#112233"


def test_bad_max_age_rejected() -> None:
    with pytest.raises(ValidationError, match="max_age"):
        _settings({"tiers": {"manual": {"kind": "manual", "precedence": 1, "max_age": "6mo"}}})


def test_bad_kind_rejected() -> None:
    with pytest.raises(ValidationError):
        _settings({"tiers": {"x": {"kind": "smoke", "precedence": 1}}})


def test_exclusion_markers() -> None:
    s = _settings({"exclusions": {"markers": ["MYPROJ_NO_COV"]}})
    assert s.coverage.exclusions.markers == ["MYPROJ_NO_COV"]
