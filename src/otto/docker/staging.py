"""
File staging onto a parent host.

Building an image and running ``docker compose`` both require getting the
relevant files (Dockerfile, build context, compose YAML) onto the parent
host that runs the docker daemon. This module wraps that with a small,
predictable layout under the parent's private staging root,
``/tmp/otto-docker-<login>/`` (:func:`~otto.host.docker_host.staging_root`),
which :func:`~otto.host.docker_host.ensure_private_root` creates with mode
0700, or verifies, before every write there. Under the root, each tree is
keyed by a compose project name: the per-project stack's
(:func:`~otto.docker.compose.get_user_compose_project`, which also keys a
project's builds) or a use-case's
(:func:`~otto.docker.compose.use_case_project`). Both carry the
``OTTO_COMPOSE_SUFFIX`` (or user), so one account's sessions on one parent
under distinct suffixes never share a tree. Two sessions of one account with
the same suffix, or with none set, share one: independent sessions need
distinct suffixes.

Layout, under the root::

    <key>/
        build/<image>/<context-as-tar>     # extracted build context
        build/<image>.<archive>            # an archive context, uploaded unopened
        compose/<n>/<basename>.yml         # one numbered dir per compose file
        compose/<n>/<sidecar relative path> # env_file: sidecars, same numbering
        compose/otto-env.sh                # the use case's env launcher

Every path builder takes the root as its first argument, so the layout is
one decision whichever root it lands under: the private one, or the older
shared one a teardown may still read back from
(:func:`trusted_legacy_staging`).

Who removes what, under the private root:

- ``build/`` — the build removes its staged input, archive or context, once
  ``docker build`` has run, and the dirs that leaves empty. Best-effort: a
  failed removal is a warning, and the next stage recovers from it.
- the whole ``<key>/`` tree of a compose stack — never ``down``: it is the
  record a later ``down`` reads back to scope what it removes.
- every tree a project owns — ``otto run cleanup``, through
  :func:`cleanup_project` (:func:`~otto.docker.compose.remove_project_staging`),
  except a stack's while that stack still has containers. A stack's removal
  checks that in the same shell command (:func:`stack_cleanup_command`); it
  does not lock the tree, so cleanup must not run while the same stack (same
  user and suffix) is being deployed or built. Nothing removes staging under
  the older shared root; that stays a manual step.

The directory layout is stable across runs so nothing leaks into
per-invocation subdirs.
"""

import logging
import os
import re
import tarfile
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from ..config.scope import DockerCompose, DockerImage
from ..host.docker_host import (
    OwnershipProbe,
    ensure_private_root,
    parent_staging_root,
    probe_ownership,
)
from ..host.errors import HostCommandError, HostUnreachableError, StagingRefusedError
from ..host.host import Host, is_dry_run
from ..host.shell_quote import path_operand
from ..result import CommandNotRunError, CommandResult

if TYPE_CHECKING:
    from collections.abc import Sequence

    from ..host.recursive_transfer import RemoteListing
    from ..host.unix_host import UnixHost

logger = logging.getLogger(__name__)

_LEGACY_ROOT = Path("/tmp/otto-docker")  # noqa: S108 — the shared root otto used before 0.18
"""Where otto before 0.18 staged for every account on a parent, world-readable.

Nothing is written here any more; a teardown reads a deployment staged here
back only through :func:`trusted_legacy_staging`."""


def _staging_failure(
    result: CommandResult, message: str
) -> HostUnreachableError | HostCommandError:
    """Pick the error type for a failed staging command; the text is the caller's.

    A command killed by its timeout never delivered a verdict, so it is an
    unreachable-host failure; anything else ran on the parent and reported
    the failure itself. Only for :meth:`~otto.host.host.Host.exec` results —
    ``put`` returns a plain :class:`~otto.result.Result`, which carries no
    ``timed_out`` to ask about.
    """
    return HostUnreachableError(message) if result.timed_out else HostCommandError(message)


def compose_staging_root(root: Path, project: str) -> Path:
    """Per-project staging directory under the parent's staging *root*."""
    return root / project


def stage_key(name: str) -> str:
    """One path segment for an image *name*, which may carry a registry, port and path."""
    return re.sub(r"[^A-Za-z0-9_.-]+", "_", name)


def build_root(root: Path, project: str) -> Path:
    """Where *project*'s build inputs are staged under the parent's staging *root*."""
    return compose_staging_root(root, project) / "build"


def image_build_dir(root: Path, project: str, image_name: str) -> Path:
    """Where this image's context will live on the parent."""
    return build_root(root, project) / stage_key(image_name)


def compose_dir(root: Path, project: str) -> Path:
    """Per-project compose staging directory on the parent host."""
    return compose_staging_root(root, project) / "compose"


def image_archive_path(root: Path, project: str, image: DockerImage) -> Path:
    """Where an archive context is uploaded on the parent."""
    return build_root(root, project) / f"{stage_key(image.name)}.{image.context.name}"


