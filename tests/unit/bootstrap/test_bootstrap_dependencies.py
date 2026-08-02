"""bootstrap(): dependency pass wiring — skip, warnings, registration order."""

import pytest

from otto import bootstrap as bs


@pytest.fixture(autouse=True)
def _fresh(monkeypatch):
    bs._reset()
    yield
    bs._reset()


def _write_repo(tmp_path, name, version="1.0.0", *, required=(), optional=()) -> str:
    """A repo whose init module appends its name to $OTTO_TEST_ORDER_FILE."""
    repo = tmp_path / name
    (repo / ".otto").mkdir(parents=True)
    req = ", ".join(f'"{e}"' for e in required)
    opt = ", ".join(f'"{e}"' for e in optional)
    (repo / ".otto" / "settings.toml").write_text(
        f'name = "{name}"\nversion = "{version}"\n'
        f'libs = ["."]\ninit = ["{name}_init"]\n\n'
        f"[dependencies]\nrequired = [{req}]\noptional = [{opt}]\n"
    )
    (repo / f"{name}_init.py").write_text(
        "import os, pathlib\n"
        "with pathlib.Path(os.environ['OTTO_TEST_ORDER_FILE']).open('a') as f:\n"
        f"    f.write('{name}\\n')\n"
    )
    return str(repo)


@pytest.fixture
def order_file(tmp_path, monkeypatch):
    path = tmp_path / "order.txt"
    path.touch()
    monkeypatch.setenv("OTTO_TEST_ORDER_FILE", str(path))
    return path


def _order(order_file):
    return order_file.read_text().split()


def test_dep_free_setup_registers_in_sut_dir_order(tmp_path, monkeypatch, order_file):
    dirs = [_write_repo(tmp_path, n) for n in ("c", "a", "b")]
    monkeypatch.setenv("OTTO_SUT_DIRS", ",".join(dirs))
    result = bs.bootstrap()
    assert result.errors == []
    assert result.warnings == []
    assert _order(order_file) == ["c", "a", "b"]


def test_required_dep_reorders_registration(tmp_path, monkeypatch, order_file):
    b = _write_repo(tmp_path, "b", required=["a >= 1"])
    a = _write_repo(tmp_path, "a")
    monkeypatch.setenv("OTTO_SUT_DIRS", f"{b},{a}")
    result = bs.bootstrap()
    assert result.errors == []
    assert _order(order_file) == ["a", "b"]


def test_missing_required_skips_registration(tmp_path, monkeypatch, order_file):
    a = _write_repo(tmp_path, "a", required=["ghost"])
    b = _write_repo(tmp_path, "b")
    monkeypatch.setenv("OTTO_SUT_DIRS", f"{a},{b}")
    result = bs.bootstrap()
    assert _order(order_file) == ["b"]  # a never registered
    assert len(result.repos) == 2  # but still discovered/visible
    (err,) = result.errors
    assert "ghost" in str(err)
    a_repo = next(r for r in result.repos if r.name == "a")
    assert a_repo.dependencies[0].status == "missing"


def test_optional_incompatible_warns_and_registers(tmp_path, monkeypatch, order_file):
    a = _write_repo(tmp_path, "a", optional=["metrics >= 1.4"])
    m = _write_repo(tmp_path, "metrics", version="1.2.0")
    monkeypatch.setenv("OTTO_SUT_DIRS", f"{a},{m}")
    result = bs.bootstrap()
    assert result.errors == []
    (warn,) = result.warnings
    assert "feature disabled" in warn.message
    assert sorted(_order(order_file)) == ["a", "metrics"]


def test_skip_propagation_through_bootstrap(tmp_path, monkeypatch, order_file):
    a = _write_repo(tmp_path, "a", required=["b"])
    b = _write_repo(tmp_path, "b", required=["ghost"])
    monkeypatch.setenv("OTTO_SUT_DIRS", f"{a},{b}")
    result = bs.bootstrap()
    assert _order(order_file) == []
    assert len(result.errors) == 2  # missing + propagation


def test_import_error_does_not_propagate_like_dep_failure(tmp_path, monkeypatch, order_file):
    # b's init module raises; a requires b (satisfied). a must STILL register.
    a = _write_repo(tmp_path, "a", required=["b"])
    b = _write_repo(tmp_path, "b")
    (tmp_path / "b" / "b_init.py").write_text("raise RuntimeError('boom')\n")
    monkeypatch.setenv("OTTO_SUT_DIRS", f"{a},{b}")
    result = bs.bootstrap()
    assert _order(order_file) == ["a"]  # b's marker never ran, a's did
    assert len(result.errors) == 1  # only the contained import error
    assert "failed to load" in str(result.errors[0])


def test_optional_provider_skipped_warns_and_dependent_registers(tmp_path, monkeypatch, order_file):
    # d optionally depends on e; e's own required dep is missing so e is
    # skipped entirely. d must still register, but a warning must surface
    # that the optional feature it thinks it has is actually unavailable.
    d = _write_repo(tmp_path, "d", optional=["e >= 1"])
    e = _write_repo(tmp_path, "e", required=["ghost"])
    monkeypatch.setenv("OTTO_SUT_DIRS", f"{d},{e}")
    result = bs.bootstrap()
    assert _order(order_file) == ["d"]  # d registered, e never did
    (warn,) = result.warnings
    assert "e >= 1" in warn.message
    assert "feature disabled" in warn.message


def test_no_dependencies_table_registers_normally(tmp_path, monkeypatch, order_file):
    # A settings.toml with no [dependencies] table at all -- must still
    # register normally, with an empty resolved-dependencies list.
    root = tmp_path / "solo"
    (root / ".otto").mkdir(parents=True)
    (root / ".otto" / "settings.toml").write_text(
        'name = "solo"\nversion = "1.0.0"\nlibs = ["."]\ninit = ["solo_init"]\n'
    )
    (root / "solo_init.py").write_text(
        "import os, pathlib\n"
        "with pathlib.Path(os.environ['OTTO_TEST_ORDER_FILE']).open('a') as f:\n"
        "    f.write('solo\\n')\n"
    )
    monkeypatch.setenv("OTTO_SUT_DIRS", str(root))
    result = bs.bootstrap()
    assert result.errors == []
    assert result.warnings == []
    assert _order(order_file) == ["solo"]
    (repo,) = result.repos
    assert repo.dependencies == []
