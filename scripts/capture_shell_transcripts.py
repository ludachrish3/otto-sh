r"""Record the raw buffer each bed shell sends back for a framed command.

Run only through ``make shell-transcripts``, which holds the bed lease. Writes
one JSON file per ``(element, term)`` under
``tests/unit/host/shell_transcripts/``. Every string is JSON-escaped, so each
``\\r``, ``\\n`` and ``\\x1b`` the shell sent is visible.

The capture touches nothing under ``src/``: it wraps
``ShellSession._run_cmd_inner`` for the length of the run and, inside each
call, the live session's ``read_transport_until`` (the bytes as read, before
the session folds ``\\r\\n`` under ``onlcr``) and its frame's
``parse_output`` (the buffer the frame was handed). It restarts, reboots or
flashes no guest; a Zephyr console serves one client, so guests are opened
one at a time.
"""

import asyncio
import contextlib
import datetime as dt
import json
import sys
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

from otto.host.session import ShellSession
from otto.host.session_setup import SessionSetup
from otto.testing import EXACT_OUTPUT_CASES
from tests._fixtures.labdata import host_data, make_host
from tests._fixtures.session_setup_hooks import noop_session_setup
from tests._fixtures.shell_transcripts import ZEPHYR_EXTRA_COMMANDS
from tests.conformance._bed import BED_TECH, bed_topology

OUT = Path(__file__).resolve().parent.parent / "tests" / "unit" / "host" / "shell_transcripts"
ZEPHYR_COMMANDS = ["kernel version", "help", "kernel uptime", "otto_not_a_command", "device list"]
POSIX_COMMANDS = [case.command for case in EXACT_OUTPUT_CASES[:12]]
NOOP_HOOK = "shell-transcripts-noop"

_records: list[dict[str, Any]] = []


def _install_tee() -> None:
    original = ShellSession._run_cmd_inner  # noqa: SLF001 — the tee wraps the one read loop

    async def teed(self: ShellSession, cmd: str, *args: Any, **kwargs: Any) -> Any:
        record: dict[str, Any] = {"cmd": cmd, "raw": [], "buffer": None}
        frame = self._frame
        reader = self.read_transport_until
        parse = frame.parse_output

        async def read(*, pattern: Any) -> str:
            data = await reader(pattern=pattern)
            record["raw"].append(data)
            return data

        def parse_output(buffer: str, *, cmd: str, m: Any) -> str:
            if record["buffer"] is None:
                record["buffer"] = buffer
                record["markers"] = {"begin": m.begin, "end_prefix": m.end_prefix}
            return parse(buffer, cmd=cmd, m=m)

        self.read_transport_until = read  # type: ignore[method-assign]
        frame.parse_output = parse_output  # type: ignore[method-assign]
        try:
            return await original(self, cmd, *args, **kwargs)
        finally:
            del self.read_transport_until
            del frame.parse_output
            record["frame"] = frame.type_name
            record["terminal_mapping"] = self.terminal_mapping
            _records.append(record)

    ShellSession._run_cmd_inner = teed  # noqa: SLF001 # type: ignore[method-assign]


def _listed(help_value: str, command: str) -> bool:
    """Whether the ``help`` output *help_value* lists *command*'s first word as a command."""
    return f"\n  {command.split(maxsplit=1)[0]} " in f"\n{help_value}"


async def _capture(host: Any, commands: list[str], entry: str) -> list[dict[str, Any]]:
    out = []
    for cmd in commands:
        before = len(_records)
        call = host.exec if entry == "exec" else host.run
        result = await call(cmd, timeout=30)
        if entry == "run":
            result = result.only
        teed = _records[before:]
        rec = teed[-1] if teed else {}
        out.append(
            {
                "cmd": cmd,
                "entry": entry,
                "buffer": rec.get("buffer"),
                "raw": "".join(rec.get("raw", [])) if teed else None,
                "value": result.value,
                "retcode": result.retcode,
                "_meta": rec,
            }
        )
    return out


def _write(
    element: str, term: str, release: str, commands: list[dict[str, Any]], name: str
) -> None:
    metas = [c.pop("_meta") for c in commands]
    framed = [m for m in metas if m]
    doc = {
        "element": element,
        "term": term,
        "release": release,
        "frame": framed[0]["frame"] if framed else None,
        "terminal_mapping": framed[0]["terminal_mapping"] if framed else None,
        "captured": dt.datetime.now(tz=dt.timezone.utc).date().isoformat(),
        "markers": framed[-1].get("markers") if framed else None,
        "commands": commands,
    }
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / f"{name}.json").write_text(json.dumps(doc, indent=2) + "\n")
    print(f"wrote {name}.json ({len(framed)}/{len(commands)} framed)", flush=True)


def _release(element: str) -> str:
    data = host_data(element, BED_TECH)
    if data.get("os_type") == "zephyr":
        return f"zephyr-{data['os_version']}"
    if "sw_version" in data:
        return f"busybox-{data['sw_version']}"
    return "gnu-bash"


@contextlib.asynccontextmanager
async def _pooled_test1() -> AsyncIterator[Any]:
    with noop_session_setup(NOOP_HOOK) as hook:
        host = make_host("test1", term="ssh", session_setup=SessionSetup(name=hook))
        try:
            yield host
        finally:
            await host.close()


async def main() -> int:
    """Capture every cell, one at a time; a cell that fails is reported, not retried."""
    _install_tee()
    failed: list[str] = []
    cells = bed_topology()
    zephyr = [c for c in cells if c.kind == "bed-zephyr"]
    ash = next(c for c in cells if c.kind == "bed-busybox" and c.cell.term == "telnet")
    test1 = next(c for c in cells if c.cell.element == "test1" and c.cell.term == "ssh")

    for rc in [*zephyr, ash, test1]:
        el, term = rc.cell.element, rc.cell.term
        commands = ZEPHYR_COMMANDS if rc.kind == "bed-zephyr" else POSIX_COMMANDS
        try:
            async with rc.open_host() as host:
                got = await _capture(host, commands, "run")
                if rc.kind == "bed-zephyr":
                    helped = next(c["value"] for c in got if c["cmd"] == "help")
                    extra = [c for c in ZEPHYR_EXTRA_COMMANDS if _listed(helped, c)]
                    got += await _capture(host, extra, "run")
            _write(el, term, _release(el), got, f"{el}-{term}")
        except Exception as exc:  # noqa: BLE001 — record and move on, never retry
            print(f"FAILED {el}-{term}: {type(exc).__name__}: {exc}", flush=True)
            failed.append(f"{el}-{term}")

    try:
        async with _pooled_test1() as host:
            got = await _capture(host, POSIX_COMMANDS, "exec")
        _write("test1", "ssh", "gnu-bash", got, "test1-ssh-pooled")
    except Exception as exc:  # noqa: BLE001
        print(f"FAILED test1-ssh-pooled: {type(exc).__name__}: {exc}", flush=True)
        failed.append("test1-ssh-pooled")

    if failed:
        # A missing cell is a failed capture: the README's model claims every cell.
        print(f"capture failed for {len(failed)} cell(s): {', '.join(failed)}", file=sys.stderr)
        return 1
    print("every cell captured", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