async def stage_image_archive(parent: Host, project: str, image: DockerImage) -> Path:
    """Copy an archive context to the parent unchanged; return its path there.

    Raises:
        ~otto.result.CommandNotRunError: this is a dry run.
    """
    if is_dry_run():
        raise CommandNotRunError(
            f"stage_image_archive({project}/{image.name})",
            getattr(parent, "id", ""),
            "Nothing was copied to the parent.",
        )
    remote = image_archive_path(await ensure_private_root(parent), project, image)
    prepared = await parent.exec(f"mkdir -p {path_operand(remote.parent)}")
    if not prepared.status.is_ok:
        raise _staging_failure(
            prepared, f"failed to prepare {remote.parent} on the parent: {prepared.value}"
        )
    put_result = await parent.put([image.context], remote.parent)
    if not put_result.is_ok:
        raise HostCommandError(f"failed to copy the build archive to the parent: {put_result.msg}")
    uploaded = remote.parent / image.context.name
    moved = await parent.exec(f"mv -f {path_operand(uploaded)} {path_operand(remote)}")
    if not moved.status.is_ok:
        raise _staging_failure(moved, f"failed to place the build archive: {moved.value}")
    return remote


async def stage_image_context(
    parent: Host,
    project: str,
    image: DockerImage,
) -> Path:
    """Tar the build context locally, ship it to the parent, untar it.

    Returns the absolute path on the parent of the extracted context
    directory. The Dockerfile is included verbatim under its declared
    name so ``docker build -f`` resolves it.

    Raises:
        ~otto.result.CommandNotRunError: this is a dry run. Staging is four
            device touches (``rm -rf``, ``mkdir``, a ``put``, an untar) and a
            ``Path`` cannot carry "I staged nothing", so the only honest
            answer is to decline -- above the local tar as well as the remote
            copy, since a dry run should not write a tarball either.
    """
    if is_dry_run():
        raise CommandNotRunError(
            f"stage_image_context({project}/{image.name})",
            getattr(parent, "id", ""),
            "Nothing was tarred locally and nothing was copied to the parent.",
        )

    remote_dir = image_build_dir(await ensure_private_root(parent), project, image.name)

    # Wipe and recreate to avoid mixing leftover files from an earlier build.
    # Checked, because `&&` makes a failed rm skip the mkdir silently and the
    # `tar -xf` below OVERLAYS rather than replaces: docker build would then
    # see a context still holding a file the user deleted locally, and produce
    # an image that still holds the deleted file.
    prepared = await parent.exec(
        f"rm -rf {path_operand(remote_dir)} && mkdir -p {path_operand(remote_dir)}"
    )
    if not prepared.status.is_ok:
        raise _staging_failure(
            prepared,
            f"failed to prepare the build-context dir {remote_dir} on the parent: {prepared.value}",
        )

    with tempfile.NamedTemporaryFile(
        prefix=f"otto-docker-{image.name}-",
        suffix=".tar",
        delete=False,
    ) as tmp:
        tmp_path = Path(tmp.name)

    try:
        with tarfile.open(tmp_path, "w") as tar:
            # Add the entire context with relative arcnames.
            tar.add(image.context, arcname=".")
            # If the Dockerfile lives outside the context, include it as
            # well at the path the user declared (relative to context).
            try:
                image.dockerfile.relative_to(image.context)
            except ValueError:
                tar.add(image.dockerfile, arcname=image.dockerfile.name)

        put_result = await parent.put([tmp_path], remote_dir)
        if not put_result.is_ok:
            raise HostCommandError(f"failed to stage build context to parent: {put_result.msg}")

        # Extract on parent. Unbounded on purpose: this command's duration IS
        # the transfer (extracting the just-uploaded build context), which
        # scales with its size — a wall-clock bound here is meaningless
        # (see nc.py).
        remote_tar = remote_dir / tmp_path.name
        result = await parent.exec(
            f"tar -xf {path_operand(remote_tar)} -C {path_operand(remote_dir)} "
            f"&& rm -f {path_operand(remote_tar)}",
            timeout=float("inf"),
        )
        if not result.status.is_ok:
            # No timed-out arm: the timeout above is `inf` by design, so this
            # can only be the untar itself reporting failure.
            raise HostCommandError(f"failed to extract build context on parent: {result.value}")
    finally:
        tmp_path.unlink(missing_ok=True)

    return remote_dir


