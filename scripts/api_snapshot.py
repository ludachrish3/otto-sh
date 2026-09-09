#!/usr/bin/env python3
"""Commit otto's public API surface as a golden snapshot.

The surface is three kinds of line:

  * every name in ``otto.__all__`` (``otto:<name>``) — the PEP 562 lazy-export
    table in ``otto/__init__.py``;
  * every deep import the USER DOCS teach, written ``<module>:<name>`` (a bare
    ``import <module>`` contributes ``<module>:`` with no name) — each
    ``from otto.a.b import Y`` or ``import otto.x`` inside a fenced code
    example, doctest block, or RST literal block under ``docs/`` (excluding
    ``docs/superpowers/``, which is archived specs/plans that intentionally
    contain dead paths, and ``docs/_build/``);
  * every public method the ``Host`` protocol (``src/otto/host/host.py``)
    declares, written ``otto.host.host:Host.<method>(<params>)`` with its
    keyword parameter names in signature order — a family-facing call shape
    that ``otto.__all__`` never sees, so it is pinned separately.

The docs are the single declaration of which deep paths are sanctioned: there
is no separate allowlist to keep in sync. That also means a stale or broken
documented import is a bug this script reports, never silently excludes.

A name added, removed, or renamed in ``otto.__all__`` — or a deep path a doc
page starts or stops teaching — is now a visible, reviewable diff in the
golden file, the same way ``scripts/import_budget.py`` makes an import-graph
change a reviewable diff instead of a silent drift.

Every line in the surface must RESOLVE: the module must import, and (for a
non-bare line) the name must exist on it. A documented import that no longer
resolves is reported with the doc file:line that teaches it — never skipped.

Usage:
    python scripts/api_snapshot.py            # print the current surface
    python scripts/api_snapshot.py --update   # regenerate the golden snapshot
    python scripts/api_snapshot.py --check    # compare against the golden; exit
                                               # non-zero on drift or a documented
                                               # import that fails to resolve
"""

import argparse
import ast
import difflib
import importlib
import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
DOCS_ROOT = REPO_ROOT / "docs"
GOLDEN_PATH = REPO_ROOT / "tests" / "unit" / "api_snapshot" / "public_api.txt"

# Archived specs/plans carry dead paths on purpose; the built site is an output.
SKIP_PARTS = ("_build", "superpowers")

# A fence delimiter is a run of 3+ backticks OR 3+ tildes (CommonMark allows
# either), optionally indented (a fence nested in a list item's continuation,
# e.g. docs/library/sessions.md:116). ``\s*`` mirrors
# scripts/lint_markdown_doctests.py:28 rather than requiring column 0.
FENCE = re.compile(r"^\s*(?P<fence>`{3,}|~{3,})(?P<info>.*)$")
CODE_FENCE_LANGS = {"python", "pycon", ""}
DOCTEST_FENCE_LANG = "{doctest}"
CODE_BLOCK_DIRECTIVE = "{code-block}"  # MyST: ```{code-block} python``` — lang is the 2nd token
# Only an explicit "show the syntax as prose" label makes a fence opaque; any
# other MyST directive (``{note}``, ``{tab-item}``, …) is a plain CONTAINER —
# its own body isn't code, but a real code fence nested inside it still is,
# and the fence stack below finds that fence regardless of relative nesting
# depth or backtick count (MyST's own convention needs the container to use
# MORE backticks than its contents, but a same-count nesting is handled too).
DISPLAY_LANGS = {"markdown", "md"}

# A candidate line, after stripping any doctest ">>> " prompt. `otto(?:\.\w+)*`
# (not `otto[\w.]*`) so `otto_something` can never match: there is no dot
# between "otto" and "_something", so the group can't consume it, and the
# whole pattern then fails at the required `\s+import` — a name that merely
# starts with "otto" is ignored rather than reported as a broken import.
IMPORT_LINE = re.compile(
    r"^(from\s+otto(?:\.\w+)*(?!\w)\s+import\s+.+|import\s+otto(?:\.\w+)*(?!\w)(?:\s+as\s+\w+)?)"
)


_INDENT_CODE_BLOCK = 4  # CommonMark: a blank line then 4+ spaces of indent opens a code block


