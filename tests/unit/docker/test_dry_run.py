"""A dry run builds no image, brings up no stack, and invents no docker fact.

Spec: ``docs/superpowers/specs/2026-08-15-dry-run-contract-design.md`` §4.

``otto/docker/`` was written with no dry-run awareness at all -- the package
contained zero ``is_dry_run()`` calls -- so every one of its verbs met a dry
run in one of two wrong ways. Either it read a decline as a device fact
(``_resolve_container_id`` answered ``""``, which reads as "not running"), or it
interpolated ``result.value`` into a warning and got a
``CommandNotRunError`` thrown from a log line -- loud by accident, at a
statement that made no mistake, naming an ``rm -rf`` when the caller had asked
to bring a stack up.

Two mechanisms, one per shape, and the split is the point:

* the verbs that DRIVE the device (``build_images``, ``compose_up``,
  ``compose_down``, ``composed``, both staging functions) refuse at the TOP,
  above every device touch and above every local side effect. Only an early
  return protects the actions below it -- a hardened return value protects
  only the caller that branches on it.
* the verbs that READ a device fact (``_stack_already_up``,
  both ``_resolve_container_id``s, ``run_on``) let the call reach the
  primitive -- keeping its ``[DRY RUN]`` announcement -- and refuse the
  ANSWER, via ``refuse_declined_fact``. Their return types (``bool``, ``str``,
  ``list``) cannot carry "I did not look".

Discipline, as everywhere in this workstream:

* the hostile condition is INJECTED -- ``active_context(dry_run=True)`` around
  real ``Repo``/``Lab``/``UnixHost`` objects and the real product functions.
* every "did not happen" carries its POSITIVE CONTROL in the same test against
  the same seam, and the controls here SUCCEED rather than merely reaching the
  seam: a build is commissioned, a stack is registered, a container id comes
  back. "Nothing was contacted" is otherwise satisfied just as well by a verb
  that does nothing at all.
* the declines the read-probes are fed are produced by the REAL primitive
  (:func:`_real_decline`), never hand-rolled, so this file cannot keep passing
  after ``host.exec`` stops answering that shape.

Nothing here runs docker, docker compose, or touches a lab address: the parent
is a real ``UnixHost`` at a non-lab address whose ``exec``/``put`` are spies.
"""

from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock

import pytest

from otto.config.lab import Lab
from otto.config.repo import Repo
from otto.docker.build import build_images
from otto.docker.compose import _resolve_container_id as _compose_resolve_container_id
from otto.docker.compose import (
    _stack_already_up,
    compose_down,
    compose_up,
    composed,
)
from otto.docker.observe import run_on
from otto.docker.staging import stage_compose_files, stage_image_context
from otto.host.docker_host import DockerContainerHost
from otto.host.element import Element
from otto.host.login_proxy import Cred
from otto.host.unix_host import UnixHost
from otto.result import CommandNotRunError, CommandResult, NotRunResult, Result
from otto.utils import Status
from tests._fixtures.sutrepo import make_sut_repo
from tests.conftest import active_context

# ---------------------------------------------------------------------------
# Fixtures: real objects, a spied device boundary, no lab address
# ---------------------------------------------------------------------------

# Deliberately NOT 10.10.200.0/24: nothing here should ever dial, and an
# address that could reach a real lab VM makes a broken guard destructive
# instead of merely red.
_PARENT_IP = "10.0.0.1"


def _ok(out: str = "") -> CommandResult:
    return CommandResult(Status.Success, value=out, command="", retcode=0)


def _fail(out: str = "boom") -> CommandResult:
    return CommandResult(Status.Failed, value=out, command="", retcode=1)


def _bare_parent() -> UnixHost:
    return UnixHost(
        ip=_PARENT_IP,
        element=Element("probe"),
        creds=[Cred(login="u", password="p")],
        board="seed",
        docker_capable=True,
    )


async def _real_decline(cmd: str = "docker ps -q") -> NotRunResult:
    """The decline a REAL ``host.exec`` produces under a dry run.

    Not hand-built. ``CommandResult(Status.NotRun, ...)`` typed out here would
    still satisfy every guard below after the primitive stopped producing that
    shape -- and the primitive's shape (a ``NotRunResult`` whose ``value``
    raises) is precisely what the guards exist to catch. Asserted on the way
    out so this helper cannot silently become a different object.
    """
    host = _bare_parent()
    with active_context(dry_run=True):
        result = await host.exec(cmd)
    assert isinstance(result, NotRunResult), f"the primitive stopped declining: {result!r}"
    assert result.status is Status.NotRun
    with pytest.raises(CommandNotRunError):
        _ = result.value
    return result


