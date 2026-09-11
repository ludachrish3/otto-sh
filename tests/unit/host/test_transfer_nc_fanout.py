"""The nc backend must not fan out past the remote sshd's channel ceiling.

Every in-flight nc transfer holds an SSH exec channel for the whole duration of
its remote ``nc -l`` listener, and its listener-readiness poll opens another. A
default OpenSSH server allows ``MaxSessions 10`` channels per connection and
REFUSES the rest — it does not queue them — so an unbounded ``asyncio.gather``
over the files silently converts "many files" into
``ChannelOpenError('open failed')`` for whichever transfers lose.

Measured on the bed (test2 via test1, one connection), concurrent
``host.exec`` calls against a default sshd:

    N=8   refused=0      N=20  refused=10
    N=10  refused=0      N=24  refused=14
    N=12  refused=2      N=32  refused=22

Exactly ``refused = N - 10``. The same ceiling reached from the other side shows
up as ``Remote nc listener on port P not ready within 5.0s``: the readiness poll
needs a channel too, so when the budget is gone the listener cannot be confirmed
and the transfer fails on a healthy listener.

Found 2026-08-11: the 3.14 leg of ``make nox-full`` lost one file of eight in
``test_a_bulk_hop_put_does_not_strand_a_forward_per_file``, ~1-2 runs in 3, and
never in isolation. Full-suite load stretches transfers so more listeners
overlap, which is what pushes an N=8 put over a ceiling it otherwise clears.

The bound is on whole TRANSFERS, not on channels, and that is deliberate: a
semaphore at the exec/channel layer deadlocks here, because an in-flight
listener holds a channel while its own readiness poll needs a second one, so
enough listeners would consume every permit and block the very polls that must
complete to release them.
"""

import asyncio
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from otto.host.connections import ConnectionManager
from otto.host.errors import HostCommandError
from otto.host.options import NcOptions
from otto.host.transfer import NcFileTransfer
from otto.host.transfer.nc import (
    _NC_CHANNEL_HEADROOM,
    _NC_CHANNELS_PER_TRANSFER,
    _NC_MAX_CONCURRENT_TRANSFERS,
    _NC_SSHD_DEFAULT_MAX_SESSIONS,
)
from otto.result import CommandResult
from otto.utils import Status

# Comfortably past any plausible bound, so the assertions read as "bounded"
# rather than "happened to fit". Also past the raw ceiling of 10, so an
# unbounded fan-out is one a real sshd would refuse.
_FILES = 20

_PER_FILE_PATHS = ["_put_files_nc", "_get_files_nc", "_get_files_nc_tunneled"]


def _make_ft(term: str = "ssh", **nc_kwargs) -> NcFileTransfer:
    connections = MagicMock(spec=ConnectionManager)
    connections.has_tunnel = False
    connections.ip = "10.0.0.1"
    connections.term = term
    return NcFileTransfer(
        connections=connections,
        name="fanout",
        transfer="nc",
        nc_options=NcOptions(**nc_kwargs),
        get_local_ip=lambda: "127.0.0.1",
        exec_cmd=AsyncMock(),
        userland=None,
    )


class _FanoutRecorder:
    """Records the peak number of per-file attempts in flight at once.

    Each call yields once so siblings get a chance to start, then fails, so the
    measurement does not depend on any of the transfer machinery beyond the
    fan-out itself.
    """

    def __init__(self) -> None:
        self.in_flight = 0
        self.peak = 0

    async def __call__(self, *_args, **_kwargs) -> int:
        self.in_flight += 1
        self.peak = max(self.peak, self.in_flight)
        await asyncio.sleep(0)
        self.in_flight -= 1
        raise HostCommandError("fan-out probe: no real transfer needed")


def _install_recorder(ft: NcFileTransfer, method: str) -> _FanoutRecorder:
    """Hook the recorder into the first await of one path's per-file attempt.

    The hook differs per path because the paths differ. The two that spawn a
    REMOTE listener open by reserving a remote port, so ``_find_free_port`` is
    their per-file entry. The plain get direction listens LOCALLY
    (``asyncio.start_server``) and spends its channel on the remote sender
    instead, so it never reserves a remote port at all — hooking
    ``_find_free_port`` there would record a peak of zero and the test would
    pass by measuring nothing.
    """
    recorder = _FanoutRecorder()
    if method != "_put_files_nc":
        # Leave the control plane working: BOTH get directions pre-fetch every
        # file's size through `_control_run` BEFORE they fan out, and each
        # REFUSES a file whose size it could not measure — the size terminates
        # the read, so there is nothing to degrade to. A recorder answering
        # those too would refuse all twenty before the gather and measure a
        # fan-out of zero.
        ft._control_run = AsyncMock(  # type: ignore[method-assign]
            # The prefetch's `stat -L -c '%s %F'` shape: a size AND a type it
            # will transfer. A bare size parses as unstatable and refuses all
            # twenty, which is the same measurement of zero.
            return_value=CommandResult(Status.Success, value="1 regular file", retcode=0)
        )
    if method == "_get_files_nc":
        ft._exec_cmd = recorder  # type: ignore[assignment]
    else:
        ft._find_free_port = recorder  # type: ignore[method-assign]
    return recorder


