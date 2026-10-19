"""Every path an ast-grep rule names exists.

A rule scopes itself with ``files:`` and ``ignores:``. A glob there matches
whatever exists; a literal path names one file, and when that file moves, the
rule silently stops checking it: ast-grep reports nothing for a path that
matches nothing, and ``ast-grep test`` never applies ``files:`` at all. When
spec 2026-10-11 placement-and-phases §3.7 moved the lab model and loader into
``otto.labs``, ``backend-construction-through-entry`` still named the file they
left. This test fails on any literal path, in any rule, that names nothing, so a
move re-points the rule in the same commit.
"""

from pathlib import Path

import pytest
import yaml

from tests._fixtures.paths import CHECKOUT_ROOT

pytestmark = pytest.mark.interpreter_agnostic

_GLOB = frozenset("*?[{")


def literal_paths(rule: dict) -> "list[str]":
    """The entries of *rule*'s ``files`` and ``ignores`` that are paths, not globs."""
    return [
        entry
        for key in ("files", "ignores")
        for entry in rule.get(key) or []
        if not _GLOB & set(entry)
    ]


def all_literal_paths(root: Path) -> "list[str]":
    """``<rule file>: <path>`` for each literal path a rule under *root* names."""
    return [
        f"{path.name}: {entry}"
        for path in sorted((root / ".ast-grep" / "rules").glob("*.yml"))
        for entry in literal_paths(yaml.safe_load(path.read_text(encoding="utf-8")))
    ]


def missing_paths(root: Path) -> "list[str]":
    """``<rule file>: <path>`` for each literal path a rule under *root* names that is missing."""
    return [
        label for label in all_literal_paths(root) if not (root / label.split(": ", 1)[1]).exists()
    ]


def test_every_literal_path_a_rule_names_exists() -> None:
    missing = missing_paths(CHECKOUT_ROOT)
    assert not missing, (
        "these ast-grep rules name a path that does not exist, so they no longer check "
        "it; re-point each to the file's new home:\n  " + "\n  ".join(missing)
    )


def test_the_scan_reads_paths_and_skips_globs(tmp_path: Path) -> None:
    rules = tmp_path / ".ast-grep" / "rules"
    rules.mkdir(parents=True)
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "here.py").write_text("")
    (rules / "r.yml").write_text(
        "id: r\nfiles:\n- src/here.py\n- src/gone.py\n- src/**\n"
        "ignores:\n- src/old.py\n- src/*.txt\n"
    )
    (rules / "s.yml").write_text("id: s\n")
    assert missing_paths(tmp_path) == ["r.yml: src/gone.py", "r.yml: src/old.py"]


def test_the_rules_name_the_moved_loader_by_path() -> None:
    """Anti-vacuity: the tree's rules name files by path, the lab loader among them."""
    named = all_literal_paths(CHECKOUT_ROOT)
    assert any(label.endswith(": src/otto/labs/loader.py") for label in named)
    # Sits below today's count only so that a scan that finds nothing fails.
    assert len(named) >= 10, named