async def _router(cmd: str, *_args: Any, **_kwargs: Any) -> CommandResult:
    """Answer the parent's commands the way a healthy docker host would.

    One router for the whole file so every POSITIVE CONTROL is a real success
    -- a build commissioned, a stack up, a container id resolved -- rather
    than "the seam was reached before it failed".
    """
    if cmd.startswith("docker image inspect"):
        return _ok("sha256:abc123abc123" + "0" * 52 + "\n")
    if cmd.startswith("docker images"):
        return _ok("api:latest abc123abc123\n")
    if "label=com.docker.compose.project=" in cmd and "service=" in cmd:
        return _ok("abc123def456\n")
    if "label=com.docker.compose.project=" in cmd:
        return _ok("")  # the stack is not up
    if "config" in cmd and "--services" in cmd:
        return _ok("api\n")
    return _ok()


def _spied_parent() -> UnixHost:
    """A real ``UnixHost`` whose two device seams are counted, never dialled."""
    parent = _bare_parent()
    parent.exec = AsyncMock(side_effect=_router)  # type: ignore[method-assign]
    parent.put = AsyncMock(return_value=Result(Status.Success, value={}))  # type: ignore[method-assign]
    return parent


def _make_repo(tmp: Path) -> Repo:
    """Every caller passes ``parent=`` explicitly, so this repo needs no placement
    of its own — [[docker.composes]] is a pure file inventory (spec §14)."""
    sut = make_sut_repo(
        tmp / "repo1",
        name="repo1",
        extra=(
            "[docker]\n"
            "\n"
            "[[docker.images]]\n"
            'name = "api"\n'
            'dockerfile = "docker/Dockerfile"\n'
            'context = "docker"\n'
            "\n"
            "[[docker.composes]]\n"
            'path = "docker/compose.yml"\n'
            'services = ["api"]\n'
        ),
        files={"docker/Dockerfile": "FROM alpine\n", "docker/compose.yml": "services: {}\n"},
    )
    return Repo(sut_dir=sut)


def _bed(tmp: Path) -> "tuple[Repo, Lab, UnixHost]":
    repo = _make_repo(tmp)
    lab = Lab(name="test")
    parent = _spied_parent()
    lab.hosts[parent.id] = parent
    return repo, lab, parent


# ---------------------------------------------------------------------------
# The verbs that DRIVE the device: nothing is asked, not even a probe
# ---------------------------------------------------------------------------


def _call_build_images(repo: Repo, lab: Lab, parent: UnixHost) -> Any:
    return build_images(repo, parent)


def _call_compose_up(repo: Repo, lab: Lab, parent: UnixHost) -> Any:
    return compose_up(repo, lab, parent=parent.id)


def _call_compose_down(repo: Repo, lab: Lab, parent: UnixHost) -> Any:
    return compose_down(repo, lab, parent=parent.id)


def _call_stage_image_context(repo: Repo, lab: Lab, parent: UnixHost) -> Any:
    return stage_image_context(parent, "repo1", repo.docker_settings.images[0])


def _call_stage_compose_files(repo: Repo, lab: Lab, parent: UnixHost) -> Any:
    return stage_compose_files(parent, "repo1", list(repo.docker_settings.composes))


_ARMED_VERBS = [
    pytest.param(_call_build_images, "build_images", id="build_images"),
    pytest.param(_call_compose_up, "compose_up", id="compose_up"),
    pytest.param(_call_compose_down, "compose_down", id="compose_down"),
    pytest.param(_call_stage_image_context, "stage_image_context", id="stage_image_context"),
    pytest.param(_call_stage_compose_files, "stage_compose_files", id="stage_compose_files"),
]


