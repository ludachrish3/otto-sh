r"""otto's composition root: repo discovery + contained user-code registration.

Replaces ``config``'s import-time side effects. Phase 1 (*discovery*)
parses the environment and repo ``settings.toml`` files — no user code runs.
Between phase 1 and phase 2 the *dependency pass* (``config.dependencies``)
validates each repo's declared dependencies, skips repos whose required deps
are unsatisfied (framed ``DependencyError``\ s), and orders phase-2 registration
topologically (stable — sut-dir order when no deps are declared). Phase 2
(*registration*) imports each repo's init modules and test files, wrapping
every user-module exec so one broken file becomes a framed
:class:`BootstrapError` instead of bricking the process. Lab loading is
deliberately NOT part of bootstrap — it happens lazily at first access.

``bootstrap()`` is idempotent: the CLI entrypoint calls it before argv
parsing, ``open_context()`` calls it lazily, and repeated calls return the
same :class:`BootstrapResult`.
"""

import importlib
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from .errors import OttoError

if TYPE_CHECKING:
    from .config.repo import Repo
    from .models.settings import OttoEnvSettings


class BootstrapError(OttoError):
    """One user file failed to load during bootstrap registration."""

    def __init__(self, sut_dir: Any, source: str, cause: BaseException) -> None:
        """Frame *cause* as ``repo <sut_dir>: failed to load <source>``."""
        super().__init__(f"repo {sut_dir}: failed to load {source}: {cause!r}")
        self.sut_dir = sut_dir
        self.source = source
        self.__cause__ = cause


class DependencyError(BootstrapError):
    """A repo's declared dependencies cannot be satisfied (or a dependency was skipped)."""

    def __init__(self, sut_dir: Any, message: str) -> None:
        """Frame *message* as ``repo <sut_dir>: <message>``."""
        Exception.__init__(self, f"repo {sut_dir}: {message}")
        self.sut_dir = sut_dir
        self.source = "dependencies"


@dataclass(frozen=True)
class BootstrapWarning:
    """A non-fatal dependency finding: rendered at startup, never gates dispatch."""

    sut_dir: Any
    message: str


@dataclass(frozen=True)
class DiscoveryResult:
    """Everything phase 1 produced: environment, repos, contained errors.

    Separate from :class:`BootstrapResult` rather than reused: ``warnings``
    comes from the dependency pass, which runs inside :func:`bootstrap` after
    discovery has returned, so a shared type would carry a field that is
    structurally always empty on this path.
    """

    env: "OttoEnvSettings"
    repos: list["Repo"]
    errors: list[BootstrapError] = field(default_factory=list)


@dataclass(frozen=True)
class BootstrapResult:
    """Everything bootstrap produced: environment, repos, contained errors."""

    env: "OttoEnvSettings"
    repos: list["Repo"]
    errors: list[BootstrapError] = field(default_factory=list)
    warnings: list[BootstrapWarning] = field(default_factory=list)


_discovered: "DiscoveryResult | None" = None
_result: BootstrapResult | None = None
_completion_names: dict[str, Any] | None = None


def discover() -> DiscoveryResult:
    """Phase 1: env + repo discovery (settings parse only — no user code). Cached.

    Per-repo config-data failures (unreadable or malformed ``settings.toml``)
    get the same containment as phase-2 user-code failures: the repo is
    skipped and a framed :class:`BootstrapError` is recorded, surfacing via
    ``bootstrap().errors`` (help degrades, real dispatch fails loud).
    Env-level failures (bad ``OTTO_SUT_DIRS`` / OTTO_* values) still raise —
    with no environment there is nothing to degrade to.

    The errors ride the cached result itself: recomputing discovery (after
    :func:`invalidate`) necessarily recomputes them, so a stale error cannot
    outlive the discovery that produced it.
    """
    global _discovered  # noqa: PLW0603 — module-level singleton/cache
    if _discovered is None:
        from .config.env import load_otto_env
        from .config.repo import TOML_SETTINGS_PATH, Repo

        env = load_otto_env()
        repos: list[Repo] = []
        errors: list[BootstrapError] = []
        for sut_dir in env.sut_dirs:
            try:
                repos.append(Repo(sut_dir=sut_dir))
            except Exception as e:  # noqa: PERF203,BLE001 — containment seam: per-item resilience, ANY config-data failure becomes a framed error
                errors.append(BootstrapError(sut_dir, str(TOML_SETTINGS_PATH), e))
        _discovered = DiscoveryResult(env=env, repos=repos, errors=errors)
    return _discovered


def bootstrap() -> BootstrapResult:
    """Run the composition root (idempotent): discovery, dependency pass, registration."""
    global _result  # noqa: PLW0603 — module-level singleton/cache
    if _result is not None:
        return _result
    discovered = discover()
    env, repos = discovered.env, discovered.repos
    errors: list[BootstrapError] = list(discovered.errors)
    from .config.dependencies import resolve_dependencies

    resolution = resolve_dependencies(repos)
    errors.extend(resolution.errors)
    for repo in resolution.ordered:
        repo.add_libs_to_pythonpath()
        for mod in repo.init:
            try:
                importlib.import_module(mod)
            except Exception as e:  # noqa: PERF203,BLE001 — containment seam: per-item resilience, ANY user-code failure becomes a framed error
                errors.append(BootstrapError(repo.sut_dir, mod, e))
        for test_file in repo.iter_test_files():
            try:
                repo.import_test_file(test_file)
            except Exception as e:  # noqa: PERF203,BLE001 — containment seam: per-item resilience, ANY user-code failure becomes a framed error
                errors.append(BootstrapError(repo.sut_dir, test_file.name, e))
    _result = BootstrapResult(env=env, repos=repos, errors=errors, warnings=resolution.warnings)
    return _result


def set_completion_names(names: "dict[str, Any] | None") -> None:
    """Install the completion-cache snapshot (fast path; set by the CLI entry)."""
    global _completion_names  # noqa: PLW0603 — module-level singleton/cache
    _completion_names = names


def get_completion_names() -> "dict[str, Any] | None":
    """Return the completion-cache snapshot, or None outside the fast path."""
    return _completion_names


def invalidate() -> None:
    """Drop every cached bootstrap result so the next call recomputes.

    The supported recovery path for long-lived embedders: fix the repo or the
    environment, call ``invalidate()``, and re-run :func:`bootstrap` — the
    prior discovery's errors are discarded together with the discovery that
    produced them.
    """
    global _discovered, _result, _completion_names  # noqa: PLW0603 — module-level singleton/cache
    _discovered = None
    _result = None
    _completion_names = None


def _reset() -> None:
    """Clear all bootstrap state (test hook; alias of :func:`invalidate`)."""
    invalidate()
