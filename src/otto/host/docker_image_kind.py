"""
The built-in ``docker_image`` kind — a container image as a product.

A docker daemon drives the verbs: ``install`` loads a ``docker save`` tarball
(or verifies/pulls a reference), removes a stopped or never-started container
of the same name (``docker rm -v``, never ``-f``: docker refuses an active one
and install fails with its message), then ``docker run -d`` starts it with the
product's ``cov_dir`` bind-mounted at the same path; ``is_installed`` asks
whether the container is running; ``uninstall`` removes the container and,
for a tarball-loaded image, resolves that image from the CONTAINER ITSELF
(its ``.Config.Image``, through ``otto.host.docker_cli.inspect_container``)
and removes it too — never from
in-process memory, since the project CLI's normal lifecycle
(``otto install`` … ``otto uninstall``) runs each verb in its own process.
The bind mount is the whole coverage story, and for an instrumented
product (``is_instrumented``, the predicate cleanup and
the fetch ask) otto sets both gcov variables on the ``docker run`` it owns:
``-e GCOV_PREFIX=<cov_dir>`` (the same path inside the container as on the
daemon host, through the same-path mount) and ``-e GCOV_PREFIX_STRIP=<n>``
from the product's ``build_root``
(:attr:`~otto.host.product.Product.gcov_prefix_strip`). An uninstrumented
product gets neither: nothing would clean or fetch what it wrote. Both flags follow
``run_args``, just before the image, so they win over any ``--env-file``
entry and an image ``ENV``; ``run_args`` that sets either variable itself
is refused at lab load (:func:`refuse_gcov_env`). An instrumented binary
the container starts therefore writes onto the daemon host, where the
fetcher and the default hooks already work — docker creates a missing
bind-mount source directory itself, root-owned, so the kind's ``cov_dir``
is root-owned (:attr:`~otto.host.product.Product.cov_dir_root_owned`):
the reset's delete and cleanup's sweep run elevated, and a non-root
container image needs its ``cov_dir`` pre-created and writable (the
fixture suite's own install does this) or nothing lands in it. The
counter reset keeps that dir even when it empties it
(:attr:`~otto.host.product.Product.cov_dir_bind_mounted`): removing a
bind mount's source leaves the running container writing into a deleted
directory, and every counter it wrote afterwards would be lost.
``docker logs`` is hauled as ``debug/container.log`` — that is PID 1's
output only; anything a suite runs via ``docker exec`` never reaches it.
Products only.

:meth:`~otto.host.product.Product.instrumented` always answers unknown for
both forms — it never scans :attr:`~otto.host.product.ShellProduct.artifact`
(see :meth:`DockerImageProduct.instrumented`): declare ``instrumented =
true`` on the entry when the build is known to carry coverage.
"""

import logging
import shlex
from collections.abc import Collection
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING, ClassVar

from typing_extensions import override

from ..declared import DeclaredEntry
from ..result import CommandResult, Result
from ..utils import Status, anchor_path
from .docker_cli import (
    image_id_of,
    image_rm_command,
    inspect_container,
    missing,
    rm_container_command,
)
from .product import (
    ProductPlan,
    ShellProduct,
    cov_dir_of,
    ensure_stage_dir,
    is_instrumented,
    planned_stage_dir,
    stage_lines,
)
from .shell_kind import (
    bool_param,
    build_root_param,
    coverage_params,
    placeholder_values,
    refuse_unknown_params,
    stage_dir_param,
    str_param,
    substitute_placeholders,
)

if TYPE_CHECKING:
    from .host import Host

logger = logging.getLogger(__name__)

TARBALL_SUFFIXES = (".tar", ".tar.gz", ".tgz")
"""An ``image`` value with one of these suffixes is a ``docker save`` tarball path."""

_VALID = (
    "image, stage_dir, pull, run_args, container_name, cov_dir, instrumented, debug_log_globs, "
    "build_root"
)
_GCOV_ENV = {
    "GCOV_PREFIX": "to the cov_dir",
    "GCOV_PREFIX_STRIP": "from build_root",
}
"""The gcov variables otto sets on ``docker run``, each with how it sets it (for the refusal)."""

