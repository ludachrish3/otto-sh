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
