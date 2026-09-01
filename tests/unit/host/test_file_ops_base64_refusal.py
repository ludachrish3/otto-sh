"""``read_file``/``write_file`` are refused on a device measured to have no ``base64``.

The ``file-ops-base64`` record in :data:`~otto.host.userland.GAPS` says why:
both methods move their payload through the device's own ``base64`` and
hard-code the spelling, so a userland with no such applet can run neither. What
came back instead was worse than a bad error message on the read side —
``read_file`` turned the device's ``base64: not found`` into a
``FileNotFoundError`` naming a file that is present — and destructive on the
write side, where the shell opens ``> <path>`` before it resolves the command
and left the destination at zero bytes.

**Both halves are pinned here, deliberately in one file**, because for this
surface the guard and its reachability are one question: the two call sites are
in the same module as the guard and are its only callers, so a test that arrives
at ``read_file`` IS the test that the guard fires. The two prior surfaces split
them (``tests/unit/host/test_daemon_launch_refusal.py`` pins the guard,
``tests/unit/link/test_manage_impair.py`` the reach) because their callers live
in another package.

**The scripted device below reproduces a measurement; it is not the evidence
for it.** The evidence is in the record's ``measured_on`` — the 1.16.1 artifact's
own ash, ``PATH`` blocked. ``_Device`` exists so the tests can watch what otto
does at the call site, including the truncation, without a rootfs.
"""

import base64 as b64
import dataclasses
from pathlib import Path

import pytest

from otto.host import userland as userland_module
from otto.host.docker_host import DockerContainerHost
from otto.host.errors import HostUnreachableError, UnsupportedOnUserlandError
from otto.host.file_ops import PosixFileOps, refuse_if_base64_is_absent
from otto.host.local_host import LocalHost
from otto.host.options import UserlandOptions
from otto.host.privilege import PosixPrivilege
from otto.host.unix_host import UnixHost
from otto.host.userland import (
    MEASURED_BROKEN,
    PROBED_APPLETS,
    UNTESTED,
    Gap,
    Userland,
    UserlandHost,
    applet_capability,
    gap_for,
)
from otto.logger.mode import LogMode
from otto.result import CommandResult
from otto.utils import Status

SURFACE = "file-ops-base64"
"""The registry record this guard is the third product consumer of."""

_PATH = "/etc/hostname"
"""A file that EXISTS on the scripted device — the whole point of the read half.

The old failure named this path in a ``FileNotFoundError``, so a path the
device really has is what makes that error a lie rather than a fair report.
"""

_READ_CMD = f"base64 {_PATH}"
"""The exact line ``read_file`` types for :data:`_PATH`.

Unquoted because ``shlex.quote`` leaves a path with no shell metacharacters
alone; the quoting itself is ``PosixFileOps._q``'s contract and is pinned in
``tests/unit/host/test_file_ops.py``, not here.
"""


def _ok(cmd: str, out: str = "") -> CommandResult:
    return CommandResult(status=Status.Success, value=out, command=cmd, retcode=0)


def _fail(cmd: str, out: str, code: int = 127) -> CommandResult:
    return CommandResult(status=Status.Error, value=out, command=cmd, retcode=code)


