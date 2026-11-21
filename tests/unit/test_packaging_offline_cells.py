"""The offline-install proof's platforms are one table, and the docs and nightly follow it.

``noxfile.py``'s ``OFFLINE_CELLS`` names, per platform, the ``pip download
--platform`` tags docs/installation.md's Step 1 uses and the uv target whose
binding ``scripts/check_jsonnet_markers.py`` pins. ``nox -s
packaging_offline`` runs on each of nightly's ``packaging-offline`` runners and
finds its own row. These tests keep the three in step: every cell's target has
a pinned binding, the docs' "Common platform tags" list is exactly the cells'
tag sets (so each set the docs give is one a nightly cell proves), and nightly
gives every cell a runner. Both sessions that install the built wheel select
it through one helper.
"""

import ast
import re

import pytest
import yaml

from scripts.check_jsonnet_markers import EXPECTED
from tests._fixtures.paths import CHECKOUT_ROOT

pytestmark = pytest.mark.interpreter_agnostic

NOXFILE = CHECKOUT_ROOT / "noxfile.py"
NIGHTLY = CHECKOUT_ROOT / ".github" / "workflows" / "nightly.yml"
INSTALLATION = CHECKOUT_ROOT / "docs" / "installation.md"


def _tree() -> ast.Module:
    return ast.parse(NOXFILE.read_text(encoding="utf-8"))


def _cells() -> "list[dict[str, object]]":
    """Each ``OfflineCell(...)`` row of ``OFFLINE_CELLS``, as its keyword values."""
    (value,) = [
        node.value
        for node in _tree().body
        if isinstance(node, ast.AnnAssign)
        and isinstance(node.target, ast.Name)
        and node.target.id == "OFFLINE_CELLS"
    ]
    assert isinstance(value, ast.Tuple)
    rows = []
    for call in value.elts:
        assert isinstance(call, ast.Call)
        rows.append({kw.arg: ast.literal_eval(kw.value) for kw in call.keywords})
    return rows


def _docs_tag_sets() -> "list[tuple[str, ...]]":
    """The ``--platform`` sets of the "Common platform tags" list, one per bullet."""
    text = INSTALLATION.read_text(encoding="utf-8")
    section = text.split("Common platform tags", 1)[1].split("\n\n", 2)[1]
    bullets = re.split(r"\n(?=- )", section.strip())
    return [tuple(re.findall(r"--platform ([a-z0-9_]+)", bullet)) for bullet in bullets]


def test_every_cell_target_has_a_pinned_binding():
    for cell in _cells():
        assert cell["target"] in EXPECTED, cell


def test_every_host_is_at_most_one_cell():
    hosts = [(c["sys_platform"], c["machine"], c["musl"]) for c in _cells()]
    assert len(hosts) == len(set(hosts)), hosts


def test_the_docs_platform_tags_are_the_cells_tag_sets():
    assert sorted(_docs_tag_sets()) == sorted(c["platforms"] for c in _cells())


def test_the_download_recipes_use_the_x86_64_cells_tags():
    (glibc,) = [
        c
        for c in _cells()
        if c["machine"] == "x86_64" and c["sys_platform"] == "linux" and not c["musl"]
    ]
    flags = " ".join(f"--platform {tag}" for tag in glibc["platforms"])
    assert INSTALLATION.read_text(encoding="utf-8").count(flags) >= 2, flags


def test_nightly_gives_every_cell_a_runner():
    jobs = yaml.safe_load(NIGHTLY.read_text(encoding="utf-8"))["jobs"]
    runners = jobs["packaging-offline"]["strategy"]["matrix"]["runner"]
    musl = jobs["packaging-offline-musl"]
    assert re.fullmatch(r"python:\$\{\{ matrix\.python \}\}-alpine", musl["container"])
    assert len(runners) == len(set(runners))
    assert len(runners) + 1 == len(_cells()), runners


def test_both_wheel_installing_sessions_select_the_wheel_through_one_helper():
    functions = {n.name: n for n in _tree().body if isinstance(n, ast.FunctionDef)}
    for name in ("_smoke_wheel", "packaging_offline"):
        called = {
            node.func.id
            for node in ast.walk(functions[name])
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
        }
        assert "_built_wheel" in called, name