_DOCKER_RUN_VALUE_SHORTS = frozenset("acehlmpuvw")
"""``docker run``'s short flags that take a value (``a`` is ``--attach``).

The one list ``_env_operands`` reads a ``docker run`` cluster with; a
check over another command's args passes that command's set instead."""

_LOAD_TIMEOUT = 600.0
_PULL_TIMEOUT = 900.0


@dataclass
class DockerImageProduct(ShellProduct):
    """A ``kind = "docker_image"`` entry's runtime form."""

    cov_dir_root_owned: ClassVar[bool] = True
    """docker creates the bind-mount source as root, and the container writes as its own user."""

    cov_dir_bind_mounted: ClassVar[bool] = True
    """``docker run`` mounts the cov_dir: the reset keeps it, emptied, for the running container."""

    image: str = ""
    """The reference to run, or the tarball path as declared (see :attr:`is_tarball`)."""

    is_tarball: bool = False
    """Whether :attr:`artifact` is a ``docker save`` tarball; else :attr:`image` is a reference."""

    pull: bool = False
    """``docker pull`` first; default requires the image already present. Reference form only."""

    run_args: str = ""
    """Extra ``docker run`` arguments, placeholders already expanded. Interpolated raw, unlike
    :attr:`image`/:attr:`container_name`/``cov_dir`` — this is the declarer's own shell text, not
    a single token, so ``shlex.quote`` is never applied to it."""

    container_name: str = ""
    """``--name``; defaults to the product name."""

    loaded_ref: str | None = None
    """What ``docker load`` reported for a tarball — :meth:`install`'s own ``docker run`` uses
    this, same-process. :meth:`uninstall` does NOT read it: a separate ``otto uninstall`` process
    never saw this instance's ``install()``, so it resolves the image to remove from the running
    container itself instead (see :meth:`uninstall`)."""

    def __post_init__(self) -> None:
        super().__post_init__()
        if not self.container_name:
            self.container_name = self.name

    @staticmethod
    def _error_from(host: "Host", what: str, result: CommandResult) -> Result:
        """Build an ``Error`` naming *what* failed, or propagate a dry-run decline unread.

        *result* is a command's own answer: reading its ``.value`` when it declined
        (``Status.NotRun``) raises (:class:`~otto.result.NotRunResult`'s whole point), so the
        decline is checked and returned first, before anything touches ``.value``.
        """
        if result.status is Status.NotRun:
            return Result(status=Status.NotRun)
        return Result(
            status=Status.Error, msg=f"{what} failed on {host.id}: {result.value.strip()}"
        )

    @override
    def instrumented(self) -> bool | None:
        """Return :attr:`instrumented_override` — never scan :attr:`artifact`.

        A tarball's suffix is always an :data:`~otto.host.product.ARCHIVE_SUFFIXES`
        member, so the inherited scan never opens it anyway; a reference's
        ``artifact`` is a bare, CWD-relative :class:`~pathlib.Path` built only
        to satisfy :class:`~otto.host.product.ShellProduct`'s constructor —
        scanning it would run :func:`~otto.host.product.scan_for_instrumentation`
        over whatever otto's own working directory happens to hold at that
        name, never a property of the declared image. Neither form's artifact
        is meaningful to scan; declare ``instrumented = true`` when the build
        is known to carry coverage.
        """
        return self.instrumented_override

    @property
    @override
    def stages_artifact(self) -> bool:
        """Answer True for a tarball entry only — a reference is pulled, never staged."""
        return self.is_tarball

    async def _staged_tarball(self, host: "Host") -> str:
        """Where the tarball is -- the one path ``stage``, ``docker load`` and the cleanup share."""
        return str(await self.resolved_stage_dir(host) / self.artifact.name)

    # One source per docker command: ``install``/``uninstall`` run these and
    # ``plan`` renders them, so the quoting cannot drift between the two.

    def _load_command(self, staged: str) -> str:
        """``docker load`` of the staged tarball at *staged* (quoted here)."""
        from .shell_quote import path_operand  # function-local: off the host import path

        return f"docker load -i {path_operand(staged)}"

    def _cleanup_command(self, staged: str) -> str:
        """Remove the staged tarball at *staged* (quoted here)."""
        from .shell_quote import path_operand  # function-local: off the host import path

        return f"rm -f {path_operand(staged)}"

    def _pull_command(self) -> str:
        """``docker pull`` of :attr:`image`."""
        return f"docker pull {shlex.quote(self.image)}"

    def _run_command(self, ref: str) -> str:
        """``docker run -d`` of *ref*, which the caller has ALREADY shell-quoted.

        Already quoted so a plan can pass a bare placeholder such as
        ``<loaded image>`` where the hook passes the real, quoted reference.
        """
        cov_dir = shlex.quote(cov_dir_of(self))
        args = f" {self.run_args}" if self.run_args else ""
        # Only a product otto cleans and fetches gets the gcov variables: an
        # uninstrumented one's counters would land on the daemon host where
        # nothing ever removes them.
        gcov_env = (
            f" -e GCOV_PREFIX={cov_dir} -e GCOV_PREFIX_STRIP={self.gcov_prefix_strip}"
            if is_instrumented(self)
            else ""
        )
        return (
            f"docker run -d --name {shlex.quote(self.container_name)} "
            f"-v {cov_dir}:{cov_dir}{args}{gcov_env} {ref}"
        )

    def _rm_command(self, *, force: bool) -> str:
        """Remove the container and its anonymous volumes; *force* also stops an active one."""
        return rm_container_command(self.container_name, force=force)

    def _image_rm_command(self, image: str) -> str:
        """Remove *image* (unquoted; the shared builder quotes it)."""
        return image_rm_command([image])

    @override
    async def stage(self, host: "Host") -> Result:
        """Put the tarball under :attr:`stage_dir` for ``docker load``.

        A reference stages nothing. ``put`` is handed the RESOLVED, absolute
        directory — the same one ``install``'s ``docker load -i`` names.
        """
        if not self.is_tarball:
            return Result(status=Status.Success)
        directory = await self.resolved_stage_dir(host)
        made = await ensure_stage_dir(host=host, directory=directory, who=f"product {self.name!r}")
        if not made.is_ok:
            return made
        return await host.put(self.artifact, directory)

    @override
    async def install(self, host: "Host") -> Result:
        probe = await host.exec("command -v docker", timeout=30)
        if probe.status is Status.NotRun:
            return Result(status=Status.NotRun)
        if not probe.status.is_ok:
            return Result(
                status=Status.Error, msg=f"{self.name}: docker is not on {host.id}'s PATH"
            )
        if self.is_tarball:
            staged = await self._staged_tarball(host)  # where stage() put it
            loaded = await host.exec(self._load_command(staged), timeout=_LOAD_TIMEOUT)
            if not loaded.status.is_ok:
                return self._error_from(host, f"{self.name}: docker load", loaded)
            # The staged tarball is an intermediate `docker load` has already
            # consumed (unlike a `shell` product, whose artifact IS the
            # product) — best-effort cleanup, same as kmod's staged .ko;
            # its own failure must not fail an otherwise-successful install.
            await host.exec(self._cleanup_command(staged), timeout=30)
            self.loaded_ref = _loaded_reference(loaded.value)
            if self.loaded_ref is None:
                return Result(
                    status=Status.Error,
                    msg=f"{self.name}: docker load reported no image on {host.id}: "
                    f"{loaded.value.strip()}",
                )
            ref = self.loaded_ref
        elif self.pull:
            pulled = await host.exec(self._pull_command(), timeout=_PULL_TIMEOUT)
            if not pulled.status.is_ok:
                return self._error_from(host, f"{self.name}: docker pull {self.image}", pulled)
            ref = self.image
        else:
            present = await image_id_of(host, self.image)
            if present.result.status is Status.NotRun:
                return Result(status=Status.NotRun)
            if present.id is None:
                return Result(
                    status=Status.Error,
                    msg=f"{self.name}: image {self.image} is not present on {host.id}; load it "
                    "there or set `pull = true` on the entry",
                )
            ref = self.image
        replaced = await self._remove_stopped(host)
        if replaced is not None:
            return replaced
        run = await host.exec(self._run_command(shlex.quote(ref)), timeout=120)
        if not run.status.is_ok:
            return self._error_from(host, f"{self.name}: docker run", run)
        return Result(status=Status.Success)

    async def _remove_stopped(self, host: "Host") -> Result | None:
        """Remove a stopped or never-started container of the name; ``None`` lets run follow.

        Plain ``docker rm -v`` (no ``-f``): docker refuses a running, paused or
        restarting container, and that refusal is returned with docker's own text.
        No container of the name is nothing to remove.
        """
        removed = await host.exec(self._rm_command(force=False), timeout=120)
        if removed.status is Status.NotRun:
            return Result(status=Status.NotRun)
        if removed.status.is_ok:
            logger.info(
                f"{host.id}: {self.name}: removed stopped container {self.container_name} "
                "before install"
            )
            return None
        if missing(removed, "container"):
            return None
        return self._error_from(host, f"{self.name}: {self._rm_command(force=False)}", removed)

    @override
    async def uninstall(self, host: "Host") -> Result:
        image_to_remove: str | None = None
        if self.is_tarball:
            # Resolve the image from the CONTAINER, not from `loaded_ref`:
            # `otto uninstall` is normally its own process, with no memory
            # of a separate `otto install`'s `loaded_ref`. Whatever image
            # the container is currently running IS otto's to remove — it
            # was loaded by install() (a reference never reaches here, see
            # below). A container that is already gone (or never existed)
            # has no image to resolve; docker rm -f below is then a no-op.
            inspected = await inspect_container(host, self.container_name)
            if inspected.declined:
                return Result(status=Status.NotRun)
            if inspected.state is not None and inspected.state.image_ref:
                image_to_remove = inspected.state.image_ref
        removed = await host.exec(self._rm_command(force=True), timeout=120)
        if removed.status is Status.NotRun:
            return Result(status=Status.NotRun)
        # Idempotent: a container that was never installed (or already removed
        # by a prior, failed run) is not an uninstall failure.
        if not removed.status.is_ok and not missing(removed, "container"):
            return self._error_from(host, f"{self.name}: {self._rm_command(force=True)}", removed)
        if image_to_remove is not None:
            # Only an image otto loaded from a tarball is otto's to remove;
            # a pulled or pre-existing reference stays in the daemon's cache
            # (is_tarball guards this branch, never a reference's image).
            remove = self._image_rm_command(image_to_remove)
            gone = await host.exec(remove, timeout=120)
            if not gone.status.is_ok:
                return self._error_from(host, f"{self.name}: {remove}", gone)
            self.loaded_ref = None
        return Result(status=Status.Success)

    @override
    def plan(self, host: "Host") -> ProductPlan:
        """Describe :meth:`stage`, :meth:`install` and :meth:`uninstall` as the plan contract asks.

        The contract is :ref:`product-kind-plan` in
        :doc:`/extend/dry-run-contract`.

        Every command is rendered by the same builder the hook runs, so the
        quoting is one expression, not two copies; the two placeholders stand
        for the two answers only the daemon can give.
        """
        host_id = getattr(host, "id", "?")
        plan = ProductPlan(unchecked=[f"that docker is on {host_id}'s PATH (command -v docker)"])
        if self.is_tarball:
            uninstall = [
                self._rm_command(force=True),
                self._image_rm_command("<image the container ran>"),
            ]
            image_gap = (
                "the image to remove, read from the container (docker container inspect "
                f"{self.container_name}); no running container means no `docker image rm`"
            )
            where = planned_stage_dir(
                stage_dir=self.stage_dir, host=host, who=f"product {self.name!r}"
            )
            if where.refusal is not None:
                # The refusal is the install's: uninstall needs no stage dir.
                return ProductPlan(uninstall=uninstall, unchecked=[where.refusal, image_gap])
            if where.unchecked is not None:
                plan.unchecked.insert(0, where.unchecked)
            staged = str(PurePosixPath(where.directory) / self.artifact.name)
            plan.stage = stage_lines(host=host, local=self.artifact, directory=where.directory)
            plan.install = [
                self._load_command(staged),
                self._cleanup_command(staged),
                self._rm_command(force=False),
                self._run_command("<loaded image>"),
            ]
            plan.uninstall = uninstall
            plan.unchecked += [
                (
                    "the reference `docker load` prints, which `docker run` then names "
                    "(<loaded image>)"
                ),
                image_gap,
            ]
            return plan
        if self.pull:
            plan.install = [self._pull_command()]
        else:
            plan.unchecked.append(
                f"that {self.image} is present on {host_id} (docker image inspect); a missing "
                "image fails the install unless the entry sets `pull = true`"
            )
        plan.install += [self._rm_command(force=False), self._run_command(shlex.quote(self.image))]
        plan.uninstall = [self._rm_command(force=True)]
        return plan

    @override
    async def is_installed(self, host: "Host") -> bool:
        """Report whether the daemon says the container is running.

        A missing container or a failed (or declined) inspect is not installed.
        """
        inspected = await inspect_container(host, self.container_name)
        return inspected.state is not None and inspected.state.running

    @override
    async def get_debug_logs(self, host: "Host", *, dest: Path) -> Result:
        """Haul the declared globs, plus ``docker logs`` as ``container.log``.

        A container that is already gone (an earlier uninstall removed it) has
        no logs to collect: that is noted at INFO and is not a failure, so a
        second cleanup succeeds like the first. Any other ``docker logs``
        failure is reported.
        """
        hauled = await super().get_debug_logs(host, dest=dest)
        logs = await host.exec(f"docker logs {shlex.quote(self.container_name)} 2>&1", timeout=60)
        if not logs.status.is_ok and missing(logs, "container"):
            host_id = getattr(host, "id", "?")
            logger.info(
                f"{host_id}: {self.name}: container {self.container_name} is gone; "
                "no docker logs to collect"
            )
            return hauled
        if not logs.status.is_ok:
            what = f"{self.name}: docker logs {self.container_name}"
            failure = self._error_from(host, what, logs)
            return hauled if not hauled.is_ok else failure
        (dest / "container.log").write_text(logs.value)
        return hauled


