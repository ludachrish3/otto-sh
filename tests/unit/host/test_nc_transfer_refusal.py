"""A netcat GET is refused on a device whose ``nc`` was measured to reject ``-N``.

The ``nc-transfer`` record in :data:`~otto.host.userland.GAPS` says why: otto
asks the device to send with ``nc -N <ip> <port>``, and ``-N`` is an OpenBSD
netcat option BusyBox's applet does not have. What came back instead was
nothing at all — the local server sat waiting for a peer whose ``nc`` had
already exited with ``unrecognized option: N``, and the stderr that said so was
redirected to ``/dev/null`` by the spawn itself.

**PRESENCE IS NOT THE PREDICATE HERE, and that is the whole difference from
``scp-transfer``.** ``nc`` is present on all five matrix artifacts; the applet's
OPTION SET is the gap. So the guard keys on
:attr:`~otto.host.userland.Userland.nc_dash_n` — an option-support capability in
the shape of ``timeout_style``, not of ``applet_scp`` — and
:class:`TestThePremiseIsAnOptionAndNotABinary` pins the facts that make that the
right shape.

**IT ALSO KEYS ON WHICH BINARY otto WOULD EXEC.**
:attr:`~otto.host.options.NcOptions.exec_name` lets an operator point this
backend at a real netcat installed alongside the applet, which is precisely the
workaround the record documents; refusing such a host would be a false refusal
of a host that works. The capability answers for the name
:data:`~otto.host.userland.NC_APPLET` alone, so the guard acts only while
``exec_name`` IS that name. :class:`TestTheRefusalIsScoped` holds both ends of
that.

**THE PUT DIRECTION IS NOT REFUSED**, and that asymmetry is deliberate rather
than an oversight: its spawn carries no ``-N`` at all. It is broken on BusyBox
for a different reason — the applet spells a listener ``-l -p PORT`` — and no
probe can settle that one without asking a device to bind a port.
:class:`TestThePutDirectionStaysOpen` pins the asymmetry and its premise, so a
``-N`` appearing in that spawn reds here.

**The scripted devices below reproduce a measurement; they are not the evidence
for it.** The evidence is in the record's ``measured_on``: all five matrix
artifacts answer ``rejected`` through this probe, and a real OpenBSD netcat
answers ``supported``.
"""

import dataclasses
import inspect
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock

import pytest

from otto.host import userland as userland_module
from otto.host.errors import UnsupportedOnUserlandError
from otto.host.options import NcOptions, UserlandOptions
from otto.host.transfer.nc import (
    NC_DASH_N_CAPABILITY,
    NcFileTransfer,
    refuse_if_nc_rejects_dash_n,
)
from otto.host.userland import (
    NC_APPLET,
    NC_DASH_N_ABSENT,
    NC_DASH_N_REJECTED,
    NC_DASH_N_SUPPORTED,
    UNTESTED,
    Gap,
    Userland,
    gap_for,
)
from otto.result import CommandResult
from otto.utils import Status

SURFACE = "nc-transfer"
"""The registry record this guard is the sixth product consumer of."""

_HOST = "bb-2"
"""The host name the backend is built with, so a message can be checked for it."""

_DIFFERENTIAL = '[ "$(nc 2>&1 </dev/null)" = "$(nc -N 2>&1 </dev/null)" ]'
"""The product's own option-support probe, spelled out rather than imported.

A FOURTH copy of a spelling that already has three (the product, Tier 1's
``test_applet_contracts.py``, and ``test_userland.py``'s pin) — and it is here
for the same reason as the others: a test that read the command from the module
under test would be scripting a device to answer whatever that module happens to
ask, which is not a device. What this file needs it for is narrower than the
pin: it is the one command whose answer decides the value these tests are about.
"""


def _ok(cmd: str, out: str = "") -> CommandResult:
    return CommandResult(status=Status.Success, value=out, command=cmd, retcode=0)


def _no(cmd: str) -> CommandResult:
    return CommandResult(status=Status.Error, value="", command=cmd, retcode=1)