def _indent(line: str) -> int:
    return len(line) - len(line.lstrip(" "))


def _fence_open(line: str) -> tuple[int, str, str] | None:
    """If *line* opens a fence, return (tick_count, fence_char, info_string)."""
    m = FENCE.match(line)
    if not m:
        return None
    fence = m.group("fence")
    return len(fence), fence[0], m.group("info").strip()


def _fence_closes(line: str, ticks: int, char: str) -> bool:
    """Return whether *line* closes a fence of *char*, >=*ticks* long, with no info string."""
    m = FENCE.match(line)
    if not m or m.group("info").strip():
        return False
    fence = m.group("fence")
    return fence[0] == char and len(fence) >= ticks


def _frame_lang(info: str) -> str:
    """Return the effective code language for a fence's info string.

    Unwraps MyST's ``{code-block} <lang>`` directive to its second token;
    everything else (``python``, ``pycon``, ``{doctest}``, a bare fence's
    ``""``, or a directive name like ``{note}``) is used as-is — the caller
    decides what counts as "code" from that value.
    """
    if not info:
        return ""
    tokens = info.split()
    if tokens[0] == CODE_BLOCK_DIRECTIVE:
        return tokens[1] if len(tokens) > 1 else ""
    return tokens[0]


def _extract_lines(body: list[tuple[int, str]], *, prompted: bool) -> list[tuple[int, str]]:
    r"""Reduce a code block's raw lines to (line_no, code) candidates.

    ``prompted`` blocks (```{doctest}``` fences) only run their ``>>> ``
    lines — everything else is expected output. Non-prompted blocks run
    every line, but still strip a ``>>> ``/``... `` prompt if one is present
    (some plain ```python``` examples show a REPL session too).

    A candidate line that opens more parens than it closes — a parenthesized
    multi-line import, ``from otto.x import (\n    Y,\n)`` — is joined with
    its continuation lines (a doctest's ``... `` stripped, a plain fence's
    taken bare) into one multi-line statement before ``ast.parse`` ever sees
    it; a lone physical line would raise "'(' was never closed".
    """
    out: list[tuple[int, str]] = []
    i, n = 0, len(body)
    while i < n:
        line_no, text = body[i]
        stripped = text.strip()
        if prompted:
            if not stripped.startswith(">>> "):
                i += 1
                continue
            stripped = stripped[4:]
        elif stripped.startswith(">>> "):
            stripped = stripped[4:]
        elif stripped.startswith("... "):
            i += 1
            continue
        i += 1
        if IMPORT_LINE.match(stripped):
            while stripped.count("(") > stripped.count(")") and i < n:
                cont = body[i][1].strip()
                if cont.startswith(">>> "):
                    break  # a new prompt starts a new statement instead
                cont = cont.removeprefix("... ")
                stripped += "\n" + cont
                i += 1
        out.append((line_no, stripped))
    return out


