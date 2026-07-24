"""Dependency resolution: statuses, cross-repo satisfiability, skip set."""

from otto.bootstrap import BootstrapWarning, DependencyError
from otto.config.dependencies import _resolve_statuses
from otto.config.repo import Repo


def _repo(tmp_path, name, version="1.0.0", *, required=(), optional=(), dirname=None):
    root = tmp_path / (dirname or name)
    (root / ".otto").mkdir(parents=True)
    req = ", ".join(f'"{e}"' for e in required)
    opt = ", ".join(f'"{e}"' for e in optional)
    (root / ".otto" / "settings.toml").write_text(
        f'name = "{name}"\nversion = "{version}"\n\n'
        f"[dependencies]\nrequired = [{req}]\noptional = [{opt}]\n"
    )
    return Repo(sut_dir=root)


def test_satisfied_required(tmp_path):
    a = _repo(tmp_path, "a", required=["b >= 1"])
    b = _repo(tmp_path, "b", version="1.2.0")
    out = _resolve_statuses([a, b])
    assert out.errors == []
    assert out.warnings == []
    assert out.skip_reason == {}
    (dep,) = a.dependencies
    assert dep.status == "satisfied"
    assert dep.provider_version is b.version
    assert out.required_edges == {(1, 0)}


def test_missing_required_errors_and_skips(tmp_path):
    a = _repo(tmp_path, "a", required=["ghost"])
    out = _resolve_statuses([a])
    (err,) = out.errors
    assert isinstance(err, DependencyError)
    assert "no project named 'ghost'" in str(err)
    assert str(a.sut_dir) in str(err)
    assert 0 in out.skip_reason
    assert a.dependencies[0].status == "missing"
    assert a.dependencies[0].provider_version is None


def test_incompatible_required(tmp_path):
    a = _repo(tmp_path, "a", required=["b >= 2"])
    _b = _repo(tmp_path, "b", version="1.2.0")
    out = _resolve_statuses([a, _b])
    (err,) = out.errors
    assert "not satisfied: found b 1.2.0" in str(err)
    assert a.dependencies[0].status == "incompatible"
    assert 0 in out.skip_reason


def test_case_and_punctuation_insensitive_match(tmp_path):
    a = _repo(tmp_path, "a", required=["My_Lib >= 1"])
    _b = _repo(tmp_path, "My.Lib", version="1.0.0", dirname="mylib")
    out = _resolve_statuses([a, _b])
    assert out.errors == []
    assert a.dependencies[0].status == "satisfied"


def test_extra_tag_ignored_by_constraints(tmp_path):
    a = _repo(tmp_path, "a", required=["b == 1.2.3"])
    _b = _repo(tmp_path, "b", version="1.2.3-rc1")
    out = _resolve_statuses([a, _b])
    assert out.errors == []
    assert a.dependencies[0].status == "satisfied"


def test_optional_absent_is_silent(tmp_path):
    a = _repo(tmp_path, "a", optional=["ghost"])
    out = _resolve_statuses([a])
    assert out.errors == []
    assert out.warnings == []
    assert out.skip_reason == {}
    assert a.dependencies[0].status == "missing"


def test_optional_incompatible_warns_only(tmp_path):
    a = _repo(tmp_path, "a", optional=["metrics >= 1.4"])
    _m = _repo(tmp_path, "metrics", version="1.2.0")
    out = _resolve_statuses([a, _m])
    assert out.errors == []
    assert out.skip_reason == {}
    (warn,) = out.warnings
    assert isinstance(warn, BootstrapWarning)
    assert "optional dependency" in warn.message
    assert "found 1.2.0" in warn.message
    assert "feature disabled" in warn.message
    assert warn.message.startswith(f"repo {a.sut_dir}:")
    assert a.dependencies[0].status == "incompatible"
    assert out.soft_edges == []  # incompatible optional contributes no edge


def test_optional_satisfied_soft_edge(tmp_path):
    a = _repo(tmp_path, "a", optional=["metrics >= 1"])
    _m = _repo(tmp_path, "metrics", version="1.4.0")
    out = _resolve_statuses([a, _m])
    assert out.soft_edges == [(1, 0)]


def test_ambiguous_name_errors_when_referenced(tmp_path):
    a = _repo(tmp_path, "a", required=["twin"])
    _t1 = _repo(tmp_path, "twin", dirname="twin1")
    _t2 = _repo(tmp_path, "Twin", dirname="twin2")
    out = _resolve_statuses([a, _t1, _t2])
    (err,) = out.errors
    assert "ambiguous" in str(err)
    assert "twin1" in str(err)
    assert "twin2" in str(err)
    assert a.dependencies[0].status == "ambiguous"
    assert 0 in out.skip_reason


def test_duplicate_names_unreferenced_no_error(tmp_path):
    t1 = _repo(tmp_path, "twin", dirname="twin1")
    t2 = _repo(tmp_path, "twin", dirname="twin2")
    out = _resolve_statuses([t1, t2])
    assert out.errors == []


def test_cross_repo_unsatisfiable_errors_all_participants(tmp_path):
    a = _repo(tmp_path, "a", required=["x >= 2"])
    b = _repo(tmp_path, "b", required=["x < 2"])
    _x = _repo(tmp_path, "x", version="2.5.0")
    out = _resolve_statuses([a, b, _x])
    unsat = [e for e in out.errors if "no possible version" in str(e)]
    assert len(unsat) == 2  # one per participating repo
    for err in unsat:
        assert "a requires" in str(err)
        assert "b requires" in str(err)
    assert 0 in out.skip_reason
    assert 1 in out.skip_reason
    # b ALSO gets the concrete incompatibility error (found 2.5.0)
    assert any("not satisfied: found x 2.5.0" in str(e) for e in out.errors)


def test_cross_repo_unsat_within_one_repo_two_entries(tmp_path):
    a = _repo(tmp_path, "a", required=["x >= 2", "x < 2"])
    _x = _repo(tmp_path, "x", version="2.5.0")
    out = _resolve_statuses([a, _x])
    unsat = [e for e in out.errors if "no possible version" in str(e)]
    assert len(unsat) == 1  # deduped per repo
    assert 0 in out.skip_reason
