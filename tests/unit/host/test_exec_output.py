"""A command's output has one owner, at the chunk level, on every exec route.

:class:`~otto.host.session.ExecOutput` holds everything read, the unfinished
last line included, so a timed-out command's value keeps what the command
printed before the deadline. :func:`~otto.host.session.timed_out_result` is
the one builder of that result, shared by the process routes and the shell.
"""

import asyncio

import pytest

from otto.host.command_frame import ZephyrFrame, command_value
from otto.host.exec_route import ExecRoute
from otto.host.local_host import _decoded_chunks
from otto.host.session import ExecOutput, drain_and_wait, timed_out_result
from otto.logger.mode import LogMode
from otto.utils import Status
from tests.unit.host._fake_channel import FakeChannelProcess, FakeConn, channel_manager
from tests.unit.host._session_feed import onlcr_session
from tests.unit.host.test_session import MockSession

PARTIALS = [
    pytest.param([], "", id="nothing"),
    pytest.param(["  lead\n"], "  lead", id="leading-space-kept"),
    pytest.param(["a\n", "\n", "b\n"], "a\n\nb", id="blank-line-kept"),
    pytest.param(["a\nha", "lf"], "a\nhalf", id="unfinished-line-across-chunks"),
    pytest.param(["tail  \n"], "tail  ", id="trailing-space-kept"),
]


@pytest.mark.parametrize(("chunks", "value"), PARTIALS)
def test_exec_output_owns_the_unfinished_chunk(chunks: list[str], value: str):
    out = ExecOutput()
    lines = [line for c in chunks for line in out.feed(c)]
    whole = "".join(chunks)
    assert out.raw() == whole
    assert out.value() == value == command_value(whole)
    # The unfinished tail is held, not lost.
    assert lines == [x for x in whole.splitlines(keepends=True) if x.endswith("\n")]


@pytest.mark.parametrize(("chunks", "value"), PARTIALS)
def test_timed_out_result_has_one_shape(chunks: list[str], value: str):
    out = ExecOutput()
    for c in chunks:
        out.feed(c)
    r = timed_out_result(cmd="c", timeout=0.75, output=out, route=ExecRoute.SSH_CHANNEL)
    assert r.value == "Command timed out after 0.75s" + (f"\n{value}" if value else "")
    assert (r.timed_out, r.retcode, r.status, r.route) == (True, -1, Status.Error, "ssh-channel")


@pytest.mark.asyncio
async def test_the_local_reader_hands_over_an_unfinished_line_on_timeout():
    # Local exec's reader on a pipe that delivered "a\nhalf" and stays open.
    # The deadline is ordered by events, not a clock: it fires once the drain
    # has taken the chunk in and asked for more. The real-process timeout is
    # the conformance `timeout` contract's.
    reader = asyncio.StreamReader()
    reader.feed_data(b"a\nhalf")
    taken = asyncio.Event()

    async def observed(chunks):
        async for chunk in chunks:
            yield chunk
            taken.set()  # the drain has recorded *chunk* and reads on

    async def never_exits() -> int:
        await asyncio.Event().wait()
        return 0

    out = ExecOutput()
    drain = asyncio.ensure_future(
        drain_and_wait(
            observed(_decoded_chunks(reader)), out, never_exits, lambda *_: None, LogMode.NEVER
        )
    )
    await taken.wait()
    drain.cancel()  # what the exec's wait_for does at its deadline
    with pytest.raises(asyncio.CancelledError):
        await drain
    r = timed_out_result(cmd="c", timeout=0.3, output=out, route=ExecRoute.LOCAL_SUBPROCESS)
    # `half` stayed in the line reader's private pending list before (M11).
    assert r.value == "Command timed out after 0.3s\na\nhalf"


@pytest.mark.asyncio
async def test_the_channel_reader_hands_over_an_unfinished_line_on_timeout():
    proc = FakeChannelProcess(["a\nha", "lf"], returncode=None, exit_status=None, eof=False)
    r = await channel_manager().exec_on(
        conn=FakeConn(proc), cmd="c", timeout=0.05, log=LogMode.NEVER
    )
    assert r.value == "Command timed out after 0.05s\na\nhalf"
    assert proc.signals[0] == "TERM"


@pytest.mark.asyncio
async def test_a_shell_partial_is_folded_like_a_finished_command():
    s = await onlcr_session()
    # Armed BEFORE the write: feed_after_write captures its baseline synchronously.
    feeder = asyncio.create_task(s.feed_after_write(f"{s.markers.begin}\r\n", "a\r\nhalf"))
    r = await s.run_cmd(cmd="c", timeout=0.05)  # never sends END
    await feeder
    assert r.value == "Command timed out after 0.05s\na\nhalf"  # not "a\r" (M11)


@pytest.mark.asyncio
async def test_what_the_shell_prints_after_the_interrupt_is_not_the_commands():
    s = await onlcr_session(after_interrupt="^C\r\nfake$ ")
    feeder = asyncio.create_task(s.feed_after_write(f"{s.markers.begin}\r\n", "a\r\nhalf"))
    r = await s.run_cmd(cmd="c", timeout=0.05)
    await feeder
    assert r.value == "Command timed out after 0.05s\na\nhalf"
    m = s.markers
    feeder = asyncio.create_task(s.feed_after_write(f"{m.begin}\r\nok\r\n{m.end_prefix}0__\r\n"))
    nxt = await s.run_cmd(cmd="echo ok")
    await feeder
    assert nxt.value == "ok"


@pytest.mark.parametrize(
    ("output", "value"),
    [
        pytest.param("", "", id="nothing-yet"),
        pytest.param("line1\r\n", "line1", id="one-output-line"),  # before: ""
        pytest.param(
            "line1\r\nline2\r\nline3\r\nline4-unfin",
            "line1\nline2\nline3\nline4-unfin",
            id="several-lines-and-an-unfinished-one",  # before: "line1"
        ),
    ],
)
def test_a_zephyr_partial_keeps_every_line_the_command_wrote(output: str, value: str):
    # The timeout arm's value on a Zephyr session: the rejected BEGIN, its
    # prompt, then what the command wrote; no prompt, retval or END yet.
    s = MockSession(command_frame=ZephyrFrame())
    out = ExecOutput(valuer=s._partial_value())
    out.feed(f"\r\n{s.markers.begin}: command not found\r\n~$ \r\n{output}")
    assert out.value() == value


def test_a_partial_before_begin_has_arrived_is_empty():
    # A cut marker is the frame's, never the command's (before: the cut marker).
    s = MockSession()
    assert s._partial_value()(s.markers.begin[:-3]) == ""
