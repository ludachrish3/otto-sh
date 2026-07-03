"""Runtime tier config parsing from the settings dict."""

from pathlib import Path

import pytest

from otto.coverage.tiers import load_tiers, resolve_get_tier


def test_default_when_unconfigured() -> None:
    tiers = load_tiers({})
    assert [t.name for t in tiers] == ["system"]
    assert tiers[0].kind == "e2e"
    assert tiers[0].color == "green"


def test_load_sorted_by_precedence_with_default_colors() -> None:
    cov = {
        "tiers": {
            "manual": {"kind": "manual", "precedence": 3, "max_age": "180d"},
            "system": {"kind": "e2e", "precedence": 1},
            "unit": {"kind": "unit", "precedence": 2, "harvest_dirs": ["build"]},
        }
    }
    tiers = load_tiers(cov)
    assert [t.name for t in tiers] == ["system", "unit", "manual"]
    assert [t.color for t in tiers] == ["green", "yellow", "orange"]
    assert tiers[2].max_age_days == 180


def test_explicit_color_wins() -> None:
    cov = {"tiers": {"system": {"kind": "e2e", "precedence": 1, "color": "#112233"}}}
    assert load_tiers(cov)[0].color == "#112233"


def test_resolve_default_is_sole_e2e() -> None:
    tiers = load_tiers({})
    assert resolve_get_tier(tiers, None).name == "system"


def test_resolve_ambiguous_e2e_raises() -> None:
    cov = {
        "tiers": {
            "sys_a": {"kind": "e2e", "precedence": 1},
            "sys_b": {"kind": "e2e", "precedence": 2},
        }
    }
    with pytest.raises(ValueError, match=r"sys_a.*sys_b|sys_b.*sys_a"):
        resolve_get_tier(load_tiers(cov), None)


def test_resolve_unknown_name_raises() -> None:
    with pytest.raises(ValueError, match="nope"):
        resolve_get_tier(load_tiers({}), "nope")


def test_harvest_dirs_expand_sut_dir() -> None:
    """``${sut_dir}`` in a harvest dir is expanded on read (runtime reads the
    raw dict, so the substitution mirrors Repo._expand_string)."""
    cov = {
        "tiers": {
            "unit": {"kind": "unit", "precedence": 1, "harvest_dirs": ["${sut_dir}/build"]},
        }
    }
    (unit,) = load_tiers(cov, Path("/home/me/myproduct"))
    assert unit.harvest_dirs == [Path("/home/me/myproduct/build")]


def test_harvest_dirs_pass_through_without_sut_dir() -> None:
    """No sut_dir given → the raw string is used verbatim (legacy behavior)."""
    cov = {
        "tiers": {
            "unit": {"kind": "unit", "precedence": 1, "harvest_dirs": ["${sut_dir}/build"]},
        }
    }
    (unit,) = load_tiers(cov)
    assert unit.harvest_dirs == [Path("${sut_dir}/build")]
