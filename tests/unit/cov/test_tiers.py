"""Runtime tier config parsing from the settings dict."""

from pathlib import Path

import pytest

from otto.config.coverage_settings import CoverageConfigError
from otto.coverage.errors import CoverageInputError
from otto.coverage.tiers import TierConfig, TierToolchain, load_tiers, resolve_get_tier

ROOT = Path("/proj")


def test_default_when_unconfigured() -> None:
    tiers = load_tiers({}, root=ROOT)
    assert [t.name for t in tiers] == ["system"]
    assert tiers[0].kind == "e2e"
    assert tiers[0].color == "green"


def test_load_sorted_by_precedence_with_default_colors() -> None:
    cov = {
        "tiers": {
            "manual": {"kind": "manual", "precedence": 3, "max_age": "180d"},
            "system": {"kind": "e2e", "precedence": 1},
            "unit": {"kind": "unit", "precedence": 2, "products": {"app": ["build"]}},
        }
    }
    tiers = load_tiers(cov, root=ROOT)
    assert [t.name for t in tiers] == ["system", "unit", "manual"]
    assert [t.color for t in tiers] == ["green", "yellow", "orange"]
    assert tiers[2].max_age_days == 180


def test_unit_tier_products_table_is_carried_through():
    tiers = load_tiers(
        {
            "tiers": {
                "unit": {
                    "kind": "unit",
                    "precedence": 2,
                    "products": {"app": ["build/app-tests"], "agent": ["b/agent"]},
                }
            }
        },
        root=ROOT,
    )
    assert tiers[0].products == {
        "app": [Path("build/app-tests")],
        "agent": [Path("b/agent")],
    }


def test_a_unit_tier_toolchain_is_carried_through_as_written():
    toolchain = {"coverage_tool": "~/bin/gcov", "lcov_args": ["--rc", "x=1"]}
    (tier,) = load_tiers(
        {"tiers": {"unit": {"kind": "unit", "precedence": 1, "toolchain": toolchain}}}, root=ROOT
    )
    assert tier.toolchain == TierToolchain(coverage_tool="~/bin/gcov", lcov_args=["--rc", "x=1"])


def test_a_tier_without_a_toolchain_carries_none():
    (tier,) = load_tiers({"tiers": {"unit": {"kind": "unit", "precedence": 1}}}, root=ROOT)
    assert tier.toolchain is None


def test_explicit_color_wins() -> None:
    cov = {"tiers": {"system": {"kind": "e2e", "precedence": 1, "color": "#112233"}}}
    assert load_tiers(cov, root=ROOT)[0].color == "#112233"


def test_resolve_default_is_sole_e2e() -> None:
    tiers = load_tiers({}, root=ROOT)
    assert resolve_get_tier(tiers, None).name == "system"


def test_resolve_ambiguous_e2e_raises() -> None:
    cov = {
        "tiers": {
            "sys_a": {"kind": "e2e", "precedence": 1},
            "sys_b": {"kind": "e2e", "precedence": 2},
        }
    }
    with pytest.raises(CoverageInputError, match=r"sys_a.*sys_b|sys_b.*sys_a") as exc:
        resolve_get_tier(load_tiers(cov, root=ROOT), None)
    assert exc.value.field == "tier"


def test_resolve_unknown_name_raises() -> None:
    with pytest.raises(CoverageInputError, match="nope") as exc:
        resolve_get_tier(load_tiers({}, root=ROOT), "nope")
    assert exc.value.field == "tier"


def test_ambiguous_get_tier_names_the_library_remedy() -> None:
    cov = {
        "tiers": {
            "sys_a": {"kind": "e2e", "precedence": 1},
            "sys_b": {"kind": "e2e", "precedence": 2},
        }
    }
    with pytest.raises(CoverageInputError, match="name one of them"):
        resolve_get_tier(load_tiers(cov, root=ROOT), None)


def test_product_dirs_are_carried_through_verbatim() -> None:
    """``load_tiers`` substitutes nothing — even a former template variable
    survives as a literal path segment, for the reporter to anchor later."""
    cov = {
        "tiers": {
            "unit": {"kind": "unit", "precedence": 1, "products": {"app": ["${sut_dir}/build"]}},
        }
    }
    (unit,) = load_tiers(cov, root=ROOT)
    assert unit.products == {"app": [Path("${sut_dir}/build")]}


# -- one dir, one view: load_tiers refuses what settings load refuses (#778) --------


def _unit(products: dict) -> dict:
    return {"tiers": {"unit": {"kind": "unit", "precedence": 1, "products": products}}}


def test_load_tiers_requires_root() -> None:
    """A library caller never gets a silent working-directory anchor."""
    with pytest.raises(TypeError):
        load_tiers({})  # ty: ignore[missing-argument]


def test_load_tiers_refuses_through_the_spec_naming_the_tier(tmp_path: Path) -> None:
    cov = {"tiers": {"unit": {"kind": "unit", "precedence": 1, "harvest_dirs": ["build"]}}}
    with pytest.raises(CoverageConfigError) as excinfo:
        load_tiers(cov, root=tmp_path)
    message = str(excinfo.value)
    assert message.startswith("[coverage.tiers.unit] ")
    assert "`harvest_dirs` was removed" in message
    assert f"{tmp_path / '.otto' / 'settings.toml'}: " in message
    assert "input_value" not in message


def test_load_tiers_refuses_overlap(tmp_path: Path) -> None:
    with pytest.raises(CoverageConfigError) as excinfo:
        load_tiers(_unit({"app": ["build"], "tool": ["build/tool-tests"]}), root=tmp_path)
    message = str(excinfo.value)
    assert message.startswith("[coverage.tiers.unit] ")
    for text in ("'app'", "'tool'", "(build)", "(build/tool-tests)"):
        assert text in message
    assert "input_value" not in message


@pytest.mark.parametrize(
    "products",
    [
        pytest.param({"app": ["build/x/../app"], "tool": ["build/app"]}, id="dotdot"),
        pytest.param({"app": ["build", "build/app-tests"]}, id="one-view-nested"),
        pytest.param({"app": ["build"], "tool": ["build"]}, id="two-views-sharing"),
    ],
)
def test_load_tiers_refuses_every_overlap_shape(products: dict, tmp_path: Path) -> None:
    with pytest.raises(CoverageConfigError, match="equals or sits inside another"):
        load_tiers(_unit(products), root=tmp_path)


def test_harvests_unit() -> None:
    def tier(kind: str, products: dict[str, list[Path]]) -> TierConfig:
        return TierConfig(name="t", kind=kind, precedence=1, color="green", products=products)

    assert tier("unit", {"app": [Path("build")]}).harvests_unit is True
    assert tier("unit", {}).harvests_unit is False
    assert tier("e2e", {}).harvests_unit is False
