"""``given_names`` reads the parser's own record of which flags were typed."""

from typing import Annotated

import pytest
import typer
from typer.testing import CliRunner

from otto.config._typer import get_current_context, given_names

_NAMES = ["level", "flag", "name"]


def _given_by(argv: list[str], env: dict[str, str]) -> frozenset[str]:
    app = typer.Typer()
    seen: list[frozenset[str]] = []

    @app.command()
    def cmd(
        level: int = 0,
        flag: bool = True,
        name: Annotated[str, typer.Option(envvar="OTTO_T_NAME")] = "n",
    ) -> None:
        seen.append(given_names(get_current_context(), _NAMES))

    result = CliRunner().invoke(app, argv, env=env)
    assert result.exit_code == 0, result.output
    return seen[0]


@pytest.mark.parametrize(
    ("argv", "env", "given"),
    [
        ([], {}, set()),
        (["--level", "0"], {}, {"level"}),  # typed, equal to the default
        (["--no-flag"], {}, {"flag"}),
        ([], {"OTTO_T_NAME": "x"}, {"name"}),
    ],
)
def test_the_parser_reports_what_was_given(argv, env, given):
    assert _given_by(argv, env) == frozenset(given)


def test_with_no_parser_context_everything_is_given():
    assert given_names(None, ["a", "b"]) == frozenset({"a", "b"})


def test_a_name_the_parser_does_not_know_is_given():
    app = typer.Typer()
    seen: list[frozenset[str]] = []

    @app.command()
    def cmd(level: int = 0) -> None:
        seen.append(given_names(get_current_context(), ["level", "unknown"]))

    assert CliRunner().invoke(app, []).exit_code == 0
    assert seen[0] == frozenset({"unknown"})
