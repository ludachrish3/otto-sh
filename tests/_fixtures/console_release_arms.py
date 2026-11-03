"""The #386 close arms: one Zephyr console released many times, judged by the guest's journal.

Zephyr's ``shell_telnet`` backend tears itself down when a send fails because
its client has gone (``shell_telnet: Failed to send …, shutting down``), and on
some guests that teardown leaves the console refusing every later connection.
The send fails whenever otto releases the line while the guest still has
output queued. otto drains a single-client console to quiet inside every
client ``close()``, so a release never lands mid-output. These arms are the
bed's evidence for that: each arm releases one guest the same way many times,
then reads the guest's own log lines from the hop's journal and counts the
signatures of a failed release.

The arms:

- **A** opens the default session, types a flood of output ahead
  (``help`` three times), waits for the first output to arrive, and closes at
  once while the rest is still in flight.
- **B** is the same setup, and it also requires the close itself to take at
  least the drain's quiet window. That is the evidence that the drain ran
  inside ``close()``. B is A's control: the bed diagnostic called the drain
  explicitly before closing, and the drain has since moved into ``close()``.
- **C** closes an idle console and re-opens it at once: a release must free
  the console's one client slot immediately.
- **D** runs a short command (``help``) to completion and closes right after,
  with only the trailing prompt in flight. That is the end of every ordinary
  bed test, and it is the shape that wedged a 3.7 guest before the drain.
  A ``help`` still running at the close is cancelled; the report counts those
  cycles, so drift in what D measures stays visible.
- **E** cancels a running command after its first output line reaches otto,
  then closes. A cycle whose command had already finished at the cancel is
  void: it measured D, not E. On the stock ``shell_module`` sample every
  command's output arrives in one burst, so every cycle is void there, and
  the caller reports that rather than a pass.

A cycle fails on any open, setup or close error, and the arm stops at the
first failed cycle: a clean journal from an arm that never set up its release
is not a pass. After the last cycle the arm opens the console once more, so
the last release gets a verdict too.

The host factory and the journal are injected, so the same logic runs on the
bed (``tests/integration/host/test_console_release_bed.py``) and against fakes
(``tests/unit/host/test_console_release_bed_logic.py``).
"""

import asyncio
import json
import re
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any, Protocol

from otto.logger.mode import LogMode

ARMS = ("A", "B", "C", "D", "E")

# Journal signatures counted on the guest's own unit, matched on the
# ANSI-stripped message. The first three are the failed-release signatures the
# arms assert on; the rest are reported beside them, for diagnosis.
FAULT_PATTERNS: dict[str, str] = {
    "failed_to_send": r"shell_telnet: Failed to send",
    "socket_error": r"Telnet socket( \d+)? error",
    "backend_init": r"Telnet shell backend initialized",
    "out_of_buffers": r"[Oo]ut of buffers",
    "already_connected": r"[Aa]lready connected",
    "fatal": r"Telnet fatal error|failed to restart server|FATAL|ASSERT",
}
ASSERTED_FAULTS = ("failed_to_send", "socket_error", "backend_init")
_ANSI = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]")

# A built-in that exits 0 on every Zephyr LTS (see
# tests/integration/host/test_embedded_host_integration.py).
HEALTH_CMD = "kernel uptime"

# Arm E's command. shell_module's `demo hexdump` prints `argc = N`, then an
# `argv[i]` line and a 16-byte hexdump line per argument: 60 seven-byte
# arguments give about 5.3 KB from one command line. 492 typed bytes stay under
# CONFIG_SHELL_CMD_BUFF_SIZE=512 and 62 argv entries under
# CONFIG_SHELL_ARGC_MAX=64, both set by the otto overlay every guest is built
# with. Letters and digits only, so no wildcard or quote handling is involved.
CANCEL_CMD = "demo hexdump " + " ".join(f"otto{n:03d}" for n in range(60))

# Arm E stops early once this many cycles in a row are all void: the guest
# cannot produce a mid-output cancel, and more cycles would measure D again.
E_ALL_VOID_STOP = 5


@dataclass(frozen=True)
class ArmConfig:
    """The arms' timings. The defaults are the bed diagnostic's."""

    cycles: int = 30
    flood_text: str = "help\n" * 3
    first_output_re: str = r"Please press|Available commands"
    first_output_timeout: float = 10.0
    cmd_timeout: float = 20.0
    short_cmd: str = "help"
    short_close_after: float = 0.15
    cancel_cmd: str = CANCEL_CMD
    # Pause between cycles, so a slot that frees a moment after its close is
    # not read as a wedge. Arm C measures the immediate re-open itself.
    settle: float = 2.0
    # Let journald catch up with the guest's stdout before the journal is read.
    journal_flush: float = 1.5
    # Last-resort bound on one cycle; otto's own deadlines normally end a
    # failing cycle well before it (a failed open costs about 35 s).
    cycle_guard: float = 90.0
    # Arm B: the shortest close that can have drained. A drain waits for at
    # least one quiet window of silence, so a faster close skipped it.
    min_drain_close: float = 0.0


