import sys
import textwrap
from pathlib import Path

import pytest

from otto.configmodule.repo import Repo
from tests._fixtures.mockrepo import MockRepo

mock_repo: MockRepo = None
tests_root = Path(__file__).parent.parent.parent


def _write_repo(tmp_path: Path, settings_body: str) -> Path:
    """Materialize a minimal SUT repo at *tmp_path* with the given TOML body
    appended after the required ``name`` / ``version`` fields.
    """
    otto_dir = tmp_path / ".otto"
    otto_dir.mkdir(parents=True)
    base = textwrap.dedent("""
        name = "tmp_repo"
        version = "1.0.0"
    """).strip()
    (otto_dir / "settings.toml").write_text(f"{base}\n{settings_body}\n")
    return tmp_path


def _repo_with_settings(tmp_path: Path, settings_body: str) -> "Repo":
    """Materialize a minimal SUT repo and return the parsed Repo.

    Accepts raw TOML (including ``name``/``version``) — no base prepended.
    """
    import textwrap as _textwrap

    otto_dir = tmp_path / ".otto"
    otto_dir.mkdir(parents=True, exist_ok=True)
    (otto_dir / "settings.toml").write_text(_textwrap.dedent(settings_body))
    return Repo(sut_dir=tmp_path)


@pytest.fixture(autouse=False, scope="function")
def default_mock_repo():

    global mock_repo

    mock_repo = MockRepo(tests_root / "repo1")


def test_repo_config_location(default_mock_repo):

    repo_settings_file = mock_repo.sut_dir / ".otto" / "settings.toml"
    assert repo_settings_file.exists()


def test_repo_commit_name(default_mock_repo):

    assert mock_repo.commit_name == f"{mock_repo.commit} ({mock_repo.description})"


def test_repo_settings_tests_sut_dir_variable(default_mock_repo):

    assert mock_repo.tests == [mock_repo.sut_dir / "tests"]


def test_repo_settings_init_sut_dir_variable(default_mock_repo):

    assert mock_repo.init == ["repo1_instructions", "custom_hosts"]


def test_repo_apply_settings(default_mock_repo):

    pylib = str(mock_repo.sut_dir / "pylib")

    # Remove any prior entries so the precondition holds even if another
    # test (or a previous run in the same worker) already appended it.
    while pylib in sys.path:
        sys.path.remove(pylib)

    assert pylib not in sys.path

    mock_repo.apply_settings()

    assert pylib in sys.path

    # Clean up so we don't pollute sys.path for subsequent tests.
    sys.path.remove(pylib)


# TODO: Test various settings fields and the recording of arbitrary additional data


class TestValidLabsParsing:
    """Tests for ``valid_labs`` parsing in ``Repo.parse_settings``.

    ``valid_labs`` lets a repo declare which labs it supports (e.g. an embedded
    product that only works in an embedded lab). Parsing stores the declared
    list; an unset key yields an empty list. Enforcement (rejecting a selected
    lab not in the list, and treating an empty list as "must declare") is a
    separate, deferred step — parsing must not silently treat unset as
    allow-all.
    """

    def test_absent_yields_empty_list(self, tmp_path):
        sut = _write_repo(tmp_path, "")
        repo = Repo(sut_dir=sut)
        assert repo.valid_labs == []

    def test_declared_labs_parsed_in_order(self, tmp_path):
        sut = _write_repo(tmp_path, 'valid_labs = ["embedded", "veggies"]')
        repo = Repo(sut_dir=sut)
        assert repo.valid_labs == ["embedded", "veggies"]


def test_repo_parses_unified_host_preferences(tmp_path):
    repo = _repo_with_settings(
        tmp_path,
        """
        name = "p"
        version = "1.0.0"
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

    def test_sutdir_expansion_in_host_preferences(self, tmp_path):
        """``${sut_dir}`` is expanded inside ``[host_preferences]`` strings, like
        every other repo settings table.
        """
        sut = _write_repo(
            tmp_path,
            textwrap.dedent("""
            [host_preferences.".*".ssh_options]
            known_hosts = "${sut_dir}/known_hosts"
        """),
        )
        repo = Repo(sut_dir=sut)
        assert repo.host_preferences[".*"]["ssh_options"]["known_hosts"] == f"{sut}/known_hosts"


@pytest.fixture
def restore_profiles():
    """Snapshot/restore the global os-profile registry around a test, since
    ``Repo.parse_settings`` registers data profiles into module-global state.
    """
    from otto.host import os_profile

    saved = dict(os_profile._OS_PROFILES)
    try:
        yield
    finally:
        os_profile._OS_PROFILES.clear()
        os_profile._OS_PROFILES.update(saved)


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

    def test_sutdir_expansion_in_profile_default(self, tmp_path, restore_profiles):
        sut = _write_repo(
            tmp_path,
            textwrap.dedent("""
            [os_profiles.nix]
            base = "unix"

            [os_profiles.nix.ssh_options]
            known_hosts = "${sut_dir}/known_hosts"
        """),
        )
        repo = Repo(sut_dir=sut)
        prof = repo.os_profiles["nix"]
        assert prof.defaults["ssh_options"]["known_hosts"] == f"{sut}/known_hosts"


class TestOsProfilesIntegration:
    """End-to-end: the repo1 fixture's ``[os_profiles]`` tables flow through
    settings parse → registry → factory, including a data-defined profile that
    references a *code-registered* command frame.
    """

    def test_repo1_profile_resolves_code_registered_frame(self, restore_profiles):
        import sys

        from otto.host.embedded_filesystem import FatRamFileSystem
        from otto.host.embedded_host import EmbeddedHost
        from otto.storage.factory import create_host_from_dict

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
                    "element": "sprout27demo",
                    "os_type": "zephyr-2.7",
                    "filesystem": "fat-ram",
                }
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
