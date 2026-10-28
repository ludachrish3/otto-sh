"""Use-case deployment against the real bed (spec §4, §6, §8, §9).

Where ``test_docker_compose.py`` drives ONE project's stack through the per-project
primitives, this module drives the layer above them: ``deploy`` / ``teardown``
merging TWO projects' fragments into one compose project on test3, with a
real/mock provider pair competing for the ``edge`` capability.

What only a real daemon can prove, and why each case is here:

* **Selection is not merge.** project1 and project2 both declare a service named
  ``edge``. If the loser's compose file joined the ``-f`` set, the merge would
  decide the winner by file order — silently. The container's IMAGE and the
  marker file baked into it are what say which fragment actually ran.
* **Both env consumption points.** ``EDGE_ADDR`` reaches the container as a
  compose ``environment:`` pass-through; ``EDGE_ORIGIN`` is interpolated into
  its value at compose PARSE time. A mapping that reached compose for one of
  the two and not the other leaves the other reading ``unset``.
* **Convergence and orphan removal.** ``up`` is convergent, so a second deploy
  must leave a running container alone; and when a provider loses, its whole
  fragment is excluded — ``--remove-orphans`` must then reap what it had left
  behind, inside the project and nowhere else.

Requires:
    vagrant up test3
"""

import json
import logging
import shlex
import uuid
from unittest.mock import patch

import pytest
import pytest_asyncio

from otto.config.project import Project
from otto.docker import deployment as deploy_mod
from otto.docker import resolve as resolve_mod
from otto.docker.compose import compose_command, compose_down_project, use_case_project
from otto.docker.deployment import deploy, teardown
from otto.docker.staging import cleanup_project
from otto.host.login_proxy import Cred
from otto.host.unix_host import UnixHost
from otto.labs.loader import Lab
from tests._fixtures.bootstrap_seam import fake_bootstrap_result
from tests._fixtures.labdata import element_for, host_data
from tests._fixtures.paths import TESTS_ROOT
from tests.conftest import active_context

PROJECT1_DIR = TESTS_ROOT / "project1"
PROJECT2_DIR = TESTS_ROOT / "project2"

#: The use-case both sample projects contribute a fragment to (spec §4's worked
#: example, as committed in their ``.otto/settings.toml``).
USE_CASE = "integration"

#: The lab test3 belongs to. Stamped onto the hand-built parent below so
#: ``use_case_project`` derives the same ``<lab>-<usecase>-<suffix>`` name the
#: CLI derives on the bed — the project segment is load-bearing (spec §9).
LAB = "unix"

# Same xdist group and same reap request as the other three docker modules:
# they share /tmp/otto-docker/<project>/ on test3 and one daemon's address
# pool. The reap is requested, never ambient
# (tests/unit/test_docker_reaper_scope.py).
pytestmark = [
    pytest.mark.xdist_group("docker_e2e"),
    pytest.mark.usefixtures("reap_orphan_docker_stacks"),
]


@pytest_asyncio.fixture
async def parent(test3_lease):
    """test3 as a docker parent, built from the fixture lab.json's own entry."""
    data = host_data("test3")
    h = UnixHost(
        ip=data["ip"],
        element=element_for("test3"),
        creds=[Cred(**c) for c in data["creds"]],
        is_virtual=True,
        term="ssh",
        transfer="scp",
        docker_capable=True,
    )
    # The loader stamps this on a lab-loaded host; a hand-built one has to say
    # so itself, and it is not cosmetic: it is the first segment of the
    # compose project (spec §9), so leaving it empty would deploy under a
    # name no `otto docker` invocation would ever produce.
    h.source_lab = LAB
    yield h
    await h.close()


@pytest.fixture
def lab(parent) -> Lab:
    """A Lab holding just test3 — the universe `deploy` places fragments into."""
    lab = Lab(name=LAB)
    lab.add_host(parent)
    return lab


@pytest.fixture
def projects() -> list[Project]:
    """The two sample projects, parsed from their committed settings.toml."""
    return [Project(root=PROJECT1_DIR), Project(root=PROJECT2_DIR)]


