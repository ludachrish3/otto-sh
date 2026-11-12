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
import enum
import json
import logging
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

logger = logging.getLogger(__name__)

META_FILENAME = ".otto-env.json"

_PROBE_ENV = """\
import importlib.metadata as m, json, sys, sysconfig
try:
    from packaging.markers import default_environment
    marker_env = default_environment()
except ImportError:
    marker_env = None
print(json.dumps({
    "site": sorted({sysconfig.get_path(k) for k in ("purelib", "platlib")}),
    "dists": [d.metadata["Name"] or "" for d in m.distributions()],
    "python": ".".join(str(n) for n in sys.version_info[:3]),
    "markers": marker_env,
}))
"""
"""Run INSIDE the env to report what it holds, where, and on which Python. Asking
the env's own interpreter answers from the installed dist-info itself, not from
what otto believes it asked uv to install, and gives the env's own PEP 508
marker values: the env runs whatever Python uv picked, not otto's. The marker
values come from ``packaging`` IN the env (otto depends on it, so it is there
once otto is), and are None in a venv that does not hold it yet."""

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


class EnvUninstallableProjectError(OttoError, ValueError):
    """A project's pyproject asks for an install that otto cannot honour as declared."""


class VirtualReason(enum.Enum):
    """Why a project is virtual -- which decides what un-virtualizes it."""

    EXPLICIT = "[tool.uv] package = false"
    """``[tool.uv] package = false``, which wins over any ``[build-system]``."""

    NO_BUILD_SYSTEM = "no [build-system]"
    """No ``[tool.uv] package`` and no ``[build-system]``."""


def is_virtual(project: "Project") -> bool:
    """Whether *project* is VIRTUAL: a pyproject that uv would not build as a package.

    :func:`virtual_reason` decides; this is its yes/no.
    """
    return virtual_reason(project) is not None


def virtual_reason(project: "Project") -> "VirtualReason | None":
    """Why *project* is VIRTUAL, or None when it is a package (or has no pyproject).

    THE one place ``otto env`` decides whether a project is a package to build
    or a virtual project whose dependencies are all there is to install; the
    install plan, its refusals and ``env show`` all ask here. The rule is uv's own, so a
    project means the same thing to ``otto env`` as to ``uv sync`` run inside
    it: an explicit ``[tool.uv] package`` decides, and without one a project
    that declares no ``[build-system]`` is virtual. That is the single-project
    layout docs/installation.md recommends. Building one anyway falls back to
    setuptools, whose auto-discovery refuses any layout with more than one
    top-level directory, and which an offline wheel directory does not hold.

    An UNREADABLE pyproject is not virtual, and neither is one whose
    ``[tool.uv] package`` is not a boolean: both are installed like a package,
    so uv refuses them in its own words (see
    :func:`otto.env.preflight.read_pyproject`).
    """
    from .preflight import read_pyproject

    pyproject = read_pyproject(project)
    if pyproject is None:
        return None
    tool = pyproject.get("tool")
    uv_table = tool.get("uv") if isinstance(tool, dict) else None
    explicit = uv_table.get("package") if isinstance(uv_table, dict) else None
    if explicit is not None:
        return VirtualReason.EXPLICIT if explicit is False else None
    return VirtualReason.NO_BUILD_SYSTEM if "build-system" not in pyproject else None


@dataclasses.dataclass(frozen=True)
class SkippedProject:
    """A discovered project that ``otto env`` does not install, and why."""

    name: str
    reason: str
    """Already phrased for the operator: ``no pyproject.toml``, ``no [project] table``."""


