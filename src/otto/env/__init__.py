"""The per-workspace orchestration venv: where it is, and what it records.

The venv sits at ``<workspace home>/env`` -- singular, because the workspace
keying already happened one directory up (``otto.config.home``). Nothing here
re-derives that key.

Its metadata lives INSIDE the venv, at ``env/.otto-env.json``. That placement
is the contract: ``create --force`` removes ``env/`` wholesale, so the metadata
goes with it and a rebuild can never inherit the previous run's backend. Stored
beside the venv instead, it would survive the rebuild and silently pin what the
operator was trying to escape.

Reads are TOTAL: absent, corrupt, and half-written metadata all read as None
rather than raising, because the caller most likely to meet a broken env is
``create --force``, whose whole job is to replace it -- and it cannot do that
if merely looking at the env throws.
"""

import dataclasses
import json
import shutil
import subprocess
from importlib import metadata
from pathlib import Path
from typing import TYPE_CHECKING
from urllib.parse import urlparse
from urllib.request import url2pathname

from ..errors import OttoError
from ..version import get_version
from .backends import BackendUnavailableError

if TYPE_CHECKING:
    from ..config.repo import Repo

META_FILENAME = ".otto-env.json"

_LIST_DISTS = (
    "import importlib.metadata as m\n"
    "print('\\n'.join(d.metadata['Name'] or '' for d in m.distributions()))"
)
"""One-liner run INSIDE the env to enumerate what it holds. Asking the env's
own interpreter is the only answer that stays true across uv and pip layouts."""

_ATTRIBUTABLE = 2
"""How many repo names a resolver message must implicate before otto adds its
one attribution line. Below two there is nothing to attribute BETWEEN, and a
guess would send the operator to edit the wrong pyproject."""


@dataclasses.dataclass(frozen=True)
class EnvMeta:
    """What an existing orchestration venv records about how it was built."""

    backend: str
    """``"uv"`` or ``"pip"`` -- re-used by ``sync`` so an env stays self-consistent."""

    otto_version: str
    """The otto version installed into it, for ``env show``'s staleness line."""


def env_path(sut_dirs: "list[Path] | None" = None) -> Path:
    """Return this workspace's orchestration venv path. Pure -- never creates."""
    from ..config.home import workspace_home

    return workspace_home(sut_dirs) / "env"


def meta_path(env: Path) -> Path:
    """Return the metadata file path for the venv at *env*."""
    return env / META_FILENAME


def read_meta(env: Path) -> "EnvMeta | None":
    """Return the venv's recorded metadata, or None if it cannot be read.

    Total by design -- see the module docstring.
    """
    try:
        raw = json.loads(meta_path(env).read_text())
        return EnvMeta(backend=raw["backend"], otto_version=raw["otto_version"])
    except (OSError, ValueError, KeyError, TypeError):
        return None


def write_meta(env: Path, meta: EnvMeta) -> None:
    """Record *meta* inside the venv at *env*."""
    meta_path(env).write_text(json.dumps(dataclasses.asdict(meta), indent=2) + "\n")


class EnvExistsError(OttoError, RuntimeError):
    """``env create`` found an environment already built for this workspace."""


def _installable(repos: "list[Repo]") -> "tuple[list[Repo], list[Repo]]":
    """Split discovered repos into (installable, skipped).

    Installable means "has a pyproject.toml". A repo without one is not an
    error and never has been: its ``libs`` ride ``sys.path`` at bootstrap, and
    that is still correct. Every sample repo but repo4 is in the skipped half,
    so a workspace of them must still build a working env.
    """
    installable = [r for r in repos if (r.sut_dir / "pyproject.toml").is_file()]
    skipped = [r for r in repos if r not in installable]
    return installable, skipped


