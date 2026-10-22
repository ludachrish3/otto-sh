"""Every ``docs/...`` path otto's source names exists, and no otto docstring names one (#700).

Two arms:

1. Anywhere in ``src/`` and ``tests/`` (comments, docstrings, strings, any
   tracked text file), a ``docs/...`` path must name something git tracks: a
   file, a directory (``docs/x/``), or an extensionless page (``docs/x/y``
   for ``docs/x/y.md`` or ``docs/x/y/index.md``). A ``docs/x (Heading)``
   pointer is held to its page. Git, not the disk, is the authority: a
   gitignored file (an SDD plan under ``docs/superpowers/plans/``, a build
   product under ``docs/_build/``) never exists in a clean checkout, so a
   pointer at one is a dangling pointer for every other reader.
2. No docstring (bare string statement) anywhere in ``src/otto`` names a
   ``docs/...`` path. A docstring is read in ``help()``, an IDE hover, or the
   published API reference, where a source path means nothing: it links a
   user-facing page with ``:doc:`` / ``:ref:``, and a pointer at a spec or
   other source page goes in a ``#`` comment beside the object.
"""

import ast
import io
import re
import subprocess
import tempfile
import tokenize
from collections.abc import Iterator
from functools import cache

from tests._fixtures.gitrepo import git_env
from tests._fixtures.paths import CHECKOUT_ROOT

DOCS_PATH = re.compile(r"(?<![\w/.:-])docs/\w[\w.-]*(?:/\w[\w.-]*)*/?")
"""A path into the ``docs/`` tree, as source text spells one (no ``..`` prefix, no anchor)."""

ALLOWED: dict[str, str] = {
    "tests/unit/scripts/test_gate_scope.py: docs/b.md": (
        "a comment about the throwaway git repository the test builds, whose tree has a docs/b.md"
    ),
    "tests/unit/test_kmodcov_matrix.py: docs/_build/html/index.html": (
        "names the Sphinx build output (a Makefile target), which is never tracked"
    ),
    "tests/unit/test_lane_invariants.py: docs/_build/html/index.html": (
        "names the Sphinx build output (a Makefile target), which is never tracked"
    ),
    "tests/unit/test_support_matrix.py: docs/architecture/support-matrix.md": (
        "the page is generated at Sphinx builder-inited and gitignored; these lines describe that"
    ),
}
"""``"<file>: <path>"`` entries arm 1 may skip, each with the reason it may stay."""

_HASH_COMMENTED = {".toml", ".sh", ".yaml", ".yml", ".cfg", ".ini", ".conf", ".mk"}
"""Non-Python files whose ``#`` comments arm 1 reads under ``tests/``."""

_SLASH_COMMENTED = {".c", ".h", ".cpp", ".js", ".ts", ".tsx", ".css"}
"""Non-Python files whose ``//`` and ``/* */`` comment lines arm 1 reads under ``tests/``."""

_SRC_TEXT = {".py", ".md", ".txt", ".json", ".rst", ".html"} | _HASH_COMMENTED | _SLASH_COMMENTED
"""Every file type arm 1 reads whole under ``src/``: it all ships, so all of it is prose."""


@cache
def _tracked() -> frozenset[str]:
    with tempfile.TemporaryDirectory() as home:
        out = subprocess.run(
            ["git", "ls-files", "-z"],
            cwd=CHECKOUT_ROOT,
            env=git_env(home),
            capture_output=True,
            text=True,
            check=True,
        ).stdout
    return frozenset(p for p in out.split("\0") if p)


@cache
def _tracked_dirs() -> frozenset[str]:
    dirs = set()
    for name in _tracked():
        parts = name.split("/")
        dirs.update("/".join(parts[:i]) for i in range(1, len(parts)))
    return frozenset(dirs)


