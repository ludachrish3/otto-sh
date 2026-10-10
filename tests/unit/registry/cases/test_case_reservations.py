"""Conformance cases for the reservation backends (a configured backend seam)."""

import functools
from dataclasses import dataclass

import pytest

from otto.registry import configured_backend
from otto.reservations import ReservationEnv
from otto.reservations.check import ReservationBackendError, ReservationConstructionError
from otto.reservations.registry import RESERVATION_BACKENDS, register_reservation_backend
from tests.unit.registry import conformance

COVERS = ["otto.reservations.registry:RESERVATION_BACKENDS"]


class _Backend:
    """The least a built reservation backend must be."""

    def backend_name(self):
        return "case"

    def fetch_reservations(self, username, start=None, end=None):
        return []


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
        return _Backend()

    return configured_backend(config=SpyConfig, factory=factory, metadata=None), spy


def _env(tmp_path):
    return ReservationEnv(tmp_path, "me", "o.toml", None)


def test_reservation_backend_case(tmp_path):
    conformance.assert_backend_registry(
        RESERVATION_BACKENDS,
        env=_env(tmp_path),
        make_spy_entry=_make_spy_entry,
        good_raw={"path": "x"},
        bad_raw={"path": "x", "token": "s3cr3t-value"},
    )


@dataclass(frozen=True)
class _CaseConfig:
    @classmethod
    def model_validate(cls, raw, context=None):
        return cls()

    def model_dump(self, mode="python"):
        return {}


def _case_factory(c):
    return _Backend()


def test_register_reservation_backend_matches_the_raw_path():
    conformance.assert_wrapper_matches_raw(
        RESERVATION_BACKENDS,
        via_wrapper=functools.partial(
            register_reservation_backend, "case-sched", config=_CaseConfig, factory=_case_factory
        ),
        record_for=lambda: configured_backend(
            config=_CaseConfig, factory=_case_factory, metadata=None
        ),
    )


def test_the_construction_error_is_a_value_error_under_the_backend_error():
    assert issubclass(ReservationConstructionError, ReservationBackendError)
    assert issubclass(ReservationConstructionError, ValueError)


def test_a_json_parse_failure_never_echoes_the_rejected_value(tmp_path):
    with pytest.raises(ReservationConstructionError, match="parse failed") as err:
        RESERVATION_BACKENDS.prepare(
            "json", {"path": "x", "token": "hunter2-secret"}, _env(tmp_path), source="o.toml"
        )
    assert "hunter2-secret" not in str(err.value)


@dataclass
class _MutableFunctor:
    """A plain (mutable) dataclass whose instances are the factory."""

    calls: int = 0

    def __call__(self, c):
        self.calls += 1
        return _Backend()


@dataclass(frozen=True)
class _FrozenFunctorHoldingAList:
    seen: list

    def __call__(self, c):
        self.seen.append(c.config)
        return _Backend()


@pytest.mark.parametrize(
    "factory",
    [_MutableFunctor(), _FrozenFunctorHoldingAList([])],
    ids=["mutable_dataclass", "frozen_holding_a_list"],
)
def test_a_callable_object_is_a_factory_the_freeze_walk_never_opens(factory, tmp_path):
    """A factory is behaviour, not record data: the walk stops at any callable.

    Mutation: let the walk recurse into a dataclass or model before it stops at callables.
    """
    register_reservation_backend("case-functor", config=_CaseConfig, factory=factory)
    prepared = RESERVATION_BACKENDS.prepare("case-functor", {}, _env(tmp_path))
    assert isinstance(RESERVATION_BACKENDS.build(prepared), _Backend)
    assert RESERVATION_BACKENDS.peek("case-functor").factory is factory


def test_a_json_path_that_cannot_anchor_never_echoes_it(tmp_path):
    """The built-in model's own validator names the rule, never the value it refused."""
    raw = {"path": "~nosuchuser_xyz/hunter2-secret"}
    with pytest.raises(ReservationConstructionError, match="parse failed: path: ") as err:
        RESERVATION_BACKENDS.prepare("json", raw, _env(tmp_path), source="o.toml")
    assert "hunter2-secret" not in str(err.value)
    assert "nosuchuser_xyz" not in str(err.value)