def _skip_reason(project: "Project") -> "str | None":
    """Return why *project* is not installed at all, or None when it is installable.

    Shared by the install plan and ``env show`` so the two cannot disagree.
    A project without a pyproject.toml is not an error and never has been: its
    ``libs`` ride ``sys.path`` at bootstrap. A virtual project with no
    ``[project]`` table declares no dependencies to install, so it is the same
    case. A PACKAGE without one is not skipped: a build backend can still build
    it (a ``setup.py`` project, say).
    """
    from .preflight import PYPROJECT, read_project_table

    if not (project.root / PYPROJECT).is_file():
        return "no pyproject.toml"
    if is_virtual(project) and read_project_table(project) is None:
        return "no [project] table"
    return None


def _refuse_dynamic(project: "Project", reason: "VirtualReason") -> None:
    """Refuse a virtual project whose requirements only a build could compute.

    *reason* is :func:`virtual_reason`'s answer, asked once by the caller; it
    picks the advice.

    Handing its pyproject to uv as a requirements file would not do instead:
    with dynamic ``dependencies`` or ``optional-dependencies`` uv falls back to
    building it with setuptools, which is exactly the build a virtual project
    must never get.
    """
    from .preflight import PYPROJECT, dynamic_fields_needing_build, read_project_table

    fields = dynamic_fields_needing_build(read_project_table(project) or {})
    if not fields:
        return
    fix = (
        "drop `package = false` (and declare a build backend if it has none)"
        if reason is VirtualReason.EXPLICIT
        else "declare a build backend"
    )
    raise EnvUninstallableProjectError(
        f"cannot install {project.name}: {project.root / PYPROJECT} declares "
        f"{' and '.join(fields)} as dynamic, but it is a virtual project "
        f"({reason.value}) and otto never builds one — {fix} so they can be "
        "computed, or list them statically in [project]"
    )


def _refuse_unsupported_python(projects: "list[Project]", python_version: str) -> None:
    """Refuse a virtual project whose ``requires-python`` the env's interpreter fails.

    The asymmetry is uv's: it enforces ``requires-python`` when it installs a
    package project (``-e``), and not when it reads a virtual project's
    pyproject as a requirements file (``-r``), and the uv otto runs has no flag
    that would make it -- so for a virtual project otto is the only one who
    can. *python_version* is the interpreter's ``sys.version_info[:3]``, as
    pip compares it, so a pre-release ``3.15.0a5`` satisfies ``>=3.15``. An
    unparseable specifier or version is left alone: the installer and
    discovery own that diagnosis.
    """
    from packaging.specifiers import InvalidSpecifier, SpecifierSet
    from packaging.version import InvalidVersion, Version

    from .preflight import PYPROJECT, read_project_table

    for project in projects:
        spec = (read_project_table(project) or {}).get("requires-python")
        if not spec:
            continue
        try:
            satisfied = SpecifierSet(str(spec)).contains(Version(python_version), prereleases=True)
        except (InvalidSpecifier, InvalidVersion):
            continue
        if not satisfied:
            raise EnvUninstallableProjectError(
                f"cannot install {project.name}: {project.root / PYPROJECT} declares "
                f"requires-python = {str(spec)!r}, and the environment runs Python "
                f"{python_version} — rebuild it on a Python that satisfies it, for "
                "example `UV_PYTHON=<version> otto env create --force`"
            )


@dataclasses.dataclass(frozen=True)
class _InstallPlan:
    """What one ``create``/``sync`` installs for the projects, decided before anything changes."""

    projects: "list[Project]"
    """Every discovered project, for attributing a resolver conflict."""

    targets: "list[str]"
    """Installer arguments for the projects' single install call; empty means none."""

    editable: "list[str]"
    """Package projects, installed editable."""

    virtual: "list[Project]"
    """Virtual projects: their dependencies installed, the project never built."""

    skipped: "list[SkippedProject]"
    """Projects with nothing to install (:func:`_skip_reason`)."""


