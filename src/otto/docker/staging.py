"""
File staging onto a parent host.

Building an image and running ``docker compose`` both require getting the
relevant files (Dockerfile, build context, compose YAML) onto the parent
host that runs the docker daemon. This module wraps that with a small,
predictable layout under ``/tmp/otto-docker/<project>/`` on the parent.

Layout::

    /tmp/otto-docker/<project>/
        build/<image>/<context-as-tar>     # extracted build context
        compose/<n>/<basename>.yml         # one numbered dir per compose file
        compose/<n>/<sidecar relative path> # env_file: sidecars, same numbering
        compose/otto.env                   # generated env file for the use case

Cleanup is the caller's responsibility (and is best-effort: a previous
crash is recovered from on the next stage). The directory layout is
stable across runs so nothing leaks into per-invocation subdirs.
"""

import os
import shlex
import tarfile
import tempfile
from dataclasses import dataclass
from pathlib import Path

from ..config.repo import DockerCompose, DockerImage
from ..host.errors import HostCommandError, HostUnreachableError
from ..host.host import Host, is_dry_run
from ..result import CommandNotRunError, CommandResult

PARENT_ROOT = Path("/tmp/otto-docker")  # noqa: S108 — deliberate staging path


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


def project_root(project: str) -> Path:
    """Per-project staging root on the parent host."""
    return PARENT_ROOT / project


def image_build_dir(project: str, image_name: str) -> Path:
    """Where this image's context will live on the parent."""
    return project_root(project) / "build" / image_name


