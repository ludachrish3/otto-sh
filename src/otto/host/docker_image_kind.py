"""
The built-in ``docker_image`` kind — a container image as a product.

A docker daemon drives the verbs: ``install`` loads a ``docker save`` tarball
(or verifies/pulls a reference), then ``docker run -d`` starts it with the
product's ``cov_dir`` bind-mounted at the same path; ``is_installed`` asks
whether the container is running; ``uninstall`` removes the container and,
for a tarball-loaded image, resolves that image from the CONTAINER ITSELF
(``docker container inspect -f '{{.Config.Image}}'``) and removes it too — never from
in-process memory, since the project CLI's normal lifecycle
(``otto install`` … ``otto uninstall``) runs each verb in its own process.
The bind mount is the whole coverage story: an instrumented binary inside
the container writing under ``GCOV_PREFIX=<cov_dir>`` writes onto the
daemon host, where the fetcher and the default hooks already work — docker
creates a missing bind-mount source directory itself, root-owned, so the
reset's delete is elevated (:func:`~otto.host.product.sudo_gcda_delete`) and
a non-root container image needs its ``cov_dir`` pre-created and writable
(the fixture suite's own install does this) or nothing lands in it.
``docker logs`` is hauled as ``debug/container.log`` — that is PID 1's
output only; anything a suite runs via ``docker exec`` never reaches it.
Products only.

:meth:`~otto.host.product.Product.instrumented` always answers unknown for
both forms — it never scans :attr:`~otto.host.product.ShellProduct.artifact`
(see :meth:`DockerImageProduct.instrumented`): declare ``instrumented =
true`` on the entry when the build is known to carry coverage.
"""

import shlex
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from typing_extensions import override

from ..declared import DeclaredEntry
from ..result import CommandResult, Result
from ..utils import Status, anchor_path
from .product import PRODUCT_KINDS, ShellProduct, cov_dir_of, cov_dir_of_name, sudo_gcda_delete
from .shell_kind import bool_param, str_list_param, str_param, substitute_placeholders

if TYPE_CHECKING:
    from .host import Host

TARBALL_SUFFIXES = (".tar", ".tar.gz", ".tgz")
"""An ``image`` value with one of these suffixes is a ``docker save`` tarball path."""

_VALID = "image, pull, run_args, container_name, cov_dir, instrumented, debug_log_globs"
_LOAD_TIMEOUT = 600.0
_PULL_TIMEOUT = 900.0