async def stage_compose_files(
    parent: Host,
    project: str,
    composes: list[DockerCompose],
    *,
    warn: bool = True,
) -> list[Path]:
    """Copy compose files to numbered directories on the parent.

    Numbered directories preserve the order the project listed them
    (which determines override precedence in ``docker compose -f a -f b``).
    Returns the absolute paths on the parent in the same order.

    *warn* gates the relative-bind-source advisory. ``compose_down`` re-runs
    this function purely to recover the ``-f`` paths for teardown, by which
    point this call's own ``rm -rf`` at the top has ALREADY wiped the
    directory the advisory would name -- so there it is a warning about a
    loss that has happened, on a path that is tearing the stack down anyway.
    The callers that actually stand a stack up keep the default.

    Raises:
        ~otto.result.CommandNotRunError: this is a dry run -- same reasoning
            as :func:`stage_image_context`, and the same reason the refusal
            is at the top rather than on the first result: ``compose_down``
            catches ``RuntimeError`` around this call, and
            ``CommandNotRunError`` IS one, so a decline raised from inside
            here is swallowed and re-reported as a failed tear-down.
    """
    if is_dry_run():
        raise CommandNotRunError(
            f"stage_compose_files({project})",
            getattr(parent, "id", ""),
            "No compose file was copied to the parent.",
        )

    base = compose_dir(await ensure_private_root(parent), project)
    prepared = await parent.exec(f"rm -rf {path_operand(base)} && mkdir -p {path_operand(base)}")
    if not prepared.status.is_ok:
        raise _staging_failure(
            prepared,
            f"failed to prepare the compose staging dir {base} on the parent: {prepared.value}",
        )

    out: list[Path] = []
    for idx, compose in enumerate(composes):
        sub = base / str(idx)
        made = await parent.exec(f"mkdir -p {path_operand(sub)}")
        if not made.status.is_ok:
            raise _staging_failure(made, f"failed to create {sub} on the parent: {made.value}")
        try:
            authored = compose.path.read_text()
        except (OSError, ValueError):
            # OSError: missing/unreadable file. ValueError: `read_text` raises
            # UnicodeDecodeError (a ValueError subclass) on non-UTF-8 bytes --
            # `compose_down` wraps this call in `except RuntimeError` so a
            # staging failure cannot interrupt a teardown sweep; a ValueError
            # would sail straight through that guard. Either way, the put()
            # below reports an unreadable/unstageable compose file.
            authored = ""
        if warn:
            # Index 0's directory, not `sub`: `compose_command` is given only `-f`
            # and no `--project-directory`, so compose resolves EVERY
            # relative bind source (whichever file declares it) against the
            # directory of the first `-f` file, not each file's own.
            warn_relative_bind_sources(compose.path.name, authored, base / "0")
        put_result = await parent.put([compose.path], sub)
        if not put_result.is_ok:
            raise HostCommandError(f"failed to stage compose file {compose.path}: {put_result.msg}")
        out.append(sub / compose.path.name)
    return out


STAGING_KEPT = "otto-staging-kept"
"""What :func:`stack_cleanup_command` prints when the stack had containers: its staging stays."""

STAGING_UNREADABLE = "otto-staging-unreadable"
"""What :func:`stack_cleanup_command` prints when the stack's containers could not be listed."""


async def cleanup_project(parent: Host, project: str, *, stack: bool) -> CommandResult:
    """Remove the staging tree under *project* on the parent. Best-effort.

    The tree is under the parent's private staging root, which is verified
    first (:func:`~otto.host.docker_host.ensure_private_root`): a root otto
    cannot trust raises rather than have ``rm -rf`` run through it.

    A compose stack's tree (*stack*) goes through :func:`stack_cleanup_command`,
    which checks the stack's containers in the same shell command as the
    removal; any other key's through :func:`cleanup_command`. Returns the
    command's own :class:`~otto.result.CommandResult` — callers that care check
    ``.is_ok`` and :func:`cleanup_outcome`; best-effort callers ignore it.
    """
    root = await ensure_private_root(parent)
    command = stack_cleanup_command(root, project) if stack else cleanup_command(root, project)
    return await parent.exec(command)


def cleanup_command(root: Path, project: str) -> str:
    """Return the ``rm -rf`` of *project*'s tree under *root*: what cleanup removes, previewed."""
    return f"rm -rf {path_operand(compose_staging_root(root, project))}"


def stack_cleanup_command(root: Path, project: str) -> str:
    """Return :func:`cleanup_command` guarded by the stack's own container check, as one command.

    The survey (:func:`~otto.docker.compose.staging_to_remove`) decides which
    trees to remove, but runs before the removal, as separate commands. A
    stack whose containers appeared since (a deployment of the same key) keeps
    its staging: the removal asks again, in the same shell, and prints
    :data:`STAGING_KEPT` instead of removing; a listing that fails prints
    :data:`STAGING_UNREADABLE`. This narrows the window between the check and
    the ``rm -rf`` to one shell command; it does not exclude a concurrent
    deployment, so cleanup must not run while the same stack (same user and
    suffix) is being deployed or built.
    """
    from ..host.docker_host import stack_containers_command  # lazy: only cleanup needs it

    return (
        f"if c=$({stack_containers_command(project, all=True)}); then "
        f'if [ -z "$c" ]; then {cleanup_command(root, project)}; else echo {STAGING_KEPT}; fi; '
        f"else echo {STAGING_UNREADABLE}; fi"
    )


def cleanup_outcome(result: CommandResult) -> "str | None":
    """Return :data:`STAGING_KEPT` or :data:`STAGING_UNREADABLE` when *result* ends with one."""
    lines = result.value.strip().splitlines()
    last = lines[-1].strip() if lines else ""
    return last if last in (STAGING_KEPT, STAGING_UNREADABLE) else None


def build_input_cleanup_command(root: Path, project: str, path: Path) -> str:
    """Return the removal of one staged build input, then of the dirs it leaves empty.

    The ``rmdir`` is quiet and never fails the command: a dir another input or
    the compose staging still uses is not empty, and stays. The private *root*
    itself is never removed.
    """
    emptied = (
        f"{path_operand(build_root(root, project))} "
        f"{path_operand(compose_staging_root(root, project))}"
    )
    return f"rm -rf {path_operand(path)} && {{ rmdir {emptied} 2>/dev/null || true; }}"


def present_roots_command(root: Path, projects: "Sequence[str]") -> str:
    """Return a command printing each of *projects*' trees under *root* that exists, one a line."""
    trees = " ".join(path_operand(compose_staging_root(root, p)) for p in projects)
    return f'for d in {trees}; do [ -e "$d" ] && echo "$d"; done; true'


