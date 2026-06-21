import asyncio
import contextlib
import importlib
import io
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
from ..utils import (
    CommandStatus,
    Status,
)
from .version import Version

if TYPE_CHECKING:
    import pytest
    from rich.panel import Panel
    from rich.text import Text

    from ..host.os_profile import OsProfile
    from ..models.settings import OsProfileSpec

logger = get_otto_logger()

SETTINGS_FILENAME  = 'settings.toml'
TOML_SETTINGS_PATH = Path('.otto') / SETTINGS_FILENAME


def _test_run_syntax(t: 'CollectedTest', sut_dir: Path) -> str:
    """Build the ``otto test`` path argument for a single collected test.

    Uses a path relative to ``sut_dir`` so panels show short, copy-pasteable
    paths. ``otto test`` transparently resolves these back to absolute paths
    before passing them to pytest.
    """
    rel_path = t.path.relative_to(sut_dir.resolve())
    if t.cls_name:
        return f'{rel_path}::{t.cls_name}::{t.name}'
    return f'{rel_path}::{t.name}'


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

    nodeid:   str
    name:     str
    path:     Path
    cls_name: str | None


@dataclass
class Repo():

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

    os_profiles: dict[str, 'OsProfile'] = field(
        default_factory=dict,
        init=False,
    )
    """Named OS profiles declared by this repo's ``[os_profiles]`` settings,
    keyed by profile name. Each is also registered into the global os-profile
    registry at parse time so lab-data entries can select it by name in the
    ``os_type`` field. See :func:`otto.host.os_profile.register_os_profile`."""

    settings: dict[str, Any] = field(default_factory=dict[str, Any])
    """Repo settings dict as parsed from the `settings.toml` file"""

    docker_settings: DockerSettings = field(
        default_factory=DockerSettings,
        init=False,
    )
    """Parsed `[docker]` table — image build definitions, compose files, and
    registry URL. Defaults to an empty :class:`DockerSettings` when the
    section is absent."""

    def __post_init__(self):
        self.parse_settings()

    def get_lab_panel(self) -> 'Panel':
        from rich.panel import Panel
        from rich.text import Text

        from ..storage import JsonFileLabRepository

        lab_search_paths: list[Path] = []
        lab_search_paths.extend(self.labs)

        lab_names = JsonFileLabRepository().list_labs(search_paths=lab_search_paths)

        lab_names = [ f"• {lab_name}" for lab_name in lab_names ]
        lab_name_text = Text('\n'.join(lab_names))

        panel = Panel(
            lab_name_text,
            title=Text(f'{self.name} {self.version}', style="bold not dim"),
            border_style="dim",
            padding=(1,5,1,1),
            expand=True,
        )
        return panel

    def get_instructions_panel(self) -> 'Panel':
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
                if any(module == m or module.startswith(m + '.') for m in self.init):
                    name = cmd.name or getattr(cmd.callback, '__name__', '').replace('_', '-')
                    instruction_names.append(name)

        lines = [f'• {n}' for n in instruction_names]
        content = Text('\n'.join(lines)) if lines else Text('no instructions found', style='dim')
        return self._make_test_panel(f'{self.name} {self.version}', content)

    def collect_tests(self) -> list[CollectedTest]:
        """Collect all tests from this repo's configured test directories.

        Performs a single pytest collection pass (no tests are executed).
        The returned list can be passed to any of the ``get*Panel`` methods
        so that multiple listing options share one collection run.

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
            loops_before = {o for o in gc.get_objects()
                            if isinstance(o, asyncio.AbstractEventLoop)
                            and not o.is_closed()}
            try:
                with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                    pytest.main(
                        paths + ['--collect-only', '-p', 'no:terminal', '-p', 'no:cov',
                                 '--override-ini', 'addopts=',
                                 '-o', 'asyncio_default_fixture_loop_scope=function'],
                        plugins=[collector],
                    )
            finally:
                sys.modules.clear()
                sys.modules.update(saved_modules)
                for leaked in [o for o in gc.get_objects()
                               if isinstance(o, asyncio.AbstractEventLoop)
                               and not o.is_closed()
                               and o not in loops_before]:
                    leaked.close()

        tests: list[CollectedTest] = []
        for item in collector.items:
            item_cls = getattr(item, 'cls', None)
            cls_name = item_cls.__name__ if item_cls is not None else None
            tests.append(CollectedTest(
                nodeid=item.nodeid,
                name=item.name,
                path=item.path,
                cls_name=cls_name,
            ))
        return tests

    def _make_test_panel(self, title: str, content: 'Text') -> 'Panel':
        from rich.panel import Panel
        from rich.text import Text

        return Panel(
            content,
            title=Text(title, style='bold not dim'),
            border_style='dim',
            padding=(1, 5, 1, 1),
            expand=True,
        )

    def get_tests_panel(self, items: list[CollectedTest]) -> 'Panel':
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

        lines = [f'• {_test_run_syntax(t, self.sut_dir)}' for t in items]
        content = Text('\n'.join(lines)) if lines else Text('(no tests found)', style='dim')
        return self._make_test_panel(f'{self.name} {self.version}', content)

    def get_test_files_panel(self, items: list[CollectedTest]) -> 'Panel':
        """Rich panel listing unique test files with their run syntax.

        Each line shows ``otto test <absolute-path>`` which runs all tests
        in that file.

        Parameters
        ----------
        items :
            Pre-collected tests from :meth:`collect_tests`.
        """
        from rich.text import Text

        seen: dict[Path, None] = {}
        sut_dir = self.sut_dir.resolve()
        for t in items:
            seen.setdefault(t.path.relative_to(sut_dir), None)
        lines = [f'• {p}' for p in seen]
        content = Text('\n'.join(lines)) if lines else Text('(no tests found)', style='dim')
        return self._make_test_panel(f'{self.name} {self.version}', content)

    def get_test_suites_panel(self, items: list[CollectedTest]) -> 'Panel':
        """Rich panel listing unique test suites with their run syntax.

        Only class-based tests are listed, using just ``ClassName`` — the
        subcommand name passed directly to ``otto test ClassName``.
        Bare functions (not part of a class) are omitted since they have no
        corresponding ``otto test`` subcommand.
        Entries are de-duplicated and preserve collection order.

        Parameters
        ----------
        items :
            Pre-collected tests from :meth:`collect_tests`.
        """
        from rich.text import Text

        seen: dict[str, None] = {}
        for t in items:
            if t.cls_name:
                seen.setdefault(t.cls_name, None)
        lines = [f'• {k}' for k in seen]
        content = Text('\n'.join(lines)) if lines else Text('(no tests found)', style='dim')
        return self._make_test_panel(f'{self.name} {self.version}', content)

    def get_otto_settings_path(self,
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

        ottoSettingsPath = self.sut_dir / TOML_SETTINGS_PATH
        if not ottoSettingsPath.exists():
            raise FileNotFoundError(
                f"The SUT repo {self.sut_dir} does not have the required TOML file, {TOML_SETTINGS_PATH}"
            ) from None

        return ottoSettingsPath


    def read_settings(self,
    ) -> str:

        ottoSettingsPath = self.get_otto_settings_path()

        with open(ottoSettingsPath) as ottoSettingsFile:
            settingsText = ottoSettingsFile.read()

        return settingsText

    def parse_settings(self) -> None:
        """Parse + validate the repo's ``.otto/settings.toml`` via SettingsModel."""
        # ``otto.models``'s package __init__ boots otto.host first to avoid an
        # import cycle (os_profile's eager registration ↔ models.host); see the
        # note in src/otto/models/__init__.py. So this import is safe here.
        from ..models.settings import SettingsModel

        settingsText = self.read_settings()
        self.settings = tomli.loads(settingsText)  # raw — coverage/reservation read it

        expanded = self._expand_recursive(self.settings)
        model = SettingsModel.model_validate(expanded)

        self.name = model.name
        self.version = Version(model.version)
        self.labs = list(model.labs)
        # valid_labs are lab *names*, not paths — populate from the raw dict so
        # they are NOT ${sut_dir}-expanded (the model still validates them as a
        # list[str]). Preserves the pre-pydantic behavior.
        self.valid_labs = list(self.settings.get('valid_labs', []))
        self.libs = list(model.libs)
        self.tests = list(model.tests)
        self.init = list(model.init)
        self.host_preferences = {
            sel: {
                k: (list(v) if isinstance(v, list) else dict(v))
                for k, v in entries.items()
            }
            for sel, entries in model.host_preferences.items()
        }
        self.os_profiles = self._register_os_profiles(model.os_profiles)
        self.docker_settings = model.docker.to_runtime()

    def _register_os_profiles(
        self, profiles: dict[str, 'OsProfileSpec'],
    ) -> dict[str, 'OsProfile']:
        """Register each validated os-profile into the global registry and
        return the built profiles, keyed by name. Runs at settings-parse time,
        before init modules import, so a code registration can override a data
        table of the same name (last writer wins).
        """
        from ..host.os_profile import build_os_profile, register_os_profile

        result: dict[str, OsProfile] = {}
        for name, prof in profiles.items():
            try:
                register_os_profile(name, prof.base, prof.defaults)
            except ValueError as e:
                raise ValueError(
                    f"{TOML_SETTINGS_PATH}: [os_profiles.{name}]: {e}"
                ) from e
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
        raw = self.settings.get('reservations', {}) or {}
        return self._expand_recursive(raw)

    def _expand_recursive(self,
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
        """Add configured library directories to the PYTHONPATH"""

        for lib in self.libs:
            sys.path.append(f'{lib}')

    def import_init_modules(self) -> None:

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
            for test_file in sorted(test_dir.glob('test_*.py')):
                mod_name = f'_otto_suite_{test_file.stem}'
                if mod_name in sys.modules:
                    continue
                spec = importlib.util.spec_from_file_location(mod_name, test_file)
                if spec is None or spec.loader is None:
                    continue
                mod = importlib.util.module_from_spec(spec)
                sys.modules[mod_name] = mod
                spec.loader.exec_module(mod)  # type: ignore[union-attr]

    def _expand_string(self,
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

        field = field.replace('${sut_dir}', f'{self.sut_dir}')

        return field

    def apply_settings(self):

        self.add_libs_to_pythonpath()
        self.import_init_modules()
        self.import_test_files()

    async def set_git_description(self):

        commandStatus = await self.run_git_command('describe')
        if commandStatus.status == Status.Success:
            self._git_description = f'({commandStatus.output.strip()})'

        # `git describe` can fail if no names or tags exist for the repo.
        # In this case, which is expected and can happen, set the description
        # to an empty string
        else:
            self._git_description = ''


    async def set_commit_hash(self):

        commandStatus = await self.run_git_command('log -1 --format=%H')
        self._git_hash = commandStatus.output

    @property
    def commit(self):
        if self._git_hash is not None:
            return self._git_hash

        asyncio.run(self.set_commit_hash())
        return self._git_hash

    @property
    def description(self):
        if self._git_description is not None:
            return self._git_description

        asyncio.run(self.set_git_description())
        return self._git_description

    @property
    def commit_name(self) -> str:
        from ..host.host import SuppressCommandOutput

        with SuppressCommandOutput():
            return f'{self.commit} ({self.description})'

    async def run_git_command(self,
        cmd: str,
    ) -> CommandStatus:
        from ..host.local_host import LocalHost

        host = LocalHost(log=False)
        try:
            return (await host.run(f'git -C {self.sut_dir} {cmd}')).only
        finally:
            await host.close()


def apply_repo_settings(
    repos: list[Repo],
) -> None:

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

    return [ Repo(sut_dir=repo) for repo in repos ]
