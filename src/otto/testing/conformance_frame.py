"""Conformance suite for a :class:`~otto.host.command_frame.CommandFrame`.

A command frame is the dialect half of a shell session: it wraps a command in
markers and parses the command's output and exit status back out of what the
device sent. :func:`assert_command_frame_conforms` feeds a frame one
transcript per case of :data:`~otto.testing.EXACT_OUTPUT_CASES` and checks
that it reports each case's exit status and its exact ``value`` -- or, for a
frame that declares a :class:`~otto.host.limitation.Limitation` on
:attr:`~otto.host.command_frame.CommandFrame.exact_output`, what the frame
holds instead.

Like the other suites in :mod:`otto.testing`, it collects every violation on
one :class:`~otto.suite.expect.ExpectCollector` and raises a single
``AssertionError``.
"""

from collections.abc import Callable
from typing import TYPE_CHECKING

from ..host.command_frame import SessionMarkers
from ..host.limitation import Limitation
from ..suite.expect import ExpectCollector
from .command_output import EXACT_OUTPUT_CASES, OutputCase

if TYPE_CHECKING:
    from ..host.command_frame import CommandFrame

_SESSION_ID = "c0ffee00"
"""The session id the kit's markers are built from; only their shape matters."""


def assert_command_frame_conforms(
    cls: "type[CommandFrame]",
    *,
    transcript: "Callable[[OutputCase, SessionMarkers], str]",
    holds_instead: "Callable[[OutputCase], str] | None" = None,
) -> None:
    r"""Assert *cls* parses every exact-output case back to its value and exit status.

    For each case of :data:`~otto.testing.EXACT_OUTPUT_CASES`, the buffer is
    ``transcript(case, m)``: what the device sends back for ``case.command``
    with the session markers *m*, as the session hands it to the frame (after
    any terminal translation is undone). The frame's ``extract_retcode`` must
    answer ``case.retcode``, and its ``parse_output`` must answer
    ``case.value``.

    Args:
        cls: The frame class to check. It is constructed with no arguments.
        transcript: Builds the buffer the frame parses for one case. Write it
            from a real device's reply, so the kit checks the frame against the
            device rather than against itself.
        holds_instead: What ``parse_output`` answers for one case INSTEAD of
            ``case.value``. Required exactly when ``cls.exact_output`` is a
            :class:`~otto.host.limitation.Limitation`, and refused otherwise.
            At least one case must hold something other than its ``value``:
            a limitation that no case witnesses is refused.

    Raises:
        AssertionError: listing every violation. Each per-case line starts with
            ``repr(case.id)``.

    Example::

        def my_transcript(case, m):
            return f"{m.begin}\n{case.text}{m.end_prefix}{case.retcode}__\n"


        def test_my_frame_conforms():
            assert_command_frame_conforms(MyFrame, transcript=my_transcript)
    """
    c = ExpectCollector()
    limitation = getattr(cls, "exact_output", None)
    declared = isinstance(limitation, Limitation)
    if declared and holds_instead is None:
        c.expect(
            False,
            msg=f"{cls.__name__} declares exact_output={limitation!r}: "
            "pass holds_instead= to say what it holds instead",
        )
        c.raise_if_failures()
    if not declared and holds_instead is not None:
        c.expect(
            False,
            msg=f"{cls.__name__} declares no exact_output Limitation, so holds_instead= "
            "is refused: its value must be exact",
        )
        c.raise_if_failures()
    frame = cls()
    m = SessionMarkers.for_session(session_id=_SESSION_ID)
    witnessed = False
    for case in EXACT_OUTPUT_CASES:
        expected = case.value if holds_instead is None else holds_instead(case)
        witnessed = witnessed or expected != case.value
        buffer = transcript(case, m)
        retcode = frame.extract_retcode(buffer, m=m)
        c.expect(
            retcode == case.retcode,
            msg=f"{case.id!r}: extract_retcode answered {retcode!r}, expected {case.retcode!r}",
        )
        value = frame.parse_output(buffer, cmd=case.command, m=m)
        c.expect(
            value == expected,
            msg=f"{case.id!r}: parse_output answered {value!r}, expected {expected!r}",
        )
    if declared and not witnessed:
        c.expect(
            False,
            msg=f"{cls.__name__} declares an exact_output Limitation, but holds_instead "
            "matches every case's value: no case is a witness of the limitation",
        )
    c.raise_if_failures()
