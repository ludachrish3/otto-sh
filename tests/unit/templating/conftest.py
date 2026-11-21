"""Shared fixtures for the Jsonnet template tests.

``OTTO_TEST_JSONNET_BINDING`` is a test-only harness opt-in (declared in
``tests/_ambient_env.py``, so the suite's ``OTTO_*`` strip spares it): the CI
runs and the ``templating_go`` nox session set it to the binding they mean to exercise
(``jsonnet`` or ``gojsonnet``). When it is set, the session fixture below
fails the run unless that binding is the one :func:`load_binding` returns, so
every templated run proves which binding executed. A templated test file
outside this directory imports :func:`jsonnet_binding_under_test` to get the
same proof.
"""

from collections.abc import Iterator
from pathlib import Path

import pytest

from otto.templating.binding import load_binding
from tests._ambient_env import ambient

BINDING_VARIABLE = "OTTO_TEST_JSONNET_BINDING"


@pytest.fixture(scope="session", autouse=True)
def jsonnet_binding_under_test() -> None:
    """Fail unless the binding that loads is the one ``OTTO_TEST_JSONNET_BINDING`` names."""
    expected = ambient(BINDING_VARIABLE)
    if expected:
        loaded = load_binding().name
        assert loaded == expected, (
            f"{BINDING_VARIABLE}={expected} but load_binding() returned the {loaded!r} binding"
        )


@pytest.fixture
def otto_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    """A private, empty ``OTTO_HOME`` for a test that builds real projects."""
    from otto.config.user_settings import clear_user_settings_memo

    path = tmp_path / "home"
    path.mkdir()
    monkeypatch.setenv("OTTO_HOME", str(path))
    clear_user_settings_memo()
    yield path
    clear_user_settings_memo()
