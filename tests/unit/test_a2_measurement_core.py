"""The A2 driver's safety core, on local shells: nothing here reaches a lab host."""

import os
import shutil
from collections.abc import AsyncIterator
from pathlib import Path

import pytest
import pytest_asyncio

from tests.measurements import a2_core as core
from tests.measurements.a2_lines import PtyLine

SHELLS = [
    pytest.param(["bash", "--norc", "--noprofile", "-i"], id="bash"),
    pytest.param(["dash", "-i"], id="dash"),
    pytest.param(["busybox", "sh", "-i"], id="busybox"),
]


@pytest_asyncio.fixture
async def cell(request: pytest.FixtureRequest, tmp_path: Path) -> AsyncIterator[core.Cell]:
    argv: list[str] = request.param
    if shutil.which(argv[0]) is None:
        pytest.skip(f"{argv[0]} is not installed")
    env = {
        "PATH": os.environ["PATH"],
        "HOME": str(tmp_path),
        "TERM": "dumb",
        "PS1": "$ ",
        "ENV": "",
        "X": "found",
    }
    line = PtyLine.spawn(argv, env=env, cwd=tmp_path)
    try:
        yield core.Cell(name=request.node.name, line=line)
    finally:
        line.close()


def test_the_asked_line_cannot_match_its_own_echo() -> None:
    typed = core.asked("true", "abcd1234")
    assert core._status_pattern("abcd1234").search(typed) is None


@pytest.mark.asyncio
@pytest.mark.parametrize("cell", SHELLS, indirect=True)
async def test_ask_reads_the_status_and_survives_errexit(cell: core.Cell) -> None:
    assert (await core.ask(cell, "set -e")).status == 0
    assert (await core.ask(cell, "false")).status == 1
    assert (await core.ask(cell, "(exit 7)")).status == 7
    assert (await core.ask(cell, "echo still-here # a comment")).status == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("cell", SHELLS, indirect=True)
async def test_the_snapshot_changes_nothing(cell: core.Cell) -> None:
    first = await core.take_snapshot(cell)
    second = await core.take_snapshot(cell)
    assert core.differences(first, second) == []
    assert first.variables["X"] == core.Variable(is_set=True, value="found", exported=True)
    assert first.variables["OTTO_CF_HOOK"].is_set is False
    assert first.tty != "none"


# The probe a plan review used against an earlier reset recipe: it left `C`
# on, lost an existing `X` and changed `OLDPWD`.
MUTATIONS = (
    "set -C; set -u; set -f; X=changed; export OTTO_CF_HOOK=ran; unset OLDPWD; "
    "cd /tmp; cd /; __otto_e=e; trap 'echo int' INT; stty -icanon </dev/tty"
)


@pytest.mark.asyncio
@pytest.mark.parametrize("cell", SHELLS, indirect=True)
async def test_a_row_s_mutations_are_all_put_back(cell: core.Cell, tmp_path: Path) -> None:
    await core.ask(cell, f"cd {tmp_path}; cd {tmp_path}")  # a known OLDPWD to lose
    before = await core.take_snapshot(cell)

    async def body() -> None:
        await core.ask(cell, MUTATIONS)
        changed = core.differences(before, await core.take_snapshot(cell))
        for field in (
            "X",
            "OTTO_CF_HOOK",
            "PWD",
            "OLDPWD",
            "__otto_e",
            "options",
            "int_trap",
            "tty",
        ):
            assert field in changed, field

    await core.run_row(cell, "mutate", body, deadline_s=20)
    assert cell.uncertain is None
    assert core.differences(before, await core.take_snapshot(cell)) == []


@pytest.mark.asyncio
@pytest.mark.parametrize("cell", SHELLS, indirect=True)
async def test_an_exported_variable_that_was_not_exported_loses_the_flag(cell: core.Cell) -> None:
    await core.ask(cell, "OTTO_CF_RUN=plain")
    before = await core.take_snapshot(cell)
    assert before.variables["OTTO_CF_RUN"].exported is False

    async def body() -> None:
        await core.ask(cell, "export OTTO_CF_RUN")

    await core.run_row(cell, "export", body, deadline_s=20)
    after = await core.take_snapshot(cell)
    assert after.variables["OTTO_CF_RUN"] == core.Variable(True, "plain", False)