class TestTheDeviceDrivingVerbsAskTheParentNothing:
    """The arms, proven against a parent that WOULD have answered.

    The spy never declines -- it answers every command the way a healthy
    docker host does. So a passing test says something stronger than "the
    decline propagated": it says the verb short-circuited without consulting
    the device AT ALL, which is the only shape that also stops the local work
    (the build-context tarball) and the lab mutations (`lab.hosts`).

    The story is asserted too, not just the silence. A decline that names
    ``rm -rf /tmp/otto-docker/...`` when the caller asked to bring a stack up
    is the wrong-story defect this workstream keeps finding, and it is what
    every one of these verbs produced before the arms existed.
    """

    @pytest.mark.parametrize(("call", "verb"), _ARMED_VERBS)
    @pytest.mark.asyncio
    async def test_the_verb_declines_above_every_device_touch(self, tmp_path, call, verb):
        repo, lab, parent = _bed(tmp_path)

        with active_context(lab=lab, dry_run=True), pytest.raises(CommandNotRunError) as caught:
            await call(repo, lab, parent)

        assert parent.exec.await_count == 0, (
            f"a dry run ran {parent.exec.await_count} command(s) on the parent via "
            f"{verb}: {[c.args[0] for c in parent.exec.await_args_list]}"
        )
        assert parent.put.await_count == 0, f"a dry run copied files to the parent via {verb}"
        assert verb in str(caught.value), (
            f"{verb} declined with someone else's story: {caught.value}"
        )

        # POSITIVE CONTROL, same objects and the same two spies, dry run off:
        # the verb runs to COMPLETION and drives the parent. Without this,
        # "nothing was contacted" passes just as happily against a verb that
        # raises unconditionally, or one the test never actually reached.
        await call(repo, lab, parent)
        assert parent.exec.await_count > 0, (
            f"{verb} contacted nothing even WITHOUT --dry-run, so the zero above "
            f"proves nothing about the arm"
        )

    @pytest.mark.asyncio
    async def test_composed_declines_before_it_arms_a_teardown(self, tmp_path):
        """``composed`` is the package's documented entry point and gets its own arm.

        Its ``finally`` tears the stack down, so a decline raised from inside
        the ``with`` body would compensate a stack that was never brought up.
        Refusing on ``__aenter__`` means the block is never entered.
        """
        repo, lab, parent = _bed(tmp_path)
        entered = False

        with active_context(lab=lab, dry_run=True), pytest.raises(CommandNotRunError) as caught:
            async with composed(repo, lab, parent=parent.id, own=True):
                entered = True

        assert entered is False, "a dry run entered the composed() body"
        assert parent.exec.await_count == 0, "a dry run drove compose through composed()"
        assert "composed(" in str(caught.value), (
            f"composed() borrowed a callee's story: {caught.value}"
        )

        # POSITIVE CONTROL, same seam: the block IS entered, a container host
        # IS yielded, and the teardown IS compensated.
        async with composed(repo, lab, parent=parent.id, own=True) as hosts:
            entered = True
            assert "api" in hosts, f"the control never registered a container: {hosts}"
        assert entered is True
        down = [c.args[0] for c in parent.exec.await_args_list if " down" in c.args[0]]
        assert down, "the control never tore the stack down, so its `finally` proves nothing"

    @pytest.mark.asyncio
    async def test_a_repo_declaring_no_images_still_answers_honestly(self, tmp_path):
        """The arm is scoped to a real selection, not bolted to the front door.

        ``build_images`` answers ``{}`` when the repo declares no matching
        image, and that answer is settled from CONFIGURATION -- equally true
        in a dry run, and not a device fact. A guard that refused here would
        be declining to read a TOML file.
        """
        repo, lab, parent = _bed(tmp_path)

        with active_context(lab=lab, dry_run=True):
            assert await build_images(repo, parent, image_names=["nosuchimage"]) == {}
        assert parent.exec.await_count == 0

        # POSITIVE CONTROL: the same call naming a DECLARED image refuses,
        # so the {} above is the empty selection talking and not a dry run
        # that quietly answers {} for everything.
        with active_context(lab=lab, dry_run=True), pytest.raises(CommandNotRunError):
            await build_images(repo, parent, image_names=["api"])


# ---------------------------------------------------------------------------
# The verbs that READ a device fact: the decline is refused, not folded
# ---------------------------------------------------------------------------


