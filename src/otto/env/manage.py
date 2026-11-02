"""The per-workspace orchestration venv: where it is, and what it records.

The venv sits at ``<workspace home>/env`` -- singular, because the workspace
keying already happened one directory up (``otto.config.home``). Nothing here
re-derives that key.

Its metadata lives INSIDE the venv, at ``env/.otto-env.json``. That placement
is the contract: ``create --force`` removes ``env/`` wholesale, so the metadata
goes with it and a rebuild can never inherit the previous build's record. Stored
beside the venv instead, it would survive the rebuild and describe an env that
no longer exists.

Reads are TOTAL: absent, corrupt, and half-written metadata all read as None
rather than raising, because the caller most likely to meet a broken env is
``create --force``, whose whole job is to replace it -- and it cannot do that
if merely looking at the env throws.
"""

import dataclasses
import json
import os
import shutil
import subprocess
import sys
from importlib import metadata
from pathlib import Path
from typing import TYPE_CHECKING
from urllib.parse import urlparse
from urllib.request import url2pathname

from ..errors import OttoError
from ..version import get_version

if TYPE_CHECKING:
    from ..config.project import Project

META_FILENAME = ".otto-env.json"

_LIST_DISTS = (
    "import importlib.metadata as m\n"
    "print('\\n'.join(d.metadata['Name'] or '' for d in m.distributions()))"
)
"""One-liner run INSIDE the env to enumerate what it holds. Asking the env's
own interpreter answers from the installed dist-info itself, not from what otto
believes it asked uv to install."""

_ATTRIBUTABLE = 2
"""How many project names a resolver message must implicate before otto adds its
one attribution line. Below two there is nothing to attribute BETWEEN, and a
guess would send the operator to edit the wrong pyproject."""


@dataclasses.dataclass(frozen=True)
class EnvMeta:
    """What an existing orchestration venv records about its last ``create``/``sync``."""

    uv_version: str
    """The version of the uv that the last successful ``create`` or ``sync`` ran,
    measured at the start of that operation."""

    otto_version: str
    """The otto version installed into it by that same ``create`` or ``sync``."""


def env_path(roots: "list[Path] | None" = None) -> Path:
    """Return this workspace's orchestration venv path. Pure -- never creates."""
    from ..config.home import workspace_home

    return workspace_home(roots) / "env"


def meta_path(env: Path) -> Path:
    """Return the metadata file path for the venv at *env*."""
    return env / META_FILENAME


def read_meta(env: Path) -> "EnvMeta | None":
    """Return the venv's recorded metadata, or None if it cannot be read.

    Total by design -- see the module docstring.
    """
    try:
        raw = json.loads(meta_path(env).read_text())
        return EnvMeta(uv_version=raw["uv_version"], otto_version=raw["otto_version"])
    except (OSError, ValueError, KeyError, TypeError):
        return None


def write_meta(env: Path, meta: EnvMeta) -> None:
    """Record *meta* inside the venv at *env*."""
    meta_path(env).write_text(json.dumps(dataclasses.asdict(meta), indent=2) + "\n")


class EnvExistsError(OttoError, RuntimeError):
    """``env create`` found an environment already built for this workspace."""


class EnvSelfRebuildError(OttoError, RuntimeError):
    """otto is running from inside the environment it was asked to rebuild or sync."""


def _installable(projects: "list[Project]") -> "tuple[list[Project], list[Project]]":
    """Split discovered projects into (installable, skipped).

    Installable means "has a pyproject.toml". A project without one is not an
    error and never has been: its ``libs`` ride ``sys.path`` at bootstrap, and
    that is still correct. Every sample project but project4 is in the skipped half,
    so a workspace of them must still build a working env.
    """
    installable = [r for r in projects if (r.root / "pyproject.toml").is_file()]
    skipped = [r for r in projects if r not in installable]
    return installable, skipped


def _otto_target() -> "list[str]":
    """Return the installer arguments that put THIS otto into the env.

    otto is installed the way otto is installed. On a dev checkout that is an
    EDITABLE install, so pinning ``otto-sh==<version>`` would fetch a released
    wheel of the same version number and leave the env running code that is
    not the code in front of you -- the version string cannot tell those
    apart, and otto's own dist-info can. An otto that came from a wheel has no
    checkout to point at, and pins.
    """
    dist_url = _editable_source()
    return ["-e", str(dist_url)] if dist_url is not None else [f"otto-sh=={get_version()}"]


def _editable_source() -> "Path | None":
    """Return the checkout otto is installed from, or None if it is not editable."""
    try:
        raw = metadata.distribution("otto-sh").read_text("direct_url.json")
    except (metadata.PackageNotFoundError, OSError):
        return None
    if not raw:
        return None
    try:
        info = json.loads(raw)
    except ValueError:
        return None
    url = info.get("url", "")
    if not info.get("dir_info", {}).get("editable") or not url.startswith("file:"):
        return None
    return Path(url2pathname(urlparse(url).path))


