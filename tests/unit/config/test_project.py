import sys
import textwrap
import typing
from pathlib import Path

import pytest

from otto.config import DockerSettings, MonitorSettings
from otto.config.project import CompiledSettings, Project
from otto.host.element import Element
from tests._fixtures.mock_project import MockProject
from tests._fixtures.paths import TESTS_ROOT
from tests._fixtures.project_scaffold import make_project

mock_project: MockProject = None
tests_root = TESTS_ROOT


def _write_project(tmp_path: Path, settings_body: str) -> Path:
    """Materialize a minimal project at *tmp_path* with the given TOML body
    appended after the required ``name`` / ``version`` fields.
    """
    return make_project(tmp_path, name="tmp_project", extra=settings_body)


def _project_with_settings(tmp_path: Path, settings_body: str) -> "Project":
    """Materialize a minimal project (named ``p``) and return the parsed Project.

    *settings_body* is TOML appended after the generated ``name``/``version``.
    """
    return Project(root=make_project(tmp_path, name="p", extra=textwrap.dedent(settings_body)))


@pytest.fixture(autouse=False)
def default_mock_project():

    global mock_project  # noqa: PLW0603 — module-level singleton/cache

    mock_project = MockProject(tests_root / "project1")


def test_project_config_location(default_mock_project):

    project_settings_file = mock_project.root / ".otto" / "settings.toml"
    assert project_settings_file.exists()


def test_project_settings_tests_under_the_root(default_mock_project):

    assert mock_project.tests == [mock_project.root / "tests"]


def test_project_settings_init_modules(default_mock_project):

    assert mock_project.init == ["project1_instructions", "custom_hosts", "project1_monitor_uptime"]


def test_bootstrap_registers_project1_instructions_and_options(monkeypatch):
    """``bootstrap()`` is the granular replacement for the deleted
    ``Project.apply_settings()`` / ``apply_project_settings()``: per project it adds libs
    to ``sys.path`` and imports init modules — which register project1's
    instructions and its verb-wide options classes into the shared
    ``STANDALONE_INSTRUCTIONS``/``PROJECT_ACTIONS``/``OPTIONS`` registries
    (module-level, process-wide), which the ``INSTRUCTIONS`` view derives from.

    Isolation: Python's import cache couples this test to any earlier test in
    the same worker that imported project1's modules — the cached modules make
    bootstrap's imports no-ops, the registrations never re-run, and the delta
    assertions see "sets are equal" (deterministically reproducible by running
    this test twice in one process). So: park any project1-originated registry
    entries, evict the cached modules, and restore both afterwards.
    """
    from otto import bootstrap as bs
    from otto.instruction_catalog import INSTRUCTIONS, PROJECT_ACTIONS, STANDALONE_INSTRUCTIONS
    from otto.params import OPTIONS

    project1 = tests_root / "project1"
    pylib = str(project1 / "pylib")
    project1_options = [
        "project1_common.options:DeviceTestOptions",
        "project1_common.options:ProjectOptions",
    ]

    # Remove any prior entries so the precondition holds even if another
    # test (or a previous run in the same worker) already appended it.
    while pylib in sys.path:
        sys.path.remove(pylib)

    assert pylib not in sys.path

    def _park(registry) -> list:
        # The raw snapshot, so parking never resolves a lazy ``Ref``. Park by
        # ORIGIN and by NAME: an earlier test in this worker may have left one
        # of project1's option names registered under another origin, and an
        # entry that is already present makes the delta below come out empty.
        state = registry._snapshot()
        for name in registry.names():
            if (
                registry.origin(name).startswith("project1_instructions")
                or name in project1_options
            ):
                registry.unregister(name)
        return state

    registries = [STANDALONE_INSTRUCTIONS, PROJECT_ACTIONS, OPTIONS]
    parked = {registry: _park(registry) for registry in registries}
    evicted = {m: sys.modules.pop(m) for m in list(sys.modules) if m.startswith("project1_")}
    before_instructions = set(INSTRUCTIONS.names())
    before_options = set(OPTIONS.names())

    monkeypatch.setenv("OTTOPATH", str(project1))
    bs._reset()
    try:
        result = bs.bootstrap()
        assert result.errors == []

        assert pylib in sys.path
        assert set(INSTRUCTIONS.names()) > before_instructions
        assert sorted(set(OPTIONS.names()) - before_options) == project1_options
        assert [OPTIONS.origin(name) for name in project1_options] == ["project1_instructions"] * 2
    finally:
        bs._reset()
        # Restore the exact pre-test world: sys.path, this test's
        # registrations out, the parked entries and cached modules back in.
        while pylib in sys.path:
            sys.path.remove(pylib)
        for registry in registries:
            registry._restore(parked[registry])
        for mod in [m for m in sys.modules if m.startswith("project1_")]:
            sys.modules.pop(mod, None)
        sys.modules.update(evicted)