@dataclass
class DockerImageProduct(ShellProduct):
    """A ``kind = "docker_image"`` entry's runtime form."""

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
            return Result(Status.NotRun)
        return Result(Status.Error, msg=f"{what} failed on {host.id}: {result.value.strip()}")

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

    @override
    async def stage(self, host: "Host") -> Result:
        """Put the tarball under ``/tmp`` for ``docker load``; a reference stages nothing."""
        if not self.is_tarball:
            return Result(Status.Success)
        return await host.put(self.artifact, Path("/tmp"))  # noqa: S108 — the staging path docker load reads

    @override
    async def install(self, host: "Host") -> Result:
        probe = await host.exec("command -v docker", timeout=30)
        if probe.status is Status.NotRun:
            return Result(Status.NotRun)
        if not probe.status.is_ok:
            return Result(Status.Error, msg=f"{self.name}: docker is not on {host.id}'s PATH")
        if self.is_tarball:
            staged = f"/tmp/{self.artifact.name}"  # noqa: S108 — where stage() put it
            loaded = await host.exec(f"docker load -i {shlex.quote(staged)}", timeout=_LOAD_TIMEOUT)
            if not loaded.status.is_ok:
                return self._error_from(host, f"{self.name}: docker load", loaded)
            # The staged tarball is an intermediate `docker load` has already
            # consumed (unlike a `shell` product, whose artifact IS the
            # product) — best-effort cleanup, same as kmod's staged .ko;
            # its own failure must not fail an otherwise-successful install.
            await host.exec(f"rm -f {shlex.quote(staged)}", timeout=30)
            self.loaded_ref = _loaded_reference(loaded.value)
            if self.loaded_ref is None:
                return Result(
                    Status.Error,
                    msg=f"{self.name}: docker load reported no image on {host.id}: "
                    f"{loaded.value.strip()}",
                )
            ref = self.loaded_ref
        elif self.pull:
            pulled = await host.exec(
                f"docker pull {shlex.quote(self.image)}", timeout=_PULL_TIMEOUT
            )
            if not pulled.status.is_ok:
                return self._error_from(host, f"{self.name}: docker pull {self.image}", pulled)
            ref = self.image
        else:
            present = await host.exec(
                f"docker image inspect {shlex.quote(self.image)} >/dev/null", timeout=60
            )
            if present.status is Status.NotRun:
                return Result(Status.NotRun)
            if not present.status.is_ok:
                return Result(
                    Status.Error,
                    msg=f"{self.name}: image {self.image} is not present on {host.id}; load it "
                    "there or set `pull = true` on the entry",
                )
            ref = self.image
        cov_dir = cov_dir_of(self)
        args = f" {self.run_args}" if self.run_args else ""
        run = await host.exec(
            f"docker run -d --name {shlex.quote(self.container_name)} "
            f"-v {shlex.quote(cov_dir)}:{shlex.quote(cov_dir)}{args} {shlex.quote(ref)}",
            timeout=120,
        )
        if not run.status.is_ok:
            return self._error_from(host, f"{self.name}: docker run", run)
        return Result(Status.Success)

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
            inspected = await host.exec(
                "docker container inspect -f '{{.Config.Image}}' "
                + shlex.quote(self.container_name),
                timeout=60,
            )
            if inspected.status is Status.NotRun:
                return Result(Status.NotRun)
            if inspected.status.is_ok:
                resolved = str(inspected.value).strip()
                if resolved:
                    image_to_remove = resolved
        removed = await host.exec(f"docker rm -f {shlex.quote(self.container_name)}", timeout=120)
        if removed.status is Status.NotRun:
            return Result(Status.NotRun)
        # Idempotent: a container that was never installed (or already removed
        # by a prior, failed run) is not an uninstall failure.
        if not removed.status.is_ok and "No such container" not in removed.value:
            what = f"{self.name}: docker rm -f {self.container_name}"
            return self._error_from(host, what, removed)
        if image_to_remove is not None:
            # Only an image otto loaded from a tarball is otto's to remove;
            # a pulled or pre-existing reference stays in the daemon's cache
            # (is_tarball guards this branch, never a reference's image).
            gone = await host.exec(f"docker rmi {shlex.quote(image_to_remove)}", timeout=120)
            if not gone.status.is_ok:
                return self._error_from(host, f"{self.name}: docker rmi {image_to_remove}", gone)
            self.loaded_ref = None
        return Result(Status.Success)

    @override
    async def is_installed(self, host: "Host") -> bool:
        """Report whether the daemon says the container is running.

        A missing container or a failed (or declined) inspect is not installed.
        """
        state = await host.exec(
            "docker container inspect -f '{{.State.Running}}' " + shlex.quote(self.container_name),
            timeout=60,
        )
        return state.status.is_ok and state.value.strip() == "true"

    @override
    async def reset_coverage(self, host: "Host") -> Result:
        """Delete the .gcda files under sudo: the container wrote them as root."""
        return await sudo_gcda_delete(self, host)

    @override
    async def get_debug_logs(self, host: "Host", dest: Path) -> Result:
        """Haul the declared globs, plus ``docker logs`` as ``container.log``."""
        hauled = await super().get_debug_logs(host, dest)
        logs = await host.exec(f"docker logs {shlex.quote(self.container_name)} 2>&1", timeout=60)
        if not logs.status.is_ok:
            what = f"{self.name}: docker logs {self.container_name}"
            failure = self._error_from(host, what, logs)
            return hauled if not hauled.is_ok else failure
        (dest / "container.log").write_text(logs.value)
        return hauled


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
    run_args = str_param(entry, params, "run_args") or ""
    container_name = str_param(entry, params, "container_name")
    cov_dir = str_param(entry, params, "cov_dir")
    if cov_dir == "":
        raise ValueError(f"[[products]] {entry.name!r}: 'cov_dir' must not be empty")
    instrumented = bool_param(entry, params, "instrumented")
    debug_log_globs = str_list_param(entry, params, "debug_log_globs")
    if params:
        raise ValueError(
            f"[[products]] {entry.name!r}: kind 'docker_image' got unknown param(s): "
            f"{sorted(params)}; valid: {_VALID}"
        )
    values = {"cov_dir": cov_dir or cov_dir_of_name(entry.name), "name": entry.name}
    expanded = substitute_placeholders(entry, "run_args", run_args, values) or ""
    # `artifact` only satisfies ShellProduct's constructor here — instrumented()
    # is overridden to never scan it (see DockerImageProduct.instrumented), so
    # both forms answer unknown until `instrumented` says otherwise.
    artifact = anchor_path(Path(image), entry.base_dir) if is_tarball else Path(image)
    return DockerImageProduct(
        artifact=artifact,
        name=entry.name,
        cov_dir=cov_dir,
        debug_log_globs=debug_log_globs,
        instrumented_override=instrumented,
        image=image,
        is_tarball=is_tarball,
        pull=pull,
        run_args=expanded,
        container_name=container_name or "",
    )


PRODUCT_KINDS.register("docker_image", _docker_image_kind, origin=__name__)
