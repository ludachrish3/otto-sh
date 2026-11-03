"""Hostless smoke run of the #386 close arms (:mod:`tests._fixtures.console_release_arms`).

The bed test (``tests/integration/host/test_console_release_bed.py``) needs the
Zephyr guests and runs for about 20 minutes per guest, so its logic is proved
here first, against a behavioural fake host and journal: every arm passes on a
clean guest, each failed-release signature in the guest's journal fails it,
a setup or close error fails the cycle even when the journal is clean, and
arm E's void guard tells a burst guest from a real mid-output cancel.
"""

import asyncio
import json
from dataclasses import dataclass, field, replace
from types import SimpleNamespace
from typing import Any

import pytest

from otto.logger.mode import LogMode
from otto.utils import Status
from tests._fixtures.console_release_arms import (
    ASSERTED_FAULTS,
    CANCEL_CMD,
    E_ALL_VOID_STOP,
    HEALTH_CMD,
    ArmConfig,
    JournalEntry,
    count_faults,
    parse_journal_json,
    run_arm,
)

UNIT = "zephyr-qemu-v3_7_fat_ram.service"
OTHER_UNIT = "zephyr-qemu-v4_4_lfs.service"

FAST = ArmConfig(
    cycles=3,
    settle=0.0,
    journal_flush=0.0,
    short_close_after=0.01,
    first_output_timeout=1.0,
    cycle_guard=10.0,
)

_FAULT_LINES = {
    "failed_to_send": "<err> shell_telnet: Failed to send -128, shutting down",
    "socket_error": "<err> shell_telnet: Telnet socket 7 error (104)",
    "backend_init": "<inf> shell_telnet: Telnet shell backend initialized",
}


def _ok(value: str = "") -> Any:
    return SimpleNamespace(only=SimpleNamespace(status=Status.Success, retcode=0, value=value))


@dataclass
class FakeGuest:
    """How the fake console behaves, and what the arms did to it."""

    open_fails_on: set[int] = field(default_factory=set)  # build indexes whose open fails
    expect_raises: bool = False
    close_raises: bool = False
    close_delay: float = 0.0
    short_run: str = "complete"  # complete | slow | error | failed
    cancel_output: str = "burst"  # burst | slow | silent
    # The health command's output no longer reaches the manager's sink, as
    # after a refactor that stops feeding _log_output.
    sink_dead: bool = False
    running: int = 0  # host.run calls not yet ended
    built: int = 0
    closed: int = 0
    sent: list[str] = field(default_factory=list)
    sink_lines: list[str] = field(default_factory=list)

    def build(self) -> "FakeHost":
        host = FakeHost(self, self.built)
        self.built += 1
        return host


class FakeManager:
    def __init__(self, guest: FakeGuest) -> None:
        self._log_output = lambda line, _mode: guest.sink_lines.append(line)


class FakeHost:
    def __init__(self, guest: FakeGuest, index: int) -> None:
        self.guest = guest
        self.index = index
        self._session_mgr = FakeManager(guest)

    def _emit(self, line: str, log: LogMode) -> None:
        # The real manager hands run_cmd a sink built from its _log_output at
        # call time, so a tap installed before the run sees every line.
        self._session_mgr._log_output(line, log)

    async def run(self, cmd: str, timeout: float, log: LogMode) -> Any:
        self.guest.running += 1
        try:
            return await self._run(cmd, log)
        finally:
            self.guest.running -= 1

    async def _run(self, cmd: str, log: LogMode) -> Any:
        g = self.guest
        if cmd == HEALTH_CMD:
            if self.index in g.open_fails_on:
                raise ConnectionError("telnet shell never became ready")
            if not g.sink_dead:
                self._emit("12345", log)
            return _ok("12345")
        if cmd == CANCEL_CMD:
            if g.cancel_output == "silent":
                await asyncio.sleep(30)
            self._emit("argc = 61", log)
            if g.cancel_output == "slow":
                await asyncio.sleep(30)
            self._emit("argv[0]: demo hexdump", log)
            return _ok("argc = 61")
        if g.short_run == "error":
            raise ConnectionError("lost connection")
        if g.short_run == "failed":
            return SimpleNamespace(only=SimpleNamespace(status=Status.Failed, retcode=-8, value=""))
        if g.short_run == "slow":
            await asyncio.sleep(30)
        return _ok("Available commands:")

    async def send(self, text: str) -> None:
        self.guest.sent.append(text)

    async def expect(self, pattern: Any, timeout: float) -> str:
        if self.guest.expect_raises:
            raise asyncio.TimeoutError
        match = pattern.search("Please press the <Tab> button. Available commands:")
        assert match is not None
        return match.group(0)

    async def close(self) -> None:
        self.guest.closed += 1
        await asyncio.sleep(self.guest.close_delay)
        if self.guest.close_raises:
            raise OSError("close blew up")


