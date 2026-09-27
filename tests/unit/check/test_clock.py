"""The shared probe clock: bash's $EPOCHREALTIME wrapping and elapsed-time parsing."""

import pytest

from otto.check.clock import PROBE_TIMEOUT_S, SOCAT_CONNECT_S, bash_script, parse_elapsed_ms


def test_bash_script_is_one_quoted_bash_word() -> None:
    assert bash_script('s=$EPOCHREALTIME; echo "$s"') == "bash -c 's=$EPOCHREALTIME; echo \"$s\"'"


def test_parses_a_start_end_pair_in_seconds() -> None:
    assert parse_elapsed_ms("junk\n1790339724.826219 1790339724.830219\n") == pytest.approx(
        4.0, abs=1e-3
    )


def test_parses_a_bare_millisecond_figure() -> None:
    assert parse_elapsed_ms("12.5\n") == 12.5


def test_no_clock_line_is_none() -> None:
    assert parse_elapsed_ms(" \n") is None
    assert parse_elapsed_ms("  \n") is None


def test_bounds_stay_below_the_host_timeout() -> None:
    from otto.check import CHECK_HOST_TIMEOUT

    assert PROBE_TIMEOUT_S + SOCAT_CONNECT_S < CHECK_HOST_TIMEOUT
