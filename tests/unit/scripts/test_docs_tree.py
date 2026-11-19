"""``scripts/docs_tree.py``: the one answer to which docs files are hand-written source."""

import subprocess
from pathlib import Path, PurePosixPath

import pytest

from scripts import docs_tree
from scripts.api_docs_blocks import teaching_corpus
from scripts.docs_tree import NON_SOURCE_DIRS, RENDERED, docs_sources, is_source
from tests._fixtures.gitrepo import git_env
from tests._fixtures.paths import CHECKOUT_ROOT

pytestmark = pytest.mark.interpreter_agnostic


def _tree(docs: Path, rels: list[str]) -> None:
    for rel in rels:
        path = docs / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("# page\n", encoding="utf-8")


def test_the_teaching_corpus_leaves_out_the_rendered_release_notes(tmp_path):
    """#1188: after a local `make docs`, the generated release notes are not teaching."""
    _tree(tmp_path / "docs", ["setup/page.md", "release-notes/0.20.0.md", "release-notes/index.md"])
    (tmp_path / "README.md").write_text("# readme\n", encoding="utf-8")

    rel = {p.relative_to(tmp_path).as_posix() for p in teaching_corpus(tmp_path)}

    assert "docs/setup/page.md" in rel
    assert not any(r.startswith("docs/release-notes/") for r in rel)


def test_docs_sources_skips_every_kind_of_non_source(tmp_path):
    kept = ["index.md", "setup/page.md", "cli/host/families.md", "notes.rst"]
    skipped = [
        "_build/html/page.md",
        "superpowers/specs/x-design.md",
        "_inventories/notes.md",
        *(r.output + "x.md" if r.output.endswith("/") else r.output for r in RENDERED),
    ]
    _tree(tmp_path, kept + skipped)

    got = {p.relative_to(tmp_path).as_posix() for p in docs_sources(tmp_path)}

    assert got == set(kept)


def test_docs_sources_takes_suffixes_and_checker_specific_skips(tmp_path):
    _tree(tmp_path, ["a.md", "b.rst", "examples/demo/README.md", "_static/x.md"])

    assert {p.name for p in docs_sources(tmp_path, suffixes=(".md",))} == {
        "a.md",
        "README.md",
        "x.md",
    }
    rel = {
        p.relative_to(tmp_path).as_posix()
        for p in docs_sources(tmp_path, also_skip=("examples/", "_static/"))
    }
    assert rel == {"a.md", "b.rst"}


@pytest.mark.parametrize("part", NON_SOURCE_DIRS)
def test_a_non_source_directory_is_skipped_at_any_depth(part):
    assert not is_source(f"{part}/page.md")
    assert not is_source(f"sub/{part}/page.md")


def test_a_skip_names_a_directory_not_a_name_prefix():
    assert is_source("examples-guide.md", also_skip=("examples/",))
    assert not is_source("examples/x.md", also_skip=("examples/",))


def test_git_ignores_every_rendered_output(tmp_path):
    """Each rendered output is git-ignored, as git itself reads `.gitignore`.

    One direction only: an ignored path the registry does not name is not a
    rendered page, so nothing here can ask the other way.
    """
    for entry in RENDERED:
        probe = f"docs/{entry.output}" + ("page.md" if entry.output.endswith("/") else "")
        ignored = subprocess.run(
            ["git", "-C", str(CHECKOUT_ROOT), "check-ignore", "-q", "--no-index", probe],
            check=False,
            env=git_env(tmp_path),
        )
        assert ignored.returncode == 0, (
            f"{probe} is rendered at build time but git does not ignore it"
        )


def test_a_rendered_asset_outside_the_page_suffixes_is_still_not_source():
    assert not is_source("_static/generated/python-badge.svg")


def test_a_skipped_directory_is_never_entered(tmp_path, monkeypatch):
    _tree(tmp_path, ["a.md", "_build/html/b.md", "superpowers/c.md", "release-notes/d.md"])
    seen: list[str] = []
    real = docs_tree._is_source

    def spy(path: PurePosixPath, skips: "tuple[str, ...]") -> bool:
        seen.append(path.as_posix())
        return real(path, skips)

    monkeypatch.setattr(docs_tree, "_is_source", spy)

    assert [p.name for p in docs_sources(tmp_path)] == ["a.md"]
    assert seen == ["a.md"]


def test_also_skip_may_be_any_iterable():
    assert not is_source("omit.md", also_skip=iter(["omit.md"]))
    assert not is_source("omit.md", also_skip=("omit.md",))


def test_a_bare_name_in_also_skip_is_refused():
    with pytest.raises(ValueError, match="a directory ends in '/'"):
        is_source("examples/x.md", also_skip=("examples",))


def test_each_renderer_writes_where_the_registry_says(tmp_path):
    """The registry's output for a renderer is the path that renderer writes."""
    from scripts import (
        render_kmodcov_matrix,
        render_module_graph,
        render_proven_range,
        render_python_badge,
        render_release_notes,
        render_support_matrix,
    )

    docs = CHECKOUT_ROOT / "docs"
    written = {
        "scripts.render_support_matrix": render_support_matrix.PAGE_PATH,
        "scripts.render_proven_range": render_proven_range.PAGE_PATH,
        "scripts.render_module_graph": render_module_graph.PAGE_PATH,
        "scripts.render_python_badge": render_python_badge.BADGE_PATH,
        "scripts.render_release_notes": render_release_notes.PAGES_DIR,
        "scripts.render_kmodcov_matrix": render_kmodcov_matrix.PAGE_PATH,
    }
    assert {r.module for r in RENDERED} == set(written)
    for entry in RENDERED:
        assert docs / entry.output.rstrip("/") == written[entry.module], entry.module
        # A directory entry is one the renderer writes many pages into: no suffix.
        assert entry.output.endswith("/") == (written[entry.module].suffix == ""), entry.module
