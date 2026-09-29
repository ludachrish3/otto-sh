import sys
import textwrap
from pathlib import Path

import pytest

from otto.config.repo import Repo
from otto.host.element import Element
from tests._fixtures.mockrepo import MockRepo
from tests._fixtures.paths import TESTS_ROOT
from tests._fixtures.sutrepo import make_sut_repo

mock_repo: MockRepo = None
tests_root = TESTS_ROOT


def _write_repo(tmp_path: Path, settings_body: str) -> Path:
    """Materialize a minimal SUT repo at *tmp_path* with the given TOML body
    appended after the required ``name`` / ``version`` fields.
    """
    return make_sut_repo(tmp_path, name="tmp_repo", extra=settings_body)


def _repo_with_settings(tmp_path: Path, settings_body: str) -> "Repo":
    """Materialize a minimal SUT repo (named ``p``) and return the parsed Repo.

    *settings_body* is TOML appended after the generated ``name``/``version``.
    """
    return Repo(sut_dir=make_sut_repo(tmp_path, name="p", extra=textwrap.dedent(settings_body)))


@pytest.fixture(autouse=False)
def default_mock_repo():

    global mock_repo  # noqa: PLW0603 — module-level singleton/cache

    mock_repo = MockRepo(tests_root / "repo1")


def test_repo_config_location(default_mock_repo):

    repo_settings_file = mock_repo.sut_dir / ".otto" / "settings.toml"
    assert repo_settings_file.exists()


def test_repo_settings_tests_sut_dir_variable(default_mock_repo):

    assert mock_repo.tests == [mock_repo.sut_dir / "tests"]


def test_repo_settings_init_sut_dir_variable(default_mock_repo):

    assert mock_repo.init == ["repo1_instructions", "custom_hosts", "repo1_monitor_uptime"]


def test_bootstrap_registers_repo1_instructions_and_options(monkeypatch):
    """``bootstrap()`` is the granular replacement for the deleted
    ``Repo.apply_settings()`` / ``apply_repo_settings()``: per repo it adds libs
    to ``sys.path`` and imports init modules — which register repo1's
    instructions and its verb-wide options classes into the shared
    ``INSTRUCTIONS``/``OPTIONS`` registries (module-level, process-wide).

    Isolation: Python's import cache couples this test to any earlier test in
    the same worker that imported repo1's modules — the cached modules make
    bootstrap's imports no-ops, the registrations never re-run, and the delta
    assertions see "sets are equal" (deterministically reproducible by running
    this test twice in one process). So: park any repo1-originated registry
    entries, evict the cached modules, and restore both afterwards.
    """
    from otto import bootstrap as bs
    from otto.cli.run import INSTRUCTIONS
    from otto.params import OPTIONS

    repo1 = tests_root / "repo1"
    pylib = str(repo1 / "pylib")
    repo1_options = [
        "repo1_common.options:DeviceTestOptions",
        "repo1_common.options:RepoOptions",
    ]

    # Remove any prior entries so the precondition holds even if another
    # test (or a previous run in the same worker) already appended it.
    while pylib in sys.path:
        sys.path.remove(pylib)

    assert pylib not in sys.path

    def _park(registry) -> dict:
        # Raw entries, so parking never resolves a lazy ``Ref``.
        parked = {}
        for name, entry, origin in registry._raw_items():
            if origin.startswith("repo1_instructions"):
                parked[name] = (entry, origin)
                registry.unregister(name)
        return parked

    registries = [INSTRUCTIONS, OPTIONS]
    parked = {registry: _park(registry) for registry in registries}
    evicted = {
        m: sys.modules.pop(m) for m in list(sys.modules) if m.startswith("repo1_instructions")
    }
    before = {registry: set(registry.names()) for registry in registries}

    monkeypatch.setenv("OTTO_SUT_DIRS", str(repo1))
    bs._reset()
    try:
        result = bs.bootstrap()
        assert result.errors == []

        assert pylib in sys.path
        assert set(INSTRUCTIONS.names()) > before[INSTRUCTIONS]
        assert sorted(set(OPTIONS.names()) - before[OPTIONS]) == repo1_options
        assert [OPTIONS.origin(name) for name in repo1_options] == ["repo1_instructions"] * 2
    finally:
        bs._reset()
        # Restore the exact pre-test world: sys.path, this test's
        # registrations out, the parked entries and cached modules back in.
        while pylib in sys.path:
            sys.path.remove(pylib)
        for registry in registries:
            for name in set(registry.names()) - before[registry]:
                registry.unregister(name)
            for name, (entry, origin) in parked[registry].items():
                registry._restore_raw(name, entry, origin)
        for mod in [m for m in sys.modules if m.startswith("repo1_instructions")]:
            sys.modules.pop(mod, None)
        sys.modules.update(evicted)