@pytest.fixture
def suffix() -> str:
    """A per-test compose-project suffix so concurrent developers never collide.

    ``e2e-`` is what makes the resulting project reapable by this tree's
    orphan sweep (see ``tests/integration/conftest.py``), which is how a stack
    left by an interrupted run stops accumulating toward address-pool
    exhaustion.
    """
    return "e2e-" + uuid.uuid4().hex[:8]


@pytest.fixture
def project(suffix) -> str:
    """The compose project this test's deployments will run under."""
    return use_case_project(LAB, USE_CASE, suffix)


@pytest.fixture(autouse=True)
def _deployment_config(monkeypatch, lab, projects, suffix):
    """Point ``deploy``/``teardown`` at THIS module's lab, projects and suffix.

    A run on THIS module's lab is installed and the composition root is
    patched rather than bootstrapped: ``deploy`` reads the lab off the active
    run and the run's projects from its context, which composes them through the
    root (``otto.bootstrap.bootstrap``), and bootstrapping here would import
    the sample projects' ``init`` modules into
    the test process (they register process-global providers) to obtain projects
    this fixture already holds.

    ``scope_for_project`` is patched to its TRUTHFUL answer, not a convenient
    one: neither sample project declares a ``[project]`` table, so the real
    lookup returns ``None`` for both — it just reaches it through the same
    lazy bootstrap.
    """
    monkeypatch.setenv("OTTO_COMPOSE_SUFFIX", suffix)
    with (
        active_context(lab),
        patch("otto.bootstrap.bootstrap", return_value=fake_bootstrap_result(projects)),
        patch.object(resolve_mod, "scope_for_project", return_value=None),
    ):
        yield


async def _reap(parent, project_name: str) -> None:
    """``docker compose -p <proj> down``, straight to the daemon. Best-effort.

    Deliberately NOT otto's ``teardown``: a test that failed BECAUSE
    resolution refused must still clean up, and re-entering the code under
    test to do it would inherit the same refusal and leak the stack.
    """
    await parent.exec(f"docker compose -p {shlex.quote(project_name)} down -t 1")


async def _container_of(
    parent, project_name: str, service: str, *, all_states: bool = False
) -> str:
    """The container id for one service of *project_name*, or ``""``: a running
    one, or with *all_states* one in any state (``docker ps -a``).

    Read off the daemon's own compose labels rather than off the returned
    ``UseCaseStack``: the stack is what otto believes it registered, and half
    of what these tests exist to check is whether that belief is true.
    """
    result = await parent.exec(
        f"docker ps -q{' -a' if all_states else ''} "
        f"--filter label=com.docker.compose.project={shlex.quote(project_name)} "
        f"--filter label=com.docker.compose.service={shlex.quote(service)}"
    )
    assert result.is_ok, f"docker ps failed on {parent.id}: {result.value}"
    return result.value.strip()


async def _image_of(parent, container_id: str) -> str:
    """The image a running container was created from."""
    result = await parent.exec(
        f"docker inspect -f '{{{{.Config.Image}}}}' {shlex.quote(container_id)}"
    )
    assert result.is_ok, f"docker inspect failed on {parent.id}: {result.value}"
    return result.value.strip()


async def _env_of(parent, container_id: str) -> dict[str, str]:
    """The process environment INSIDE a running container."""
    result = await parent.exec(f"docker exec {shlex.quote(container_id)} env")
    assert result.is_ok, f"docker exec env failed on {parent.id}: {result.value}"
    pairs = (line.partition("=") for line in result.value.splitlines() if "=" in line)
    return {key: value for key, _sep, value in pairs}


