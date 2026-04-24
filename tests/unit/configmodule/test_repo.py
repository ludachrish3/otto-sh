import sys
from pathlib import Path

import pytest

from tests.mockrepo import MockRepo

mockRepo: MockRepo = None
testsRoot = Path(__file__).parent.parent.parent

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