def _env_operands(tokens: list[str], value_shorts: Collection[str]) -> list[str]:
    """Return the operands *tokens* hand to ``-e``/``--env``, in order.

    *tokens* are one command's arguments, already shell-split. ``--env X`` and
    ``--env=X`` name ``X``; ``--env-file`` is a different flag. A short-flag
    token is read letter by letter: the first letter in *value_shorts* claims
    the operand -- the rest of the token if any is left (one leading ``=``
    stripped, as pflag does), else the next token -- and the token ends there.
    An operand claimed by any other letter is consumed and never re-read as a
    flag, so ``-l '-eX=1'`` names nothing. A long flag other than ``--env`` is
    not known to take a value, so the token after it is read as a flag in its
    own right. That makes the walk inexact in both directions, for values no
    real entry writes: ``--label -eGCOV_PREFIX=x`` is read as an env operand
    (docker gives the token to ``--label``), and ``--label -p -e GCOV_PREFIX=1``
    is not (docker gives ``-p`` to ``--label``, then sets the variable). A
    table of docker's long flags would close both, at the cost of a second
    list to drift from the docker version. The missed case changes nothing:
    otto's own ``-e`` flags come after ``run_args`` and docker keeps the last
    ``-e`` per variable, so otto's value wins anyway
    (``test_otto_env_follows_run_args`` pins the order).
    """
    found: list[str] = []
    i = 0
    while i < len(tokens):
        arg = tokens[i]
        i += 1
        if arg == "--env":
            if i < len(tokens):
                found.append(tokens[i])
                i += 1
            continue
        if arg.startswith("--env="):
            found.append(arg.removeprefix("--env="))
            continue
        if arg.startswith("--") or not arg.startswith("-"):
            continue
        letters = arg[1:]
        for j, letter in enumerate(letters):
            if letter not in value_shorts:
                continue
            rest = letters[j + 1 :]
            if rest:
                operand: str | None = rest.removeprefix("=")
            elif i < len(tokens):
                operand = tokens[i]
                i += 1
            else:
                operand = None
            if letter == "e" and operand is not None:
                found.append(operand)
            break
    return found