@pytest.mark.asyncio
@pytest.mark.parametrize("cell", SHELLS, indirect=True)
async def test_a_row_past_its_deadline_is_interrupted_and_restored(cell: core.Cell) -> None:
    before = await core.take_snapshot(cell)

    async def body() -> None:
        await core.ask(cell, "X=late; sleep 30", timeout=60)

    async def interrupt() -> None:
        await cell.line.send("\x03")

    await core.run_row(cell, "slow", body, deadline_s=0.5, on_timeout=interrupt)
    assert [r.outcome for r in cell.records if r.outcome == "TIMED OUT"] == ["TIMED OUT"]
    assert cell.uncertain is None
    assert core.differences(before, await core.take_snapshot(cell)) == []


@pytest.mark.asyncio
@pytest.mark.parametrize("cell", SHELLS, indirect=True)
async def test_a_restore_that_cannot_finish_makes_the_cell_uncertain(
    cell: core.Cell, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(core, "CLEANUP_BOUND_S", 0.3)
    monkeypatch.setattr(core, "READ_TIMEOUT_S", 5.0)

    async def body() -> None:
        # Leaves the shell inside `cat`: the restore's snapshot gets no answer.
        await cell.line.send("cat\n")

    await core.run_row(cell, "stuck", body, deadline_s=5)
    assert cell.uncertain == "cleanup did not finish within its bound"
    with pytest.raises(core.CellUncertainError):
        await core.ask(cell, "true")

    typed_before = len(cell.records)
    await core.run_row(cell, "next", body, deadline_s=5)
    assert cell.records[typed_before:][0].outcome.startswith("SKIPPED")


@pytest.mark.asyncio
@pytest.mark.parametrize("cell", SHELLS, indirect=True)
async def test_exit_is_typed_only_into_an_identified_nested_shell(cell: core.Cell) -> None:
    outer = await core.take_snapshot(cell)
    shell = "sh"
    await cell.line.send(f"{shell}\n")
    inner = await core.take_snapshot(cell)
    assert inner.pid != outer.pid

    await core.leave_nested(cell, outer=outer, entry=frozenset({shell}))
    assert cell.uncertain is None
    assert (await core.take_snapshot(cell)).pid == outer.pid


@pytest.mark.asyncio
@pytest.mark.parametrize("cell", SHELLS, indirect=True)
async def test_the_outer_shell_is_never_exited(cell: core.Cell) -> None:
    outer = await core.take_snapshot(cell)
    await core.leave_nested(cell, outer=outer, entry=frozenset({"sh"}))
    assert cell.uncertain is not None
    assert "outer shell itself" in cell.uncertain
    assert [r for r in cell.records if r.typed == repr("exit\n")] == []


@pytest.mark.asyncio
@pytest.mark.parametrize("cell", SHELLS, indirect=True)
async def test_an_unrecorded_intermediary_is_ambiguous_and_gets_no_exit(cell: core.Cell) -> None:
    outer = await core.take_snapshot(cell)
    # An `sh -c` that stays as the parent of the inner shell: an intermediary
    # the row did not record (it recorded "su").
    await cell.line.send("sh -c 'sh -i; :'\n")
    inner = await core.take_snapshot(cell)
    assert inner.pid != outer.pid

    await core.leave_nested(cell, outer=outer, entry=frozenset({"su"}))
    assert cell.uncertain is not None
    assert cell.uncertain.startswith("RESET FAILED")
    assert [r for r in cell.records if r.typed == repr("exit\n")] == []


def test_ancestry_rules() -> None:
    entry = frozenset({"su"})
    direct = "A2A:20:sh:10:\nA2A:10:bash:1:\n"
    via_su = "A2A:30:sh:20:\nA2A:20:su:10:\nA2A:10:bash:1:\n"
    via_other = "A2A:30:sh:20:\nA2A:20:screen:10:\nA2A:10:bash:1:\n"
    too_far = "A2A:50:sh:40:\nA2A:40:su:30:\nA2A:30:su:20:\nA2A:20:su:10:\nA2A:10:bash:1:\n"
    assert core.nested_identity(direct, outer_pid=10, entry=entry) is None
    assert core.nested_identity(via_su, outer_pid=10, entry=entry) is None
    assert "screen" in (core.nested_identity(via_other, outer_pid=10, entry=entry) or "")
    assert "not within" in (core.nested_identity(too_far, outer_pid=10, entry=entry) or "")
    assert "itself" in (core.nested_identity("A2A:10:bash:1:\n", outer_pid=10, entry=entry) or "")
    assert "no ancestry" in (core.nested_identity("", outer_pid=10, entry=entry) or "")