def test_logging_levels_parse_and_default_empty(tmp_path):
    """Spec §4.2: ``[logging.levels]`` lands on the Repo; absent means empty."""
    repo = _repo_with_settings(
        tmp_path,
        """
        [logging.levels]
        asyncssh = "DEBUG"
        vendor = "ERROR"
        """,
    )
    assert repo.logging_levels == {"asyncssh": "DEBUG", "vendor": "ERROR"}
    bare = _repo_with_settings(tmp_path / "bare", "")
    assert bare.logging_levels == {}


def test_removed_capture_key_is_a_hard_cutover_error(tmp_path):
    """Spec §4.3: the removed key is REJECTED, and the message points at its successor.

    ``extra='forbid'`` alone would name ``capture`` without saying what to do
    instead, so the model carries an explicit before-validator.
    """
    with pytest.raises(ValueError, match=r"\[logging\.levels\]") as exc:
        _repo_with_settings(
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
        _repo_with_settings(
            tmp_path,
            """
            [logging.levels]
            "otto.host" = "DEBUG"
            """,
        )
    with pytest.raises(ValueError, match="LOUD"):
        _repo_with_settings(
            tmp_path / "bad_level",
            """
            [logging.levels]
            vendor = "LOUD"
            """,
        )


def test_logging_levels_accept_the_warn_crit_aliases(tmp_path):
    """Spec §4.2: otto's own short aliases are levels too, and values normalize."""
    repo = _repo_with_settings(
        tmp_path,
        """
        [logging.levels]
        vendor = "warn"
        other = "CRIT"
        """,
    )
    assert repo.logging_levels == {"vendor": "WARN", "other": "CRIT"}


# TODO: Test various settings fields and the recording of arbitrary additional data


def test_repo_parses_unified_host_preferences(tmp_path):
    repo = _repo_with_settings(
        tmp_path,
        """
        [host_preferences.".*"]
        term = ["telnet"]
        ssh_options = { connect_timeout = 5.0 }
    """,
    )
    assert repo.host_preferences[".*"]["term"] == ["telnet"]
    assert repo.host_preferences[".*"]["ssh_options"] == {"connect_timeout": 5.0}
    assert not hasattr(repo, "host_defaults")


class TestHostPreferencesParsing:
    """Tests for unified ``[host_preferences]`` parsing in ``Repo.parse_settings``."""

    def test_absent_section_yields_empty_dict(self, tmp_path):
        sut = _write_repo(tmp_path, "")
        repo = Repo(sut_dir=sut)
        assert repo.host_preferences == {}

    def test_selections_and_option_tables_parsed(self, tmp_path):
        sut = _write_repo(
            tmp_path,
            textwrap.dedent("""
            [host_preferences.".*"]
            term = ["telnet"]

            [host_preferences.".*".ssh_options]
            port = 2222
            connect_timeout = 5.0
        """),
        )
        repo = Repo(sut_dir=sut)
        assert repo.host_preferences[".*"]["term"] == ["telnet"]
        assert repo.host_preferences[".*"]["ssh_options"] == {
            "port": 2222,
            "connect_timeout": 5.0,
        }

    def test_legacy_host_defaults_rejected(self, tmp_path):
        sut = _write_repo(
            tmp_path,
            textwrap.dedent("""
            [host_defaults.ssh_options]
            port = 2222
        """),
        )
        with pytest.raises(ValueError, match=r"\[host_defaults\] was removed"):
            Repo(sut_dir=sut)

    def test_unknown_preference_key_raises(self, tmp_path):
        sut = _write_repo(
            tmp_path,
            textwrap.dedent("""
            [host_preferences.".*"]
            bogus_options = { x = 1 }
        """),
        )
        with pytest.raises(ValueError, match="unknown"):
            Repo(sut_dir=sut)


@pytest.fixture
def restore_profiles():
    """Snapshot/restore the global os-profile registry around a test, since
    ``Repo.parse_settings`` registers data profiles into module-global state.
    """
    from otto.host import os_profile

    saved = dict(os_profile.OS_PROFILES._entries)
    saved_origins = dict(os_profile.OS_PROFILES._origins)
    try:
        yield
    finally:
        os_profile.OS_PROFILES._entries.clear()
        os_profile.OS_PROFILES._entries.update(saved)
        os_profile.OS_PROFILES._origins.clear()
        os_profile.OS_PROFILES._origins.update(saved_origins)


class TestOsProfilesParsing:
    """Tests for ``[os_profiles]`` parsing in ``Repo.parse_settings``."""

    def test_absent_section_yields_empty_dict(self, tmp_path, restore_profiles):
        sut = _write_repo(tmp_path, "")
        repo = Repo(sut_dir=sut)
        assert repo.os_profiles == {}

    def test_profile_parsed_and_registered(self, tmp_path, restore_profiles):
        from otto.host.os_profile import build_os_profile

        sut = _write_repo(
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
        repo = Repo(sut_dir=sut)
        assert "zephyr-3_7" in repo.os_profiles
        # Registered globally so lab data can select it by name.
        prof = build_os_profile("zephyr-3_7")
        assert prof.base == "embedded"
        assert prof.defaults["os_version"] == "3.7"
        assert prof.defaults["max_filename_len"] == 32
        # The ``base`` key is consumed, not kept as a default field.
        assert "base" not in prof.defaults

    def test_missing_base_raises(self, tmp_path, restore_profiles):
        sut = _write_repo(
            tmp_path,
            textwrap.dedent("""
            [os_profiles.broken]
            os_name = "Zephyr"
        """),
        )
        # pydantic.ValidationError (a ValueError subclass) now fires for the
        # missing required 'base' field; the error location names the field.
        with pytest.raises(ValueError, match=r"os_profiles\.broken\.base"):
            Repo(sut_dir=sut)

    def test_invalid_base_raises(self, tmp_path, restore_profiles):
        sut = _write_repo(
            tmp_path,
            textwrap.dedent("""
            [os_profiles.broken]
            base = "windows"
        """),
        )
        # _register_os_profiles wraps register_os_profile's rejection of an
        # unregistered base host class.
        with pytest.raises(ValueError, match="base must name a registered host class"):
            Repo(sut_dir=sut)

    def test_unknown_default_field_raises(self, tmp_path, restore_profiles):
        sut = _write_repo(
            tmp_path,
            textwrap.dedent("""
            [os_profiles.broken]
            base = "unix"
            osTyp = "unix"
        """),
        )
        with pytest.raises(ValueError, match="unknown default field"):
            Repo(sut_dir=sut)


class TestOsProfilesIntegration:
    """End-to-end: the repo1 fixture's ``[os_profiles]`` tables flow through
    settings parse → registry → factory, including a data-defined profile that
    references a *code-registered* command frame.
    """

    def test_repo1_profile_resolves_code_registered_frame(self, restore_profiles):
        import sys

        from otto.host.embedded_filesystem import FatRamFileSystem
        from otto.host.embedded_host import EmbeddedHost
        from otto.host.factory import create_host_from_dict

        # Constructing the repo parses settings, registering the data profiles.
        repo = MockRepo(tests_root / "repo1")
        assert {"zephyr-3.7", "zephyr-2.7", "zephyr-4.4"} <= set(repo.os_profiles)

        # Importing the init modules registers the `zephyr-inline` frame the
        # 2.7 profile names — this runs *after* parse, mirroring bootstrap order.
        pylib = str(repo.sut_dir / "pylib")
        added = pylib not in sys.path
        repo.add_libs_to_pythonpath()
        try:
            repo.import_init_modules()

            # A host need only declare its identity + filesystem; the profile
            # supplies the rest (the copy-paste this feature eliminates).
            host = create_host_from_dict(
                {
                    "ip": "192.0.2.13",
                    "os_type": "zephyr-2.7",
                    "filesystem": "fat-ram",
                },
                element=Element("zephyr27_demo"),
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