def _attribute_conflict(projects: "list[Project]", stderr: str) -> "str | None":
    """Name the two projects whose installs collided, if the message implicates them.

    otto NEVER resolves and never rewrites the resolver's text. This adds ONE
    line, and only when it can actually attribute the failure -- a guess here
    would be worse than silence, because it would send the operator to edit the
    wrong pyproject.

    ONLY BARE NAMES COUNT -- every path-shaped word is dropped before matching,
    and that is not tidiness. The workspace key is ``<hash>-<slug>`` with the
    slug built from the project roots' basenames, so EVERY installer message that echoes
    the environment path contains every project in the workspace. Matching the raw
    text therefore attributed an ordinary "setuptools not found" build failure
    to two projects that had not collided at all. A resolver naming a package in a
    conflict names it bare; a path is the one place a name means nothing.
    """
    prose = " ".join(w for w in stderr.lower().split() if "/" not in w and "\\" not in w)
    named = [r.name for r in projects if r.name.lower() in prose]
    if len(named) < _ATTRIBUTABLE:
        return None
    return f"the conflicting requirements came from: {', '.join(sorted(named)[:2])}"


class EnvBuildError(OttoError, RuntimeError):
    """An installer refused. Carries the resolver's own text, unrewritten."""


@dataclasses.dataclass(frozen=True)
class EnvBuild:
    """What a ``create``/``sync`` run did, for the CLI to present.

    Wider than the :class:`EnvMeta` it carries because the skip notice is
    contractual: a workspace of projects without pyprojects must still build, and
    the operator has to be told which projects were passed over and why.
    """

    env: Path
    meta: EnvMeta
    installed: "list[str]"
    skipped: "list[str]"


def _relay(
    result: "subprocess.CompletedProcess[str]", projects: "list[Project]", what: str
) -> None:
    """Raise :class:`EnvBuildError` carrying the installer's own text, if it failed.

    otto NEVER resolves and never rewrites the resolver's message. At most it
    appends one attribution line, and only when it can actually name the two
    projects that collided.
    """
    if result.returncode == 0:
        return
    text = (result.stderr or result.stdout or "").rstrip()
    attribution = _attribute_conflict(projects, text)
    raise EnvBuildError(f"{what} failed:\n{text}" + (f"\n{attribution}" if attribution else ""))


def _inside(path: Path, env: Path) -> bool:
    """Whether *path* lies inside *env*, as spelled OR as resolved.

    Both forms matter: ``rmtree`` deletes an entry under *env* even when it is a
    symlink whose target lives elsewhere, and a path spelled outside *env* can
    still resolve into it. The spelling is normalized without following links,
    so ``<env>/../x/uv`` reads as the sibling it names.
    """

    def spelled(p: Path) -> Path:
        return Path(os.path.normpath(p.absolute()))

    return spelled(path).is_relative_to(spelled(env)) or path.resolve().is_relative_to(
        env.resolve()
    )


def _acquire_uv(env: Path) -> "tuple[Path, str]":
    """Return the uv this operation runs and its version, or refuse before anything changes.

    Called ONCE per ``create``/``sync``, before the env is touched: every uv call
    of the operation gets this path, and the version recorded is the version of
    the binary that ran.

    Refuses when otto is running from the env it is about to replace: when the
    uv it would call lies inside *env* (an entry there, even a symlink pointing
    out to the uv cache, or a path that resolves there), or when otto's own
    interpreter prefix does (``find_uv_bin`` can fall back to a uv elsewhere when
    the env's own is missing). ``create --force`` would delete the otto and uv it
    then needs, and a sync could replace them mid-run. Refused here, so both
    verbs share the guard.
    """
    from .backends import uv_bin, uv_version

    uv = uv_bin()
    version = uv_version(uv)
    if _inside(uv, env) or _inside(Path(sys.prefix), env):
        raise EnvSelfRebuildError(
            f"otto is running from inside the environment it would replace ({env}) — "
            "run otto from an installation outside that env (for example, the "
            "project's own .venv)"
        )
    return uv, version


def _fill(
    env: Path, uv: Path, version: str, projects: "list[Project]", passthrough: "list[str]"
) -> "EnvBuild":
    """Install every installable project plus otto itself into the venv at *env*."""
    from .backends import install

    installable, skipped = _installable(projects)
    if installable:
        targets: "list[str]" = []
        for project in installable:
            targets += ["-e", str(project.root)]
        _relay(install(uv, env, targets, passthrough), projects, "installing projects")
    _relay(install(uv, env, _otto_target(), passthrough), projects, "installing otto")
    meta = EnvMeta(uv_version=version, otto_version=get_version())
    write_meta(env, meta)
    return EnvBuild(
        env=env,
        meta=meta,
        installed=[r.name for r in installable],
        skipped=[r.name for r in skipped],
    )


def _create(
    env: Path,
    projects: "list[Project]",
    uv: Path,
    version: str,
    *,
    force: bool,
    passthrough: "list[str]",
) -> "EnvBuild":
    """Build the venv at *env* from scratch with the already-acquired *uv*."""
    from .backends import create_venv

    if env.exists():
        if not force:
            raise EnvExistsError(
                f"an environment already exists at {env} — pass --force to remove and "
                "rebuild it, or run `otto env sync` to update it in place"
            )
        shutil.rmtree(env)
    _relay(create_venv(uv, env), projects, "creating the venv")
    return _fill(env, uv, version, projects, passthrough)