def _files(tmp_path: Path) -> list[Path]:
    out = []
    for i in range(_FILES):
        f = tmp_path / f"fanout_{i}.txt"
        f.write_text("x")
        out.append(f)
    return out


@pytest.mark.parametrize("method", _PER_FILE_PATHS)
@pytest.mark.asyncio
async def test_the_per_file_fan_out_is_bounded(method: str, tmp_path: Path):
    """All three per-file paths dispatch with a bound, not unbounded.

    Parametrized rather than written once because each path had its own copy of
    the gather, and fixing one would leave the others exposed — which is how
    the get directions would have kept the defect after the put direction that
    produced the failing test was fixed.
    """
    ft = _make_ft()
    recorder = _install_recorder(ft, method)

    await getattr(ft, method)(_files(tmp_path), Path("/tmp"))

    assert recorder.peak <= _NC_MAX_CONCURRENT_TRANSFERS, (
        f"{method} ran {recorder.peak} transfers at once for {_FILES} files. Each "
        f"holds ~{_NC_CHANNELS_PER_TRANSFER} SSH channels, so this needs "
        f"{recorder.peak * _NC_CHANNELS_PER_TRANSFER} against a default sshd "
        f"ceiling of {_NC_SSHD_DEFAULT_MAX_SESSIONS}; the excess is refused, not "
        "queued"
    )


@pytest.mark.parametrize("method", _PER_FILE_PATHS)
@pytest.mark.asyncio
async def test_the_bound_still_uses_the_available_budget(method: str, tmp_path: Path):
    """The bound must not collapse to serial.

    A bound of 1 would satisfy the test above while making every bulk transfer
    N times slower than it needs to be. The point is to fit the channel budget,
    not to abandon concurrency — so this pins the floor the other test's
    ceiling leaves open, and pins it for each path, since a path that lost its
    fan-out entirely (an ``async for`` where a gather belonged) would otherwise
    look like a pass.
    """
    ft = _make_ft()
    recorder = _install_recorder(ft, method)

    await getattr(ft, method)(_files(tmp_path), Path("/tmp"))

    assert recorder.peak > 1, f"{method} serialized its transfers entirely"


@pytest.mark.parametrize("method", _PER_FILE_PATHS)
@pytest.mark.asyncio
async def test_a_configured_limit_overrides_the_derived_one(method: str, tmp_path: Path):
    """A host whose sshd is not default can say so, and be obeyed exactly.

    The derived default is a guess about the SERVER — otto cannot read its
    ``MaxSessions`` — so the knob is the only way a host with a lowered ceiling
    can be transferred to at all. Asserting equality, not ``<=``: a limit that
    is silently narrowed is as much a bug as one that is ignored.
    """
    limit = 2
    assert limit < _NC_MAX_CONCURRENT_TRANSFERS, "pick a limit the default would not produce"
    ft = _make_ft(max_concurrent_transfers=limit)
    recorder = _install_recorder(ft, method)

    await getattr(ft, method)(_files(tmp_path), Path("/tmp"))

    assert recorder.peak == limit, (
        f"{method} ran {recorder.peak} at once against a configured limit of {limit}"
    )


@pytest.mark.asyncio
async def test_concurrent_transfer_calls_share_one_host_budget(tmp_path: Path):
    """The budget belongs to the host, not to one call.

    ``MaxSessions`` is enforced per CONNECTION, so a bound scoped to a single
    bulk transfer leaves the ceiling just as reachable by many small ones. That
    shape is not hypothetical: ``test_real_nc_high_fanout_put`` gathers 20
    separate one-file puts against one host, and a per-call bound gives each of
    them its own full budget while they all spend the same channels.
    """
    ft = _make_ft()
    recorder = _install_recorder(ft, "_put_files_nc")

    await asyncio.gather(
        *(ft._put_files_nc([f], Path("/tmp")) for f in _files(tmp_path)),
        return_exceptions=True,
    )

    assert recorder.peak <= _NC_MAX_CONCURRENT_TRANSFERS, (
        f"{_FILES} separate one-file puts on one host reached {recorder.peak} "
        f"concurrent transfers — the bound is scoped per call, not per connection"
    )


