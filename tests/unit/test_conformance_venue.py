"""The host-contract conformance venue switch and the declarations it needs.

Not to be confused with :mod:`otto.testing.conformance`, which asserts that
pluggable *backend interfaces* conform. This module covers the *host
contract* conformance suite under ``tests/conformance/``.
"""

import pytest

from tests._ambient_env import AMBIENT_OPT_INS
from tests.conformance._venue import BED, HERMETIC, cell_budget, current_venue


@pytest.mark.parametrize("var", ["OTTO_CONFORMANCE_BED", "OTTO_CONFORMANCE_CELLS"])
def test_conformance_knobs_are_declared_ambient_opt_ins(var: str) -> None:
    """Undeclared, the root conftest STRIPS these and the venue switch goes silent.

    tests/conftest.py drops every ``OTTO_*`` not in AMBIENT_OPT_INS. Issue #192
    was this exact bug one variable over: ``OTTO_CHAOS_DOCKER`` was missing, so
    nightly's ``OTTO_CHAOS_DOCKER=loopback`` job ran against the BED and still
    passed. A venue switch that fails by silently selecting the other venue is
    worse than one that crashes.
    """
    assert var in AMBIENT_OPT_INS, (
        f"{var} is not declared in tests/_ambient_env.py, so tests/conftest.py "
        f"will strip it and the conformance venue will silently be the default"
    )


def test_venue_defaults_to_hermetic(monkeypatch) -> None:
    monkeypatch.delenv("OTTO_CONFORMANCE_BED", raising=False)
    assert current_venue() == HERMETIC


@pytest.mark.parametrize("raw", ["1", "true", "TRUE", "yes"])
def test_venue_switches_to_bed_on_truthy_values(monkeypatch, raw: str) -> None:
    monkeypatch.setenv("OTTO_CONFORMANCE_BED", raw)
    assert current_venue() == BED


@pytest.mark.parametrize("raw", ["0", "false", "no", ""])
def test_venue_stays_hermetic_on_falsy_values(monkeypatch, raw: str) -> None:
    """``OTTO_CONFORMANCE_BED=0`` must not select the bed.

    Pinned because a truthiness check on the raw string would make every
    non-empty value bed.
    """
    monkeypatch.setenv("OTTO_CONFORMANCE_BED", raw)
    assert current_venue() == HERMETIC


def test_cell_budget_defaults_to_eight(monkeypatch) -> None:
    monkeypatch.delenv("OTTO_CONFORMANCE_CELLS", raising=False)
    assert cell_budget() == 8


def test_cell_budget_all_means_no_limit(monkeypatch) -> None:
    monkeypatch.setenv("OTTO_CONFORMANCE_CELLS", "all")
    assert cell_budget() is None


@pytest.mark.parametrize("raw", ["banana", "0", "-3", "3.5"])
def test_a_malformed_budget_raises_rather_than_defaulting(monkeypatch, raw: str) -> None:
    """A typo must not silently become 8.

    The run would otherwise report a sample size it did not use.
    """
    monkeypatch.setenv("OTTO_CONFORMANCE_CELLS", raw)
    with pytest.raises(ValueError, match="positive integer or 'all'"):
        cell_budget()
