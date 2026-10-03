"""Repo settings loading, parsing, and test-collection helpers for SUT repositories."""

import importlib
import logging
import sys
from dataclasses import (
    dataclass,
    field,
)
from pathlib import Path
from typing import (
    TYPE_CHECKING,
    Any,
)

import tomli

from .scope import ProjectScopeConfig
from .version import Version

if TYPE_CHECKING:
    from importlib.machinery import ModuleSpec

    import pytest
    from rich.panel import Panel
    from rich.text import Text

    from ..declared import DeclaredEntry
    from ..host.os_profile import OsProfile
    from ..labs.sources import CompiledLabSource
    from ..models.dependencies import ParsedDependency
    from ..models.settings import OsProfileSpec
    from ..registry import RegistrationRefused
    from .dependencies import ResolvedDependency

logger = logging.getLogger(__name__)

SETTINGS_FILENAME = "settings.toml"
TOML_SETTINGS_PATH = Path(".otto") / SETTINGS_FILENAME


ARCHIVE_SUFFIXES = (".tar", ".tar.gz", ".tgz", ".tar.bz2", ".tar.xz")
"""Context file names docker reads as a tar archive on ``docker build -``."""


@dataclass(frozen=True)
class DockerImage:
    """A Dockerfile-built image declared by a project."""

    name: str
    """The image repository name docker sees, verbatim; no tag."""

    dockerfile: Path
    """Absolute path to the Dockerfile."""

    context: Path
    """Absolute path to the build context directory."""

    target: str | None = None
    """Optional multi-stage build target."""

    build_args: tuple[tuple[str, str], ...] = ()
    """Frozen list of (name, value) build args. Tuples (not dicts) so the
    container is hashable and order is preserved."""

    dockerfile_in_archive: str = ""
    """For an archive ``context``: the Dockerfile's path inside the archive, as written."""

    @property
    def is_archive(self) -> bool:
        """True when ``context`` is a tar archive given to docker unopened."""
        return self.context.name.endswith(ARCHIVE_SUFFIXES)


@dataclass(frozen=True)
class DockerCompose:
    """A docker-compose file contributed by a project."""

    path: Path
    """Absolute path to the compose YAML file."""

    services: tuple[str, ...] = ()
    """Service names declared in the compose file.

    Authoritative, not a hint: this is the list container hosts are registered
    from, and it is what lets tab-completion synthesize ids without parsing
    YAML on the completion fast path. The legacy per-repo ``compose_up`` path
    additionally compares it with ``docker compose config --services`` once the
    stack is up and warns on drift; the use-case deploy path registers from the
    declaration alone.
    """

    name: str = ""
    """Handle use-case fragments reference; the spec fills the path stem when unset."""

    users: tuple[tuple[str, str], ...] = ()
    """Frozen, sorted (service, user) pairs from ``users = {...}``: the declared
    default access user per service. Tuples rather than a dict so the container
    stays hashable, matching ``build_args``. Values go to docker verbatim."""


@dataclass(frozen=True)
class DockerUseCase:
    """One ``[[docker.use_cases]]`` fragment (spec §3.1): participation + placement atom."""

    name: str
    """Use-case this fragment belongs to; same name across repos = one use-case."""
    composes: "tuple[str, ...]"
    """Handles into this repo's [[docker.composes]]."""
    role: "str | None" = None
    """Placement role, resolved against `roles` host tags in the owning repo's scope."""
    placement: "dict[str, str]" = field(default_factory=dict)
    """Committed role→host pins.

    A value may be lab-qualified (``unix:test3``) for multi-lab sessions.
    """
    provides: "str | None" = None
    """Capability this fragment offers to the provider competition (spec §4)."""
    priority: int = 0
    """Competition rank; higher wins; only meaningful with provides."""
    env: "dict[str, str]" = field(default_factory=dict)
    """Channel-1 static env; values may use ``${otto:...}`` fact refs (spec §6)."""
    pass_env: "tuple[str, ...]" = ()
    """Allowlisted variable names copied from the invoking user's shell."""