def _plan(projects: "list[Project]") -> "_InstallPlan":
    """Decide how each discovered project goes into the env, refusing before any change.

    A package goes in as ``-e <root>``. A virtual project (:func:`is_virtual`)
    goes in as ``-r <root>/pyproject.toml``: uv reads its static dependencies
    from the file without building it, and resolves each through the
    project's own ``[tool.uv.sources]`` -- a path, git or editable source that
    a bare requirement string would send to an index instead. Its own code
    needs no install: its ``libs`` ride ``sys.path`` at bootstrap.
    """
    from .preflight import PYPROJECT

    targets: "list[str]" = []
    editable: "list[str]" = []
    virtual: "list[Project]" = []
    skipped: "list[SkippedProject]" = []
    for project in projects:
        skip = _skip_reason(project)
        reason = None if skip is not None else virtual_reason(project)
        if skip is not None:
            skipped.append(SkippedProject(name=project.name, reason=skip))
        elif reason is not None:
            _refuse_dynamic(project, reason)
            targets += ["-r", str(project.root / PYPROJECT)]
            virtual.append(project)
        else:
            targets += ["-e", str(project.root)]
            editable.append(project.name)
    return _InstallPlan(
        projects=projects, targets=targets, editable=editable, virtual=virtual, skipped=skipped
    )


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
    """Package projects, installed editable."""

    virtual: "list[str]"
    """Virtual projects (:func:`is_virtual`): dependencies installed, never built."""

    skipped: "list[SkippedProject]"


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
    env: Path, uv: Path, version: str, plan: "_InstallPlan", passthrough: "list[str]"
) -> "EnvBuild":
    """Install *plan*'s project targets plus otto itself into the venv at *env*."""
    from .backends import install

    if plan.virtual:
        contents = _env_contents(env)
        if contents is None:
            logger.warning(
                "could not ask the environment's interpreter for its Python version; "
                "the requires-python check for %s was skipped",
                ", ".join(p.name for p in plan.virtual),
            )
        else:
            _refuse_unsupported_python(plan.virtual, contents.python_version)
    if plan.targets:
        _relay(install(uv, env, plan.targets, passthrough), plan.projects, "installing projects")
    _relay(install(uv, env, _otto_target(), passthrough), plan.projects, "installing otto")
    meta = EnvMeta(uv_version=version, otto_version=get_version())
    write_meta(env, meta)
    return EnvBuild(
        env=env,
        meta=meta,
        installed=plan.editable,
        virtual=[p.name for p in plan.virtual],
        skipped=plan.skipped,
    )


