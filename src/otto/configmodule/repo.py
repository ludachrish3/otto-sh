"""Repo settings loading, parsing, and test-collection helpers for SUT repositories."""

import asyncio
import contextlib
import importlib
import os
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

from ..logger import get_otto_logger
from ..result import CommandResult
from ..utils import Status
from .version import Version

if TYPE_CHECKING:
    from rich.panel import Panel
    from rich.text import Text

    from ..host.os_profile import OsProfile
    from ..models.settings import OsProfileSpec

logger = get_otto_logger()

SETTINGS_FILENAME = "settings.toml"
TOML_SETTINGS_PATH = Path(".otto") / SETTINGS_FILENAME


def _test_run_syntax(t: "CollectedTest", sut_dir: Path) -> str:
    """Build the ``otto test`` path argument for a single collected test.

    Uses a path relative to ``sut_dir`` so panels show short, copy-pasteable
    paths. ``otto test`` transparently resolves these back to absolute paths
    before passing them to pytest.
    """
    rel_path = t.path.relative_to(sut_dir.resolve())
    if t.cls_name:
        return f"{rel_path}::{t.cls_name}::{t.name}"
    return f"{rel_path}::{t.name}"


@dataclass(frozen=True)
class DockerImage:
    """A Dockerfile-built image declared by a project."""

    name: str
    """Short logical name used in tags and CLI selection."""

    dockerfile: Path
    """Absolute path to the Dockerfile."""

    context: Path
    """Absolute path to the build context directory."""

    target: str | None = None
    """Optional multi-stage build target."""

    build_args: tuple[tuple[str, str], ...] = ()
    """Frozen list of (name, value) build args. Tuples (not dicts) so the
    container is hashable and order is preserved for context-hash inputs."""


@dataclass(frozen=True)
class DockerCompose:
    """A docker-compose file contributed by a project."""

    path: Path
    """Absolute path to the compose YAML file."""

    default_host: str | None = None
    """Lab host id where this stack should run by default. Overridden by
    ``otto docker up --on <host>``."""

    services: tuple[str, ...] = ()
    """Service names declared in the compose file. Used to synthesize
    container host ids for tab-completion without parsing YAML at
    completion-fast-path time. The runtime is the source of truth and
    will warn on mismatch with ``docker compose config --services``."""


@dataclass(frozen=True)
class DockerSettings:
    """Per-repo docker configuration parsed from `[docker]` in `settings.toml`."""

    registry_url: str = "docker.io"
    """Default registry. Overridable per-image via the image's tag prefix."""

    images: tuple[DockerImage, ...] = ()
    """Images this project knows how to build."""

    composes: tuple[DockerCompose, ...] = ()
    """Compose files this project contributes."""


@dataclass(frozen=True)
class CollectedTest:
    """A single test item collected from a SUT repo's test directories.

    Attributes
    ----------
    nodeid :
        Full pytest node ID, e.g. ``dir/test_x.py::ClassName::test_fn``.
        Suitable for use directly as the ``SUITE`` argument to ``otto test``.
    name :
        Test function name only, e.g. ``test_fn``.
    path :
        Absolute path to the test file.
    cls_name :
        Class name if the test belongs to a class, else ``None``.
    """

    nodeid: str
    name: str
    path: Path
    cls_name: str | None


