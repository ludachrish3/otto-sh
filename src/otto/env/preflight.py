"""Check each repo's declared Python requirements against the running environment.

METADATA-ONLY, and that is the whole design. This module reads
``pyproject.toml`` as TEXT and ``*.dist-info`` as METADATA -- it imports
nothing from any repo, opens no socket and starts no subprocess. That is what
makes it safe to run on the path of every ordinary command, which is where the
answer is worth having: a missing Python dependency should stop a run before a
host is contacted, not halfway through one as an ``ImportError`` traceback.

Requirement source, in order:

1. **The installed distribution's metadata.** Look the repo's pyproject
   ``[project].name`` up with :mod:`importlib.metadata`; if the repo is
   installed here (editable or not), its ``Requires-Dist`` is the exact answer
   and the only one that can speak for ``dynamic = ["dependencies"]``.
2. **The pyproject's own ``[project.dependencies]``**, when it is not
   installed.
3. Dynamic AND not installed -> one warning, and no check. There is nothing to
   read, and guessing would refuse runs over a requirement nobody declared.

Base dependencies only (an extra's requirements are not ours) and direct
dependencies only (transitive consistency is the installer's promise, not
otto's).

``packaging`` is imported INSIDE the evaluation path, never at module scope.
It appears in zero import-budget snapshots, every repo without a pyproject
returns before reaching the evaluation, and every sample repo is that shape --
so the lazy import is what keeps a real run's footprint where it was. The
guard for it is a direct one (``test_importing_the_module_does_not_import_packaging``):
no measured surface runs the CLI preamble, so the budget snapshots cannot
witness this import moving.
"""

import dataclasses
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Iterable

PYPROJECT = "pyproject.toml"
"""The file a repo declares its Python requirements in."""


@dataclasses.dataclass(frozen=True)
class Unsatisfied:
    """One requirement a repo declares that this environment does not meet."""

    repo: str
    """The declaring repo's ``Repo.name``, so the message can name it."""

    requirement: str
    """The requirement AS THE REPO WROTE IT -- reprinted verbatim so the
    operator can paste it into an installer without translating otto's
    rendering back into a requirement string."""

    found: str
    """``"none"`` when the package is absent, else the version that IS
    installed. The distinction is the whole diagnostic value: absent means
    install it, and a version means the pin is wrong somewhere."""


@dataclasses.dataclass(frozen=True)
class PreflightResult:
    """Everything one preflight pass learned, with no severity attached.

    The two lists answer DIFFERENT questions and so cannot be one list. An
    :class:`Unsatisfied` is a verdict -- this environment does not meet a
    declared requirement -- and what it costs depends on whether the repo is
    part of this run. A warning says the check could not be MADE at all, which
    is not a verdict about the repo and is reported the same way regardless.
    """

    unsatisfied: "list[Unsatisfied]"
    """Requirements this environment does not meet, in repo order."""

    warnings: "list[str]"
    """One line per repo that could not be checked, already phrased for a user."""


def read_project_table(repo: Any) -> "dict[str, Any] | None":
    """Return *repo*'s pyproject ``[project]`` table, or None if there isn't one.

    None covers three shapes on purpose -- no pyproject, an unreadable one, and
    one with no ``[project]`` table -- because every caller here treats them
    identically: there is nothing to check, and no verdict to render.

    Unreadable is NOT an error this module reports. A malformed pyproject
    already has an owner: discovery frames a broken repo, and an installer
    refuses one in its own words. Diagnosing it a second time here would put
    otto's name on a syntax error it did not parse.
    """
    import tomli

    pyproject = Path(repo.sut_dir) / PYPROJECT
    if not pyproject.is_file():
        return None
    try:
        data = tomli.loads(pyproject.read_text())
    except (OSError, ValueError):  # tomli.TOMLDecodeError subclasses ValueError
        return None
    project = data.get("project")
    return project if isinstance(project, dict) else None


class _Environment:
    """The installed distributions a check reads, enumerated at most once.

    Enumeration is the only part of this module with a cost worth thinking
    about, so it happens LAZILY and once per :func:`preflight` call rather than
    once per repo. A workspace whose repos have no pyproject never enumerates
    at all -- which is every sample repo, and the reason the preflight is free
    on the suites that do not exercise it.
    """

    def __init__(self, site_dirs: "Iterable[Path] | None") -> None:
        self._site_dirs = None if site_dirs is None else [str(d) for d in site_dirs]
        self._dists: "dict[str, Any] | None" = None

    @property
    def dists(self) -> "dict[str, Any]":
        """Installed distributions keyed by PEP-503-normalized name."""
        if self._dists is None:
            from importlib import metadata

            from ..models.dependencies import normalize_name

            # Spelled as two calls rather than one with `**kwargs`: the
            # `distributions` overloads do not accept an untyped mapping, and
            # the branch is the honest shape anyway -- None means "the live
            # environment", a list means "only what these directories hold".
            dists = (
                metadata.distributions()
                if self._site_dirs is None
                else metadata.distributions(path=self._site_dirs)
            )
            found: "dict[str, Any]" = {}
            for dist in dists:
                name = dist.metadata["Name"]
                if not name:
                    continue
                # First wins: `distributions()` walks the path in order, so the
                # first hit is the one this interpreter would actually import.
                found.setdefault(normalize_name(str(name)), dist)
            self._dists = found
        return self._dists


