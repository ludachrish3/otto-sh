"""The one reader for a value a command returned (:mod:`otto.host.value_reader`)."""

from pathlib import PurePosixPath

import pytest

from otto.host.errors import CommandValueError, HostCommandError
from otto.host.value_reader import AbsolutePath, Integer, Word, read_value, read_value_or_none

_WORDS = Word(frozenset({"ss", "proc"}))

_ACCEPTED = [
    (Integer(), "42", 42),
    (Integer(), " 42\n", 42),
    (Integer(1, 65535), "1", 1),
    (Integer(1, 65535), "65535", 65535),
    (_WORDS, "ss\n", "ss"),
    (AbsolutePath(), "/root", PurePosixPath("/root")),
]

_REJECTED = [
    (Integer(), ""),
    (Integer(), None),
    # BusyBox netstat's stderr on a kernel without IPv6, merged ahead of the count
    (Integer(), "netstat: /proc/net/tcp6: No such file or directory\n10744"),
    (Integer(), "4 2"),
    (Integer(), "-1"),
    (Integer(), "+1"),
    (Integer(), "1_0"),
    (Integer(), "0x1F"),
    (Integer(), "12\n13"),
    (Integer(), "\uff11\uff12"),  # full-width digits: str.isdigit() and int() accept them
    (Integer(1, 65535), "0"),
    (Integer(1, 65535), "65536"),
    (_WORDS, "ss proc"),
    (_WORDS, "SS"),
    (_WORDS, ""),
    (AbsolutePath(), "root"),
    (AbsolutePath(), "/a\n/b"),
    (AbsolutePath(), ""),
]


@pytest.mark.parametrize(("kind", "text", "value"), _ACCEPTED)
def test_a_value_of_the_kind_is_read(kind, text, value):
    assert read_value(text, kind, what="probe") == value
    assert read_value_or_none(text, kind) == value


@pytest.mark.parametrize(("kind", "text"), _REJECTED)
def test_text_that_is_not_the_kind_is_refused(kind, text):
    with pytest.raises(CommandValueError) as info:
        read_value(text, kind, what="the probe of x")
    err = info.value
    assert isinstance(err, HostCommandError)
    assert "the probe of x" in str(err)
    assert repr(text) in str(err)
    assert err.what == "the probe of x"
    assert err.text == text


@pytest.mark.parametrize(("kind", "text"), _REJECTED)
def test_the_non_raising_form_answers_none_for_every_refusal(kind, text):
    assert read_value_or_none(text, kind) is None


def test_a_long_answer_is_cut_in_the_message_but_kept_whole_on_the_error():
    text = "x" * 5000
    with pytest.raises(CommandValueError) as info:
        read_value(text, Integer(), what="count")
    assert repr("x" * 200) in str(info.value)
    assert "x" * 201 not in str(info.value)
    assert info.value.text == text


_TOO_MANY_DIGITS = "9" * 5000
"""Longer than the digit count Python converts to an integer by default."""


def test_a_digit_string_too_long_to_convert_is_refused_by_name():
    with pytest.raises(CommandValueError):
        read_value(_TOO_MANY_DIGITS, Integer(), what="count")


def test_a_digit_string_too_long_to_convert_is_none_in_the_non_raising_form():
    assert read_value_or_none(_TOO_MANY_DIGITS, Integer()) is None
