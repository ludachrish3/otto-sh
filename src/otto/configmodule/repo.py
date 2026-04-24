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

from ..logger import getOttoLogger
from ..utils import (
    CommandStatus,
    Status,
)
from .version import Version

if TYPE_CHECKING:
    import pytest
    from rich.panel import Panel
    from rich.text import Text

logger = getOttoLogger()

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

    sutDir: Path
    """SUT directory from which the settings came."""

    _gitHash: str | None = field(default=None, init=False, repr=False)
    """HEAD git hash of repo. None if `sutDir` is not a git repo."""

    _gitDescription: str | None = field(default=None, init=False, repr=False)
    """HEAD git hash of repo. None if `sutDir` is not a git repo."""

    name: str = field(init=False)
    """Product/repo name"""

    version: Version = field(init=False)
    """Product version"""

    labs: list[Path] = field(default_factory=list[Path], init=False)
    """Paths to lab data"""

    libs: list[Path] = field(default_factory=list[Path], init=False)
    """Extra paths to add to the PYTHONPATH"""

    init: list[str] = field(default_factory=list[str], init=False)
    """Module paths that need to be imported during `otto` init.

    Modules containing instructions are an example of modules that need to be imported eagerly.
    """

    tests: list[Path] = field(default_factory=list[Path], init=False)
    """Directories that contain test suites."""

    settings: dict[str, Any] = field(default_factory=dict[str, Any])
    """Repo settings dict as parsed from the `settings.toml` file"""

    def __post_init__(self):
        self.parseSettings()

    def getLabPanel(self) -> 'Panel':
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

    def getInstructionsPanel(self) -> 'Panel':
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
        return self._makeTestPanel(f'{self.name} {self.version}', content)

    def collectTests(self) -> list[CollectedTest]:
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
            saved_modules = sys.modules.copy()
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

    def _makeTestPanel(self, title: str, content: 'Text') -> 'Panel':
        from rich.panel import Panel
        from rich.text import Text

        return Panel(
            content,
            title=Text(title, style='bold not dim'),
            border_style='dim',
            padding=(1, 5, 1, 1),
            expand=True,
        )

    def getTestsPanel(self, items: list[CollectedTest]) -> 'Panel':
        """Rich panel listing every individual test with its full run syntax.

        Each line shows ``otto test <absolute-path>::[Class::]test_fn`` which
        can be copy-pasted directly to run that specific test regardless of
        the current working directory.

        Parameters
        ----------
        items :
            Pre-collected tests from :meth:`collectTests`.
        """
        from rich.text import Text

        lines = [f'• {_test_run_syntax(t, self.sutDir)}' for t in items]
        content = Text('\n'.join(lines)) if lines else Text('(no tests found)', style='dim')
        return self._makeTestPanel(f'{self.name} {self.version}', content)

    def getTestFilesPanel(self, items: list[CollectedTest]) -> 'Panel':
        """Rich panel listing unique test files with their run syntax.

        Each line shows ``otto test <absolute-path>`` which runs all tests
        in that file.

        Parameters
        ----------
        items :
            Pre-collected tests from :meth:`collectTests`.
        """
        from rich.text import Text

        seen: dict[Path, None] = {}
        sut_dir = self.sutDir.resolve()
        for t in items:
            seen.setdefault(t.path.relative_to(sut_dir), None)
        lines = [f'• {p}' for p in seen]
        content = Text('\n'.join(lines)) if lines else Text('(no tests found)', style='dim')
        return self._makeTestPanel(f'{self.name} {self.version}', content)

    def getTestSuitesPanel(self, items: list[CollectedTest]) -> 'Panel':
        """Rich panel listing unique test suites with their run syntax.

        Only class-based tests are listed, using just ``ClassName`` — the
        subcommand name passed directly to ``otto test ClassName``.
        Bare functions (not part of a class) are omitted since they have no
        corresponding ``otto test`` subcommand.
        Entries are de-duplicated and preserve collection order.

        Parameters
        ----------
        items :
            Pre-collected tests from :meth:`collectTests`.
        """
        from rich.text import Text

        seen: dict[str, None] = {}
        for t in items:
            if t.cls_name:
                seen.setdefault(t.cls_name, None)
        lines = [f'• {k}' for k in seen]
        content = Text('\n'.join(lines)) if lines else Text('(no tests found)', style='dim')
        return self._makeTestPanel(f'{self.name} {self.version}', content)

    def getOttoSettingsPath(self,
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

        ottoSettingsPath = self.sutDir / TOML_SETTINGS_PATH
        if not ottoSettingsPath.exists():
            raise FileNotFoundError(
                f"The SUT repo {self.sutDir} does not have the required TOML file, {TOML_SETTINGS_PATH}"
            ) from None

        return ottoSettingsPath


    def readSettings(self,
    ) -> str:

        ottoSettingsPath = self.getOttoSettingsPath()

        with open(ottoSettingsPath) as ottoSettingsFile:
            settingsText = ottoSettingsFile.read()

        return settingsText

    def parseSettings(self) -> None:
        """Parse the settings TOML file in the repo's `.otto` directory."""

        settingsText = self.readSettings()
        self.settings = tomli.loads(settingsText)

        self.labs  = [ Path(self._expandString(lab)) for lab in self.settings.get('labs',  []) ]
        self.libs  = [ Path(self._expandString(lib)) for lib in self.settings.get('libs',  []) ]
        self.tests = [ Path(self._expandString(dir)) for dir in self.settings.get('tests', []) ]
        self.init  = [      self._expandString(mod)  for mod in self.settings.get('init',  []) ]

        try:
            self.name = self._expandString(self.settings['name'])
            self.version = Version(self._expandString(self.settings['version']))
        except KeyError as e:
            errorStr = f"{TOML_SETTINGS_PATH} does not specify a required field: {e}"
            logger.critical(errorStr)
            raise KeyError(errorStr) from e

    @property
    def reservationSettings(self) -> dict[str, Any]:
        """Return the ``[reservations]`` settings sub-dict with ${sutDir} expanded.

        Returns an empty dict when the section is absent. Every string value
        (including nested tables) has ``${sutDir}`` substituted so the
        reservation backend can use the same path-expansion convention as
        the other repo settings.
        """
        raw = self.settings.get('reservations', {}) or {}
        return self._expandRecursive(raw)

    def _expandRecursive(self,
        value: Any,
    ) -> Any:
        """Recursively walk a dict/list, expanding every string via :meth:`_expandString`."""
        if isinstance(value, str):
            return self._expandString(value)
        if isinstance(value, dict):
            return {k: self._expandRecursive(v) for k, v in value.items()}
        if isinstance(value, list):
            return [self._expandRecursive(v) for v in value]
        return value

    def addLibsToPythonpath(self) -> None:
        """Add configured library directories to the PYTHONPATH"""

        for lib in self.libs:
            sys.path.append(f'{lib}')

    def importInitModules(self) -> None:

        for mod in self.init:
            importlib.import_module(mod)

    def importTestFiles(self) -> None:
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

    def _expandString(self,
        field: str,
    ) -> str:
        """
        Expand a string value from the settings file with variable values.

        The special strings are:
        - `"${sutDir}"`: Replaced with the `Repo.sutDir` value.

        Parameters
        ----------
        field : Raw string from `otto` settings TOML file.

        Returns
        -------
        `str` object after all supported substitutions.
        """

        field = field.replace('${sutDir}', f'{self.sutDir}')

        return field

    def applySettings(self):

        self.addLibsToPythonpath()
        self.importInitModules()
        self.importTestFiles()

    async def setGitDescription(self):

        commandStatus = await self.runGitCommand('describe')
        if commandStatus.status == Status.Success:
            self._gitDescription = f'({commandStatus.output.strip()})'

        # `git describe` can fail if no names or tags exist for the repo.
        # In this case, which is expected and can happen, set the description
        # to an empty string
        else:
            self._gitDescription = ''


    async def setCommitHash(self):

        commandStatus = await self.runGitCommand('log -1 --format=%H')
        self._gitHash = commandStatus.output

    @property
    def commit(self):
        if self._gitHash is not None:
            return self._gitHash

        asyncio.run(self.setCommitHash())
        return self._gitHash

    @property
    def description(self):
        if self._gitDescription is not None:
            return self._gitDescription

        asyncio.run(self.setGitDescription())
        return self._gitDescription

    @property
    def commitName(self) -> str:
        from ..host.host import SuppressCommandOutput

        with SuppressCommandOutput():
            return f'{self.commit} ({self.description})'

    async def runGitCommand(self,
        cmd: str,
    ) -> CommandStatus:
        from ..host.localHost import LocalHost

        return (await LocalHost(log=False).run(f'git -C {self.sutDir} {cmd}')).only


def applyRepoSettings(
    repos: list[Repo],
) -> None:

    for repo in repos:
        repo.applySettings()


def getRepos(
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

    return [ Repo(sutDir=repo) for repo in repos ]