@dataclass
class Repo:
    """Runtime representation of a single SUT (system-under-test) repository.

    Parsed from ``.otto/settings.toml`` at construction time (via
    ``__post_init__``).  Holds the resolved paths for lab data, test
    directories, and init modules, plus Docker and OS-profile settings
    contributed by that repo.  Multiple ``Repo`` instances are managed by
    the configmodule package when ``OTTO_SUT_DIRS`` lists more than one
    directory.
    """

    sut_dir: Path
    """SUT directory from which the settings came."""

    _git_hash: str | None = field(default=None, init=False, repr=False)
    """HEAD git hash of repo. None if `sut_dir` is not a git repo."""

    _git_description: str | None = field(default=None, init=False, repr=False)
    """HEAD git hash of repo. None if `sut_dir` is not a git repo."""

    name: str = field(init=False)
    """Product/repo name"""

    version: Version = field(init=False)
    """Product version"""

    labs: list[Path] = field(default_factory=list[Path], init=False)
    """Paths to lab data"""

    valid_labs: list[str] = field(default_factory=list[str], init=False)
    """Lab names this repo supports (by ``labs`` membership), e.g. an embedded
    product that only runs in an embedded lab. Empty when the key is unset.

    Parsed here; *enforcement* — rejecting a selected ``--lab`` that is not in
    this list, and treating an empty list as "the repo must declare its labs"
    rather than allow-all — is intentionally deferred to lab-selection time and
    not yet wired in. Parsing must not silently treat unset as allow-all."""

    libs: list[Path] = field(default_factory=list[Path], init=False)
    """Extra paths to add to the PYTHONPATH"""

    init: list[str] = field(default_factory=list[str], init=False)
    """Module paths that need to be imported during `otto` init.

    Modules containing instructions are an example of modules that need to be imported eagerly.
    """

    tests: list[Path] = field(default_factory=list[Path], init=False)
    """Directories that contain test suites."""

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

    logging_capture: list[str] = field(default_factory=list[str], init=False)
    """Explicit top-level logger prefixes from ``[logging] capture`` whose
    ``logging.getLogger(__name__)`` records otto should route into its sinks,
    in addition to the package prefixes auto-derived from ``init``/``libs``.
    See :meth:`product_log_prefixes`."""

    settings: dict[str, Any] = field(default_factory=dict[str, Any])
    """Repo settings dict as parsed from the `settings.toml` file"""

    docker_settings: DockerSettings = field(
        default_factory=DockerSettings,
        init=False,
    )
    """Parsed `[docker]` table — image build definitions, compose files, and
    registry URL. Defaults to an empty :class:`DockerSettings` when the
    section is absent."""

    def __post_init__(self) -> None:
        self.parse_settings()

    def get_lab_panel(self) -> "Panel":
        """Build a Rich panel listing all lab names available from this repo's host source."""
        from rich.panel import Panel
        from rich.text import Text

        from ..storage import LabRepositoryError, build_lab_repository

        try:
            repository = build_lab_repository(
                self.lab_settings, self.sut_dir, search_paths=self.labs
            )
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
            border_style="dim",
            padding=(1, 5, 1, 1),
            expand=True,
        )

    def get_instructions_panel(self) -> "Panel":
        """Build a Rich panel listing all instructions contributed by this repo.

        Instructions are attributed to this repo by matching each registered
        instruction's module against the module prefixes in :attr:`init`.
        """
        from rich.text import Text

        from ..cli.run import run_app  # lazy import — avoids circular dependency

        instruction_names: list[str] = []
        for group in run_app.registered_groups:
            if group.typer_instance is None:
                continue
            for cmd in group.typer_instance.registered_commands:
                if cmd.callback is None:
                    continue
                module: str = cmd.callback.__module__
                if any(module == m or module.startswith(m + ".") for m in self.init):
                    name = cmd.name or getattr(cmd.callback, "__name__", "").replace("_", "-")
                    instruction_names.append(name)

        lines = [f"• {n}" for n in instruction_names]
        content = Text("\n".join(lines)) if lines else Text("no instructions found", style="dim")
        return self._make_test_panel(f"{self.name} {self.version}", content)

    def collect_tests(
        self,
        markers: str | None = None,
        suite: str | None = None,
        tests: str | None = None,
    ) -> list[CollectedTest]:
        """Collect all tests from this repo's configured test directories.

        Performs a single pytest collection pass (no tests are executed).
        The returned list can be passed to any of the ``get*Panel`` methods
        so that multiple listing options share one collection run.

        Parameters
        ----------
        markers :
            Passed as ``-m <markers>`` to the inner pytest run, narrowing
            collection to tests matching the marker expression.
        suite :
            Restrict collection to the registered suite of this name (its
            source file is looked up in ``_SUITE_FILES``).  Also passes
            ``-k <suite>`` so only the matching class is selected within
            that file.
        tests :
            Passed as ``-k <tests>`` to the inner pytest run, narrowing
            collection to tests whose name matches the keyword expression.

        Returns
        -------
        list[CollectedTest]
            One entry per discovered test item, in collection order.
        """
        import pytest

        class _Collector:
            def __init__(self) -> None:
                self.items: list[pytest.Item] = []

            def pytest_collection_finish(self, session: pytest.Session) -> None:
                self.items = list(session.items)

        collector = _Collector()
        paths = [str(d) for d in self.tests if d.exists()]
        if paths:
            import gc

            saved_modules = sys.modules.copy()
            # pytest-asyncio installs a session-scoped event loop on first
            # async test collection. The inner pytest.main() session leaves
            # that loop open (held by plugin reference cycles); without
            # explicit cleanup its self-pipe socketpair lingers and surfaces
            # later as PytestUnraisableExceptionWarning when an outer
            # gc.collect() breaks the cycle. Same pattern as the fix in
            # tests/unit/suite/test_plugin.py.
            loops_before = {
                o
                for o in gc.get_objects()
                if isinstance(o, asyncio.AbstractEventLoop) and not o.is_closed()
            }
            try:
                selector_args: list[str] = []
                if markers:
                    selector_args += ["-m", markers]
                if tests:
                    selector_args += ["-k", tests]
                if suite:
                    from ..suite.register import _SUITE_FILES

                    suite_file = _SUITE_FILES.get(suite)
                    if suite_file is not None:
                        paths = [suite_file]
                    else:
                        logger.warning(
                            "suite %r not found in the registry; listing all tests in %s",
                            suite,
                            self.name,
                        )
                    # -k narrows to the class within that file
                    selector_args += ["-k", suite]

                with (
                    Path(os.devnull).open("w") as sink_out,
                    Path(os.devnull).open("w") as sink_err,
                    contextlib.redirect_stdout(sink_out),
                    contextlib.redirect_stderr(sink_err),
                ):
                    rc = pytest.main(
                        [
                            *paths,
                            "--collect-only",
                            "-p",
                            "no:terminal",
                            "-p",
                            "no:cov",
                            "--override-ini",
                            "addopts=",
                            "--override-ini",
                            "filterwarnings=",
                            "-o",
                            "asyncio_default_fixture_loop_scope=function",
                            *selector_args,
                        ],
                        plugins=[collector],
                    )
                # Surface a real collection failure instead of returning [] silently.
                if rc not in (0, 5):  # 0 = OK, 5 = no tests collected
                    logger.error(
                        "Test collection failed for repo %r (pytest exit %s); "
                        "see above. Listing may be incomplete.",
                        self.name,
                        rc,
                    )
            finally:
                sys.modules.clear()
                sys.modules.update(saved_modules)
                for leaked in [
                    o
                    for o in gc.get_objects()
                    if isinstance(o, asyncio.AbstractEventLoop)
                    and not o.is_closed()
                    and o not in loops_before
                ]:
                    leaked.close()

        collected: list[CollectedTest] = []
        for item in collector.items:
            item_cls = getattr(item, "cls", None)
            cls_name = item_cls.__name__ if item_cls is not None else None
            collected.append(
                CollectedTest(
                    nodeid=item.nodeid,
                    name=item.name,
                    path=item.path,
                    cls_name=cls_name,
                )
            )
        return collected

    def _make_test_panel(self, title: str, content: "Text") -> "Panel":
        from rich.panel import Panel
        from rich.text import Text

        return Panel(
            content,
            title=Text(title, style="bold not dim"),
            border_style="dim",
            padding=(1, 5, 1, 1),
            expand=True,
        )

    def get_tests_panel(self, items: list[CollectedTest]) -> "Panel":
        """Rich panel listing every individual test with its full run syntax.

        Each line shows ``otto test <absolute-path>::[Class::]test_fn`` which
        can be copy-pasted directly to run that specific test regardless of
        the current working directory.

        Parameters
        ----------
        items :
            Pre-collected tests from :meth:`collect_tests`.
        """
        from rich.text import Text

        lines = [f"• {_test_run_syntax(t, self.sut_dir)}" for t in items]
        content = Text("\n".join(lines)) if lines else Text("(no tests found)", style="dim")
        return self._make_test_panel(f"{self.name} {self.version}", content)

    def registered_suites(self) -> list[str]:
        """Names of ``@register_suite`` suites whose source file is under this repo.

        Reads ``otto.suite.register._SUITE_FILES`` (populated at suite import
        time) and returns the registered suite names — the exact subcommand
        names ``otto test <name>`` accepts — for suites defined under this
        repo's ``sut_dir``, preserving registration order.
        """
        from ..suite.register import _SUITE_FILES, _SUITE_REGISTRY

        sut_root = self.sut_dir.resolve()
        names: list[str] = []
        for name, _sub_app in _SUITE_REGISTRY:
            src = _SUITE_FILES.get(name)
            if src is None:
                continue
            try:
                Path(src).resolve().relative_to(sut_root)
            except ValueError:
                continue
            names.append(name)
        return names

    def get_test_suites_panel(self) -> "Panel":
        """Rich panel listing this repo's runnable suite names.

        Sourced from the suite registry (``registered_suites``) — the exact
        ``otto test <name>`` subcommands — not from a pytest collection.
        """
        from rich.text import Text

        names = self.registered_suites()
        lines = [f"• {n}" for n in names]
        content = Text("\n".join(lines)) if lines else Text("(no tests found)", style="dim")
        return self._make_test_panel(f"{self.name} {self.version}", content)

    def configured_markers(self) -> list[str]:
        """Marker names declared in this repo's pytest config (for ``--list-markers``).

        Reads ``pyproject.toml [tool.pytest.ini_options].markers``. Each entry
        is reduced to the token before ``:`` or ``(``. Static read — no
        collection.
        """
        pyproject = self.sut_dir / "pyproject.toml"
        if not pyproject.is_file():
            return []
        try:
            data = tomli.loads(pyproject.read_text())
        except (OSError, tomli.TOMLDecodeError):
            return []
        raw = data.get("tool", {}).get("pytest", {}).get("ini_options", {}).get("markers", [])
        out: list[str] = []
        for entry in raw:
            token = str(entry).split(":", 1)[0].split("(", 1)[0].strip()
            if token:
                out.append(token)
        return out

    def get_markers_panel(self) -> "Panel":
        """Rich panel listing this repo's configured pytest markers.

        Sourced statically from ``pyproject.toml [tool.pytest.ini_options].markers``
        via :meth:`configured_markers` — no inner pytest collection is performed.
        """
        from rich.text import Text

        markers = self.configured_markers()
        lines = [f"• {m}" for m in markers]
        content = Text("\n".join(lines)) if lines else Text("(no markers configured)", style="dim")
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

        with otto_settings_path.open() as otto_settings_file:
            return otto_settings_file.read()

    def parse_settings(self) -> None:
        """Parse + validate the repo's ``.otto/settings.toml`` via SettingsModel."""
        # ``otto.models``'s package __init__ boots otto.host first to avoid an
        # import cycle (os_profile's eager registration ↔ models.host); see the
        # note in src/otto/models/__init__.py. So this import is safe here.
        from ..models.settings import SettingsModel

        settings_text = self.read_settings()
        self.settings = tomli.loads(settings_text)  # raw — coverage/reservation read it

        expanded = self._expand_recursive(self.settings)
        model = SettingsModel.model_validate(expanded)

        self.name = model.name
        self.version = Version(model.version)
        self.labs = list(model.labs)
        # valid_labs are lab *names*, not paths — populate from the raw dict so
        # they are NOT ${sut_dir}-expanded (the model still validates them as a
        # list[str]). Preserves the pre-pydantic behavior.
        self.valid_labs = list(self.settings.get("valid_labs", []))
        self.libs = list(model.libs)
        self.tests = list(model.tests)
        self.init = list(model.init)
        self.host_preferences = {
            sel: {k: (list(v) if isinstance(v, list) else dict(v)) for k, v in entries.items()}
            for sel, entries in model.host_preferences.items()
        }
        self.logging_capture = list(model.logging.capture)
        self.os_profiles = self._register_os_profiles(model.os_profiles)
        self.docker_settings = model.docker.to_runtime()

    def product_log_prefixes(self) -> set[str]:
        """Top-level package names whose ``getLogger(__name__)`` records otto captures.

        The set is declared init module roots, immediate sub-packages of each
        ``libs`` dir, and explicit ``[logging] capture`` entries.
        """
        prefixes: set[str] = set(self.logging_capture)
        for mod in self.init:
            prefixes.add(mod.split(".", 1)[0])
        for lib in self.libs:
            if lib.is_dir():
                for child in lib.iterdir():
                    if (child / "__init__.py").exists():
                        prefixes.add(child.name)
        return prefixes

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
            try:
                register_os_profile(name, prof.base, prof.defaults)
            except ValueError as e:
                raise ValueError(f"{TOML_SETTINGS_PATH}: [os_profiles.{name}]: {e}") from e
            result[name] = build_os_profile(name)
        return result

    @property
    def reservation_settings(self) -> dict[str, Any]:
        """Return the ``[reservations]`` settings sub-dict with ${sut_dir} expanded.

        Returns an empty dict when the section is absent. Every string value
        (including nested tables) has ``${sut_dir}`` substituted so the
        reservation backend can use the same path-expansion convention as
        the other repo settings.
        """
        raw = self.settings.get("reservations", {}) or {}
        return self._expand_recursive(raw)

    @property
    def lab_settings(self) -> dict[str, Any]:
        """Return the ``[lab]`` settings sub-dict with ``${sut_dir}`` expanded.

        Returns an empty dict when the section is absent, so the host-source
        factory falls back to the built-in ``json`` backend over this repo's
        ``labs`` search paths.
        """
        raw = self.settings.get("lab", {}) or {}
        return self._expand_recursive(raw)

    def _expand_recursive(
        self,
        value: Any,
    ) -> Any:
        """Recursively walk a dict/list, expanding every string via :meth:`_expand_string`."""
        if isinstance(value, str):
            return self._expand_string(value)
        if isinstance(value, dict):
            return {k: self._expand_recursive(v) for k, v in value.items()}
        if isinstance(value, list):
            return [self._expand_recursive(v) for v in value]
        return value

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

    def import_test_files(self) -> None:
        """Import test_*.py files from each configured tests directory.

        This triggers ``@register_suite()`` decorators, which populate
        ``otto.suite.register._SUITE_REGISTRY`` at import time.  The registry
        is later consumed by ``cli/test.py`` to add sub-Typers to ``testing_app``.
        """
        import importlib.util

        for test_dir in self.tests:
            if not test_dir.is_dir():
                continue
            for test_file in sorted(test_dir.glob("test_*.py")):
                mod_name = f"_otto_suite_{test_file.stem}"
                if mod_name in sys.modules:
                    continue
                spec = importlib.util.spec_from_file_location(mod_name, test_file)
                if spec is None or spec.loader is None:
                    continue
                mod = importlib.util.module_from_spec(spec)
                sys.modules[mod_name] = mod
                spec.loader.exec_module(mod)  # type: ignore[union-attr]

    def _expand_string(
        self,
        field: str,
    ) -> str:
        """
        Expand a string value from the settings file with variable values.

        The special strings are:
        - `"${sut_dir}"`: Replaced with the `Repo.sut_dir` value.

        Parameters
        ----------
        field : Raw string from `otto` settings TOML file.

        Returns
        -------
        `str` object after all supported substitutions.
        """
        return field.replace("${sut_dir}", f"{self.sut_dir}")

    def apply_settings(self) -> None:
        """Apply all repo settings.

        Extends ``sys.path`` with configured lib directories, imports init modules,
        and imports test files to trigger suite registration.
        """
        self.add_libs_to_pythonpath()
        self.import_init_modules()
        self.import_test_files()

    async def set_git_description(self) -> None:
        """Populate ``_git_description`` from ``git describe`` output.

        Sets ``_git_description`` to the parenthesised tag description on
        success, or to an empty string when ``git describe`` fails (e.g. no
        tags exist in the repo).
        """
        result = await self.run_git_command("describe")
        if result.status == Status.Success:
            self._git_description = f"({result.value.strip()})"

        # `git describe` can fail if no names or tags exist for the repo.
        # In this case, which is expected and can happen, set the description
        # to an empty string
        else:
            self._git_description = ""

    async def set_commit_hash(self) -> None:
        """Populate ``_git_hash`` with the full SHA of the current HEAD commit."""
        result = await self.run_git_command("log -1 --format=%H")
        self._git_hash = result.value

    @property
    def commit(self) -> str | None:
        """Return the full HEAD commit SHA, fetching it on first access if needed."""
        if self._git_hash is not None:
            return self._git_hash

        asyncio.run(self.set_commit_hash())
        return self._git_hash

    @property
    def description(self) -> str | None:
        """Return the cached ``git describe`` string, fetching it on first access.

        The value is the parenthesised tag ``"(<tag>)"`` on success, or ``""``
        when no tags exist (``None`` before the first access).
        """
        if self._git_description is not None:
            return self._git_description

        asyncio.run(self.set_git_description())
        return self._git_description

    @property
    def commit_name(self) -> str:
        """Return a display string combining the commit SHA and the git description."""
        from ..host.host import SuppressCommandOutput

        with SuppressCommandOutput():
            return f"{self.commit} ({self.description})"

    async def run_git_command(
        self,
        cmd: str,
    ) -> CommandResult:
        """Run a git sub-command in this repo's ``sut_dir`` and return the result.

        Args:
            cmd: The git sub-command and its arguments (e.g. ``"log -1 --format=%H"``).

        Returns:
            A ``CommandResult`` containing the command's exit status and output.
        """
        from ..host.local_host import LocalHost
        from ..logger.mode import LogMode

        host = LocalHost(log=LogMode.QUIET)
        try:
            return (await host.run(f"git -C {self.sut_dir} {cmd}")).only
        finally:
            await host.close()


def apply_repo_settings(
    repos: list[Repo],
) -> None:
    """Call ``apply_settings()`` on each ``Repo`` in *repos* in order."""
    for repo in repos:
        repo.apply_settings()


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