async def _unreachable_runner(cmd: str, **_kwargs: object) -> CommandResult:
    """A runner that cannot answer, so only DECLARED capabilities settle."""
    raise OSError(f"no device in this test: {cmd!r}")


class _NcRunner:
    """A device that answers ONLY the two probes ``nc_dash_n`` is made of.

    Everything else is unanswerable, deliberately: the other capabilities then
    go unasked and stay unsettled, so a test that started depending on one of
    them would be depending on a guess — and this answer demonstrably came from
    a probe rather than from a declaration.
    """

    def __init__(self, *, present: bool = True, parses: bool = False) -> None:
        self.present = present
        self.parses = parses
        self.issued: list[str] = []

    async def __call__(self, cmd: str, **_kwargs: object) -> CommandResult:
        self.issued.append(cmd)
        if cmd == f"command -v {NC_APPLET}":
            return _ok(cmd, f"/bin/{NC_APPLET}") if self.present else _no(cmd)
        if cmd == _DIFFERENTIAL:
            return _ok(cmd) if self.parses else _no(cmd)
        raise OSError(f"the scripted device answers only the nc probes, not {cmd!r}")

    @property
    def nc_probes(self) -> "list[str]":
        wanted = {f"command -v {NC_APPLET}", _DIFFERENTIAL}
        return [c for c in self.issued if c in wanted]


async def _measured(*, present: bool = True, parses: bool = False) -> Userland:
    """A resolved ``Userland`` whose ``nc_dash_n`` came from a real probe round."""
    userland = Userland(UserlandOptions(), _NcRunner(present=present, parses=parses))
    await userland.resolve()
    return userland


async def _declaring(value: str) -> Userland:
    """A resolved ``Userland`` whose ``nc_dash_n`` was declared, never probed."""
    userland = Userland(UserlandOptions(nc_dash_n=value), _unreachable_runner)
    await userland.resolve()
    return userland


async def _unasked() -> Userland:
    """A resolved ``Userland`` that reached no device at all: nothing is settled."""
    userland = Userland(UserlandOptions(), _unreachable_runner)
    await userland.resolve()
    return userland


class _Connections:
    """A connection manager that fails loudly if anything asks it for anything.

    Every emitter below reaches this object before it can send: the plain GET
    warms the exec pool, the tunnelled GET forwards a port. So "nothing was
    attempted" is observable as this never being touched, which is a stronger
    claim than "no bytes moved".
    """

    term = "ssh"

    def __init__(self, *, has_tunnel: bool = False) -> None:
        self.has_tunnel = has_tunnel
        self.touched = 0

    async def ssh(self) -> Any:
        self.touched += 1
        raise AssertionError("the nc backend opened a connection; the refusal comes first")

    async def warm_exec_pool(self, _n: int) -> None:
        self.touched += 1
        raise AssertionError("the nc backend warmed its pool; the refusal comes first")


def _backend(
    userland: "Userland | None",
    *,
    exec_name: str = NC_APPLET,
    has_tunnel: bool = False,
) -> NcFileTransfer:
    """A real ``NcFileTransfer`` — real ``_run_get``/``_get_files_nc`` — on a dead wire."""

    async def _exploding_exec(cmd: str, **_kwargs: object) -> CommandResult:
        raise AssertionError(f"the nc backend ran {cmd!r}; the refusal comes first")

    return NcFileTransfer(
        connections=_Connections(has_tunnel=has_tunnel),  # type: ignore[arg-type]
        name=_HOST,
        transfer="nc",
        nc_options=NcOptions(exec_name=exec_name),
        get_local_ip=lambda: "10.0.0.1",
        exec_cmd=_exploding_exec,
        userland=userland,
    )