@dataclass(frozen=True)
class JournalEntry:
    unit: str
    text: str


class Journal(Protocol):
    """The guest's journal on its hop, read from a mark."""

    def mark(self) -> str:
        """Return a position just before the arm (a journal cursor)."""
        ...

    def since(self, position: str) -> list[JournalEntry]:
        """Every entry for the guest's unit after *position*."""
        ...


def parse_journal_json(out: str) -> list[JournalEntry]:
    """Parse ``journalctl -o json`` output into entries, one JSON object per line.

    journald encodes a message that is not valid UTF-8 as a list of byte
    values, so that form is decoded too. A line that is not JSON is skipped.
    """
    entries: list[JournalEntry] = []
    for raw in out.splitlines():
        line = raw.strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
        except json.JSONDecodeError:
            continue
        msg = rec.get("MESSAGE", "")
        if isinstance(msg, list):
            msg = bytes(msg).decode(errors="replace")
        entries.append(
            JournalEntry(
                unit=str(rec.get("_SYSTEMD_UNIT", "?")), text=_ANSI.sub("", str(msg)).rstrip()
            )
        )
    return entries


def count_faults(entries: list[JournalEntry], unit: str) -> tuple[dict[str, int], list[str]]:
    """Count each fault signature on *unit* only, and return the matching lines."""
    counts = dict.fromkeys(FAULT_PATTERNS, 0)
    lines: list[str] = []
    for entry in entries:
        if entry.unit != unit:
            continue
        hit = False
        for key, pattern in FAULT_PATTERNS.items():
            if re.search(pattern, entry.text):
                counts[key] += 1
                hit = True
        if hit:
            lines.append(entry.text[:300])
    return counts, lines


@dataclass
class CycleRecord:
    cycle: int
    error: str | None = None
    void_reason: str | None = None
    close_s: float | None = None
    # The command was still running at the cancel (arms D and E). Arm D means a
    # command that runs to completion before the close; a count drifting up
    # says the guest got slower and D is measuring a cancel instead.
    cancelled_mid_run: bool = False


@dataclass
class ArmOutcome:
    guest: str
    arm: str
    planned: int
    cycles: list[CycleRecord] = field(default_factory=list)
    final_error: str | None = None
    faults: dict[str, int] = field(default_factory=dict)
    fault_lines: list[str] = field(default_factory=list)

    @property
    def judged(self) -> list[CycleRecord]:
        """The cycles that were not void."""
        return [c for c in self.cycles if c.void_reason is None]

    @property
    def all_void(self) -> bool:
        """Every cycle ran cleanly and none produced the release it set up (arm E)."""
        return bool(self.cycles) and not self.judged and not any(c.error for c in self.cycles)

    def failures(self) -> list[str]:
        """Every reason this arm fails; empty when it passes."""
        problems = [f"cycle {c.cycle + 1}: {c.error}" for c in self.cycles if c.error]
        if self.final_error:
            problems.append(f"re-open after the last cycle: {self.final_error}")
        problems.extend(
            f"{self.faults[key]} journal line(s) matching {key} on the guest's unit"
            for key in ASSERTED_FAULTS
            if self.faults.get(key)
        )
        return problems

    def report(self) -> str:
        """A failure message: what failed, the fault counts and the guest's lines."""
        lines = [
            (
                f"{self.guest} arm {self.arm}: {len(self.cycles)}/{self.planned} cycles, "
                f"{len(self.cycles) - len(self.judged)} void, "
                f"{sum(c.cancelled_mid_run for c in self.cycles)} cancelled mid-run"
            ),
            *(f"  - {p}" for p in self.failures()),
            f"  journal counts on the guest's unit: {self.faults}",
            *(f"  | {t}" for t in self.fault_lines[:40]),
        ]
        return "\n".join(lines)


def _err(exc: BaseException) -> str:
    text = f"{type(exc).__name__}: {exc}"
    return text if len(text) < 400 else text[:400] + "..."


HostFactory = Callable[[], Any]