def exists(path: str) -> bool:
    """Return whether the ``docs/...`` *path* names something git tracks."""
    path = path.rstrip(".")  # a sentence's full stop
    bare = path.rstrip("/")
    if path.endswith("/"):
        return bare in _tracked_dirs()
    return (
        bare in _tracked()
        or bare in _tracked_dirs()
        or f"{bare}.md" in _tracked()
        or f"{bare}/index.md" in _tracked()
    )


_COMMENTED_DOCS = re.compile(r"#[^\n]*docs/")
"""A line where ``docs/`` follows a ``#``: only a file with one can have a comment naming a page."""


def _python_prose(text: str) -> list[tuple[int, str]]:
    """Return ``(line, text)`` for every comment and bare string statement (docstring) in *text*.

    A test's string constants are data (a fake docs tree, a path a script is
    fed); its pointers at real pages live in its comments and docstrings. A
    file that is deliberately not Python (a fixture with a syntax error) is
    read for its ``#`` lines. Tokenizing is the slow step, so it stops after
    the last line with a ``#`` before a ``docs/``, and a file with none skips it.
    """
    try:
        tree = ast.parse(text)
    except SyntaxError:
        return _comment_lines(text, ".toml")
    candidates = [m.start() for m in _COMMENTED_DOCS.finditer(text)]
    comments = []
    if candidates:
        last = text.count("\n", 0, candidates[-1]) + 1
        for tok in tokenize.generate_tokens(io.StringIO(text).readline):
            if tok.start[0] > last:
                break
            if tok.type == tokenize.COMMENT:
                comments.append((tok.start[0], tok.string))
    return comments + _bare_strings(tree)


def _statements(nodes: list) -> Iterator[ast.stmt]:
    """Yield every statement under *nodes*, through every nested block; expressions are skipped."""
    for node in nodes:
        if isinstance(node, ast.stmt):
            yield node
        for field in ("body", "orelse", "finalbody", "handlers", "cases"):
            children = getattr(node, field, None)
            if isinstance(children, list):
                yield from _statements(children)


def _bare_strings(tree: ast.Module) -> list[tuple[int, str]]:
    """Return ``(first line, text)`` for every bare string statement (docstring) in *tree*."""
    return [
        (node.lineno, node.value.value)
        for node in _statements(tree.body)
        if isinstance(node, ast.Expr)
        and isinstance(node.value, ast.Constant)
        and isinstance(node.value.value, str)
    ]


def _comment_lines(text: str, suffix: str) -> list[tuple[int, str]]:
    out = []
    for lineno, line in enumerate(text.splitlines(), 1):
        if suffix in _HASH_COMMENTED and "#" in line:
            out.append((lineno, line[line.index("#") :]))
        elif suffix in _SLASH_COMMENTED and line.lstrip().startswith(("//", "/*", "*")):
            out.append((lineno, line))
    return out


@cache
def _prose(rel: str) -> list[tuple[int, str]]:
    """Return the ``(line, text)`` pieces of tracked file *rel* that arm 1 holds to the rule.

    Cached: both arm-1 checks walk every tracked file, and a file that never
    spells ``docs/`` is skipped before it is tokenized and parsed.
    """
    path = CHECKOUT_ROOT / rel
    if rel.startswith("src/") and path.suffix not in _SRC_TEXT:
        return []
    text = path.read_text(errors="replace")
    if "docs/" not in text:
        return []
    if rel.startswith("src/"):
        return list(enumerate(text.splitlines(), 1))
    if path.suffix == ".py":
        return _python_prose(text)
    return _comment_lines(text, path.suffix)


def _source_files() -> list[str]:
    return [
        name
        for name in sorted(_tracked())
        if name.startswith(("src/", "tests/"))
        and name != "tests/unit/test_docs_paths_exist.py"
        and (CHECKOUT_ROOT / name).is_file()
    ]


def _arm1_offenders() -> list[str]:
    found = []
    for rel in _source_files():
        for lineno, text in _prose(rel):
            found.extend(
                f"{rel}:{at}: {hit}"
                for at, hit in _hits(DOCS_PATH, lineno, text)
                if not exists(hit) and f"{rel}: {hit}" not in ALLOWED
            )
    return found