@dataclass
class FakeJournal:
    entries: list[JournalEntry] = field(default_factory=list)
    marks: int = 0
    read_from: list[str] = field(default_factory=list)

    def mark(self) -> str:
        self.marks += 1
        return f"cursor-{self.marks}"

    def since(self, position: str) -> list[JournalEntry]:
        self.read_from.append(position)
        return list(self.entries)


async def _run(
    arm: str, guest: FakeGuest, journal: FakeJournal | None = None, cfg: ArmConfig = FAST
):
    journal = journal if journal is not None else FakeJournal()
    outcome = await run_arm(
        arm, guest="zephyr-3.7-fat", unit=UNIT, build_host=guest.build, journal=journal, cfg=cfg
    )
    return outcome, journal


@pytest.mark.parametrize("arm", ["A", "B", "C", "D"])
@pytest.mark.asyncio
async def test_clean_guest_passes_every_arm(arm: str) -> None:
    guest = FakeGuest()
    outcome, journal = await _run(arm, guest)
    assert outcome.failures() == []
    assert len(outcome.cycles) == FAST.cycles
    assert outcome.final_error is None
    # One journal slice, read from the mark taken before the first cycle.
    assert journal.read_from == ["cursor-1"]
    # Every host the arm built was closed, the final re-open's included.
    assert guest.closed == guest.built
    per_cycle = 2 if arm == "C" else 1
    assert guest.built == FAST.cycles * per_cycle + 1


@pytest.mark.parametrize("arm", ["A", "B"])
@pytest.mark.asyncio
async def test_flood_arms_type_the_flood_ahead(arm: str) -> None:
    guest = FakeGuest()
    await _run(arm, guest)
    assert guest.sent == [FAST.flood_text] * FAST.cycles


@pytest.mark.parametrize("fault", ASSERTED_FAULTS)
@pytest.mark.asyncio
async def test_a_failed_release_signature_on_the_guests_unit_fails_the_arm(fault: str) -> None:
    journal = FakeJournal(entries=[JournalEntry(UNIT, _FAULT_LINES[fault])])
    outcome, _ = await _run("A", FakeGuest(), journal)
    assert outcome.faults[fault] == 1
    assert any(fault in p for p in outcome.failures())
    assert _FAULT_LINES[fault][:30] in outcome.report()


@pytest.mark.asyncio
async def test_another_units_faults_are_not_this_guests_verdict() -> None:
    journal = FakeJournal(
        entries=[JournalEntry(OTHER_UNIT, line) for line in _FAULT_LINES.values()]
    )
    outcome, _ = await _run("A", FakeGuest(), journal)
    assert outcome.failures() == []


