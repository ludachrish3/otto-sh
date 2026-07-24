import pytest

from otto.config.version import Version


def test_version_no_arguments():

    with pytest.raises(TypeError):
        Version()


def test_version_null_string():

    with pytest.raises(ValueError, match="does not match the expected format"):
        Version("")


def test_version_valid_string():

    version = Version("1.2.3")

    assert version.major == 1
    assert version.minor == 2
    assert version.patch == 3
    assert f"{version}" == "1.2.3"


def test_version_extra_values():

    version = Version("1.2.3.4")

    assert version.major == 1
    assert version.minor == 2
    assert version.patch == 3
    assert version.extra == ".4"
    assert f"{version}" == "1.2.3.4"


def test_version_extra_tag_parsed():
    v = Version("1.2.3-rc1")
    assert (v.major, v.minor, v.patch) == (1, 2, 3)
    assert v.extra == "-rc1"
    assert repr(v) == "1.2.3-rc1"


def test_version_extra_plus_and_dot_separators():
    assert Version("1.2.3+build.5").extra == "+build.5"
    assert Version("1.2.3.dev1").extra == ".dev1"


def test_version_without_extra_has_none():
    v = Version("1.2.3")
    assert v.extra is None
    assert repr(v) == "1.2.3"


def test_version_key_ignores_extra():
    assert Version("1.2.3-rc1").key == (1, 2, 3)
    assert Version("1.2.3").key == (1, 2, 3)


def test_version_garbage_suffix_now_rejected():
    with pytest.raises(ValueError, match="does not match the expected format"):
        Version("1.2.3garbage")


def test_version_bare_separator_rejected():
    with pytest.raises(ValueError, match="does not match the expected format"):
        Version("1.2.3-")


def test_settings_regex_drift_lockstep():
    """models/settings.py deliberately duplicates the pattern — keep behavior identical."""
    from otto.config.version import version_re
    from otto.models.settings import _VERSION_RE

    probes = [
        "1.2.3",
        "1.2.3-rc1",
        "1.2.3+build.5",
        "1.2.3.dev1",
        "10.20.30",
        "1.2.3garbage",
        "1.2.3-",
        "1.2",
        "1.2.3 ",
        "x.y.z",
        "",
    ]
    for probe in probes:
        assert (version_re.match(probe) is None) == (_VERSION_RE.match(probe) is None), probe
