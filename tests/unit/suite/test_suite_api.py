"""The suite registry and its loader are gone (spec §5.1, §5.6)."""

import importlib

import pytest

import otto.suite


def test_the_suite_api_is_exactly_the_documented_one():
    assert sorted(otto.suite.__all__) == [
        "ExpectCollector",
        "MonitorHandle",
        "NoTestsMatchedError",
        "OttoFixturesPlugin",
        "RunOptions",
        "SuiteRunResult",
        "UnknownSelectionError",
        "prepare_run",
        "run_tests",
    ]


@pytest.mark.parametrize("module", ["otto.suite.suite", "otto.suite.register"])
def test_the_suite_modules_are_deleted(module):
    with pytest.raises(ModuleNotFoundError):
        importlib.import_module(module)


def test_every_registry_refuses_test_file_registrations():
    from otto.registry import Registry

    assert "accepts_test_files" not in Registry.__init__.__code__.co_varnames
