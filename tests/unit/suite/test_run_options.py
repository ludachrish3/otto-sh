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