@dataclass
class ComposeFileToStage:
    """One compose file's FINAL text (adapter-rendered or verbatim) plus its origin dir."""

    handle: str
    text: str
    source_dir: Path


@dataclass
class StagedUseCase:
    """Where staging put things on the parent."""

    compose_paths: list[Path]
    launcher: Path


_COMPOSE_SUFFIX = ".yml"
"""The suffix every staged use-case compose file carries; the read-back keys on it."""


def use_case_compose_paths(
    root: Path, compose_project: str, files: "list[ComposeFileToStage]"
) -> list[Path]:
    """Where :func:`stage_use_case` will put each rendered compose file. PURE.

    The parent-side layout, computed without touching the parent, so a caller
    that has settled a deployment but must not contact a device -- a dry run
    rendering spec §12's exact compose command -- can name the ``-f`` paths
    the real run would use. :func:`stage_use_case` returns THIS function's
    answer rather than accumulating its own, so the preview and the staging
    cannot drift: one of them changing the layout changes both.
    """
    base = compose_dir(root, compose_project)
    return [base / str(idx) / f"{f.handle}{_COMPOSE_SUFFIX}" for idx, f in enumerate(files)]


def use_case_launcher(root: Path, compose_project: str) -> Path:
    """Where :func:`stage_use_case` will put the use case's env launcher. PURE.

    Every compose call of the deployment runs under it (``sh <launcher>
    docker compose …``). Same reasoning as :func:`use_case_compose_paths`.
    """
    return compose_dir(root, compose_project) / "otto-env.sh"


def _resolution_error(message: str) -> Exception:
    """Build the use-case-staging refusal, importing its class function-scope.

    ``UseCaseResolutionError`` (``otto.docker.resolve``) already means exactly
    this: "a configuration error, settled before anything is staged or
    started" — reused here rather than a bare ``ValueError`` so the docker
    use-case error taxonomy is one decision, not two.
    Imported function-scope (not at module level) so a bare
    ``from .staging import stage_compose_files`` — what ``compose.py`` does
    today, and the only caller before this function existed — does not also
    pull ``otto.docker.resolve`` and its ``otto.config.scope`` import; only a
    caller that actually reaches a refusal pays for it. It still subclasses
    ``ValueError``, so every existing ``pytest.raises(ValueError, ...)`` and
    any caller's ``except ValueError`` keeps working unchanged.
    """
    from .resolve import UseCaseResolutionError

    return UseCaseResolutionError(message)


def _refuse_if_path_escapes(prefix: str, rel: str) -> None:
    """Refuse a relative reference that is absolute or climbs above its staging root.

    Compose accepts an absolute ``env_file:`` path (resolved against the
    filesystem, not the compose file) and a ``../`` reference (resolved
    against the compose file's own directory) — both legal Compose syntax.
    Staging cannot honor either without writing somewhere on the parent
    outside the parent's staging root (an absolute ref) or above the staging root
    the reference is supposed to land under (a ``../`` ref that normalizes
    outside it) — spec §8 constraint 5 says sidecars stay under ``<n>/``, so
    both are a loud, distinct refusal instead of a silent escape. Shared by
    project-committed ``env_file:`` refs (whose root is a numbered ``<n>/`` dir)
    and unclaimed ``extra_files`` keys (whose root is ``compose/`` itself) —
    both name a path relative to a staging subtree and need the identical
    guard, just phrased without assuming which root it is.
    """
    normalized = Path(os.path.normpath(rel))
    if Path(rel).is_absolute() or normalized.parts[:1] == ("..",):
        raise _resolution_error(
            f"{prefix} {rel!r}, which is absolute or escapes its staging "
            "root — it must resolve underneath it"
        )


def _env_file_refs(handle: str, ef: object) -> list[str]:
    """Normalize one service's ``env_file:`` value into a list of relative paths.

    Every Compose Spec shape is handled: a bare string, a list of strings,
    and the long form (``{path: ..., required: ...}``, Compose Spec >= 2.24)
    — dropping any of these silently is the exact defect spec §8 exists to
    close, so an unrecognized shape is a loud refusal naming the handle, not
    a skip.
    """
    if ef is None:
        return []
    if isinstance(ef, str):
        return [ef]
    if isinstance(ef, list):
        out: list[str] = []
        for entry in ef:
            if isinstance(entry, str):
                out.append(entry)
            elif isinstance(entry, dict) and isinstance(entry.get("path"), str):
                out.append(entry["path"])
            else:
                raise _resolution_error(
                    f"compose file {handle!r}: unrecognized env_file entry {entry!r} "
                    "— expected a string, or a mapping with a string 'path' key"
                )
        return out
    raise _resolution_error(
        f"compose file {handle!r}: unrecognized env_file value {ef!r} — expected "
        "a string, or a list of strings and/or mappings with a string 'path' key. "
        "A bare mapping is not a valid env_file value: the long form is a LIST "
        "entry (env_file: [{path: ...}]), never the value itself."
    )


