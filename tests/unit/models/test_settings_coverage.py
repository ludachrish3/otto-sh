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
            "embedded": {"builds": {"3.7": {"build_dir": "b"}}},
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
    with pytest.raises(
        ValidationError,
        match=r"(?m)^coverage\.tiers\.system\.color\n\s+Value error, invalid color 'nope'",
    ):
        _settings({"tiers": {"system": {"kind": "e2e", "precedence": 1, "color": "nope"}}})


def test_good_colors_accepted() -> None:
    s = _settings({"tiers": {"system": {"kind": "e2e", "precedence": 1, "color": "#112233"}}})
    assert s.coverage.tiers["system"].color == "#112233"


def test_bad_max_age_rejected() -> None:
    with pytest.raises(
        ValidationError,
        match=r"(?m)^coverage\.tiers\.manual\.max_age\n\s+Value error, max_age '6mo' must be",
    ):
        _settings({"tiers": {"manual": {"kind": "manual", "precedence": 1, "max_age": "6mo"}}})


def test_bad_kind_rejected() -> None:
    with pytest.raises(
        ValidationError,
        match=r"(?m)^coverage\.tiers\.x\.kind\n\s+Input should be 'e2e', 'unit' or 'manual'",
    ):
        _settings({"tiers": {"x": {"kind": "smoke", "precedence": 1}}})


def test_exclusion_rules_parse_per_kind() -> None:
    s = _settings(
        {
            "exclusions": {
                "rules": [
                    {"kind": "marker", "name": "MYPROJ_NO_COV"},
                    {"kind": "preprocessor", "macros": ["DEBUG_LOG"]},
                    {"kind": "path", "patterns": ["vendor/**"]},
                    {"kind": "regex", "pattern": "assert[(]", "stat": "branch"},
                ]
            }
        }
    )
    assert [r.kind for r in s.coverage.exclusions.rules] == [
        "marker",
        "preprocessor",
        "path",
        "regex",
    ]
    assert s.coverage.exclusions.rules[3].stat == "branch"


def test_old_markers_key_is_rejected() -> None:
    """extra='forbid' IS the migration: the removed key fails loud, named."""
    with pytest.raises(ValidationError, match="markers") as excinfo:
        _settings({"exclusions": {"markers": ["MYPROJ_NO_COV"]}})
    assert "markers" in str(excinfo.value)


def test_preprocessor_rule_rejects_both_matchers() -> None:
    with pytest.raises(ValidationError, match="exactly one"):
        _settings(
            {
                "exclusions": {
                    "rules": [{"kind": "preprocessor", "pattern": "#if 0", "macros": ["X"]}]
                }
            }
        )


def test_regex_rule_pattern_must_compile_at_parse() -> None:
    """Spec 2: a bad regex is a settings error, not a coverage-run error."""
    with pytest.raises(ValidationError, match="invalid regex"):
        _settings({"exclusions": {"rules": [{"kind": "regex", "pattern": "unbalanced("}]}})


def test_preprocessor_rule_pattern_must_compile_at_parse() -> None:
    with pytest.raises(ValidationError, match="invalid regex"):
        _settings({"exclusions": {"rules": [{"kind": "preprocessor", "pattern": "unbalanced("}]}})


def test_report_defaults() -> None:
    s = SettingsModel.model_validate(BASE)
    assert s.coverage.report.high == 80.0
    assert s.coverage.report.medium == 70.0


def test_report_parses_values() -> None:
    s = _settings({"report": {"high": 90, "medium": 75}})
    assert s.coverage.report.high == 90.0
    assert s.coverage.report.medium == 75.0


def test_report_rejects_medium_above_high() -> None:
    with pytest.raises(
        ValidationError,
        match=r"(?m)^coverage\.report\n\s+Value error, \[coverage\.report\] medium \(80\.0\)",
    ):
        _settings({"report": {"high": 70, "medium": 80}})


def test_report_rejects_out_of_range() -> None:
    with pytest.raises(
        ValidationError,
        match=r"(?m)^coverage\.report\.high\n\s+Input should be less than or equal to 100",
    ):
        _settings({"report": {"high": 101}})
    with pytest.raises(
        ValidationError,
        match=r"(?m)^coverage\.report\.medium\n\s+Input should be greater than or equal to 0",
    ):
        _settings({"report": {"medium": -1}})