WRAPPED = re.compile(DOCS_PATH.pattern + r"(?<=/)(?=[`'\"]*[ \t]*$)", re.MULTILINE)
"""A ``docs/`` path cut at a ``/`` by a line break: only its head is on this line.

The head is a directory that exists, so the existence check would pass a
path whose tail (the page) nobody checks.
"""


def _wrapped() -> list[str]:
    return [
        f"{rel}:{at}: {hit}"
        for rel in _source_files()
        for lineno, text in _prose(rel)
        for at, hit in _hits(WRAPPED, lineno, text)
    ]


# --- arm 2 -------------------------------------------------------------------


def _docstring_hits(text: str) -> list[tuple[int, str]]:
    """Return ``(line, path)`` for every ``docs/...`` path a bare string statement in *text* names.

    A ``#`` comment is the place for a pointer at a source page; a docstring
    is read in ``help()``, an IDE hover, or the published API reference, where
    a source path means nothing.
    """
    if "docs/" not in text:
        return []
    return [
        hit for line, doc in _bare_strings(ast.parse(text)) for hit in _hits(DOCS_PATH, line, doc)
    ]


def _arm2_offenders() -> list[str]:
    found = []
    for path in sorted((CHECKOUT_ROOT / "src" / "otto").rglob("*.py")):
        rel = path.relative_to(CHECKOUT_ROOT).as_posix()
        found += [f"{rel}:{at}: {hit}" for at, hit in _docstring_hits(path.read_text())]
    return found


def _hits(pattern: re.Pattern[str], line: int, text: str) -> list[tuple[int, str]]:
    """Return ``(line, match)`` for *pattern* in *text*, which starts at *line*."""
    return [(line + text.count("\n", 0, m.start()), m.group()) for m in pattern.finditer(text)]


# --- the tests ---------------------------------------------------------------


def test_every_docs_path_the_source_names_exists():
    assert _arm1_offenders() == [], (
        "these docs/ paths name nothing git tracks: a new page needs `git add`, "
        "a moved page needs the pointer updated"
    )


def test_no_docs_path_is_wrapped_across_lines():
    assert _wrapped() == [], (
        "a docs/ path ends a line at '/': if it wrapped, keep it on one line; "
        "if it names a directory, end it with punctuation or a word"
    )


def test_no_docstring_names_a_docs_source_path():
    assert _arm2_offenders() == [], (
        "a docstring names a docs/ source path: link with :doc:/:ref:, "
        "or move the path to a `#` comment beside the object"
    )


def test_arm_2_flags_a_docstring_and_ignores_a_comment():
    text = (
        '"""Module; see docs/a/b.md."""\n'
        "\n"
        "# Spec: docs/c/d.md\n"
        "def f():\n"
        '    """Line one.\n'
        "\n"
        "    See docs/e/f.md.\n"
        '    """\n'
    )
    assert _docstring_hits(text) == [(1, "docs/a/b.md."), (7, "docs/e/f.md.")]


def test_every_allowed_entry_is_still_needed():
    unused = []
    for entry in ALLOWED:
        rel, _, hit = entry.partition(": ")
        texts = [text for _, text in _prose(rel)] if (CHECKOUT_ROOT / rel).is_file() else []
        if not any(hit in DOCS_PATH.findall(text) for text in texts) or exists(hit):
            unused.append(entry)
    assert unused == []


def test_the_guard_sees_what_it_claims():
    assert "src/otto/host/userland.py" in _source_files()
    assert exists("docs/cli/link/check")  # extensionless page
    assert exists("docs/cli/reservation/")  # directory
    assert exists("docs/configuration/lab-config.md")
    assert not exists("docs/superpowers/plans/never-tracked.md")
    assert not exists("docs/no/such/page.md")
