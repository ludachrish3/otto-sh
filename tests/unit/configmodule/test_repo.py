import sys
import textwrap
from pathlib import Path

import pytest

from otto.configmodule.repo import Repo
from tests.mockrepo import MockRepo

mockRepo: MockRepo = None
testsRoot = Path(__file__).parent.parent.parent


def _write_repo(tmp_path: Path, settings_body: str) -> Path:
    """Materialize a minimal SUT repo at *tmp_path* with the given TOML body
    appended after the required ``name`` / ``version`` fields.
    """
    otto_dir = tmp_path / '.otto'
    otto_dir.mkdir(parents=True)
    base = textwrap.dedent('''
        name = "tmp_repo"
        version = "1.0.0"
    ''').strip()
    (otto_dir / 'settings.toml').write_text(f'{base}\n{settings_body}\n')
    return tmp_path

@pytest.fixture(autouse=False, scope='function')
def default_mock_repo():

    global mockRepo

    mockRepo = MockRepo(testsRoot / 'repo1')

def test_repo_config_location(default_mock_repo):

    repoSettingsFile = mockRepo.sutDir / '.otto' / 'settings.toml'
    assert repoSettingsFile.exists()

def test_repo_commit_name(default_mock_repo):

    assert mockRepo.commitName == f"{mockRepo.commit} ({mockRepo.description})"

def test_repo_settings_tests_sut_dir_variable(default_mock_repo):

    assert mockRepo.tests == [ mockRepo.sutDir / 'tests' ]

def test_repo_settings_init_sut_dir_variable(default_mock_repo):

    assert mockRepo.init == [ 'repo1_instructions' ]

def test_repo_apply_settings(default_mock_repo):

    pylib = str(mockRepo.sutDir / 'pylib')

    # Remove any prior entries so the precondition holds even if another
    # test (or a previous run in the same worker) already appended it.
    while pylib in sys.path:
        sys.path.remove(pylib)

    assert pylib not in sys.path

    mockRepo.applySettings()

    assert pylib in sys.path

    # Clean up so we don't pollute sys.path for subsequent tests.
    sys.path.remove(pylib)


# TODO: Test various settings fields and the recording of arbitrary additional data


class TestHostDefaultsParsing:
    """Tests for ``[host_defaults]`` parsing in ``Repo.parseSettings``."""

    def test_absent_section_yields_empty_dict(self, tmp_path):
        sut = _write_repo(tmp_path, '')
        repo = Repo(sutDir=sut)
        assert repo.host_defaults == {}

    def test_empty_section_yields_empty_dict(self, tmp_path):
        sut = _write_repo(tmp_path, '[host_defaults]')
        repo = Repo(sutDir=sut)
        assert repo.host_defaults == {}

    def test_single_protocol_default(self, tmp_path):
        sut = _write_repo(tmp_path, textwrap.dedent('''
            [host_defaults.ssh_options]
            port = 2222
            connect_timeout = 5.0
        '''))
        repo = Repo(sutDir=sut)
        assert repo.host_defaults == {
            'ssh_options': {'port': 2222, 'connect_timeout': 5.0},
        }

    def test_multiple_protocol_defaults(self, tmp_path):
        sut = _write_repo(tmp_path, textwrap.dedent('''
            [host_defaults.ssh_options]
            connect_timeout = 5.0

            [host_defaults.telnet_options]
            cols = 200
        '''))
        repo = Repo(sutDir=sut)
        assert repo.host_defaults == {
            'ssh_options': {'connect_timeout': 5.0},
            'telnet_options': {'cols': 200},
        }

    def test_unknown_options_table_raises(self, tmp_path):
        sut = _write_repo(tmp_path, textwrap.dedent('''
            [host_defaults.bogus_options]
            x = 1
        '''))
        with pytest.raises(ValueError, match='unknown'):
            Repo(sutDir=sut)

    def test_sutdir_expansion_in_host_defaults(self, tmp_path):
        """``${sutDir}`` is expanded inside ``[host_defaults]`` strings, like
        every other repo settings table."""
        sut = _write_repo(tmp_path, textwrap.dedent('''
            [host_defaults.ssh_options]
            known_hosts = "${sutDir}/known_hosts"
        '''))
        repo = Repo(sutDir=sut)
        assert repo.host_defaults['ssh_options']['known_hosts'] == f'{sut}/known_hosts'


@pytest.fixture
def restore_profiles():
    """Snapshot/restore the global os-profile registry around a test, since
    ``Repo.parseSettings`` registers data profiles into module-global state."""
    from otto.host import os_profile
    saved = dict(os_profile._OS_PROFILES)
    try:
        yield
    finally:
        os_profile._OS_PROFILES.clear()
        os_profile._OS_PROFILES.update(saved)