def _settings_backend(repos: "list[Repo]") -> "str | None":
    """Return the ``[env] backend`` the repos agree on, or None if none declares one.

    A workspace is several repos by definition, so two of them CAN declare
    different installers. Picking one silently would bind the wrong answer
    with no indication it happened; this refuses instead, naming both, the
    same way ``[monitor]`` TLS disagreement is refused in ``cli/monitor.py``.
    """
    declaring = [(r.name, r.env_backend) for r in repos if r.env_backend]
    if not declaring:
        return None
    chosen = {value for _, value in declaring}
    if len(chosen) > 1:
        names = ", ".join(sorted(name for name, _ in declaring))
        raise BackendUnavailableError(
            f"[env] backend disagrees across repos ({names}) — make them identical, "
            "declare it in only one settings.toml, or pass --backend for this run"
        )
    return declaring[0][1]


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


def _attribute_conflict(repos: "list[Repo]", stderr: str) -> "str | None":
    """Name the two repos whose installs collided, if the message implicates them.

    otto NEVER resolves and never rewrites the resolver's text. This adds ONE
    line, and only when it can actually attribute the failure -- a guess here
    would be worse than silence, because it would send the operator to edit the
    wrong pyproject.

    ONLY BARE NAMES COUNT -- every path-shaped word is dropped before matching,
    and that is not tidiness. The workspace key is ``<hash>-<slug>`` with the
    slug built from the repo basenames, so EVERY installer message that echoes
    the environment path contains every repo in the workspace. Matching the raw
    text therefore attributed an ordinary "setuptools not found" build failure
    to two repos that had not collided at all. A resolver naming a package in a
    conflict names it bare; a path is the one place a name means nothing.
    """
    prose = " ".join(w for w in stderr.lower().split() if "/" not in w and "\\" not in w)
    named = [r.name for r in repos if r.name.lower() in prose]
    if len(named) < _ATTRIBUTABLE:
        return None
    return f"the conflicting requirements came from: {', '.join(sorted(named)[:2])}"


class EnvBuildError(OttoError, RuntimeError):
    """An installer refused. Carries the resolver's own text, unrewritten."""


@dataclasses.dataclass(frozen=True)
class EnvBuild:
    """What a ``create``/``sync`` run did, for the CLI to present.

    Wider than the :class:`EnvMeta` it carries because the skip notice is
    contractual: a workspace of repos without pyprojects must still build, and
    the operator has to be told which repos were passed over and why.
    """

    env: Path
    meta: EnvMeta
    installed: "list[str]"
    skipped: "list[str]"


def _relay(result: "subprocess.CompletedProcess[str]", repos: "list[Repo]", what: str) -> None:
    """Raise :class:`EnvBuildError` carrying the installer's own text, if it failed.

    otto NEVER resolves and never rewrites the resolver's message. At most it
    appends one attribution line, and only when it can actually name the two
    repos that collided.
    """
    if result.returncode == 0:
        return
    text = (result.stderr or result.stdout or "").rstrip()
    attribution = _attribute_conflict(repos, text)
    raise EnvBuildError(f"{what} failed:\n{text}" + (f"\n{attribution}" if attribution else ""))


def _fill(env: Path, backend: str, repos: "list[Repo]", passthrough: "list[str]") -> "EnvBuild":
    """Install every installable repo plus otto itself into the venv at *env*."""
    from .backends import install

    installable, skipped = _installable(repos)
    if installable:
        targets: "list[str]" = []
        for repo in installable:
            targets += ["-e", str(repo.sut_dir)]
        _relay(install(backend, env, targets, passthrough), repos, "installing repos")
    _relay(install(backend, env, _otto_target(), passthrough), repos, "installing otto")
    meta = EnvMeta(backend=backend, otto_version=get_version())
    write_meta(env, meta)
    return EnvBuild(
        env=env,
        meta=meta,
        installed=[r.name for r in installable],
        skipped=[r.name for r in skipped],
    )


