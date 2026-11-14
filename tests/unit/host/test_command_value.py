"""``command_value`` is the one statement of what a command's ``value`` holds.

The rule (docs/architecture/utilities/results.md, ``command-output-contract``):
the command's output with exactly one trailing newline removed, nothing else
stripped or added. Every route computes ``value`` through this function, and
the shipped table :data:`otto.testing.EXACT_OUTPUT_CASES` is the input it is
judged on.
"""

import pytest

from otto.host.command_frame import command_value
from otto.testing import EXACT_OUTPUT_CASES
from tests._fixtures.line_reader import lf_lines


@pytest.mark.parametrize("case", EXACT_OUTPUT_CASES, ids=lambda c: c.id)
def test_command_value_gives_the_table_value(case):
    assert command_value(case.text) == case.value


def test_only_the_given_line_ending_is_removed():
    assert command_value("a\r\n", line_ending="\r\n") == "a"
    assert command_value("a\n", line_ending="\r\n") == "a\n"
    assert command_value("a\n\n") == "a\n"


def test_lf_lines_keeps_a_lone_carriage_return():
    assert lf_lines("a\r\nb\rc\n\n") == ["a\r\n", "b\rc\n", "\n"]
