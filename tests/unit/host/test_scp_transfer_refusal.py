"""An scp transfer is refused on a device measured to have no ``scp`` binary.

The ``scp-transfer`` record in :data:`~otto.host.userland.GAPS` says why: the
legacy scp protocol execs an ``scp`` binary on the FAR SIDE, and a stock BusyBox
userland has none. What came back instead was asyncssh's own error, once per
file, after the connection was already up — and nothing landed.

**A refusal rather than an adaptation, and that is a property of the surface
rather than a preference.** ``shutdown-command`` could ADAPT because the device
had a second spelling otto could emit instead; here there is no second spelling.
:class:`~otto.host.options.ScpOptions` carries no binary-name override — which
is what separates this surface from ``nc-transfer``, where
``NcOptions.exec_name`` means the presence of *an* ``nc`` is not the question —
and the name the far side runs is the protocol's, not otto's. The record's own
answer is a different ``transfer``, so the only thing a call site can do here is
decline. :class:`TestTheProtocolIsWhyThisGuardExists` pins that premise, because
a future ``exec_name`` on ``ScpOptions`` would make this whole file the wrong
shape.

**The resolver this guard reads is OPTIONAL on the backend**, which is not the
call :class:`~otto.host.transfer.shell.ShellFileTransfer` made and is the one
thing here that is a property of ``scp``'s POSITION rather than of the surface:
it is :class:`~otto.host.unix_host.UnixHost`'s default ``transfer``, so
demanding a resolver would refuse to BUILD a host rather than merely leave it
unguarded. :class:`TestTheResolverIsOptionalBecauseScpIsTheDefaultTransfer`
holds each fact that argument rests on.

**The scripted device below reproduces a measurement; it is not the evidence
for it.** The evidence is in the record's ``measured_on`` — Tier 3's real
``scp -O`` into the pinned BusyBox root, and the batched applet probe against
all five matrix artifacts. :class:`_ProbingRunner` exists so these tests can
watch what otto does at the call site without a rootfs.
"""

import dataclasses
import inspect
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from otto.host import userland as userland_module
from otto.host.errors import UnsupportedOnUserlandError
from otto.host.options import ScpOptions, UserlandOptions
from otto.host.os_profile import get_os_profile
from otto.host.transfer.base import TransferContext
from otto.host.transfer.scp import SCP_APPLET, ScpFileTransfer, refuse_if_scp_is_absent
from otto.host.userland import (
    APPLET_ABSENT,
    APPLET_PRESENT,
    PROBED_APPLETS,
    UNTESTED,
    Gap,
    Userland,
    applet_capability,
    gap_for,
)
from otto.result import CommandResult
from otto.utils import Status

SURFACE = "scp-transfer"
"""The registry record this guard is the fifth product consumer of."""

_HOST = "bb-1"
"""The host name the backend is built with, so the message can be checked for it."""


def _ok(cmd: str, out: str = "") -> CommandResult:
    return CommandResult(status=Status.Success, value=out, command=cmd, retcode=0)


async def _unreachable_runner(cmd: str, **_kwargs: object) -> CommandResult:
    """A runner that cannot answer, so only DECLARED capabilities settle.

    ``Userland._send`` turns an exception into "could not be asked" rather than
    letting it escape, so this is the cheap way to build a resolved userland
    that reached no device at all.
    """
    raise OSError(f"no device in this test: {cmd!r}")


class _ProbingRunner:
    """A device that answers ONLY the batched applet probe, naming *present*.

    Everything else is unanswerable, which is deliberate: the six non-applet
    capabilities then go unasked and stay unsettled, so a test that accidentally
    started depending on one of them would be depending on a guess — and the
    applet answers demonstrably came from the batch rather than from a
    declaration.

    A callable object rather than a closure so :attr:`issued` is an ordinary
    attribute, which is what lets a test count the round trips the guard costs.
    """

    def __init__(self, present: "set[str]") -> None:
        self.present = present
        self.issued: list[str] = []

    async def __call__(self, cmd: str, **_kwargs: object) -> CommandResult:
        self.issued.append(cmd)
        if not cmd.startswith("for a in "):
            raise OSError(f"the scripted device answers only the applet batch, not {cmd!r}")
        names = cmd.split("for a in ", 1)[1].split(";", 1)[0].split()
        # `echo` is the batch's positive control and is always a shell builtin.
        lines = [f"{n}={1 if n == 'echo' or n in self.present else 0}" for n in names]
        return _ok(cmd, "\n".join(lines))

    @property
    def batches(self) -> "list[str]":
        """Just the applet batches, which is the cost this guard adds."""
        return [c for c in self.issued if c.startswith("for a in ")]


