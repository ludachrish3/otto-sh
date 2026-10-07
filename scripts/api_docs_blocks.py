"""Walk otto's teaching material and yield its code blocks, in page order.

One walker, shared by every script that reads code out of the docs, so they
all agree on what a "code block" is. ``scripts/api_teaching.py``, the
public-surface docs validator (#590), reads them to check every name a page
teaches.

A :class:`Block` is a code block's raw lines, each paired with its 1-based
line number in the file, plus a ``lang`` label saying what kind of block it
is. The walker yields every block and never decides which ones are "code":
callers filter on ``lang``. The labels:

  * Markdown (:func:`markdown_blocks`): the fence's effective language, as
    :func:`_frame_lang` reads it from the info string — ``"python"``,
    ``"pycon"``, ``""`` for a bare fence, ``"{doctest}"``,
    ``"{testsetup}"``, a MyST ``{code-block} <lang>``'s ``<lang>``, and
    container directives such as ``"{note}"`` too (their body holds only
    the lines no nested fence claimed). A CommonMark indented code block is
    ``"indented"``. MyST colon fences (``:::{code-block} python``) are
    fences too; one whose info is not a ``{directive}`` is a Markdown div,
    ``":::"``. A fence never closed runs to the end of the page, as in
    CommonMark. A ``{eval-rst}`` fence is not yielded itself: its body
    is RST, so it yields the RST blocks inside it instead, labelled as
    below and numbered by their lines on the page. A fence labelled
    ``markdown``/``md`` shows fence SYNTAX as prose, so neither it nor
    anything nested in it is yielded.
  * RST (:func:`rst_blocks`): a ``.. code-block:: <lang>`` gives ``<lang>``;
    ``.. doctest::``, ``.. testsetup::``, ``.. testcode::`` and
    ``.. testcleanup::`` give ``"{doctest}"``, ``"{testsetup}"``,
    ``"{testcode}"`` and ``"{testcleanup}"``; any other ``::`` literal
    block gives ``""``.
  * Python (:func:`python_blocks`): ``"py-file"`` for the whole source,
    then, inside docstrings, an RST code directive's label as above,
    ``"py-doctest"`` for each run of ``>>>`` prompt lines and
    ``"py-literal"`` for each ``::`` literal block.

:func:`literal_includes` finds the files a page pulls in with
``{literalinclude}``, and :func:`teaching_corpus` lists the files that make
up the teaching boundary (spec §3 of
``docs/superpowers/specs/2026-10-04-public-api-manifest-design.md``): docs
pages and the README, ``docs/examples/**/*.py``, and ``src/otto/examples/*.py``.
A doctest on a function elsewhere in ``src/otto`` tests that function; it does
not teach, so it is not in the corpus.
"""

import ast
import re
import sys
from dataclasses import dataclass
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))
from scripts.api_parse import parse_quietly  # noqa: E402 -- path set up above

# Archived specs/plans carry dead paths on purpose; the built site is an output.
SKIP_PARTS = ("_build", "superpowers")

# A fence delimiter is a run of 3+ backticks OR 3+ tildes (CommonMark allows
# either) OR 3+ colons (MyST's ``colon_fence`` extension, enabled in
# docs/conf.py), optionally indented (a fence nested in a list item's
# continuation, e.g. docs/cookbook/sessions.md:116). ``\s*`` mirrors
# scripts/lint_markdown_doctests.py:28 rather than requiring column 0.
FENCE = re.compile(r"^\s*(?P<fence>`{3,}|~{3,}|:{3,})(?P<info>.*)$")
CODE_BLOCK_DIRECTIVE = "{code-block}"  # MyST: ```{code-block} python``` — lang is the 2nd token
# Only an explicit "show the syntax as prose" label makes a fence opaque; any
# other MyST directive (``{note}``, ``{tab-item}``, …) is a plain CONTAINER —
# its own body isn't code, but a real code fence nested inside it still is,
# and the fence stack below finds that fence regardless of relative nesting
# depth or backtick count (MyST's own convention needs the container to use
# MORE backticks than its contents, but a same-count nesting is handled too).
DISPLAY_LANGS = {"markdown", "md"}
EVAL_RST_LANG = "{eval-rst}"  # MyST: the fence body is RST, read with the RST scan
# MyST renders a colon fence whose info is not a ``{directive}`` as a div whose
# body is Markdown: a container, never code, so it gets a label no caller reads
# as a language.
COLON_DIV_LANG = ":::"
# MyST directives whose body is literal text (code, doctest source or
# include options), never Markdown: like a plain-language fence, nothing
# inside one opens a nested fence. Every other ``{directive}`` (``{note}``,
# ``{tab-item}``, ``{eval-rst}``, ...) and a colon div is a container.
LITERAL_DIRECTIVES = {
    CODE_BLOCK_DIRECTIVE,
    "{code}",
    "{sourcecode}",
    "{doctest}",
    "{testsetup}",
    "{testcode}",
    "{testcleanup}",
    "{literalinclude}",
}