def compose_dir(project: str) -> Path:
    """Per-project compose staging directory on the parent host."""
    return project_root(project) / "compose"


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

    remote_dir = image_build_dir(project, image.name)

    # Wipe and recreate to avoid mixing leftover files from an earlier build.
    # Checked, because `&&` makes a failed rm skip the mkdir silently and the
    # `tar -xf` below OVERLAYS rather than replaces: docker build would then
    # see a context still holding a file the user deleted locally, and produce
    # a wrong image under a context hash that says it is right. Build staging
    # is keyed on the repo name (unlike compose staging, keyed on the
    # suffix-bearing project), so two users on one parent really do collide here.
    prepared = await parent.exec(
        f"rm -rf {shlex.quote(str(remote_dir))} && mkdir -p {shlex.quote(str(remote_dir))}"
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
            f"tar -xf {shlex.quote(str(remote_tar))} -C {shlex.quote(str(remote_dir))} "
            f"&& rm -f {shlex.quote(str(remote_tar))}",
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
) -> list[Path]:
    """Copy compose files to numbered directories on the parent.

    Numbered directories preserve the order the project listed them
    (which determines override precedence in ``docker compose -f a -f b``).
    Returns the absolute paths on the parent in the same order.

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

    base = compose_dir(project)
    prepared = await parent.exec(
        f"rm -rf {shlex.quote(str(base))} && mkdir -p {shlex.quote(str(base))}"
    )
    if not prepared.status.is_ok:
        raise _staging_failure(
            prepared,
            f"failed to prepare the compose staging dir {base} on the parent: {prepared.value}",
        )

    out: list[Path] = []
    for idx, compose in enumerate(composes):
        sub = base / str(idx)
        made = await parent.exec(f"mkdir -p {shlex.quote(str(sub))}")
        if not made.status.is_ok:
            raise _staging_failure(made, f"failed to create {sub} on the parent: {made.value}")
        put_result = await parent.put([compose.path], sub)
        if not put_result.is_ok:
            raise HostCommandError(f"failed to stage compose file {compose.path}: {put_result.msg}")
        out.append(sub / compose.path.name)
    return out


async def cleanup_project(parent: Host, project: str) -> CommandResult:
    """Remove the per-project staging tree on the parent. Best-effort.

    Returns the ``rm -rf``'s own :class:`~otto.result.CommandResult` — callers
    that care check ``.is_ok``; best-effort callers ignore it.
    """
    return await parent.exec(f"rm -rf {shlex.quote(str(project_root(project)))}")


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
    env_file: Path


def use_case_compose_paths(compose_project: str, files: "list[ComposeFileToStage]") -> list[Path]:
    """Where :func:`stage_use_case` will put each rendered compose file. PURE.

    The parent-side layout, computed without touching the parent, so a caller
    that has settled a deployment but must not contact a device -- a dry run
    rendering spec §12's exact compose command -- can name the ``-f`` paths
    the real run would use. :func:`stage_use_case` returns THIS function's
    answer rather than accumulating its own, so the preview and the staging
    cannot drift: one of them changing the layout changes both.
    """
    base = compose_dir(compose_project)
    return [base / str(idx) / f"{f.handle}.yml" for idx, f in enumerate(files)]


def use_case_env_file(compose_project: str) -> Path:
    """Where :func:`stage_use_case` will put the generated ``otto.env``. PURE.

    Same reasoning as :func:`use_case_compose_paths`.
    """
    return compose_dir(compose_project) / "otto.env"


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
    outside ``/tmp/otto-docker`` (an absolute ref) or above the staging root
    the reference is supposed to land under (a ``../`` ref that normalizes
    outside it) — spec §8 constraint 5 says sidecars stay under ``<n>/``, so
    both are a loud, distinct refusal instead of a silent escape. Shared by
    repo-committed ``env_file:`` refs (whose root is a numbered ``<n>/`` dir)
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


def _dest_for_relative_name(root: Path, rel: str) -> Path:
    """Where a relative name lands under *root*, preserving its own subdirectory.

    Shared by env_file sidecars (``root`` is a numbered ``<n>/`` dir) and
    unreferenced ``extra_files`` entries (``root`` is ``compose/`` itself) —
    both need "preserve the relative dir, don't flatten" and nothing else.
    """
    rel_parent = Path(rel).parent
    return root / rel_parent if str(rel_parent) != "." else root


async def stage_use_case(
    parent: Host,
    compose_project: str,
    files: list[ComposeFileToStage],
    env_text: str,
    extra_files: dict[str, str] | None = None,
) -> StagedUseCase:
    """Stage a use-case's compose files, sidecars, and otto.env (spec §8).

    *extra_files* is the vehicle for ADAPTER-GENERATED ``env_file:``
    sidecars (spec §7's ``AdapterResult.extra_files``: "relative name -> text
    ... env_file: sidecars the adapter generates rather than the repo
    committing"). A rendered ``env_file:`` reference that does not resolve
    under its compose file's ``source_dir`` is looked up here by its
    relative name in *extra_files* before being refused as missing, and its
    text is staged at the same ``<n>/<relative path>`` a repo-committed
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
            "No compose file, sidecar or env file was copied to the parent.",
        )

    extra_files_map = dict(extra_files or {})
    claimed_extra_keys: set[str] = set()

    # Validate everything local BEFORE the first device touch.
    local_sidecars: list[tuple[int, Path, str]] = []  # (file index, local path, rel name)
    adapter_sidecar_refs: list[
        tuple[int, str]
    ] = []  # (file index, rel name); text in extra_files_map
    seen_refs: set[tuple[int, str]] = set()
    for idx, f in enumerate(files):
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
    for name, _text in unreferenced_extras:
        _refuse_if_path_escapes("extra_files key", name)

    base = compose_dir(compose_project)
    prepared = await parent.exec(
        f"rm -rf {shlex.quote(str(base))} && mkdir -p {shlex.quote(str(base))}"
    )
    if not prepared.status.is_ok:
        raise _staging_failure(
            prepared,
            f"failed to prepare the compose staging dir {base} on the parent: {prepared.value}",
        )

    compose_paths = use_case_compose_paths(compose_project, files)
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
        # the dry-run preview -- not a second literal. A helper that renamed the
        # file while this line kept writing `otto.env` would produce a live
        # `docker compose --env-file <path>` pointing at a file nothing staged.
        env_remote = use_case_env_file(compose_project)
        env_local = tmpdir / env_remote.name
        env_local.write_text(env_text)
        await _mkdir_and_put(parent, env_remote.parent, [env_local])

        for name, text in unreferenced_extras:
            materialized = tmpdir / "extra" / name
            materialized.parent.mkdir(parents=True, exist_ok=True)
            materialized.write_text(text)
            await _mkdir_and_put(parent, _dest_for_relative_name(base, name), [materialized])

    return StagedUseCase(compose_paths=compose_paths, env_file=env_remote)


async def _mkdir_and_put(parent: Host, dest: Path, paths: list[Path]) -> None:
    made = await parent.exec(f"mkdir -p {shlex.quote(str(dest))}")
    if not made.status.is_ok:
        raise _staging_failure(made, f"failed to create {dest} on the parent: {made.value}")
    put_result = await parent.put(paths, dest)
    if not put_result.is_ok:
        raise HostCommandError(
            f"failed to stage {[p.name for p in paths]} to {dest}: {put_result.msg}"
        )
