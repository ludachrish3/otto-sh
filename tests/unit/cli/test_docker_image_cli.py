"""``otto docker image ls|rm|prune`` (and the ``images`` alias): docker's command tree, thin.

Each leaf's flag forwarding and refusal spelling are rows of the canonical split-brain
table, ``tests/unit/cli/test_docker_differential.py``. Here: each leaf prints docker's
text under one ``== host ==`` line per host, exits 1 when any host failed, and
records the images of every host it reached for the next TAB. The end-to-end
rows drive the real library under the production dispatch against real
``UnixHost`` objects whose ``exec`` is a spy: no address is dialled.
"""

from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from otto import fleet as fleet_mod
from otto.cli import docker as docker_cli
from otto.config.completion_cache import ObservedDockerState
from otto.docker.observe import HostOutput, ObserveReport
from otto.host.element import Element
from otto.host.login_proxy import Cred
from otto.host.unix_host import UnixHost
from otto.labs.loader import Lab
from otto.result import CommandResult
from otto.utils import DRY_RUN_HEADLINE, Status
from tests._fixtures.dispatch import DispatchRunner
from tests.conftest import active_context
from tests.unit.cli.conftest import _flat

_LS = "REPOSITORY   TAG       IMAGE ID       CREATED   SIZE\n"


def _out(host: str, text: str, *, ok: bool = True, reached: bool = True) -> HostOutput:
    status = Status.Success if ok else Status.Failed
    result = CommandResult(status, value=text, command="docker x", retcode=0 if ok else 1)
    return HostOutput(host, "docker x", result, reached=reached)


def _invoke(*argv: str):
    return DispatchRunner().invoke(docker_cli.docker_app, list(argv), spec_name="docker")


# ---------------------------------------------------------------------------
# Usage (the flag-forwarding and refusal rows live in test_docker_differential.py)
# ---------------------------------------------------------------------------


def test_rm_needs_an_image():
    with patch("otto.docker.image_verbs.remove_images", AsyncMock()) as fake:
        result = _invoke("image", "rm")
    assert result.exit_code == 2, result.output
    fake.assert_not_awaited()


# ---------------------------------------------------------------------------
# A failing host: its own error, the others still printed, exit 1
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("argv", "seam"),
    [
        (["image", "rm", "api"], "otto.docker.image_verbs.remove_images"),
        (["image", "prune", "-f"], "otto.docker.image_verbs.prune_images"),
        (["image", "ls"], "otto.docker.observe.list_images"),
    ],
    ids=["rm", "prune", "ls"],
)
def test_a_failing_host_prints_its_own_error_and_the_verb_exits_1(argv, seam):
    report = ObserveReport(
        [
            _out("test3", "Error response from daemon: No such image: api:latest\n", ok=False),
            _out("alt2", "Untagged: api:latest\n"),
        ]
    )
    with (
        patch(seam, AsyncMock(return_value=report)),
        patch.object(docker_cli, "_record_observed", AsyncMock()),
    ):
        result = _invoke(*argv)
    assert result.exit_code == 1, result.output
    assert "== test3 ==\nError response from daemon: No such image: api:latest\n" in result.output
    assert "== alt2 ==\nUntagged: api:latest\n" in result.output


# ---------------------------------------------------------------------------
# The completion record: removed names must not linger
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("argv", "seam"),
    [
        (["image", "rm", "api"], "otto.docker.image_verbs.remove_images"),
        (["image", "prune", "-f"], "otto.docker.image_verbs.prune_images"),
    ],
    ids=["rm", "prune"],
)
def test_rm_and_prune_re_record_every_host_they_reached_even_one_that_failed(argv, seam):
    # `rm a b` on a host holding only `a` removes `a` and still fails: that host's
    # record must be refreshed, or TAB keeps offering the image just removed. A
    # host otto could not reach is not asked again.
    report = ObserveReport(
        [
            _out("test3", "Untagged: a\nError: No such image: b\n", ok=False),
            _out("alt2", "Untagged: a\n"),
            _out("east", "otto: host 'east' unreachable", ok=False, reached=False),
        ]
    )
    with (
        patch(seam, AsyncMock(return_value=report)),
        patch.object(docker_cli, "_record_observed", AsyncMock()) as record,
    ):
        result = _invoke(*argv)
    assert result.exit_code == 1, result.output
    record.assert_awaited_once_with(["test3", "alt2"], images=True)


