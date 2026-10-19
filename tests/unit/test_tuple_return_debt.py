"""Ratchet for `.ast-grep/rules/no-tuple-return.yml`'s baseline.

The ast-grep rule fails a NEW public tuple return. This is the other half: the
baseline it was landed with can only SHRINK. Three ways to fail:

* a site exists that is not enumerated here (the rule catches it too, but this
  says which one and why it matters);
* a site is enumerated here but no longer exists — someone converted it and left
  the entry, so the list stops describing the tree. This is the anti-vacuity
  half, and it is the one a count-based ratchet cannot do;
* a site exists without its inline ``# ast-grep-ignore`` comment, or an ignore
  comment exists with no site under it — either way the suppression and the
  thing suppressed have drifted apart.

Deleting an entry here without fixing the code fails the ast-grep rule, and
fixing the code without deleting the entry fails this test, so the two gates
pin each other.

Background: docs/architecture/quality-gates.md and
todo/churn-review-remaining-work-2026-08-05.md.
"""

import ast
import functools
import os
import re
import sys
from typing import NamedTuple

import pytest

from tests._fixtures.paths import CHECKOUT_ROOT

pytestmark = pytest.mark.interpreter_agnostic

_SRC = CHECKOUT_ROOT / "src" / "otto"
_OUTERMOST_TUPLE = re.compile(r"^(tuple|Tuple)\[")
_IGNORE = "# ast-grep-ignore: no-tuple-return"

# (module path relative to src/otto, function name) -> why it is still here.
# PERMANENT entries are contracts otto does not own and are not expected to shrink.
# DEBT entries name their fix: a small class with named fields (a frozen
# dataclass or a NamedTuple, the latter where the value must stay hashable or
# ordered), or a list for a collection of similar items. Delete the entry with
# the fix.
PERMANENT: dict[tuple[str, str], str] = {
    ("monitor/db.py", "event_insert_params"): "sqlite DB-API parameter row — the driver's contract",
    ("suite/plugin.py", "pytest_report_teststatus"): "pytest dictates this hook's return shape",
}

DEBT: dict[tuple[str, str], str] = {
    ("config/version.py", "key"): "sort key (major, minor, patch) — a NamedTuple keeps the order",
    ("coverage/attribution.py", "attribute_tickets"): "three unrelated maps",
    ("coverage/store/model.py", "branch_id"): "composite dict key — a hashable NamedTuple",
    ("host/builtin_hosts.py", "builtin_host_ids"): "similar ids iterated as a group — a list",
    ("link/params.py", "canonical_key"): "seven named slots — a hashable NamedTuple",
    ("host/binary_loader.py", "check_loaded"): "(ok, detail) — a Result in disguise",
    ("host/connections.py", "credentials"): "(user, password) pair",
    ("host/login_proxy.py", "resolve_chain"): "target credential plus hop chain",
    ("link/derive.py", "addressing_from_dict"): "resolved id plus addressing",
    ("link/sentinel.py", "parse_impair_sentinel"): "three parsed fields",
    ("monitor/event_ops.py", "resolve_create"): "(start, end) pair",
    ("tunnel/discovery.py", "scan"): "observations plus an error string",
    ("tunnel/discovery.py", "discover_observations"): "observations plus error list",
}

BASELINE = PERMANENT | DEBT


class _Site(NamedTuple):
    """One public def whose outermost return annotation is a tuple."""

    name: str
    lineno: int


def _sites() -> dict[tuple[str, str], list[int]]:
    """Every public function in src/otto whose OUTERMOST return type is a tuple.

    Every file is read on every call; each distinct file content is scanned
    once (:func:`_file_sites`), so an edit is always seen and every test
    that asks does not re-parse the whole tree again.
    """
    found: dict[tuple[str, str], list[int]] = {}
    for path in sorted(_SRC.rglob("*.py")):
        rel = str(path.relative_to(_SRC))
        for name, lineno in _file_sites(path.read_text()):
            found.setdefault((rel, name), []).append(lineno)
    return found


@functools.cache
def _file_sites(source: str) -> "list[_Site]":
    """The public tuple-returning defs in *source*, in ``ast.walk`` order; shared, so read only."""
    sites: list[_Site] = []
    for node in ast.walk(ast.parse(source)):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        if node.name.startswith("_") or node.returns is None:
            continue
        annotation = ast.unparse(node.returns).strip().strip("\"'")
        if _OUTERMOST_TUPLE.match(annotation):
            sites.append(_Site(node.name, node.lineno))
    return sites