def _spy_on_the_emitters(backend: NcFileTransfer) -> "dict[str, AsyncMock]":
    """Replace what lies past the guard, so "did it get through" is a fact.

    The guard raising is only half the claim; the other half is that a device
    NOT in the measured class still reaches the emitter it came for. Both GET
    shapes are spied, so one backend can answer both questions with no wire.
    """
    spies = {
        "tunneled": AsyncMock(return_value={}),
        "warmup": AsyncMock(return_value=None),
        "gather": AsyncMock(return_value={}),
        # The size prefetch a GET runs before it spawns anything. Stubbed rather
        # than left to explode, so a test about what happens PAST the guard is
        # not stopped by the first control-plane command instead.
        "control": AsyncMock(return_value=_ok("stat -c %s", "12")),
    }
    backend._get_files_nc_tunneled = spies["tunneled"]  # type: ignore[method-assign]
    backend._warmup_for_transfer = spies["warmup"]  # type: ignore[method-assign]
    backend._gather_per_file = spies["gather"]  # type: ignore[method-assign]
    backend._control_run = spies["control"]  # type: ignore[method-assign]
    return spies


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


def _unasked_reads_as_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make the ``is_settled`` half of the predicate ABLE to fire.

    ``_UNASKABLE_DEFAULTS`` maps ``nc_dash_n`` to ``supported``, so an unasked
    round currently reads as "the option parses" and the VALUE half of the guard
    already declines to refuse — which would leave the ``is_settled`` half
    structurally redundant and its deletion unobservable. Flipping the default
    the other way is the mutation that makes it observable, and it is not
    hypothetical: ``rejected`` is what a table-wide "assume the worst" change
    would naturally choose, and an sshd at its ``MaxSessions`` ceiling — the very
    condition a bulk transfer's own fan-out creates — would then turn every
    working netcat host into a refused one.
    """
    monkeypatch.setitem(userland_module._UNASKABLE_DEFAULTS, "nc_dash_n", NC_DASH_N_REJECTED)


# ===========================================================================
# The premise: an OPTION, on a binary otto lets you rename
# ===========================================================================


class TestThePremiseIsAnOptionAndNotABinary:
    """Each fact the guard's shape rests on, asserted rather than described.

    Any of them can stop being true without another assertion in this file
    reddening, and each would make the guard the wrong shape in a different
    way.
    """

    def test_the_plain_get_asks_the_device_to_send_with_dash_n(self) -> None:
        spawn = _emitter_lines(NcFileTransfer._get_files_nc)
        assert any(" -N " in line for line in spawn), (
            "the plain GET no longer emits `-N`. That option IS this guard's predicate; "
            "if the spawn changed spelling, the measurement behind the refusal is about "
            "a command otto no longer sends."
        )

    def test_the_tunnelled_get_asks_the_device_to_listen_with_dash_n(self) -> None:
        spawn = _emitter_lines(NcFileTransfer._get_files_nc_tunneled)
        assert any("-Nl" in line for line in spawn), (
            "the tunnelled GET no longer emits `-Nl`. It is recorded PROTECTED by the "
            "plain GET's guard on the strength of needing the same option; without it, "
            "that path needs its own predicate rather than this one's."
        )

    def test_the_capability_answers_for_the_default_exec_name(self) -> None:
        """Otherwise the guard would be one nothing reaches by default.

        The probe asks about one name. If ``NcOptions``' default stopped being
        that name, every stock host would fall down the guard's own
        "not otto's binary" arm and the refusal would never fire.
        """
        assert NcOptions().exec_name == NC_APPLET

    def test_the_capability_key_is_one_the_module_actually_resolves(self) -> None:
        """A key outside the resolved set would be a guard that never fires."""
        assert NC_DASH_N_CAPABILITY in userland_module._UNASKABLE_DEFAULTS

    def test_the_option_the_probe_asks_about_is_the_option_the_backend_emits(self) -> None:
        """The two ends of the same claim, which live in different modules.

        ``Userland`` asks whether ``-N`` parses; ``transfer/nc.py`` emits
        ``-N``. Nothing but this couples them, and a probe drifting to another
        option would answer a question no call site asks.
        """
        probe = inspect.getsource(Userland._probe_nc_dash_n)
        assert f"{NC_APPLET} -N " in probe, "the probe no longer asks about `nc -N`"


def _emitter_lines(fn: object) -> "list[str]":
    """The lines of *fn* that build a command out of the configured netcat.

    Source-level, and scoped to the interpolation rather than to the whole
    function, so a comment mentioning an option is not mistaken for otto
    emitting one.
    """
    return [
        line.strip()
        for line in inspect.getsource(fn).splitlines()  # type: ignore[arg-type]
        if "self._nc_exec" in line
    ]


# ===========================================================================
# The guard keys on a SETTLED rejection
# ===========================================================================


class TestTheGuardKeysOnASettledRejection:
    """Both halves of the predicate, each with its own way of being wrong."""

    @pytest.mark.asyncio
    async def test_a_declared_rejection_is_refused(self) -> None:
        with pytest.raises(UnsupportedOnUserlandError):
            await refuse_if_nc_rejects_dash_n(
                await _declaring(NC_DASH_N_REJECTED), exec_name=NC_APPLET
            )

    @pytest.mark.asyncio
    async def test_a_measured_rejection_is_refused(self) -> None:
        """The arm every matrix row lands on: `nc` is there and `-N` does not parse."""
        userland = await _measured(present=True, parses=False)
        assert userland.nc_dash_n == NC_DASH_N_REJECTED
        with pytest.raises(UnsupportedOnUserlandError):
            await refuse_if_nc_rejects_dash_n(userland, exec_name=NC_APPLET)

    @pytest.mark.asyncio
    async def test_a_measured_acceptance_is_not_refused(self) -> None:
        """A real netcat answers the differential with equality, and transfers."""
        userland = await _measured(present=True, parses=True)
        assert userland.nc_dash_n == NC_DASH_N_SUPPORTED
        assert await refuse_if_nc_rejects_dash_n(userland, exec_name=NC_APPLET) is None

    @pytest.mark.asyncio
    async def test_a_device_with_no_netcat_at_all_is_not_refused_by_this_record(self) -> None:
        """``absent`` is a measurement, and it is NOT this record's measurement.

        This surface is about an applet's option set. A device with no netcat is
        a different fact — ``applet_nc``'s ``absent``, recorded and still
        unwired — and refusing from here would render an operator a message
        about a spelling for a device that has no binary to spell it with.
        """
        userland = await _measured(present=False)
        assert userland.nc_dash_n == NC_DASH_N_ABSENT
        assert await refuse_if_nc_rejects_dash_n(userland, exec_name=NC_APPLET) is None

    @pytest.mark.asyncio
    async def test_a_probe_round_that_never_arrived_is_not_refused(self) -> None:
        """The ``is_settled`` half, made ABLE to fire — see ``_unasked_reads_as_rejected``."""
        with pytest.MonkeyPatch.context() as mp:
            _unasked_reads_as_rejected(mp)
            userland = await _unasked()
            assert userland.nc_dash_n == NC_DASH_N_REJECTED
            assert not userland.is_settled(NC_DASH_N_CAPABILITY)
            assert await refuse_if_nc_rejects_dash_n(userland, exec_name=NC_APPLET) is None

    @pytest.mark.asyncio
    async def test_a_settled_rejection_is_still_refused_under_that_same_mutation(self) -> None:
        """The other half of the pair, so the flip above cannot pass by refusing nothing."""
        with pytest.MonkeyPatch.context() as mp:
            _unasked_reads_as_rejected(mp)
            with pytest.raises(UnsupportedOnUserlandError):
                await refuse_if_nc_rejects_dash_n(
                    await _measured(present=True, parses=False), exec_name=NC_APPLET
                )


# ===========================================================================
# What the guard must NOT refuse
# ===========================================================================


class TestTheRefusalIsScoped:
    """Every arm that returns quietly, because each is a host that works today."""

    @pytest.mark.asyncio
    async def test_a_host_pointed_at_another_netcat_is_not_refused(self) -> None:
        """The record's own workaround, and the expensive direction if broken.

        A BusyBox device with a real OpenBSD netcat installed alongside is fine
        by pointing ``exec_name`` at it. The capability answered about ``nc``;
        otto would exec ``ncat``. Refusing here would decline a transfer the
        device can serve.
        """
        userland = await _measured(present=True, parses=False)
        assert userland.nc_dash_n == NC_DASH_N_REJECTED
        assert await refuse_if_nc_rejects_dash_n(userland, exec_name="ncat") is None

    @pytest.mark.asyncio
    async def test_a_host_with_no_resolver_is_not_refused(self) -> None:
        """``_userland()`` answers ``None`` by default, and nothing was measured."""
        assert await refuse_if_nc_rejects_dash_n(None, exec_name=NC_APPLET) is None

    @pytest.mark.asyncio
    async def test_the_message_names_the_host_and_what_was_attempted(self) -> None:
        with pytest.raises(UnsupportedOnUserlandError) as raised:
            await refuse_if_nc_rejects_dash_n(
                await _declaring(NC_DASH_N_REJECTED),
                exec_name=NC_APPLET,
                host=_HOST,
                attempted="get of 3 file(s)",
            )
        assert _HOST in str(raised.value)
        assert "get of 3 file(s)" in str(raised.value)


class TestTheTableDecidesWhetherTheClassIsRefused:
    """The caller decides this host is in the measured class; the TABLE decides the rest."""

    @pytest.mark.asyncio
    async def test_flipping_the_record_to_untested_stops_the_refusal(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """What makes this WIRED rather than a refusal of its own authority.

        Nothing about the device changes here: the same userland, the same
        settled ``rejected``. Only the record's status moves, and the refusal
        goes with it — which is the property ``PATH_WIRED`` claims and
        ``PATH_PROBE_REFUSED`` explicitly does not.
        """
        userland = await _declaring(NC_DASH_N_REJECTED)
        _downgraded(monkeypatch)
        assert await refuse_if_nc_rejects_dash_n(userland, exec_name=NC_APPLET) is None


# ===========================================================================
# The call site: the GET direction, above the tunnel dispatch
# ===========================================================================


class TestGetArrivesAtTheGuard:
    """The real ``_run_get`` on a real backend, with the wire replaced by an assertion."""

    @pytest.mark.asyncio
    async def test_a_device_whose_nc_rejects_dash_n_is_refused_before_anything_is_spawned(
        self,
    ) -> None:
        backend = _backend(await _declaring(NC_DASH_N_REJECTED))
        spies = _spy_on_the_emitters(backend)

        with pytest.raises(UnsupportedOnUserlandError):
            await backend._run_get([Path("/tmp/a.bin")], Path("/tmp"), None)

        spies["warmup"].assert_not_awaited()
        spies["gather"].assert_not_awaited()
        assert backend._connections.touched == 0  # type: ignore[attr-defined]

    @pytest.mark.asyncio
    async def test_a_device_whose_nc_accepts_dash_n_still_gets_its_transfer(self) -> None:
        """The other half: the guard is a gate, not a wall."""
        backend = _backend(await _declaring(NC_DASH_N_SUPPORTED))
        spies = _spy_on_the_emitters(backend)

        await backend._run_get([Path("/tmp/a.bin")], Path("/tmp"), None)

        spies["gather"].assert_awaited_once()


class TestTheTunnelledGetIsProtectedByItsOnlyCaller:
    """``PATH_PROTECTED``: unreachable on the devices this record covers.

    The claim is not "this path is safe" in the abstract — it is that
    ``_get_files_nc`` refuses ABOVE its own ``has_tunnel`` dispatch, so the
    tunnelled emitter is never entered on a refused device. Both directions are
    asserted, because a protection that also holds on a working host would mean
    the path was simply dead.
    """

    @pytest.mark.asyncio
    async def test_the_tunnelled_path_is_never_entered_on_a_refused_device(self) -> None:
        backend = _backend(await _declaring(NC_DASH_N_REJECTED), has_tunnel=True)
        spies = _spy_on_the_emitters(backend)

        with pytest.raises(UnsupportedOnUserlandError):
            await backend._run_get([Path("/tmp/a.bin")], Path("/tmp"), None)

        spies["tunneled"].assert_not_awaited()

    @pytest.mark.asyncio
    async def test_the_tunnelled_path_is_still_reached_on_a_working_device(self) -> None:
        backend = _backend(await _declaring(NC_DASH_N_SUPPORTED), has_tunnel=True)
        spies = _spy_on_the_emitters(backend)

        await backend._run_get([Path("/tmp/a.bin")], Path("/tmp"), None)

        spies["tunneled"].assert_awaited_once()


class TestThePutDirectionStaysOpen:
    """The stated asymmetry, pinned so it cannot drift into an accident."""

    def test_the_put_spawn_carries_no_dash_n_to_key_on(self) -> None:
        """The premise of leaving it open. If it grows one, this record is wrong here."""
        spawn = _emitter_lines(NcFileTransfer._put_files_nc)
        assert spawn, "the put spawn no longer builds a command from `exec_name`"
        assert not any("-N" in line for line in spawn), (
            "the put listener now emits `-N`. It is recorded OPEN on the strength of "
            "NOT emitting it -- the `nc_dash_n` measurement would now cover this path, "
            "and leaving it unguarded would be a hole rather than a scoped decision."
        )

    @pytest.mark.asyncio
    async def test_a_refused_device_still_reaches_the_put_emitter(self) -> None:
        """Not an oversight: a netcat can reject `-N` and still serve `-l PORT`.

        Refusing the put on the `-N` answer would decline a transfer this
        measurement says nothing about. The path is recorded OPEN for exactly
        this reason, and a future guard here needs its own predicate.
        """
        backend = _backend(await _declaring(NC_DASH_N_REJECTED))
        put = AsyncMock(return_value={})
        backend._put_files_nc = put  # type: ignore[method-assign]

        await backend._run_put([Path("/tmp/a.bin")], Path("/tmp"), None)

        put.assert_awaited_once()


class TestTheGuardCostsNoRoundTripThisPathDidNotAlreadyPay:
    """``prepare()`` already resolves the userland on every transfer.

    So the guard's own ``await resolve()`` is idempotent by the time it runs,
    and the added cost of the REFUSAL is zero round trips — unlike
    ``refuse_if_scp_is_absent``, which added a resolution to a path that awaited
    none. What the CAPABILITY costs is two probes in the round, and that is
    charged once per host object rather than once per transfer.
    """

    @pytest.mark.asyncio
    async def test_a_second_transfer_issues_no_further_probe(self) -> None:
        runner = _NcRunner(present=True, parses=True)
        userland = Userland(UserlandOptions(), runner)
        backend = _backend(userland)
        _spy_on_the_emitters(backend)

        await backend._run_get([Path("/tmp/a.bin")], Path("/tmp"), None)
        first = list(runner.nc_probes)
        await backend._run_get([Path("/tmp/b.bin")], Path("/tmp"), None)

        assert first == [f"command -v {NC_APPLET}", _DIFFERENTIAL], (
            "the capability is two probes: presence, then whether the option parses"
        )
        assert runner.nc_probes == first, (
            "a second transfer re-probed the device; `Userland.resolve()` is supposed to "
            "be idempotent once the capability is settled"
        )

    def test_the_backend_still_builds_without_a_resolver(self) -> None:
        """The trap the scp guard hit: a guard must not become a build requirement.

        ``NcFileTransfer.create`` threads ``userland`` through rather than
        validating it. Making it mandatory would convert "this host cannot be
        guarded" into "this host cannot be BUILT" for any host whose
        ``_userland()`` hook answers ``None``.
        """
        source = inspect.getsource(NcFileTransfer.create)
        assert "ctx.userland is None" not in source, (
            "`create` now rejects a context with no resolver, which is a host that "
            "cannot be built rather than one that cannot be guarded"
        )
        assert _backend(None) is not None

    def test_nc_options_still_carries_the_override_this_guard_reads(self) -> None:
        """``exec_name`` is what scopes the refusal; losing it changes the predicate."""
        fields = {f.name for f in dataclasses.fields(NcOptions)}
        assert "exec_name" in fields
