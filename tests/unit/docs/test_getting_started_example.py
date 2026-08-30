"""The Getting Started example project is a real otto project (spec 2026-08-27 §3, §4 tier 1).

Everything the pages include is a fragment of these files, so they must load
through the same code every user's project loads through: ``Repo`` parses the
settings, ``load_lab`` builds every lab, and a stray key fails here before it
can fail for a reader.
"""

import ast
import sys
from pathlib import Path

import pytest

from otto.config.lab import load_lab
from otto.config.repo import Repo
from otto.inventory import compile_inventory, construct_inventory
from otto.models.settings import InventoryConfigSpec
from otto.testing import assert_reservation_backend_conforms
from tests._fixtures.paths import PROJECT_ROOT

EXAMPLE = PROJECT_ROOT / "docs" / "examples" / "getting-started"
TWIN = PROJECT_ROOT / "docs" / "examples" / "getting-started-inventory"
_LIBS = EXAMPLE / "libs"
_MIRROR_SOURCE = PROJECT_ROOT / "tests" / "custom_hosts" / "custom_hosts" / "zephyr_inline.py"
_UNIX = ["test1", "test2", "test3"]
_ATTRS = [
    "id",
    "ip",
    "element",
    "element_id",
    "is_virtual",
    "site",
    "rack",
    "shelf",
    "docker_capable",
    "valid_terms",
    "valid_transfers",
    "source_lab",
    "element_metadata",
    "lab_info",
    "hop",
    "resources",
    "element_resources",
]


def _import_gs_example() -> None:
    # The example's init module registers the ``zephyr-inline`` frame the
    # embedded lab needs; idempotent, so it coexists with ``custom_hosts``.
    if str(_LIBS) not in sys.path:
        sys.path.insert(0, str(_LIBS))
    __import__("gs_example")


def load_example_lab(labs: str):
    _import_gs_example()
    return load_lab(labs, search_paths=[EXAMPLE / "lab_data"])


def test_settings_parse_as_a_real_repo() -> None:
    repo = Repo(sut_dir=EXAMPLE)
    assert repo.name == "gs"
    assert [s.backend for s in repo.lab_sources] == ["json"]


@pytest.mark.parametrize("lab", ["unix", "busybox", "embedded"])
def test_every_lab_loads(lab: str) -> None:
    built = load_example_lab(lab)
    assert built.hosts, lab


def test_the_bed_is_all_there() -> None:
    # otto's `slug()` (src/otto/host/remote_host.py) replaces every run of
    # non-[a-z0-9] characters -- including `_` -- with a single `-` when
    # composing an id from an element name; a locked stability contract, so
    # these element names (copied verbatim from the bed fixture) slug to
    # hyphenated ids below even though the source data spells them with `_`.
    ids = set(load_example_lab("unix+busybox+embedded").hosts) - {"local"}
    assert ids == {
        "test1",
        "test2",
        "test3",
        "test4",
        "zephyr37-fat",
        "zephyr37-lfs",
        "zephyr37-nofs",
        "zephyr27-fat",
        "zephyr44-lfs",
        "zephyr37-llext",
        "zephyr44-llext",
        "bb1161_qemu",
        "bb1211_qemu",
        "bb1281_qemu",
        "bb1310_qemu",
        "bb1350_qemu",
    }


def _drop_leading_docstring(body: list[ast.stmt]) -> list[ast.stmt]:
    """Drop *body*'s leading ``Expr(Constant(str))`` docstring statement, if any."""
    first = body[0] if body else None
    if (
        isinstance(first, ast.Expr)
        and isinstance(first.value, ast.Constant)
        and isinstance(first.value.value, str)
    ):
        return body[1:]
    return body


def _strip_docstrings(node: ast.ClassDef) -> ast.ClassDef:
    """Drop the class docstring and every method's docstring from *node*, in place.

    The guard compares behaviour, not prose: prose (docstrings) may differ
    between the mirror and the copy; behaviour may not.
    """
    node.body = _drop_leading_docstring(node.body)
    for child in node.body:
        if isinstance(child, ast.FunctionDef | ast.AsyncFunctionDef):
            child.body = _drop_leading_docstring(child.body)
    return node


def _frame_nodes(path: Path) -> list[str]:
    tree = ast.parse(path.read_text())
    keep = []
    for node in tree.body:
        if isinstance(node, ast.ClassDef) and node.name == "ZephyrInlineRetcodeFrame":
            keep.append(ast.dump(_strip_docstrings(node), include_attributes=False))
        if isinstance(node, ast.Assign) and any(
            isinstance(t, ast.Name) and t.id == "_RETCODE_RE" for t in node.targets
        ):
            keep.append(ast.dump(node, include_attributes=False))
    assert len(keep) == 2, f"{path}: expected the class and _RETCODE_RE, found {len(keep)} nodes"
    return keep


def test_the_zephyr_27_frame_mirrors_the_bed_module() -> None:
    """Ruling 3: copyable example, zero drift -- compared by AST with
    docstrings stripped, so prose may differ; behaviour may not.
    """
    assert _frame_nodes(_LIBS / "gs_example" / "zephyr_inline.py") == _frame_nodes(_MIRROR_SOURCE)


@pytest.fixture
def twin_inventory():
    # The twin's `[inventory]` table is a per-project override (spec §8, the
    # same TOML the `.otto/settings.toml` file's own `name`/`version`/
    # `[[lab.sources]]` live in) -- read the way `otto.inventory.config.build_inventory`
    # reads any active repo's declaration, not `load_user_settings` (that model is
    # `extra="forbid"` for a standalone `~/.otto/settings.toml` and would reject
    # this file's other top-level keys).
    repo = Repo(sut_dir=TWIN)
    assert repo.inventory_settings
    cfg = InventoryConfigSpec.model_validate(repo.inventory_settings)
    return construct_inventory(compile_inventory(cfg, anchor_dir=TWIN, origin="example"))


def test_the_referenced_twin_builds_the_same_unix_lab(twin_inventory) -> None:
    """Spec §3: two states of one lab, allowed only because this proves them equal."""
    inline = load_example_lab("unix")
    referenced = load_lab("unix", search_paths=[TWIN / "lab_data"], inventory=twin_inventory)
    assert set(inline.hosts) == set(referenced.hosts)
    for name in _UNIX:
        a, b = inline.hosts[name], referenced.hosts[name]
        assert {k: getattr(a, k) for k in _ATTRS} == {k: getattr(b, k) for k in _ATTRS}, name
        # test1's third cred names a login proxy (the customizations page) --
        # project *code*, registered by this project's init module, not a
        # machine fact an inventory supplies. The twin declares no init module
        # on purpose (it is the inventory example, and a copy of it has to load
        # standalone), so its creds file cannot name a proxy that nothing there
        # registers. What the two states must still agree on to the letter is
        # every directly-loginable cred, which is what the inventory supplies.
        assert [(c.login, c.password) for c in a.creds if c.proxy is None] == [
            (c.login, c.password) for c in b.creds
        ]
        assert {k: (i.ip, i.subnet) for k, i in a.interfaces.items()} == {
            k: (i.ip, i.subnet) for k, i in b.interfaces.items()
        }, name
        assert b.inventory_ref.key == name
        assert a.inventory_ref.referenced is False


def test_the_example_reservation_backend_conforms() -> None:
    _import_gs_example()
    from gs_example.reservations import TeamFileBackend

    assert_reservation_backend_conforms(
        TeamFileBackend(repo_dir=EXAMPLE, path="team-reservations.txt"),
        known_user="chris",
        known_resources=["bb-bench", "bb1350-chassis", "bb1350-slot"],
    )
