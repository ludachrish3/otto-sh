"""The A2 bed measurement for the exec frame (#935): the driver's two entries.

``dry_run`` types every row into local shells on a pty, with a fake ``sudo``
on ``PATH``: nothing leaves this machine. It is what the tests run.

``measure_bed`` types the same rows into the lab's guests. It is reached
ONLY from the command line with both ``--bed`` and ``--i-hold-the-lease``
(the operator takes the bed lease first, ``scripts/bed_lease.py``); no test
and no import calls it. It writes no file on a guest (sudo keeps its own
timestamp record and, on a login's first use, its lecture record), switches
no user, attaches no tracer and never touches a Zephyr guest. What the
brief asks for and this driver leaves out is listed in :data:`NOT_MEASURED`
and written beside the results.

Raw output: one file per line measured, every typed line and every buffer
as ``repr``, with known passwords struck out.
"""

import argparse
import asyncio
import contextlib
import dataclasses
import getpass
import sys
from collections.abc import AsyncIterator
from pathlib import Path

from pydantic import SecretStr

from tests._fixtures.fake_elevation import install_fake_elevation
from tests.measurements import a2_core as core
from tests.measurements import a2_steps as steps
from tests.measurements.a2_lines import PtyLine

OUT_DIR = Path("tests/measurements/a2_exec_frame")

WRONG_PASSWORD = SecretStr("otto-a2-not-the-password")

DRY_SHELLS = {
    "bash": ["bash", "--norc", "--noprofile", "-i"],
    "dash": ["dash", "-i"],
    "busybox": ["busybox", "sh", "-i"],
}


def write_records(cell: core.Cell, out_dir: Path) -> Path:
    """Write *cell*'s raw records to ``<out_dir>/<cell name>.txt`` and return the path."""
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"{cell.name.replace('/', '_').replace(':', '_').replace(' ', '_')}.txt"
    lines = [f"# {cell.name}", f"# uncertain: {cell.uncertain}", ""]
    for record in cell.records:
        lines += [
            f"## {record.row}: {record.outcome}",
            f"typed  {record.typed}",
            f"read   {record.buffer}",
            "",
        ]
    path.write_text("\n".join(lines))
    return path


def write_findings(name: str, findings: object, out_dir: Path) -> None:
    """Write one line's findings (a dataclass or a dict) beside its raw records."""
    out_dir.mkdir(parents=True, exist_ok=True)
    body = dataclasses.asdict(findings) if dataclasses.is_dataclass(findings) else findings  # type: ignore[arg-type]
    rows = body.items() if isinstance(body, dict) else [("value", body)]
    (out_dir / f"{name}.findings.txt").write_text("".join(f"{k}: {v}\n" for k, v in rows))


# --- the dry run -------------------------------------------------------------


def install_fakes(root: Path, *, password: str, keeps_timestamp: bool) -> "dict[str, str]":
    """Put the stand-in ``sudo``, ``su`` and ``id`` under *root*; return the shell's environment."""
    return {
        **install_fake_elevation(root, password=password, keeps_timestamp=keeps_timestamp),
        "HOME": str(root),
        "TERM": "dumb",
        "PS1": "$ ",
        "ENV": "",
    }


@contextlib.asynccontextmanager
async def dry_cell(
    shell: str, root: Path, *, password: SecretStr, keeps_timestamp: bool
) -> AsyncIterator[core.Cell]:
    """A local *shell* on a pty with the fakes on its ``PATH``, as a cell."""
    env = install_fakes(root, password=password.get_secret_value(), keeps_timestamp=keeps_timestamp)
    line = PtyLine.spawn(DRY_SHELLS[shell], env=env, cwd=root)
    try:
        kind = "timestamp" if keeps_timestamp else "no-timestamp"
        yield core.Cell(
            name=f"dry-run {shell} {kind}", line=line, secrets=[password, WRONG_PASSWORD]
        )
    finally:
        line.close()