_INDENT_CODE_BLOCK = 4  # CommonMark: a blank line then 4+ spaces of indent opens a code block

# An RST directive line: ``.. <name>:: <one optional argument>``.
_RST_DIRECTIVE = re.compile(r"^(\s*)\.\.\s+([\w-]+)::\s*(\S*)\s*$")
_RST_LANG = {
    "doctest": "{doctest}",
    "testsetup": "{testsetup}",
    "testcode": "{testcode}",
    "testcleanup": "{testcleanup}",
}

_MD_INCLUDE = re.compile(r"^\s*(?:`{3,}|~{3,}|:{3,})\{literalinclude\}\s+(\S+)")
_RST_INCLUDE = re.compile(r"^\s*\.\.\s+literalinclude::\s+(\S+)")

_DOCTEST_PROMPT = ">>>"
_DOCTEST_CONTINUATION = "..."
_DOCSTRING_CLOSERS = ('"""', "'''")


@dataclass
class Block:
    """One code block: where it lives, what kind it is, and its raw lines.

    ``lines`` holds ``(line_no, raw_text)`` pairs, 1-based, in file order,
    with the text exactly as the file has it (indentation and any doctest
    prompt included). Blank lines inside a block are kept only where the
    source format makes them part of the block (a fence's body); indented
    and literal blocks keep their non-blank lines only.
    """

    path: Path
    lang: str
    lines: list[tuple[int, str]]


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


def _frame_lang(info: str, char: str = "`") -> str:
    """Return the effective code language for a fence's info string.

    Unwraps MyST's ``{code-block} <lang>`` directive to its second token;
    everything else (``python``, ``pycon``, ``{doctest}``, a bare fence's
    ``""``, or a directive name like ``{note}``) is used as-is — the caller
    decides what counts as "code" from that value. A colon fence (*char*
    ``":"``) is code only when its info names a ``{directive}``; any other
    colon fence is a Markdown div, labelled :data:`COLON_DIV_LANG`.
    """
    if char == ":" and not (info.startswith("{") and info.split(maxsplit=1)[0].endswith("}")):
        return COLON_DIV_LANG
    if not info:
        return ""
    tokens = info.split()
    if tokens[0] == CODE_BLOCK_DIRECTIVE:
        return tokens[1] if len(tokens) > 1 else ""
    return tokens[0]


def _block_start(block: Block) -> int:
    return block.lines[0][0] if block.lines else 0


def _is_literal(info: str, lang: str) -> bool:
    """Return whether a fence with *info* (effective *lang*) holds literal text.

    CommonMark: a fenced code block's content is literal, so no line in it
    opens a fence. That holds for every plain-language or bare fence and
    every :data:`LITERAL_DIRECTIVES` directive. A container directive or a
    colon div is not literal. Neither is a ``markdown``/``md`` display
    fence: its whole content is suppressed anyway, and tracking the fences
    it shows keeps their closers from ending it early.
    """
    first = info.split(maxsplit=1)[0] if info else ""
    if lang == COLON_DIV_LANG or lang in DISPLAY_LANGS:
        return False
    if first.startswith("{"):
        return first in LITERAL_DIRECTIVES
    return True


def _open_frame(line: str, suppressed: bool) -> dict | None:
    """Return a fence frame if *line* opens one.

    The frame is ``{ticks, char, lang, literal, suppressed, body}``.
    """
    opened = _fence_open(line)
    if opened is None:
        return None
    ticks, char, info = opened
    lang = _frame_lang(info, char)
    return {
        "ticks": ticks,
        "char": char,
        "lang": lang,
        "literal": _is_literal(info, lang),
        "suppressed": suppressed or lang in DISPLAY_LANGS,
        "body": [],
    }


