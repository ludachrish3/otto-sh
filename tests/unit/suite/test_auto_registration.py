"""OttoSuite subclasses named Test* auto-register into SUITES."""

import pytest

from otto.suite import OttoSuite
from otto.suite.register import SUITES

# SUITES registry isolation is provided package-wide by the autouse
# ``_isolate_suites`` fixture in ``tests/unit/suite/conftest.py``.


def test_test_named_subclass_registers() -> None:
    class TestAutoReg(OttoSuite):
        async def test_something(self) -> None: ...

    assert "TestAutoReg" in SUITES
    assert SUITES.get("TestAutoReg").name == "TestAutoReg"


def test_non_test_named_base_does_not_register() -> None:
    class SharedSuiteBase(OttoSuite):
        pass

    assert "SharedSuiteBase" not in SUITES


def test_subclass_of_shared_base_registers() -> None:
    class BaseForReg(OttoSuite):
        pass

    class TestFromBase(BaseForReg):
        pass

    assert "BaseForReg" not in SUITES
    assert "TestFromBase" in SUITES


def test_options_inner_class_is_captured() -> None:
    from otto import options

    @options
    class _Opts:
        retries: int = 3

    class TestWithOpts(OttoSuite[_Opts]):
        Options = _Opts

    entry = SUITES.get("TestWithOpts")
    # the sub-app carries the synthesized --retries flag
    import typer.main

    cmd = typer.main.get_command(entry.sub_app)
    leaf = cmd.commands["TestWithOpts"] if hasattr(cmd, "commands") else cmd
    assert any("--retries" in (p.opts or []) for p in leaf.params)


def test_same_name_from_different_file_still_collides() -> None:
    class TestCollide(OttoSuite):
        pass

    # simulate a re-registration from a DIFFERENT file: entry.file differs
    import dataclasses

    from otto.suite.register import register_suite_class

    entry = SUITES.get("TestCollide")
    SUITES.register(
        "TestCollide",
        dataclasses.replace(entry, file="/somewhere/else/test_other.py"),
        origin="elsewhere",
        overwrite=True,
    )
    with pytest.raises(Exception, match="TestCollide"):
        register_suite_class(TestCollide)