def _markdown_candidates(path: Path) -> list[tuple[int, str]]:
    """Candidate (line_no, code) pairs from a Markdown file's code examples.

    Three shapes: fenced blocks (backtick or tilde; ```python```/```pycon```/
    plain/```{doctest}```/MyST ```{code-block} python```, possibly nested in
    a MyST container directive or a list item's indented continuation),
    CommonMark's blank-line-then-4-space-indent code blocks, and — inside a
    fence explicitly labelled ``markdown``/``md`` — nothing at all: that
    fence exists to show fence *syntax* as prose (see
    ``docs/contributing.md``'s doctest-fence example), so it and everything
    textually inside it, however it's nested, stays unscanned.

    Nesting is tracked with an explicit stack rather than one open/close
    pair: a fence line WITH an info string is always a new, independent
    open (even at the same tick count as its enclosing fence — MyST only
    asks for *more* backticks on the container, it doesn't require it), and
    a bare fence line with no info string closes the innermost fence it's
    long/thick enough for. That is enough to find a real ```` ```python ````
    block correctly however many ``{note}``/``{tab-item}``/... containers it
    sits inside.
    """
    lines = path.read_text(encoding="utf-8").splitlines()
    out: list[tuple[int, str]] = []
    i, n = 0, len(lines)
    prev_blank = True
    stack: list[dict] = []  # innermost frame last: {ticks, char, lang, suppressed, body}
    while i < n:
        line = lines[i]
        if stack:
            top = stack[-1]
            if _fence_closes(line, top["ticks"], top["char"]):
                frame = stack.pop()
                lang = frame["lang"]
                if not frame["suppressed"] and lang in (*CODE_FENCE_LANGS, DOCTEST_FENCE_LANG):
                    out.extend(_extract_lines(frame["body"], prompted=lang == DOCTEST_FENCE_LANG))
                i += 1
                prev_blank = False
                continue
            opened = _fence_open(line)
            if opened is not None:
                ticks, char, info = opened
                lang = _frame_lang(info)
                stack.append(
                    {
                        "ticks": ticks,
                        "char": char,
                        "lang": lang,
                        "suppressed": top["suppressed"] or lang in DISPLAY_LANGS,
                        "body": [],
                    }
                )
                i += 1
                prev_blank = False
                continue
            top["body"].append((i + 1, line))
            prev_blank = False
            i += 1
            continue
        opened = _fence_open(line)
        if opened is not None:
            ticks, char, info = opened
            lang = _frame_lang(info)
            stack.append(
                {
                    "ticks": ticks,
                    "char": char,
                    "lang": lang,
                    "suppressed": lang in DISPLAY_LANGS,
                    "body": [],
                }
            )
            i += 1
            prev_blank = False
            continue
        if prev_blank and line.strip() and _indent(line) >= _INDENT_CODE_BLOCK:
            body = []
            while i < n and (not lines[i].strip() or _indent(lines[i]) >= _INDENT_CODE_BLOCK):
                if lines[i].strip():
                    body.append((i + 1, lines[i]))
                i += 1
            out.extend(_extract_lines(body, prompted=False))
            prev_blank = True
            continue
        prev_blank = not line.strip()
        i += 1
    return out


def _rst_candidates(path: Path) -> list[tuple[int, str]]:
    """Candidate (line_no, code) pairs from an RST file's code examples.

    Two shapes: an explicit ``.. code-block:: python`` directive, and RST's
    ``::``-then-indented-paragraph literal block shorthand — both are an
    indented body following a line at some base indent, so both are handled
    by the same "consume while blank-or-more-indented" scan.
    """
    lines = path.read_text(encoding="utf-8").splitlines()
    out: list[tuple[int, str]] = []
    i, n = 0, len(lines)
    while i < n:
        line = lines[i]
        stripped = line.rstrip()
        code_block = re.match(r"^(\s*)\.\.\s+code-block::\s*(\S*)\s*$", stripped)
        literal_marker = stripped.endswith("::") and not code_block
        if not (code_block or literal_marker):
            i += 1
            continue
        base_indent = _indent(line)
        i += 1
        while i < n and not lines[i].strip():
            i += 1
        if i >= n or _indent(lines[i]) <= base_indent:
            continue  # the "::"/directive announced no indented body — not a code block
        block_indent = _indent(lines[i])
        body = []
        while i < n and (not lines[i].strip() or _indent(lines[i]) >= block_indent):
            if lines[i].strip():
                body.append((i + 1, lines[i]))
            i += 1
        out.extend(_extract_lines(body, prompted=False))
    return out


def _parse_statement(code: str) -> ast.Import | ast.ImportFrom:
    """Parse one candidate line as a single import statement, or raise."""
    tree = ast.parse(code.strip(), mode="exec")
    if len(tree.body) != 1 or not isinstance(tree.body[0], (ast.Import, ast.ImportFrom)):
        raise ValueError("not a single import statement")
    return tree.body[0]  # type: ignore[return-value]


def _module_name_pairs(node: ast.Import | ast.ImportFrom) -> list[tuple[str, str]]:
    """Return the (module, name) pairs *node* contributes; name is "" for a bare `import module`."""
    if isinstance(node, ast.ImportFrom):
        module = node.module or ""
        return [(module, alias.name) for alias in node.names]
    return [(alias.name, "") for alias in node.names]