def create_env(
    *,
    force: bool = False,
    passthrough: "list[str] | None" = None,
) -> "EnvBuild":
    """Build this workspace's orchestration venv from scratch.

    Refuses an existing env naming ``--force``; ``--force`` removes it first,
    which is the recovery story for a wedged env. otto's uv is acquired BEFORE
    that removal, so a missing uv or a self-rebuild is refused with the old env
    still standing.
    """
    projects = _discovered_projects()
    env = env_path()
    uv, version = _acquire_uv(env)
    return _create(env, projects, uv, version, force=force, passthrough=passthrough or [])


def sync_env(*, passthrough: "list[str] | None" = None) -> "EnvBuild":
    """Bring this workspace's orchestration venv up to date, creating it if absent.

    NEVER destroys anything. It is the verb every error message names, so it
    has to stay safe to suggest -- including when the thing it is suggested
    for is that there is no env at all, which is why a missing env is built
    rather than refused. Either way uv is acquired once, so the build shares
    :func:`create_env`'s core rather than calling it.

    Nothing is read from the existing metadata: a record from an older otto,
    or none at all, is simply re-stamped when the installs succeed.
    """
    projects = _discovered_projects()
    env = env_path()
    uv, version = _acquire_uv(env)
    if not env.exists():
        return _create(env, projects, uv, version, force=False, passthrough=passthrough or [])
    return _fill(env, uv, version, projects, passthrough or [])


def _discovered_projects() -> "list[Project]":
    """Return the DISCOVERED project set, not the active one.

    An environment is workspace-scoped: which labs today's command happens to
    load must not change what gets installed into it.
    """
    from ..config.project import discover

    return discover().projects


@dataclasses.dataclass(frozen=True)
class ProjectState:
    """One project's standing in the environment, as ``env show`` reports it."""

    name: str
    dist_name: "str | None"
    """``[project] name`` from its pyproject, or None when it has none (and so
    is not installable at all)."""

    installed: "bool | None"
    """True/False when the env could be asked, None when it could not."""

    stale: bool
    """Its pyproject is newer than the env's metadata -- the env was built
    before the project's requirements last changed."""


@dataclasses.dataclass(frozen=True)
class EnvStatus:
    """Everything ``env show`` prints, with no presentation attached."""

    env: Path
    exists: bool
    meta: "EnvMeta | None"
    projects: "list[ProjectState]"


def _dist_name(project: "Project") -> "str | None":
    """Return the project's distribution name from its pyproject, if it has one.

    Reads through :func:`otto.env.preflight.read_project_table` rather than
    re-opening the file, so this package has ONE answer to "what does this
    project's pyproject say" -- including what it does with an unreadable one.
    Two readers with independently maintained error handling is exactly the
    drift that ends with ``env show`` and the preflight disagreeing about
    whether a project is installable.
    """
    from .preflight import read_project_table

    pyproject_table = read_project_table(project) or {}
    name = pyproject_table.get("name")
    return str(name) if name else None


def _installed_dists(env: Path) -> "set[str] | None":
    """Return the normalized distribution names inside *env*, or None if unaskable.

    One subprocess for the whole env rather than one per project, and None rather
    than an exception when the env's interpreter cannot answer -- ``show`` is
    the DIAGNOSTIC verb, so a half-broken env has to be describable.
    """
    from ..models.dependencies import normalize_name
    from .backends import venv_python

    python = venv_python(env)
    if not python.is_file():
        return None
    probe = subprocess.run(  # noqa: S603 — fixed argv, path derived from the env we own
        [
            str(python),
            "-c",
            _LIST_DISTS,
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    if probe.returncode != 0:
        return None
    return {normalize_name(line) for line in probe.stdout.split() if line}


def env_status() -> "EnvStatus":
    """Describe this workspace's environment. NEVER raises on a missing one.

    ``show`` is the verb you reach for when things look wrong, and a
    diagnostic that fails when things are broken is the one you needed most --
    so a missing env is a described state, not an error.
    """
    env = env_path()
    projects = _discovered_projects()
    if not env.exists():
        return EnvStatus(env=env, exists=False, meta=None, projects=[])

    meta = read_meta(env)
    present = _installed_dists(env)
    stamp = meta_path(env)
    built_at = stamp.stat().st_mtime if stamp.is_file() else 0.0

    states: "list[ProjectState]" = []
    for project in projects:
        dist = _dist_name(project)
        pyproject = project.root / "pyproject.toml"
        states.append(
            ProjectState(
                name=project.name,
                dist_name=dist,
                installed=None if present is None or dist is None else _norm(dist) in present,
                stale=pyproject.is_file() and pyproject.stat().st_mtime > built_at,
            )
        )
    return EnvStatus(env=env, exists=True, meta=meta, projects=states)


def _norm(name: str) -> str:
    """PEP-503 normalization, through otto's one implementation."""
    from ..models.dependencies import normalize_name

    return normalize_name(name)