@dataclasses.dataclass(slots=True)
class DryRun:
    """What one dry-run cell produced: the policy read, the sudo rows, the console rows."""

    cell: core.Cell
    policy: "steps.PolicyClass | None"
    sudo: steps.SudoFindings
    console: "dict[str, str]"
    start: core.Snapshot
    end: "core.Snapshot | None"


async def dry_run_one(shell: str, root: Path, out_dir: Path, *, keeps_timestamp: bool) -> DryRun:
    """Every row on one local shell, start to end, with its raw records written."""
    password = SecretStr("dry-run-password")
    async with dry_cell(shell, root, password=password, keeps_timestamp=keeps_timestamp) as cell:
        start = await core.take_snapshot(cell)
        policy = await steps.inventory(cell, password, login=getpass.getuser())
        sudo = await steps.sudo_rows(cell, password, WRONG_PASSWORD)
        console = await steps.console_rows(cell)
        end = await core.take_snapshot(cell) if cell.uncertain is None else None
        write_records(cell, out_dir)
        write_findings(cell.name.replace(" ", "_") + ".sudo", sudo, out_dir)
        write_findings(cell.name.replace(" ", "_") + ".console", console, out_dir)
        return DryRun(cell, policy, sudo, console, start, end)


# --- the bed -----------------------------------------------------------------

GNU_GUESTS = ("test1", "test2", "test3", "test4", "ubu2404")
CONSOLES = ("test2", "bb1350")
SUDO_TERMS = ("telnet", "ssh")
"""The terms a sudo policy is read on; the rows run on the first a guest offers."""

NOT_MEASURED = {
    "step 2, test2:console inventory": (
        "no password is typed at a console in this visit; the policy is read on the "
        "guest's telnet and ssh lines"
    ),
    "step 3, process cost by strace": (
        "the driver attaches no tracer to a guest's shell; the pid delta is recorded "
        "and marked 'not an exact count'"
    ),
    "step 3, candidates with `su - test` active": (
        "the driver switches no user; a child of the console's shell inherits its "
        "effective user by construction"
    ),
    "step 5, exec-frame costs under strace": "no tracer is attached to a guest",
    "step 5, the restore line after a Ctrl-C on the bed": (
        "the driver interrupts nothing on a guest; the product's own path types that "
        "line and the bed conformance run exercises it"
    ),
    "su with a password": "no device witness: every su host logs in as root",
    "an uncertain console cell's state": (
        "nothing is typed into an uncertain cell, so a console row that timed out leaves "
        "what it set (the directory, `set -f`, `-icanon`, the OTTO_CF_* exports, possibly a "
        "nested shell) until that console's login ends; the cell's record names the row"
    ),
    "an uncertain cell's `sudo -k`": (
        "nothing is typed into an uncertain cell; its session is closed, which ends a "
        "tty-scoped timestamp. Under a global or ppid timestamp the record can outlive it"
    ),
}
"""What the task's brief lists and this visit does not measure, each with why."""


def secrets_for(login: str, password: SecretStr) -> "list[SecretStr]":
    """The strings struck out of a cell's records: its password, unless it is the login name.

    A lab login whose password IS its name (a committed fixture value) would
    have every mention of the user struck from the evidence, so that one is
    left readable.
    """
    return [WRONG_PASSWORD] if password.get_secret_value() == login else [password, WRONG_PASSWORD]


