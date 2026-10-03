"""THE SPLIT-BRAIN GUARD for the docker verbs: each leaf hands its library function
exactly the parsed flags and reports exactly the library's refusal in flag spelling.
No rule may live in a leaf.
Verified red when written: making `_build` pass `repo=None` regardless of --repo fails
the build/--repo row; replacing `_run_docker`'s `except DockerVerbError` arm with
`return _DECLINED` failed all four REFUSALS rows on `assert result.exit_code == 2`
(`assert 0 == 2`).
"""

from unittest.mock import AsyncMock, patch

import pytest

from otto.cli.docker import docker_app
from otto.docker.deployment import UseCaseStack
from otto.docker.observe import DockerVerbError, LogsTarget, ObserveReport
from otto.docker.reports import BuildReport, TeardownReport
from otto.docker.resolve import Selection, UseCaseResolutionError
from tests._fixtures.dispatch import DispatchRunner
from tests.unit.cli.conftest import _flat

_EMPTY_BUILD = BuildReport(repos=[])
_EMPTY_DOWN = TeardownReport(hosts={}, use_case="integration")
# A stack with no participating fragment. This deliberately drives
# `_print_stack_report`'s defensive "registered no container on any host"
# branch (unreachable from a resolvable selection in normal use); it exits 0,
# which is all a flag-forwarding row needs from the return value. Built
# straight from `UseCaseStack`'s own defaults (no helper in
# tests/unit/docker/test_deploy.py builds one without a resolved selection,
# host and lab in play) rather than reaching for the full deploy machinery
# this differential does not otherwise need.
_EMPTY_UP = UseCaseStack(
    use_case="integration", selection=Selection(use_case="integration", fragments=[])
)

# (argv, seam, expected positional args, expected kwargs, an ok return)
ROWS = [
    (
        ["build", "--on", "test3"],
        "otto.docker.build_verbs.build_on",
        ("test3",),
        {
            "repo": None,
            "images": None,
            "tags": None,
            "no_cache": False,
            "pull": False,
            "build_args": {},
            "target": None,
        },
        _EMPTY_BUILD,
    ),
    (
        ["build", "--on", "test3", "--repo", "r1", "api", "db"],
        "otto.docker.build_verbs.build_on",
        ("test3",),
        {
            "repo": "r1",
            "images": ["api", "db"],
            "tags": None,
            "no_cache": False,
            "pull": False,
            "build_args": {},
            "target": None,
        },
        _EMPTY_BUILD,
    ),
    (
        ["compose", "build", "integration"],
        "otto.docker.build_verbs.compose_build",
        ("integration",),
        {
            "on": None,
            "provide": {},
            "images": None,
            "no_cache": False,
            "pull": False,
            "build_args": {},
        },
        _EMPTY_BUILD,
    ),
    (
        [
            "compose",
            "build",
            "integration",
            "api",
            "--on",
            "test3",
            "--provide",
            "db=r2",
        ],
        "otto.docker.build_verbs.compose_build",
        ("integration",),
        {
            "on": "test3",
            "provide": {"db": "r2"},
            "images": ["api"],
            "no_cache": False,
            "pull": False,
            "build_args": {},
        },
        _EMPTY_BUILD,
    ),
    (
        ["compose", "down", "integration"],
        "otto.docker.deployment.teardown",
        ("integration",),
        {"services": None, "on": None, "provide": {}},
        _EMPTY_DOWN,
    ),
    (
        ["compose", "down", "integration", "api", "--on", "test3", "--provide", "db=r2"],
        "otto.docker.deployment.teardown",
        ("integration",),
        {"services": ["api"], "on": "test3", "provide": {"db": "r2"}},
        _EMPTY_DOWN,
    ),
    (
        ["compose", "up", "integration"],
        "otto.docker.deployment.deploy",
        ("integration",),
        {
            "services": None,
            "on": None,
            "provide": {},
            "env": {},
            "env_files": None,
            "build": False,
            "force_recreate": False,
            "pull": None,
        },
        _EMPTY_UP,
    ),
    (
        ["compose", "up", "integration", "--force-recreate", "--pull", "never"],
        "otto.docker.deployment.deploy",
        ("integration",),
        {
            "services": None,
            "on": None,
            "provide": {},
            "env": {},
            "env_files": None,
            "build": False,
            "force_recreate": True,
            "pull": "never",
        },
        _EMPTY_UP,
    ),
    (
        ["ps", "-a"],
        "otto.docker.observe.list_containers",
        (),
        {"on": None, "all": True},
        ObserveReport([]),
    ),
    (
        ["images", "--on", "test3"],
        "otto.docker.observe.list_images",
        (),
        {"on": "test3"},
        ObserveReport([]),
    ),
    (
        ["logs", "test3.integration.web"],
        "otto.docker.observe.container_logs",
        ("test3.integration.web",),
        {"on": None, "tail": None, "since": None, "timestamps": False},
        ObserveReport([]),
    ),
    (
        ["compose", "ps", "integration", "-a", "--on", "test3", "--provide", "db=r2"],
        "otto.docker.observe.compose_ps",
        ("integration",),
        {"all": True, "on": "test3", "provide": {"db": "r2"}},
        ObserveReport([]),
    ),
    (
        [
            "compose",
            "logs",
            "integration",
            "api",
            "--on",
            "test3",
            "--provide",
            "db=r2",
            "--tail",
            "5",
            "--since",
            "1m",
            "-t",
        ],
        "otto.docker.observe.compose_logs",
        ("integration", ["api"]),
        {
            "on": "test3",
            "provide": {"db": "r2"},
            "tail": "5",
            "since": "1m",
            "timestamps": True,
        },
        ObserveReport([]),
    ),
]


