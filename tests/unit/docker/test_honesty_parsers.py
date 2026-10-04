"""The honesty differentials' parsers, pinned against otto's real console output.

The differentials themselves run only on the lab. What they parse is otto's own
rendering: the report line `_render_build_report` prints, and the daemon rows a
host command's output becomes once `Host._log_output` has relayed it through the
console handler. A parser that read a shape the product never prints would pass
every differential vacuously, so each is fed text the product rendered.
"""

import logging
import os
import subprocess
import sys
from types import SimpleNamespace

import pytest

from otto.cli import docker as docker_cli
from otto.console import CONSOLE
from otto.docker.deployment import _refuse_failed_up
from otto.docker.reports import BuildReport, ImageBuild, RepoBuild
from otto.host.errors import HostCommandError
from otto.host.host import BaseHost
from otto.logger import management
from otto.result import CommandResult
from otto.utils import Status
from tests.e2e.docker._cli import _WIDE
from tests.e2e.docker._honesty import (
    DAEMON_IMAGE_PAIRS_COMMAND,
    DAEMON_LIST_COMMAND,
    DAEMON_PS_PAIRS_COMMAND,
    MARK,
    names_a_missing_image,
    parse_built_line,
    parse_compose_ps_names,
    parse_daemon_pairs,
    parse_daemon_rows,
    parse_images_rows,
    parse_project_image_ids,
    parse_ps_ids,
    project_image_ids_command,
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


def test_parse_daemon_pairs_keeps_the_daemons_order_and_skips_the_echo(console, capsys):
    host = SimpleNamespace(name="test3")
    rows = [("web-1", "cbd8571d4b6e"), ("db-1", "0123456789ab"), ("web-0", "aaaaaaaaaaaa")]
    BaseHost._log_command(host, DAEMON_PS_PAIRS_COMMAND)  # ty: ignore[invalid-argument-type] — a name-only stand-in is all the logger reads
    stray = [
        f"{MARK} too many fields here",  # marked, but not a two-field row
        f"{MARK} lonely",  # marked, one field
        "unmarked two",  # two fields, no mark: not the daemon query's row
    ]
    BaseHost._log_output(host, "\n".join([*stray, *(f"{MARK} {name} {cid}" for name, cid in rows)]))  # ty: ignore[invalid-argument-type] — as above
    out = capsys.readouterr().out

    assert "{{.Names}}" in out, (
        "the echo is in the text, which is why the parser must tell it apart"
    )
    assert parse_daemon_pairs(out) == [[name, cid] for name, cid in rows]


def test_parse_daemon_pairs_reads_the_images_command_and_keeps_a_repeated_id(console, capsys):
    host = SimpleNamespace(name="test3")
    BaseHost._log_command(host, DAEMON_IMAGE_PAIRS_COMMAND)  # ty: ignore[invalid-argument-type] — a name-only stand-in is all the logger reads
    BaseHost._log_output(
        host,  # ty: ignore[invalid-argument-type] — as above
        f"{MARK} a:1 cbd8571d4b6e\n{MARK} a:2 cbd8571d4b6e\n{MARK} <none>:<none> 0123456789ab",
    )
    assert parse_daemon_pairs(capsys.readouterr().out) == [
        ["a:1", "cbd8571d4b6e"],
        ["a:2", "cbd8571d4b6e"],
        ["<none>:<none>", "0123456789ab"],
    ]


_FULL_ID = "cbd8571d4b6e" + "0123456789abcdef" * 3 + "0123"


def test_parse_project_image_ids_reads_the_console_relay_of_docker_inspect(console, capsys):
    host = SimpleNamespace(name="test3")
    command = project_image_ids_command("unix-repo1-e2e-1a2b3c4d")
    other = "e" * 64
    # docker prints one `sha256:<64 hex>` per container, after the mark the format string wrote
    output = "\n".join(f"{MARK} sha256:{image_id}" for image_id in (_FULL_ID, other))
    BaseHost._log_command(host, command)  # ty: ignore[invalid-argument-type] — a name-only stand-in is all the logger reads
    BaseHost._log_output(host, output)  # ty: ignore[invalid-argument-type] — as above
    out = capsys.readouterr().out

    assert any(
        "INFO" in line and f"@test3 > | OTTO-HONESTY sha256:{_FULL_ID}" in line
        for line in out.splitlines()
    ), out
    assert parse_project_image_ids(out) == [_FULL_ID, other]


_RELAY_IN_A_CHILD = """
import sys
from types import SimpleNamespace
from otto.host.host import BaseHost
from otto.logger import management

management.install_console("INFO")
BaseHost._log_output(SimpleNamespace(name="test3"), sys.argv[1])
"""


def _relayed_by_a_child(row: str, *, env: "dict[str, str]") -> str:
    """*row* as a child otto process prints it: piped stdout, so no tty to size from."""
    # CI sets variables that force a terminal and ignore COLUMNS; the lane's child
    # runs on a workstation, so they are removed to model the lane, not the runner.
    base = {k: v for k, v in os.environ.items() if k not in {"GITHUB_ACTIONS", "FORCE_COLOR"}}
    base.pop("COLUMNS", None)
    done = subprocess.run(
        [sys.executable, "-c", _RELAY_IN_A_CHILD, row],
        env={**base, "TERM": "dumb", **env},
        capture_output=True,
        text=True,
        timeout=60,
        check=True,
    )
    return done.stdout


def test_the_inspect_row_wraps_without_columns_and_parses_with_the_lanes_width():
    """The width the lane gives its child (``COLUMNS``) is what keeps the row on one line.

    The ``docker inspect`` row is ~84 characters before the console's own prefix,
    so a child that falls back to 80 columns wraps it, and a parser reading line
    by line then finds nothing. The `console` fixture above sets ``CONSOLE._width``,
    which skips the path the lane really uses; this one renders in a child
    process and hands it :data:`_WIDE` the way ``_run_otto(env=_WIDE)`` does.
    """
    row = f"{MARK} sha256:{_FULL_ID}"

    narrow = _relayed_by_a_child(row, env={})
    assert parse_project_image_ids(narrow) == [], (
        "an 80-column child no longer wraps the row, so the lane's `env=_WIDE` "
        f"guards nothing and this pin is vacuous:\n{narrow}"
    )

    wide = _relayed_by_a_child(row, env=_WIDE)
    assert parse_project_image_ids(wide) == [_FULL_ID], wide


def test_parse_project_image_ids_ignores_the_echo_of_the_command_itself(console, capsys):
    command = project_image_ids_command("unix-repo1-e2e-1a2b3c4d")
    BaseHost._log_command(SimpleNamespace(name="test3"), command)  # ty: ignore[invalid-argument-type] — a name-only stand-in is all the logger reads
    out = capsys.readouterr().out

    assert MARK in out, "the echo carries the mark, which is why the parser must tell it apart"
    assert "label=com.docker.compose.project=unix-repo1-e2e-1a2b3c4d" in out
    assert parse_project_image_ids(out) == []


def test_parse_project_image_ids_is_empty_when_docker_names_no_container():
    refusal = "\"docker inspect\" requires at least 1 argument.\nSee 'docker inspect --help'."
    assert parse_project_image_ids(refusal) == []


def test_names_a_missing_image_reads_each_form_docker_prints():
    forms = [
        # the classic image store
        (
            "Error response from daemon: pull access denied for repo1-api, repository does not "
            "exist or may require 'docker login': denied: requested access to the resource is "
            "denied"
        ),
        # the containerd image store, anonymous
        (
            'Error response from daemon: failed to resolve reference "docker.io/library/'
            'repo1-api:latest": pull access denied, repository does not exist or may require '
            "authorization: server message: insufficient_scope: authorization failed"
        ),
        # the containerd image store, logged in to the registry: a bare 404
        (
            'Error response from daemon: failed to resolve reference "docker.io/library/'
            'repo1-api:latest": docker.io/library/repo1-api:latest: not found'
        ),
        (
            "Error response from daemon: manifest for repo1-api:latest not found: manifest "
            "unknown: manifest unknown"
        ),
        "Error response from daemon: No such image: repo1-api:latest",
    ]
    for form in forms:
        assert names_a_missing_image(form), form


def test_names_a_missing_image_reads_the_registry_unreachable_forms_of_the_classic_store():
    # Each sample trips exactly one arm, so dropping either arm reddens its own line.
    assert names_a_missing_image(
        'Error response from daemon: Get "https://registry-1.docker.io/v2/": '
        "dial tcp 54.236.113.205:443: i/o timeout"
    )
    assert names_a_missing_image(
        'Error response from daemon: Get "https://registry-1.docker.io/v2/": '
        "lookup registry-1.docker.io on 10.0.2.3:53: server misbehaving"
    )


def test_names_a_missing_image_reads_the_reference_not_found_form_by_its_reference_alone():
    # No "pull access denied", no "manifest": only the reference followed by `not found`.
    assert names_a_missing_image("docker.io/library/repo1-api:latest: not found")
    assert not names_a_missing_image("not found")


def test_names_a_missing_image_reads_the_failure_otto_wraps_it_in():
    with pytest.raises(HostCommandError) as raised:
        _refuse_failed_up(
            "unix-repo1-e2e-1a2b3c4d",
            "test3",
            "Error response from daemon: pull access denied for repo1-api, repository does not "
            "exist or may require 'docker login': denied: requested access to the resource is "
            "denied",
        )
    wrapped = str(raised.value)

    assert "docker compose up failed for unix-repo1-e2e-1a2b3c4d on test3" in wrapped
    assert names_a_missing_image(wrapped)
    # a console that broke the sentence across lines is the same sentence
    assert names_a_missing_image(wrapped.replace("repository does", "repository\n    does"))


def test_names_a_missing_image_is_false_for_a_stack_that_came_up_or_failed_otherwise():
    for text in [
        "Container unix-repo1-e2e-1a2b3c4d-api-1  Started",
        "bash: docker: command not found",
        "sh: 1: docker: not found",
        "Error response from daemon: driver failed programming external connectivity",
    ]:
        assert not names_a_missing_image(text), text


# `otto docker ps` / `images` fan-outs, in docker's real column layout. Text
# before the first header belongs to no host and must not be read as a row.
_PS_FAN_OUT = """\
0123456789ab   stray      line before any header
== test3 ==
CONTAINER ID   IMAGE              COMMAND   CREATED         STATUS         PORTS   NAMES
3f1c2a9b7d10   repo1-api:latest   "api"     2 minutes ago   Up 2 minutes           repo1-api-1

== test1 ==
CONTAINER ID   IMAGE     COMMAND   CREATED   STATUS    PORTS     NAMES
"""

_IMAGES_FAN_OUT = """\
stray        latest    cafecafecafe   before any header   1MB
== test3 ==
REPOSITORY   TAG       IMAGE ID       CREATED         SIZE
repo1-api    latest    cbd8571d4b6e   2 minutes ago   148MB
<none>       <none>    0123456789ab   3 days ago      1MB

== test1 ==
REPOSITORY   TAG       IMAGE ID       CREATED         SIZE
"""


# The new layout, verbatim from a lab daemon's `docker images`, plus a dangling row.
_NEW_LAYOUT_WARNING = (
    "WARNING: This output is designed for human readability. "
    "For machine-readable output, please use --format."
)
_IMAGES_NEW_LAYOUT = f"""\
== test1 ==
{_NEW_LAYOUT_WARNING}
IMAGE                          ID             DISK USAGE   CONTENT SIZE   EXTRA
alpine:3.20                     d9e853e87e55       13.7MB         4.17MB
centos:7                        be65f488b776        434MB          108MB
repo1-api:65af53dc0cb421d6      775f6229be55       18.9MB         4.95MB
repo1-api:latest                47973a8d470c       18.9MB         4.95MB
repo2-worker:latest             ec32860faf18       13.6MB         4.09MB
<none>:<none>                   0123456789ab       1.2MB          1MB

== test2 ==
{_NEW_LAYOUT_WARNING}
IMAGE  ID   DISK USAGE   CONTENT SIZE   EXTRA
"""


def test_parse_ps_ids_reads_each_hosts_rows_and_ignores_text_before_a_header():
    assert parse_ps_ids(_PS_FAN_OUT) == {"test3": ["3f1c2a9b7d10"], "test1": []}


def test_parse_images_rows_skips_dangling_rows_and_ignores_text_before_a_header():
    assert parse_images_rows(_IMAGES_FAN_OUT) == {
        "test3": {"repo1-api:latest": "cbd8571d4b6e"},
        "test1": {},
    }


def test_parse_images_rows_reads_the_new_layout_and_skips_its_warning_header_and_dangling_row():
    assert parse_images_rows(_IMAGES_NEW_LAYOUT) == {
        "test1": {
            "alpine:3.20": "d9e853e87e55",
            "centos:7": "be65f488b776",
            "repo1-api:65af53dc0cb421d6": "775f6229be55",
            "repo1-api:latest": "47973a8d470c",
            "repo2-worker:latest": "ec32860faf18",
        },
        "test2": {},
    }


def test_the_parsers_read_what_the_renderer_prints(capsys):
    from otto.docker.observe import HostOutput, ObserveReport

    def _out(host: str, text: str) -> HostOutput:
        return HostOutput(host, "docker ps", CommandResult(Status.Success, value=text, retcode=0))

    ps = "CONTAINER ID   IMAGE   NAMES\n3f1c2a9b7d10   img     c1\n"
    images = "REPOSITORY   TAG      IMAGE ID       SIZE\nr            latest   cbd8571d4b6e   1MB\n"
    docker_cli._render_observe(ObserveReport([_out("test3", ps), _out("alt2", "")]), header=True)
    assert parse_ps_ids(capsys.readouterr().out) == {"test3": ["3f1c2a9b7d10"], "alt2": []}
    docker_cli._render_observe(ObserveReport([_out("test3", images)]), header=True)
    assert parse_images_rows(capsys.readouterr().out) == {"test3": {"r:latest": "cbd8571d4b6e"}}


# `otto docker compose ps` fan-out, in compose's real column layout. Text before
# the first header belongs to no host and must not be read as a row.
_COMPOSE_PS_FAN_OUT = """\
stray-name   stray line before any header
== test3 ==
NAME                 IMAGE        COMMAND   SERVICE   CREATED         STATUS         PORTS
unix-repo1-e2e-api   repo1-api    "api"     api       2 minutes ago   Up 2 minutes
unix-repo1-e2e-db    repo1-db     "db"      db        2 minutes ago   Up 2 minutes   5432/tcp

== test1 ==
NAME      IMAGE     COMMAND   SERVICE   CREATED   STATUS    PORTS
"""


def test_parse_compose_ps_names_reads_each_hosts_rows_and_ignores_text_before_a_header():
    assert parse_compose_ps_names(_COMPOSE_PS_FAN_OUT) == {
        "test3": ["unix-repo1-e2e-api", "unix-repo1-e2e-db"],
        "test1": [],
    }


def test_parse_compose_ps_names_reads_what_the_renderer_prints(capsys):
    from otto.docker.observe import HostOutput, ObserveReport

    text = (
        "NAME                 IMAGE   COMMAND   SERVICE   CREATED   STATUS   PORTS\n"
        "unix-repo1-x-api-1   img     cmd       api       1m ago    Up 1 minute\n"
    )
    done = CommandResult(Status.Success, value=text, retcode=0)
    docker_cli._render_observe(
        ObserveReport([HostOutput("test3", "docker compose ps", done)]), header=True
    )
    assert parse_compose_ps_names(capsys.readouterr().out) == {"test3": ["unix-repo1-x-api-1"]}
