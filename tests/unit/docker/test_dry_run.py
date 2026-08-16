"""A dry run builds no image, brings up no stack, and invents no docker fact.

Spec: ``docs/superpowers/specs/2026-08-15-dry-run-contract-design.md`` §4.

``otto/docker/`` was written with no dry-run awareness at all -- the package
contained zero ``is_dry_run()`` calls -- so every one of its verbs met a dry
run in one of two wrong ways. Either it read a decline as a device fact
(``_image_exists`` answered "not present", which is the answer that
COMMISSIONS A BUILD; ``_resolve_container_id`` answered ``""``, which reads as
"not running"), or it interpolated ``result.value`` into a warning and got a
``CommandNotRunError`` thrown from a log line -- loud by accident, at a
statement that made no mistake, naming an ``rm -rf`` when the caller had asked
to bring a stack up.

Two mechanisms, one per shape, and the split is the point:

* the verbs that DRIVE the device (``build_images``, ``compose_up``,
  ``compose_down``, ``composed``, both staging functions) refuse at the TOP,
  above every device touch and above every local side effect. Only an early
  return protects the actions below it -- a hardened return value protects
  only the caller that branches on it.
* the verbs that READ a device fact (``_image_exists``, ``_stack_already_up``,
  both ``_resolve_container_id``s, ``compose_ps``) let the call reach the
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

import json
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock

import pytest

from otto.config.lab import Lab
from otto.config.repo import Repo
from otto.docker.build import _build_one, _image_exists, build_images
from otto.docker.compose import _resolve_container_id as _compose_resolve_container_id
from otto.docker.compose import (
    _stack_already_up,
    compose_down,
    compose_ps,
    compose_up,
    composed,
)
from otto.docker.staging import stage_compose_files, stage_image_context
from otto.host.docker_host import DockerContainerHost
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
        element="probe",
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
        return _ok('[{"Id": "sha256:abc"}]')
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


def _make_repo(tmp: Path, *, default_host: str = "probe_seed") -> Repo:
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
            f'default_host = "{default_host}"\n'
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
    return compose_up(repo, lab, on=parent.id)


def _call_compose_down(repo: Repo, lab: Lab, parent: UnixHost) -> Any:
    return compose_down(repo, lab, on=parent.id)


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
            async with composed(repo, lab, on=parent.id, own=True):
                entered = True

        assert entered is False, "a dry run entered the composed() body"
        assert parent.exec.await_count == 0, "a dry run drove compose through composed()"
        assert "composed(" in str(caught.value), (
            f"composed() borrowed a callee's story: {caught.value}"
        )

        # POSITIVE CONTROL, same seam: the block IS entered, a container host
        # IS yielded, and the teardown IS compensated.
        async with composed(repo, lab, on=parent.id, own=True) as hosts:
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
# THE HEADLINE: a fabricated "not present" commissioned a real build
# ---------------------------------------------------------------------------


class TestADeclinedInspectDoesNotCommissionABuild:
    """`_image_exists` folded a decline to ``False`` and ``_build_one`` BUILT.

    Driven at ``_build_one``, one level BELOW ``build_images``' arm, because
    that is where the fabrication lived and because a test that only exercised
    the arm would stay green if the fold came back. The two halves differ in
    exactly one thing -- whether the ``docker image inspect`` was a decline or
    a genuine miss -- and that is the whole claim: a MISS commissions a build,
    a NON-MEASUREMENT must not.
    """

    @pytest.mark.asyncio
    async def test_a_decline_and_a_genuine_miss_part_ways_at_the_build(self, tmp_path):
        repo = _make_repo(tmp_path)
        image = repo.docker_settings.images[0]
        settings = repo.docker_settings

        declined = await _real_decline("docker image inspect repo1-api:x")
        parent = _bare_parent()
        issued: list[str] = []

        async def inspect_declines(cmd: str, *_a, **_kw) -> CommandResult:
            issued.append(cmd)
            return declined if cmd.startswith("docker image inspect") else _ok()

        parent.exec = AsyncMock(side_effect=inspect_declines)  # type: ignore[method-assign]
        parent.put = AsyncMock(return_value=Result(Status.Success, value={}))  # type: ignore[method-assign]

        with pytest.raises(CommandNotRunError):
            await _build_one(parent, "repo1", settings, image, rebuild=False)

        assert [c for c in issued if c.startswith("docker build")] == [], (
            f"a dry run's declined inspect was read as 'no such image' and "
            f"commissioned a build: {issued}"
        )
        assert parent.put.await_count == 0, "a dry run staged a build context to the parent"

        # POSITIVE CONTROL, same function and the same seam: a GENUINE miss
        # (the daemon answered, and said no such image) still builds. Without
        # it, "no build was issued" is satisfied by a `_build_one` that never
        # builds anything.
        issued.clear()

        async def inspect_misses(cmd: str, *_a, **_kw) -> CommandResult:
            issued.append(cmd)
            return _fail("No such image") if "inspect" in cmd else _ok()

        parent.exec = AsyncMock(side_effect=inspect_misses)  # type: ignore[method-assign]
        result = await _build_one(parent, "repo1", settings, image, rebuild=False)

        assert [c for c in issued if c.startswith("docker build")], (
            f"a genuine 'no such image' stopped commissioning builds: {issued}"
        )
        assert result.status is Status.Success


# ---------------------------------------------------------------------------
# The verbs that READ a device fact: the decline is refused, not folded
# ---------------------------------------------------------------------------


class TestTheReadProbesRefuseInsteadOfInventing:
    """Each probe is fed the REAL decline, then the real device answers.

    The control half is the load-bearing one here: these probes have genuine
    falsy answers (no such image; the stack is down; the daemon errored and
    ``compose_ps`` reports none for that host) and the refusal must not have
    swallowed any of them. So every test asserts the decline raises AND that
    the ordinary answers still come back unchanged, through the same seam.
    """

    @pytest.mark.asyncio
    async def test_image_exists_refuses_a_decline_and_keeps_a_genuine_miss(self):
        parent = _bare_parent()
        parent.exec = AsyncMock(return_value=await _real_decline())  # type: ignore[method-assign]

        with pytest.raises(CommandNotRunError, match="image_exists"):
            await _image_exists(parent, "repo1-api:abc")

        # POSITIVE CONTROLS, same seam: both real answers survive.
        parent.exec = AsyncMock(return_value=_fail("No such image"))  # type: ignore[method-assign]
        assert await _image_exists(parent, "repo1-api:abc") is False
        parent.exec = AsyncMock(return_value=_ok('[{"Id":"sha"}]'))  # type: ignore[method-assign]
        assert await _image_exists(parent, "repo1-api:abc") is True

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
    async def test_compose_ps_refuses_a_decline_and_keeps_its_best_effort_fold(self):
        """The empty list is this module's own named silent-wrong shape."""
        parent = _bare_parent()
        parent.exec = AsyncMock(return_value=await _real_decline())  # type: ignore[method-assign]

        with pytest.raises(CommandNotRunError, match="compose_ps"):
            await compose_ps(parent)

        # POSITIVE CONTROLS, same seam: a failed probe still folds to [] (one
        # unreachable daemon must not hide the fleet), and a real answer still
        # parses. The refusal is scoped to the non-measurement alone.
        parent.exec = AsyncMock(return_value=_fail("permission denied"))  # type: ignore[method-assign]
        assert await compose_ps(parent) == []
        row = {"ID": "abc123", "Image": "alpine", "Status": "Up", "Names": "api"}
        parent.exec = AsyncMock(return_value=_ok(json.dumps(row) + "\n"))  # type: ignore[method-assign]
        assert await compose_ps(parent) == [row]


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
    ARMED = frozenset({"build_images", "compose_up", "compose_down", "composed"})

    #: Reach the primitive, keep its announcement, refuse the ANSWER.
    REFUSING_PROBE = frozenset({"compose_ps"})

    #: No device contact at all -- pure configuration, hashing or lab lookup.
    PURE = frozenset(
        {
            "context_hash",
            "get_container_host",
            "get_user_compose_project",
            "image_full_tag",
            "image_latest_tag",
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

    @pytest.mark.parametrize("sub", ["build", "up", "down", "ps"])
    @pytest.mark.parametrize("dry", [True, False])
    def test_the_cli_never_reaches_the_library_under_a_dry_run(self, sub, dry):
        """WHY these hazards are library-only, pinned rather than asserted in prose.

        ``otto docker`` registers with the safe default -- no
        ``dry_run_preview`` -- so Task 3's seam validates, prints the block and
        exits 0 above every one of these bodies. That is what makes the
        package's dry-run holes a LIBRARY-surface concern (suites, instructions
        and third-party embedders that import ``otto.docker`` directly), and
        it is the reason the arms above are backstops rather than the only
        thing standing between ``-n`` and a running container.

        ``get_lab`` is the seam: it is the first statement of all four bodies,
        and the patch is on ``otto.cli.docker``'s name for it, so the
        preamble's own lab access cannot be mistaken for the body's. Both
        halves are asserted in the same test -- 0 calls under ``-n``, 1
        without -- because "the body did not run" is satisfied just as well by
        a dispatch that ran nothing at all.
        """
        from unittest.mock import MagicMock, patch

        from otto.cli.docker import docker_app
        from otto.utils import DRY_RUN_HEADLINE
        from tests._fixtures.dispatch import DispatchRunner

        lab = Lab(name="test")
        get_lab = MagicMock(return_value=lab)
        reached: list[str] = []

        async def spy(*_a, **_kw):
            reached.append(sub)
            return {}

        with (
            active_context(lab=lab, dry_run=dry),
            patch("otto.cli.docker.get_lab", get_lab),
            patch("otto.cli.docker._canonicalize_on", return_value=None),
            patch("otto.cli.docker._select_repos", return_value=[]),
            patch("otto.cli.docker.build_images", AsyncMock(side_effect=spy)),
            patch("otto.cli.docker.compose_up", AsyncMock(side_effect=spy)),
            patch("otto.cli.docker.compose_down", AsyncMock(side_effect=spy)),
            patch("otto.cli.docker.compose_ps", AsyncMock(side_effect=spy)),
        ):
            result = DispatchRunner().invoke(
                docker_app, [sub], spec_name="docker", async_leaves=True
            )

        assert result.exit_code == 0, result.output
        assert reached == [], f"`otto docker {sub}` reached the library: {reached}"
        if dry:
            assert get_lab.call_count == 0, f"`otto docker {sub} -n` ran its body"
            # SUPPRESS THE PAYLOAD, NEVER THE ANNOUNCEMENT: an empty dry run
            # is a bug, so the stop has to say what it stopped.
            assert DRY_RUN_HEADLINE in result.output, result.output
            assert f"would run: docker {sub}" in " ".join(result.output.split())
        else:
            assert get_lab.call_count == 1, (
                f"`otto docker {sub}` did not run its body even WITHOUT --dry-run, "
                f"so the zero above proves nothing about the seam"
            )
            assert DRY_RUN_HEADLINE not in result.output

    @pytest.mark.asyncio
    async def test_the_pure_exports_stay_usable_under_a_dry_run(self, tmp_path):
        """SUPPRESS THE PAYLOAD, NEVER THE ANNOUNCEMENT: a preview needs these.

        Naming an image's tag or a compose project is how a caller says what
        WOULD happen. If the sweep had refused these too, a dry run would have
        nothing left to report -- and an empty dry run is a bug.
        """
        from otto.docker import context_hash, get_user_compose_project, image_full_tag

        repo = _make_repo(tmp_path)
        image = repo.docker_settings.images[0]
        with active_context(dry_run=True):
            hash_hex = context_hash(image)
            assert image_full_tag("", "repo1", image, hash_hex) == f"repo1-api:{hash_hex[:16]}"
            assert get_user_compose_project("repo1", "ci").startswith("otto-repo1-")