@dataclass(frozen=True)
class DockerSettings:
    """Per-repo docker configuration parsed from `[docker]` in `settings.toml`."""

    images: tuple[DockerImage, ...] = ()
    """Images this project knows how to build."""

    composes: tuple[DockerCompose, ...] = ()
    """Compose files this project contributes."""

    use_cases: tuple[DockerUseCase, ...] = ()
    """`[[docker.use_cases]]` fragments this project contributes."""


@dataclass(frozen=True)
class MonitorSettings:
    """Per-repo monitor configuration parsed from `[monitor]` in `settings.toml`.

    ``tls_cert``/``tls_key`` point at a PEM certificate/key on the machine that
    runs ``otto monitor`` (conventionally under ``~/.otto/tls/`` — the
    committed settings value is shared team-wide, so it must not name a
    machine-local absolute path). ``tls_key`` may stay ``None`` when the cert
    PEM bundles the private key.
    """

    tls_cert: Path | None = None
    tls_key: Path | None = None


def selectable_names(classes: list[str], name: str) -> list[str]:
    """Return the names one test answers to: its base name, its classes, ``Class::test``.

    *name* is collapsed to its base (``test_x[a]`` → ``test_x``); ``Class`` in
    the pair is the class the test is defined in directly, at whatever depth
    that class is nested. Completion (static and collected) and the
    did-you-mean hint of an unknown name all offer exactly these, so they
    cannot disagree on shape.
    """
    base = name.partition("[")[0]
    return [base, *classes, *([f"{classes[-1]}::{base}"] if classes else [])]


def classes_from_nodeid(nodeid: str, name: str) -> list[str]:
    """Return the classes a collected test is nested in, outermost first, from its node ID.

    ``tests/t.py::TestOuter::TestInner::test_x[a::b]`` with *name*
    ``test_x[a::b]`` gives ``["TestOuter", "TestInner"]``. The test's own
    *name* is cut off the end first, so a parametrization id that itself
    contains ``::`` never reads as a class.
    """
    head = nodeid.removesuffix(name).removesuffix("::")
    return head.split("::")[1:]


def collect_failure_reason(report: "pytest.CollectReport") -> str:
    """Say in one line why a failed collection *report* failed.

    The last line of pytest's report is the exception itself
    (``E   SyntaxError: ...``); the traceback above it is noise in a one-line
    log record or a cache entry.
    """
    lines = report.longreprtext.strip().splitlines()
    return lines[-1].removeprefix("E").strip() if lines else "no detail"


def registration_refusal(exc: BaseException) -> "RegistrationRefused | None":
    """Return the registration refusal behind *exc*, if a refused registration is what failed.

    A conftest's own exception reaches pytest wrapped in
    ``ConftestImportFailure``, whose ``cause`` is the original.
    """
    from ..registry import RegistrationRefused

    cause = getattr(exc, "cause", exc)
    return cause if isinstance(cause, RegistrationRefused) else None


def marker_name(entry: str) -> str:
    """Reduce one ``markers`` ini line to the marker's name.

    ``"slow: marks slow tests"`` and ``"timeout(timeout, method=None)"`` give
    ``slow`` and ``timeout``; a blank entry gives ``""``.
    """
    return entry.split(":", 1)[0].split("(", 1)[0].strip()


#: Every filename pytest looks for its config in, in ITS order —
#: `_pytest.config.findpaths`'s `config_names`. otto parses none of them: each
#: is a stat key of a test table's `env`, so editing, adding or removing any
#: one of them sends the table back to a whole-tree collection.
PYTEST_CONFIG_NAMES: tuple[str, ...] = (
    "pytest.toml",
    ".pytest.toml",
    "pytest.ini",
    ".pytest.ini",
    "pyproject.toml",
    "tox.ini",
    "setup.cfg",
)


def pytest_config_paths(sut_dir: Path) -> list[Path]:
    """Every file pytest may read its config from in *sut_dir*, existing or not.

    Non-existent paths are included on purpose: a test table stores a
    ``None`` stat for each, which is what lets it see someone ADD a
    ``pytest.ini``.
    """
    return [sut_dir / name for name in PYTEST_CONFIG_NAMES]


