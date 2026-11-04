"""A path otto prints on its own line is one literal line, through the real binary (#785).

The ``Output directory:`` line is printed at exit, outside any test console
capture, so only a real ``otto`` process shows whether it wraps at 80 columns
or reads ``[red]`` in the path as a style tag.
"""

import pytest

from tests._fixtures.generated_project import generate_project
from tests._fixtures.literal_text import assert_one_verbatim_line
from tests.e2e._otto_subprocess import output_dirs, run_otto

pytestmark = pytest.mark.hostless


def test_the_output_directory_line_is_one_literal_line(tmp_path) -> None:
    xdir = tmp_path / "[red]x[/red]" / ("d" * 60) / ("e" * 60)
    xdir.mkdir(parents=True)
    roots = generate_project(tmp_path, files=0, dirs=1, realistic=True)
    result = run_otto(
        ["-R", "--xdir", str(xdir), "run", "noop"],
        roots=roots,
        extra_env={"OTTO_LAB": "unix"},
    )
    assert result.returncode == 0, result.stdout + result.stderr
    [run_dir] = output_dirs(xdir)
    assert len(str(run_dir)) > 80
    assert_one_verbatim_line(result.stdout, f"Output directory: {run_dir}")