def test_a_limit_below_one_is_refused_at_construction():
    """Fail at construction, not as a hang with nothing to point at.

    ``Semaphore(0)`` hands out no permits ever, so an accepted zero would turn
    every bulk transfer on that host into an indefinite park — the failure mode
    hardest to trace back to a config value.
    """
    with pytest.raises(ValueError, match="at least 1"):
        _make_ft(max_concurrent_transfers=0)


def test_the_channel_budget_constants_leave_headroom():
    """The derived limit must fit under the ceiling it is derived from.

    Encodes the arithmetic rather than the answer, so raising
    ``_NC_CHANNELS_PER_TRANSFER`` (say, if a third control call joins each
    attempt) cannot silently produce a limit that overruns the ceiling again.
    """
    assert _NC_MAX_CONCURRENT_TRANSFERS >= 1
    needed = _NC_MAX_CONCURRENT_TRANSFERS * _NC_CHANNELS_PER_TRANSFER + _NC_CHANNEL_HEADROOM
    assert needed <= _NC_SSHD_DEFAULT_MAX_SESSIONS, (
        f"a full fan-out needs {needed} channels plus headroom against a default "
        f"sshd ceiling of {_NC_SSHD_DEFAULT_MAX_SESSIONS}"
    )


@pytest.mark.parametrize("method", _PER_FILE_PATHS)
@pytest.mark.asyncio
async def test_no_concurrent_moves_one_file_at_a_time(method: str, tmp_path: Path):
    """``concurrent=False`` is honoured by every per-file path.

    The opt-out has to reach the dispatcher on all three, not just the one a
    caller happened to exercise: a path that ignored the flag would keep
    overlapping transfers a caller asked to serialize, and the only symptom is
    load the caller explicitly said it could not take.
    """
    ft = _make_ft()
    recorder = _install_recorder(ft, method)

    await getattr(ft, method)(_files(tmp_path), Path("/tmp"), concurrent=False)

    assert recorder.peak == 1, f"{method} ran {recorder.peak} at once under concurrent=False"


def test_the_constants_are_the_shared_derivation():
    """nc's channel budget is the shared derivation, not a parallel copy of it.

    The four names stay because they say what they bound, but each must be the
    base's answer: a second spelling of the same arithmetic is one that drifts
    the moment the ceiling or the headroom moves.
    """
    from otto.host.transfer.base import (
        SSH_CHANNEL_HEADROOM,
        SSHD_DEFAULT_MAX_SESSIONS,
        derive_concurrency_limit,
    )

    assert _NC_SSHD_DEFAULT_MAX_SESSIONS == SSHD_DEFAULT_MAX_SESSIONS
    assert _NC_CHANNEL_HEADROOM == SSH_CHANNEL_HEADROOM
    assert derive_concurrency_limit(_NC_CHANNELS_PER_TRANSFER) == _NC_MAX_CONCURRENT_TRANSFERS
    assert _make_ft().concurrency_limit == _NC_MAX_CONCURRENT_TRANSFERS


def _warmup_true_execs(ft: NcFileTransfer) -> int:
    """How many pool-warming ``true`` execs the transfer asked for.

    The warm-up is the only thing that execs a bare ``true``; the strategy
    probe and the listener spawn are long scripts, so counting the literal
    separates pool warming from everything else on the same seam.
    """
    return sum(1 for call in ft._exec_cmd.call_args_list if call.args and call.args[0] == "true")


@pytest.mark.parametrize(
    ("concurrent", "expected"),
    [(True, _NC_MAX_CONCURRENT_TRANSFERS), (False, 1)],
)
@pytest.mark.asyncio
async def test_the_warm_up_opens_only_the_sessions_the_batch_will_spend(
    concurrent: bool, expected: int, tmp_path: Path
):
    """Pool warming is sized by the fan-out, not by the file count.

    On telnet each warmed session is a real login handshake, so warming one
    per file is load the host pays for sessions the transfer can never use at
    once — the cap is the ceiling on in-flight transfers. Under
    ``concurrent=False`` it is worse than waste: firing twenty handshakes is
    exactly the burst the caller passed the flag to avoid, so the opt-out
    would be honoured by the transfers and broken by the warm-up.
    """
    ft = _make_ft(term="telnet")
    _install_recorder(ft, "_put_files_nc")

    await ft._put_files_nc(_files(tmp_path), Path("/tmp"), concurrent=concurrent)

    assert _warmup_true_execs(ft) == expected, (
        f"warmed {_warmup_true_execs(ft)} telnet sessions for {_FILES} files under "
        f"concurrent={concurrent}, which can spend at most {expected} at once"
    )