def refuse_gcov_env(entry: DeclaredEntry, run_args: str) -> None:
    """Refuse *run_args* that sets ``GCOV_PREFIX`` or ``GCOV_PREFIX_STRIP``.

    *run_args* is the EXPANDED string, so a placeholder that becomes one of
    the names is caught too. Otto sets both on the ``docker run`` it owns,
    after ``run_args``; the only thing that could compete is another ``-e``
    for the same name, and two answers to one variable is the defect, so
    every spelling ``_env_operands`` reads is refused. ``--env-file`` is
    allowed: docker reads it before any ``-e``, so otto's flags win.
    """
    who = f"[[{entry.seam}]] {entry.name!r}"
    try:
        tokens = shlex.split(run_args)
    except ValueError as e:
        raise ValueError(f"{who}: 'run_args' cannot be split as shell words ({e})") from e
    for operand in _env_operands(tokens, _DOCKER_RUN_VALUE_SHORTS):
        var = operand.split("=", 1)[0]
        if var in _GCOV_ENV:
            raise ValueError(
                f"{who}: 'run_args' must not set {var}: otto sets it on docker run "
                f"{_GCOV_ENV[var]} (found {operand!r} in the expanded run_args)"
            )


def _loaded_reference(output: str) -> str | None:
    """Return the reference ``docker load`` printed: ``Loaded image[ ID]: <ref>``."""
    for raw_line in output.splitlines():
        line = raw_line.strip()
        if line.startswith("Loaded image: "):
            return line[len("Loaded image: ") :]
        if line.startswith("Loaded image ID: "):
            return line[len("Loaded image ID: ") :]
    return None


