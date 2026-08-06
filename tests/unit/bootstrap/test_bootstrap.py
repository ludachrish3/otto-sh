"""bootstrap(): phases, idempotence, containment framing."""

import pathlib
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
            tests = ["tests"]
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
    discovered = bs.discover()  # broken test file must NOT explode discovery
    assert len(discovered.repos) == 1
    assert discovered.errors == []  # phase 1 never imports it, so it cannot have failed yet


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
    discovered = bs.discover()  # malformed config data must NOT explode discovery
    assert discovered.repos == []
    # The name of this test promises the error is CONTAINED, not merely survived;
    # asserting only `repos == []` passed just as well when nothing was recorded.
    assert len(discovered.errors) == 1
    assert "settings.toml" in str(discovered.errors[0])


def test_invalidate_recovers_from_fixed_repo(tmp_path, monkeypatch):
    """The embedder recovery path (todo/bootstrap-discovery-errors-accumulate.md):
    fix the repo, invalidate(), re-bootstrap — the stale error is gone because
    errors ride the cached ``DiscoveryResult`` instead of a parallel global."""
    bad = _write_bad_toml_repo(tmp_path)
    monkeypatch.setenv("OTTO_SUT_DIRS", bad)
    assert bs.bootstrap().errors
    # Fix the same repo in place, then invalidate: recomputed discovery must
    # not carry the prior failure.
    fixed_toml = 'name = "fixed"\nversion = "1.0.0"\n'
    (pathlib.Path(bad) / ".otto" / "settings.toml").write_text(fixed_toml)
    bs.invalidate()
    assert bs.bootstrap().errors == []


def test_reset_clears_discovery_errors(tmp_path, monkeypatch):
    monkeypatch.setenv("OTTO_SUT_DIRS", _write_bad_toml_repo(tmp_path))
    assert bs.bootstrap().errors
    bs._reset()
    # A re-bootstrap against a now-healthy world must not carry stale errors.
    monkeypatch.setenv("OTTO_SUT_DIRS", _write_repo(tmp_path))
    assert bs.bootstrap().errors == []
