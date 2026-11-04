"""``render_status``: the table and the exit-code answer, off one ProjectStatus."""

import pytest

from otto.params import OptionsSource
from otto.project import InstallState, ProjectStatus
from otto.project.render import STATE_ANSWERS, render_status
from otto.utils import Status


@pytest.mark.parametrize(
    ("state", "status"),
    [
        (InstallState.INSTALLED, Status.Success),
        (InstallState.UNINSTALLED, Status.Failed),
        (InstallState.PARTIAL, Status.Error),
    ],
)
@pytest.mark.asyncio
async def test_the_answer_is_the_state(state, status, capsys) -> None:
    report = ProjectStatus(overall=state, projects={"acme": state}, scoping={})
    result = await render_status(report, OptionsSource.from_kwargs({"full": False}))
    assert result.status is status
    assert result == STATE_ANSWERS[state]
    assert "acme" in capsys.readouterr().out


def _lines_naming(out: str, name: str) -> list[str]:
    return [line for line in out.splitlines() if line.startswith(name)]


def test_a_dependency_skipped_row_is_marked_in_the_full_scoping_table(capsys) -> None:
    """``--full`` lists a skipped project's verdict, and the row says why it was not walked."""
    from otto.project.render import _print_scoping
    from otto.project.state import ProjectScopeSummary

    walked = ProjectScopeSummary(applicable=True, applicable_labs=("bench",), universe=("h0",))
    skipped = ProjectScopeSummary(
        applicable=True, applicable_labs=("bench",), universe=("h1",), skipped=True
    )
    _print_scoping({"acme": walked, "core": skipped})
    out = capsys.readouterr().out
    assert "skipped (unmet dependencies)" in _lines_naming(out, "core")[0]
    assert "skipped" not in _lines_naming(out, "acme")[0]


@pytest.mark.asyncio
async def test_the_bare_table_never_lists_a_dependency_skipped_project(capsys) -> None:
    """Only ``--full`` shows a skipped project: the bare table accounts for the walked ones.

    The skipped project here is also lab-inactive, the one shape the bare table
    would otherwise print as "not applicable".
    """
    from otto.project.state import ProjectScopeSummary

    state = InstallState.INSTALLED
    ghost = ProjectScopeSummary(
        applicable=False, usable=False, loaded_labs=("bench",), skipped=True
    )
    report = ProjectStatus(overall=state, projects={"acme": state}, scoping={"ghost": ghost})
    await render_status(report, OptionsSource.from_kwargs({"full": False}))
    out = capsys.readouterr().out
    assert "acme" in out
    assert "ghost" not in out


def test_the_project_cleanliness_slug_is_project():
    from otto.project import CleanlinessKind

    assert CleanlinessKind.PROJECT.value == "project"


def test_the_scoping_table_folds_long_lab_and_host_ids_whole(capsys, monkeypatch) -> None:
    """#785 D3: a lab id or host id wider than its cell folds, never ends in "…"."""
    from otto.project.render import _print_scoping
    from otto.project.state import ProjectScopeSummary

    monkeypatch.setenv("COLUMNS", "60")
    lab = "l" * 90
    host = "h" * 90
    row = ProjectScopeSummary(applicable=True, applicable_labs=(lab,), universe=(host,))
    _print_scoping({"acme": row})
    out = capsys.readouterr().out
    assert "…" not in out
    heading, *lines = out.splitlines()
    assert heading == "fleet of interest"
    # Borderless, so read each column at the offsets its first line puts it.
    labs_at, hosts_at = lines[0].index("labs:"), lines[0].index("hosts:")
    assert "".join(ln[labs_at:hosts_at].strip() for ln in lines) == f"labs:{lab}"
    assert "".join(ln[hosts_at:].strip() for ln in lines) == f"hosts:{host}"


@pytest.mark.asyncio
async def test_a_project_name_prints_literally_in_the_status_table(capsys, monkeypatch) -> None:
    monkeypatch.setenv("COLUMNS", "60")
    state = InstallState.INSTALLED
    report = ProjectStatus(overall=state, projects={"[red]p": state}, scoping={})
    await render_status(report, OptionsSource.from_kwargs({"full": False}))
    assert "[red]p" in capsys.readouterr().out