_DECLARED_SIX = {
    "elevation": "sudo",
    "timeout_style": "coreutils",
    "base64_flag": "-d",
    "stat_size": "stat",
    "checksum": "md5sum",
    "shell_dialect": "bash",
}
"""The six non-applet capabilities, pinned so a resolution settles COMPLETELY.

Only needed where a test's claim is about ``resolve()`` being idempotent: a
round that leaves anything unsettled is instead rate-limited by
``_RETRY_COOLDOWN_S``, and a test that could not tell those two apart would pass
for the wrong reason.
"""


async def _declaring(**applets: str) -> Userland:
    """A resolved ``Userland`` whose applet answers are the ones passed in.

    Every name in :data:`~otto.host.userland.PROBED_APPLETS` is declared — the
    ones not named default to present — so the batch is never issued and
    ``is_settled`` answers True for all of them. The runner refuses to answer
    anything, which is what proves the answers came from the declaration.
    """
    declared = {applet_capability(a): applets.get(a, APPLET_PRESENT) for a in PROBED_APPLETS}
    userland = Userland(UserlandOptions(**declared), _unreachable_runner)
    await userland.resolve()
    return userland


async def _measuring(present: "set[str]") -> Userland:
    """A resolved ``Userland`` whose applet answers came from a real probe round."""
    userland = Userland(UserlandOptions(), _ProbingRunner(present))
    await userland.resolve()
    return userland


class _Connections:
    """A connection manager that fails loudly if anything asks it for a connection.

    ``_get_files_scp``/``_put_files_scp`` both open with
    ``await self._connections.ssh()``, so "nothing was attempted" is observable
    as this never being reached — a stronger claim than "no bytes moved".
    """

    term = "ssh"

    def __init__(self) -> None:
        self.ssh_calls = 0

    async def ssh(self) -> Any:
        self.ssh_calls += 1
        raise AssertionError(
            "the scp backend opened a connection; the refusal is supposed to come first"
        )


def _backend(userland: "Userland | None") -> ScpFileTransfer:
    """A real ``ScpFileTransfer`` — real ``_run_put``/``_run_get`` — on a dead wire."""
    return ScpFileTransfer(
        connections=_Connections(),  # type: ignore[arg-type]
        name=_HOST,
        exec_cmd=AsyncMock(),
        scp_options=ScpOptions(),
        userland=userland,
    )


def _spy_on_the_protocol(backend: ScpFileTransfer) -> "tuple[AsyncMock, AsyncMock]":
    """Replace both protocol halves, so "did it get past the guard" is a fact.

    The guard raising is only half the claim; the other half is that a device
    that is NOT in the measured class still reaches the protocol. Spying on both
    lets one backend answer both questions without asyncssh.
    """
    put = AsyncMock(return_value={})
    get = AsyncMock(return_value={})
    backend._put_files_scp = put  # type: ignore[method-assign]
    backend._get_files_scp = get  # type: ignore[method-assign]
    return put, get


def _unix_host(*, resolverless: bool = False):
    """A real ``UnixHost`` on its default ``transfer``, which is ``scp``.

    *resolverless* overrides the ``_userland()`` hook back to the ``None`` its
    base implementation answers — a subclass rather than an assignment, since
    the hosts are ``@dataclass(slots=True)``. That is the shape the guard's
    ``None`` arm exists for, and the shape a mandatory resolver would refuse to
    build at all.
    """
    from otto.host.login_proxy import Cred
    from otto.host.unix_host import UnixHost
    from otto.logger.mode import LogMode

    class _Resolverless(UnixHost):
        def _userland(self) -> "Userland | None":  # type: ignore[override]
            return None

    cls = _Resolverless if resolverless else UnixHost
    return cls(
        ip="10.0.0.9",
        element="box",
        creds=[Cred(login="u", password="p")],
        log=LogMode.QUIET,
    )