def test_no_unbaselined_tuple_returns():
    """A new public tuple return must be converted, not added to the baseline."""
    extra = sorted(set(_sites()) - set(BASELINE))
    assert not extra, (
        f"New public tuple return(s): {extra}. otto's convention is a frozen dataclass "
        "or a Result-family value — see .ast-grep/rules/no-tuple-return.yml. "
        "Do not add an entry to the baseline in this file to silence it."
    )


def test_baseline_has_no_stale_entries():
    """Fixing a site without deleting its entry leaves the list describing nothing."""
    stale = sorted(set(BASELINE) - set(_sites()))
    assert not stale, (
        f"Baselined site(s) no longer exist: {stale}. They were converted — delete "
        "the entry (and its inline # ast-grep-ignore comment) so the baseline keeps "
        "meaning what it says."
    )


@pytest.mark.parametrize("key", sorted(BASELINE), ids=lambda k: f"{k[0]}::{k[1]}")
def test_each_baselined_site_carries_its_suppression(key):
    """The ignore comment must sit immediately above each def, or the gate reddens."""
    lines = (_SRC / key[0]).read_text().split("\n")
    # `.get(key, [])`, not `[key]`: a stale baseline entry is already reported
    # by test_baseline_has_no_stale_entries, and a bare KeyError here would
    # bury that one good failure under a parametrized error per surviving
    # entry. A vanished site vacuously passes this guard, which is correct —
    # this one asks "is each live site suppressed", not "does each entry live".
    for lineno in _sites().get(key, []):
        assert _IGNORE in lines[lineno - 2], (
            f"{key[0]}:{lineno} ({key[1]}) is baselined but its "
            f"'{_IGNORE}' comment is missing from the line directly above the def. "
            "For a decorated function the comment goes BETWEEN the decorator and "
            "the def — above the decorator does not suppress."
        )


def test_no_orphan_suppressions():
    """An ignore comment with nothing to suppress is a lie about the code below it."""
    live = {(k[0], line) for k, lines in _sites().items() for line in lines}
    paths = sorted(_SRC.rglob("*.py"))
    assert paths, f"fixture sanity: no Python files under {_SRC} — the orphan scan would be vacuous"
    for path in paths:
        rel = str(path.relative_to(_SRC))
        for index, line in enumerate(path.read_text().split("\n"), start=1):
            if _IGNORE in line:
                assert (rel, index + 1) in live, (
                    f"{rel}:{index} suppresses no-tuple-return but the function below "
                    "it does not return a bare tuple. Delete the comment."
                )


def test_the_scan_sees_an_edit_that_keeps_the_size_and_the_mtime(tmp_path, monkeypatch):
    """The scan is memoised per file TEXT, so no rewrite can be served stale sites.

    The rewrite keeps the byte count and is stamped back to the first write's
    mtime: exactly what a key on ``(mtime, size)``, or a cache on ``_sites``
    itself, cannot tell apart.
    """
    monkeypatch.setattr(sys.modules[__name__], "_SRC", tmp_path)
    path = tmp_path / "pairs.py"
    path.write_text("def pair() -> tuple[int, int]: ...\n")
    first = path.stat()
    assert _sites() == {("pairs.py", "pair"): [1]}
    path.write_text("def twin() -> tuple[int, int]: ...\n")
    os.utime(path, ns=(first.st_atime_ns, first.st_mtime_ns))
    assert (path.stat().st_size, path.stat().st_mtime_ns) == (first.st_size, first.st_mtime_ns)
    assert _sites() == {("pairs.py", "twin"): [1]}


def test_the_scan_parses_each_file_content_once(tmp_path, monkeypatch):
    """Eighteen tests ask for the sites; unchanged text is parsed once, not once per test."""
    monkeypatch.setattr(sys.modules[__name__], "_SRC", tmp_path)
    # The cache is process-wide, so the text is unique to this run.
    (tmp_path / "once.py").write_text(f"# {tmp_path}\ndef once() -> tuple[int, int]: ...\n")
    parses: list[str] = []
    real_parse = ast.parse

    def counting_parse(source, *args, **kwargs):
        parses.append(source)
        return real_parse(source, *args, **kwargs)

    monkeypatch.setattr(ast, "parse", counting_parse)
    assert _sites() == _sites() == {("once.py", "once"): [2]}
    assert len(parses) == 1