@dataclass(frozen=True)
class CompiledSettings:
    """Every value :meth:`Repo.parse_settings` assigns, compiled from one settings document.

    Produced by :func:`compile_settings`, which registers nothing: the OS
    profiles travel as validated specs, and only ``Repo.parse_settings``
    registers them.
    """

    name: str
    version: Version
    lab_sources: list["CompiledLabSource"]
    project_scope: ProjectScopeConfig | None
    libs: list[Path]
    tests: list[Path]
    init: list[str]
    declared_dependencies: list["ParsedDependency"]
    host_preferences: dict[str, dict[str, Any]]
    logging_levels: dict[str, str]
    os_profiles: dict[str, "OsProfileSpec"]
    docker_settings: DockerSettings
    monitor_settings: MonitorSettings
    env_backend: str | None
    declared_products: list["DeclaredEntry"]
    declared_dev_tools: list["DeclaredEntry"]


def _check_os_profile_section(name: str, prof: "OsProfileSpec") -> None:
    """Check one ``[os_profiles.<name>]`` table, naming the table in a refusal."""
    from ..host.os_profile import check_os_profile

    try:
        check_os_profile(name, prof.base, prof.defaults)
    except ValueError as e:
        raise ValueError(f"[os_profiles.{name}]: {e}") from e


def compile_settings(data: dict[str, Any], sut_dir: Path) -> CompiledSettings:
    """Validate and compile one parsed ``.otto/settings.toml`` exactly as the loader does.

    Pure: reads no file of its own, registers nothing. The loader
    (:meth:`Repo.parse_settings`) and ``otto init``'s doctor
    (:func:`validate_settings`) both call it, so a repo the doctor passes is
    a repo that loads.

    Raises:
        pydantic_core.ValidationError: the document does not match ``SettingsModel``.
        ValueError: a later compile step refused it — a lab source, a
            dependency entry, or an OS profile (its message starts with the
            ``[os_profiles.<name>]`` table).
    """
    # ``otto.models``'s package __init__ boots otto.host first to avoid an
    # import cycle (os_profile's eager registration <-> models.host); see the
    # note in src/otto/models/__init__.py. So these imports are safe here.
    from ..labs.sources import compile_lab_sources
    from ..models.dependencies import parse_dependency_entry
    from ..models.settings import SettingsModel

    model = SettingsModel.model_validate(data, context={"sut_dir": sut_dir})
    lab_sources = compile_lab_sources(model.lab, repo_name=model.name, sut_dir=sut_dir)
    # Compiled here, at parse, so an unusable regex is a settings error rather
    # than a fleet walk that silently matches nothing later on.
    project_scope = ProjectScopeConfig.from_spec(model.project) if model.project else None
    declared_dependencies = [
        parse_dependency_entry(e, required=True) for e in model.dependencies.required
    ] + [parse_dependency_entry(e, required=False) for e in model.dependencies.optional]
    for name, prof in model.os_profiles.items():
        _check_os_profile_section(name, prof)
    return CompiledSettings(
        name=model.name,
        version=Version(model.version),
        lab_sources=lab_sources,
        project_scope=project_scope,
        libs=list(model.libs),
        tests=list(model.tests),
        init=list(model.init),
        declared_dependencies=declared_dependencies,
        host_preferences={
            sel: {k: (list(v) if isinstance(v, list) else dict(v)) for k, v in entries.items()}
            for sel, entries in model.host_preferences.items()
        },
        logging_levels=dict(model.logging.levels),
        os_profiles=dict(model.os_profiles),
        docker_settings=model.docker.to_runtime(),
        monitor_settings=model.monitor.to_runtime(),
        env_backend=model.env.backend,
        declared_products=[
            e.to_runtime(owner=model.name, base_dir=sut_dir, seam="products")
            for e in model.products
        ],
        declared_dev_tools=[
            e.to_runtime(owner=model.name, base_dir=sut_dir, seam="dev_tools")
            for e in model.dev_tools
        ],
    )