def _downgraded(monkeypatch: pytest.MonkeyPatch) -> None:
    """Replace the record with an ``untested`` copy of itself. Nothing else changes."""
    gap = gap_for(SURFACE)
    assert gap is not None
    monkeypatch.setattr(
        userland_module,
        "GAPS",
        [
            Gap(
                surface=gap.surface,
                status=UNTESTED,
                reason=gap.reason,
                measured_on="",
                queued_for=gap.queued_for,
            )
        ],
    )


def _applets_default_to_absent(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make the ``is_settled`` half of the predicate ABLE to fire.

    ``_UNASKABLE_DEFAULTS`` maps every applet to ``present``, so an unasked
    batch currently reads as "the device has ``scp``" and the value half of the
    guard already declines to refuse — which would leave the ``is_settled`` half
    structurally redundant and its deletion unobservable. Flipping the default
    the other way is the mutation that makes it observable, and it is not
    hypothetical: ``absent`` is what a table-wide "assume nothing" change would
    naturally choose, and an sshd at its ``MaxSessions`` ceiling would then turn
    every working scp host into a refused one.
    """
    monkeypatch.setattr(
        userland_module,
        "_UNASKABLE_DEFAULTS",
        {
            **{
                k: v
                for k, v in userland_module._UNASKABLE_DEFAULTS.items()
                if not k.startswith("applet_")
            },
            **{applet_capability(a): APPLET_ABSENT for a in PROBED_APPLETS},
        },
    )


# ===========================================================================
# The premise: the protocol needs a remote binary and otto cannot rename it
# ===========================================================================


class TestTheProtocolIsWhyThisGuardExists:
    """Both facts that make this a refusal rather than an adaptation.

    Each is a premise the guard's shape rests on, and each can stop being true
    without anything else reddening — which is exactly why they are asserted
    rather than described.
    """

    def test_scp_options_carries_no_binary_name_override(self) -> None:
        """The difference from ``nc-transfer``, and the reason presence settles it.

        ``NcOptions.exec_name`` means "is there an ``nc``" is not the question
        there: a real netcat installed under another name answers it. Nothing
        here plays that role, so ``applet_scp`` IS the whole question. Add such
        a field and this guard becomes wrong in the expensive direction — it
        would refuse a host whose override names a binary the device has.
        """
        fields = {f.name for f in dataclasses.fields(ScpOptions)}
        assert fields == {"preserve", "recurse", "block_size", "extra"}, (
            f"ScpOptions now carries {sorted(fields)}. `refuse_if_scp_is_absent` keys on "
            f"the `scp` applet alone because no field here can point the protocol at a "
            f"differently-named binary; a new one that can makes presence of `scp` the "
            f"wrong question, exactly as `NcOptions.exec_name` does for `nc-transfer`."
        )

    def test_both_directions_exec_a_binary_on_the_far_side(self) -> None:
        """`asyncssh.scp` speaks the CLASSIC protocol, so the remote name matters.

        A modern scp-over-sftp path would make this record's measurement the
        wrong one — the question would be the subsystem, which is
        ``sftp-transfer``'s. Both halves calling ``asyncssh.scp`` is what keeps
        the two surfaces apart.
        """
        for half in (ScpFileTransfer._get_files_scp, ScpFileTransfer._put_files_scp):
            source = inspect.getsource(half)
            assert "asyncssh.scp(" in source, f"{half.__qualname__} no longer runs asyncssh.scp"

    def test_the_applet_name_is_one_the_module_actually_probes(self) -> None:
        """A name outside the closed list would be a guard that never fires."""
        assert SCP_APPLET in PROBED_APPLETS


# ===========================================================================
# The guard keys on a SETTLED absence
# ===========================================================================


class TestTheGuardKeysOnASettledAbsence:
    """Both halves of the predicate, each with its own way of being wrong."""

    @pytest.mark.asyncio
    async def test_a_declared_absence_is_refused(self) -> None:
        userland = await _declaring(scp=APPLET_ABSENT)
        with pytest.raises(UnsupportedOnUserlandError):
            await refuse_if_scp_is_absent(userland)

    @pytest.mark.asyncio
    async def test_a_measured_absence_is_refused(self) -> None:
        """The BusyBox shape: the batch landed and said no ``scp``."""
        userland = await _measuring(present={"base64", "nc", "poweroff"})
        assert userland.is_settled(applet_capability(SCP_APPLET))
        with pytest.raises(UnsupportedOnUserlandError):
            await refuse_if_scp_is_absent(userland)

    @pytest.mark.asyncio
    async def test_a_device_with_a_real_scp_is_not_refused(self) -> None:
        """The host the ``busybox`` profile keeps ``scp`` in ``valid_transfers`` for."""
        userland = await _measuring(present={"scp", "nc", "poweroff"})
        assert userland.is_settled(applet_capability(SCP_APPLET))
        await refuse_if_scp_is_absent(userland)

    @pytest.mark.asyncio
    async def test_a_probe_round_that_never_arrived_is_not_refused(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The ``is_settled`` half, made able to fire.

        With the applet default flipped to ``absent`` the value half of the
        predicate is satisfied and only ``is_settled`` stands between an
        unanswered probe round and a refusal. Delete that half and this reddens;
        leave it and a device otto never managed to ask is left to attempt the
        transfer exactly as it did before the guard existed.
        """
        _applets_default_to_absent(monkeypatch)
        userland = Userland(UserlandOptions(), _unreachable_runner)
        await userland.resolve()
        assert userland.has_applet(SCP_APPLET) == APPLET_ABSENT
        assert not userland.is_settled(applet_capability(SCP_APPLET))
        await refuse_if_scp_is_absent(userland)

    @pytest.mark.asyncio
    async def test_the_guard_resolves_what_it_reads(self) -> None:
        """The caller hands over an UNRESOLVED userland and still gets a verdict.

        ``has_applet`` raises on a userland nobody resolved, so a guard that
        forgot to await ``resolve()`` would fail with a ``RuntimeError`` about
        otto's internals rather than refuse. Both callers rely on this: neither
        ``_run_put`` nor ``_run_get`` resolves anything of its own.
        """
        runner = _ProbingRunner(present={"nc"})
        userland = Userland(UserlandOptions(), runner)
        with pytest.raises(UnsupportedOnUserlandError):
            await refuse_if_scp_is_absent(userland)
        assert runner.batches, "the guard refused without ever asking the device"


# ===========================================================================
# The verdict and the message are the RECORD's
# ===========================================================================


class TestTheMessageComesFromTheRecord:
    """What separates ``WIRED`` from ``PROBE_REFUSED``.

    A guard that raised on its own authority would satisfy every assertion in
    the class above and leave the record unread.
    """

    @pytest.mark.asyncio
    async def test_the_message_is_the_records_own(self) -> None:
        gap = gap_for(SURFACE)
        assert gap is not None
        userland = await _declaring(scp=APPLET_ABSENT)
        with pytest.raises(UnsupportedOnUserlandError) as exc_info:
            await refuse_if_scp_is_absent(userland)
        message = str(exc_info.value)
        assert gap.surface in message
        assert gap.reason in message
        assert gap.measured_on in message
        assert gap.docs_anchor in message

    @pytest.mark.asyncio
    async def test_the_message_names_the_host_and_what_was_attempted(self) -> None:
        """The two things the record cannot know, supplied by the caller."""
        userland = await _declaring(scp=APPLET_ABSENT)
        with pytest.raises(UnsupportedOnUserlandError) as exc_info:
            await refuse_if_scp_is_absent(userland, host=_HOST, attempted="put of 3 file(s)")
        message = str(exc_info.value)
        assert _HOST in message
        assert "put of 3 file(s)" in message
        assert "nothing was attempted" in message

    @pytest.mark.asyncio
    async def test_flipping_the_record_to_untested_stops_the_refusal(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The half that makes this the TABLE's verdict rather than the guard's.

        The caller decides the host is in the measured class; the record decides
        whether that class is refused at all. Downgraded, the transfer is
        attempted — because "we no longer claim this is broken" cannot mean
        "refuse anyway". A guard that raised directly instead of going through
        ``refuse_if_gapped`` passes every other test in this file and reds here.
        """
        _downgraded(monkeypatch)
        userland = await _declaring(scp=APPLET_ABSENT)
        await refuse_if_scp_is_absent(userland, host=_HOST)


# ===========================================================================
# Both call sites arrive at the guard
# ===========================================================================


class TestPutArrivesAtTheGuard:
    """``_run_put`` is one of the two paths the record names, and it is reachable.

    ``BaseHost.put`` -> ``BaseFileTransfer.put_files`` -> ``_run_put`` is the
    only route bytes take out over this backend, so a guard the public verb
    cannot reach would be decoration.
    """

    @pytest.mark.asyncio
    async def test_a_device_with_no_scp_is_refused_before_the_connection_is_opened(
        self,
    ) -> None:
        backend = _backend(await _declaring(scp=APPLET_ABSENT))
        put, _ = _spy_on_the_protocol(backend)
        with pytest.raises(UnsupportedOnUserlandError) as exc_info:
            await backend.put_files([Path("/tmp/a.bin")], Path("/opt"), show_progress=False)
        assert _HOST in str(exc_info.value)
        put.assert_not_awaited()
        assert backend._connections.ssh_calls == 0  # type: ignore[attr-defined]

    @pytest.mark.asyncio
    async def test_a_device_with_scp_still_reaches_the_protocol(self) -> None:
        """Non-vacuity: the refusal above is the guard's, not the fake wire's."""
        backend = _backend(await _declaring(scp=APPLET_PRESENT))
        put, _ = _spy_on_the_protocol(backend)
        result = await backend.put_files([Path("/tmp/a.bin")], Path("/opt"), show_progress=False)
        assert result.is_ok
        put.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_the_probe_round_is_paid_once_per_call_and_not_once_per_file(self) -> None:
        """The cost this guard adds is charged above the per-file fan-out.

        It sits in ``_run_put``, which ``put_files`` calls once however many
        files it was given, and ``resolve()`` is idempotent once everything is
        settled — so a five-file put issues the batch once and a second put on
        the same host issues nothing at all. The six non-applet capabilities are
        DECLARED here so the round settles completely: left unsettled, the
        second call would issue nothing for the unrelated reason that
        ``_RETRY_COOLDOWN_S`` refused it, and this assertion would pass without
        measuring idempotence at all.
        """
        runner = _ProbingRunner(present={"scp"})
        userland = Userland(UserlandOptions(**_DECLARED_SIX), runner)
        backend = _backend(userland)
        put, _ = _spy_on_the_protocol(backend)
        files = [Path(f"/tmp/f{i}.bin") for i in range(5)]
        await backend.put_files(files, Path("/opt"), show_progress=False)
        await backend.put_files(files, Path("/opt"), show_progress=False)
        assert len(runner.batches) == 1, (
            f"the applet batch was issued {len(runner.batches)} times: {runner.batches}"
        )
        assert put.await_count == 2


class TestGetArrivesAtTheGuard:
    """The other direction, and it is not a restatement of the one above.

    A GET asks the DEVICE to run ``scp`` too — the binary is missing on the same
    side either way — but the two are separate methods and a guard added to one
    is not a guard on the other.
    """

    @pytest.mark.asyncio
    async def test_a_device_with_no_scp_is_refused_before_the_connection_is_opened(
        self,
    ) -> None:
        backend = _backend(await _declaring(scp=APPLET_ABSENT))
        _, get = _spy_on_the_protocol(backend)
        with pytest.raises(UnsupportedOnUserlandError) as exc_info:
            await backend.get_files([Path("/opt/a.bin")], Path("/tmp"), show_progress=False)
        assert _HOST in str(exc_info.value)
        get.assert_not_awaited()
        assert backend._connections.ssh_calls == 0  # type: ignore[attr-defined]

    @pytest.mark.asyncio
    async def test_a_device_with_scp_still_reaches_the_protocol(self) -> None:
        backend = _backend(await _declaring(scp=APPLET_PRESENT))
        _, get = _spy_on_the_protocol(backend)
        result = await backend.get_files([Path("/opt/a.bin")], Path("/tmp"), show_progress=False)
        assert result.is_ok
        get.assert_awaited_once()


# ===========================================================================
# The backend cannot be built without the thing the guard reads
# ===========================================================================


class TestTheResolverIsOptionalBecauseScpIsTheDefaultTransfer:
    """Why this backend threads the resolver through instead of demanding one.

    :class:`~otto.host.transfer.shell.ShellFileTransfer` rejects a context with
    no ``userland`` and is right to: nothing builds it unless a lab entry asked
    for it. ``scp`` is :class:`~otto.host.unix_host.UnixHost`'s DEFAULT
    ``transfer``, so the same rejection here would fire out of
    ``__post_init__`` for every host whose ``_userland()`` hook answers ``None``
    — turning "cannot be guarded" into "cannot be built". This class pins the
    three facts that argument rests on, because each can stop being true on its
    own.
    """

    @staticmethod
    def _ctx(**overrides: Any) -> TransferContext:
        base: dict[str, Any] = {
            "transfer": "scp",
            "host_name": _HOST,
            "connections": MagicMock(),
            "scp_options": ScpOptions(),
            "exec_cmd": AsyncMock(),
            "userland": Userland(UserlandOptions(), _unreachable_runner),
        }
        base.update(overrides)
        return TransferContext(**base)

    def test_scp_is_still_the_default_unix_transfer(self) -> None:
        """The whole reason the resolver is optional. If this moves, revisit."""
        from otto.host.unix_host import UnixHost

        default = next(f.default for f in dataclasses.fields(UnixHost) if f.name == "transfer")
        assert default == "scp", (
            f"UnixHost's default transfer is now {default!r}. `ScpFileTransfer.create` "
            f"threads its userland through instead of demanding one BECAUSE this backend "
            f"is built for every plain unix host; if that is no longer so, the argument "
            f"for the optional resolver has to be made again."
        )

    def test_a_context_with_no_userland_still_builds_a_backend(self) -> None:
        backend = ScpFileTransfer.create(self._ctx(userland=None))
        assert backend._userland is None

    def test_the_contexts_userland_is_what_the_backend_reads(self) -> None:
        """Threading it through is the load-bearing half, and it is not free.

        A backend handed a fresh resolver instead of the host's own would pass
        every refusal test in this file and quietly pay a second probe round per
        transfer.
        """
        userland = Userland(UserlandOptions(), _unreachable_runner)
        backend = ScpFileTransfer.create(self._ctx(userland=userland))
        assert backend._userland is userland

    def test_a_real_scp_host_is_built_with_its_own_resolver(self) -> None:
        """The production wiring, end to end, through the registry seam."""
        host = _unix_host()
        backend = host._file_transfer
        assert isinstance(backend, ScpFileTransfer)
        assert backend._userland is host._userland()

    @pytest.mark.asyncio
    async def test_a_host_whose_hook_answers_none_builds_and_is_not_refused(self) -> None:
        """The ``None`` arm, reached by the shape that actually produces it.

        ``UserlandHost._userland`` answers ``None`` by default and subclasses may
        keep it that way, so this is not a defensive branch. Two claims in one
        test because they are one failure: the host must BUILD (a rejecting
        ``create`` reddens here), and the transfer must not be refused (deleting
        the ``None`` arm reddens here with an ``AttributeError`` on ``None``).
        """
        host = _unix_host(resolverless=True)
        backend = host._file_transfer
        assert isinstance(backend, ScpFileTransfer)
        assert backend._userland is None
        put, _ = _spy_on_the_protocol(backend)
        await backend.put_files([Path("/tmp/a.bin")], Path("/opt"), show_progress=False)
        put.assert_awaited_once()


# ===========================================================================
# The refusal is about the DEVICE, never about the profile
# ===========================================================================


class TestTheRefusalIsKeyedOnTheDeviceNotTheProfile:
    """A BusyBox host with a real ``scp`` installed must keep working.

    The cheap way to write this guard would have been to key on ``os_type`` or
    on the profile, and it would have been wrong: the ``busybox`` profile keeps
    ``scp`` in ``valid_transfers`` precisely so such a device can opt in.
    """

    def test_the_busybox_profile_still_offers_scp(self) -> None:
        profile = get_os_profile("busybox")
        assert profile is not None
        assert "scp" in profile.defaults["valid_transfers"]

    @pytest.mark.asyncio
    async def test_a_busybox_shaped_device_that_has_scp_transfers(self) -> None:
        """Everything else about the userland is BusyBox-shaped; only ``scp`` is there."""
        userland = await _measuring(present={"scp", "nc", "poweroff", "uuencode", "uudecode"})
        backend = _backend(userland)
        put, _ = _spy_on_the_protocol(backend)
        await backend.put_files([Path("/tmp/a.bin")], Path("/opt"), show_progress=False)
        put.assert_awaited_once()