def _close_frame(path: Path, frame: dict, blocks: list[Block]) -> None:
    """Add a finished fence frame's blocks: the RST blocks of an ``{eval-rst}``, else itself."""
    if frame["suppressed"]:
        return
    if frame["lang"] == EVAL_RST_LANG:
        blocks.extend(_rst_scan(path, frame["body"]))
    else:
        blocks.append(Block(path, frame["lang"], frame["body"]))


def markdown_blocks(path: Path) -> list[Block]:
    """Every code block in a Markdown page, sorted by first line.

    Three shapes: fenced blocks (backtick, tilde or MyST colon; ```python```/```pycon```/
    plain/```{doctest}```/MyST ```{code-block} python```, possibly nested in
    a MyST container directive or a list item's indented continuation),
    CommonMark's blank-line-then-4-space-indent code blocks (``lang`` is
    ``"indented"``), and — inside a fence explicitly labelled
    ``markdown``/``md`` — nothing at all: that fence exists to show fence
    *syntax* as prose (see ``docs/contributing.md``'s doctest-fence example),
    so it and everything textually inside it, however it's nested, is
    suppressed.

    Nesting is tracked with an explicit stack rather than one open/close
    pair: inside a container, a fence line WITH an info string is always a
    new, independent open (even at the same tick count as its enclosing
    fence — MyST only asks for *more* backticks on the container, it
    doesn't require it), and a bare fence line with no info string closes
    the innermost fence it's long/thick enough for. That is enough to find
    a real ```` ```python ```` block correctly however many
    ``{note}``/``{tab-item}``/... containers it sits inside. A code fence's
    content is literal (CommonMark): inside one, no line of either fence
    kind opens anything, and only its own closing fence ends it. Container
    frames are yielded too, holding only the lines no nested fence claimed;
    callers filter on ``lang``. An ``{eval-rst}`` fence's body is RST, so
    it yields the blocks :func:`rst_blocks` would find there (same ``lang``
    labels, page line numbers) in its place. A fence still open at the end
    of the page is closed there, as CommonMark runs it to the end of the
    document.
    """
    lines = path.read_text(encoding="utf-8").splitlines()
    blocks: list[Block] = []
    i, n = 0, len(lines)
    prev_blank = True
    stack: list[dict] = []  # innermost frame last: see _open_frame
    while i < n:
        line = lines[i]
        if stack:
            top = stack[-1]
            if _fence_closes(line, top["ticks"], top["char"]):
                _close_frame(path, stack.pop(), blocks)
                i += 1
                prev_blank = False
                continue
            frame = None if top["literal"] else _open_frame(line, top["suppressed"])
            if frame is not None:
                stack.append(frame)
                i += 1
                prev_blank = False
                continue
            top["body"].append((i + 1, line))
            prev_blank = False
            i += 1
            continue
        frame = _open_frame(line, False)
        if frame is not None:
            stack.append(frame)
            i += 1
            prev_blank = False
            continue
        if prev_blank and line.strip() and _indent(line) >= _INDENT_CODE_BLOCK:
            body = []
            while i < n and (not lines[i].strip() or _indent(lines[i]) >= _INDENT_CODE_BLOCK):
                if lines[i].strip():
                    body.append((i + 1, lines[i]))
                i += 1
            blocks.append(Block(path, "indented", body))
            prev_blank = True
            continue
        prev_blank = not line.strip()
        i += 1
    while stack:  # CommonMark: a fence never closed runs to the end of the document
        _close_frame(path, stack.pop(), blocks)
    return sorted(blocks, key=_block_start)


def _rst_directive_lang(line: str) -> str | None:
    """Return the ``lang`` of the code block RST directive *line* opens, else None.

    ``.. code-block:: <lang>`` gives ``<lang>``; a Sphinx doctest directive
    (``.. doctest::``, ``.. testsetup::``, ...) gives its ``{doctest}``-style
    name. Any other line, including any other directive, gives None.
    """
    directive = _RST_DIRECTIVE.match(line.rstrip())
    if not directive:
        return None
    if directive.group(2) == "code-block":
        return directive.group(3)
    return _RST_LANG.get(directive.group(2))


def _rst_indented_block(
    path: Path, numbered: list[tuple[int, str]], i: int, lang: str
) -> tuple[Block | None, int]:
    """Read the *lang* block whose opener (a directive or ``::`` line) is ``numbered[i]``.

    Returns the block, or None when the opener announced no body indented
    deeper than itself, and the index of the first line after it. Only
    indentation RELATIVE to the opener matters.
    """
    n = len(numbered)
    base_indent = _indent(numbered[i][1])
    i += 1
    while i < n and not numbered[i][1].strip():
        i += 1
    if i >= n or _indent(numbered[i][1]) <= base_indent:
        return None, i  # the "::"/directive announced no indented body — not a code block
    block_indent = _indent(numbered[i][1])
    body = []
    while i < n and (not numbered[i][1].strip() or _indent(numbered[i][1]) >= block_indent):
        if numbered[i][1].strip():
            body.append(numbered[i])
        i += 1
    return Block(path, lang, body), i