class _Device:
    """A scripted BusyBox-shaped userland, with or without a ``base64`` applet.

    Answers the exact probe spellings ``Userland`` issues (pinned as a set in
    ``tests/unit/host/test_userland.py``; this only has to agree with them,
    which the premise test below checks by observing that resolution SETTLES).
    The answers are the 1.16.1 shape: no ``sudo`` but ``su``, ``timeout -t``,
    no ``bash``.

    ``files`` is the device's filesystem, so the read half can return real
    content and the write half can show what it does to an existing file.
    """

    def __init__(self, *, has_base64: bool, unreachable: bool = False) -> None:
        self.has_base64 = has_base64
        self.unreachable = unreachable
        self.files: dict[str, str] = {_PATH: "device-hostname\n"}
        self.commands: list[str] = []

    def answer(self, cmd: str) -> CommandResult:
        self.commands.append(cmd)
        if self.unreachable:
            raise HostUnreachableError(f"scripted device refused a channel for {cmd!r}")
        if cmd == "command -v sudo":
            return _fail(cmd, "")
        if cmd in ("command -v su", "command -v timeout", "stat -c %s /dev/null"):
            return _ok(cmd)
        if cmd == "md5sum < /dev/null":
            return _ok(cmd)
        if cmd == "timeout 1 true":
            return _fail(cmd, "timeout: invalid number '1'", code=1)
        if cmd == "timeout -t 1 true":
            return _ok(cmd)
        if cmd == 'test -n "$BASH_VERSION"':
            return _fail(cmd, "", code=1)
        if cmd in ("echo aGk= | base64 -d", "echo aGk= | base64 --decode"):
            if not self.has_base64:
                return _fail(cmd, "sh: base64: not found")
            return _ok(cmd, "hi") if cmd.endswith("-d") else _fail(cmd, "base64: bad option", 1)
        return self._file_op(cmd)

    def _file_op(self, cmd: str) -> CommandResult:
        """The two commands under test: ``base64 <path>`` and ``… | base64 -d > <path>``."""
        if cmd.startswith("base64 "):
            path = cmd.split(" ", 1)[1].strip("'")
            if not self.has_base64:
                return _fail(cmd, "sh: base64: not found")
            if path not in self.files:
                return _fail(cmd, f"base64: {path}: No such file or directory", code=1)
            return _ok(cmd, b64.b64encode(self.files[path].encode()).decode())
        if "| base64 -d " in cmd:
            payload, _, tail = cmd.partition("| base64 -d ")
            redirect, path = tail.split(" ", 1)
            path = path.strip("'")
            # The redirect is opened by the shell BEFORE the command is
            # resolved, so an overwrite empties the destination even when
            # nothing can decode into it. Measured on the 1.16.1 artifact; see
            # the record's `measured_on`.
            if redirect == ">":
                self.files[path] = ""
            if not self.has_base64:
                return _fail(cmd, "sh: base64: not found")
            decoded = b64.b64decode(payload.split()[1]).decode()
            self.files[path] = decoded if redirect == ">" else self.files.get(path, "") + decoded
            return _ok(cmd)
        raise AssertionError(f"the scripted device was asked something unmodelled: {cmd!r}")


class _Host(UnixHost):
    """A real ``UnixHost`` — real ``PosixFileOps``, real ``_userland()`` — on a fake wire.

    ``_exec_one`` is the family hook ``BaseHost.exec`` delegates to ("do not
    override ``exec``"), and it is what ``UnixHost._userland()``'s probe runner
    reaches too, so one script answers both the probe round and the file op it
    gates. A plain subclass rather than a dataclass one, which is what gives it
    a ``__dict__`` to hang the script on — the host itself is
    ``@dataclass(slots=True)``.
    """

    device: _Device

    async def _exec_one(
        self,
        cmd: str,
        timeout: float = 0.0,
        log: LogMode = LogMode.NORMAL,
        user: str | None = None,
    ) -> CommandResult:
        del user
        return self.device.answer(cmd)


def _host(device: _Device, host_id: str = "bb-1") -> _Host:
    host = _Host(ip="10.0.0.9", element=host_id, creds=[], user="admin", log=LogMode.QUIET)
    host.device = device
    return host


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


def _file_ops_commands(device: _Device) -> list[str]:
    """Everything the device was asked that was NOT a userland probe."""
    return [c for c in device.commands if _PATH in c or "hostname" in c]


# ===========================================================================
# The premise: the codec really is hard-coded
# ===========================================================================


