"""An sftp transfer that fails for want of a subsystem says SO, in the record's words.

**This file is the one in the busybox series that guards a MESSAGE rather than
a refusal, and that is the finding rather than a shortcut.** Its sibling
``test_scp_transfer_refusal.py`` pins a guard that declines before opening a
connection; nothing here declines anything. The ``sftp-transfer`` surface
cannot be pre-checked without refusing hosts that work — ``sftp-server`` is not
on ``PATH`` even on Debian, its absolute path differs across distros and is
compiled into dropbear rather than configured, and the daemon is not the
authority, since packaged dropbear serves sftp perfectly well on a machine that
provides the binary. The only definitive test is opening the subsystem, which
is the operation. So otto attempts it, and
:func:`~otto.host.transfer.sftp.open_sftp_or_attribute` turns the failure into
this record's four facts.

WHAT WAS MEASURED, AND WHY IT IS NOT IN THIS FILE. The evidence is in the
record's ``measured_on``: ``UnixHost.put`` on a host built with
``transfer: sftp``, against the Tier 3 dropbear serving the pinned BusyBox root
over real ssh, raised ``asyncssh.sftp.SFTPConnectionLost: 0 bytes read on a
total of 4 expected bytes`` in 22ms, moved no bytes and left nothing on either
side. ``tests/busybox/test_tier3_sftp_attribution.py`` re-runs that against the
real device. What THIS file adds is the part a real device cannot show cheaply:
that the message is the record's and not a second copy of it, that downgrading
the record puts asyncssh's error back untouched, and that the arms which must
NOT fire do not.

THE THREE THINGS THAT WOULD MAKE THIS GUARD A DEFECT, each with its own class:

* it fires on a host that CAN do sftp — impossible by construction and pinned
  anyway (:class:`TestNothingThatWorksIsTouched`), because it is the mistake
  the whole gap registry is ordered against and because "impossible by
  construction" is a claim about today's construction;
* it swallows a failure that is not this surface's, turning an sshd at its
  ``MaxSessions`` ceiling into "your userland has no sftp"
  (:class:`TestTheCatchIsNarrowerThanEveryWayAnSftpSessionFails`);
* it stops being the RECORD's message and becomes a hard-coded one, at which
  point the table is decoration (:class:`TestTheMessageComesFromTheRecord`).
"""

import inspect
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from asyncssh.misc import ChannelOpenError
from asyncssh.sftp import SFTPConnectionLost

from otto.host import userland as userland_module
from otto.host.errors import UnsupportedOnUserlandError
from otto.host.os_profile import get_os_profile
from otto.host.transfer.sftp import SftpFileTransfer, open_sftp_or_attribute
from otto.host.userland import UNTESTED, Gap, gap_for
from otto.result import Result
from otto.utils import Status

SURFACE = "sftp-transfer"
"""The record every assertion here reads. One spelling, so a rename reds once."""

_MEASURED_ASYNCSSH_TEXT = "0 bytes read on a total of 4 expected bytes"
"""What asyncssh actually said, verbatim, against the Tier 3 BusyBox root.

Not invented for the fake. It is the string
``asyncssh.sftp.SFTPClientHandler.start`` builds when the subsystem channel
closes before the version exchange, and it is quoted in the record's
``measured_on`` for the same reason it is used here: a scripted failure whose
text is nothing like the real one would let the "asyncssh's own error survives"
assertions pass against a shape no device produces.
"""


class _FakeConnections:
    """A connection manager whose ``sftp()`` does whatever the test needs.

    Standing in for :class:`~otto.host.connections.ConnectionManager` and for
    nothing else: the guard reads exactly one method off it. A ``MagicMock``
    would do the same job and would also answer every OTHER attribute, so a
    guard that started consulting the manager for something else would go on
    passing here.
    """

    def __init__(self, outcome: "BaseException | object") -> None:
        self._outcome = outcome
        self.calls = 0

    async def sftp(self) -> Any:
        self.calls += 1
        if isinstance(self._outcome, BaseException):
            raise self._outcome
        return self._outcome


def _downgraded(monkeypatch: pytest.MonkeyPatch) -> None:
    """Replace the record with an ``untested`` copy of itself. Nothing else changes.

    THE MUTATION THIS FILE'S CENTRAL CLAIM RESTS ON. Everything else here could
    pass against a guard that raised a hard-coded
    :exc:`~otto.host.errors.UnsupportedOnUserlandError` of its own and never
    read the table at all. Under this one, such a guard would go on raising and
    the assertion that asyncssh's exception comes back reds.

    Lifted in shape from ``test_scp_transfer_refusal.py::_downgraded``, and the
    ``measured_on=""`` is not cosmetic: :class:`~otto.host.userland.Gap`'s
    invariant refuses an ``untested`` record that carries a measurement, so the
    copy would fail to construct with it.
    """
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


