import pytest

from otto.configmodule.version import Version


def test_version_no_arguments():

    with pytest.raises(TypeError):
        version = Version()

def test_version_null_string():

    with pytest.raises(ValueError):
        version = Version('')

def test_version_valid_string():

    version = Version('1.2.3')

    assert version.major == 1
    assert version.minor == 2
    assert version.patch == 3
    assert f'{version}' == '1.2.3'

def test_version_extra_values():

    version = Version('1.2.3.4')

    assert version.major == 1
    assert version.minor == 2
    assert version.patch == 3
    assert f'{version}' == '1.2.3'
