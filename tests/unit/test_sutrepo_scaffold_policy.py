"""SUT-repo scaffold policy: one builder, no hand-rolled settings.toml writes
(review §7.5).

The defect class: the ``mkdir(.otto)`` → ``write settings.toml literal`` →
``Repo(sut_dir=…)`` dance was re-typed across the suite, so every new
settings field means dozens of edits and the copies drift (the triplicated
``_repo(...)`` grew three diverging dependency-section renderers).  The
scaffold must be ONE builder — ``tests/_fixtures/sutrepo.py::make_sut_repo``
(or ``touch_settings`` for the empty fingerprint stubs) — because copies
drop the newest field first.

Scan: any write whose PATH mentions ``settings.toml`` in suite code outside
the fixture module is an offender, unless the line carries a
``# sutrepo-exempt: <reason>`` marker — the sanctioned spelling for tests
whose SUBJECT is the raw file (malformed-TOML readers, in-place edits of a
product-scaffolded file).  A bare marker with no reason is an offence, and
so is a DEAD marker (one whose line is no longer an offence — it would
silently pre-exempt future regrowth on that line).

Arms:
- ``<expr>.write_text/.write_bytes/.touch()`` where the receiver's subtree
  contains a ``"settings.toml"`` (or ``"…/settings.toml"``) string
- ``open(<path>, "w"/"a"/…)`` and ``<path>.open("w"/…)`` on such a path
- the same four write shapes on a LOCAL NAME bound from such a path
  (``settings_file = otto_dir / "settings.toml"; settings_file.write_text``)
  — found live at adoption in test_init_scaffold.py after the first cut
  claimed this shape "grepped zero"; the claim was wrong, so it became an
  arm (a blind-spot enumeration inherits its omissions)

Blind spots, stated: paths built via the product's ``SETTINGS_FILENAME``
constant or other variable filenames (grepped zero in tests/ at adoption),
``shutil.copy*``/``os.rename`` moves (zero), names re-bound across
statements from non-literal sources, bindings the name-tracker's plain
``Assign``-to-``Name`` shape cannot see — annotated (``x: Path = …``),
augmented, tuple-unpacked, walrus, attribute and loop targets (each grepped
zero at adoption, cross-checked by enumerating every ``settings.toml``
string constant in the suite AST), markers inside string literals (the
marker match is line-text, not AST), and product-side writes (``otto init``
runs are the product writing, not the suite — deliberately out of scope).
Marker semantics are LINE-scoped like noqa: one reasoned marker exempts
every offence on its physical line — all landed exempt sites are
single-offence multi-line calls, where only the call's first line works
(anywhere else goes loud as offence + dead marker).

Corpus: the house seven-tree denylist over ``tests/`` (parsed-or-fail), same
as test_gitenv_hermeticity.
"""

import ast
from pathlib import Path

import pytest

from tests._fixtures.paths import TESTS_ROOT

_FIXTURE_MODULE = TESTS_ROOT / "_fixtures" / "sutrepo.py"
_EXCLUDED_TREES = ("repo1", "repo2", "repo3", "repo4", "repo_broken", "repo_e2e", "firmware")
_EXEMPT_MARKER = "# sutrepo-exempt:"
_WRITE_ATTRS = ("write_text", "write_bytes", "touch")


def _parsed_or_fail(path: Path) -> ast.AST:
    try:
        return ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    except SyntaxError as e:
        pytest.fail(
            f"{path} is unparseable ({e}) — if it is corpus input data, add its "
            "top-level tree to _EXCLUDED_TREES; if it is suite code, fix it"
        )


def _suite_files() -> list[Path]:
    self_path = Path(__file__).resolve()
    files = [
        path
        for path in sorted(TESTS_ROOT.rglob("*.py"))
        if not any(part in _EXCLUDED_TREES for part in path.relative_to(TESTS_ROOT).parts)
        and path.resolve() != self_path
        and path != _FIXTURE_MODULE
    ]
    assert len(files) > 400, f"scan corpus collapsed: only {len(files)} files seen"
    return files


def _mentions_settings_toml(node: ast.AST) -> bool:
    return any(
        isinstance(sub, ast.Constant)
        and isinstance(sub.value, str)
        and (sub.value == "settings.toml" or sub.value.endswith("/settings.toml"))
        for sub in ast.walk(node)
    )


def _is_write_mode(node: ast.Call, arg_index: int) -> bool:
    mode = (
        node.args[arg_index]
        if len(node.args) > arg_index
        else next((kw.value for kw in node.keywords if kw.arg == "mode"), None)
    )
    return (
        isinstance(mode, ast.Constant)
        and isinstance(mode.value, str)
        and any(ch in mode.value for ch in "wax+")
    )


def _settings_bound_names(tree: ast.AST) -> set[str]:
    """Local names assigned from an expression that mentions settings.toml."""
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign) and _mentions_settings_toml(node.value):
            names.update(t.id for t in node.targets if isinstance(t, ast.Name))
    return names