def _collect_env_file_refs(handle: str, text: str) -> list[str]:
    """Relative env_file references in a rendered compose text (spec §8).

    The rendered text must be valid YAML and its top level must be a mapping
    — that is the adapter contract; a template that is only valid post-render
    belongs INSIDE the adapter.
    """
    import yaml  # function-scope: keep staging import-light

    try:
        doc = yaml.safe_load(text)
    except yaml.YAMLError as e:
        raise _resolution_error(
            f"compose file {handle!r}: rendered text is not valid YAML — the "
            f"adapter must return a fully rendered file: {e}"
        ) from e
    if doc is not None and not isinstance(doc, dict):
        raise _resolution_error(
            f"compose file {handle!r}: rendered text's top level is a "
            f"{type(doc).__name__}, not a mapping — the adapter must return "
            "a fully rendered compose document"
        )
    refs: list[str] = []
    services = (doc or {}).get("services") or {}
    for svc in services.values() if isinstance(services, dict) else []:
        if not isinstance(svc, dict):
            continue
        refs.extend(_env_file_refs(handle, svc.get("env_file")))
    return refs


def _relative_bind_sources(handle: str, text: str) -> "list[str]":
    """Return the relative bind-mount sources declared in a rendered compose text.

    Only sources that are RELATIVE to the compose file's directory --
    absolute ones are the author's own business (§ the design's silent
    case), and a bare name is a named volume, not a bind.
    """
    import yaml  # function-scope: keep staging import-light

    try:
        doc = yaml.safe_load(text)
    except yaml.YAMLError as e:
        raise _resolution_error(
            f"compose file {handle!r}: rendered text is not valid YAML — the "
            f"adapter must return a fully rendered file: {e}"
        ) from e
    if doc is not None and not isinstance(doc, dict):
        raise _resolution_error(
            f"compose file {handle!r}: rendered text's top level is a "
            f"{type(doc).__name__}, not a mapping — the adapter must return "
            "a fully rendered compose document"
        )
    out: "list[str]" = []
    services = (doc or {}).get("services") or {}
    for svc in services.values() if isinstance(services, dict) else []:
        if not isinstance(svc, dict):
            continue
        volumes = svc.get("volumes")
        if not isinstance(volumes, list):
            continue  # a mapping or bare string here is not valid Compose; nothing to walk
        for entry in volumes:
            source = _volume_source(entry)
            # "$" means compose will interpolate this at `up` time from the
            # env file; a static walk genuinely cannot resolve it, and otto
            # must not warn about a path it has not evaluated.
            if source and "$" not in source and source.startswith(("./", "../")):
                out.append(source)
    return out


def _volume_source(entry: object) -> "str | None":
    """Return the host-side source of one ``volumes:`` entry, if it is a bind."""
    if isinstance(entry, str):
        return entry.split(":", 1)[0] or None
    if isinstance(entry, dict):
        if (entry.get("type") or "volume") != "bind":
            return None
        source = entry.get("source")
        return source if isinstance(source, str) and source else None
    return None


def warn_relative_bind_sources(handle: str, text: str, staged_dir: Path) -> None:
    """Warn that a relative bind source follows the compose file into staging.

    Not a refusal: the mount works, and a product may even want it. What it
    cannot survive is the NEXT deploy, and nothing in the author's own file
    says so.

    Advisory all the way down -- a text this cannot parse produces no
    warning and no error. ``stage_compose_files`` ships project-authored files
    verbatim and has never parsed them, so letting an advisory walk turn an
    unreadable compose file into a staging refusal would be a behaviour
    change smuggled in behind a warning. The use-case path keeps its own
    refusal in ``_collect_env_file_refs``, where it belongs.
    """
    try:
        sources = _relative_bind_sources(handle, text)
    except Exception:  # noqa: BLE001 -- advisory only; docker reports a bad compose file
        return
    for source in sources:
        resolved = Path(os.path.normpath(str(staged_dir / source)))
        logger.warning(
            f"[docker] compose file {handle!r} binds the relative source {source!r}, "
            rf"which resolves to {resolved} on the parent — otto re-creates that staging "
            rf"directory on every deploy, so anything written there is lost. Use an "
            rf"absolute path on the parent, or a named volume."
        )


def _dest_for_relative_name(root: Path, rel: str) -> Path:
    """Where a relative name lands under *root*, preserving its own subdirectory.

    Shared by env_file sidecars (``root`` is a numbered ``<n>/`` dir) and
    unreferenced ``extra_files`` entries (``root`` is ``compose/`` itself) —
    both need "preserve the relative dir, don't flatten" and nothing else.
    """
    rel_parent = Path(rel).parent
    return root / rel_parent if str(rel_parent) != "." else root


