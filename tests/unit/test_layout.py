"""The run-tree contract (spec 2026-09-16 §3): one table, both trees.

Kills: any drive-by reorganization of either tree, and any product name that
could escape its directory or collide with the host-level debug dir.
"""

from pathlib import Path

import pytest

from otto import layout

BASE = Path("/run")

# (function, args, expected relative path) — the whole contract in one table.
TEMPLATES = [
    (layout.host_logs_dir, ("h1",), "logs/h1"),
    (layout.host_debug_dir, ("h1",), "logs/h1/debug"),
    (layout.product_logs_dir, ("h1", "app"), "logs/h1/app/product"),
    (layout.product_debug_dir, ("h1", "app"), "logs/h1/app/debug"),
    (layout.cov_host_dir, ("h1",), "cov/h1"),
    (layout.cov_product_dir, ("h1", "app"), "cov/h1/app"),
]


@pytest.mark.parametrize(("fn", "args", "rel"), TEMPLATES, ids=lambda x: getattr(x, "__name__", x))
def test_templates(fn, args, rel):
    assert fn(BASE, *args) == BASE / rel


def test_run_dir_of_is_the_inverse_of_host_logs_dir():
    # The one path walked back UP the tree, and its only caller holds nothing
    # but a host log root. Kills: a builder that changes the depth of
    # logs/<host_id> without the inverse following it — every consumer that
    # recovers the run dir would then key its subtrees off the wrong directory.
    assert layout.run_dir_of(layout.host_logs_dir(BASE, "h1")) == BASE


def test_logs_and_cov_trees_share_the_host_product_shape():
    logs = layout.product_logs_dir(BASE, "h1", "app").parent.relative_to(BASE / "logs")
    cov = layout.cov_product_dir(BASE, "h1", "app").relative_to(BASE / "cov")
    assert logs == cov == Path("h1/app")


def test_cov_product_dir_in_is_the_same_leaf_from_an_already_resolved_cov_dir():
    # The fetchers hold the cov dir itself (`--cov-dir` is an arbitrary user
    # path, so `<run>/cov` cannot be recovered from it). Kills: the two
    # spellings drifting — a leaf reached from a run dir and one reached from
    # a cov dir must be the same directory, and both must validate the name.
    assert layout.cov_product_dir_in(BASE / "cov", "h1", "app") == layout.cov_product_dir(
        BASE, "h1", "app"
    )
    with pytest.raises(ValueError, match=r"product name"):
        layout.cov_product_dir_in(BASE / "cov", "h1", "debug")


def test_no_function_creates_directories(tmp_path):
    layout.product_logs_dir(tmp_path, "h1", "app")
    layout.cov_product_dir(tmp_path, "h1", "app")
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize(
    "bad",
    ["", ".", "..", "a/b", "a\\b", "nul\0", "debug"],
    ids=["empty", "dot", "dotdot", "slash", "backslash", "nul", "reserved-debug"],
)
def test_validate_product_name_rejects(bad):
    with pytest.raises(ValueError, match=r"product name"):
        layout.validate_product_name(bad)


@pytest.mark.parametrize("good", ["app", "my-app_2", "Agent.v1", "a b"])
def test_validate_product_name_accepts(good):
    layout.validate_product_name(good)


@pytest.mark.parametrize(
    "fn", [layout.product_logs_dir, layout.product_debug_dir, layout.cov_product_dir]
)
def test_product_path_builders_validate_the_name(fn):
    # The guard lives where the state lives: a bad name never reaches disk
    # even from a caller that skipped ingest.
    with pytest.raises(ValueError, match=r"product name"):
        fn(BASE, "h1", "debug")