def test_logging_levels_parse_and_default_empty(tmp_path):
    """Spec §4.2: ``[logging.levels]`` lands on the Project; absent means empty."""
    project = _project_with_settings(
        tmp_path,
        """
        [logging.levels]
        asyncssh = "DEBUG"
        vendor = "ERROR"
        """,
    )
    assert project.logging_levels == {"asyncssh": "DEBUG", "vendor": "ERROR"}
    bare = _project_with_settings(tmp_path / "bare", "")
    assert bare.logging_levels == {}


def test_removed_capture_key_is_a_hard_cutover_error(tmp_path):
    """Spec §4.3: the removed key is REJECTED, and the message points at its successor.

    ``extra='forbid'`` alone would name ``capture`` without saying what to do
    instead, so the model carries an explicit before-validator.
    """
    with pytest.raises(ValueError, match=r"\[logging\.levels\]") as exc:
        _project_with_settings(
            tmp_path,
            """
            [logging]
            capture = ["myproduct"]
            """,
        )
    # Spec §4.3: the error must name the file that carried the key.
    assert str(tmp_path / ".otto" / "settings.toml") in str(exc.value)


def test_logging_levels_reject_otto_names_and_bad_levels(tmp_path):
    """Spec §4.2: otto's own verbosity is --log-level's job; levels must be levels."""
    with pytest.raises(ValueError, match="--log-level"):
        _project_with_settings(
            tmp_path,
            """
            [logging.levels]
            "otto.host" = "DEBUG"
            """,
        )
    with pytest.raises(ValueError, match="LOUD"):
        _project_with_settings(
            tmp_path / "bad_level",
            """
            [logging.levels]
            vendor = "LOUD"
            """,
        )


def test_logging_levels_accept_the_warn_crit_aliases(tmp_path):
    """Spec §4.2: otto's own short aliases are levels too, and values normalize."""
    project = _project_with_settings(
        tmp_path,
        """
        [logging.levels]
        vendor = "warn"
        other = "CRIT"
        """,
    )
    assert project.logging_levels == {"vendor": "WARN", "other": "CRIT"}


# TODO: Test various settings fields and the recording of arbitrary additional data


def test_project_parses_unified_host_preferences(tmp_path):
    project = _project_with_settings(
        tmp_path,
        """
        [host_preferences.".*"]
        term = ["telnet"]
        ssh_options = { connect_timeout = 5.0 }
    """,
    )
    assert project.host_preferences[".*"]["term"] == ["telnet"]
    assert project.host_preferences[".*"]["ssh_options"] == {"connect_timeout": 5.0}
    assert not hasattr(project, "host_defaults")


class TestHostPreferencesParsing:
    """Tests for unified ``[host_preferences]`` parsing in ``Project.parse_settings``."""

    def test_absent_section_yields_empty_dict(self, tmp_path):
        sut = _write_project(tmp_path, "")
        project = Project(root=sut)
        assert project.host_preferences == {}

    def test_selections_and_option_tables_parsed(self, tmp_path):
        sut = _write_project(
            tmp_path,
            textwrap.dedent("""
            [host_preferences.".*"]
            term = ["telnet"]

            [host_preferences.".*".ssh_options]
            port = 2222
            connect_timeout = 5.0
        """),
        )
        project = Project(root=sut)
        assert project.host_preferences[".*"]["term"] == ["telnet"]
        assert project.host_preferences[".*"]["ssh_options"] == {
            "port": 2222,
            "connect_timeout": 5.0,
        }

    def test_legacy_host_defaults_rejected(self, tmp_path):
        sut = _write_project(
            tmp_path,
            textwrap.dedent("""
            [host_defaults.ssh_options]
            port = 2222
        """),
        )
        with pytest.raises(ValueError, match=r"\[host_defaults\] was removed"):
            Project(root=sut)

    def test_unknown_preference_key_raises(self, tmp_path):
        sut = _write_project(
            tmp_path,
            textwrap.dedent("""
            [host_preferences.".*"]
            bogus_options = { x = 1 }
        """),
        )
        with pytest.raises(ValueError, match="unknown"):
            Project(root=sut)


