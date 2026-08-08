"""Behavior pins for tests/_fixtures/sutrepo.py — against the migration's
load-bearing properties, not the implementation."""

import pytest
import tomli

from otto.config.repo import Repo
from tests._fixtures.sutrepo import make_sut_repo


def test_scaffold_loads_through_the_product_reader(tmp_path):
    """The point of the builder: what it writes IS a valid otto repo."""
    root = make_sut_repo(tmp_path / "sut", tests=["tests"])
    repo = Repo(sut_dir=root)
    assert repo.settings["name"] == "sut"
    assert repo.settings["version"] == "1.0.0"


def test_extra_toml_is_carried_verbatim(tmp_path):
    """Dependency tables etc. must arrive byte-for-byte — sites carry their
    exact TOML through `extra`, and a re-encoding renderer would change what
    the migrated tests feed the parser."""
    extra = '[dependencies]\nrequired = ["b >= 1"]\noptional = []\n'
    root = make_sut_repo(tmp_path / "a", name="a", extra=extra)
    text = (root / ".otto" / "settings.toml").read_text()
    assert extra.rstrip("\n") in text
    parsed = tomli.loads(text)
    assert parsed["dependencies"]["required"] == ["b >= 1"]
    assert parsed["name"] == "a"


def test_tests_list_renders_only_when_given(tmp_path):
    with_tests = (
        make_sut_repo(tmp_path / "w", tests=["tests", "more"]) / ".otto" / "settings.toml"
    ).read_text()
    assert tomli.loads(with_tests)["tests"] == ["tests", "more"]
    without = (make_sut_repo(tmp_path / "wo") / ".otto" / "settings.toml").read_text()
    assert "tests" not in tomli.loads(without)


def test_files_are_written_under_root_with_parents(tmp_path):
    root = make_sut_repo(
        tmp_path / "sut",
        files={"tests/test_a.py": "def test_ok():\n    assert True\n"},
    )
    assert (root / "tests" / "test_a.py").read_text().startswith("def test_ok")


def test_empty_tests_list_renders_an_empty_array(tmp_path):
    """tests=[] is the third case: distinct from None (no line) and from
    a populated list."""
    text = (make_sut_repo(tmp_path / "e", tests=[]) / ".otto" / "settings.toml").read_text()
    assert tomli.loads(text)["tests"] == []


def test_files_cannot_escape_the_root_or_clobber_settings(tmp_path):
    """files= is a convenience, not a bypass: a traversal out of the repo or
    a rewrite of the settings the scaffold just wrote must fail loudly (the
    drift guard cannot see writes routed through the fixture)."""
    with pytest.raises(ValueError, match="escapes the repo root"):
        make_sut_repo(tmp_path / "a", files={"../evil.txt": "x"})
    with pytest.raises(ValueError, match="must not overwrite the settings"):
        make_sut_repo(tmp_path / "b", files={".otto/settings.toml": "PWNED\n"})


def test_unescapable_name_or_version_fails_loudly(tmp_path):
    """A quote or backslash in name/version would silently corrupt the TOML
    (name="a\\b" parses to a control char) — refuse instead."""
    with pytest.raises(ValueError, match="needs TOML escaping"):
        make_sut_repo(tmp_path / "a", name='say "hi"')
    with pytest.raises(ValueError, match="needs TOML escaping"):
        make_sut_repo(tmp_path / "b", version="1.0\\0")


def test_touch_settings_writes_one_empty_fingerprint_file(tmp_path):
    from tests._fixtures.sutrepo import touch_settings

    settings = touch_settings(tmp_path / "stub")
    assert settings.read_text() == ""
    assert settings == tmp_path / "stub" / ".otto" / "settings.toml"
    # Stand-ins are re-touched freely (MagicMock repos recreate at will).
    assert touch_settings(tmp_path / "stub").read_text() == ""


def test_existing_root_fails_loudly(tmp_path):
    """Two scaffolds into one root would silently overwrite the first —
    mkdir(parents=True) without exist_ok makes the collision loud."""
    make_sut_repo(tmp_path / "sut")
    with pytest.raises(FileExistsError):
        make_sut_repo(tmp_path / "sut")