@pytest.mark.parametrize("argv", [["image", "ls"], ["images"]], ids=["image-ls", "images"])
def test_ls_records_the_hosts_that_answered(argv):
    report = ObserveReport([_out("test3", _LS), _out("alt2", "down", ok=False)])
    with (
        patch("otto.docker.observe.list_images", AsyncMock(return_value=report)),
        patch.object(docker_cli, "_record_observed", AsyncMock()) as record,
    ):
        result = _invoke(*argv)
    assert result.exit_code == 1, result.output
    record.assert_awaited_once_with(["test3"], images=True)


# ---------------------------------------------------------------------------
# End to end through the real library: the exact command, and --dry-run
# ---------------------------------------------------------------------------


def _lab() -> Lab:
    lab = Lab(name="unix")
    for host_id, ip in (("test3", "10.0.0.13"), ("alt2", "10.0.0.22")):
        lab.add_host(
            UnixHost(
                ip=ip,
                element=Element(host_id),
                creds=[Cred(login="u", password="p")],
                docker_capable=True,
            )
        )
    return lab


def _run(argv, *, dry_run: bool):
    """Drive *argv* through the dispatch and the real library; return (result, commands)."""
    lab = _lab()
    commands: list[str] = []

    async def _exec(self, cmd, *_a, **_kw):
        commands.append(f"{self.id}: {cmd}")
        return CommandResult(Status.Success, value=f"done on {self.id}\n", command=cmd, retcode=0)

    with (
        patch.object(fleet_mod, "get_lab", return_value=lab),
        patch.object(UnixHost, "exec", _exec),
        patch.object(docker_cli, "_record_observed", AsyncMock()),
        active_context(lab, dry_run=dry_run),
    ):
        result = _invoke(*argv)
    return result, commands


@pytest.mark.parametrize(
    ("argv", "command"),
    [
        (["image", "ls", "-a"], "docker image ls -a"),
        (["images"], "docker image ls"),
        (["image", "rm", "my image", "-f"], "docker image rm -f -- 'my image'"),
        # `--` on otto's line ends otto's flags; `-f` is then an image, and stays one.
        (["image", "rm", "--", "-f", "api"], "docker image rm -- -f api"),
        (
            ["image", "prune", "-f", "--filter", "label=otto e2e"],
            "docker image prune -f --filter 'label=otto e2e'",
        ),
    ],
    ids=["ls", "images", "rm", "rm-dashdash", "prune"],
)
def test_a_dry_run_previews_each_hosts_exact_command_and_runs_nothing(argv, command):
    dry, dry_commands = _run(argv, dry_run=True)
    assert dry.exit_code == 0, dry.output
    assert dry_commands == []
    flat = _flat(dry.output)
    assert f"on test3: {command}" in flat
    assert f"on alt2: {command}" in flat
    assert DRY_RUN_HEADLINE not in dry.output, "the leaf previews; the generic block must not"
    # Positive control: without --dry-run the same line runs exactly that command per host.
    live, live_commands = _run(argv, dry_run=False)
    assert live.exit_code == 0, live.output
    assert live_commands == [f"test3: {command}", f"alt2: {command}"]
    assert "== test3 ==\ndone on test3\n" in live.output


def test_prune_without_f_fails_saying_so_and_runs_nothing_even_in_a_dry_run():
    for dry_run in (False, True):
        result, commands = _run(["image", "prune", "-a"], dry_run=dry_run)
        assert result.exit_code == 2, result.output
        assert commands == []
        flat = _flat(result.output)
        assert "Invalid value for --force" in flat
        assert "needs -f" in flat
        assert "never prompts" in flat


def test_images_is_documented_as_the_alias_of_image_ls():
    result = _invoke("images", "--help")
    assert result.exit_code == 0, result.output
    assert "otto docker image ls" in _flat(result.output)