class TestTheReadProbesRefuseInsteadOfInventing:
    """Each probe is fed the REAL decline, then the real device answers.

    The control half is the load-bearing one here: these probes have genuine
    falsy answers (no such image; the stack is down; a daemon errored and
    ``run_on`` reports its error for that host) and the refusal must not have
    swallowed any of them. So every test asserts the decline raises AND that
    the ordinary answers still come back unchanged, through the same seam.
    """

    @pytest.mark.asyncio
    async def test_stack_already_up_refuses_a_decline_and_keeps_its_three_states(self):
        """``None`` means "the probe ran and could not tell" -- not "nobody asked".

        ``compose_up`` answers unknown by running the convergent ``up -d``, so
        folding a decline into ``None`` buys an ACTION with a shrug.
        """
        parent = _bare_parent()
        parent.exec = AsyncMock(return_value=await _real_decline())  # type: ignore[method-assign]

        with pytest.raises(CommandNotRunError, match="stack_already_up"):
            await _stack_already_up(parent, "otto-repo1-x")

        parent.exec = AsyncMock(return_value=_ok("abc123\n"))  # type: ignore[method-assign]
        assert await _stack_already_up(parent, "otto-repo1-x") is True
        parent.exec = AsyncMock(return_value=_ok(""))  # type: ignore[method-assign]
        assert await _stack_already_up(parent, "otto-repo1-x") is False
        parent.exec = AsyncMock(return_value=_fail("daemon down"))  # type: ignore[method-assign]
        assert await _stack_already_up(parent, "otto-repo1-x") is None

    @pytest.mark.asyncio
    async def test_compose_resolve_container_id_refuses_instead_of_polling(self):
        """Four bounded polls for a container nobody asked docker about."""
        parent = _bare_parent()
        parent.exec = AsyncMock(return_value=await _real_decline())  # type: ignore[method-assign]

        with pytest.raises(CommandNotRunError, match="resolve_container_id"):
            await _compose_resolve_container_id(parent, "proj", "api")

        assert parent.exec.await_count == 1, (
            f"the decline was polled {parent.exec.await_count} times instead of refused once"
        )

        # POSITIVE CONTROL, same seam: a real id still resolves.
        parent.exec = AsyncMock(return_value=_ok("abc123def456\n"))  # type: ignore[method-assign]
        assert await _compose_resolve_container_id(parent, "proj", "api") == "abc123def456"

    @pytest.mark.asyncio
    async def test_run_on_refuses_a_decline_and_keeps_a_failed_daemons_error(self):
        """An empty listing is a fact about a daemon; a decline measured none."""
        parent = _bare_parent()
        parent.exec = AsyncMock(return_value=await _real_decline("docker ps"))  # type: ignore[method-assign]

        with pytest.raises(CommandNotRunError, match="list_containers"):
            await run_on([parent], "docker ps", asked="list_containers")

        # POSITIVE CONTROLS, same seam: a failed daemon is reported, not hidden
        # and not refused; a real listing comes back whole.
        parent.exec = AsyncMock(return_value=_fail("permission denied"))  # type: ignore[method-assign]
        report = await run_on([parent], "docker ps", asked="list_containers")
        assert not report.ok
        assert report.hosts[0].result.value == "permission denied"
        parent.exec = AsyncMock(return_value=_ok("CONTAINER ID\n"))  # type: ignore[method-assign]
        report = await run_on([parent], "docker ps", asked="list_containers")
        assert report.ok
        assert report.hosts[0].result.value == "CONTAINER ID\n"


class TestIsRunningStopsAnsweringForAContainerItNeverAskedAbout:
    """``is_running() -> bool`` cannot carry "I did not look", so it declines.

    otto's own two callers (``tunnel.manage`` and ``tunnel.discovery``) go
    through ``_device_running``, which refuses one level above -- verified by
    ``grep``: they are the only ``is_running()`` call sites in ``src/``. This
    is therefore a LIBRARY-surface backstop, and it is tested where the
    fabrication is born (``_resolve_container_id``) so both callers inherit
    it, including ``_ensure_running`` -> ``_auto_up`` -> ``compose_up``, which
    is the path that STARTS A CONTAINER.
    """

    def _container(self, parent: UnixHost) -> DockerContainerHost:
        return DockerContainerHost(
            parent=parent,
            container_id="",  # the declared-but-unresolved placeholder
            project="repo1",
            service="api",
            compose_project="otto-repo1-vagrant",
        )

    @pytest.mark.asyncio
    async def test_a_declined_probe_is_not_a_container_that_is_down(self):
        parent = _bare_parent()
        parent.exec = AsyncMock(return_value=await _real_decline())  # type: ignore[method-assign]
        host = self._container(parent)

        with pytest.raises(CommandNotRunError, match="is_running"):
            await host.is_running()

        assert host.container_id == "", "a dry run cached an id it never resolved"

        # POSITIVE CONTROLS, same host and the same seam: a container that is
        # genuinely down still answers False, and a live one still answers
        # True and caches its id. Without these, "it raised" is satisfied by
        # an `is_running` that raises for everybody.
        parent.exec = AsyncMock(return_value=_ok(""))  # type: ignore[method-assign]
        assert await host.is_running() is False
        parent.exec = AsyncMock(return_value=_ok("abc123def456\n"))  # type: ignore[method-assign]
        assert await host.is_running() is True
        assert host.container_id == "abc123def456"