def _docker_image_kind(entry: DeclaredEntry, host: "Host") -> DockerImageProduct:  # noqa: ARG001 — factory signature; any host with a shell can front a daemon, install checks for docker
    """Build a :class:`DockerImageProduct` from a validated entry's params."""
    params = dict(entry.params)
    image = str_param(entry, params, "image", required=True)
    assert image is not None  # noqa: S101 — required=True raises above when missing
    if not image:
        raise ValueError(f"[[products]] {entry.name!r}: 'image' must not be empty")
    is_tarball = image.lower().endswith(TARBALL_SUFFIXES)
    pull = bool_param(entry, params, "pull") or False
    if is_tarball and pull:
        raise ValueError(
            f"[[products]] {entry.name!r}: 'pull' only applies to a reference image, not a tarball"
        )
    stage_dir = stage_dir_param(entry, params)
    run_args = str_param(entry, params, "run_args") or ""
    container_name = str_param(entry, params, "container_name")
    cov = coverage_params(entry, params)
    build_root = build_root_param(entry, params)
    refuse_unknown_params(entry, params, "kind 'docker_image'", _VALID)
    values = placeholder_values(entry, cov.cov_dir, build_root)
    expanded = substitute_placeholders(entry, "run_args", run_args, values) or ""
    refuse_gcov_env(entry, expanded)
    # `artifact` only satisfies ShellProduct's constructor here — instrumented()
    # is overridden to never scan it (see DockerImageProduct.instrumented), so
    # both forms answer unknown until `instrumented` says otherwise.
    artifact = anchor_path(Path(image), entry.base_dir) if is_tarball else Path(image)
    return DockerImageProduct(
        artifact=artifact,
        name=entry.name,
        stage_dir=stage_dir,
        cov_dir=cov.cov_dir,
        build_root=build_root,
        debug_log_globs=cov.debug_log_globs,
        instrumented_override=cov.instrumented,
        image=image,
        is_tarball=is_tarball,
        pull=pull,
        run_args=expanded,
        container_name=container_name or "",
    )