def _scaffold_offences(tree: ast.AST) -> list[int]:
    bound = _settings_bound_names(tree)

    def targets_settings(receiver: ast.AST) -> bool:
        if _mentions_settings_toml(receiver):
            return True
        return isinstance(receiver, ast.Name) and receiver.id in bound

    hits: list[int] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if isinstance(func, ast.Attribute) and func.attr in _WRITE_ATTRS:
            if targets_settings(func.value):
                hits.append(node.lineno)
        elif isinstance(func, ast.Attribute) and func.attr == "open":
            if targets_settings(func.value) and _is_write_mode(node, 0):
                hits.append(node.lineno)
        elif (
            isinstance(func, ast.Name)
            and func.id == "open"
            and node.args
            and targets_settings(node.args[0])
            and _is_write_mode(node, 1)
        ):
            hits.append(node.lineno)
    return sorted(set(hits))


def _exempt_lines(path: Path) -> tuple[set[int], list[int]]:
    """(exempted line numbers, marker lines whose reason is missing)."""
    exempt: set[int] = set()
    bare: list[int] = []
    for i, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        if _EXEMPT_MARKER in line:
            reason = line.split(_EXEMPT_MARKER, 1)[1].strip()
            if reason:
                exempt.add(i)
            else:
                bare.append(i)
    return exempt, bare


def _file_offenders(path: Path, tree: ast.AST, rel: str) -> list[str]:
    hits = _scaffold_offences(tree)
    exempt, bare = _exempt_lines(path)
    out = [f"{rel}:{line} (marker without reason)" for line in bare]
    out.extend(
        f"{rel}:{line} (dead marker — line is no longer an offence)"
        for line in sorted(exempt)
        if line not in hits
    )
    out.extend(f"{rel}:{line}" for line in hits if line not in exempt)
    return out


def test_no_hand_rolled_sut_scaffolds_outside_the_fixture():
    offenders: list[str] = []
    for path in _suite_files():
        tree = _parsed_or_fail(path)
        offenders.extend(_file_offenders(path, tree, str(path.relative_to(TESTS_ROOT))))
    assert offenders == [], (
        f"{len(offenders)} hand-rolled settings.toml write(s) outside "
        "tests/_fixtures/sutrepo.py — scaffold via make_sut_repo(...) or "
        "touch_settings(...), or mark a writer-under-test line with "
        "'# sutrepo-exempt: <reason>':\n  " + "\n  ".join(offenders)
    )


def test_fixture_module_imports_nothing_from_otto():
    """Stated invariant of sutrepo.py: fixture import order must never
    entangle product import-time behavior."""
    tree = ast.parse(_FIXTURE_MODULE.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            assert not any(a.name.split(".")[0] == "otto" for a in node.names)
        elif isinstance(node, ast.ImportFrom):
            assert (node.module or "").split(".")[0] != "otto"


@pytest.mark.parametrize(
    ("snippet", "expected_lines"),
    [
        ('(sut / ".otto" / "settings.toml").write_text("x")\n', [1]),
        ('(root / "sub/.otto/settings.toml").write_text(t)\n', [1]),
        ('p.joinpath(".otto", "settings.toml").write_bytes(b)\n', [1]),
        ('(d / "settings.toml").touch()\n', [1]),
        ('open(d / "settings.toml", "w")\n', [1]),
        ('open(d / "settings.toml", mode="a")\n', [1]),
        ('(d / "settings.toml").open("w")\n', [1]),
        # The local-binding shape found live at adoption (test_init_scaffold).
        ('f = d / "settings.toml"\nf.write_text(t)\n', [2]),
        ('f = d / ".otto" / "settings.toml"\nf.touch()\nopen(f, "w")\n', [2, 3]),
        # Negative controls: reads, non-settings writes, unbound names,
        # subprocess runs.
        ('(sut / ".otto" / "settings.toml").read_text()\n', []),
        ('open(d / "settings.toml")\n', []),
        ('open(d / "settings.toml", "r")\n', []),
        ('(d / "settings.toml").open()\n', []),
        ('(sut / "lab.json").write_text("x")\n', []),
        ('(d / "lab.json").touch()\n', []),
        ('g = d / "lab.json"\ng.write_text(t)\n', []),
        ('subprocess.run(["otto", "init"], cwd=d)\n', []),
    ],
)
def test_scanner_positive_and_negative_controls(snippet, expected_lines):
    assert _scaffold_offences(ast.parse(snippet)) == expected_lines


def test_exempt_marker_rules(tmp_path):
    """The marker mechanism's own controls, through the SAME composition the
    main scan uses: a reasoned marker on an offence line exempts it; a bare
    marker is flagged; a reasoned marker on a NON-offence line is flagged as
    dead (it would silently pre-exempt future regrowth on its line)."""
    live = tmp_path / "live.py"
    live.write_text('(d / "settings.toml").write_text("x")  # sutrepo-exempt: writer under test\n')
    assert _file_offenders(live, ast.parse(live.read_text()), "live.py") == []

    bare = tmp_path / "bare.py"
    bare.write_text('(d / "settings.toml").write_text("x")  # sutrepo-exempt:\n')
    assert _file_offenders(bare, ast.parse(bare.read_text()), "bare.py") == [
        "bare.py:1 (marker without reason)",
        "bare.py:1",
    ]

    dead = tmp_path / "dead.py"
    dead.write_text("x = 1  # sutrepo-exempt: stale reason\n")
    assert _file_offenders(dead, ast.parse(dead.read_text()), "dead.py") == [
        "dead.py:1 (dead marker — line is no longer an offence)",
    ]