def _read_back(listing: "RemoteListing", root: Path, compose_project: str) -> StagedUseCase:
    """Read a walk of :func:`compose_dir` under *root* as :func:`stage_use_case` leaves it.

    Pure. Exactly one compose file (``<handle>`` plus the layout's suffix)
    directly in every numbered directory, the numbers contiguous from 0, and
    the env launcher at the top. Everything else is ignored: a sidecar of another
    suffix, anything nested deeper, an unreferenced extra at the top.

    Raises:
        ValueError: what is there is not such a staging, named in the message.
    """
    if listing.kind != "d":
        raise ValueError("it is not a directory")
    base = compose_dir(root, compose_project)
    launcher = use_case_launcher(root, compose_project)
    by_index: "dict[int, list[str]]" = {}
    launcher_found = False
    for path in listing.files:
        match path.relative_to(base).parts:
            case (name,) if name == launcher.name:
                launcher_found = True
            case (index, name) if index.isdigit() and name.endswith(_COMPOSE_SUFFIX):
                by_index.setdefault(int(index), []).append(name)
            case _:
                pass
    if not launcher_found:
        raise ValueError(f"no {launcher.name}")
    if not by_index:
        raise ValueError("no compose file")
    index_dirs = {
        int(p.relative_to(base).name)
        for p in listing.dirs
        if len(p.relative_to(base).parts) == 1 and p.name.isdigit()
    }
    empty = sorted(index_dirs - set(by_index))
    if empty:
        raise ValueError(f"index directories {empty} hold no compose file")
    if sorted(by_index) != list(range(len(by_index))):
        raise ValueError(f"index directories {sorted(by_index)} are not contiguous from 0")
    crowded = {i: sorted(names) for i, names in by_index.items() if len(names) != 1}
    if crowded:
        raise ValueError(f"more than one compose file in an index directory: {crowded}")
    return StagedUseCase(
        compose_paths=[base / str(i) / by_index[i][0] for i in sorted(by_index)],
        launcher=launcher,
    )


async def staged_use_case(parent: "UnixHost", compose_project: str) -> "StagedUseCase | None":
    """Read back what the last :func:`stage_use_case` left on *parent*, or ``None`` if nothing.

    :func:`stage_use_case` wipes :func:`compose_dir` on every deploy, so what
    is there is exactly the last deploy's ``-f`` set: one compose file per
    numbered directory, in the deploy's order, plus its env launcher. A teardown
    that goes through those files removes only what they declare, without
    re-running an adapter or re-staging a file. The listing is the host's own
    tree walk, the one every recursive transfer uses.

    The parent's private staging root is verified first
    (:func:`~otto.host.docker_host.ensure_private_root`), since a teardown
    runs the launcher it finds there. A deployment with nothing under it but
    staging left by otto before 0.18 under the shared root is read back from
    there, only once :func:`trusted_legacy_staging` has found that tree
    trustworthy.

    Returns ``None`` only when the directory is confirmed absent under both
    roots (the parent's ``/tmp`` was cleared, or nothing was ever deployed
    from here).

    Raises:
        HostCommandError: the walk failed, or what is there is not a staging
            this module wrote (see ``_read_back``). Unknown is never read
            as absent: a caller that falls back to the project label would
            remove more than the deploy started.
        ~otto.host.errors.StagingRefusedError: the private root, or the older
            staging, could have been written by another account. Nothing was
            run, and the caller must not fall back to the label either.
        ~otto.result.CommandNotRunError: this is a dry run; the walk refuses
            to read a declined listing as an empty one.
    """
    root = await ensure_private_root(parent)
    listing = await _walk_compose_dir(parent, root, compose_project)
    legacy = None
    if listing.kind == "-":
        legacy = await trusted_legacy_staging(parent, compose_project)
        if legacy is None:
            return None
        root = legacy
        listing = await _walk_compose_dir(parent, root, compose_project)
        if listing.kind == "-":
            return None
    base = compose_dir(root, compose_project)
    where = _staged_files_on(parent, compose_project, base)
    try:
        staged = _read_back(listing, root, compose_project)
    except ValueError as e:
        raise _not_a_staging(where, base, str(e)) from e
    if legacy is not None:
        await _verify_legacy_compose_files(parent, compose_project, staged)
    # A transfer that dropped before its first write leaves the launcher empty,
    # and `sh <empty file> docker compose down` exits 0 having run nothing.
    held = await parent.exec(f"test -s {path_operand(staged.launcher)}")
    if not held.status.is_ok:
        raise _not_a_staging(where, base, f"{staged.launcher.name} is empty")
    return staged


def _staged_files_on(parent: "UnixHost", compose_project: str, base: Path) -> str:
    return f"the staged compose files for {compose_project} in {base} on {parent.id}"


async def _walk_compose_dir(
    parent: "UnixHost", root: Path, compose_project: str
) -> "RemoteListing":
    """Walk :func:`compose_dir` under *root* with the host's own tree walk."""
    # Function-scope: only a teardown reaches this, and the walk is a host
    # module this one does not otherwise import.
    from ..host.recursive_transfer import ListingError

    base = compose_dir(root, compose_project)
    try:
        return await parent._walk_remote(base)  # noqa: SLF001 — the host's declared walk hook
    except ListingError as e:
        raise HostCommandError(
            f"cannot read {_staged_files_on(parent, compose_project, base)}: {e}"
        ) from e