async def _open(host: Any, cfg: ArmConfig) -> str | None:
    """Open the default session by running the health command; the error, or None."""
    try:
        res = (await host.run(HEALTH_CMD, timeout=cfg.cmd_timeout, log=LogMode.QUIET)).only
    except Exception as exc:  # noqa: BLE001 — every failure is the cycle's verdict
        return _err(exc)
    if not res.status.is_ok:
        value = str(res.value)[:200]
        return f"{HEALTH_CMD!r} returned {res.status} (retcode {res.retcode}): {value!r}"
    return None


async def _close(host: Any, rec: CycleRecord) -> None:
    """Close *host*, timing it; a close error fails the cycle unless an earlier one did."""
    t0 = time.monotonic()
    try:
        await host.close()
    except Exception as exc:  # noqa: BLE001 — a close error fails the cycle
        rec.error = rec.error or f"close: {_err(exc)}"
    rec.close_s = time.monotonic() - t0


async def _finish(task: "asyncio.Task[Any]") -> None:
    """Wait for *task* to end without letting its cancellation become ours."""
    await asyncio.wait({task})


async def _stop(task: "asyncio.Task[Any] | None") -> None:
    """Cancel a task the cycle spawned and wait for it, so none outlives its cycle.

    Runs in the cycle's ``finally``: a cycle cancelled by its guard, or one
    that raised, would otherwise leave its ``host.run`` task running against a
    host the same ``finally`` is about to close.
    """
    if task is not None and not task.done():
        task.cancel()
        await _finish(task)


async def flood_cycle(arm: str, build_host: HostFactory, i: int, cfg: ArmConfig) -> CycleRecord:
    """Arms A and B: output in flight, then the ordinary close."""
    rec = CycleRecord(cycle=i)
    host = build_host()
    try:
        opened = await _open(host, cfg)
        if opened is not None:
            rec.error = f"open: {opened}"
        else:
            await host.send(cfg.flood_text)
            # The setup's own assertion: the flood's OUTPUT (not its echo) has
            # started arriving, so the close below lands with output in flight.
            await host.expect(re.compile(cfg.first_output_re), timeout=cfg.first_output_timeout)
    except Exception as exc:  # noqa: BLE001 — a setup error fails the cycle
        rec.error = rec.error or f"setup (output in flight): {_err(exc)}"
    finally:
        await _close(host, rec)
    if (
        arm == "B"
        and rec.error is None
        and rec.close_s is not None
        and rec.close_s < cfg.min_drain_close
    ):
        rec.error = (
            f"close took {rec.close_s:.3f} s, under the drain's {cfg.min_drain_close} s quiet "
            "window, so close() released the console without draining it"
        )
    return rec


async def short_run_cycle(build_host: HostFactory, i: int, cfg: ArmConfig) -> CycleRecord:
    """Arm D: a short command, then an immediate close."""
    rec = CycleRecord(cycle=i)
    host = build_host()
    task: asyncio.Task[Any] | None = None
    try:
        opened = await _open(host, cfg)
        if opened is not None:
            rec.error = f"open: {opened}"
        else:
            task = asyncio.ensure_future(
                host.run(cfg.short_cmd, timeout=cfg.cmd_timeout, log=LogMode.QUIET)
            )
            await asyncio.sleep(cfg.short_close_after)
            rec.cancelled_mid_run = not task.done()
            task.cancel()
            await _finish(task)
            if not task.cancelled():
                exc = task.exception()
                if exc is not None:
                    rec.error = f"setup ({cfg.short_cmd!r}): {_err(exc)}"
                elif not task.result().only.status.is_ok:
                    rec.error = f"setup: {cfg.short_cmd!r} returned {task.result().only.status}"
    finally:
        await _stop(task)
        await _close(host, rec)
    return rec


async def reopen_cycle(build_host: HostFactory, i: int, cfg: ArmConfig) -> CycleRecord:
    """Arm C: an idle close, then an immediate re-open."""
    rec = CycleRecord(cycle=i)
    host = build_host()
    try:
        opened = await _open(host, cfg)
        if opened is not None:
            rec.error = f"open: {opened}"
    finally:
        await _close(host, rec)
    if rec.error is not None:
        return rec
    again = build_host()
    try:
        reopened = await _open(again, cfg)
        if reopened is not None:
            rec.error = f"immediate re-open: {reopened}"
    finally:
        await _close(again, rec)
    return rec