# ---------------------------------------------------------------------------
# The package-wide pin: a new export cannot skip the adjudication
# ---------------------------------------------------------------------------


class TestEveryPublicDockerExportIsAdjudicated:
    """The set, pinned, because the defect was package-wide and not per-site.

    ``otto/docker/`` reached this workstream with zero ``is_dry_run()`` calls
    in it -- not one verb that had been thought about. A per-site fix leaves
    the next export free to arrive with the same omission, so the public
    surface is enumerated and each name is filed under how it answers a dry
    run. Adding an export makes this red until someone decides which it is.
    """

    #: Refuse at the top, above every device touch (an arm).
    #:
    #: ``deploy``/``teardown``/``deployed`` arm BELOW their pure prefix
    #: (select, place, validate) and above the first build/stage/up, so a
    #: dry run still reports the resolved plan and still fires every
    #: configuration refusal -- but they are device-contacting verbs, never
    #: PURE, and the arm is what makes that safe.
    ARMED = frozenset(
        {
            "build_images",
            "build_on",
            "compose_build",
            "compose_up",
            "compose_down",
            "composed",
            "deploy",
            "deployed",
            "follow_logs",
            "teardown",
        }
    )

    #: Reach the primitive and refuse the ANSWER. With ``LogMode.QUIET`` the
    #: observe execs' ``[DRY RUN]`` announcement goes to ``verbose.log`` only;
    #: the refusal still names the probe (``list_containers(test3)``).
    REFUSING_PROBE = frozenset(
        {
            "list_containers",
            "list_images",
            "compose_ps",
            "compose_logs",
            "container_logs",
        }
    )

    #: No device contact at all -- pure configuration, lab lookup, or a
    #: data/exception type that never itself runs a command.
    PURE = frozenset(
        {
            "AdapterResult",
            "UseCaseStack",
            "get_container_host",
            "get_user_compose_project",
            "register_compose_adapter",
            "DockerVerbError",
            "HostOutput",
            "ObserveReport",
            "LogsTarget",
            "resolve_compose_logs",
            "resolve_logs",
            "default_docker_parent",
            "docker_parent",
            "docker_parents",
            "BuildOptions",
            "BuildReport",
            "FailedImage",
            "HostReport",
            "ImageBuild",
            "RepoBuild",
            "TeardownReport",
        }
    )

    def test_the_public_surface_is_exactly_these_three_groups(self):
        from otto import docker

        assert set(docker.__all__) == self.ARMED | self.REFUSING_PROBE | self.PURE, (
            "otto.docker's public surface changed. Every export must be filed as "
            "ARMED (refuses at the top), REFUSING_PROBE (refuses the answer) or "
            "PURE (touches no device) -- the package's original defect was that "
            "nobody had asked the question of any of them."
        )

    @pytest.mark.parametrize(
        ("argv", "seam"),
        [
            (["ps"], "otto.docker.observe.list_containers"),
            (["images"], "otto.docker.observe.list_images"),
            (["compose", "ps", "integration"], "otto.docker.observe.compose_ps"),
            (["compose", "logs", "integration"], "otto.docker.observe.compose_logs"),
            (["logs", "x"], "otto.docker.observe.container_logs"),
        ],
        ids=lambda v: " ".join(v) if isinstance(v, list) else None,
    )
    @pytest.mark.parametrize("dry", [True, False])
    def test_the_cli_never_reaches_the_library_under_a_dry_run(self, argv, seam, dry):
        """WHY this hazard is library-only, pinned rather than asserted in prose.

        ``ps``, ``images``, ``compose ps`` and ``compose logs`` register with the
        safe default -- no ``dry_run_preview`` -- so the CLI dry-run seam validates,
        prints the block and exits 0 above its body.
        That is what makes the package's dry-run holes a LIBRARY-surface
        concern (suites, instructions and third-party embedders that import
        ``otto.docker`` directly), and it is the reason the arms above are
        backstops rather than the only thing standing between ``-n`` and a
        running container.

        ``build``/``compose build``/``up``/``down`` are DELIBERATELY not in
        this set: they all stamp ``__cli_dry_run_preview__`` (the two build
        rows alongside the two deploy rows) so ``build_on``/
        ``compose_build``/``deploy``/``teardown`` run their pure halves and
        decline with spec §12's resolved plan. Their opt-in is pinned by
        ``tests/unit/docker/test_verb_table.py`` and their reach-the-library
        behavior by ``TestTheDeployVerbsOwnTheirDryRunPreview`` below -- so the
        parameters removed here did not just vanish.

        The verb's library function is the seam: it is patched at its home in
        ``otto.docker.observe`` (the leaf imports it inside its body, so the
        preamble cannot be mistaken for the body). Both halves are asserted in
        the same test -- never reached under ``-n``, reached once without --
        because "the body did not run" is satisfied just as well by a dispatch
        that ran nothing at all.
        """
        from unittest.mock import patch

        from otto.cli.docker import docker_app
        from otto.docker.observe import ObserveReport
        from otto.utils import DRY_RUN_HEADLINE
        from tests._fixtures.dispatch import DispatchRunner

        verb = " ".join(argv)
        reached: list[str] = []

        async def spy(*_a, **_kw):
            reached.append(verb)
            return ObserveReport([])

        with (
            active_context(lab=Lab(name="test"), dry_run=dry),
            patch(seam, AsyncMock(side_effect=spy)),
        ):
            result = DispatchRunner().invoke(
                docker_app, argv, spec_name="docker", async_leaves=True
            )

        if dry:
            assert reached == [], f"`otto docker {verb} -n` reached the library: {reached}"
            assert result.exit_code == 0, result.output
            # SUPPRESS THE PAYLOAD, NEVER THE ANNOUNCEMENT: an empty dry run
            # is a bug, so the stop has to say what it stopped.
            assert DRY_RUN_HEADLINE in result.output, result.output
            assert f"would run: docker {verb}" in " ".join(result.output.split())
        else:
            assert reached == [verb], (
                f"`otto docker {verb}` did not run its body even WITHOUT --dry-run, "
                f"so the absence above proves nothing about the seam"
            )
            assert DRY_RUN_HEADLINE not in result.output
            assert result.exit_code == 0, result.output

    @pytest.mark.parametrize(
        ("argv", "resolver"),
        [
            (["logs", "x", "-f"], "otto.docker.observe.resolve_logs"),
            (["compose", "logs", "integration", "-f"], "otto.docker.observe.resolve_compose_logs"),
        ],
        ids=lambda v: " ".join(v) if isinstance(v, list) else None,
    )
    @pytest.mark.parametrize("dry", [True, False])
    def test_a_follow_never_reaches_the_bridge_under_a_dry_run(self, argv, resolver, dry):
        """``logs -f`` is two seams: the resolver, then the bridge.

        The parametrized reach test above patches one seam, and without ``-n``
        a ``-f`` leaf stops at the resolver (which would find no lab), so the
        follow gets its own pair: the bridge seam is never reached under
        ``-n`` and is reached once without it.
        """
        from unittest.mock import patch

        from otto.cli.docker import docker_app
        from otto.docker.observe import LogsTarget
        from otto.utils import DRY_RUN_HEADLINE
        from tests._fixtures.dispatch import DispatchRunner

        target = LogsTarget(object(), "docker logs x")
        resolved = (
            patch(resolver, AsyncMock(return_value=target))
            if resolver.endswith("resolve_logs")
            else patch(resolver, return_value=[target])
        )
        follow = AsyncMock(return_value=None)
        with (
            active_context(lab=Lab(name="test"), dry_run=dry),
            resolved,
            patch("otto.docker.observe.follow_logs", follow),
        ):
            result = DispatchRunner().invoke(
                docker_app, argv, spec_name="docker", async_leaves=True
            )

        assert result.exit_code == 0, result.output
        assert follow.await_count == (0 if dry else 1)
        if dry:
            assert DRY_RUN_HEADLINE in result.output, result.output
            # the preview spells the flag in its long form
            would = " ".join(argv).replace(" -f", " --follow")
            assert f"would run: docker {would}" in " ".join(result.output.split())
        else:
            assert DRY_RUN_HEADLINE not in result.output

    def test_the_pure_exports_stay_usable_under_a_dry_run(self):
        """SUPPRESS THE PAYLOAD, NEVER THE ANNOUNCEMENT: a preview needs these.

        Naming a compose project is how a caller says what WOULD happen. If
        the sweep had refused these too, a dry run would have nothing left to
        report -- and an empty dry run is a bug.
        """
        from otto.docker import get_user_compose_project

        with active_context(dry_run=True):
            assert get_user_compose_project("repo1", "ci").startswith("otto-repo1-")


