"""Quoting host data into host commands: bash scripts, shell operands and socat hosts."""

import shlex
from pathlib import Path

import pytest

from otto.host.shell_quote import bash_script, path_operand, shell_operand, socat_host
from tests._fixtures.argv_stub import SOCAT_REFUSAL, SOCAT_REFUSED


def test_bash_script_is_one_quoted_bash_word() -> None:
    assert bash_script('s=$EPOCHREALTIME; echo "$s"') == "bash -c 's=$EPOCHREALTIME; echo \"$s\"'"


def test_bash_script_passes_each_argument_as_its_own_positional_word() -> None:
    hostile = "1.2.3.4'; touch /tmp/x; '"
    words = shlex.split(bash_script('echo "$1" "$2"', hostile, "23"))
    assert words == ["bash", "-c", 'echo "$1" "$2"', "_", hostile, "23"]


@pytest.mark.parametrize("value", ["-e/bin/sh", "-f", "--"])
def test_an_option_shaped_operand_is_refused(value: str) -> None:
    with pytest.raises(ValueError, match="would be read as an option"):
        shell_operand(value)


def test_an_operand_is_one_shell_word() -> None:
    hostile = "$(touch /tmp/x)"
    assert shlex.split(f"ping {shell_operand(hostile)}") == ["ping", hostile]
    assert shell_operand("10.0.0.5") == "10.0.0.5"


@pytest.mark.parametrize("path", ["/tmp/a b", "/tmp/x'; touch /tmp/pwned; '", "rel/$(id)"])
def test_a_path_is_one_shell_word(path: str) -> None:
    assert shlex.split(f"rm -f {path_operand(Path(path))}") == ["rm", "-f", path]
    assert path_operand(path) == shell_operand(path)


@pytest.mark.parametrize(("path", "word"), [("-rf", "./-rf"), ("--", "./--"), ("-x/y", "./-x/y")])
def test_a_leading_dash_path_is_spelled_from_the_current_directory(path: str, word: str) -> None:
    """The same file, never an option: a path operand is never refused."""
    assert shlex.split(f"rm {path_operand(Path(path))}") == ["rm", word]


@pytest.mark.parametrize("value", SOCAT_REFUSED)
def test_socat_refuses_anything_that_is_not_an_address(value: str) -> None:
    with pytest.raises(ValueError, match=SOCAT_REFUSAL):
        socat_host(value)


@pytest.mark.parametrize(
    ("value", "host"),
    [("10.0.0.5", "10.0.0.5"), ("router1.lab", "router1.lab"), ("fe80::1%eth0", "[fe80::1%eth0]")],
)
def test_socat_takes_an_address_and_brackets_ipv6(value: str, host: str) -> None:
    assert socat_host(value) == host