def _backend(connections: _FakeConnections) -> SftpFileTransfer:
    """The real backend over a scripted manager. Nothing about it is faked."""
    return SftpFileTransfer(
        connections=connections,  # ty: ignore[invalid-argument-type]
        name="dut",
        exec_cmd=AsyncMock(),
    )


# ===========================================================================
# The message is the record's, and the record is what decides
# ===========================================================================


class TestTheMessageComesFromTheRecord:
    """Four facts from the table, not a fifth copy of them written here.

    The same claim ``TestATableBackedClaimIsCheckedNotTrusted`` makes
    statically in ``test_gap_registry.py`` — that the guard reaches
    :func:`~otto.host.userland.refuse_if_gapped` with this surface — checked
    behaviourally, because the static read cannot tell whether control gets
    there.
    """

    @pytest.mark.asyncio
    async def test_the_message_is_the_records_own(self) -> None:
        gap = gap_for(SURFACE)
        assert gap is not None
        conns = _FakeConnections(SFTPConnectionLost(_MEASURED_ASYNCSSH_TEXT))
        with pytest.raises(UnsupportedOnUserlandError) as caught:
            await open_sftp_or_attribute(conns, host="dut")  # ty: ignore[invalid-argument-type]
        message = str(caught.value)
        for fragment in (gap.surface, gap.reason, gap.measured_on, gap.docs_anchor):
            assert fragment in message, (
                f"the attributed failure does not carry the record's {fragment[:40]!r}. "
                f"An operator who gets this message and still has to find the docs page "
                f"themselves has gained nothing over asyncssh's byte count"
            )

    @pytest.mark.asyncio
    async def test_the_lead_says_what_happened_and_not_that_nothing_did(self) -> None:
        """The one clause this site could not reuse, and the reason ``observed`` exists.

        Every other consumer of the registry leads with ``nothing was
        attempted``, which is the honest thing to say when a guard declined
        before emitting anything. Here the subsystem WAS opened and the device
        WAS asked, so that lead would be false in the message's first clause —
        and false in a way that changes what an operator does next, since it
        says no channel was spent.
        """
        conns = _FakeConnections(SFTPConnectionLost(_MEASURED_ASYNCSSH_TEXT))
        with pytest.raises(UnsupportedOnUserlandError) as caught:
            await open_sftp_or_attribute(conns, host="dut")  # ty: ignore[invalid-argument-type]
        message = str(caught.value)
        assert "nothing was attempted" not in message, (
            "the attributed failure claims nothing was attempted. The subsystem was "
            "opened and the device closed it — that lead is for the guards that decline "
            "first, and saying it here would be the message's first sentence being untrue"
        )
        assert "closed it before the SFTP handshake" in message, (
            f"the message does not say what was observed, so an operator cannot tell "
            f"whether otto declined or the device did: {message[:200]!r}"
        )

    @pytest.mark.asyncio
    async def test_asyncsshs_own_words_survive_into_the_message(self) -> None:
        """The record explains; the original still has to be readable.

        Attribution that DISCARDED the underlying error would trade one
        unreadable failure for one unfalsifiable one — the operator would have
        otto's theory and no way to check it against what the transport said.
        """
        conns = _FakeConnections(SFTPConnectionLost(_MEASURED_ASYNCSSH_TEXT))
        with pytest.raises(UnsupportedOnUserlandError) as caught:
            await open_sftp_or_attribute(conns, host="dut")  # ty: ignore[invalid-argument-type]
        assert _MEASURED_ASYNCSSH_TEXT in str(caught.value)
        assert isinstance(caught.value.__context__, SFTPConnectionLost), (
            "asyncssh's exception is not chained beneath the attributed one, so the "
            "traceback shows otto's sentence and nothing about where it came from"
        )

    @pytest.mark.asyncio
    async def test_the_host_reaches_the_message(self) -> None:
        conns = _FakeConnections(SFTPConnectionLost(_MEASURED_ASYNCSSH_TEXT))
        with pytest.raises(UnsupportedOnUserlandError) as caught:
            await open_sftp_or_attribute(conns, host="tomato", attempted="PUT 3 file(s)")  # ty: ignore[invalid-argument-type]
        assert "tomato" in str(caught.value)
        assert "PUT 3 file(s)" in str(caught.value)

    @pytest.mark.asyncio
    async def test_flipping_the_record_to_untested_hands_back_asyncsshs_error(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """THE MUTATION. Downgrade the record and the translation stops entirely.

        Not "a softer message": the caller gets the original
        :exc:`~asyncssh.sftp.SFTPConnectionLost` object, because the guard's
        fallthrough is a bare ``raise``. That is what makes the table the
        authority here rather than this function, and it is the assertion a
        guard with its own hard-coded message would fail.
        """
        _downgraded(monkeypatch)
        original = SFTPConnectionLost(_MEASURED_ASYNCSSH_TEXT)
        conns = _FakeConnections(original)
        with pytest.raises(SFTPConnectionLost) as caught:
            await open_sftp_or_attribute(conns, host="dut")  # ty: ignore[invalid-argument-type]
        assert caught.value is original, (
            "the record was downgraded to untested and the caller still did not get "
            "asyncssh's own exception back. The guard is deciding on its own authority"
        )


# ===========================================================================
# Nothing that works is touched — the property that makes attribution safe
# ===========================================================================


class TestNothingThatWorksIsTouched:
    """A device whose subsystem STARTS never reaches the attribution at all.

    This is the argument for doing this after the fact instead of before it,
    so it is asserted rather than reasoned about: the guard has no predicate,
    no probe and no profile check, and the only thing that can send it down the
    attributed arm is a subsystem that already failed.
    """

    @pytest.mark.asyncio
    async def test_a_working_subsystem_returns_the_client_untouched(self) -> None:
        client = MagicMock(name="SFTPClient")
        conns = _FakeConnections(client)
        assert await open_sftp_or_attribute(conns, host="dut") is client  # ty: ignore[invalid-argument-type]

    def test_the_busybox_profile_still_offers_sftp(self) -> None:
        """The profile lists ``sftp`` deliberately, and nothing here may change that.

        A BusyBox device with an sftp-server installed transfers perfectly well,
        and the record says so. If this ever stops being true, the reasoning
        behind the whole file changes and it should red here rather than in a
        lab six months later.
        """
        profile = get_os_profile("busybox")
        assert profile is not None
        assert "sftp" in profile.defaults["valid_transfers"], (
            "the `busybox` profile no longer offers `sftp`. This file's premise is that "
            "such a host is SERVED, not refused; a profile that removed the option would "
            "be refusing it one layer up"
        )

    @pytest.mark.asyncio
    async def test_the_guard_consults_nothing_about_the_host(self) -> None:
        """No userland, no options, no ``os_type`` — the backend does not even hold one.

        Written because the OBVIOUS way to wire this surface, and the way its
        five siblings are wired, is a predicate on the device. A future edit
        that added one would produce exactly the false absent the record warns
        about, and it would look like consistency.
        """
        signature = inspect.signature(open_sftp_or_attribute)
        assert set(signature.parameters) == {"connections", "host", "attempted"}, (
            f"open_sftp_or_attribute takes {sorted(signature.parameters)}. A `userland` "
            f"parameter here would mean the guard had grown a predicate about the device "
            f"— which for this surface can only answer `absent` on hosts that work"
        )


# ===========================================================================
# The catch is narrow, and the exclusion is the measured half
# ===========================================================================


class TestTheCatchIsNarrowerThanEveryWayAnSftpSessionFails:
    """``ChannelOpenError`` is NOT this surface, and translating it would lie.

    Measured 2026-08-14 against an in-process asyncssh server that refuses the
    session: ``asyncssh.misc.ChannelOpenError: Session refused``. It is a real
    way an sftp session fails to start — and it is also what an sshd at its
    ``MaxSessions`` ceiling answers, on any host, sftp-server or not. This
    repo has already paid for that confusion once, in the nc bulk-put channel
    budget. Catching it here would tell an operator whose device serves sftp
    perfectly well that their userland cannot.
    """

    @pytest.mark.asyncio
    async def test_a_refused_channel_is_not_attributed_to_the_userland(self) -> None:
        original = ChannelOpenError(4, "Session refused")
        conns = _FakeConnections(original)
        with pytest.raises(ChannelOpenError) as caught:
            await open_sftp_or_attribute(conns, host="dut")  # ty: ignore[invalid-argument-type]
        assert caught.value is original

    @pytest.mark.asyncio
    async def test_an_unreachable_host_is_not_attributed_to_the_userland(self) -> None:
        """``sftp()`` connects SSH first, so a dead host raises through this call too."""
        original = ConnectionRefusedError("nothing listening")
        conns = _FakeConnections(original)
        with pytest.raises(ConnectionRefusedError):
            await open_sftp_or_attribute(conns, host="dut")  # ty: ignore[invalid-argument-type]


# ===========================================================================
# Both directions arrive at the guard, once per call
# ===========================================================================


class TestPutArrivesAtTheGuard:
    """``_run_put`` is a declared ``ATTRIBUTED`` path; prove the message gets there."""

    @pytest.mark.asyncio
    async def test_a_device_with_no_sftp_server_gets_this_record_instead_of_a_byte_count(
        self, tmp_path: Path
    ) -> None:
        gap = gap_for(SURFACE)
        assert gap is not None
        src = tmp_path / "payload.bin"
        src.write_bytes(b"x")
        backend = _backend(_FakeConnections(SFTPConnectionLost(_MEASURED_ASYNCSSH_TEXT)))
        with pytest.raises(UnsupportedOnUserlandError) as caught:
            await backend._run_put([src], Path("/tmp"), None)
        assert gap.docs_anchor in str(caught.value)

    @pytest.mark.asyncio
    async def test_no_file_is_blamed_for_the_missing_subsystem(self, tmp_path: Path) -> None:
        """The ``file-ops-base64`` defect shape, checked for specifically.

        That bug reported a PRESENT file as missing, because a failure of the
        mechanism was rendered through a per-file error arm. This backend has
        exactly such an arm — ``_put_files_sftp`` gathers with
        ``return_exceptions=True`` and renders ``f"{src}: {outcome}"`` — so the
        shape is available here too. It does not occur, and the reason is
        structural rather than careful: the subsystem is opened ABOVE the
        fan-out, so no ``src`` has been named when it fails. Asserted because
        moving the open one level down would reintroduce it silently.
        """
        src = tmp_path / "payload.bin"
        src.write_bytes(b"x")
        backend = _backend(_FakeConnections(SFTPConnectionLost(_MEASURED_ASYNCSSH_TEXT)))
        with pytest.raises(UnsupportedOnUserlandError) as caught:
            await backend._run_put([src], Path("/tmp"), None)
        assert src.name not in str(caught.value), (
            f"the attributed failure names {src.name}, so the missing subsystem is being "
            f"reported as a problem with a file that is present and readable — the "
            f"`file-ops-base64` misattribution, in a second place"
        )

    @pytest.mark.asyncio
    async def test_the_subsystem_is_opened_once_per_call_and_not_once_per_file(
        self, tmp_path: Path
    ) -> None:
        """Above the fan-out, so three files cost one attempt and one message."""
        srcs = []
        for name in ("a.bin", "b.bin", "c.bin"):
            src = tmp_path / name
            src.write_bytes(b"x")
            srcs.append(src)
        conns = _FakeConnections(SFTPConnectionLost(_MEASURED_ASYNCSSH_TEXT))
        backend = _backend(conns)
        with pytest.raises(UnsupportedOnUserlandError):
            await backend._run_put(srcs, Path("/tmp"), None)
        assert conns.calls == 1, (
            f"the subsystem was opened {conns.calls} times for a 3-file PUT. The open sits "
            f"above the per-file gather so the operator gets one message, not one per file "
            f"— which is the shape the scp record's own measurement complained about"
        )


class TestGetArrivesAtTheGuard:
    """``_run_get`` is the other declared ``ATTRIBUTED`` path. Same guard, same message."""

    @pytest.mark.asyncio
    async def test_a_device_with_no_sftp_server_gets_this_record_instead_of_a_byte_count(
        self, tmp_path: Path
    ) -> None:
        gap = gap_for(SURFACE)
        assert gap is not None
        backend = _backend(_FakeConnections(SFTPConnectionLost(_MEASURED_ASYNCSSH_TEXT)))
        with pytest.raises(UnsupportedOnUserlandError) as caught:
            await backend._run_get([Path("/tmp/remote.bin")], tmp_path, None)
        assert gap.docs_anchor in str(caught.value)

    @pytest.mark.asyncio
    async def test_a_working_subsystem_still_transfers(self, tmp_path: Path) -> None:
        """The success path through the new signature, so the wiring is not just the sad one.

        ``_run_get`` now opens the client and HANDS IT to ``_get_files_sftp``
        rather than letting it open its own. That is a real change to how the
        bytes move, and a file whose every test raises would not notice it
        breaking.
        """
        client = MagicMock(name="SFTPClient")
        client.get = AsyncMock()
        backend = _backend(_FakeConnections(client))
        per_file = await backend._run_get([Path("/tmp/remote.bin")], tmp_path, None)
        assert per_file[Path("/tmp/remote.bin")].status is Status.Success
        client.get.assert_awaited_once()
        assert isinstance(per_file[Path("/tmp/remote.bin")], Result)