class TestTheDeployVerbsOwnTheirDryRunPreview:
    """``up``/``down`` opt OUT of the seam default so the plan is reachable.

    The seam exits 0 with a generic block above every leaf body unless the leaf
    stamps ``__cli_dry_run_preview__``. Without the opt-in, ``otto --dry-run
    docker up integration`` would never reach ``deploy``, and spec §12's
    resolved plan -- the exact compose command -- would be unreachable from the
    CLI no matter how good the library's decline is.

    Asserted through the production dispatch (``DispatchRunner``), not by
    reading the marker: the marker is only interesting if the seam honors it.
    """

    @pytest.mark.parametrize(("sub", "verb"), [("up", "deploy"), ("down", "teardown")])
    def test_the_body_runs_under_a_dry_run_and_the_plan_is_printed(self, sub, verb):
        from unittest.mock import patch

        from otto.cli.docker import docker_app
        from otto.result import CommandNotRunError
        from tests._fixtures.dispatch import DispatchRunner

        decline = CommandNotRunError(
            f"{verb}(integration)", "test3", "Resolved plan: test3 <- repo1[core]."
        )
        called: "list[str]" = []

        async def _spy(*_a, **_kw):
            called.append(verb)
            raise decline

        with (
            active_context(lab=Lab(name="unix"), dry_run=True),
            patch(f"otto.docker.deployment.{verb}", AsyncMock(side_effect=_spy)),
        ):
            result = DispatchRunner().invoke(
                docker_app, ["compose", sub, "integration"], spec_name="docker", async_leaves=True
            )

        assert called == [verb], (
            f"`otto docker compose {sub} -n` never reached {verb}(): {result.output}"
        )
        assert result.exit_code == 0, result.output
        assert "Resolved plan: test3" in " ".join(result.output.split()), result.output

    @pytest.mark.parametrize(
        ("argv", "seam"),
        [
            (["build", "--parent", "test3"], "otto.docker.build_verbs.build_on"),
            (["compose", "build", "integration"], "otto.docker.build_verbs.compose_build"),
        ],
    )
    def test_a_build_verb_also_runs_under_a_dry_run_and_the_plan_is_printed(self, argv, seam):
        """``build``/``compose build`` carry the same opt-in as ``up``/``down``.

        Both now stamp ``__cli_dry_run_preview__`` (the ``_VERBS`` rows set
        ``dry_run_preview=True``), so their bodies reach ``build_on``/
        ``compose_build`` under ``-n`` instead of seam-stopping above them --
        the same proof as the deploy verbs above, for the build verbs.
        """
        from unittest.mock import patch

        from otto.cli.docker import docker_app
        from otto.result import CommandNotRunError
        from tests._fixtures.dispatch import DispatchRunner

        decline = CommandNotRunError("build_on(test3)", "test3", "Build plan: test3 <- repo1[api].")
        called: "list[str]" = []

        async def _spy(*_a, **_kw):
            called.append(seam)
            raise decline

        with (
            active_context(lab=Lab(name="unix"), dry_run=True),
            patch(seam, AsyncMock(side_effect=_spy)),
        ):
            result = DispatchRunner().invoke(
                docker_app, argv, spec_name="docker", async_leaves=True
            )

        assert called == [seam], f"{' '.join(argv)!r} -n never reached {seam}: {result.output}"
        assert result.exit_code == 0, result.output
        assert "Build plan: test3" in " ".join(result.output.split()), result.output