def _resolve(module: str, name: str) -> str | None:
    """Import *module* and getattr *name* off it; return an error message, or None.

    *name* may be a dotted attribute chain (``Host.put``) and may carry a
    trailing ``(...)`` parameter list — a :func:`host_protocol_lines` line's
    ``Host.put(src_files, ...)`` — which is stripped before resolving; the
    parameters themselves aren't independently checkable via ``getattr``, so
    a signature drift is caught by the surface-equality check, not here.
    """
    try:
        mod = importlib.import_module(module)
    except Exception as exc:  # noqa: BLE001 — a broken documented path is a finding, not a crash
        return f"cannot import {module!r}: {exc}"
    if name:
        obj = mod
        try:
            for part in name.split("(", 1)[0].split("."):
                obj = getattr(obj, part)
        except AttributeError as exc:
            return f"{module}.{name} does not exist: {exc}"
    return None


def documented_deep_imports(docs_root: Path | None = None) -> tuple[list[str], list[str]]:
    """Return (sorted deduped ``<module>:<name>`` lines, human-readable failures).

    A failure names the doc file:line that teaches the broken path — the docs
    are the declaration, so that is where the fix belongs.

    ``docs_root`` defaults to the module-level :data:`DOCS_ROOT` — read at
    call time (not bound as a default argument), so a test can point this at
    an empty or synthetic tree either by passing ``docs_root`` explicitly or
    by monkeypatching :data:`DOCS_ROOT` itself (which also exercises
    :func:`compute_surface` / ``main`` end to end).
    """
    docs_root = DOCS_ROOT if docs_root is None else docs_root
    lines: set[str] = set()
    failures: list[str] = []
    for path in sorted(
        p
        for pattern in ("*.md", "*.rst")
        for p in docs_root.rglob(pattern)
        if not any(part in SKIP_PARTS for part in p.relative_to(docs_root).parts)
    ):
        candidates = _markdown_candidates(path) if path.suffix == ".md" else _rst_candidates(path)
        try:
            rel = path.relative_to(REPO_ROOT)
        except ValueError:
            rel = path  # a synthetic docs_root outside the repo (e.g. a test's tmp_path)
        for line_no, code in candidates:
            if not IMPORT_LINE.match(code):
                continue
            try:
                node = _parse_statement(code)
            except (SyntaxError, ValueError) as exc:
                failures.append(f"{rel}:{line_no}: does not parse as an import statement: {exc}")
                continue
            for module, name in _module_name_pairs(node):
                error = _resolve(module, name)
                if error is not None:
                    failures.append(f"{rel}:{line_no}: {error}")
                else:
                    lines.add(f"{module}:{name}")
    return sorted(lines), failures


def public_export_lines() -> list[str]:
    """``otto:<name>`` for every name in ``otto.__all__``."""
    import otto

    return sorted(f"otto:{name}" for name in otto.__all__)


def host_protocol_lines() -> list[str]:
    """``otto.host.host:Host.<method>(<params>)`` for every public method ``Host`` declares.

    ``Host`` (``src/otto/host/host.py``) is a structural :class:`typing.Protocol`
    with no default bump-tool visibility of its own: a family renames or drops a
    parameter on ``run``/``put``/... and nothing about that shows up in a commit
    subject unless a human remembers to mark it. Reading every public method's
    parameter names straight off ``inspect.signature`` — via
    :func:`otto.testing.conformance_host._keyword_names`, the same reader
    :func:`~otto.testing.conformance_host.assert_host_conforms` already trusts,
    not a retyped copy — turns that into a golden line per method, so a
    parameter added, removed, renamed, or reordered is a reviewable diff here
    exactly like a change to ``otto.__all__``.

    Only names ``vars(Host)`` defines directly and that don't start with an
    underscore qualify: that excludes the private ``_login`` hook, the dunder
    protocol machinery (``__aenter__``/``__aexit__``), and the read-only
    ``element`` property (a :class:`property` object, not callable, so
    :func:`callable` already filters it out) — none of those is a call shape a
    caller passes keyword arguments into.

    What this line shape CANNOT see: a parameter moved to keyword-only behind
    a bare ``*`` renders identically to one that was already keyword-only —
    :func:`~otto.testing.conformance_host._keyword_names` reports a name, not
    its :class:`inspect.Parameter.kind`, so a positional-or-keyword parameter
    narrowed to keyword-only (breaking for a caller passing it positionally)
    does not move the golden line. Nor is a removed or changed *default*
    pinned — only the ordered name list is. Both are real, if narrower,
    breaking changes this golden does not catch; only a genuinely
    added/removed/renamed/reordered name does.
    """
    from otto.host.host import Host
    from otto.testing.conformance_host import _keyword_names

    lines = []
    for name, member in vars(Host).items():
        if name.startswith("_") or not callable(member):
            continue
        params = ", ".join(_keyword_names(member))
        lines.append(f"otto.host.host:Host.{name}({params})")
    return sorted(lines)