class TestOsProfilesParsing:
    """Tests for ``[os_profiles]`` parsing in ``Project.parse_settings``."""

    def test_absent_section_yields_empty_dict(self, tmp_path):
        sut = _write_project(tmp_path, "")
        project = Project(root=sut)
        assert project.os_profiles == {}

    def test_profile_parsed_and_kept_as_data(self, tmp_path):
        from otto.host.os_profile import OS_PROFILES, ProfileContext, build_os_profile

        sut = _write_project(
            tmp_path,
            textwrap.dedent("""
            [os_profiles.zephyr-3_7]
            base = "embedded"
            os_name = "Zephyr"
            os_version = "3.7"
            command_frame = "zephyr"
            filesystem = "fat-ram"
            max_filename_len = 32
        """),
        )
        project = Project(root=sut)
        assert "zephyr-3_7" in project.os_profiles
        # Data, never registered: it resolves through the projects' context.
        assert "zephyr-3_7" not in OS_PROFILES
        prof = build_os_profile("zephyr-3_7", data=ProfileContext.from_projects([project]))
        assert prof.base == "embedded"
        assert prof.fields.defaults["os_version"] == "3.7"
        assert prof.fields.defaults["max_filename_len"] == 32
        # The ``base`` key is consumed, not kept as a default field.
        assert "base" not in prof.fields.defaults

    def test_missing_base_raises(self, tmp_path):
        sut = _write_project(
            tmp_path,
            textwrap.dedent("""
            [os_profiles.broken]
            os_name = "Zephyr"
        """),
        )
        # pydantic.ValidationError (a ValueError subclass) now fires for the
        # missing required 'base' field; the error location names the field.
        with pytest.raises(ValueError, match=r"os_profiles\.broken\.base"):
            Project(root=sut)

    def test_invalid_base_raises(self, tmp_path):
        sut = _write_project(
            tmp_path,
            textwrap.dedent("""
            [os_profiles.broken]
            base = "windows"
        """),
        )
        # Parsing keeps the table; the after-init check refuses the base,
        # naming the table and the project.
        from otto.host.os_profile import check_data_profiles

        project = Project(root=sut)
        with pytest.raises(
            ValueError,
            match=r"\[os_profiles\.broken\] in project .*base must name a registered host class",
        ):
            check_data_profiles([project])

    def test_unknown_default_field_raises(self, tmp_path):
        sut = _write_project(
            tmp_path,
            textwrap.dedent("""
            [os_profiles.broken]
            base = "unix"
            osTyp = "unix"
        """),
        )
        from otto.host.os_profile import check_data_profiles

        project = Project(root=sut)
        with pytest.raises(ValueError, match="unknown default field"):
            check_data_profiles([project])


class TestOsProfilesIntegration:
    """End-to-end: the project1 fixture's ``[os_profiles]`` tables flow through
    settings parse → profile context → factory, including a data-defined
    profile that references a *code-registered* command frame.
    """

    def test_project1_profile_resolves_code_registered_frame(self):
        import sys

        from otto.host.embedded_filesystem import FatRamFileSystem
        from otto.host.embedded_host import EmbeddedHost
        from otto.host.factory import create_host_from_dict
        from otto.host.os_profile import ProfileContext

        # Constructing the project parses settings, keeping the data profiles.
        project = MockProject(tests_root / "project1")
        assert {"zephyr-3.7", "zephyr-2.7", "zephyr-4.4"} <= set(project.os_profiles)

        # Importing the init modules registers the `zephyr-inline` frame the
        # 2.7 profile names — this runs *after* parse, mirroring bootstrap order.
        pylib = str(project.root / "pylib")
        added = pylib not in sys.path
        project.add_libs_to_pythonpath()
        try:
            project.import_init_modules()

            # A host need only declare its identity + filesystem; the profile
            # supplies the rest (the copy-paste this feature eliminates).
            host = create_host_from_dict(
                {
                    "ip": "192.0.2.13",
                    "os_type": "zephyr-2.7",
                    "filesystem": "fat-ram",
                },
                element=Element("zephyr27_demo"),
                profiles=ProfileContext.from_projects([project]),
            )
        finally:
            if added:
                while pylib in sys.path:
                    sys.path.remove(pylib)

        assert isinstance(host, EmbeddedHost)
        assert host.os_type == "zephyr-2.7"  # the profile selector is recorded
        assert host.os_name == "Zephyr"
        assert host.os_version == "2.7"
        assert host.max_filename_len == 32
        # The data profile resolved a frame that only code registered:
        assert type(host.command_frame).__name__ == "ZephyrInlineRetcodeFrame"
        # filesystem stays per-host:
        assert isinstance(host.filesystem, FatRamFileSystem)


@pytest.mark.parametrize("cls", [Project, CompiledSettings], ids=lambda cls: cls.__name__)
def test_settings_field_annotations_resolve_at_runtime(cls):
    """``typing.get_type_hints`` resolves the settings fields of the project dataclasses.

    The settings types the fields name live in another module, and the loader
    binds them at module scope, so a tool that reads the annotations at runtime
    (a dataclass serializer, a schema generator) gets the classes, not a NameError.
    Only those two fields are resolved, in the loader's own module globals: other
    fields name types imported for type checking only (``list["PendingLabSource"]``),
    which Python 3.14 resolves inside the generic where 3.10 left the string alone.
    """

    class SettingsFields:
        __annotations__ = {
            field: cls.__annotations__[field] for field in ("docker_settings", "monitor_settings")
        }

    SettingsFields.__module__ = cls.__module__
    hints = typing.get_type_hints(SettingsFields)

    assert hints["docker_settings"] is DockerSettings
    assert hints["monitor_settings"] is MonitorSettings
