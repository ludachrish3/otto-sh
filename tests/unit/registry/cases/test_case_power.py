"""Conformance cases for the power controllers (a configured backend seam)."""

import functools
from dataclasses import dataclass

import pytest

from otto.host.power import (
    POWER_CONTROLLERS,
    PowerConstructionError,
    PowerControlError,
    PowerController,
    PowerEnv,
    register_power_controller,
)
from otto.registry import configured_backend
from otto.result import Result
from otto.utils import Status
from tests.unit.registry import conformance

COVERS = ["otto.host.power:POWER_CONTROLLERS"]


class _Controller(PowerController):
    """The least a built power controller must be."""

    async def on(self, host):
        return Result(Status.Success)

    async def off(self, host):
        return Result(Status.Success)


@dataclass
class _Spy:
    parses: int = 0
    builds: int = 0


def _make_spy_entry():
    spy = _Spy()

    @dataclass
    class SpyConfig:
        @classmethod
        def model_validate(cls, raw, context=None):
            spy.parses += 1
            if "token" in raw:
                raise ValueError(f"bad value {raw['token']}")
            return cls()

        def model_dump(self, mode="python"):
            return {}

    def factory(c):
        spy.builds += 1
        return _Controller()

    return configured_backend(config=SpyConfig, factory=factory, metadata=None), spy


def test_power_controller_case():
    conformance.assert_backend_registry(
        POWER_CONTROLLERS,
        env=PowerEnv("h1"),
        make_spy_entry=_make_spy_entry,
        good_raw={"on_cmd": "x", "off_cmd": "y"},
        # Not the brief's "x"/"y": the harness checks that NO value of bad_raw
        # appears in the error, and one-letter values occur in any message.
        bad_raw={"on_cmd": "power-on-cmd", "off_cmd": "power-off-cmd", "token": "s3cr3t-value"},
    )


@dataclass(frozen=True)
class _CaseConfig:
    @classmethod
    def model_validate(cls, raw, context=None):
        return cls()

    def model_dump(self, mode="python"):
        return {}


def _case_factory(c):
    return _Controller()


def test_register_power_controller_matches_the_raw_path():
    conformance.assert_wrapper_matches_raw(
        POWER_CONTROLLERS,
        via_wrapper=functools.partial(
            register_power_controller, "case-pdu", config=_CaseConfig, factory=_case_factory
        ),
        record_for=lambda: configured_backend(
            config=_CaseConfig, factory=_case_factory, metadata=None
        ),
    )


def test_the_construction_error_is_a_value_error_under_the_power_error():
    assert issubclass(PowerConstructionError, PowerControlError)
    assert issubclass(PowerConstructionError, ValueError)


def test_a_result_that_is_not_a_power_controller_is_refused():
    register_power_controller("not-a-controller", config=_CaseConfig, factory=lambda c: object())
    with pytest.raises(PowerConstructionError, match="result failed"):
        POWER_CONTROLLERS.build(POWER_CONTROLLERS.prepare("not-a-controller", {}, PowerEnv("h")))


def test_a_command_parse_failure_never_echoes_the_rejected_value():
    with pytest.raises(PowerConstructionError, match="parse failed") as err:
        POWER_CONTROLLERS.prepare(
            "command",
            {"on_cmd": "x", "off_cmd": "y", "token": "hunter2-secret"},
            PowerEnv("h1"),
            source="power_control of host 'h1'",
        )
    assert "hunter2-secret" not in str(err.value)
    assert "power_control of host 'h1'" in str(err.value)