class TestTheHardCodedCodecIsWhyThisGuardExists:
    """If this fails, the guard's reason has gone away — re-derive it.

    Every refusal below is justified by these two methods being unable to
    ADAPT. The day they read ``Userland.base64_flag`` for the spelling, a
    device with a different one stops being refusable and this whole module
    needs rewriting rather than repairing.
    """

    @pytest.mark.asyncio
    async def test_the_read_command_is_a_bare_base64_whatever_was_resolved(self) -> None:
        device = _Device(has_base64=True)
        host = _host(device)
        assert await host.read_file(_PATH) == "device-hostname\n"
        assert _file_ops_commands(device) == [_READ_CMD], (
            "read_file no longer emits one fixed `base64 <path>`; if it now adapts, "
            "the file-ops-base64 refusal is refusing something that could work"
        )

    @pytest.mark.asyncio
    async def test_the_write_command_is_a_fixed_base64_dash_d(self) -> None:
        device = _Device(has_base64=True)
        host = _host(device)
        result = await host.write_file(_PATH, "new\n")
        assert result.status is Status.Success
        assert device.files[_PATH] == "new\n"
        (sent,) = _file_ops_commands(device)
        assert "| base64 -d > " in sent, (
            f"write_file no longer emits the fixed `-d` decode spelling: {sent!r}"
        )


# ===========================================================================
# The guard's own contract
# ===========================================================================


class TestTheGuardKeysOnASettledAbsence:
    """Measured or declared absence refuses; an unanswered probe round does not."""

    @pytest.mark.asyncio
    async def test_a_declared_absent_is_refused(self) -> None:
        """A declaration is settled by definition — the operator asserted it."""
        userland = Userland(UserlandOptions(base64_flag="absent"), _unreachable_runner)
        with pytest.raises(UnsupportedOnUserlandError):
            await refuse_if_base64_is_absent(userland)

    @pytest.mark.asyncio
    async def test_a_measured_absence_is_refused(self) -> None:
        device = _Device(has_base64=False)
        userland = Userland(UserlandOptions(), _runner_for(device))
        with pytest.raises(UnsupportedOnUserlandError):
            await refuse_if_base64_is_absent(userland)
        assert userland.is_settled("base64_flag"), (
            "the device answered both decode probes, so this row has to be a "
            "measurement — otherwise the refusal above came from the assumed default"
        )

    @pytest.mark.asyncio
    async def test_a_usable_flag_is_not_refused(self) -> None:
        device = _Device(has_base64=True)
        userland = Userland(UserlandOptions(), _runner_for(device))
        await refuse_if_base64_is_absent(userland)
        assert userland.base64_flag == "-d"

    @pytest.mark.asyncio
    async def test_a_probe_round_that_never_arrived_is_not_refused(self) -> None:
        """THE discriminator against the obvious predicate.

        ``base64_flag`` reads ``"absent"`` here too, from
        ``_UNASKABLE_DEFAULTS``, and refusing on it would convert an sshd that
        would not answer into a verdict about the device's applets — otto
        deciding a host has no ``base64`` having never got a reply. The
        operation is left to fail with the transport's own error instead, which
        is what it did before this guard existed.
        """
        device = _Device(has_base64=True, unreachable=True)
        userland = Userland(UserlandOptions(), _runner_for(device))
        await refuse_if_base64_is_absent(userland)
        assert userland.base64_flag == "absent", "this row cannot discriminate anything"
        assert not userland.is_settled("base64_flag")

    @pytest.mark.asyncio
    async def test_a_host_with_no_resolver_at_all_is_not_refused(self) -> None:
        await refuse_if_base64_is_absent(None)

    @pytest.mark.asyncio
    async def test_the_guard_resolves_what_it_reads(self) -> None:
        """The predicate is not free, and this is the assertion that says so.

        A guard that read the properties without resolving would raise
        ``RuntimeError`` from ``Userland._get`` on every call; one that
        resolved nothing and returned would be a no-op. The device is asked.
        """
        device = _Device(has_base64=False)
        userland = Userland(UserlandOptions(), _runner_for(device))
        with pytest.raises(UnsupportedOnUserlandError):
            await refuse_if_base64_is_absent(userland)
        assert "echo aGk= | base64 -d" in device.commands


