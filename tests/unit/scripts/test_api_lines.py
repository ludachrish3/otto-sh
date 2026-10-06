"""``scripts/api_lines.py``: the one definition of the public-API golden's line format."""

import pytest

from scripts import api_lines
from tests._fixtures.paths import PROJECT_ROOT

pytestmark = pytest.mark.interpreter_agnostic


@pytest.mark.parametrize(
    ("line", "kind"),
    [
        ("otto:Status", "root"),
        ("otto:", "bare"),
        ("otto.docker:", "bare"),
        ("otto.host.element:Element", "deep"),
        ("otto.host.host:Host", "deep"),
        ("otto.host.host:Host.put(src_files, dest_dir)", "host"),
        ("otto.host.host:Host.close()", "host"),
        ("a line with no colon", "unknown"),
    ],
)
def test_v1_kind(line, kind):
    assert api_lines.v1_kind(line) == kind


def test_schema_of_reads_the_v2_header_anywhere_in_the_header_block():
    assert api_lines.schema_of("# comment\n# api-snapshot v2\nname\totto:Status\tclass\n") == 2
    assert api_lines.schema_of("# comment\notto:Status\n") == 1
    assert api_lines.schema_of("") == 1


def test_data_lines_drops_comments_and_blanks():
    assert api_lines.data_lines("# h\n\notto:A\n# x\notto:B\n") == ["otto:A", "otto:B"]


def test_host_line_round_trips():
    line = api_lines.host_line("put", ["src_files", "dest_dir"])
    assert line == "otto.host.host:Host.put(src_files, dest_dir)"
    assert api_lines.parse_host_line(line) == ("Host.put", ["src_files", "dest_dir"])
    assert api_lines.parse_host_line("otto.host.host:Host.close()") == ("Host.close", [])
    assert api_lines.parse_host_line("otto:Status") is None


def test_every_line_of_the_committed_golden_has_a_known_v1_kind():
    """The live golden is v1 until P1; an ``unknown`` line would slip past every rule."""
    text = (PROJECT_ROOT / "tests/unit/api_snapshot/public_api.txt").read_text(encoding="utf-8")
    assert api_lines.schema_of(text) == 1
    unknown = [ln for ln in api_lines.data_lines(text) if api_lines.v1_kind(ln) == "unknown"]
    assert unknown == []
