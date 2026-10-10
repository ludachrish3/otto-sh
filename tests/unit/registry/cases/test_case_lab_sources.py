"""Conformance cases for the lab-source backends (a configured backend seam)."""

import functools
from dataclasses import dataclass

import pytest

from otto.host.os_profile import ProfileContext
from otto.labs.errors import LabRepositoryError, LabSourceConstructionError
from otto.labs.registry import LAB_REPOSITORIES, register_lab_repository
from otto.labs.sources import LabSourceEnv
from otto.registry import Configured, configured_backend
from tests.unit.registry import conformance

COVERS = ["otto.labs.registry:LAB_REPOSITORIES"]


class _Source:
    """The least a built source must be: it can load and list labs."""

    def load_lab(self, name, preferences=None, inventory=None):
        raise NotImplementedError

    def list_labs(self):
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
            if "secret" in raw:
                raise ValueError(f"bad value {raw['secret']}")
            return cls()

        def model_dump(self, mode="python"):
            return {}

    def factory(c: "Configured[SpyConfig, LabSourceEnv]") -> _Source:
        spy.builds += 1
        return _Source()

    return configured_backend(config=SpyConfig, factory=factory, metadata=None), spy


def test_lab_sources_backend_case(tmp_path):
    conformance.assert_backend_registry(
        LAB_REPOSITORIES,
        env=LabSourceEnv(tmp_path, "r/x", "test", ProfileContext.empty()),
        make_spy_entry=_make_spy_entry,
        good_raw={},
        bad_raw={"secret": "s3cr3t-value"},
    )


@dataclass(frozen=True)
class _CaseConfig:
    @classmethod
    def model_validate(cls, raw, context=None):
        return cls()

    def model_dump(self, mode="python"):
        return {}


def _case_factory(c: "Configured[_CaseConfig, LabSourceEnv]") -> _Source:
    return _Source()


def test_lab_sources_wrapper_case():
    conformance.assert_wrapper_matches_raw(
        LAB_REPOSITORIES,
        via_wrapper=functools.partial(
            register_lab_repository, "case-source", config=_CaseConfig, factory=_case_factory
        ),
        record_for=lambda: configured_backend(
            config=_CaseConfig, factory=_case_factory, metadata=None
        ),
    )


def test_a_built_object_that_is_not_a_source_fails_the_result_stage(tmp_path):
    """The seam's result check: a source must have callable load_lab and list_labs."""
    register_lab_repository("case-wrong", config=_CaseConfig, factory=lambda c: object())
    prepared = LAB_REPOSITORIES.prepare(
        "case-wrong", {}, LabSourceEnv(tmp_path, "r/x", "test", ProfileContext.empty())
    )
    with pytest.raises(LabSourceConstructionError, match="result failed") as err:
        LAB_REPOSITORIES.build(prepared)
    assert isinstance(err.value, LabRepositoryError)
    assert isinstance(err.value, ValueError)


def test_a_json_parse_failure_never_echoes_the_rejected_value(tmp_path):
    env = LabSourceEnv(tmp_path, "r/x", "test", ProfileContext.empty())
    with pytest.raises(LabSourceConstructionError, match="parse failed") as err:
        LAB_REPOSITORIES.prepare("json", {"paths": ["d"], "token": "hunter2-secret"}, env)
    assert "hunter2-secret" not in str(err.value)


def test_a_json_path_that_cannot_anchor_never_echoes_it(tmp_path):
    """The built-in model's own validator names the rule, never the value it refused."""
    env = LabSourceEnv(tmp_path, "r/x", "test", ProfileContext.empty())
    raw = {"paths": ["~nosuchuser_xyz/hunter2-secret"]}
    with pytest.raises(LabSourceConstructionError, match="parse failed: paths: ") as err:
        LAB_REPOSITORIES.prepare("json", raw, env)
    assert "hunter2-secret" not in str(err.value)
    assert "nosuchuser_xyz" not in str(err.value)
