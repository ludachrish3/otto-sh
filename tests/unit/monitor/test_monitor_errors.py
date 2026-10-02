"""The monitor library's refusals and the host-pattern helper."""

import re

import pytest

from otto.errors import FieldError, OttoError
from otto.monitor.errors import (
    MonitorInputError,
    MonitorTlsError,
    NoMonitorableHostsError,
    ReviewSourceError,
)
from otto.utils import compile_host_pattern


def test_input_error_is_a_field_error_naming_its_field():
    exc = MonitorInputError("bad interval", field="interval")
    assert isinstance(exc, FieldError)
    assert exc.field == "interval"
    assert str(exc) == "bad interval"


def test_review_source_error_always_names_source():
    exc = ReviewSourceError("not an export")
    assert isinstance(exc, FieldError)
    assert exc.field == "source"


def test_tls_error_carries_setting_and_repos():
    exc = MonitorTlsError("bad", setting="tls_key", repos=["a", "b"])
    assert isinstance(exc, OttoError)
    assert not isinstance(exc, FieldError)
    assert (exc.setting, exc.repos) == ("tls_key", ["a", "b"])


def test_empty_walk_says_the_lab_is_empty_and_never_mentions_a_pattern():
    msg = str(NoMonitorableHostsError([]))
    assert msg == "No hosts available in the active lab."
    assert "pattern" not in msg
    assert "regex" not in msg


def test_walk_names_hosts_both_routes_and_absolves_the_selection():
    exc = NoMonitorableHostsError(["zeta", "alpha"])
    msg = str(exc)
    assert exc.walked == ["alpha", "zeta"]
    assert msg.startswith("2 host(s) selected, but none of them can be monitored: alpha, zeta.")
    assert "over a shell (Unix hosts)" in msg
    assert "over SNMP" in msg
    assert "The selection is not the problem" in msg
    assert "--" not in msg  # no CLI flag spelling in a library message


def test_more_than_five_hosts_are_summarized():
    msg = str(NoMonitorableHostsError([f"h{i}" for i in range(7)]))
    assert "h0, h1, h2, h3, h4 (+2 more)" in msg


def test_compile_host_pattern_compiles():
    assert compile_host_pattern("web.*").fullmatch("web1")


def test_compile_host_pattern_turns_re_error_into_value_error_naming_the_pattern():
    with pytest.raises(ValueError, match=r"host pattern '\(' is not a valid regex") as excinfo:
        compile_host_pattern("(")
    assert isinstance(excinfo.value.__cause__, re.error)