# ---------------------------------------------------------------------------
# Completion: `image rm IMAGE` offers declared names, then what the daemon last listed
# ---------------------------------------------------------------------------

NAMES = {
    "docker_images": ["db", "api"],
    "docker_hosts": ["alt2", "test3", "west1"],
    "hosts_by_lab": {"east": ["test3", "alt2"], "west": ["west1", "alt2"]},
    # west ties on docker_priority: the one rule refuses there. `image rm` does not care.
    "docker_default_parent_by_lab": {"east": "test3"},
}
OBSERVED = {
    "test3": ObservedDockerState(["api:latest", "redis:7"], ["sha-a", "sha-r"], [], []),
    "alt2": ObservedDockerState(["db", "redis:7", "nginx:1"], ["sha-d", "sha-r", "sha-n"], [], []),
    "west1": ObservedDockerState(["west:only"], ["sha-w"], [], []),
}


@pytest.fixture
def warm_cache():
    with (
        patch("otto.registry.get_completion_names", return_value=NAMES),
        patch(
            "otto.config.completion_cache.read_docker_observed",
            side_effect=lambda _r, h: OBSERVED.get(h, ObservedDockerState([], [], [], [])),
        ),
        patch("otto.config.project.discover", return_value=SimpleNamespace(projects=["p"])),
    ):
        yield


def _ctx(**params):
    return SimpleNamespace(params=params, parent=None, resilient_parsing=True)


def test_image_rm_completes_declared_names_then_the_parents_observed_refs(warm_cache):
    complete = docker_cli._image_ref_completer
    assert complete(_ctx(parent="test3"), "") == ["api", "db", "api:latest", "redis:7"]
    assert complete(_ctx(parent="test3"), "r") == ["redis:7"]
    # A name both declared and observed is offered once.
    assert complete(_ctx(parent="alt2"), "") == ["api", "db", "redis:7", "nginx:1"]


def _labs_ctx(*labs, **params):
    return SimpleNamespace(
        params={"labs": list(labs), **params} if labs else params,
        parent=None,
        resilient_parsing=True,
    )


def test_image_rm_without_a_parent_offers_the_union_of_every_docker_hosts_refs(warm_cache):
    # The verb runs on every docker-capable host, so TAB offers what any of them
    # holds: hosts in the docker_hosts payload's order, refs in each daemon's order,
    # each once.
    complete = docker_cli._image_ref_completer
    assert complete(_labs_ctx(parent=None), "") == [
        "api",
        "db",
        "redis:7",
        "nginx:1",
        "api:latest",
        "west:only",
    ]
    assert complete(_labs_ctx(parent=None), "r") == ["redis:7"]


def test_image_rm_without_a_parent_is_scoped_to_the_selected_labs_docker_hosts(warm_cache):
    complete = docker_cli._image_ref_completer
    assert complete(_labs_ctx("east", parent=None), "") == [
        "api",
        "db",
        "redis:7",
        "nginx:1",
        "api:latest",
    ]
    # west ties on docker_priority, where the parent rule refuses; image rm still has hosts.
    assert complete(_labs_ctx("west", parent=None), "") == [
        "api",
        "db",
        "redis:7",
        "nginx:1",
        "west:only",
    ]


def test_image_rm_with_a_parent_offers_that_hosts_refs_alone(warm_cache):
    complete = docker_cli._image_ref_completer
    assert complete(_labs_ctx("west", parent="west1"), "") == ["api", "db", "west:only"]


def test_image_rm_completion_never_raises():
    boom = RuntimeError("boom")
    with (
        patch("otto.registry.get_completion_names", side_effect=boom),
        patch("otto.declared.get_projects", side_effect=boom),
        patch("otto.config.project.discover", side_effect=boom),
    ):
        assert docker_cli._image_ref_completer(_ctx(parent="test3"), "") == []


def test_image_rm_is_wired_to_its_completer():
    import inspect

    annotation = inspect.signature(docker_cli._image_rm).parameters["image"].annotation
    (info,) = [m for m in annotation.__metadata__ if hasattr(m, "autocompletion")]
    assert info.autocompletion is docker_cli._image_ref_completer
