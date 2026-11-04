"""``tests/_fixtures/env_guard.py``: the one snapshot-and-restore core for env guards."""

import os
import time

import pytest

from tests._fixtures.env_guard import preserved_env_var, preserved_timezone, set_timezone

_VAR = "OTTO_TEST_ENV_GUARD_PROBE"


def test_a_prior_value_is_restored(monkeypatch) -> None:
    monkeypatch.setenv(_VAR, "before")
    with preserved_env_var(_VAR):
        os.environ[_VAR] = "during"
    assert os.environ[_VAR] == "before"


def test_absence_is_restored(monkeypatch) -> None:
    monkeypatch.delenv(_VAR, raising=False)
    with preserved_env_var(_VAR):
        os.environ[_VAR] = "during"
    assert _VAR not in os.environ


def test_absent_during_removes_the_variable_for_the_block(monkeypatch) -> None:
    monkeypatch.setenv(_VAR, "before")
    with preserved_env_var(_VAR, absent_during=True):
        assert _VAR not in os.environ
    assert os.environ[_VAR] == "before"


@pytest.mark.parametrize("prior", [None, "UTC"], ids=["tz-absent", "tz-utc"])
def test_the_timezone_and_tz_are_restored(prior) -> None:
    with preserved_timezone():
        if prior is not None:
            set_timezone(prior)
        else:
            os.environ.pop("TZ", None)
            time.tzset()
        before = (os.environ.get("TZ"), time.tzname)
        with preserved_timezone():
            set_timezone("Pacific/Kiritimati")
            assert time.tzname != before[1]
        assert (os.environ.get("TZ"), time.tzname) == before
