"""Compose adapters: one per (repo, use case), attributed to the decorating module."""

import pytest

from otto.docker.adapter import (
    COMPOSE_ADAPTERS,
    AdapterResult,
    ComposeAdapterEntry,
    adapter_for,
    register_compose_adapter,
)
from otto.registry import DuplicateRegistration, RegistrationRefused, registering_repo

from .. import conformance

COVERS = ["otto.docker.adapter:COMPOSE_ADAPTERS"]


def _fn(facts):
    return AdapterResult()


def _other(facts):
    return AdapterResult(env={"x": "1"})


def test_raw_case():
    conformance.assert_raw_registry(
        COMPOSE_ADAPTERS,
        make=lambda i: (
            "case-repo:bench",
            ComposeAdapterEntry(use_case="bench", fn=_fn if i % 2 else _other),
        ),
        require_repo="case-repo",
    )


def test_the_decorator_credits_the_decorating_module():
    with registering_repo("acme"):
        register_compose_adapter("bench")(_fn)
    assert COMPOSE_ADAPTERS.origin("acme:bench") == __name__
    assert adapter_for("acme", "bench") is _fn


def test_a_key_that_disagrees_with_the_record_is_refused():
    with registering_repo("acme"), pytest.raises(ValueError, match="use case"):
        COMPOSE_ADAPTERS.register("acme:other", ComposeAdapterEntry(use_case="bench", fn=_fn))


def test_a_key_naming_another_repo_is_refused():
    with registering_repo("beta"), pytest.raises(ValueError, match="use case"):
        COMPOSE_ADAPTERS.register("acme:bench", ComposeAdapterEntry("bench", _fn))
    assert "acme:bench" not in COMPOSE_ADAPTERS


def test_a_raw_use_case_containing_a_colon_is_refused():
    with registering_repo("acme"), pytest.raises(ValueError, match="use case"):
        COMPOSE_ADAPTERS.register("acme:a:b", ComposeAdapterEntry("a:b", _fn))
    assert "acme:a:b" not in COMPOSE_ADAPTERS


def test_a_second_adapter_needs_overwrite():
    with registering_repo("acme"):
        register_compose_adapter("bench")(_fn)
        with pytest.raises(DuplicateRegistration):
            register_compose_adapter("bench")(_fn)
        register_compose_adapter("bench", overwrite=True)(_fn)


def test_outside_an_init_import_is_refused():
    with pytest.raises(RegistrationRefused):
        register_compose_adapter("bench")(_fn)
