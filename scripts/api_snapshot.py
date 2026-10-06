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

Dump mode (the API dump, spec ``docs/superpowers/specs/2026-10-05-api-dump-design.md``):
    python scripts/api_snapshot.py --manifest M [--golden G]     # print the dump
    python scripts/api_snapshot.py --manifest M [--golden G] --update | --check
    python scripts/api_snapshot.py --manifest M --assume-dir --report
                                               # print producer refusals and
                                               # agreement failures; always exit 0
"""

import argparse
import ast
import difflib
import importlib
import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))
from scripts import api_lines  # noqa: E402 -- path set up above
from scripts.api_docs_blocks import (  # noqa: E402 -- path set up above
    markdown_blocks,
    rst_blocks,
)

DOCS_ROOT = REPO_ROOT / "docs"
GOLDEN_PATH = REPO_ROOT / "tests" / "unit" / "api_snapshot" / "public_api.txt"
SCHEMA_V2 = 2

# Archived specs/plans carry dead paths on purpose; the built site is an output.
SKIP_PARTS = ("_build", "superpowers")

CODE_FENCE_LANGS = {"python", "pycon", ""}
DOCTEST_FENCE_LANG = "{doctest}"

# A candidate line, after stripping any doctest ">>> " prompt. `otto(?:\.\w+)*`
# (not `otto[\w.]*`) so `otto_something` can never match: there is no dot
# between "otto" and "_something", so the group can't consume it, and the
# whole pattern then fails at the required `\s+import` — a name that merely
# starts with "otto" is ignored rather than reported as a broken import.
IMPORT_LINE = re.compile(
    r"^(from\s+otto(?:\.\w+)*(?!\w)\s+import\s+.+|import\s+otto(?:\.\w+)*(?!\w)(?:\s+as\s+\w+)?)"
)


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


_SCANNED_MD_LANGS = {*CODE_FENCE_LANGS, DOCTEST_FENCE_LANG, "indented"}


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
    out: list[tuple[int, str]] = []
    for block in markdown_blocks(path):
        if block.lang in _SCANNED_MD_LANGS:
            out.extend(_extract_lines(block.lines, prompted=block.lang == DOCTEST_FENCE_LANG))
    return out


def _rst_candidates(path: Path) -> list[tuple[int, str]]:
    """Candidate (line_no, code) pairs from an RST file's code examples.

    Two shapes: an explicit ``.. code-block:: python`` directive, and RST's
    ``::``-then-indented-paragraph literal block shorthand — both are an
    indented body following a line at some base indent, so both are handled
    by the same "consume while blank-or-more-indented" scan.
    """
    out: list[tuple[int, str]] = []
    for block in rst_blocks(path):
        out.extend(_extract_lines(block.lines, prompted=False))
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
        lines.append(api_lines.host_line(name, _keyword_names(member)))
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
    return api_lines.data_lines(GOLDEN_PATH.read_text(encoding="utf-8"))


def describe_drift(golden: list[str], current: list[str]) -> str:
    """Say whether the public API only GREW, or CHANGED, since the golden was written.

    For a person to read and decide on, never acted on: ``--check`` writes
    nothing. Growth adds lines and leaves every existing one in place, so
    no caller can break. A change removes or rewrites at least one line: a
    rename, a dropped name, a parameter that moved. It lists the removed
    lines because those are the ones callers depend on.
    """
    removed = sorted(set(golden) - set(current))
    added = sorted(set(current) - set(golden))
    if not removed:
        return (
            f"public API GREW: {len(added)} line(s) added, nothing removed or changed. "
            "Review the additions; if they are meant to be public, record them with "
            "`make api-snapshot` and commit the golden."
        )
    shown = "\n".join(f"  - {line}" for line in removed)
    return (
        f"public API CHANGED: {len(removed)} line(s) removed or rewritten, "
        f"{len(added)} added. Callers of these break:\n{shown}\n"
        "If the change is intended, record it with `make api-snapshot`, commit the "
        "golden, and mark the commit breaking (`!` or `BREAKING CHANGE:`)."
    )


def describe_dump_drift(golden_text: str, current_text: str) -> str:
    """Say how a stale dump differs: a breaking change, growth, or a re-sort.

    Report-only, like :func:`describe_drift`. The findings are the checker's own
    (``scripts/api_compat.py``), so what this says is what ``check-breaking`` will say.
    """
    from scripts import api_compat, api_records

    try:
        golden = api_records.parse_dump(golden_text)
    except api_records.DumpError as exc:
        return f"the golden does not parse ({exc}); regenerate it with `make api-snapshot`."
    findings = api_compat.compare_dumps(golden, api_records.parse_dump(current_text))
    if findings:
        shown = "\n".join(f"  - {f}" for f in api_compat.group_findings(findings))
        return (
            f"public API CHANGED: {len(findings)} breaking finding(s):\n{shown}\n"
            "If the change is intended, record it with `make api-snapshot`, commit the "
            "golden, and mark the commit breaking (`!` or `BREAKING CHANGE:`)."
        )
    if sorted(golden_text.splitlines()) == sorted(current_text.splitlines()):
        return "the golden is not in canonical order; regenerate it with `make api-snapshot`."
    return (
        "public API GREW (or changed without breaking anything): record it with "
        "`make api-snapshot` and commit the golden."
    )


def _main_dump(args: argparse.Namespace) -> int:
    """Run ``main`` in dump mode: the manifest's namespaces, as the API dump records them.

    Every failure is a ``FAIL <reason>`` line and exit 1, never a traceback. With
    ``--report`` it prints the producer's refusals, counts them, and exits 0: a
    measurement before P1, never a gate.
    """
    from scripts import api_regen
    from scripts.api_agreement import AgreementError, agreement_failures, namespace_reports
    from scripts.api_manifest import ManifestError, load_manifest

    try:
        names = sorted(load_manifest(args.manifest))
    except ManifestError as exc:
        print(f"FAIL {exc}")
        return 1
    try:
        reports = namespace_reports(names, REPO_ROOT)
    except AgreementError as exc:
        print(f"FAIL cannot report the declared namespaces: {exc}")
        return 1
    failures = agreement_failures(names, reports)
    generated = api_regen.generate_worktree(REPO_ROOT, args.manifest, assume_dir=args.assume_dir)
    problems = [*failures, *generated.refusals]
    if args.report:
        # Agreement failures are api_teaching.py --report's to list; only the
        # producer's own refusals are this mode's.
        for refusal in generated.refusals:
            print(f"refusal: {refusal}")
        print(f"api-dump-report: {len(generated.refusals)} producer refusal(s)")
        return 0
    for problem in problems:
        print(f"FAIL {problem}")
    golden = args.golden or GOLDEN_PATH
    if args.update:
        if problems or generated.text is None:
            print("\nnot writing the golden: resolve the failures above first.")
            return 1
        golden.write_bytes(generated.text.encode("utf-8"))
        return 0
    if args.check:
        return _check_dump(golden, generated.text, problems)
    if generated.text is not None:
        print(generated.text, end="")
    return 1 if problems else 0


def _check_dump(golden: Path, current: "str | None", problems: list[str]) -> int:
    """Compare the committed dump *golden* with *current*; return the exit code.

    The comparison is of BYTES, as ``check-breaking``'s freshness check makes it
    (dump spec §5.1): a golden whose text matches but whose bytes do not (CRLF
    line endings, say) is stale here exactly as it is there.
    """
    try:
        data = golden.read_bytes()
    except OSError as exc:
        print(f"FAIL cannot read the golden {golden}: {exc}")
        return 1
    text = data.decode("utf-8", errors="replace")
    if api_lines.schema_of(text) != SCHEMA_V2:
        print(f"FAIL {golden} is not an api-snapshot v2 golden: no {api_lines.V2_HEADER!r} line")
        return 1
    if problems or current is None:
        return 1
    if data != current.encode("utf-8"):
        if text.splitlines() == current.splitlines():
            print(
                "FAIL the golden's bytes differ from the dump's though every line matches "
                "(line endings or encoding); regenerate it with `make api-snapshot`."
            )
            return 1
        print(
            "\n".join(
                difflib.unified_diff(
                    text.splitlines(), current.splitlines(), "golden", "current", lineterm=""
                )
            )
        )
        print(describe_dump_drift(text, current))
        return 1
    print("api snapshot: OK")
    return 0


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
    ap.add_argument(
        "--manifest", type=Path, help="dump mode: declared namespaces (api/public.toml shape)"
    )
    ap.add_argument(
        "--assume-dir",
        action="store_true",
        help="dump mode: a namespace with no __all__ exports its public globals (report only)",
    )
    ap.add_argument("--report", action="store_true", help="dump mode: print refusals and exit 0")
    ap.add_argument(
        "--golden", type=Path, default=None, help="golden path (default: the committed one)"
    )
    args = ap.parse_args(argv)
    if (args.golden is not None or args.assume_dir or args.report) and args.manifest is None:
        # v1 always reads and writes the committed golden; ignoring --golden
        # would let `--golden X --update` silently overwrite it.
        ap.error("--golden, --assume-dir and --report require --manifest")

    if args.manifest is not None:
        return _main_dump(args)

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
            print()
            print(describe_drift(expected, lines))
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