def _rst_scan(path: Path, numbered: list[tuple[int, str]]) -> list[Block]:
    """Every code block in RST *numbered* lines, each keeping its own line number.

    The scan behind :func:`rst_blocks` and a Markdown page's ``{eval-rst}``
    fence. Only indentation RELATIVE to a directive line matters, so a body
    indented as a whole (a fence inside a list item) scans the same as one
    at column 0. A paragraph that starts with ``>>>`` is an RST doctest
    block: it runs to the next blank line and is labelled ``"{doctest}"``,
    like the ``.. doctest::`` directive. A ``>>>`` line in the middle of a
    paragraph is prose.
    """
    blocks: list[Block] = []
    i, n = 0, len(numbered)
    while i < n:
        line = numbered[i][1]
        paragraph_start = i == 0 or not numbered[i - 1][1].strip()
        if paragraph_start and line.lstrip().startswith(_DOCTEST_PROMPT):
            run = []
            while i < n and numbered[i][1].strip():
                run.append(numbered[i])
                i += 1
            blocks.append(Block(path, _RST_LANG["doctest"], run))
            continue
        lang = _rst_directive_lang(line)
        if lang is None and line.rstrip().endswith("::"):
            lang = ""
        if lang is None:
            i += 1
            continue
        block, i = _rst_indented_block(path, numbered, i, lang)
        if block is not None:
            blocks.append(block)
    return blocks


def rst_blocks(path: Path) -> list[Block]:
    """Every code block in an RST page, in page order.

    Three shapes: an explicit directive (``.. code-block:: python``,
    ``.. doctest::``, ``.. testsetup::``, ...) and RST's
    ``::``-then-indented-paragraph literal block shorthand — both are an
    indented body following a line at some base indent, so both are handled
    by the same "consume while blank-or-more-indented" scan — and RST's
    doctest block, a paragraph starting with ``>>>``. A directive with no
    indented body (``.. literalinclude:: x.py``, ``.. automodule::`` without
    options) is not a code block and yields nothing.

    ``lang`` is the ``code-block`` argument, the ``{doctest}``-style name of
    a Sphinx doctest directive (a doctest block is ``"{doctest}"`` too), or
    ``""`` for any other literal block.
    """
    lines = path.read_text(encoding="utf-8").splitlines()
    return _rst_scan(path, list(enumerate(lines, 1)))


def _docstring_spans(tree: ast.Module) -> list[tuple[int, int]]:
    """``(first, last)`` 1-based source lines of every module/class/function docstring."""
    spans = []
    for node in ast.walk(tree):
        if not isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        if not node.body:
            continue
        first = node.body[0]
        if (
            isinstance(first, ast.Expr)
            and isinstance(first.value, ast.Constant)
            and isinstance(first.value.value, str)
        ):
            spans.append((first.lineno, first.end_lineno or first.lineno))
    return sorted(spans)


def _docstring_blocks(path: Path, body: list[tuple[int, str]]) -> list[Block]:
    """Split one docstring's raw lines into its code blocks.

    A ``.. code-block:: <lang>`` directive or a Sphinx doctest directive
    (``.. doctest::``, ``.. testsetup::``, ...) gives a block labelled as
    :func:`rst_blocks` labels it (``<lang>``, ``"{doctest}"``, ...), its
    body read by the same scan. Otherwise a doctest run opens on a ``>>>``
    line and continues through every following ``>>>``/``...`` line;
    expected output, a blank line or prose ends it (a prose line that
    merely starts with ``...`` opens nothing) — a ``"py-doctest"`` block. A
    line ending in ``::`` opens a ``"py-literal"`` block: the blank lines
    after it, then every line indented deeper than it — the same rule as
    :func:`rst_blocks`, and like there a ``::`` with no deeper body yields
    nothing. A line lands in at most one block.
    """
    blocks: list[Block] = []
    i, n = 0, len(body)
    while i < n:
        text = body[i][1]
        stripped = text.strip()
        lang = _rst_directive_lang(text)
        if lang is not None:
            block, i = _rst_indented_block(path, body, i, lang)
            if block is not None:
                blocks.append(block)
            continue
        if stripped.startswith(_DOCTEST_PROMPT):
            run = []
            while i < n and body[i][1].strip().startswith((_DOCTEST_PROMPT, _DOCTEST_CONTINUATION)):
                run.append(body[i])
                i += 1
            blocks.append(Block(path, "py-doctest", run))
            continue
        if text.rstrip().endswith("::"):
            base_indent = _indent(text)
            i += 1
            while i < n and not body[i][1].strip():
                i += 1
            if i >= n or _indent(body[i][1]) <= base_indent:
                continue
            literal = []
            while i < n and (not body[i][1].strip() or _indent(body[i][1]) > base_indent):
                if body[i][1].strip():
                    literal.append(body[i])
                i += 1
            blocks.append(Block(path, "py-literal", literal))
            continue
        i += 1
    return blocks


