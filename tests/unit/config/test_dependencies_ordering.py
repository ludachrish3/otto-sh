"""Skip propagation, required cycles, soft edges, stable topological order."""

from otto.config.dependencies import resolve_dependencies
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


def _names(repos):
    return [r.name for r in repos]


def test_dep_free_setup_keeps_sut_dir_order(tmp_path):
    repos = [_repo(tmp_path, n) for n in ("c", "a", "b")]
    out = resolve_dependencies(repos)
    assert _names(out.ordered) == ["c", "a", "b"]
    assert out.errors == []
    assert out.warnings == []


def test_required_dep_registers_provider_first(tmp_path):
    b = _repo(tmp_path, "b", required=["a >= 1"])
    a = _repo(tmp_path, "a")
    out = resolve_dependencies([b, a])  # b listed first in sut dirs
    assert _names(out.ordered) == ["a", "b"]


def test_stable_tiebreak_among_ready(tmp_path):
    # z and m both depend on a; among simultaneously-ready repos sut-dir order holds
    z = _repo(tmp_path, "z", required=["a"])
    m = _repo(tmp_path, "m", required=["a"])
    a = _repo(tmp_path, "a")
    out = resolve_dependencies([z, m, a])
    assert _names(out.ordered) == ["a", "z", "m"]


def test_skip_propagates_to_dependents(tmp_path):
    a = _repo(tmp_path, "a", required=["b"])
    b = _repo(tmp_path, "b", required=["ghost"])
    out = resolve_dependencies([a, b])
    assert out.ordered == []
    msgs = [str(e) for e in out.errors]
    assert any("no project named 'ghost'" in m for m in msgs)
    prop = [m for m in msgs if "was skipped" in m]
    assert len(prop) == 1
    assert "'b'" in prop[0]
    assert "root cause" in prop[0]
    assert "ghost" in prop[0]


def test_propagation_is_dependency_only_not_import_errors(tmp_path):
    # covered again at bootstrap level; here: a healthy dep graph never skips
    a = _repo(tmp_path, "a", required=["b"])
    b = _repo(tmp_path, "b")
    out = resolve_dependencies([a, b])
    assert _names(out.ordered) == ["b", "a"]


def test_required_cycle_errors_and_skips_members(tmp_path):
    a = _repo(tmp_path, "a", required=["b"])
    b = _repo(tmp_path, "b", required=["a"])
    out = resolve_dependencies([a, b])
    assert out.ordered == []
    cyc = [str(e) for e in out.errors if "cycle" in str(e)]
    assert len(cyc) == 2
    assert any("a -> b -> a" in m or "b -> a -> b" in m for m in cyc)


def _requires_direction_rotations(names):
    """All rotations of *names* rendered as a closed ``"X -> Y -> ... -> X"`` cycle string."""
    rotations = []
    for start in range(len(names)):
        rotated = names[start:] + names[:start]
        rotations.append(" -> ".join([*rotated, rotated[0]]))
    return rotations


def test_three_node_cycle_renders_requires_direction(tmp_path):
    # a requires b, b requires c, c requires a: must read "a -> b -> c -> a"
    # (or a rotation thereof) under the natural "X -> Y means X requires Y"
    # reading -- NOT the reversed "a -> c -> b -> a" family.
    a = _repo(tmp_path, "a", required=["b"])
    b = _repo(tmp_path, "b", required=["c"])
    c = _repo(tmp_path, "c", required=["a"])
    out = resolve_dependencies([a, b, c])
    assert out.ordered == []
    cyc = [str(e) for e in out.errors if "required dependency cycle:" in str(e)]
    assert len(cyc) == 3

    forward = _requires_direction_rotations(["a", "b", "c"])
    backward = _requires_direction_rotations(["a", "c", "b"])
    for msg in cyc:
        assert any(r in msg for r in forward), msg
        assert not any(r in msg for r in backward), msg


def test_downstream_of_cycle_skipped_with_pointer(tmp_path):
    a = _repo(tmp_path, "a", required=["b"])
    b = _repo(tmp_path, "b", required=["a"])
    c = _repo(tmp_path, "c", required=["a"])
    out = resolve_dependencies([a, b, c])
    assert out.ordered == []
    downstream = [str(e) for e in out.errors if "part of a dependency cycle" in str(e)]
    assert len(downstream) == 1
    assert str(c.sut_dir) in downstream[0]


def test_soft_edge_orders_optional_provider_first(tmp_path):
    b = _repo(tmp_path, "b", optional=["a >= 1"])
    a = _repo(tmp_path, "a")
    out = resolve_dependencies([b, a])
    assert _names(out.ordered) == ["a", "b"]


def test_soft_edge_dropped_on_cycle_no_error(tmp_path):
    # required a->b plus optional b->a: the soft edge would close a cycle — dropped
    a = _repo(tmp_path, "a", required=["b"])
    b = _repo(tmp_path, "b", optional=["a"])
    out = resolve_dependencies([a, b])
    assert _names(out.ordered) == ["b", "a"]  # required edge wins
    assert out.errors == []


def test_absent_optional_contributes_no_edge(tmp_path):
    b = _repo(tmp_path, "b", optional=["ghost"])
    a = _repo(tmp_path, "a")
    out = resolve_dependencies([b, a])
    assert _names(out.ordered) == ["b", "a"]  # sut-dir order preserved


def test_satisfied_optional_warns_when_provider_skipped(tmp_path):
    # d optionally depends on e (present, compatible -> soft edge, "satisfied").
    # e requires a missing "ghost" -> e is skipped. d still registers (its own
    # deps are fine), but the feature it thinks it has is a lie unless we warn.
    d = _repo(tmp_path, "d", optional=["e >= 1"])
    e = _repo(tmp_path, "e", required=["ghost"])
    out = resolve_dependencies([d, e])
    assert _names(out.ordered) == ["d"]
    assert len(out.warnings) == 1
    (warn,) = out.warnings
    assert "e >= 1" in warn.message
    assert str(e.sut_dir) in warn.message
    assert "feature disabled" in warn.message
    # the stored status is unchanged -- it describes the discovered set, not
    # registration success.
    (dep,) = d.dependencies
    assert dep.status == "satisfied"
