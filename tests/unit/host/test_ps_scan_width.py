"""Every ``ps`` scan reads a whole process line, whatever ``COLUMNS`` says (#785).

procps cuts ``args`` to ``COLUMNS`` when stdout is a pipe, which turned a long
sentinel into an undecodable stub: a daemon vanished from list and reap, a
tunnel echo was dropped, a link tag was missed. A command-string assertion
cannot reproduce that, so each scan runs here against the host's real ``ps``.
"""

import pytest

from tests._fixtures.ps_scans import PS_SCAN_CASES, assert_the_scan_reads_the_argv_whole


@pytest.mark.parametrize("case", list(PS_SCAN_CASES.values()), ids=list(PS_SCAN_CASES))
def test_a_long_argv_is_read_whole_under_a_narrow_columns(case) -> None:
    assert_the_scan_reads_the_argv_whole(case, {"PATH": "/usr/bin:/bin", "COLUMNS": "60"})
