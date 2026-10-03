"""A Skipped ``Result`` shows its message instead of the generic success line."""

from otto.cli.invoke import RenderPolicy, render_leaf_value
from otto.result import Result
from otto.utils import Status

_SUCCESS = "Transfer complete."  # a real host-verb success string


def _policy() -> RenderPolicy:
    return RenderPolicy(success=_SUCCESS, none_message=_SUCCESS)


def test_a_skipped_result_prints_its_message_not_the_success_line(capsys):
    render_leaf_value(Result(Status.Skipped, msg="already installed"), _policy())

    out = capsys.readouterr().out
    assert "skipped: already installed" in out
    assert _SUCCESS not in out


def test_a_skipped_result_without_a_policy_still_prints_its_message(capsys):
    render_leaf_value(Result(Status.Skipped, msg="already clean"))

    assert "skipped: already clean" in capsys.readouterr().out


def test_a_skipped_result_without_a_message_keeps_the_success_line(capsys):
    render_leaf_value(Result(Status.Skipped), _policy())

    assert _SUCCESS in capsys.readouterr().out


def test_a_success_result_keeps_the_success_line(capsys):
    render_leaf_value(Result(Status.Success, msg="ignored"), _policy())

    out = capsys.readouterr().out
    assert _SUCCESS in out
    assert "skipped" not in out


def test_the_message_is_escaped_not_read_as_markup(capsys):
    render_leaf_value(Result(Status.Skipped, msg="[bold]x[/bold] [oops]"), _policy())

    assert "skipped: [bold]x[/bold] [oops]" in capsys.readouterr().out
