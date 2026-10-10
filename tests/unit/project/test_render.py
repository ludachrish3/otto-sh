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
    report = ProjectStatus(overall=state, repos={"acme": state}, scoping={})
    result = await render_status(report, OptionsSource.from_kwargs({"full": False}))
    assert result.status is status
    assert result == STATE_ANSWERS[state]
    assert "acme" in capsys.readouterr().out


def _lines_naming(out: str, name: str) -> list[str]:
    return [line for line in out.splitlines() if line.startswith(name)]


def test_a_dependency_skipped_row_is_marked_in_the_full_scoping_table(capsys) -> None:
    """``--full`` lists a skipped repo's verdict, and the row says why it was not walked."""
    from otto.project.render import _print_scoping
    from otto.project.state import RepoScope

    walked = RepoScope(applicable=True, applicable_labs=("bench",), universe=("h0",))
    skipped = RepoScope(applicable=True, applicable_labs=("bench",), universe=("h1",), skipped=True)
    _print_scoping({"acme": walked, "core": skipped})
    out = capsys.readouterr().out
    assert "skipped (unmet dependencies)" in _lines_naming(out, "core")[0]
    assert "skipped" not in _lines_naming(out, "acme")[0]


@pytest.mark.asyncio
async def test_the_bare_table_never_lists_a_dependency_skipped_repo(capsys) -> None:
    """Only ``--full`` shows a skipped repo: the bare table accounts for the walked ones.

    The skipped repo here is also lab-inactive, the one shape the bare table
    would otherwise print as "not applicable".
    """
    from otto.project.state import RepoScope

    state = InstallState.INSTALLED
    ghost = RepoScope(applicable=False, usable=False, loaded_labs=("bench",), skipped=True)
    report = ProjectStatus(overall=state, repos={"acme": state}, scoping={"ghost": ghost})
    await render_status(report, OptionsSource.from_kwargs({"full": False}))
    out = capsys.readouterr().out
    assert "acme" in out
    assert "ghost" not in out
