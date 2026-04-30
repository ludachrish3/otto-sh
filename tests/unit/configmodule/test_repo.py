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
