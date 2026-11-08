"""The counter reset's postcondition parser: ``enoent_for_start`` and ``classify_gcda_check``.

Pure: no shell. The lines are the ones the measured ``find``s print — GNU
findutils 4.9 in the C locale quotes the start path (``'/x/cov'``, with ``'``
rendered ``\\'`` and ``\\`` rendered ``\\\\``) and prefixes its ``argv[0]``;
BusyBox prints the path bare. The real-shell legs of the same rendering pin
are in ``test_product.py`` (GNU) and ``tests/busybox/test_gcda_sweep.py``
(every BusyBox row), so a ``find`` that renders the path some other way
turns RED there and reads as an error, never as "absent", in production.
"""

import pytest

from otto.host.product import (
    GcdaFindError,
    classify_gcda_check,
    enoent_for_start,
    gcda_check_cmd,
)
from otto.result import CommandResult
from otto.utils import Status
from tests.unit.host._gcda_frame import CHECK_MARK, CHECK_PREFIX


def _raw(retcode: int, value: str) -> CommandResult:
    status = Status.Success if retcode == 0 else Status.Failed
    return CommandResult(status=status, value=value, command="find", retcode=retcode)


def _cmd(retcode: int, value: str) -> CommandResult:
    """The check's answer as the script frames it: a newline, the marker, then find."""
    return _raw(retcode, f"\n{CHECK_MARK}\n{value}")


# ── enoent_for_start ─────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("line", "cov_dir"),
    [
        ("/usr/bin/find: '/x/cov': No such file or directory", "/x/cov"),
        ("find: '/x/cov': No such file or directory", "/x/cov"),
        ("find: /x/cov: No such file or directory", "/x/cov"),
        ("find: '/x/my cov': No such file or directory", "/x/my cov"),
        ("find: 'it\\'s gone': No such file or directory", "it's gone"),
        ("find: '/x/a\\\\b': No such file or directory", "/x/a\\b"),
    ],
)
def test_enoent_for_start_matches_every_measured_rendering_of_the_start_path(line, cov_dir):
    assert enoent_for_start(line, cov_dir)


@pytest.mark.parametrize(
    ("line", "cov_dir"),
    [
        # A descendant's ENOENT (a dir vanished mid-walk) is not the start path.
        ("find: '/x/cov/sub': No such file or directory", "/x/cov"),
        ("find: /x/cov/sub: No such file or directory", "/x/cov"),
        # A different path, and a path that merely contains the text.
        ("find: '/y/cov': No such file or directory", "/x/cov"),
        ("find: '/x/covNo such file or directory': No such file or directory", "/x/cov"),
        # Another error for the start path.
        ("find: '/x/cov': Permission denied", "/x/cov"),
        # Not find's own diagnostic.
        ("rm: '/x/cov': No such file or directory", "/x/cov"),
        ("xfind: '/x/cov': No such file or directory", "/x/cov"),
        # The quoting half-applied.
        ("find: '/x/cov: No such file or directory", "/x/cov"),
        ("find: 'it's gone': No such file or directory", "it's gone"),
    ],
)
def test_enoent_for_start_refuses_anything_but_the_start_path_itself(line, cov_dir):
    assert not enoent_for_start(line, cov_dir)


# ── classify_gcda_check ──────────────────────────────────────────────────────


def test_a_clean_check_is_an_empty_list():
    assert classify_gcda_check("/x/cov", _cmd(0, "")) == []


def test_a_check_that_found_counters_lists_them():
    out = "/x/cov/a.gcda\n/x/cov/sub/b.gcda\n"
    assert classify_gcda_check("/x/cov", _cmd(0, out)) == ["/x/cov/a.gcda", "/x/cov/sub/b.gcda"]


def test_a_leftover_whose_name_holds_the_enoent_text_is_still_a_leftover():
    leftover = "/x/cov/No such file or directory.gcda"
    assert classify_gcda_check("/x/cov", _cmd(0, leftover)) == [leftover]


@pytest.mark.parametrize(
    "line",
    [
        "/usr/bin/find: '/x/cov': No such file or directory",
        "find: /x/cov: No such file or directory",
    ],
)
def test_one_start_path_enoent_on_failure_is_absent(line):
    assert classify_gcda_check("/x/cov", _cmd(1, line)) == "absent"


def test_an_absent_start_path_with_a_quote_is_absent():
    line = "find: 'it\\'s gone': No such file or directory"
    assert classify_gcda_check("it's gone", _cmd(1, line)) == "absent"