def compute_surface() -> tuple[list[str], list[str]]:
    """Return (sorted deduped golden lines, resolution failures) for the whole surface."""
    deep_lines, failures = documented_deep_imports()
    lines = sorted(set(public_export_lines()) | set(deep_lines) | set(host_protocol_lines()))
    return lines, failures


SURFACE_FLOOR = 100
"""Minimum plausible surface size — shared by ``--update`` and the pytest floor test.

Well under the ~147 lines measured when this was written (28 lazy exports +
~120 deduped documented deep paths): enough margin that ordinary doc edits
never trip it, tight enough that a docs-walk regression (wrong root, wrong
glob, a fence-parsing bug that stops matching anything) still does. A single
constant so ``--update`` can refuse to write a suspiciously small golden
instead of only a later test catching it.
"""


_HEADER = """\
# Golden snapshot of otto's public API surface — see scripts/api_snapshot.py.
#
# Three kinds of line:
#   otto:<name>              a name in otto.__all__ (the PEP 562 lazy-export table)
#   <module>:<name>          a deep import path the user docs teach; a bare
#                             `import <module>` line contributes `<module>:` (no name)
#   otto.host.host:Host.<method>(<params>)
#                             a public Host protocol method, with its keyword
#                             parameter names pinned in signature order
#
# A name added/removed/renamed here is a public-API change and must show up
# in this diff, and so is a Host protocol method whose parameters changed —
# both are pinned so a breaking change to either one shows up unmarked
# nowhere but here. The deep paths are declared entirely by docs/ (excluding
# the archived docs/superpowers/) — there is no separate allowlist to
# maintain.
#
# Regenerate with `make api-snapshot`; review the diff before committing it.
"""


def write_golden(lines: list[str]) -> None:
    """Write *lines* as the golden snapshot, with the explanatory header on top."""
    GOLDEN_PATH.parent.mkdir(parents=True, exist_ok=True)
    GOLDEN_PATH.write_text(_HEADER + "\n".join(lines) + "\n", encoding="utf-8")


def read_golden() -> list[str]:
    """Return the golden snapshot's data lines (its ``#`` header stripped)."""
    text = GOLDEN_PATH.read_text(encoding="utf-8")
    return [ln for ln in text.splitlines() if ln and not ln.startswith("#")]


def main(argv: list[str]) -> int:
    """Print the current surface; optionally --update the golden or --check it."""
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("--update", action="store_true", help="regenerate the golden snapshot")
    ap.add_argument(
        "--check",
        action="store_true",
        help="compare against the golden; exit non-zero on drift or an unresolved import",
    )
    args = ap.parse_args(argv)

    lines, failures = compute_surface()
    for failure in failures:
        print(f"FAIL {failure}")

    if args.update:
        if failures:
            print("\nnot writing the golden: resolve the failures above first.")
            return 1
        if len(lines) < SURFACE_FLOOR:
            print(
                f"\nnot writing the golden: only {len(lines)} lines, below the floor of "
                f"{SURFACE_FLOOR} — the docs walk is probably broken, not the API shrinking."
            )
            return 1
        write_golden(lines)
        print(f"wrote {GOLDEN_PATH.relative_to(REPO_ROOT)} ({len(lines)} lines)")
        return 0

    if args.check:
        expected = read_golden()
        if lines != expected:
            diff = "\n".join(
                difflib.unified_diff(
                    expected, lines, fromfile="golden", tofile="current", lineterm=""
                )
            )
            print(diff)
            print("\npublic API surface changed. If intentional, run `make api-snapshot`.")
            return 1
        if failures:
            return 1
        print("api snapshot: OK")
        return 0

    for line in lines:
        print(line)
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