def create_env(
    *,
    force: bool = False,
    backend_flag: "str | None" = None,
    passthrough: "list[str] | None" = None,
) -> "EnvBuild":
    """Build this workspace's orchestration venv from scratch.

    Refuses an existing env naming ``--force``; ``--force`` removes it first,
    which is the recovery story for a wedged env. The recorded backend is read
    AFTER that removal on purpose (F6): a rebuild must not inherit the backend
    the operator is trying to escape, and the metadata living inside the venv
    is what makes that automatic rather than remembered.
    """
    from .backends import create_venv, select_backend

    repos = _discovered_repos()
    env = env_path()
    if env.exists():
        if not force:
            raise EnvExistsError(
                f"an environment already exists at {env} — pass --force to remove and "
                "rebuild it, or run `otto env sync` to update it in place"
            )
        shutil.rmtree(env)
    recorded = read_meta(env)
    backend = select_backend(
        backend_flag, _settings_backend(repos), recorded.backend if recorded else None
    )
    _relay(create_venv(backend, env), repos, "creating the venv")
    return _fill(env, backend, repos, passthrough or [])


def sync_env(
    *,
    backend_flag: "str | None" = None,
    passthrough: "list[str] | None" = None,
) -> "EnvBuild":
    """Bring this workspace's orchestration venv up to date, creating it if absent.

    NEVER destroys anything. It is the verb every error message names, so it
    has to stay safe to suggest -- including when the thing it is suggested
    for is that there is no env at all, which is why a missing env delegates
    to :func:`create_env` rather than refusing.
    """
    from .backends import select_backend

    env = env_path()
    if not env.exists():
        return create_env(force=False, backend_flag=backend_flag, passthrough=passthrough)
    repos = _discovered_repos()
    recorded = read_meta(env)
    backend = select_backend(
        backend_flag, _settings_backend(repos), recorded.backend if recorded else None
    )
    return _fill(env, backend, repos, passthrough or [])


def _discovered_repos() -> "list[Repo]":
    """Return the DISCOVERED repo set, not the active one.

    An environment is workspace-scoped: which labs today's command happens to
    load must not change what gets installed into it.
    """
    from ..bootstrap import discover

    return discover().repos


@dataclasses.dataclass(frozen=True)
class RepoState:
    """One repo's standing in the environment, as ``env show`` reports it."""

    name: str
    dist_name: "str | None"
    """``[project] name`` from its pyproject, or None when it has none (and so
    is not installable at all)."""

    installed: "bool | None"
    """True/False when the env could be asked, None when it could not."""

    stale: bool
    """Its pyproject is newer than the env's metadata -- the env was built
    before the repo's requirements last changed."""


@dataclasses.dataclass(frozen=True)
class EnvStatus:
    """Everything ``env show`` prints, with no presentation attached."""

    env: Path
    exists: bool
    meta: "EnvMeta | None"
    repos: "list[RepoState]"


def _dist_name(repo: "Repo") -> "str | None":
    """Return the repo's distribution name from its pyproject, if it has one."""
    import tomli

    pyproject = repo.sut_dir / "pyproject.toml"
    if not pyproject.is_file():
        return None
    try:
        data = tomli.loads(pyproject.read_text())
    except (OSError, tomli.TOMLDecodeError):
        return None
    name = data.get("project", {}).get("name")
    return str(name) if name else None


def _installed_dists(env: Path) -> "set[str] | None":
    """Return the normalized distribution names inside *env*, or None if unaskable.

    One subprocess for the whole env rather than one per repo, and None rather
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
    repos = _discovered_repos()
    if not env.exists():
        return EnvStatus(env=env, exists=False, meta=None, repos=[])

    meta = read_meta(env)
    present = _installed_dists(env)
    stamp = meta_path(env)
    built_at = stamp.stat().st_mtime if stamp.is_file() else 0.0

    states: "list[RepoState]" = []
    for repo in repos:
        dist = _dist_name(repo)
        pyproject = repo.sut_dir / "pyproject.toml"
        states.append(
            RepoState(
                name=repo.name,
                dist_name=dist,
                installed=None if present is None or dist is None else _norm(dist) in present,
                stale=pyproject.is_file() and pyproject.stat().st_mtime > built_at,
            )
        )
    return EnvStatus(env=env, exists=True, meta=meta, repos=states)


def _norm(name: str) -> str:
    """PEP-503 normalization, through the repo's one implementation."""
    from ..models.dependencies import normalize_name

    return normalize_name(name)