@pytest.mark.parametrize(
    ("retcode", "value"),
    [
        # A descendant's ENOENT.
        (1, "find: '/x/cov/sub': No such file or directory"),
        # A pathname that merely contains the text.
        (1, "find: '/x/covNo such file or directory': No such file or directory"),
        # A silent failure.
        (1, ""),
        # Permission denied, for the start path and for an ancestor.
        (1, "find: '/x/cov': Permission denied"),
        (1, "find: '/x': Permission denied"),
        # The start-path ENOENT plus anything else.
        (1, "find: '/x/cov': No such file or directory\nfind: '/x': Permission denied"),
        (1, "find: '/x/cov': No such file or directory\nfind: '/x/cov': No such file or directory"),
        # A failed check that prints exactly the sentinel's text.
        (1, "absent"),
    ],
)
def test_anything_else_on_failure_is_a_tagged_error(retcode, value):
    answer = classify_gcda_check("/x/cov", _cmd(retcode, value))
    assert isinstance(answer, GcdaFindError)
    assert answer != "absent"


def test_a_silent_failure_names_its_exit_status():
    answer = classify_gcda_check("/x/cov", _cmd(2, ""))
    assert isinstance(answer, GcdaFindError)
    assert answer.detail == "exit 2, no output"


def test_a_failure_carries_finds_output():
    answer = classify_gcda_check("/x/cov", _cmd(1, "find: '/x': Permission denied\n"))
    assert isinstance(answer, GcdaFindError)
    assert answer.detail == "find: '/x': Permission denied"


def test_a_zero_exit_that_otto_marked_failed_is_an_error_not_a_listing():
    cmd = CommandResult(status=Status.Error, value=f"\n{CHECK_MARK}\n", command="find", retcode=0)
    assert isinstance(classify_gcda_check("/x/cov", cmd), GcdaFindError)


def test_the_check_command_prints_the_marker_before_find():
    assert gcda_check_cmd("/x/my cov") == (
        f"{CHECK_PREFIX}LC_ALL=C find '/x/my cov' -name '*.gcda' -type f -print 2>&1"
    )


# ── the marker frame: what the session printed before the check is not its answer ──


_NOISE = "sudo: unable to resolve host h1: Name or service not known\notto-sudo-AAAAAAAAAAAAAAAA:"


@pytest.mark.parametrize(
    ("retcode", "body", "expected"),
    [
        (0, "", []),
        (0, "/x/cov/a.gcda\n", ["/x/cov/a.gcda"]),
        (1, "find: '/x/cov': No such file or directory\n", "absent"),
    ],
)
def test_noise_before_the_marker_is_discarded(retcode, body, expected):
    answer = classify_gcda_check("/x/cov", _raw(retcode, f"{_NOISE}\n{CHECK_MARK}\n{body}"))
    assert answer == expected


def test_the_marker_is_found_as_an_occurrence_not_a_whole_line():
    # A prompt with no trailing newline glued to the marker's line.
    value = f"otto-sudo-AAAAAAAAAAAAAAAA:{CHECK_MARK}\n/x/cov/a.gcda\n"
    assert classify_gcda_check("/x/cov", _raw(0, value)) == ["/x/cov/a.gcda"]


def test_only_the_first_marker_frames_the_answer():
    # A leftover whose path holds the marker text is still a leftover.
    leftover = f"/x/cov/{CHECK_MARK}.gcda"
    assert classify_gcda_check("/x/cov", _cmd(0, leftover)) == [leftover]


@pytest.mark.parametrize("retcode", [0, 1])
def test_no_marker_is_an_error_carrying_the_whole_output(retcode):
    value = "sudo: a password is required"
    answer = classify_gcda_check("/x/cov", _raw(retcode, value))
    assert answer == GcdaFindError(value)


@pytest.mark.parametrize(
    "line",
    [
        "sudo: unable to resolve host h1: Name or service not known",  # noise after the marker
        "/x/cov2/a.gcda",  # a sibling sharing the string prefix
        "/x/cov/a.gcno",  # under cov_dir, but not a counter
        "/y/cov/a.gcda",  # elsewhere
        "half of a name",  # a filename split by a newline
    ],
)
def test_after_the_marker_a_zero_exit_lists_only_counters_under_cov_dir(line):
    answer = classify_gcda_check("/x/cov", _cmd(0, f"/x/cov/a.gcda\n{line}\n"))
    assert isinstance(answer, GcdaFindError)