def _requirements(
    repo: Any, project: "dict[str, Any]", env: _Environment
) -> "tuple[list[str], str | None]":
    """Return (*requirement strings*, *warning*) for one repo.

    The order is the contract, not a preference: an INSTALLED repo's
    ``Requires-Dist`` is what its interpreter will actually enforce, and a
    pyproject that has since gained a requirement describes a future install
    rather than this one.
    """
    from ..models.dependencies import normalize_name

    name = project.get("name")
    dist = env.dists.get(normalize_name(str(name))) if name else None
    if dist is not None:
        return [str(r) for r in (dist.requires or [])], None
    if "dependencies" in (project.get("dynamic") or []):
        return [], (
            f"cannot preflight {repo.name}: dependencies are dynamic and "
            f"{repo.name} is not installed in this environment"
        )
    return [str(d) for d in (project.get("dependencies") or [])], None


def _check(repo: Any, env: _Environment) -> "tuple[list[Unsatisfied], str | None]":
    """Evaluate one repo's requirements, returning its findings and any warning."""
    project = read_project_table(repo)
    if project is None:
        return [], None

    from packaging.requirements import InvalidRequirement, Requirement
    from packaging.version import InvalidVersion, Version

    from ..models.dependencies import normalize_name

    requirements, warning = _requirements(repo, project, env)
    if warning is not None:
        return [], warning

    unsatisfied: "list[Unsatisfied]" = []
    for raw in requirements:
        try:
            req = Requirement(raw)
        except InvalidRequirement:
            # Same reasoning as an unreadable pyproject: the installer owns
            # that diagnosis and gives a better one.
            continue
        # A marker that excludes this interpreter excludes this requirement.
        # `extra == "..."` evaluates False with no extra in context, which is
        # exactly how base-dependencies-only stays true for an installed
        # dist's `Requires-Dist`, where extras and base deps share one list.
        if req.marker is not None and not req.marker.evaluate():
            continue
        dist = env.dists.get(normalize_name(req.name))
        if dist is None:
            unsatisfied.append(Unsatisfied(repo=repo.name, requirement=raw, found="none"))
            continue
        version = str(dist.version)
        try:
            # Parse before comparing. A non-PEP-440 version in installed
            # metadata is unjudgeable, and the two packaging generations
            # disagree on how `contains` says so: <26 raises InvalidVersion,
            # >=26 answers False -- which would turn "cannot judge" into
            # "judged and failed". Parsing ourselves says it the same way on
            # both, and staying quiet beats a preflight that CRASHES or lies.
            parsed = Version(version)
        except InvalidVersion:
            continue
        # Prereleases count. The installer put this version here deliberately,
        # and refusing a run over a `2.0.0b1` it chose would be otto
        # second-guessing the resolver it defers to everywhere else.
        satisfied = req.specifier.contains(parsed, prereleases=True)
        if not satisfied:
            unsatisfied.append(Unsatisfied(repo=repo.name, requirement=raw, found=version))
    return unsatisfied, None


def check_repo(repo: Any, site_dirs: "Iterable[Path] | None" = None) -> "list[Unsatisfied]":
    """Return the requirements *repo* declares that this environment does not meet.

    *repo* is duck-typed: only ``.name`` and ``.sut_dir`` are read. That is
    deliberate -- it keeps this module free of ``otto.config`` and makes the
    evaluator testable against a two-attribute stand-in.

    *site_dirs* exists so tests can stage a fabricated site directory. Real
    callers pass None and the live environment answers.
    """
    unsatisfied, _ = _check(repo, _Environment(site_dirs))
    return unsatisfied


def preflight(repos: "Iterable[Any]", site_dirs: "Iterable[Path] | None" = None) -> PreflightResult:
    """Check every repo and report what this environment does not meet.

    Raises nothing. The caller decides what a finding costs -- severity is an
    activation question, and this module deliberately knows nothing about
    activation.
    """
    env = _Environment(site_dirs)
    unsatisfied: "list[Unsatisfied]" = []
    warnings: "list[str]" = []
    for repo in repos:
        found, warning = _check(repo, env)
        unsatisfied.extend(found)
        if warning is not None:
            warnings.append(warning)
    return PreflightResult(unsatisfied=unsatisfied, warnings=warnings)