async def trusted_legacy_staging(parent: "Host", compose_project: str) -> "Path | None":
    """Return the shared pre-0.18 root if *compose_project*'s staging there can be trusted.

    otto before 0.18 staged every account's files under ``/tmp/otto-docker``.
    A teardown runs the launcher it reads back, so a tree another account
    could have written is never used: ``/tmp/otto-docker``, the project's
    directory and its compose directory must each be a directory its owner
    can search, and the launcher (when there) a regular file, none a symbolic
    link, all owned by the session's account and writable by neither group
    nor others. The check is the private root's own
    (:func:`~otto.host.docker_host.probe_ownership`), with "not writable by
    group or others" in place of "exactly 0700". The numbered compose
    directories and files a teardown passes as ``-f`` are checked the same way
    once read back (:func:`staged_use_case`). Nothing is written there.

    Precondition: ``/tmp`` has already been found root-owned and sticky, by
    :func:`~otto.host.docker_host.ensure_private_root` on the same parent;
    this function does not check it again. :func:`staged_use_case` calls it
    only after that check.

    Returns ``None`` when there is no such staging: one of the directories
    reads as absent, walking outermost first, with none above it unreadable.
    The probe answers "absent" only where it could look (the parent is
    searchable, or itself absent); a path behind a directory the session
    cannot search reads as unreadable, and an unreadable path is refused,
    never taken for absent. Trust is judged only once the staging is there,
    so a 0.17 ``/tmp/otto-docker`` left group-writable by a umask of 002, or
    owned by another account, refuses a teardown only of a project it holds.
    A missing launcher is left to the read-back, which reports it.

    Raises:
        ~otto.host.errors.StagingRefusedError: the staging is there and could
            have been written by another account. The message names the path
            and the manual teardown; the caller must not fall back to the
            project label.
    """
    project_dir = compose_staging_root(_LEGACY_ROOT, compose_project)
    compose = compose_dir(_LEGACY_ROOT, compose_project)
    launcher = use_case_launcher(_LEGACY_ROOT, compose_project)
    directories = [_LEGACY_ROOT, project_dir, compose]
    probe = await probe_ownership(parent, [*directories, launcher])
    for directory in directories:
        if probe.missing(directory):
            return None
        if directory in probe.unreadable:
            _refuse_untrusted_legacy(parent, compose_project, probe, directory, "directory")
    for directory in directories:
        _refuse_untrusted_legacy(parent, compose_project, probe, directory, "directory")
    if not probe.missing(launcher):
        _refuse_untrusted_legacy(parent, compose_project, probe, launcher, "regular file")
    return _LEGACY_ROOT


async def _verify_legacy_compose_files(
    parent: "Host", compose_project: str, staged: StagedUseCase
) -> None:
    """Refuse a pre-0.18 staging whose ``-f`` directories or files another account could write.

    :func:`trusted_legacy_staging` has trusted every directory above them;
    these are the numbered directories and compose files the read-back found,
    judged by the same rule. Each must be there: the walk listed it.
    """
    directories = list(dict.fromkeys(path.parent for path in staged.compose_paths))
    probe = await probe_ownership(parent, [*directories, *staged.compose_paths])
    for directory in directories:
        _refuse_untrusted_legacy(parent, compose_project, probe, directory, "directory")
    for path in staged.compose_paths:
        _refuse_untrusted_legacy(parent, compose_project, probe, path, "regular file")


def _refuse_untrusted_legacy(
    parent: "Host", compose_project: str, probe: OwnershipProbe, path: Path, kind: str
) -> None:
    """Raise :class:`StagingRefusedError` if *path* of the pre-0.18 staging cannot be trusted."""
    problem = probe.problem(path, kind=kind, private=False)
    if problem is None:
        return
    from .compose import compose_command  # compose imports this module

    project_dir = compose_staging_root(_LEGACY_ROOT, compose_project)
    down = compose_command(compose_project, "down")
    raise StagingRefusedError(
        f"{parent.id}: {compose_project} was staged by otto before 0.18 under "
        f"{_LEGACY_ROOT}, and {path} could have been written by another account "
        f"({problem}), so otto will not run what it holds. Tear that deployment "
        f"down by hand on {parent.id} (`{down}`), then delete {project_dir}."
    )


def _not_a_staging(where: str, base: Path, why: str) -> HostCommandError:
    return HostCommandError(
        f"{where} are not what a deploy leaves there ({why}); "
        f"remove {base} to tear the stack down by its project label instead"
    )