class TestOsProfilesParsing:
    """Tests for ``[os_profiles]`` parsing in ``Repo.parseSettings``."""

    def test_absent_section_yields_empty_dict(self, tmp_path, restore_profiles):
        sut = _write_repo(tmp_path, '')
        repo = Repo(sutDir=sut)
        assert repo.os_profiles == {}

    def test_profile_parsed_and_registered(self, tmp_path, restore_profiles):
        from otto.host.os_profile import build_os_profile
        sut = _write_repo(tmp_path, textwrap.dedent('''
            [os_profiles.zephyr-3_7]
            base = "embedded"
            osName = "Zephyr"
            osVersion = "3.7"
            command_frame = "zephyr"
            filesystem = "fat-ram"
            max_filename_len = 32
        '''))
        repo = Repo(sutDir=sut)
        assert 'zephyr-3_7' in repo.os_profiles
        # Registered globally so lab data can select it by name.
        prof = build_os_profile('zephyr-3_7')
        assert prof.base == 'embedded'
        assert prof.defaults['osVersion'] == '3.7'
        assert prof.defaults['max_filename_len'] == 32
        # The ``base`` key is consumed, not kept as a default field.
        assert 'base' not in prof.defaults

    def test_missing_base_raises(self, tmp_path, restore_profiles):
        sut = _write_repo(tmp_path, textwrap.dedent('''
            [os_profiles.broken]
            osName = "Zephyr"
        '''))
        with pytest.raises(ValueError, match="missing the required 'base'"):
            Repo(sutDir=sut)

    def test_invalid_base_raises(self, tmp_path, restore_profiles):
        sut = _write_repo(tmp_path, textwrap.dedent('''
            [os_profiles.broken]
            base = "windows"
        '''))
        with pytest.raises(ValueError, match='base must be one of'):
            Repo(sutDir=sut)

    def test_unknown_default_field_raises(self, tmp_path, restore_profiles):
        sut = _write_repo(tmp_path, textwrap.dedent('''
            [os_profiles.broken]
            base = "unix"
            osTyp = "unix"
        '''))
        with pytest.raises(ValueError, match='unknown default field'):
            Repo(sutDir=sut)

    def test_sutdir_expansion_in_profile_default(self, tmp_path, restore_profiles):
        sut = _write_repo(tmp_path, textwrap.dedent('''
            [os_profiles.nix]
            base = "unix"

            [os_profiles.nix.ssh_options]
            known_hosts = "${sutDir}/known_hosts"
        '''))
        repo = Repo(sutDir=sut)
        prof = repo.os_profiles['nix']
        assert prof.defaults['ssh_options']['known_hosts'] == f'{sut}/known_hosts'


class TestOsProfilesIntegration:
    """End-to-end: the repo1 fixture's ``[os_profiles]`` tables flow through
    settings parse → registry → factory, including a data-defined profile that
    references a *code-registered* command frame."""

    def test_repo1_profile_resolves_code_registered_frame(self, restore_profiles):
        import sys

        from otto.host.embedded_filesystem import FatRamFileSystem
        from otto.host.embeddedHost import EmbeddedHost
        from otto.storage.factory import create_host_from_dict

        # Constructing the repo parses settings, registering the data profiles.
        repo = MockRepo(testsRoot / 'repo1')
        assert {'zephyr-3.7', 'zephyr-2.7', 'zephyr-4.4'} <= set(repo.os_profiles)

        # Importing the init modules registers the `zephyr-inline` frame the
        # 2.7 profile names — this runs *after* parse, mirroring bootstrap order.
        pylib = str(repo.sutDir / 'pylib')
        added = pylib not in sys.path
        repo.addLibsToPythonpath()
        try:
            repo.importInitModules()

            # A host need only declare its identity + filesystem; the profile
            # supplies the rest (the copy-paste this feature eliminates).
            host = create_host_from_dict({
                'ip': '192.0.2.13', 'ne': 'sprout27demo',
                'osType': 'zephyr-2.7', 'filesystem': 'fat-ram',
            })
        finally:
            if added:
                while pylib in sys.path:
                    sys.path.remove(pylib)

        assert isinstance(host, EmbeddedHost)
        assert host.osType == 'embedded'      # base family, not the profile name
        assert host.osName == 'Zephyr'
        assert host.osVersion == '2.7'
        assert host.max_filename_len == 32
        # The data profile resolved a frame that only code registered:
        assert type(host.command_frame).__name__ == 'ZephyrInlineRetcodeFrame'
        # filesystem stays per-host:
        assert isinstance(host.filesystem, FatRamFileSystem)