@pytest.mark.parametrize(
    ("argv", "seam", "args", "kwargs", "ret"),
    ROWS,
    ids=[" ".join(r[0]) for r in ROWS],
)
def test_each_leaf_hands_the_library_exactly_its_flags(argv, seam, args, kwargs, ret):
    fake = AsyncMock(return_value=ret)
    with patch(seam, fake):
        result = DispatchRunner().invoke(docker_app, argv, spec_name="docker")
    assert result.exit_code == 0, result.output
    fake.assert_awaited_once()
    assert fake.await_args.args == args
    assert fake.await_args.kwargs == kwargs


def test_compose_up_passes_env_and_env_file_through(tmp_path):
    """``compose up``'s ``--env``/``--env-file`` reach ``deploy`` untouched.

    ``--env-file`` has ``exists=True``, so it needs a real file on disk — not
    representable as a module-level :data:`ROWS` tuple — hence this row lives
    in its own test rather than folded into
    :func:`test_each_leaf_hands_the_library_exactly_its_flags`.
    """
    env_file = tmp_path / "extra.env"
    env_file.write_text("K=V\n")
    fake = AsyncMock(return_value=_EMPTY_UP)
    with patch("otto.docker.deployment.deploy", fake):
        result = DispatchRunner().invoke(
            docker_app,
            [
                "compose",
                "up",
                "integration",
                "--build",
                "--env",
                "K=V",
                "--env-file",
                str(env_file),
            ],
            spec_name="docker",
        )
    assert result.exit_code == 0, result.output
    fake.assert_awaited_once()
    assert fake.await_args.args == ("integration",)
    assert fake.await_args.kwargs == {
        "services": None,
        "on": None,
        "provide": {},
        "env": {"K": "V"},
        "env_files": [env_file],
        "build": True,
        "force_recreate": False,
        "pull": None,
    }


REFUSALS = [
    (
        ["build"],
        "otto.docker.build_verbs.build_on",
        DockerVerbError(
            "host is required; docker-capable hosts in lab 'unix': ['test3']", field="host"
        ),
        "--on",
    ),
    (
        ["build", "--on", "t", "--repo", "zz"],
        "otto.docker.build_verbs.build_on",
        DockerVerbError(
            "repo 'zz' is not a loaded repo with a [docker] section; docker repos: ['r1']",
            field="repo",
        ),
        "--repo",
    ),
    (
        ["build", "--on", "t", "apo"],
        "otto.docker.build_verbs.build_on",
        DockerVerbError(
            "no selected repo declares an image named 'apo'; declared: ['api']", field="images"
        ),
        "IMAGE",
    ),
    (
        ["compose", "build", "integration", "apo"],
        "otto.docker.build_verbs.compose_build",
        DockerVerbError(
            "no selected repo declares an image named 'apo'; declared: ['api']", field="images"
        ),
        "IMAGE",
    ),
    (
        ["ps", "--on", "ghost"],
        "otto.docker.observe.list_containers",
        DockerVerbError(
            "host 'ghost' is not a docker-capable unix host in lab 'unix'; "
            "docker-capable hosts here: ['test3']",
            field="host",
        ),
        "--on",
    ),
    (
        ["ps"],
        "otto.docker.observe.list_containers",
        DockerVerbError("lab 'unix' has no docker-capable unix host", field="host"),
        "--on",
    ),
    (
        ["logs", "web-1"],
        "otto.docker.observe.container_logs",
        DockerVerbError(
            "'web-1' is not a container host id of lab 'unix' (declared: "
            "['test3.integration.web']); to name a docker container or id directly, "
            "name the host it is on (`on`)",
            field="container",
        ),
        "CONTAINER",
    ),
    (
        ["logs", "x", "--on", "test3", "-f"],
        "otto.docker.observe.follow_logs",
        DockerVerbError("follow needs an SSH parent; test3 is reached by telnet", field="follow"),
        "--follow",
    ),
]


@pytest.mark.parametrize(
    ("argv", "seam", "exc", "flag"),
    REFUSALS,
    ids=[" ".join(r[0]) for r in REFUSALS],
)
def test_each_library_refusal_reaches_the_user_in_flag_spelling(argv, seam, exc, flag):
    # `-f` resolves the target before it follows; the resolver is not what is under test.
    target = AsyncMock(return_value=LogsTarget(object(), "docker logs x"))
    with patch(seam, AsyncMock(side_effect=exc)), patch("otto.docker.observe.resolve_logs", target):
        result = DispatchRunner().invoke(docker_app, argv, spec_name="docker")
    assert result.exit_code == 2, result.output
    # `_flat` collapses rich's wrapped error-panel borders and whitespace: the
    # panel wraps a message this long across lines, so a plain substring check
    # on `result.output` false-negatives on where it wraps (same reasoning as
    # `tests/unit/cli/test_test_differential.py`).
    flat_output = _flat(result.output)
    assert f"Invalid value for {flag}" in flat_output
    assert _flat(str(exc)) in flat_output


def test_a_use_case_resolution_refusal_is_the_librarys_text_at_exit_1():
    """``UseCaseResolutionError`` is a configuration answer, not a bad flag.

    ``_run_docker`` reports it verbatim and exits 1, unlike a ``DockerVerbError``
    (exit 2, in flag spelling), so it is not a row of :data:`REFUSALS`.
    """
    exc = UseCaseResolutionError(
        "use-case 'integration': no participating fragment declares service(s) ['web']; "
        "declared services: ['api']"
    )
    with patch("otto.docker.observe.compose_logs", AsyncMock(side_effect=exc)):
        result = DispatchRunner().invoke(
            docker_app, ["compose", "logs", "integration", "web"], spec_name="docker"
        )
    assert result.exit_code == 1, result.output
    assert _flat(str(exc)) in _flat(result.output)