async def stage_use_case(
    parent: Host,
    compose_project: str,
    files: list[ComposeFileToStage],
    launcher_text: str,
    extra_files: dict[str, str] | None = None,
) -> StagedUseCase:
    """Stage a use-case's compose files, sidecars, and env launcher.

    *extra_files* is the vehicle for ADAPTER-GENERATED ``env_file:``
    sidecars (spec §7's ``AdapterResult.extra_files``: "relative name -> text
    ... env_file: sidecars the adapter generates rather than the project
    committing"). A rendered ``env_file:`` reference that does not resolve
    under its compose file's ``source_dir`` is looked up here by its
    relative name in *extra_files* before being refused as missing, and its
    text is staged at the same ``<n>/<relative path>`` a project-committed
    sidecar would get. Any *extra_files* key left unclaimed by a reference
    is genuinely-unreferenced content and is shipped once, under
    ``compose/`` preserving its own relative directory.

    Raises:
        ~otto.result.CommandNotRunError: this is a dry run — staging is all
            device touches, same reasoning as :func:`stage_compose_files`.
        ~otto.docker.resolve.UseCaseResolutionError: a rendered text is not
            YAML (or not a mapping), an ``env_file:`` entry has an
            unrecognized shape, a reference (or an *extra_files* key) is
            absolute or escapes its staging root, or a
            sidecar is missing both locally and from *extra_files* —
            configuration refusals settled before anything is staged or
            started.
    """
    if is_dry_run():
        raise CommandNotRunError(
            f"stage_use_case({compose_project})",
            getattr(parent, "id", ""),
            "No compose file, sidecar or env launcher was copied to the parent.",
        )

    extra_files_map = dict(extra_files or {})
    claimed_extra_keys: set[str] = set()

    # The layout under the root the parent's login names, computed without
    # touching the parent, so the validation below can name paths in its
    # warnings and refusals. The root itself is created or verified after the
    # validation, before the first write, and staging writes under the root
    # that check returns.
    planned = parent_staging_root(parent)
    planned_paths = use_case_compose_paths(planned, compose_project, files)

    # Validate everything local BEFORE the first device touch.
    local_sidecars: list[tuple[int, Path, str]] = []  # (file index, local path, rel name)
    adapter_sidecar_refs: list[
        tuple[int, str]
    ] = []  # (file index, rel name); text in extra_files_map
    seen_refs: set[tuple[int, str]] = set()
    for idx, f in enumerate(files):
        # planned_paths[0].parent, not planned_paths[idx].parent: this
        # function's stack is brought up by `_up_command`, which -- like
        # compose_up's `up` on the per-project path -- passes only `-f` and no
        # `--project-directory`. So compose resolves every relative bind
        # source (whichever file declares it) against the directory of the
        # FIRST `-f` file, not each file's own.
        warn_relative_bind_sources(f.handle, f.text, planned_paths[0].parent)
        for rel in _collect_env_file_refs(f.handle, f.text):
            if (idx, rel) in seen_refs:
                continue  # the same file referencing one env_file from two services stages it once
            seen_refs.add((idx, rel))
            _refuse_if_path_escapes(f"compose file {f.handle!r} declares env_file", rel)
            local = (f.source_dir / rel).resolve()
            if local.is_file():
                local_sidecars.append((idx, local, rel))
                continue
            if rel in extra_files_map:
                adapter_sidecar_refs.append((idx, rel))
                claimed_extra_keys.add(rel)
                continue
            raise _resolution_error(
                f"compose file {f.handle!r} references env_file {rel!r} but {local} does not exist"
            )

    unreferenced_extras = [
        (name, text) for name, text in extra_files_map.items() if name not in claimed_extra_keys
    ]
    launcher_name = use_case_launcher(planned, compose_project).name
    for name, _text in unreferenced_extras:
        _refuse_if_path_escapes("extra_files key", name)
        if os.path.normpath(name) == launcher_name:
            raise _resolution_error(
                f"extra_files key {name!r} is the name otto stages the use case's env "
                f"launcher under; rename the extra file"
            )

    root = await ensure_private_root(parent)
    compose_paths = use_case_compose_paths(root, compose_project, files)
    launcher_remote = use_case_launcher(root, compose_project)
    base = compose_dir(root, compose_project)
    prepared = await parent.exec(f"rm -rf {path_operand(base)} && mkdir -p {path_operand(base)}")
    if not prepared.status.is_ok:
        raise _staging_failure(
            prepared,
            f"failed to prepare the compose staging dir {base} on the parent: {prepared.value}",
        )

    with tempfile.TemporaryDirectory(prefix="otto-usecase-") as tmp:
        tmpdir = Path(tmp)
        for idx, f in enumerate(files):
            sub = compose_paths[idx].parent
            local = tmpdir / str(idx) / compose_paths[idx].name
            local.parent.mkdir(parents=True)
            local.write_text(f.text)
            await _mkdir_and_put(parent, sub, [local])

            for sidx, spath, rel in local_sidecars:
                if sidx != idx:
                    continue
                await _mkdir_and_put(parent, _dest_for_relative_name(sub, rel), [spath])

            for sidx, rel in adapter_sidecar_refs:
                if sidx != idx:
                    continue
                materialized = tmpdir / "adapter-sidecar" / str(idx) / rel
                materialized.parent.mkdir(parents=True, exist_ok=True)
                materialized.write_text(extra_files_map[rel])
                await _mkdir_and_put(parent, _dest_for_relative_name(sub, rel), [materialized])

        # The NAME comes from the same helper that names the returned path and
        # the dry-run preview -- not a second literal, so every compose call's
        # `sh <launcher>` names the file this line stages.
        launcher_local = tmpdir / launcher_remote.name
        launcher_local.write_text(launcher_text)
        # The mapping can carry secrets, so the launcher is left readable by the
        # connecting account only, on top of the 0700 root every staged file
        # sits under. `put` applies the mode once the bytes land.
        await _mkdir_and_put(parent, launcher_remote.parent, [launcher_local], mode=0o600)

        for name, text in unreferenced_extras:
            materialized = tmpdir / "extra" / name
            materialized.parent.mkdir(parents=True, exist_ok=True)
            materialized.write_text(text)
            await _mkdir_and_put(parent, _dest_for_relative_name(base, name), [materialized])

    return StagedUseCase(compose_paths=compose_paths, launcher=launcher_remote)


async def _mkdir_and_put(
    parent: Host, dest: Path, paths: list[Path], *, mode: "int | None" = None
) -> None:
    made = await parent.exec(f"mkdir -p {path_operand(dest)}")
    if not made.status.is_ok:
        raise _staging_failure(made, f"failed to create {dest} on the parent: {made.value}")
    put_result = await parent.put(paths, dest, mode=mode)
    if not put_result.is_ok:
        raise HostCommandError(
            f"failed to stage {[p.name for p in paths]} to {dest}: {put_result.msg}"
        )