class TestTheMessageComesFromTheRecord:
    """Rendered from ``GAPS``, not hand-written here or at the call site."""

    @pytest.mark.asyncio
    async def test_the_message_is_the_records_own(self) -> None:
        gap = gap_for(SURFACE)
        assert gap is not None
        assert gap.status == MEASURED_BROKEN
        userland = Userland(UserlandOptions(base64_flag="absent"), _unreachable_runner)
        with pytest.raises(UnsupportedOnUserlandError) as excinfo:
            await refuse_if_base64_is_absent(userland)
        message = str(excinfo.value)
        assert SURFACE in message
        assert gap.reason in message
        assert gap.measured_on in message
        assert gap.docs_anchor in message

    @pytest.mark.asyncio
    async def test_the_message_names_the_host_and_what_was_attempted(self) -> None:
        userland = Userland(UserlandOptions(base64_flag="absent"), _unreachable_runner)
        with pytest.raises(UnsupportedOnUserlandError) as excinfo:
            await refuse_if_base64_is_absent(
                userland, host="edge-router-3", attempted="read_file('/etc/hostname')"
            )
        message = str(excinfo.value)
        assert "edge-router-3" in message
        assert "read_file('/etc/hostname')" in message

    @pytest.mark.asyncio
    async def test_flipping_the_record_to_untested_stops_the_refusal(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The TABLE decides, not this module.

        The caller decides only that the host is in the measured class. If the
        refusal survived the downgrade it would be a hard-coded block wearing
        the registry's message.
        """
        _downgraded(monkeypatch)
        userland = Userland(UserlandOptions(base64_flag="absent"), _unreachable_runner)
        await refuse_if_base64_is_absent(userland)


# ===========================================================================
# Reachability of the read half
# ===========================================================================


class TestReadFileArrivesAtTheGuard:
    """Driven through a real ``UnixHost.read_file``, not through the guard."""

    @pytest.mark.asyncio
    async def test_a_device_with_no_base64_is_refused_before_anything_is_read(self) -> None:
        device = _Device(has_base64=False)
        host = _host(device)
        with pytest.raises(UnsupportedOnUserlandError) as excinfo:
            await host.read_file(_PATH)
        assert "read_file" in str(excinfo.value)
        assert _file_ops_commands(device) == [], (
            "the refusal has to land BEFORE the read: nothing was attempted is the "
            "one thing UnsupportedOnUserlandError means"
        )

    @pytest.mark.asyncio
    async def test_the_refusal_replaced_a_file_not_found_error_naming_a_present_file(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """What the same device did before, on the same path, in this suite.

        This is the reachability proof and the "what it replaces" claim in one
        assertion: with the record downgraded the guard steps aside, the read is
        actually issued, and the caller gets a ``FileNotFoundError`` for a file
        the device HAS.
        """
        _downgraded(monkeypatch)
        device = _Device(has_base64=False)
        host = _host(device)
        with pytest.raises(FileNotFoundError) as excinfo:
            await host.read_file(_PATH)
        message = str(excinfo.value)
        assert _PATH in message
        assert "base64: not found" in message, (
            "the device blamed the missing applet and read_file re-attributed it to "
            "the path — that re-attribution is the defect the refusal removes"
        )
        assert _PATH in device.files, "this test is only a lie if the file is present"
        assert _file_ops_commands(device) == [_READ_CMD]

    @pytest.mark.asyncio
    async def test_a_device_with_base64_reads_normally(self) -> None:
        """The expensive mistake is refusing something that works."""
        device = _Device(has_base64=True)
        host = _host(device)
        assert await host.read_file(_PATH) == "device-hostname\n"

    @pytest.mark.asyncio
    async def test_the_probe_round_is_paid_once_for_the_host_not_once_per_call(self) -> None:
        """The cost claim in the guard's docstring, as an assertion.

        ``UnixHost._userland()`` caches one resolver per host object and
        ``resolve()`` is idempotent once everything is settled, so the second
        ``read_file`` puts nothing on the wire but the read itself. Without
        that, every file op on every host would pay a probe round.
        """
        device = _Device(has_base64=True)
        host = _host(device)
        await host.read_file(_PATH)
        first = list(device.commands)
        assert len(first) > 1, "the first call is supposed to resolve; this device answered"
        device.commands.clear()
        await host.read_file(_PATH)
        assert device.commands == [_READ_CMD], (
            f"the second read re-issued probes: {device.commands}"
        )


# ===========================================================================
# Reachability of the write half
# ===========================================================================


class TestWriteFileArrivesAtTheGuard:
    """The destructive half — refusing leaves the destination as it was found."""

    @pytest.mark.asyncio
    async def test_a_device_with_no_base64_is_refused_and_the_file_is_untouched(self) -> None:
        device = _Device(has_base64=False)
        host = _host(device)
        with pytest.raises(UnsupportedOnUserlandError) as excinfo:
            await host.write_file(_PATH, "replacement\n")
        assert "write_file" in str(excinfo.value)
        assert _file_ops_commands(device) == []
        assert device.files[_PATH] == "device-hostname\n", (
            "the destination was modified by a refused write"
        )

    @pytest.mark.asyncio
    async def test_the_refusal_replaced_a_truncated_destination(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """What the same device did before: a non-ok Result and an empty file.

        The shell opens the redirect before it resolves ``base64``, so the old
        path did not merely fail — it destroyed the file it was pointed at.
        Measured on the 1.16.1 artifact (see the record's ``measured_on``);
        reproduced by ``_Device`` so the call site's behaviour is observable.
        """
        _downgraded(monkeypatch)
        device = _Device(has_base64=False)
        host = _host(device)
        result = await host.write_file(_PATH, "replacement\n")
        assert result.status is not Status.Success
        assert "base64: not found" in result.msg
        assert device.files[_PATH] == "", (
            "the pre-guard path is supposed to have emptied the destination; if it no "
            "longer does, this test's claim about what the refusal prevents is stale"
        )

    @pytest.mark.asyncio
    async def test_an_append_is_refused_on_the_same_terms(self) -> None:
        """``>>`` does not truncate, and is still refused: it would write nothing."""
        device = _Device(has_base64=False)
        host = _host(device)
        with pytest.raises(UnsupportedOnUserlandError) as excinfo:
            await host.write_file(_PATH, "more\n", append=True)
        assert "append=True" in str(excinfo.value), (
            "the operator has to be able to tell which call was refused"
        )
        assert device.files[_PATH] == "device-hostname\n"

    @pytest.mark.asyncio
    async def test_a_device_with_base64_writes_normally(self) -> None:
        device = _Device(has_base64=True)
        host = _host(device)
        assert (await host.write_file(_PATH, "replacement\n")).status is Status.Success
        assert device.files[_PATH] == "replacement\n"


# ===========================================================================
# The other two families that mix this in
# ===========================================================================


class TestTheFamiliesWithNoResolver:
    """``LocalHost`` and ``DockerContainerHost`` mix in ``PosixFileOps`` and have no ``Userland``.

    Not refused, and not by omission: nothing has been measured about the
    userland of a host that never probes one, and "we were not told" must not
    become "does not work". What they DO instead is stated here rather than
    left as a gap in coverage — the docker container case is the interesting
    one, since an alpine image really is a BusyBox userland and nothing here
    will refuse it.
    """

    def test_the_hook_is_declared_once_and_only_unix_host_overrides_it(self) -> None:
        """Both mixins read the same hook; two definitions would make MRO decide.

        A re-declared identical default on either mixin passes every behavioural
        test in this file and reds this one.
        """
        assert PosixFileOps._userland is UserlandHost._userland
        assert PosixPrivilege._userland is UserlandHost._userland
        assert LocalHost._userland is UserlandHost._userland
        assert DockerContainerHost._userland is UserlandHost._userland
        assert UnixHost._userland is not UserlandHost._userland

    @pytest.mark.asyncio
    async def test_local_host_still_round_trips_a_real_file(self, tmp_path: Path) -> None:
        """The guard is on ``LocalHost``'s path too, and must be inert there.

        A crash on the ``None`` arm would show up as an ``AttributeError`` from
        every local file op, which is what makes this more than a smoke test.
        """
        host = LocalHost()
        assert host._userland() is None
        target = tmp_path / "note.txt"
        assert (await host.write_file(target, "hello\n")).status is Status.Success
        assert await host.read_file(target) == "hello\n"


# ===========================================================================
# Why those two paths are still OPEN, rather than closed in passing
# ===========================================================================


# Every capability the runner below settles: all of them except the applet
# batch, which it answers with empty stdout and which is therefore discarded.
# DERIVED from `UserlandOptions` rather than counted, because the count is not
# what the assertion is about -- "the round really answered" is -- and a hand
# written number reds on an eighth capability for a reason that has nothing to
# do with this test.
_EVERY_NON_APPLET_CAPABILITY = {
    f.name
    for f in dataclasses.fields(UserlandOptions)
    if f.name != "version" and f.name not in {applet_capability(a) for a in PROBED_APPLETS}
}


def _applets(*present: str):
    """A probe runner whose ELEVATION applets are exactly *present*.

    Everything else answers the way ``alpine:3.20`` measurably does (BusyBox
    1.36.1: ``base64``, ``timeout`` and ``stat -c`` all work, and there is no
    ``bash``), so every capability settles and the three arms below differ only
    in the one thing under test. Answering "no" to a probe is a MEASUREMENT
    here, not a refusal — ``_ok``/``_fail`` both return a result, and a runner
    that raised would leave the capability unsettled instead.
    """

    async def run(cmd: str, **_kwargs: object) -> CommandResult:
        if cmd in ("command -v sudo", "command -v su"):
            return _ok(cmd) if cmd.rsplit(maxsplit=1)[-1] in present else _fail(cmd, "")
        if cmd == 'test -n "$BASH_VERSION"':
            return _fail(cmd, "", code=1)
        return _ok(cmd, "hi" if "base64" in cmd else "")

    return run


class _WiredLocalHost(LocalHost):
    """A ``LocalHost`` given the resolver it does not have, to measure what that changes.

    A plain subclass rather than a dataclass one, for the same reason ``_Host``
    above is: ``LocalHost`` is ``@dataclass(slots=True)``, so there is nowhere to
    hang the resolver on an instance otherwise. Production cannot hand these
    tests this state at all — which is the point, and why it is built here rather
    than shipped.
    """

    userland: Userland

    def _userland(self) -> Userland:
        return self.userland


class TestWhyTheseTwoPathsAreStillOpen:
    """The coupling that keeps them open, asserted rather than argued.

    ``_userland()`` is declared on :class:`~otto.host.userland.UserlandHost` and
    read by TWO mixins, so a resolver added to close this surface also decides
    how ``run(sudo=True)`` elevates on the same host — and ``resolve()`` has no
    scoped form, so there is no way to take the one without the other. These
    tests are what makes that a measurement instead of a paragraph: they arrive
    at ``_elevate`` on a host wired the way closing the path would wire it, and
    show the built command moving.

    They are also the discriminator for whether the recorded reason is still
    true. If a later change makes elevation resolver-independent, or gives
    ``resolve()`` a scoped form, these go red — and the ``PATH_OPEN`` details on
    ``file-ops-base64`` should be revisited rather than re-worded.
    """

    @pytest.mark.asyncio
    async def test_a_resolver_here_moves_the_elevation_mechanism(self) -> None:
        """The alpine shape: ``su`` and no ``sudo``, measured on ``alpine:3.20``."""
        plain = LocalHost(log=LogMode.QUIET)
        assert plain._elevate("id -u") == ("sudo -S -p 'otto-sudo:' id -u", []), (
            "the premise: today this family builds sudo whatever the device has"
        )

        wired = _WiredLocalHost(log=LogMode.QUIET)
        wired.userland = Userland(UserlandOptions(), _applets("su"))
        await wired._prepare_elevation()
        assert set(wired.userland.as_lab_json()) == _EVERY_NON_APPLET_CAPABILITY, (
            "`as_lab_json` reports only SETTLED keys, so this is the premise that the "
            "round really answered rather than falling back to its cannot-ask defaults — "
            "and that the whole round is what `base64_flag` alone would have cost"
        )
        assert wired.userland.elevation == "su"
        # No password expect: neither of these two families has a `creds` field,
        # so `_switch_creds()` is empty and nothing answers `su`'s prompt.
        assert wired._elevate("id -u") == ("su -c 'id -u'", [])

    @pytest.mark.asyncio
    async def test_a_resolver_here_can_turn_a_non_ok_result_into_a_raise(self) -> None:
        """The third arm, and the one that changes the TYPE of the failure.

        A device with neither applet resolves ``elevation='none'``, which
        ``_elevate`` refuses outright. Today the same call builds ``sudo`` and
        the caller gets a non-ok ``CommandResult`` to check; wired, it raises
        before anything is sent. That is the arm this surface's paths cannot
        close in passing.
        """
        wired = _WiredLocalHost(log=LogMode.QUIET)
        wired.userland = Userland(UserlandOptions(), _applets())
        await wired._prepare_elevation()
        assert wired.userland.elevation == "none"
        with pytest.raises(UnsupportedOnUserlandError, match="no elevation mechanism"):
            wired._elevate("id -u")

    @pytest.mark.asyncio
    async def test_the_probes_would_not_run_in_the_shell_local_commands_run_in(self) -> None:
        """``LocalHost``'s own extra reason, and it is structural, not this box's.

        A resolver's probes go through ``exec``, which is
        ``loop.subprocess_shell`` — ``/bin/sh``. Commands go through ``run``,
        which is a persistent ``bash`` (``LocalSession``). ``$0`` is asserted
        rather than ``$BASH_VERSION`` deliberately: it names the shell each path
        INVOKES, which is fixed by otto's own code, so this states the split on
        a machine whose ``/bin/sh`` happens to be bash too — where a
        ``BASH_VERSION`` comparison would pass by agreeing.
        """
        host = LocalHost(log=LogMode.QUIET)
        try:
            probe_shell = (await host.exec('echo "$0"')).value.strip()
            command_shell = (await host.run('echo "$0"')).only.value.strip()
        finally:
            # `run` opens the persistent session; the fd watermark fixture fails
            # the test if it is left open.
            await host.close()
        assert probe_shell == "/bin/sh"
        assert command_shell == "bash"
        assert probe_shell != command_shell, (
            "a resolver on this class would describe the shell its probes ran in, which "
            "is not the shell `run()` executes the caller's commands in"
        )

    @pytest.mark.asyncio
    async def test_a_round_in_a_non_bash_sh_denies_the_bash_this_class_declares(self) -> None:
        """The consequence of that split, INJECTED rather than inherited.

        The runner answers the way a non-bash ``/bin/sh`` does, so this measures
        the outcome on any machine — including one whose ``/bin/sh`` IS bash,
        where relying on the ambient shell would make the test inert. Nothing
        consumes ``shell_dialect`` yet, which is the only thing keeping this
        latent; ``Userland.resolve``'s debug line already offers the value as a
        pasteable ``lab.json`` pin.
        """
        wired = _WiredLocalHost(log=LogMode.QUIET)
        wired.userland = Userland(UserlandOptions(), _applets("sudo", "su"))
        await wired._prepare_elevation()
        assert wired.has_bash is True, "the class declares bash, and it is right: run() uses it"
        assert wired.userland.shell_dialect == "ash"
        assert wired.userland.is_settled("shell_dialect"), (
            "a MEASURED contradiction, not an unasked default — an assumed value would be "
            "`bash` and there would be nothing to report"
        )


# ===========================================================================
# Runners for the guard-level tests
# ===========================================================================


async def _unreachable_runner(cmd: str, **_kwargs: object) -> CommandResult:
    """A device that answers nothing. Used where the value is DECLARED.

    Declared values skip the probe entirely, so a runner that would fail loudly
    is the right one: it proves the declaration was read rather than measured.
    """
    raise HostUnreachableError(f"no channel for {cmd!r}")


def _runner_for(device: _Device):
    async def run(cmd: str, **_kwargs: object) -> CommandResult:
        return device.answer(cmd)

    return run
