"""The honesty differentials' parsers, pinned against otto's real console output.

The differentials themselves run only on the lab. What they parse is otto's own
rendering: the report line `_render_build_report` prints, and the daemon rows a
host command's output becomes once `Host._log_output` has relayed it through the
console handler. A parser that read a shape the product never prints would pass
every differential vacuously, so each is fed text the product rendered.
"""

import logging
from types import SimpleNamespace

import pytest

from otto.cli import docker as docker_cli
from otto.console import CONSOLE
from otto.docker.reports import BuildReport, ImageBuild, RepoBuild
from otto.host.host import BaseHost
from otto.logger import management
from otto.result import CommandResult
from otto.utils import Status
from tests.e2e.docker._honesty import (
    DAEMON_LIST_COMMAND,
    MARK,
    parse_built_line,
    parse_daemon_rows,
)


@pytest.fixture
def console(monkeypatch):
    """The real console handler at INFO, 200 columns wide, as the e2e child runs it."""
    management.reset()
    management.install_console("INFO")
    # No public knob: rich reads COLUMNS once, when the module-level CONSOLE is built.
    monkeypatch.setattr(CONSOLE, "_width", 200)
    yield
    management.reset()


def _built(refs: "list[str]", image_id: str = "cbd8571d4b6e") -> BuildReport:
    image = ImageBuild(
        "repo1-api", refs, image_id, CommandResult(Status.Success, value="built", retcode=0)
    )
    return BuildReport(repos=[RepoBuild("repo1", "test3", "built", {"repo1-api": image})])


def test_parse_built_line_reads_the_line_the_report_renders(capsys):
    docker_cli._render_build_report(_built(["repo1-api:latest"]))
    out = capsys.readouterr().out
    assert "repo1/repo1-api: built repo1-api:latest  cbd8571d4b6e  (test3)" in out

    line = parse_built_line(out, "repo1/repo1-api")
    assert line is not None
    assert (line.refs, line.image_id, line.host) == (["repo1-api:latest"], "cbd8571d4b6e", "test3")


def test_parse_built_line_reads_several_references_raw_and_flattened(capsys):
    docker_cli._render_build_report(_built(["repo1-api:latest", "repo1-api:v2"]))
    out = capsys.readouterr().out

    for text in (out, " ".join(out.split())):
        line = parse_built_line(text, "repo1/repo1-api")
        assert line is not None, text
        assert line.refs == ["repo1-api:latest", "repo1-api:v2"]
        assert line.image_id == "cbd8571d4b6e"


def test_parse_built_line_is_none_for_another_image_or_a_failure(capsys):
    docker_cli._render_build_report(_built(["repo1-api:latest"]))
    out = capsys.readouterr().out

    assert parse_built_line(out, "repo2/repo2-worker") is None
    assert parse_built_line("repo1/repo1-api: FAILED on test3\nboom", "repo1/repo1-api") is None


def test_parse_daemon_rows_reads_the_console_relay_of_a_host_command(console, capsys):
    host = SimpleNamespace(name="test3")
    output = "\n".join(
        f"{MARK} {ref} {image_id}"
        for ref, image_id in [
            ("repo1-api:latest", "cbd8571d4b6e"),
            ("repo1-api:honesty-typed", "cbd8571d4b6e"),
            ("<none>:<none>", "0123456789ab"),
        ]
    )
    BaseHost._log_command(host, DAEMON_LIST_COMMAND)  # ty: ignore[invalid-argument-type] — a name-only stand-in is all the logger reads
    BaseHost._log_output(host, output)  # ty: ignore[invalid-argument-type] — as above
    out = capsys.readouterr().out

    # What the relay really looks like: a level column, then `@host > | row`, with
    # the first row on the INFO line and the rest continuing under it.
    assert any(
        "INFO" in line and "@test3 > | OTTO-HONESTY repo1-api:latest cbd8571d4b6e" in line
        for line in out.splitlines()
    ), out
    assert not any(line.startswith(MARK) for line in out.splitlines()), out

    assert parse_daemon_rows(out) == {
        "repo1-api:latest": "cbd8571d4b6e",
        "repo1-api:honesty-typed": "cbd8571d4b6e",
        "<none>:<none>": "0123456789ab",
    }


def test_parse_daemon_rows_ignores_the_echo_of_the_command_itself(console, capsys):
    BaseHost._log_command(SimpleNamespace(name="test3"), DAEMON_LIST_COMMAND)  # ty: ignore[invalid-argument-type] — a name-only stand-in is all the logger reads
    out = capsys.readouterr().out

    assert MARK in out, "the echo carries the mark, which is why the parser must tell it apart"
    assert parse_daemon_rows(out) == {}


def test_the_relay_logs_at_info_so_no_debug_level_is_needed(console, capsys):
    BaseHost._log_output(SimpleNamespace(name="test3"), f"{MARK} a:b 0123456789ab")  # ty: ignore[invalid-argument-type] — a name-only stand-in is all the logger reads
    assert logging.getLogger().getEffectiveLevel() <= logging.INFO
    assert parse_daemon_rows(capsys.readouterr().out) == {"a:b": "0123456789ab"}
