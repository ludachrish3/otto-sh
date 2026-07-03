"""LCOV exclusion-marker scanning."""

from otto.coverage.exclusions import scan_excluded_lines

SRC = """int main() {
  int a = 1;             // LCOV_EXCL_LINE
  // LCOV_EXCL_START
  debug_dump();
  debug_dump2();
  // LCOV_EXCL_STOP
  if (a) {}              // LCOV_EXCL_BR_LINE
  return 0;
}
"""


def test_line_and_block_markers() -> None:
    excluded = scan_excluded_lines(SRC)
    assert excluded == {2, 3, 4, 5, 6, 7}


def test_custom_marker() -> None:
    src = "a;\nb; // MYPROJ_NO_COV\nc;\n"
    assert scan_excluded_lines(src, ["MYPROJ_NO_COV"]) == {2}


def test_unclosed_start_runs_to_eof() -> None:
    src = "a;\n// LCOV_EXCL_START\nb;\nc;\n"
    assert scan_excluded_lines(src) == {2, 3, 4}