# -- a unit tier's own toolchain ------------------------------------------------


def _unit_toolchain(toolchain: dict, kind: str = "unit") -> SettingsModel:
    return _settings({"tiers": {"unit": {"kind": kind, "precedence": 1, "toolchain": toolchain}}})


def test_a_unit_tier_toolchain_parses_every_field() -> None:
    s = _unit_toolchain(
        {
            "coverage_tool": "gcov-13",
            "lcov": "/opt/lcov2/bin/lcov",
            "lcov_args": ["--rc", "geninfo_unexecuted_blocks=1"],
        }
    )
    toolchain = s.coverage.tiers["unit"].toolchain
    assert toolchain is not None
    assert toolchain.coverage_tool == "gcov-13"
    assert toolchain.lcov == "/opt/lcov2/bin/lcov"
    assert toolchain.lcov_args == ["--rc", "geninfo_unexecuted_blocks=1"]


def test_an_empty_toolchain_table_is_the_same_as_none() -> None:
    toolchain = _unit_toolchain({}).coverage.tiers["unit"].toolchain
    assert toolchain is not None
    assert (toolchain.coverage_tool, toolchain.lcov, toolchain.lcov_args) == (None, None, [])


def test_paths_and_tilde_values_are_accepted_as_written() -> None:
    toolchain = _unit_toolchain({"coverage_tool": "~/bin/gcov", "lcov": "./tools/lcov"})
    assert toolchain.coverage.tiers["unit"].toolchain.coverage_tool == "~/bin/gcov"
    assert toolchain.coverage.tiers["unit"].toolchain.lcov == "./tools/lcov"


def test_an_unknown_toolchain_key_is_refused_with_its_full_key() -> None:
    with pytest.raises(
        ValidationError, match=r"(?m)^coverage\.tiers\.unit\.toolchain\.gcov\n\s+Extra inputs"
    ):
        _unit_toolchain({"gcov": "gcov-13"})


@pytest.mark.parametrize("field", ["coverage_tool", "lcov"])
@pytest.mark.parametrize(
    "value", ["", "llvm-cov gcov", "/opt/GCC 13/bin/gcov", "gcov;rm", "$(x)", "a\tb", "gc`ov`"]
)
def test_an_unusable_tool_value_is_refused_with_its_full_key(field: str, value: str) -> None:
    with pytest.raises(
        ValidationError,
        match=rf"(?m)^coverage\.tiers\.unit\.toolchain\.{field}\n\s+Value error, ",
    ):
        _unit_toolchain({field: value})


@pytest.mark.parametrize("value", ["", "--ignore-errors source", "a\tb"])
def test_an_unusable_lcov_argument_is_refused_with_its_full_key(value: str) -> None:
    with pytest.raises(
        ValidationError,
        match=r"(?m)^coverage\.tiers\.unit\.toolchain\.lcov_args\n\s+Value error, ",
    ):
        _unit_toolchain({"lcov_args": ["--rc", value]})


def test_lcov_arguments_may_carry_shell_metacharacters() -> None:
    """otto quotes each lcov argument; only the reader's own path goes through geninfo's shell."""
    args = ["--exclude", "*/tests/*", "--rc", "x='a|b'", "--rc", "y=$HOME"]
    assert _unit_toolchain({"lcov_args": args}).coverage.tiers["unit"].toolchain.lcov_args == args


@pytest.mark.parametrize("kind", ["e2e", "manual"])
@pytest.mark.parametrize("toolchain", [{}, {"lcov": "lcov"}])
def test_a_toolchain_table_on_another_kind_is_refused_even_when_empty(
    kind: str, toolchain: dict
) -> None:
    with pytest.raises(
        ValidationError,
        match=r"(?m)^coverage\.tiers\.unit\n\s+Value error, `toolchain` is only valid on a "
        r"unit-kind tier",
    ):
        _unit_toolchain(toolchain, kind=kind)


@pytest.mark.parametrize("value", ["llvm-cov gcov", "/usr/bin/llvm-cov-18 gcov"])
def test_a_two_word_llvm_cov_reader_is_told_to_name_the_binary(value: str) -> None:
    with pytest.raises(ValidationError, match=r"name the llvm-cov binary alone; otto adds `gcov`"):
        _unit_toolchain({"coverage_tool": value})