async def measure_bed(out_dir: Path) -> None:
    """Steps 2-4 on the lab. Reached only from :func:`main` with both guard flags."""
    from tests._fixtures.labdata import host_data
    from tests.conformance._bed import BED_TECH, bed_topology

    topology = bed_topology()

    def cell_for(element: str, term: str):
        for bed_cell in topology:
            if bed_cell.cell.element == element and bed_cell.cell.term == term:
                return bed_cell
        raise LookupError(f"the bed topology has no {element}:{term} cell")

    write_findings("not-measured", NOT_MEASURED, out_dir)
    # The rows run once per policy class AND login, on the first line that
    # shows the pair: so one account on one guest sees at most one wrong
    # password in the whole visit.
    classes: "dict[str, list[str]]" = {}
    # A login whose policy could not be read on one line (its lab password
    # refused, say) is not tried on the guest's other lines: each try could
    # be one more failed attempt on the same account.
    unread: "dict[str, str]" = {}
    for element in GNU_GUESTS:
        offered = {c.cell.term for c in topology if c.cell.element == element}
        if not offered:
            unread[element] = "not measured: the bed topology has no cell for this guest"
            continue
        for term in (t for t in SUDO_TERMS if t in offered):
            for cred in host_data(element, BED_TECH)["creds"]:
                login, password = cred["login"], SecretStr(cred["password"])
                name = f"{element}:{term}:{login}"
                if f"{element}:{login}" in unread:
                    unread[name] = f"not tried: unread on {unread[f'{element}:{login}']}"
                    continue
                try:
                    async with (
                        cell_for(element, term).open_host_as(login) as host,
                        await host.open_session("a2") as session,
                    ):
                        cell = core.Cell(
                            name=name, line=session, secrets=secrets_for(login, password)
                        )
                        try:
                            policy = await steps.inventory(cell, password, login=login)
                            if policy is None:
                                unread[f"{element}:{login}"] = name
                                continue
                            key = f"{policy} login={login}"
                            if key not in classes:
                                sudo = await steps.sudo_rows(cell, password, WRONG_PASSWORD)
                                write_findings(name.replace(":", "_") + ".sudo", sudo, out_dir)
                            classes.setdefault(key, []).append(name)
                        finally:
                            write_records(cell, out_dir)
                except Exception as exc:  # noqa: BLE001 - one line's failure is recorded, the rest are measured
                    # Not tried on the guest's other lines either: a refused
                    # login is a failed attempt on the account too.
                    unread[f"{element}:{login}"] = name
                    unread[name] = f"FAILED: {type(exc).__name__}: {exc}"
    write_findings("policy-classes", classes, out_dir)

    for element in CONSOLES:
        try:
            async with cell_for(element, "console").open_host() as host:
                cell = core.Cell(name=f"{element}:console", line=host)
                try:
                    console = await steps.console_rows(cell)
                    write_findings(f"{element}_console.console", console, out_dir)
                finally:
                    write_records(cell, out_dir)
        except Exception as exc:  # noqa: BLE001, PERF203 - recorded; the other console is still measured
            unread[f"{element}:console"] = f"FAILED: {type(exc).__name__}: {exc}"
    write_findings("policy-unread", unread, out_dir)


def main(argv: "list[str] | None" = None) -> int:
    """Run the dry run (the default) or, with both guard flags, the lab measurement."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bed", action="store_true", help="measure the lab's guests")
    parser.add_argument(
        "--i-hold-the-lease",
        action="store_true",
        help="confirm the bed lease is held for this run (scripts/bed_lease.py acquire)",
    )
    parser.add_argument("--out", type=Path, default=OUT_DIR)
    args = parser.parse_args(argv)
    if args.bed != args.i_hold_the_lease:
        parser.error("the lab is measured only with BOTH --bed and --i-hold-the-lease")
    if args.bed:
        asyncio.run(measure_bed(args.out))
        return 0

    async def every_shell() -> None:
        import shutil
        import tempfile

        for shell, shell_argv in DRY_SHELLS.items():
            if shutil.which(shell_argv[0]) is None:
                continue
            for keeps in (True, False):
                with tempfile.TemporaryDirectory(prefix="otto-a2-") as root:
                    await dry_run_one(
                        shell, Path(root), args.out / "dry-run", keeps_timestamp=keeps
                    )

    asyncio.run(every_shell())
    return 0


if __name__ == "__main__":
    sys.exit(main())