@pytest.mark.asyncio
async def test_merged_stack_displaces_mock(parent, project):
    """One project, three services, and the WINNER's image on the edge.

    The two projects' fragments merge into a single compose project; the mock
    edge stands down whole, so nothing of project2's ``mock-edge.yml`` reaches
    the daemon. Both of spec §6's consumption points are then read out of the
    running container.
    """
    try:
        stack = await deploy(USE_CASE, parent=parent.id, build=True)

        assert sorted(stack.hosts) == ["api", "edge", "worker"], (
            f"the merged stack must carry each service exactly once: {sorted(stack.hosts)}"
        )
        assert stack.projects == {parent.id: project}, stack.projects

        # The competition's record, as it IS: project1 won at 10, project2 stood
        # down at 0. Both priorities, neither described as "the higher one".
        (displacement,) = stack.selection.displaced
        assert displacement.capability == "edge"
        assert (displacement.winner_project, displacement.winner_priority) == ("project1", 10)
        assert (displacement.loser_project, displacement.loser_priority) == ("project2", 0)

        edge = await _container_of(parent, project, "edge")
        assert edge, f"no running `edge` container in project {project}"
        assert await _image_of(parent, edge) == "project1-api:latest", (
            "the edge container came from the MOCK's image — the loser's compose "
            "file reached the -f merge instead of being excluded whole"
        )
        # Belt and braces, from inside the container: the two images differ
        # only by the marker they bake in, so this is the same claim made
        # without trusting the image tag the daemon reports.
        marker = await parent.exec(f"docker exec {shlex.quote(edge)} cat /etc/project1-marker.txt")
        assert marker.is_ok, marker.value
        assert "project1-fixture" in marker.value, marker.value

        env = await _env_of(parent, edge)
        assert env.get("EDGE_ADDR") == parent.ip, (
            f"`${{otto:parent.addr}}` did not reach the container's environment: {env}"
        )
        assert env.get("EDGE_ORIGIN") == parent.ip, (
            f"the mapping did not reach compose at PARSE time — `EDGE_ORIGIN` fell "
            f"back to its compose-native default: {env}"
        )

        # The other two services are the unconditional fragments' own, and
        # they are running, not merely registered.
        for service in ("api", "worker"):
            assert await _container_of(parent, project, service), (
                f"{service!r} was registered but is not running in {project}"
            )
    finally:
        await _reap(parent, project)


@pytest.mark.asyncio
async def test_additive_up_converges_and_removes_orphans(parent, project):
    """A narrowed deploy, then a full one, then a losing provider.

    Three deployments into ONE project, each asserted against the daemon:

    1. ``services=["api"]`` starts just that service.
    2. A full deploy adds ``worker`` and ``edge`` and leaves ``api``'s
       container ID untouched — compose converged rather than recreated.
    3. ``provide={"edge": "project2"}`` seats the mock. project1 loses the
       capability and is excluded WHOLE, so its ``core`` goes too: ``api``
       and the real edge are both orphans of the new file set, and
       ``--remove-orphans`` reaps them inside this project.
    """
    try:
        narrowed = await deploy(USE_CASE, parent=parent.id, services=["api"], build=True)
        assert sorted(narrowed.hosts) == ["api"], sorted(narrowed.hosts)
        api_first = await _container_of(parent, project, "api")
        assert api_first, f"no running `api` container in {project}"
        for absent in ("worker", "edge"):
            assert not await _container_of(parent, project, absent), (
                f"`services=['api']` started {absent!r} as well"
            )

        full = await deploy(USE_CASE, parent=parent.id, build=True)
        assert sorted(full.hosts) == ["api", "edge", "worker"], sorted(full.hosts)
        assert await _container_of(parent, project, "api") == api_first, (
            "the convergent second deploy RECREATED the already-running api container"
        )
        real_edge = await _container_of(parent, project, "edge")
        assert real_edge, f"no running `edge` container in {project} after the full deploy"
        assert await _image_of(parent, real_edge) == "project1-api:latest"

        flipped = await deploy(USE_CASE, parent=parent.id, provide={"edge": "project2"})
        assert sorted(flipped.hosts) == ["edge", "worker"], (
            f"project1 lost `edge`, so its whole fragment — `core` included — must be "
            f"excluded: {sorted(flipped.hosts)}"
        )
        mock_edge = await _container_of(parent, project, "edge")
        assert mock_edge, f"no running `edge` container in {project} after the flip"
        assert await _image_of(parent, mock_edge) == "project2-worker:latest", (
            "`provide` did not seat the mock"
        )
        assert mock_edge != real_edge, "the real edge container was reused for the mock"
        assert not await _container_of(parent, project, "api"), (
            "--remove-orphans left the excluded project's api container running"
        )
        # The override seats a LOWER priority than what it displaced, and the
        # record says exactly that rather than claiming the winner ranked higher.
        (displacement,) = flipped.selection.displaced
        assert (displacement.winner_project, displacement.winner_priority) == ("project2", 0)
        assert (displacement.loser_project, displacement.loser_priority) == ("project1", 10)
    finally:
        await _reap(parent, project)