def python_blocks(path: Path) -> list[Block]:
    """Return a Python file whole, then every code block in its docstrings.

    The first block is ``"py-file"``: every line of the source. After it,
    in line order, come the code blocks of every module, class and function
    docstring (see :func:`_docstring_blocks`): a block per RST code
    directive, labelled as :func:`rst_blocks` labels it, a ``"py-doctest"``
    block per run of ``>>>`` prompt lines and a ``"py-literal"`` block per
    ``::`` literal block. Lines are the raw source lines, except that a
    docstring's closing delimiter sharing a line with code is cut off, so
    the code reads as code. A file that does not parse has no docstrings to
    find: it yields its ``"py-file"`` block alone, for the caller to judge.

    Lines split on newlines only, as the parser numbers them —
    :meth:`str.splitlines` also breaks on form feeds and other separators
    a source line may contain.
    """
    source = path.read_text(encoding="utf-8")
    lines = source.split("\n")
    if lines[-1] == "":
        lines.pop()  # the newline ending the last line opens no line of its own
    blocks = [Block(path, "py-file", list(enumerate(lines, 1)))]
    try:
        tree = parse_quietly(source, str(path))
    except SyntaxError:
        return blocks
    for first, last in _docstring_spans(tree):
        body = [(no, lines[no - 1]) for no in range(first, last + 1)]
        closing = body[-1][1].rstrip()
        for closer in _DOCSTRING_CLOSERS:
            if closing.endswith(closer) and closing[: -len(closer)].strip():
                body[-1] = (last, closing[: -len(closer)])
                break
        blocks.extend(_docstring_blocks(path, body))
    return blocks


def literal_includes(path: Path, docs_root: Path) -> list[tuple[int, Path]]:
    """``(line_no, resolved_target)`` for every ``literalinclude`` on a docs page.

    Covers MyST ``{literalinclude} <target>`` fences (backtick, tilde or
    ``:::`` colon fences) and RST ``.. literalinclude:: <target>``
    directives, whichever kind of page holds them (an ``eval-rst`` block
    puts RST inside Markdown). Sphinx resolves a target starting with ``/``
    against the source root, *docs_root*; any other target resolves against
    the page's own directory.
    """
    out: list[tuple[int, Path]] = []
    for line_no, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        m = _MD_INCLUDE.match(line) or _RST_INCLUDE.match(line)
        if not m:
            continue
        target = m.group(1)
        if target.startswith("/"):
            resolved = (docs_root / target.lstrip("/")).resolve()
        else:
            resolved = (path.parent / target).resolve()
        out.append((line_no, resolved))
    return out


def teaching_corpus(repo_root: Path) -> list[Path]:
    """Every file inside the teaching boundary, sorted.

    The boundary (spec §3): docs pages (``docs/**/*.md`` and
    ``docs/**/*.rst``, skipping ``_build`` and the archived
    ``superpowers`` specs and plans), the top-level ``README.md``, every
    ``docs/examples/**/*.py``, and every ``src/otto/examples/*.py``.
    Nothing else in ``src/otto`` is teaching: a function's doctest there
    tests that function.
    """
    docs = repo_root / "docs"
    pages = [
        p
        for pattern in ("*.md", "*.rst")
        for p in docs.rglob(pattern)
        if not any(part in SKIP_PARTS for part in p.relative_to(docs).parts)
    ]
    files = [
        *pages,
        repo_root / "README.md",
        *(docs / "examples").rglob("*.py"),
        *(repo_root / "src" / "otto" / "examples").glob("*.py"),
    ]
    return sorted(files)
