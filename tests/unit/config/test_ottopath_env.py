"""``OTTOPATH`` names the project roots; an exported ``OTTO_SUT_DIRS`` is not read.

Spec ``docs/superpowers/specs/2026-10-17-649-project-vocabulary-design.md``
(Reading OTTOPATH; OTTO_SUT_DIRS). This file is the one place that must spell
the old variable: it pins that the old name does nothing.
"""

import os

import pytest

from otto.config.env import OTTOPATH_ENV_VAR, load_otto_env
from otto.models.settings import OttoEnvSettings

OLD_VARIABLE = "OTTO_SUT_DIRS"


@pytest.fixture(autouse=True)
def _no_roots_in_the_environment(monkeypatch):
    monkeypatch.delenv(OTTOPATH_ENV_VAR, raising=False)
    monkeypatch.delenv(OLD_VARIABLE, raising=False)


def test_the_variable_is_ottopath_and_the_field_reads_it():
    assert OTTOPATH_ENV_VAR == "OTTOPATH"
    assert OttoEnvSettings.model_fields["ottopath"].alias == OTTOPATH_ENV_VAR


def test_ottopath_is_read_and_split_on_the_path_separator(monkeypatch, tmp_path):
    a, b = tmp_path / "a", tmp_path / "b"
    monkeypatch.setenv("OTTOPATH", f"{a}{os.pathsep}{b}")
    assert OttoEnvSettings().ottopath == [a, b]


@pytest.mark.parametrize("value", [None, ""], ids=["unset", "empty"])
def test_unset_or_empty_names_no_roots(monkeypatch, value):
    if value is not None:
        monkeypatch.setenv("OTTOPATH", value)
    assert OttoEnvSettings().ottopath == []


def test_the_raw_string_reaches_the_splitter(monkeypatch, tmp_path):
    """NoDecode: a value that is not JSON is split as a path list, not JSON-decoded."""
    root = tmp_path / "a,b [x]"
    monkeypatch.setenv("OTTOPATH", str(root))
    assert OttoEnvSettings().ottopath == [root]


def test_an_exported_otto_sut_dirs_has_no_effect(monkeypatch, tmp_path):
    monkeypatch.setenv(OLD_VARIABLE, str(tmp_path))
    assert OttoEnvSettings().ottopath == []
    assert load_otto_env().ottopath == []


def test_only_ottopath_counts_when_both_are_exported(monkeypatch, tmp_path):
    (tmp_path / "new").mkdir()
    monkeypatch.setenv(OLD_VARIABLE, str(tmp_path / "old-and-missing"))
    monkeypatch.setenv("OTTOPATH", str(tmp_path / "new"))
    assert load_otto_env().ottopath == [tmp_path / "new"]