@pytest.mark.asyncio
async def test_teardown_removes_stack_and_hosts(parent, lab, project):
    """``teardown`` stops the containers AND unregisters the lab ids."""
    try:
        stack = await deploy(USE_CASE, parent=parent.id, build=True)
        registered = sorted(host.id for host in stack.hosts.values())
        assert registered == [f"{parent.id}.{USE_CASE}.{s}" for s in ("api", "edge", "worker")], (
            f"container ids are `<parent>.<usecase>.<service>` (spec §9): {registered}"
        )
        assert all(host_id in lab.hosts for host_id in registered)

        await teardown(USE_CASE, parent=parent.id)

        remaining = await parent.exec(
            f"docker ps -aq --filter label=com.docker.compose.project={shlex.quote(project)}"
        )
        assert remaining.is_ok, remaining.value
        assert not remaining.value.strip(), (
            f"containers survived the teardown of {project}: {remaining.value!r}"
        )
        stranded = [h for h in lab.hosts if h.startswith(f"{parent.id}.{USE_CASE}.")]
        assert not stranded, f"teardown left container hosts registered in the lab: {stranded}"
    finally:
        await _reap(parent, project)


@pytest.mark.asyncio
async def test_teardown_leaves_a_service_another_file_set_started_under_the_project(
    parent, project, suffix
):
    """A teardown removes what its deploy staged, not everything under the label.

    The extra service is started by a second file set under the SAME compose
    project, so it carries compose's own project and service labels: a
    label-wide ``down`` would take it, a ``down`` through the deploy's files
    must not.
    """
    extra_dir = f"/tmp/otto-e2e-extra-{suffix}"
    extra_file = f"{extra_dir}/extra.yml"
    try:
        await deploy(USE_CASE, parent=parent.id, build=True)
        image = await _image_of(parent, await _container_of(parent, project, "api"))
        extra_yaml = f"services:\n  extra:\n    image: {image}\n"
        wrote = await parent.exec(
            f"mkdir -p {shlex.quote(extra_dir)} && "
            f"printf %s {shlex.quote(extra_yaml)} > {shlex.quote(extra_file)}"
        )
        assert wrote.is_ok, wrote.value
        started = await parent.exec(
            f"docker compose -p {shlex.quote(project)} -f {shlex.quote(extra_file)} up -d"
        )
        assert started.is_ok, started.value
        extra = await _container_of(parent, project, all_states=True, service="extra")
        assert extra, "the extra service did not start under the project"

        report = await teardown(USE_CASE, parent=parent.id)

        # The extra service still holds the project's network, which compose
        # leaves standing; the teardown itself must still report success.
        assert report.ok, report.failed

        for service in ("api", "edge", "worker"):
            assert not await _container_of(parent, project, all_states=True, service=service), (
                f"{service} survived the teardown of {project}"
            )
        assert await _container_of(parent, project, all_states=True, service="extra") == extra, (
            "the teardown removed a container its deploy's files do not declare"
        )
    finally:
        await _reap(parent, project)
        await parent.exec(f"rm -rf {shlex.quote(extra_dir)}")


@pytest.mark.asyncio
async def test_teardown_with_the_staged_files_gone_goes_by_the_label_and_says_so(
    parent, project, caplog
):
    """With nothing staged left on the parent, the label is all there is to go by."""
    try:
        await deploy(USE_CASE, parent=parent.id, build=True)
        cleaned = await cleanup_project(parent, project)
        assert cleaned.is_ok, cleaned.value

        with caplog.at_level(logging.INFO, logger="otto.docker.deployment"):
            await teardown(USE_CASE, parent=parent.id)

        remaining = await parent.exec(
            f"docker ps -aq --filter label=com.docker.compose.project={shlex.quote(project)}"
        )
        assert remaining.is_ok, remaining.value
        assert not remaining.value.strip(), f"containers survived: {remaining.value!r}"
        assert "by its project label" in caplog.text
    finally:
        await _reap(parent, project)