def validate_settings(root: Path) -> list[str]:
    """Return ``[]`` when *root*'s settings load, else one problem line naming the file.

    Reads ``.otto/settings.toml`` and runs :func:`compile_settings` — the
    loader's own compile, so ``otto init``'s doctor cannot pass settings the
    loader refuses. The line carries the TOML parse error, the pydantic error
    (rendered by :func:`~otto.models.base.compact_validation_error`, never
    ``str(ValidationError)``, whose ``input_value=`` can echo a secret), or
    the compile error.
    """
    from pydantic import ValidationError

    from ..models.base import compact_validation_error

    # Absolute, as ``Repo.__post_init__`` makes it, so anchored paths and the
    # file named in the problem line match what the loader sees.
    root = root.absolute()
    path = root / TOML_SETTINGS_PATH
    try:
        data = tomli.loads(path.read_bytes().decode())
    except (tomli.TOMLDecodeError, UnicodeDecodeError, OSError) as e:
        return [f"{path}: {e}"]
    try:
        compile_settings(data, root)
    except ValidationError as e:
        return [f"{path}: {compact_validation_error(e)}"]
    except ValueError as e:
        return [f"{path}: {e}"]
    return []


def find_init_module(name: str, libs: list[Path]) -> "ModuleSpec | None":
    """Find init module *name* the way the loader will import it, executing nothing.

    The loader appends each ``libs`` directory to ``sys.path`` and calls
    :func:`importlib.import_module`; this asks the same finder,
    :class:`importlib.machinery.PathFinder`, over ``[*sys.path, *libs]`` —
    ``sys.path`` first, then ``libs``, because bootstrap appends them. A
    dotted name is walked one segment at a time through each parent's
    ``submodule_search_locations``, so a package, a single-file module, a
    dotted name and a namespace package all resolve, and a name whose parent
    is a plain module does not. Returns ``None`` when nothing matches. Only
    ``PathFinder`` is asked, so built-in and frozen modules resolve to
    ``None`` too (no init module is built in).

    ``importlib.invalidate_caches()`` runs first: ``otto init`` writes a
    module and checks it in one process, and FileFinder's cached directory
    listing is keyed on an mtime a fast write can leave unchanged. The
    empty-segment check before it is a shortcut that skips that invalidation
    for a name no finder could match.
    """
    from importlib.machinery import PathFinder

    parts = name.split(".")
    if not all(parts):
        return None
    importlib.invalidate_caches()
    search = sys.path + [str(lib) for lib in libs]
    spec = None
    for depth in range(1, len(parts) + 1):
        spec = PathFinder.find_spec(".".join(parts[:depth]), search)
        if spec is None:
            return None
        if depth < len(parts):
            if spec.submodule_search_locations is None:
                return None
            search = list(spec.submodule_search_locations)
    return spec