async def cancel_cycle(build_host: HostFactory, i: int, cfg: ArmConfig) -> CycleRecord:
    """Arm E: cancel a running command after its first output line, then close.

    The first output line is seen through the session manager's output sink,
    which ``run_cmd`` feeds line by line with the echo and otto's markers
    already filtered out, so it is the command's own output.
    """
    rec = CycleRecord(cycle=i)
    host = build_host()
    task: asyncio.Task[Any] | None = None
    waiter: asyncio.Task[Any] | None = None
    try:
        # Private on purpose: the manager's output sink is the one place a
        # command's streamed output is observable while the command runs. The
        # tap goes on this host's manager only, before the open, so the health
        # command proves the tap is live; the host is closed below.
        seen: list[str] = []
        first = asyncio.Event()
        mgr = host._session_mgr
        sink = mgr._log_output

        def tap(line: str, mode: LogMode) -> None:
            seen.append(line)
            first.set()
            sink(line, mode)

        mgr._log_output = tap
        opened = await _open(host, cfg)
        if opened is not None:
            rec.error = f"open: {opened}"
            return rec
        if not seen:
            # Without this, a refactor that stops feeding the manager's sink
            # would void every cycle, and the arm would report "one burst".
            rec.error = (
                f"setup: the output tap saw none of {HEALTH_CMD!r}'s output, so the "
                "session manager's _log_output no longer carries a command's streamed "
                "output and this arm cannot see a first output line"
            )
            return rec
        first.clear()
        task = asyncio.ensure_future(
            host.run(cfg.cancel_cmd, timeout=cfg.cmd_timeout, log=LogMode.QUIET)
        )
        waiter = asyncio.ensure_future(first.wait())
        await asyncio.wait(
            {task, waiter}, timeout=cfg.first_output_timeout, return_when=asyncio.FIRST_COMPLETED
        )
        done_before = task.done()
        task.cancel()
        await _finish(task)
        rec.cancelled_mid_run = not done_before and task.cancelled()
        if not task.cancelled() and task.exception() is not None:
            rec.error = f"setup ({cfg.cancel_cmd[:24]}…): {_err(task.exception())}"
        elif not rec.cancelled_mid_run:
            # Only a run still going at cancel() that then ended cancelled
            # measures a mid-output cancel.
            rec.void_reason = "run completed before cancel"
            if not task.cancelled() and not task.result().only.status.is_ok:
                rec.error = f"setup: the cancel command returned {task.result().only.status}"
        elif not first.is_set():
            rec.void_reason = "no command output reached otto before the cancel"
    except Exception as exc:  # noqa: BLE001 — a setup error fails the cycle
        rec.error = rec.error or f"setup: {_err(exc)}"
    finally:
        await _stop(waiter)
        await _stop(task)
        await _close(host, rec)
    return rec


def _cycle_for(
    arm: str, build_host: HostFactory, cfg: ArmConfig
) -> Callable[[int], Awaitable[CycleRecord]]:
    if arm in ("A", "B"):
        return lambda i: flood_cycle(arm, build_host, i, cfg)
    if arm == "C":
        return lambda i: reopen_cycle(build_host, i, cfg)
    if arm == "D":
        return lambda i: short_run_cycle(build_host, i, cfg)
    if arm == "E":
        return lambda i: cancel_cycle(build_host, i, cfg)
    raise ValueError(f"unknown arm {arm!r}; choose from {ARMS}")


async def run_arm(
    arm: str,
    *,
    guest: str,
    unit: str,
    build_host: HostFactory,
    journal: Journal,
    cfg: ArmConfig,
) -> ArmOutcome:
    """Run *arm*'s cycles against one guest, then judge its journal slice."""
    cycle = _cycle_for(arm, build_host, cfg)
    outcome = ArmOutcome(guest=guest, arm=arm, planned=cfg.cycles)
    position = journal.mark()
    # A fixed run of work cycles paced by the settle sleep, stopping early on
    # a failed cycle, not a poll of a state (the rule's note names the shape).
    # ast-grep-ignore: no-handrolled-deadline-poll
    for i in range(cfg.cycles):
        try:
            rec = await asyncio.wait_for(cycle(i), cfg.cycle_guard)
        except asyncio.TimeoutError:
            rec = CycleRecord(cycle=i, error=f"cycle did not finish within {cfg.cycle_guard} s")
        outcome.cycles.append(rec)
        if rec.error is not None:
            break
        if arm == "E" and i + 1 >= E_ALL_VOID_STOP and not outcome.judged:
            break
        await asyncio.sleep(cfg.settle)
    if not any(c.error for c in outcome.cycles):
        host = build_host()
        final = CycleRecord(cycle=cfg.cycles)
        try:
            final.error = await _open(host, cfg)
        finally:
            await _close(host, final)
        outcome.final_error = final.error
    await asyncio.sleep(cfg.journal_flush)
    outcome.faults, outcome.fault_lines = count_faults(journal.since(position), unit)
    return outcome