def _create(
    env: Path,
    plan: "_InstallPlan",
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
    _relay(create_venv(uv, env), plan.projects, "creating the venv")
    return _fill(env, uv, version, plan, passthrough)


def create_env(
    *,
    force: bool = False,
    passthrough: "list[str] | None" = None,
) -> "EnvBuild":
    """Build this workspace's orchestration venv from scratch.

    Refuses an existing env naming ``--force``; ``--force`` removes it first,
    which is the recovery story for a wedged env. otto's uv is acquired BEFORE
    that removal, so a missing uv or a self-rebuild is refused with the old env
    still standing -- and so is the install plan, so a virtual project with
    dynamic requirements is refused with it standing too. Everything after
    the removal runs in the NEW venv: a ``requires-python`` refusal (it needs
    that venv's interpreter) and any installer failure leave the new, partly
    filled venv behind, and with ``--force`` the old one is already gone.
    """
    plan = _plan(_discovered_projects())
    env = env_path()
    uv, version = _acquire_uv(env)
    return _create(env, plan, uv, version, force=force, passthrough=passthrough or [])


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
    plan = _plan(_discovered_projects())
    env = env_path()
    uv, version = _acquire_uv(env)
    if not env.exists():
        return _create(env, plan, uv, version, force=False, passthrough=passthrough or [])
    return _fill(env, uv, version, plan, passthrough or [])


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
    """True/False when the env could be asked, None when it could not. For a
    virtual project (:func:`is_virtual`), which never has a distribution of its
    own, whether the env satisfies its declared dependencies instead."""

    virtual: bool
    """It is a virtual project, so ``installed`` answers for its dependencies."""

    skipped: "str | None"
    """Why it is not installed at all (:class:`SkippedProject`'s reason), or None."""

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


@dataclasses.dataclass(frozen=True)
class _EnvContents:
    """What the env's own interpreter reports it holds."""

    dists: "set[str]"
    """Normalized distribution names."""

    site_dirs: "list[Path]"
    """Its site-packages directories, for checking requirements against it."""

    python_version: str
    """Its interpreter's ``sys.version_info[:3]``, for checking ``requires-python``."""

    marker_env: "dict[str, str] | None"
    """Its PEP 508 marker values, or None when it cannot compute them."""


def _env_contents(env: Path) -> "_EnvContents | None":
    """Return what *env* holds, or None if its interpreter cannot answer.

    One subprocess for the whole env rather than one per project, and None rather
    than an exception when the env's interpreter cannot answer -- ``show`` is
    the DIAGNOSTIC verb, so a half-broken env has to be describable.
    """
    from .backends import venv_python

    python = venv_python(env)
    if not python.is_file():
        return None
    probe = subprocess.run(  # noqa: S603 — fixed argv, path derived from the env we own
        [str(python), "-c", _PROBE_ENV],
        capture_output=True,
        text=True,
        check=False,
    )
    if probe.returncode != 0:
        return None
    try:
        raw = json.loads(probe.stdout)
        return _EnvContents(
            dists={_norm(name) for name in raw["dists"] if name},
            site_dirs=[Path(d) for d in raw["site"]],
            python_version=str(raw["python"]),
            marker_env=raw["markers"],
        )
    except (ValueError, KeyError, TypeError):
        return None


def _installed(
    project: "Project", dist: "str | None", virtual: bool, contents: "_EnvContents | None"
) -> "bool | None":
    """Answer ``env show``'s installed column for one project against *contents*.

    A package project is installed when its distribution is present. A virtual
    one never has a distribution, so it is "installed" when the env satisfies
    its dependencies -- answered by the dependency preflight itself, pointed at
    the env's site directories, so ``show`` and the preflight cannot disagree.
    Markers are judged in the ENV's marker environment, not otto's. A
    preflight that cannot judge (dynamic dependencies, or no marker values from
    the env) answers None.
    *virtual* is :func:`is_virtual`'s answer, asked once by the caller.
    """
    if contents is None or dist is None:
        return None
    if not virtual:
        return _norm(dist) in contents.dists
    from .preflight import preflight

    if contents.marker_env is None:
        return None
    result = preflight([project], site_dirs=contents.site_dirs, marker_env=contents.marker_env)
    return None if result.warnings else not result.unsatisfied


def env_status() -> "EnvStatus":
    """Describe this workspace's environment. NEVER raises on a missing one.

    ``show`` is the verb you reach for when things look wrong, and a
    diagnostic that fails when things are broken is the one you needed most --
    so a missing env is a described state, not an error.
    """
    from .preflight import PYPROJECT

    env = env_path()
    projects = _discovered_projects()
    if not env.exists():
        return EnvStatus(env=env, exists=False, meta=None, projects=[])

    meta = read_meta(env)
    contents = _env_contents(env)
    stamp = meta_path(env)
    built_at = stamp.stat().st_mtime if stamp.is_file() else 0.0

    states: "list[ProjectState]" = []
    for project in projects:
        dist = _dist_name(project)
        virtual = is_virtual(project)
        pyproject = project.root / PYPROJECT
        states.append(
            ProjectState(
                name=project.name,
                dist_name=dist,
                installed=_installed(project, dist, virtual, contents),
                virtual=virtual,
                skipped=_skip_reason(project),
                stale=pyproject.is_file() and pyproject.stat().st_mtime > built_at,
            )
        )
    return EnvStatus(env=env, exists=True, meta=meta, projects=states)


def _norm(name: str) -> str:
    """PEP-503 normalization, through otto's one implementation."""
    from ..models.dependencies import normalize_name

    return normalize_name(name)