@dataclass
class Repo:
    """Runtime representation of a single SUT (system-under-test) repository.

    Parsed from ``.otto/settings.toml`` at construction time (via
    ``__post_init__``).  Holds the resolved paths for lab data, test
    directories, and init modules, plus Docker and OS-profile settings
    contributed by that repo.  Multiple ``Repo`` instances are managed by
    the config package when ``OTTO_SUT_DIRS`` lists more than one
    directory.
    """

    sut_dir: Path
    """SUT directory from which the settings came."""

    name: str = field(init=False)
    """Product/repo name"""

    version: Version = field(init=False)
    """Product version"""

    lab_sources: list["CompiledLabSource"] = field(default_factory=list, init=False)
    """Compiled ``[[lab.sources]]`` declarations, in declaration order.

    Built by :func:`otto.labs.sources.compile_lab_sources` at parse, so a
    malformed declaration is a settings error rather than a backend that fails
    to construct much later. The single input to the process-wide
    ``build_lab_sources`` construction seam; also read directly by the
    completion cache (fingerprint + raw link scan) via
    :meth:`~otto.labs.sources.CompiledLabSource.lab_files`."""

    project_scope: ProjectScopeConfig | None = field(default=None, init=False)
    """Compiled ``[project]`` declaration — the labs and hosts this repo targets.

    ``None`` when the repo declares no ``[project]`` table, which every
    consumer reads through :func:`otto.config.scope.repo_targets` as "targets
    everything"; that is what the whole-lab fallback for product-less repos is
    built on. Membership is never re-derived from this by hand — the predicate
    is the only reader of the patterns."""

    libs: list[Path] = field(default_factory=list[Path], init=False)
    """Extra paths to add to the PYTHONPATH"""

    init: list[str] = field(default_factory=list[str], init=False)
    """Module paths that need to be imported during `otto` init.

    Modules containing instructions are an example of modules that need to be imported eagerly.
    """

    tests: list[Path] = field(default_factory=list[Path], init=False)
    """Directories that contain test suites."""

    declared_dependencies: list["ParsedDependency"] = field(default_factory=list, init=False)
    """Parsed ``[dependencies]`` entries — required first, then optional, declaration order."""

    declared_products: list["DeclaredEntry"] = field(default_factory=list, init=False)
    """Parsed ``[[products]]`` entries, in runtime form (``owner``/``base_dir`` stamped
    at parse time). Consumed by :func:`otto.declared.declared_for_host`."""

    declared_dev_tools: list["DeclaredEntry"] = field(default_factory=list, init=False)
    """The identical schema's ``[[dev_tools]]`` twin — see :attr:`declared_products`."""

    dependencies: list["ResolvedDependency"] = field(default_factory=list, init=False)
    """Per-dependency resolution outcome; populated by bootstrap's dependency pass.

    This list is the runtime query surface: ``bootstrap().repos`` gives global
    name→version, this gives the structured per-repo view. Statuses reflect
    the *discovered* set only, not registration success: a ``"satisfied"``
    optional dependency whose provider repo was itself skipped (unsatisfied
    required deps of its own, or a dependency cycle) still shows
    ``"satisfied"`` here — that gap surfaces separately as a startup
    ``BootstrapWarning``, not as a status change."""

    host_preferences: dict[str, dict[str, Any]] = field(
        default_factory=dict,
        init=False,
    )
    """Unified per-selector product preferences:
    ``{regex_selector: {capability: [ordered backends] | option_table: {key: val}}}``.
    The factory matches each host's ``id`` against the selectors
    (definition-order cascade) and partitions the result into capability
    selections (forwarded to the resolver) and option-value defaults (applied
    per-key, product-wins)."""

    os_profiles: dict[str, "OsProfile"] = field(
        default_factory=dict,
        init=False,
    )
    """Named OS profiles declared by this repo's ``[os_profiles]`` settings,
    keyed by profile name. Each is also registered into the global os-profile
    registry at parse time so lab-data entries can select it by name in the
    ``os_type`` field. See :func:`otto.host.os_profile.register_os_profile`."""

    logging_levels: dict[str, str] = field(default_factory=dict[str, str], init=False)
    """This repo's ``[logging.levels]`` table: logger name → the minimum level
    that ENTERS otto's funnel, overriding or extending
    :data:`otto.logger.management.DEFAULT_LIBRARY_LEVELS`. There is no capture
    list — otto configures the root logger, so every logger is captured; this
    only quiets or un-quiets. Merged across repos by ``otto.cli.invoke``."""

    settings: dict[str, Any] = field(default_factory=dict[str, Any])
    """Repo settings dict as parsed from the `settings.toml` file"""

    docker_settings: DockerSettings = field(
        default_factory=DockerSettings,
        init=False,
    )
    """Parsed `[docker]` table — image build definitions, compose files, and
    registry URL. Defaults to an empty :class:`DockerSettings` when the
    section is absent."""

    monitor_settings: MonitorSettings = field(
        default_factory=MonitorSettings,
        init=False,
    )
    """Parsed `[monitor]` table — optional TLS cert/key for the dashboard server."""

    env_backend: "str | None" = field(default=None, init=False)
    """Parsed `[env] backend` — this repo's standing choice of installer for the
    orchestration venv, or None to auto-detect. Read through the validated model
    rather than the raw settings dict, so ``EnvSettingsSpec`` stays the one place
    the key is spelled."""

    def __post_init__(self) -> None:
        # ``anchor_path`` needs an absolute root to anchor relative settings
        # paths against, so ``sut_dir`` is made absolute here, before
        # ``parse_settings`` runs. Deliberately ``.absolute()``, not
        # ``.resolve()``: this must not collapse symlinks or ``..`` (see
        # ``anchor_to_repo``).
        self.sut_dir = self.sut_dir.absolute()
        self.parse_settings()

    def get_lab_panel(self) -> "Panel":
        """Build a Rich panel listing all lab names available from this repo's host source."""
        from rich.panel import Panel
        from rich.text import Text

        from ..labs import LabRepositoryError, build_lab_sources

        if not self.lab_sources:
            lab_name_text = Text("no [[lab.sources]] declared", style="dim")
        else:
            try:
                repository = build_lab_sources([self])
                lab_names = repository.list_labs()
            except (ValueError, LabRepositoryError) as e:
                # Panel rendering must never crash on a misconfigured/unreachable
                # host source; surface the reason in-panel instead of a traceback.
                lab_name_text = Text(f"⚠ host source unavailable: {e}", style="red")
            else:
                lab_name_text = Text("\n".join(f"• {lab_name}" for lab_name in lab_names))

        return Panel(
            lab_name_text,
            title=Text(f"{self.name} {self.version}", style="bold not dim"),
            subtitle=self.dependencies_summary(),
            subtitle_align="left",
            border_style="dim",
            padding=(1, 5, 1, 1),
            expand=True,
        )

    def dependencies_summary(self) -> "Text | None":
        """One-line per-dependency status summary, or ``None`` when none are declared.

        Reads :attr:`dependencies` (populated by bootstrap's resolution pass),
        so statuses reflect the discovered set, not registration success.
        Rendered as a panel subtitle — dep-free repos keep their panels
        byte-identical.
        """
        from rich.text import Text

        if not self.dependencies:
            return None
        parts: list[Text] = []
        for dep in self.dependencies:
            if dep.status == "satisfied":
                parts.append(Text(f"✓ {dep.name} {dep.provider_version}", style="green"))
            elif not dep.required and dep.status == "missing":
                parts.append(Text(f"○ {dep.name} (absent)", style="dim"))
            else:
                detail = (
                    f"found {dep.provider_version}" if dep.status == "incompatible" else dep.status
                )
                symbol, style = ("✗", "red") if dep.required else ("⚠", "yellow")
                parts.append(Text(f"{symbol} {dep.name} ({detail})", style=style))
        return Text(" · ").join(parts)

    def get_instructions_panel(self) -> "Panel":
        """Build a Rich panel listing all instructions contributed by this repo.

        Instructions are attributed to this repo by matching each registered
        instruction's module against the module prefixes in :attr:`init`.
        """
        from rich.text import Text

        from ..instructions import INSTRUCTIONS  # lazy import — keeps repo import-light

        instruction_names: list[str] = [
            entry.name
            for _, entry in INSTRUCTIONS.items()
            if any(entry.module == m or entry.module.startswith(m + ".") for m in self.init)
        ]

        lines = [f"• {n}" for n in instruction_names]
        content = Text("\n".join(lines)) if lines else Text("no instructions found", style="dim")
        return self._make_test_panel(f"{self.name} {self.version}", content)

    def _make_test_panel(self, title: str, content: "Text") -> "Panel":
        from rich.panel import Panel
        from rich.text import Text

        return Panel(
            content,
            title=Text(title, style="bold not dim"),
            subtitle=self.dependencies_summary(),
            subtitle_align="left",
            border_style="dim",
            padding=(1, 5, 1, 1),
            expand=True,
        )

    def get_markers_panel(self, markers: list[str]) -> "Panel":
        """Rich panel listing *markers*, the markers pytest knows in this repo's tests."""
        from rich.text import Text

        lines = [f"• {m}" for m in markers]
        content = Text("\n".join(lines)) if lines else Text("(no markers found)", style="dim")
        return self._make_test_panel(f"{self.name} {self.version}", content)

    def get_otto_settings_path(
        self,
    ) -> Path:
        """
        Create the path to the `otto` settings TOML file.

        Returns
        -------
        Path to the `otto` settings TOML file.

        Raises
        ------
        FileNotFoundError
            If the TOML file is not found.
        """
        otto_settings_path = self.sut_dir / TOML_SETTINGS_PATH
        if not otto_settings_path.exists():
            raise FileNotFoundError(
                f"The SUT repo {self.sut_dir} does not have the required TOML file, {TOML_SETTINGS_PATH}"  # noqa: E501 — long error message f-string
            ) from None

        return otto_settings_path

    def read_settings(
        self,
    ) -> str:
        """Read and return the raw text of this repo's ``.otto/settings.toml`` file."""
        otto_settings_path = self.get_otto_settings_path()

        with otto_settings_path.open(encoding="utf-8") as otto_settings_file:
            return otto_settings_file.read()

    def parse_settings(self) -> None:
        """Parse + validate the repo's ``.otto/settings.toml`` via :func:`compile_settings`."""
        settings_text = self.read_settings()
        self.settings = tomli.loads(settings_text)  # raw — coverage/reservation read it

        compiled = compile_settings(self.settings, self.sut_dir)

        self.name = compiled.name
        self.version = compiled.version
        self.lab_sources = compiled.lab_sources
        self.project_scope = compiled.project_scope
        self.libs = compiled.libs
        self.tests = compiled.tests
        self.init = compiled.init
        self.declared_dependencies = compiled.declared_dependencies
        self.host_preferences = compiled.host_preferences
        self.logging_levels = compiled.logging_levels
        self.os_profiles = self._register_os_profiles(compiled.os_profiles)
        self.docker_settings = compiled.docker_settings
        self.monitor_settings = compiled.monitor_settings
        self.env_backend = compiled.env_backend
        self.declared_products = compiled.declared_products
        self.declared_dev_tools = compiled.declared_dev_tools

    def _register_os_profiles(
        self,
        profiles: dict[str, "OsProfileSpec"],
    ) -> dict[str, "OsProfile"]:
        """Register each validated os-profile into the global registry; return built profiles.

        Runs at settings-parse time,
        before init modules import, so a code registration can override a data
        table of the same name (last writer wins).
        """
        from ..host.os_profile import build_os_profile, register_os_profile

        result: dict[str, OsProfile] = {}
        for name, prof in profiles.items():
            register_os_profile(name, prof.base, prof.defaults)
            result[name] = build_os_profile(name)
        return result

    @property
    def reservation_settings(self) -> dict[str, Any]:
        """Return the raw ``[reservations]`` settings sub-dict.

        Returns an empty dict when the section is absent. Values are the
        literal parsed TOML — no path expansion or anchoring is applied.
        """
        return self.settings.get("reservations", {}) or {}

    @property
    def inventory_settings(self) -> dict[str, Any]:
        """Return the raw ``[inventory]`` sub-dict — the per-project override.

        Spec 2026-08-28 host-inventory §8. Empty when the section is absent.
        Literal parsed TOML, exactly like ``reservation_settings``: a plain
        dict, so this module never has to import :mod:`otto.inventory` (which
        would invert the layering). Anchoring and backend-kwarg validation
        happen in :func:`otto.inventory.config.compile_inventory`.
        """
        return self.settings.get("inventory", {}) or {}

    @property
    def creds_settings(self) -> dict[str, Any]:
        """Return the raw ``[creds]`` sub-dict — the per-project creds-store override.

        Spec 2026-09-06 creds-store §4.2. Empty when absent; literal parsed
        TOML like ``inventory_settings``, so this module never imports
        :mod:`otto.creds`. Anchoring and kwarg validation happen in
        :func:`otto.creds.config.compile_creds`.
        """
        return self.settings.get("creds", {}) or {}

    def add_libs_to_pythonpath(self) -> None:
        """Add configured library directories to the PYTHONPATH."""
        for lib in self.libs:
            sys.path.append(f"{lib}")

    def import_init_modules(self) -> None:
        """Import each module path listed in ``self.init``.

        Importing these modules triggers any registration side effects they
        perform at module level — e.g. registering custom hosts, products, or
        term/transfer backends, or defining ``@instruction`` commands.
        """
        for mod in self.init:
            importlib.import_module(mod)


def get_repos(
    repos: list[Path],
) -> list[Repo]:
    """Create `Repo` objects from the list of provided repo paths.

    Parameters
    ----------
    repos : List of paths to repos under test.

    Returns
    -------
        List of `Repo` objects

    Raises
    ------
    FileNotFoundError
        If a repo's settings TOML file is not found.
    """
    return [Repo(sut_dir=repo) for repo in repos]