@pytest.mark.asyncio
async def test_a_setup_failure_fails_the_arm_even_with_a_clean_journal() -> None:
    guest = FakeGuest(expect_raises=True)
    outcome, journal = await _run("A", guest)
    failures = outcome.failures()
    assert len(failures) == 1
    assert "setup (output in flight)" in failures[0]
    # The arm stops at the failed cycle, skips the final re-open, still reads
    # the journal, and still closes the host.
    assert len(outcome.cycles) == 1
    assert outcome.final_error is None
    assert journal.read_from == ["cursor-1"]
    assert guest.closed == guest.built == 1


@pytest.mark.asyncio
async def test_an_open_failure_carries_the_wedge_signature() -> None:
    """The bed's wedge gate recognises a failure by "shell never became ready"."""
    outcome, _ = await _run("A", FakeGuest(open_fails_on={0}))
    assert "shell never became ready" in outcome.report()


@pytest.mark.parametrize("arm", ["A", "C", "D", "E"])
@pytest.mark.asyncio
async def test_a_close_error_fails_the_cycle(arm: str) -> None:
    outcome, _ = await _run(arm, FakeGuest(close_raises=True, cancel_output="slow"))
    assert outcome.failures()[0].startswith("cycle 1: close: OSError")


@pytest.mark.asyncio
async def test_arm_b_fails_a_close_that_cannot_have_drained() -> None:
    outcome, _ = await _run("B", FakeGuest(), cfg=replace(FAST, min_drain_close=5.0))
    assert "without draining" in outcome.failures()[0]


@pytest.mark.asyncio
async def test_arm_b_passes_a_close_that_lasted_the_quiet_window() -> None:
    guest = FakeGuest(close_delay=0.02)
    outcome, _ = await _run("B", guest, cfg=replace(FAST, min_drain_close=0.01))
    assert outcome.failures() == []


@pytest.mark.asyncio
async def test_arm_c_fails_when_the_immediate_reopen_fails() -> None:
    # Build 0 is cycle 1's first host, build 1 its immediate re-open.
    outcome, _ = await _run("C", FakeGuest(open_fails_on={1}))
    assert outcome.failures()[0].startswith("cycle 1: immediate re-open:")


@pytest.mark.asyncio
async def test_a_failed_reopen_after_the_last_cycle_fails_the_arm() -> None:
    outcome, _ = await _run("A", FakeGuest(open_fails_on={FAST.cycles}))
    assert outcome.failures() == [
        "re-open after the last cycle: ConnectionError: telnet shell never became ready"
    ]


@pytest.mark.parametrize("short_run", ["error", "failed"])
@pytest.mark.asyncio
async def test_arm_d_fails_when_its_short_command_does(short_run: str) -> None:
    outcome, _ = await _run("D", FakeGuest(short_run=short_run))
    assert outcome.failures()[0].startswith("cycle 1: setup")


@pytest.mark.asyncio
async def test_arm_d_accepts_a_command_still_running_at_the_close_and_counts_it() -> None:
    outcome, _ = await _run("D", FakeGuest(short_run="slow"))
    assert outcome.failures() == []
    assert all(c.cancelled_mid_run for c in outcome.cycles)
    assert f"{FAST.cycles} cancelled mid-run" in outcome.report()


@pytest.mark.asyncio
async def test_arm_d_counts_no_cancel_when_the_command_finished_first() -> None:
    outcome, _ = await _run("D", FakeGuest(short_run="complete"))
    assert not any(c.cancelled_mid_run for c in outcome.cycles)
    assert "0 cancelled mid-run" in outcome.report()


@pytest.mark.parametrize(
    ("arm", "behaviour"),
    [("D", {"short_run": "slow"}), ("E", {"cancel_output": "silent"})],
    ids=["D", "E"],
)
@pytest.mark.asyncio
async def test_a_guard_cancelled_cycle_leaves_no_run_task_behind(
    arm: str, behaviour: dict[str, str]
) -> None:
    """The cycle guard cancels the cycle while its spawned ``host.run`` is still going."""
    guest = FakeGuest(**behaviour)
    cfg = replace(FAST, cycles=1, cycle_guard=0.2, short_close_after=5.0, first_output_timeout=5.0)
    outcome, _ = await _run(arm, guest, cfg=cfg)
    assert outcome.failures()[0] == "cycle 1: cycle did not finish within 0.2 s"
    assert guest.running == 0
    assert guest.closed == guest.built