@pytest.mark.asyncio
async def test_a_down_on_a_project_with_nothing_running_is_a_clean_no_op(parent, lab, suffix):
    """Both shapes of the one ``down``, against a project the daemon has never seen."""
    project = use_case_project(LAB, "empty", suffix)
    work = f"/tmp/otto-e2e-empty-{suffix}"
    compose_file = f"{work}/c.yml"
    launcher = f"{work}/otto-env.sh"
    try:
        idle_yaml = "services:\n  idle:\n    image: otto-none:never\n"
        wrote = await parent.exec(
            f"mkdir -p {shlex.quote(work)} && "
            f"printf %s {shlex.quote(deploy_mod._launcher_text({}))} > {shlex.quote(launcher)} && "
            f"printf %s {shlex.quote(idle_yaml)} > {shlex.quote(compose_file)}"
        )
        assert wrote.is_ok, wrote.value

        with_files = await compose_down_project(
            parent,
            project,
            lab=lab,
            remove_ids_under=None,
            files=[compose_file],
            launcher=launcher,
        )
        label_only = await compose_down_project(parent, project, lab=lab, remove_ids_under=None)

        assert with_files.is_ok, with_files.value
        assert label_only.is_ok, label_only.value
    finally:
        await parent.exec(f"rm -rf {shlex.quote(work)}")


@pytest.mark.asyncio
async def test_the_launcher_hands_compose_every_value_as_written_over_the_parent_env(
    parent, suffix
):
    """The staged launcher, read by the parent's real ``sh`` and compose's real parser.

    Each key is interpolated into a service's ``environment:`` and read back
    from ``docker compose config``. Two keys are also exported by the parent
    with other values (one empty), so a launcher that lost to the parent's
    environment, or a value the shell or compose re-read, fails here.
    """
    mapping = {
        "DOLLAR": "${OTHER}",
        "REPEATED": "$$x$",
        "HASH": "a # b",
        "QUOTES": "'single' \"double\"",
        "BACKSLASHES": "a\\b\\",
        "SPACES": "  padded  ",
        "EMPTY": "",
        "BACKTICK": "`id`",
        "NEWLINE": "line1\nline2",
        "IMAGE": "alpine:3",
    }
    project = use_case_project(LAB, "launcher", suffix)
    work = f"/tmp/otto-e2e-launcher-{suffix}"
    compose_file = f"{work}/c.yml"
    launcher = f"{work}/otto-env.sh"
    compose_yaml = "services:\n  probe:\n    image: ${IMAGE}\n    environment:\n" + "".join(
        f"      {key}: ${{{key}}}\n" for key in mapping
    )
    try:
        launcher_text = deploy_mod._launcher_text(mapping)
        wrote = await parent.exec(
            f"mkdir -p {shlex.quote(work)} && "
            f"printf %s {shlex.quote(launcher_text)} > {shlex.quote(launcher)} && "
            f"printf %s {shlex.quote(compose_yaml)} > {shlex.quote(compose_file)}"
        )
        assert wrote.is_ok, wrote.value

        # The parent's own exports, set in front of the command the deployment runs.
        config = await parent.exec(
            "IMAGE= HASH=from-the-parent "
            + compose_command(
                project, "config", "--format", "json", files=[compose_file], launcher=launcher
            )
        )

        assert config.is_ok, config.value
        service = json.loads(config.value)["services"]["probe"]
        # `config` prints a model compose can load again, so it writes each
        # literal `$` of a resolved value as `$$`; undo only that escape.
        environment = {k: v.replace("$$", "$") for k, v in service["environment"].items()}
        assert service["image"] == mapping["IMAGE"]
        assert environment == mapping
    finally:
        await parent.exec(f"rm -rf {shlex.quote(work)}")
