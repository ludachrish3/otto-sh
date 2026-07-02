"""bootstrap(): phases, idempotence, containment framing."""

import textwrap

import pytest

from otto import bootstrap as bs


@pytest.fixture(autouse=True)
def _fresh(monkeypatch):
    bs._reset()
    yield
    bs._reset()


def _write_repo(tmp_path, *, broken_test: bool = False) -> str:
    repo = tmp_path / "repo"
    (repo / ".otto").mkdir(parents=True)
    (repo / ".otto" / "settings.toml").write_text(
        textwrap.dedent(
            """
            name = "repo"
            version = "1.0.0"
            tests = ["${sut_dir}/tests"]
            """
        )
    )
    tests = repo / "tests"
    tests.mkdir()
    if broken_test:
        (tests / "test_broken.py").write_text("def broken(:\n")  # SyntaxError
    else:
        (tests / "test_ok.py").write_text("X = 1\n")
    return str(repo)


def test_idempotent_single_result(tmp_path, monkeypatch):
    monkeypatch.setenv("OTTO_SUT_DIRS", _write_repo(tmp_path))
    first = bs.bootstrap()
    assert first is bs.bootstrap()
    assert first.errors == []
    assert len(first.repos) == 1


def test_broken_test_file_is_contained_and_framed(tmp_path, monkeypatch):
    monkeypatch.setenv("OTTO_SUT_DIRS", _write_repo(tmp_path, broken_test=True))
    result = bs.bootstrap()
    assert len(result.errors) == 1
    msg = str(result.errors[0])
    assert "failed to load" in msg
    assert "test_broken.py" in msg
    assert "repo" in msg
    assert isinstance(result.errors[0].__cause__, SyntaxError)


def test_discover_runs_no_user_code(tmp_path, monkeypatch):
    monkeypatch.setenv("OTTO_SUT_DIRS", _write_repo(tmp_path, broken_test=True))
    _env, repos = bs.discover()  # broken test file must NOT explode discovery
    assert len(repos) == 1


def _write_bad_toml_repo(tmp_path) -> str:
    repo = tmp_path / "bad"
    (repo / ".otto").mkdir(parents=True)
    (repo / ".otto" / "settings.toml").write_text("this is [not valid toml\n")
    return str(repo)


def test_malformed_settings_toml_is_contained_and_framed(tmp_path, monkeypatch):
    good = _write_repo(tmp_path)
    bad = _write_bad_toml_repo(tmp_path)
    monkeypatch.setenv("OTTO_SUT_DIRS", f"{good},{bad}")
    result = bs.bootstrap()
    assert len(result.repos) == 1  # the healthy repo still loads
    assert len(result.errors) == 1
    msg = str(result.errors[0])
    assert "failed to load" in msg
    assert "settings.toml" in msg
    assert str(bad) in msg


def test_discover_contains_settings_errors_without_raising(tmp_path, monkeypatch):
    monkeypatch.setenv("OTTO_SUT_DIRS", _write_bad_toml_repo(tmp_path))
    _env, repos = bs.discover()  # malformed config data must NOT explode discovery
    assert repos == []


def test_reset_clears_discovery_errors(tmp_path, monkeypatch):
    monkeypatch.setenv("OTTO_SUT_DIRS", _write_bad_toml_repo(tmp_path))
    assert bs.bootstrap().errors
    bs._reset()
    # A re-bootstrap against a now-healthy world must not carry stale errors.
    monkeypatch.setenv("OTTO_SUT_DIRS", _write_repo(tmp_path))
    assert bs.bootstrap().errors == []