@pytest.mark.asyncio
async def test_arm_e_on_a_burst_guest_is_all_void_and_stops_early() -> None:
    guest = FakeGuest(cancel_output="burst")
    outcome, _ = await _run("E", guest, cfg=replace(FAST, cycles=30))
    assert len(outcome.cycles) == E_ALL_VOID_STOP
    assert {c.void_reason for c in outcome.cycles} == {"run completed before cancel"}
    assert outcome.all_void
    assert outcome.failures() == []
    # The tap forwards every line to the manager's own sink.
    assert guest.sink_lines.count("argc = 61") == E_ALL_VOID_STOP


@pytest.mark.asyncio
async def test_arm_e_judges_a_real_mid_output_cancel() -> None:
    outcome, _ = await _run("E", FakeGuest(cancel_output="slow"))
    assert len(outcome.judged) == FAST.cycles
    assert not outcome.all_void
    assert outcome.failures() == []


@pytest.mark.asyncio
async def test_arm_e_with_no_output_before_the_cancel_is_void() -> None:
    cfg = replace(FAST, first_output_timeout=0.05)
    outcome, _ = await _run("E", FakeGuest(cancel_output="silent"), cfg=cfg)
    assert {c.void_reason for c in outcome.cycles} == {
        "no command output reached otto before the cancel"
    }
    assert outcome.all_void


@pytest.mark.asyncio
async def test_arm_e_judges_the_journal_even_when_every_cycle_is_void() -> None:
    journal = FakeJournal(entries=[JournalEntry(UNIT, _FAULT_LINES["failed_to_send"])])
    outcome, _ = await _run("E", FakeGuest(cancel_output="burst"), journal)
    assert outcome.all_void
    assert outcome.failures() == ["1 journal line(s) matching failed_to_send on the guest's unit"]


@pytest.mark.asyncio
async def test_arm_e_fails_when_the_output_tap_sees_nothing() -> None:
    """A tap that stopped seeing output would void every cycle into an xfail."""
    guest = FakeGuest(sink_dead=True)
    outcome, _ = await _run("E", guest)
    assert not outcome.all_void
    assert "output tap saw none" in outcome.failures()[0]
    assert guest.closed == guest.built


@pytest.mark.asyncio
async def test_arm_e_open_failure_is_a_failure_not_a_void() -> None:
    outcome, _ = await _run("E", FakeGuest(open_fails_on={0}))
    assert not outcome.all_void
    assert outcome.failures()[0].startswith("cycle 1: open:")


@pytest.mark.asyncio
async def test_unknown_arm_is_refused() -> None:
    with pytest.raises(ValueError, match="unknown arm"):
        await _run("F", FakeGuest())


def test_parse_journal_json_strips_ansi_decodes_byte_messages_and_skips_junk() -> None:
    out = "\n".join(
        [
            json.dumps(
                {
                    "_SYSTEMD_UNIT": UNIT,
                    "MESSAGE": "\x1b[1;31m<err> shell_telnet: Failed to send -128\x1b[0m",
                }
            ),
            "not json",
            "",
            json.dumps(
                {"_SYSTEMD_UNIT": UNIT, "MESSAGE": list(b"Telnet socket 7 error (104)\xff")}
            ),
        ]
    )
    entries = parse_journal_json(out)
    assert [e.text for e in entries] == [
        "<err> shell_telnet: Failed to send -128",
        "Telnet socket 7 error (104)�",
    ]
    counts, lines = count_faults(entries, UNIT)
    assert counts["failed_to_send"] == 1
    assert counts["socket_error"] == 1
    assert len(lines) == 2
