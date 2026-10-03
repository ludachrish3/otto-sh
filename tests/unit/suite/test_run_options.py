"""RunOptions applies its implication and contradiction rules at construction."""

import dataclasses
from pathlib import Path

import pytest

from otto.params import OptionsValidationError
from otto.suite.run import RunOptions

P = Path("/tmp/x")

IMPLICATIONS = [
    # (fields given, fields expected to differ from their defaults)
    ({"cov_report_dir": P}, {"cov_report": True, "cov": True}),
    ({"cov_tickets_json": P}, {"cov_report": True, "cov": True}),
    ({"cov_report": True}, {"cov": True}),
    ({"cov_dir": P}, {"cov": True}),
    ({"cov_dir": P, "cov": True}, {"cov": True}),
    ({"monitor_output": P}, {"monitor": True}),
    ({"monitor_hosts": "router.*"}, {"monitor": True}),
    ({"seed": 7}, {"seed": 7, "random_order": True}),
    ({}, {}),
]

CONTRADICTIONS = [
    ({"cov": False, "cov_dir": P}, "cov=False cannot be combined with"),
    ({"cov": False, "cov_report": True}, "cov=False cannot be combined with"),
    ({"cov": False, "cov_report_dir": P}, "cov=False cannot be combined with"),
    ({"cov": False, "cov_tickets_json": P}, "cov=False cannot be combined with"),
    ({"seed": 7, "random_order": False}, "seed cannot be combined with random_order=False"),
    ({"cov_dir": P, "cov_report_dir": P}, "^cov_report_dir cannot be or contain cov_dir"),
    ({"cov_dir": P / "cov", "cov_report_dir": P}, "^cov_report_dir cannot be or contain cov_dir"),
]

# The same directory, spelled so that only a lexical normalisation equates
# them (pathlib alone already folds "./cov" and a trailing slash).
SAME_DIRECTORY_SPELLINGS = [
    (Path("cov"), Path("cov/../cov")),
    (Path("cov"), Path.cwd() / "cov"),
    (Path("~/cov"), Path.home() / "cov"),
]


@pytest.mark.parametrize(("given", "expected"), IMPLICATIONS)
def test_construction_applies_the_implications(given, expected):
    opts = RunOptions(**given)
    defaults = dataclasses.asdict(RunOptions())
    for name, value in dataclasses.asdict(opts).items():
        if name in expected:
            assert value == expected[name], name
        elif name in given:
            assert value == given[name], name
        else:
            assert value == defaults[name], name


@pytest.mark.parametrize(("given", "message"), CONTRADICTIONS)
def test_construction_refuses_the_contradictions(given, message):
    with pytest.raises(OptionsValidationError, match=message):
        RunOptions(**given)


@pytest.mark.parametrize(("cov_dir", "cov_report_dir"), SAME_DIRECTORY_SPELLINGS)
def test_a_report_dir_spelled_differently_is_still_the_cov_dir(cov_dir, cov_report_dir):
    """Under ``overwrite_cov_report_dir`` the report's clear would empty the
    coverage data it is about to read, however the two paths are spelled."""
    with pytest.raises(OptionsValidationError, match="cannot be or contain cov_dir"):
        RunOptions(cov_dir=cov_dir, cov_report_dir=cov_report_dir)


@pytest.mark.parametrize(
    ("cov_dir", "cov_report_dir"),
    [(P, P / "report"), (P / "cov", P / "report"), (P / "cov-data", P / "cov")],
    ids=["report-inside-cov", "siblings", "shared-name-prefix"],
)
def test_a_report_dir_that_cannot_clear_the_cov_dir_is_allowed(cov_dir, cov_report_dir):
    """A report inside the coverage tree, or beside it, destroys no data.

    The shared-name-prefix row is what a string-prefix comparison would get
    wrong: ``cov-data`` starts with ``cov`` but is not inside it.
    """
    opts = RunOptions(cov_dir=cov_dir, cov_report_dir=cov_report_dir)
    assert (opts.cov_dir, opts.cov_report_dir) == (cov_dir, cov_report_dir)


def test_a_path_that_cannot_be_made_absolute_skips_the_rule_instead_of_crashing():
    """An unknown ``~user`` makes ``expanduser`` raise; construction must not
    leak that ``RuntimeError`` (``prepare_run``'s destination checks own it)."""
    odd = Path("~nosuchuser_otto_xyz/cov")
    assert RunOptions(cov_dir=odd, cov_report_dir=odd).cov_report_dir == odd


def test_no_cov_without_a_destination_is_allowed():
    assert RunOptions(cov=False).cov is False


def test_replace_reapplies_the_rules():
    opts = RunOptions(cov_dir=P)
    with pytest.raises(OptionsValidationError, match="cov=False cannot be combined with"):
        dataclasses.replace(opts, cov=False)
    replaced = dataclasses.replace(RunOptions(), cov_report_dir=P)
    assert replaced.cov_report is True
    assert replaced.cov is True


def test_the_class_stays_frozen():
    with pytest.raises(dataclasses.FrozenInstanceError):
        RunOptions().cov = True  # type: ignore[misc]


def test_resolve_coverage_never_turns_a_destination_off(monkeypatch, tmp_path):
    """A destination forces coverage on; resolution may raise, never resolve off."""
    from otto.config.coverage_settings import CoverageConfigError
    from otto.suite import run as run_module

    monkeypatch.setattr("otto.config.coverage_settings.get_cov_config", lambda repos: {})
    with pytest.raises(CoverageConfigError):
        run_module.resolve_coverage(RunOptions(cov_dir=tmp_path), [], command="otto test")


def test_a_monitor_interval_below_the_floor_is_refused_naming_the_field():
    with pytest.raises(
        OptionsValidationError,
        match=r"^monitor_interval: interval must be at least 1\.0s, got 0\.5s",
    ):
        RunOptions(monitor_interval=0.5)


def test_an_invalid_monitor_hosts_regex_is_refused_naming_the_field():
    with pytest.raises(
        OptionsValidationError, match=r"^monitor_hosts: host pattern '\(' is not a valid regex"
    ):
        RunOptions(monitor_hosts="(")


def test_an_empty_monitor_hosts_is_no_filter_not_a_refusal():
    assert RunOptions(monitor_hosts="").monitor
