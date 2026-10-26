#!/usr/bin/env python3
"""Fail if a user page cites what only otto's maintainers have.

A reader has three things: the published docs, the example projects under
``docs/examples/`` (each one also a downloadable archive), and otto's installed
sources under ``src/otto/``. A reader does not have otto's checkout: its
``tests/``, ``scripts/``, ``Makefile`` and issue tracker. A page outside
Developing otto that includes, downloads, links or names one of those, runs a
maintainer make target, or cites an issue number sends the reader to something
they cannot open.

Developing otto pages (``developing/``, ``architecture/``, ``contributing.md``,
``release_process.md``), generated pages, HTML comments in Markdown and RST
comments are exempt. A page listed in ``EXEMPT`` may hold up to its ceiling of
findings while its track is fixed; the count must match the ceiling exactly,
so a fix lowers the ceiling and a new finding fails.

Known gap: Markdown's 4-space indented code blocks are read as prose, not
code, because a list item's indented continuation looks the same. Fenced code
is read everywhere, including inside blockquotes and list items.
A maintainer target in backtick command substitution is flagged in any fenced
block, whatever its language: a page that shows ``make release`` in any
visible form is what this lint exists to catch.

Usage: python scripts/lint_docs_citations.py
"""

import bisect
import os
import posixpath
import re
import shlex
import subprocess
import sys
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import NamedTuple


@dataclass(frozen=True)
class Offense:
    """One citation a reader cannot follow."""

    page: str
    line: int
    rule: str
    detail: str


@dataclass(frozen=True)
class Exemption:
    """A page allowed ``ceiling`` findings until ``track`` fixes it."""

    track: str
    ceiling: int


@dataclass(frozen=True)
class Tree:
    """The paths otto owns: its tracked files and their directories."""

    files: frozenset[str]
    dirs: frozenset[str]

    @classmethod
    def of(cls, paths: list[str]) -> "Tree":
        """Build a tree from tracked file paths, deriving every directory prefix."""
        dirs: set[str] = set()
        for path in paths:
            parts = path.split("/")
            dirs.update("/".join(parts[:i]) for i in range(1, len(parts)))
        return cls(frozenset(paths), frozenset(dirs))


@dataclass(frozen=True)
class Region:
    """A run of consecutive page lines of one kind: prose, code or directive."""

    kind: str
    first_line: int
    lines: list[str]


class _Numbered(NamedTuple):
    """A page line, or a piece of one, with its page line number."""

    number: int
    text: str


class _Span(NamedTuple):
    """A code span's offsets: ``low`` inclusive, ``high`` exclusive."""

    low: int
    high: int


class _Container(NamedTuple):
    """A line split from its block containers: its blockquote count and the rest."""

    quotes: int
    body: str


@dataclass(frozen=True)
class _Visible:
    """A line's visible text, and whether an HTML comment is still open after it."""

    text: str
    in_comment: bool


@dataclass(frozen=True)
class _Fence:
    """An open Markdown fence: its marker, its width and how its body is read.

    ``mode`` is ``"code"``, ``"prose"`` (a non-code directive) or ``"rst"``
    (an ``{eval-rst}`` body, read with the RST rules). ``quotes`` is how many
    blockquote markers the fence sits under; its body lines lose that many.
    """

    char: str
    width: int
    mode: str
    quotes: int


@dataclass(frozen=True)
class _Classified:
    """A page's visible lines and the kind of each."""

    lines: list[str]
    kinds: list[str]


@dataclass(frozen=True)
class _ParagraphSpans:
    """One scan of a paragraph's code spans, shared by each line it covers."""

    first: int
    starts: list[int]
    lows: list[int]
    highs: list[int]

    def covers(self, index: int) -> bool:
        """Whether line *index* lies in the scanned paragraph."""
        return self.first <= index < self.first + len(self.starts)

    def ranges(self, index: int, length: int) -> list[_Span]:
        """Return the spans touching line *index*, as offsets into that line."""
        base = self.starts[index - self.first]
        found = []
        for span in range(bisect.bisect_right(self.highs, base), len(self.lows)):
            if self.lows[span] >= base + length:
                break
            found.append(_Span(self.lows[span] - base, self.highs[span] - base))
        return found


@dataclass(frozen=True)
class _Page:
    """What every rule needs to know about the page it reads."""

    rel: str
    directory: Path
    docs_root: Path
    checkout: Path
    tree: Tree
    examples: list[Path]


@dataclass(frozen=True)
class _Prose:
    """A prose region joined into one text, starting at ``first_line``."""

    first_line: int
    text: str

    def line_of(self, offset: int) -> int:
        """Return the page line holding character *offset* of the text."""
        return self.first_line + self.text.count("\n", 0, offset)


ROOT = Path(__file__).resolve().parents[1]

# Pages a pending docs track rewrites, each at its exact count of findings. The
# list only shrinks: each entry goes when its track lands (and delete its
# FIRST_CEILINGS pin in the test), and a fix before then lowers its ceiling.
# test_exemptions_only_shrink pins every first ceiling. A ceiling is an exact
# count, not a per-finding pin, so a fixed finding plus a new one on the same
# page can swap unseen.
EXEMPT: dict[str, Exemption] = {
    "cli/cov/instrumenting/containers.md": Exemption("L4.5 docker track", 10),
    "cli/cov/instrumenting/embedded.md": Exemption("L4.3 embedded track", 3),
    "setup/coverage.md": Exemption("L4.5 docker track", 1),
}

GENERIC_TARGETS = frozenset({"all", "build", "check", "clean", "dev", "format", "help", "lint"})
MAINTAINER_TARGETS = frozenset(
    {
        "api-snapshot",
        "api-surface-report",
        "browsers",
        "budget-delta",
        "busybox",
        "busybox-cache",
        "busybox-drift",
        "busybox-preflight",
        "changelog",
        "chaos",
        "chaos-embedded",
        "check-api-snapshot",
        "check-api-teaching",
        "check-breaking",
        "check-python",
        "check-ts",
        "ci",
        "clean-dist",
        "collect-check",
        "conformance",
        "conformance-bed",
        "console-logout",
        "coverage",
        "coverage-embedded",
        "coverage-gate-args",
        "coverage-hostless",
        "coverage-integration",
        "coverage-python",
        "coverage-stamp-start",
        "coverage-ts",
        "coverage-ts-unit",
        "coverage-unit",
        "coverage-unix",
        "dashboard",
        "dashboard-all",
        "dashboard-soak",
        "docs",
        "docs-captures",
        "docs-captures-check",
        "docs-html",
        "docs-inventories",
        "docs-lint",
        "docs-media",
        "doctest",
        "doctest-src",
        "format-python",
        "format-ts",
        "gate-fresh",
        "gate-scope",
        "gate-tip",
        "import-snapshot",
        "kmodcov",
        "kmodcov-matrix",
        "lab-reachability",
        "lint-arch",
        "lint-python",
        "lint-ts",
        "monitor-fixtures",
        "nox",
        "nox-embedded",
        "nox-full",
        "nox-hostless",
        "nox-integration",
        "nox-release",
        "nox-unit",
        "nox-unit-repeat",
        "nox-unix",
        "profile",
        "qemu-restart",
        "quick",
        "quick-guards",
        "release",
        "release-campaign",
        "release-kmodcov-matrix",
        "release-matrix",
        "release-preflight",
        "repeat",
        "schema",
        "stability",
        "stability-embedded",
        "stability-tunnel",
        "stability-unit",
        "stability-unix",
        "support-matrix",
        "test-ts",
        "typecheck",
        "typecheck-python",
        "typecheck-ts",
        "unit-outliers",
        "validate",
        "validate-python",
        "validate-ts",
        "vm-health",
        "web",
        "web-clean",
        "web-dev",
        "web-install",
        "wheel-check",
        "wheel-smoke",
    }
)

DEVELOPING_DIRS = ["developing/", "architecture/"]
DEVELOPING_FILES = ["contributing.md", "release_process.md"]
SKIP_DIRS = ["superpowers/", "_build/", "_inventories/", "_static/", "_templates/", "examples/"]
SUFFIXES = [".md", ".rst"]

INCLUDE_ALLOWED = ["docs/examples/", "src/otto/"]
DOWNLOAD_ALLOWED = ["docs/examples/", "docs/_static/generated/examples/"]
LINK_ALLOWED = ["docs/", "src/otto/"]
CODE_DIRECTIVES = frozenset({"code-block", "code", "sourcecode"})

COMMENT_OPEN = re.compile(r"<!--")
COMMENT_CLOSE = "-->"
LABEL = re.compile(r"^\([^()\s]+\)=$")
ATX = re.compile(r"^#{1,6}(\s|$)")
FENCE = re.compile(r"^\s*(`{3,}|~{3,}|:{3,})(.*)$")
# Block containers a fence may sit in: blockquote markers, then a list marker.
BLOCKQUOTE = re.compile(r"^[ \t]*>[ \t]?")
LIST_MARKER = re.compile(r"^[ \t]*(?:[-*+]|\d{1,9}[.)])[ \t]+")
FENCE_DIRECTIVE = re.compile(r"^\{([\w:.-]+)\}")
MYST_INCLUDE = re.compile(r"^\s*(?:`{3,}|~{3,}|:{3,})\s*\{(?:literalinclude|include)\}\s+(\S+)")
RST_DIRECTIVE = re.compile(r"^(\s*)\.\.\s+([\w:.-]+)::(?:\s|$)")
RST_INCLUDE = re.compile(r"^\s*\.\.\s+(?:literalinclude|include)::[ \t]*(\S*)")
INCLUDE_DIRECTIVES = frozenset({"include", "literalinclude"})
# An RST explicit markup line (``..``) that is not a directive, a target, a
# substitution or a footnote is a comment, together with its indented body.
RST_EXPLICIT = re.compile(r"^\s*\.\.(?:\s|$)")
RST_NOT_COMMENT = re.compile(r"^\s*\.\.\s+(?:_|\|[^|]+\|\s|\[[^\]]+\])")
# Inline markup may wrap across lines, but never across a blank line.
INLINE = r"(?:(?!\n[ \t]*\n).)+?"
# Roles the lint knows. A literal role's content is a code span; a download
# role goes to the download rule; a cross-reference names a docs object, not a
# path. Any other ``{word}`` or ``:word:`` before a backtick is not a role.
LITERAL_ROLES = frozenset({"file", "command", "samp", "code", "literal"})
DOWNLOAD_ROLE = "download"
XREF_ROLES = frozenset(
    {
        "abbr",
        "any",
        "attr",
        "class",
        "const",
        "data",
        "doc",
        "emoji",
        "envvar",
        "exc",
        "func",
        "keyword",
        "kbd",
        "meth",
        "mod",
        "numref",
        "obj",
        "option",
        "ref",
        "term",
    }
)
ROLE_NAMES = "|".join(
    [r"[a-z]+:[\w-]+", *sorted(LITERAL_ROLES | XREF_ROLES | {DOWNLOAD_ROLE}, key=len, reverse=True)]
)
ROLE_PATTERN = (
    rf"(?:\{{(?P<curly>{ROLE_NAMES})\}}|(?<![\w:]):(?P<colon>{ROLE_NAMES}):)"
    rf"(?P<ticks>`+)(?!`)(?P<body>{INLINE})(?<!`)(?P=ticks)(?!`)"
)
# A backtick run opens a span unless an odd number of backslashes escape it;
# escaped-backslash pairs before it are part of the match. As in CommonMark, a
# span (or role) closes only on a backtick run of exactly its opener's length.
SPAN_PATTERN = rf"(?<![`\\])(?:\\\\)*(?P<sticks>`+)(?!`)(?P<sbody>{INLINE})(?<!`)(?P=sticks)(?!`)"
# Roles and code spans are read left to right, so a role written inside a
# double-backtick literal is the literal's text, not a role.
INLINE_MARKUP = re.compile(rf"(?P<role>{ROLE_PATTERN})|(?P<span>{SPAN_PATTERN})", re.DOTALL)
LABELLED = re.compile(r"^.*<([^<>]+)>\s*$", re.DOTALL)
SPAN = re.compile(SPAN_PATTERN, re.DOTALL)
# A definition may sit in a blockquote, may open a list item, and its
# destination may wrap to the next line.
QUOTES = r"(?:[ \t]*>[ \t]?)*"
ITEM = r"(?:[ \t]*(?:[-*+]|\d{1,9}[.)])[ \t]+)?"
REFERENCE_DEFINITION = re.compile(
    rf"^{QUOTES}{ITEM} {{0,3}}\[(?!\^)[^\]]+\]:(?:[ \t]+|[ \t]*\n{QUOTES}[ \t]*)<?([^\s>]+)>?",
    re.MULTILINE,
)
DEFINITION_HEAD = re.compile(rf"^{QUOTES} {{0,3}}\[(?!\^)[^\]]+\]:[ \t]*$")
SETEXT_UNDERLINE = re.compile(r"^ {0,3}(?:=+|-+)[ \t]*$")
LINK = re.compile(r"\[[^\]]*\]\(\s*<?([^)\s>]+)>?(?:\s+[\"'][^\"']*[\"'])?\s*\)")
SCHEME = re.compile(r"^[A-Za-z][A-Za-z0-9+.-]*:")
LINE_SUFFIX = re.compile(r":\d+$")
ISSUES = [re.compile(r"\(#\d+(?![\w-])"), re.compile(r"\b[Ii]ssue #\d+")]

PROMPT = re.compile(r"^\s*[$#] ")
ASSIGNMENT = re.compile(r"^[A-Za-z_]\w*=")
TARGET = re.compile(r"[a-z][a-z0-9_-]*")
NUMBER = re.compile(r"[0-9]+(\.[0-9]+)?")
OPERAND_OPTIONS = frozenset(
    {
        "-C",
        "-f",
        "-I",
        "-o",
        "-W",
        "--directory",
        "--file",
        "--makefile",
        "--include-dir",
        "--old-file",
        "--what-if",
    }
)
NUMERIC_OPTIONS = frozenset({"-j", "--jobs", "-l", "--load-average"})
SHORT_OPTION = len("-C")
CHECKED_SEGMENTS = 2
# Commands that run the command after them, and their options that take an operand.
# A short-flag cluster (``-Eu``) takes an operand when its last letter does.
# ``env -S 'make kmodcov'`` (a command inside one string) is not read.
PREFIX_OPERANDS = {
    "sudo": frozenset({"-u", "-g", "-C", "-D", "-h", "-p", "-r", "-t", "-U"}),
    "time": frozenset({"-f", "-o"}),
    "nice": frozenset({"-n"}),
    "env": frozenset({"-u", "-C", "-S"}),
    "command": frozenset(),
    "exec": frozenset({"-a"}),
    "xargs": frozenset({"-a", "-d", "-E", "-e", "-I", "-i", "-L", "-l", "-n", "-P", "-s"}),
}
DIRECTORY_OPTIONS = frozenset({"-C", "--directory"})
FILE_OPTIONS = frozenset({"-f", "--file", "--makefile"})


def owned_tree(checkout: Path, env: dict[str, str] | None = None) -> Tree:
    """Return the tracked tree of *checkout*, from one ``git ls-files``."""
    listed = subprocess.run(  # noqa: S603 — git with fixed arguments, no shell
        ["git", "-C", str(checkout), "ls-files", "-z"],  # noqa: S607
        capture_output=True,
        check=True,
        env=env,
    ).stdout.decode()
    return Tree.of([name for name in listed.split("\0") if name])


def in_scope(rel: str) -> bool:
    """Whether the docs-relative posix path *rel* is a user page the lint reads."""
    if posixpath.splitext(rel)[1] not in SUFFIXES or rel in DEVELOPING_FILES:
        return False
    return not _excluded_dir(rel)


def _excluded_dir(rel: str) -> bool:
    """Whether *rel* lies under a Developing otto or skipped directory."""
    return any(rel.startswith(prefix) for prefix in [*DEVELOPING_DIRS, *SKIP_DIRS])


def is_generated(text: str, suffix: str) -> bool:
    """Whether the page opens with a ``GENERATED`` header."""
    heading = False
    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            continue
        if suffix == ".rst":
            return line.startswith(".. GENERATED")
        if LABEL.match(line):
            continue
        if ATX.match(line) and not heading:
            heading = True
            continue
        return line.startswith("<!-- GENERATED")
    return False


def _uncomment(line: str, *, in_comment: bool, spans: list[_Span]) -> _Visible:
    """Blank one line's commented text, and say whether a comment stays open after it.

    A ``<!--`` inside one of *spans* (code spans, as offsets into *line*) opens nothing.
    """
    visible, pos = "", 0
    while True:
        if in_comment:
            end = line.find(COMMENT_CLOSE, pos)
            if end < 0:
                return _Visible(visible + " " * (len(line) - pos), in_comment=True)
            end += len(COMMENT_CLOSE)
            visible, pos, in_comment = visible + " " * (end - pos), end, False
            continue
        start = next(
            (
                match.start()
                for match in COMMENT_OPEN.finditer(line, pos)
                if not any(span.low <= match.start() < span.high for span in spans)
            ),
            -1,
        )
        if start < 0:
            return _Visible(visible + line[pos:], in_comment=False)
        visible, pos, in_comment = visible + line[pos:start], start, True


def _closes(fence: _Fence, match: re.Match[str]) -> bool:
    """Whether a fence line closes *fence*: same marker, at least as wide, no info."""
    marker = match.group(1)
    return marker[0] == fence.char and len(marker) >= fence.width and not match.group(2).strip()


def _fence_mode(marker: str, info: str) -> str:
    """How a fence's body is read: a plain backtick or tilde fence is code."""
    directive = FENCE_DIRECTIVE.match(info.strip())
    if directive is None:
        return "prose" if marker[0] == ":" else "code"
    if directive.group(1) == "eval-rst":
        return "rst"
    return "code" if directive.group(1) in CODE_DIRECTIVES else "prose"


def _find_spans(text: str) -> list[_Span]:
    """Return the offsets of every code span in *text*."""
    return [_Span(*match.span()) for match in SPAN.finditer(text)]


def _paragraph_spans(paragraph: list[str], lines: list[str], index: int) -> _ParagraphSpans:
    """Scan the code spans of the paragraph that ``lines[index]`` continues, once.

    *paragraph* is the visible text of the paragraph's earlier lines; the
    paragraph runs on to the next blank or fence line.
    """
    covered = [lines[index]]
    for line in lines[index + 1 :]:
        if not line.strip() or FENCE.match(line):
            break
        covered.append(line)
    before = "".join(f"{line}\n" for line in paragraph)
    starts, offset = [], len(before)
    for line in covered:
        starts.append(offset)
        offset += len(line) + 1
    spans = _find_spans(before + "\n".join(covered))
    return _ParagraphSpans(
        index, starts, [span.low for span in spans], [span.high for span in spans]
    )


def _unquote(line: str, quotes: int) -> str:
    """Strip up to *quotes* blockquote markers from the front of *line*."""
    for _ in range(quotes):
        marker = BLOCKQUOTE.match(line)
        if marker is None:
            break
        line = line[marker.end() :]
    return line


def _container_body(line: str) -> _Container:
    """Split off a line's block containers: its blockquote count and the rest.

    A list marker goes too, so a fence that opens a list item is still a fence.
    """
    quotes = 0
    while (marker := BLOCKQUOTE.match(line)) is not None:
        line, quotes = line[marker.end() :], quotes + 1
    return _Container(quotes, LIST_MARKER.sub("", line, count=1))


def _md_lines(lines: list[str]) -> _Classified:
    """Blank a Markdown page's HTML comments and classify each line by its fences.

    The invariant that shapes this pass: a ``<!--`` inside a code span opens
    nothing, and a code span may wrap lines but never crosses a blank line.
    So whether a marker opens a comment depends on the whole paragraph around
    it, not on its line; the span scan is done once per paragraph and cached
    for the lines it covers. Comments and fences are read in one pass, so a
    fence line inside a comment opens nothing and a marker inside code opens
    nothing. A fence may sit in a blockquote or open a list item; its body
    lines lose the blockquote markers it sits under.
    """
    out = _Classified([], [])
    stack: list[_Fence] = []
    rst: list[str] = []
    paragraph: list[str] = []
    cache: _ParagraphSpans | None = None
    in_comment = False
    for index, line in enumerate(lines):
        top = stack[-1] if stack else None
        if top and top.mode != "prose" and not in_comment:
            body = _unquote(line, top.quotes)
            match = FENCE.match(body)
            if match and _closes(top, match):
                stack.pop()
                _flush_rst(out, rst)
                rst = []
                out.lines.append(body)
                out.kinds.append("directive")
            elif top.mode == "rst":
                rst.append(body)
            else:
                out.lines.append(body)
                out.kinds.append("code")
            continue
        spans: list[_Span] = []
        if not in_comment:
            if cache is None or not cache.covers(index):
                cache = _paragraph_spans(paragraph, lines, index)
            spans = cache.ranges(index, len(line))
        visible = _uncomment(line, in_comment=in_comment, spans=spans)
        in_comment = visible.in_comment
        if in_comment or visible.text != line:
            cache = None  # a comment changed the visible text; rescan from the next line
        quotes, body = _container_body(visible.text)
        match = FENCE.match(body)
        if top and match and _closes(top, match):
            stack.pop()
            out.lines.append(body)
            out.kinds.append("directive")
        elif match and not (match.group(1)[0] == "`" and "`" in match.group(2)):
            marker = match.group(1)
            mode = _fence_mode(marker, match.group(2))
            stack.append(_Fence(marker[0], len(marker), mode, quotes))
            out.lines.append(body)
            out.kinds.append("directive")
        else:
            out.lines.append(visible.text)
            out.kinds.append("prose")
        if match or not visible.text.strip():
            paragraph, cache = [], None
        else:
            paragraph.append(visible.text)
    _flush_rst(out, rst)
    return out


def _flush_rst(out: _Classified, rst: list[str]) -> None:
    """Append a finished ``{eval-rst}`` body, read with the RST rules."""
    body = _rst_lines(rst)
    out.lines.extend(body.lines)
    out.kinds.extend(body.kinds)


def _indent(line: str) -> int:
    """Return the width of the line's leading whitespace."""
    return len(line) - len(line.lstrip())


def _block_end(lines: list[str], start: int, base: int) -> int:
    """Return the index after the last line, from *start*, indented deeper than *base*."""
    end = start
    for index in range(start, len(lines)):
        if lines[index].strip():
            if _indent(lines[index]) <= base:
                break
            end = index + 1
    return end


def _rst_comment_end(lines: list[str], index: int) -> int:
    """Return the index after the RST comment that starts at ``lines[index]``."""
    line = lines[index]
    bare = not line.strip()[2:].strip()
    if bare and (index + 1 >= len(lines) or not lines[index + 1].strip()):
        return index + 1  # an empty comment ends at the blank line after it
    return max(index + 1, _block_end(lines, index + 1, _indent(line)))


def _rst_lines(lines: list[str]) -> _Classified:
    """Classify each RST line, and blank RST comments.

    Code directives and literal blocks are code. A comment and its indented
    body are invisible, as an HTML comment is in Markdown. An include
    directive's argument may sit on the next indented line.
    """
    out = _Classified(list(lines), [])
    index = 0
    while index < len(lines):
        line = lines[index]
        directive = RST_DIRECTIVE.match(line)
        if directive is None and RST_EXPLICIT.match(line) and not RST_NOT_COMMENT.match(line):
            end = _rst_comment_end(lines, index)
            out.lines[index:end] = [""] * (end - index)
            out.kinds.extend(["prose"] * (end - index))
            index = end
            continue
        out.kinds.append("prose" if directive is None else "directive")
        index += 1
        if directive is not None and directive.group(2) in INCLUDE_DIRECTIVES:
            wrapped = index < len(lines) and _indent(lines[index]) > _indent(line)
            if not line[directive.end() :].strip() and wrapped and lines[index].strip():
                out.kinds.append("directive")
                index += 1
            continue
        if directive is not None and directive.group(2) not in CODE_DIRECTIVES:
            continue
        if directive is None and not line.rstrip().endswith("::"):
            continue
        end = _block_end(lines, index, _indent(line))
        out.kinds.extend(["code"] * (end - index))
        index = end
    return out


def _regions(text: str, suffix: str) -> list[Region]:
    """Split a page into prose, code and directive regions.

    Markdown pages lose their HTML comments here, and RST pages their RST
    comments; RST has no HTML comments.
    """
    lines = text.splitlines()
    classified = _rst_lines(lines) if suffix == ".rst" else _md_lines(lines)
    lines, kinds = classified.lines, classified.kinds
    regions: list[Region] = []
    for number, (line, kind) in enumerate(zip(lines, kinds, strict=True), start=1):
        if regions and regions[-1].kind == kind:
            regions[-1].lines.append(line)
        else:
            regions.append(Region(kind, number, [line]))
    return regions


def _blank(match: re.Match[str]) -> str:
    """Replace a match with spaces, keeping its newlines so offsets keep their lines."""
    return re.sub(r"[^\n]", " ", match.group(0))


def _prose(numbered: list[_Numbered]) -> _Prose:
    """Join a prose region into one text, so inline markup may wrap across lines."""
    return _Prose(numbered[0].number, "\n".join(line.text for line in numbered))


def _role_name(markup: re.Match[str]) -> str:
    """Return the name of a matched role, in either syntax."""
    return markup.group("curly") or markup.group("colon")


def _markup(prose: _Prose) -> list[re.Match[str]]:
    """Find the roles and code spans in a prose region, left to right."""
    return list(INLINE_MARKUP.finditer(prose.text))


def _prose_spans(prose: _Prose) -> list[_Numbered]:
    """Find the code in a prose region, by line: code spans and literal roles."""
    found = []
    for markup in _markup(prose):
        if markup.group("span") is not None:
            found.append(_Numbered(markup.start(), markup.group("sbody")))
        elif _role_name(markup) in LITERAL_ROLES:
            found.append(_Numbered(markup.start(), markup.group("body")))
    return [_Numbered(prose.line_of(offset), " ".join(body.split())) for offset, body in found]


def _prose_rest(prose: _Prose) -> str:
    """Return a prose region's text with its roles and code spans blanked out."""
    return INLINE_MARKUP.sub(_blank, prose.text)


def _logical_lines(numbered: list[_Numbered]) -> Iterator[_Numbered]:
    """Join lines ending in a backslash, numbering each by its first line."""
    start, parts = 0, []
    for number, line in numbered:
        if not parts:
            start = number
        if line.rstrip().endswith("\\"):
            parts.append(line.rstrip()[:-1])
            continue
        parts.append(line)
        yield _Numbered(start, " ".join(parts))
        parts = []
    if parts:
        yield _Numbered(start, " ".join(parts))


def _resolve(page: _Page, target: str, *, rooted: bool) -> str:
    """Return the checkout-relative posix path a citation lands on."""
    target = target.split("#", 1)[0]
    base = page.docs_root if rooted and target.startswith("/") else page.directory
    if rooted:
        target = target.lstrip("/")
    full = os.path.normpath(base / target)
    return Path(os.path.relpath(full, page.checkout)).as_posix()


def _under(rel: str, prefixes: list[str]) -> bool:
    """Whether the checkout-relative *rel* lies under one of *prefixes*."""
    return any(f"{rel}/".startswith(prefix) for prefix in prefixes)


def _include(page: _Page, numbered: list[_Numbered]) -> list[Offense]:
    """Rule ``include``: include targets must land in examples or shipped sources."""
    offenses = []
    for index, (number, line) in enumerate(numbered):
        match = MYST_INCLUDE.match(line) or RST_INCLUDE.match(line)
        if match is None:
            continue
        target = match.group(1)
        if not target and index + 1 < len(numbered):
            target = numbered[index + 1].text.strip()  # an RST argument on the next line
        if target and not _under(_resolve(page, target, rooted=True), INCLUDE_ALLOWED):
            detail = f"{target} is outside docs/examples/ and src/otto/"
            offenses.append(Offense(page.rel, number, "include", detail))
    return offenses


def _download(page: _Page, numbered: list[_Numbered]) -> list[Offense]:
    """Rule ``download``: downloads must be examples or their built archives."""
    offenses = []
    prose = _prose(numbered)
    for role in _markup(prose):
        if role.group("role") is None or _role_name(role) != DOWNLOAD_ROLE:
            continue
        labelled = LABELLED.match(role.group("body"))
        target = (labelled.group(1) if labelled else role.group("body")).strip()
        if not _under(_resolve(page, target, rooted=True), DOWNLOAD_ALLOWED):
            detail = f"{target} is not an example or an example archive"
            offenses.append(Offense(page.rel, prose.line_of(role.start()), "download", detail))
    return offenses


def _starts_a_block(text: str, offset: int) -> bool:
    """Whether a definition at *offset* starts a block.

    It does at the start of the text, after a blank line, an ATX heading or
    a setext underline, or after a run of definitions that itself starts a block.
    A definition whose destination wrapped to its own line counts as one entry.
    Blockquote markers are not part of a line's content, and a definition that
    opens a list item always starts that item's block.
    """
    stop = text.find("\n", offset)
    line = text[offset : len(text) if stop < 0 else stop]
    if LIST_MARKER.match(_unquote(line, line.count(">"))):
        return True
    end = offset - 1
    while end >= 0:
        begin = text.rfind("\n", 0, end) + 1
        previous = (
            _container_body(text[begin:end]).body if ">" in text[begin:end] else text[begin:end]
        )
        if not previous.strip() or ATX.match(previous.strip()) or SETEXT_UNDERLINE.match(previous):
            return True
        if REFERENCE_DEFINITION.match(previous):
            end = begin - 1
            continue
        head = text.rfind("\n", 0, begin - 1) + 1 if begin > 0 else -1
        if head < 0 or not DEFINITION_HEAD.match(text[head : begin - 1]):
            return False
        end = head - 1  # previous is the wrapped destination of the definition above it
    return True


def _link(page: _Page, numbered: list[_Numbered]) -> list[Offense]:
    """Rule ``link``: a relative link must not land in the checkout outside docs and sources."""
    offenses = []
    prose = _prose(numbered)
    rest = _prose_rest(prose)
    definitions = REFERENCE_DEFINITION.finditer(rest) if page.rel.endswith(".md") else []
    starts = [match for match in definitions if _starts_a_block(prose.text, match.start())]
    matches = sorted([*LINK.finditer(rest), *starts], key=re.Match.start)
    for match in matches:
        target = match.group(1)
        if SCHEME.match(target) or target.startswith(("//", "#")):
            continue
        rel = _resolve(page, target, rooted=False)
        if rel == ".." or rel.startswith("../") or _under(rel, LINK_ALLOWED):
            continue
        detail = f"{target} links into otto's checkout ({rel})"
        offenses.append(Offense(page.rel, prose.line_of(match.start()), "link", detail))
    return offenses


def normalize(candidate: str) -> str | None:
    """Return a cited path in canonical form, or None when it is not one to check."""
    path = candidate.strip("'\"`").lstrip("(").rstrip(",.;:)").strip("'\"`")
    path = LINE_SUFFIX.sub("", path.split("::", 1)[0])
    if not path or any(char in path for char in "{$<") or "://" in path:
        return None
    if path.startswith(("~", "/")):
        return None
    normal = posixpath.normpath(path)
    if normal in {".", ".."} or normal.startswith("../"):
        return None
    return f"{normal}/" if path.endswith("/") else normal


def _otto_owns(path: str, tree: Tree) -> bool:
    """Whether the normalized *path* is otto's own, by rules (a) and (b)."""
    segments = path.rstrip("/").split("/")
    if segments[0] == "scripts" and path in tree.files:
        return True
    directories = segments if path.endswith("/") else segments[:-1]
    if len(directories) < CHECKED_SEGMENTS or path.startswith(("docs/examples/", "src/otto/")):
        return False
    first, second = directories[0], directories[1]
    if any(char in first + second for char in "*?["):
        return False
    return f"{first}/{second}" in tree.dirs


def _in_an_example(path: str, examples: list[Path]) -> bool:
    """Whether some example project holds *path*: then it is the reader's."""
    return any(_holds(project, path.rstrip("/")) for project in examples)


def _holds(project: Path, pattern: str) -> bool:
    """Whether *pattern* matches anything under *project*."""
    try:
        return next(iter(project.glob(pattern)), None) is not None
    except ValueError:
        return False


def _option_value(word: str) -> str:
    """Return the value of a ``--opt=value`` or ``NAME=value`` word, where its path is."""
    if (word.startswith("-") and "=" in word) or ASSIGNMENT.match(word):
        return word.split("=", 1)[1]
    return word


def _path(page: _Page, numbered: list[_Numbered], *, code: bool) -> list[Offense]:
    """Rule ``path``: spans in prose and words in code must not name otto's own paths."""
    pieces = numbered if code else _prose_spans(_prose(numbered))
    candidates = [
        _Numbered(number, _option_value(word)) for number, piece in pieces for word in piece.split()
    ]
    offenses = []
    for number, candidate in candidates:
        path = normalize(candidate)
        if path is None or not _otto_owns(path, page.tree):
            continue
        if _in_an_example(path, page.examples):
            continue
        detail = f"{candidate} is otto's own ({path})"
        offenses.append(Offense(page.rel, number, "path", detail))
    return offenses


def _make_operand_is_other(option: str, operand: str) -> bool:
    """Whether a ``-C`` or ``-f`` operand points make at a Makefile other than otto's."""
    if option in DIRECTORY_OPTIONS:
        return posixpath.normpath(operand) != "."
    if option in FILE_OPTIONS:
        return posixpath.normpath(operand) != "Makefile"
    return False


def _attached_is_other(word: str) -> bool:
    """Ask the same question of attached forms: ``-Cdir``, ``--file=x``."""
    if word.startswith("--") and "=" in word:
        option, operand = word.split("=", 1)
        return _make_operand_is_other(option, operand)
    option, operand = word[:SHORT_OPTION], word[SHORT_OPTION:]
    if not word.startswith("--") and operand:
        return _make_operand_is_other(option, operand)
    return False


def _invocation_targets(words: list[str]) -> list[str]:
    """Return one make invocation's target words, or none when it runs another Makefile."""
    targets: list[str] = []
    other = False
    index = 0
    while index < len(words):
        word = words[index]
        index += 1
        if ASSIGNMENT.match(word):
            continue
        if word in OPERAND_OPTIONS:
            operand = words[index] if index < len(words) else ""
            index += 1
            other = other or _make_operand_is_other(word, operand)
        elif word in NUMERIC_OPTIONS:
            if index < len(words) and NUMBER.fullmatch(words[index]):
                index += 1
        elif word.startswith("-"):
            other = other or _attached_is_other(word)
        elif TARGET.fullmatch(word):
            targets.append(word)
    return [] if other else targets


def _shell_words(command: str) -> list[str]:
    """Split one command into shell words; an unclosed quote falls back to whitespace."""
    try:
        return shlex.split(command, comments=True, posix=True)
    except ValueError:
        return [word.strip("'\"") for word in command.split()]


def _command_starts(line: str) -> list[int]:
    """Where commands start in *line*: its start, and after each separator.

    A backtick substitution starts one too. Separators inside quotes, and
    anything after a ``#`` comment, start nothing.
    """
    starts, quote, index = [0], "", 0
    while index < len(line):
        char = line[index]
        if quote:
            if char == quote:
                quote = ""
            elif char == "\\" and quote == '"':
                index += 1
        elif char in "'\"":
            quote = char
        elif char == "\\":
            index += 1
        elif char == "#" and (index == 0 or line[index - 1].isspace()):
            break
        elif char in ";|(`":
            starts.append(index + 1)
        elif line.startswith("&&", index):
            starts.append(index + 2)
            index += 1
        index += 1
    return starts


def _takes_operand(flag: str, operands: frozenset[str]) -> bool:
    """Whether a prefix's *flag*, or the last letter of a short-flag cluster, takes an operand."""
    if flag in operands:
        return True
    return not flag.startswith("--") and len(flag) > SHORT_OPTION and f"-{flag[-1]}" in operands


def _make_words(words: list[str]) -> list[str]:
    """Return the target words when *words* run make, past assignments and prefixes."""
    index = 0
    while index < len(words) and ASSIGNMENT.match(words[index]):
        index += 1
    while index < len(words) and words[index] in PREFIX_OPERANDS:
        operands = PREFIX_OPERANDS[words[index]]
        index += 1
        while index < len(words) and (
            words[index].startswith("-") or ASSIGNMENT.match(words[index])
        ):
            index += 2 if _takes_operand(words[index], operands) else 1
    if index < len(words) and words[index] == "make":
        return _invocation_targets(words[index + 1 :])
    return []


def _command_end(text: str) -> int:
    """Where the command in *text* ends: a separator or backtick outside quotes and ``$(…)``."""
    depth, quote, index = 0, "", 0
    while index < len(text):
        char = text[index]
        if quote:
            if char == quote:
                quote = ""
            elif char == "\\" and quote == '"':
                index += 1
        elif char in "'\"":
            quote = char
        elif char == "\\":
            index += 1
        elif char == "(":
            depth += 1
        elif char == ")":
            if depth == 0:
                return index
            depth -= 1
        elif depth == 0 and (char in ";|`" or text.startswith("&&", index)):
            return index
        index += 1
    return len(text)


def make_targets(code: str) -> list[str]:
    """Every checked target word of every make invocation in *code*."""
    targets: list[str] = []
    numbered = [_Numbered(number, line) for number, line in enumerate(code.splitlines(), start=1)]
    for _number, line in _logical_lines(numbered):
        command = PROMPT.sub("", line, count=1)
        for start in _command_starts(command):
            rest = command[start:]
            targets.extend(_make_words(_shell_words(rest[: _command_end(rest)])))
    return targets


def _make(page: _Page, numbered: list[_Numbered], *, code: bool) -> list[Offense]:
    """Rule ``make``: code must not run a maintainer target."""
    pieces = list(_logical_lines(numbered)) if code else _prose_spans(_prose(numbered))
    return [
        Offense(page.rel, number, "make", f"make {target} is a maintainer target")
        for number, piece in pieces
        for target in make_targets(piece)
        if target in MAINTAINER_TARGETS
    ]


def _issue(page: _Page, numbered: list[_Numbered]) -> list[Offense]:
    """Rule ``issue``: no issue references anywhere visible."""
    return [
        Offense(page.rel, number, "issue", f"issue reference {match.group(0)!r}")
        for number, line in numbered
        for pattern in ISSUES
        for match in pattern.finditer(line)
    ]


def _example_projects(docs_root: Path) -> list[Path]:
    """List the example project directories under ``docs/examples/``."""
    examples = docs_root / "examples"
    if not examples.is_dir():
        return []
    return sorted(path for path in examples.iterdir() if path.is_dir())


def page_offenses(
    page: Path,
    docs_root: Path,
    checkout: Path,
    tree: Tree,
    *,
    examples: list[Path] | None = None,
) -> list[Offense]:
    """Every offense on one page, in line order; a generated page has none."""
    text = page.read_text(encoding="utf-8")
    if is_generated(text, page.suffix):
        return []
    context = _Page(
        rel=page.relative_to(docs_root).as_posix(),
        directory=page.parent,
        docs_root=docs_root,
        checkout=checkout,
        tree=tree,
        examples=_example_projects(docs_root) if examples is None else examples,
    )
    offenses: list[Offense] = []
    for region in _regions(text, page.suffix):
        numbered = [
            _Numbered(number, line) for number, line in enumerate(region.lines, region.first_line)
        ]
        if region.kind == "directive":
            offenses += _include(context, numbered)
        elif region.kind == "code":
            offenses += _path(context, numbered, code=True)
            offenses += _make(context, numbered, code=True)
        else:
            offenses += _download(context, numbered)
            offenses += _link(context, numbered)
            offenses += _path(context, numbered, code=False)
            offenses += _make(context, numbered, code=False)
        offenses += _issue(context, numbered)
    return sorted(offenses, key=lambda offense: offense.line)


def scan(docs_root: Path, checkout: Path, tree: Tree | None = None) -> list[Offense]:
    """Every offense on every in-scope page under *docs_root*."""
    owned = owned_tree(checkout) if tree is None else tree
    examples = _example_projects(docs_root)
    offenses: list[Offense] = []
    for dirpath, dirnames, filenames in os.walk(docs_root):
        rel_dir = Path(dirpath).relative_to(docs_root).as_posix()
        prefix = "" if rel_dir == "." else f"{rel_dir}/"
        dirnames[:] = sorted(name for name in dirnames if not _excluded_dir(f"{prefix}{name}/"))
        for name in sorted(filenames):
            if in_scope(f"{prefix}{name}"):
                page = Path(dirpath) / name
                offenses += page_offenses(page, docs_root, checkout, owned, examples=examples)
    return offenses


def _finding(offense: Offense) -> str:
    """One finding as an editor-clickable line."""
    return f"docs/{offense.page}:{offense.line}: {offense.rule}: {offense.detail}"


def check(offenses: list[Offense], exempt: dict[str, Exemption]) -> list[str]:
    """Failure lines for *offenses* under *exempt*; an empty list passes."""
    by_page: dict[str, list[Offense]] = {}
    for offense in offenses:
        by_page.setdefault(offense.page, []).append(offense)
    failures: list[str] = []
    for page in sorted(set(by_page) | set(exempt)):
        found = by_page.get(page, [])
        exemption = exempt.get(page)
        if exemption is None:
            failures += [_finding(offense) for offense in found]
            continue
        count, ceiling, track = len(found), exemption.ceiling, exemption.track
        if count == 0:
            failures.append(f"docs/{page}: clean now; remove its exemption ({track})")
        elif count > ceiling:
            over = f"{count} findings, over its ceiling of {ceiling} ({track})"
            failures.append(f"docs/{page}: {over}; fix the new one")
            failures += [_finding(offense) for offense in found]
        elif count < ceiling:
            lower = f"lower its ceiling from {ceiling} to {count}"
            failures.append(f"docs/{page}: {count} findings; {lower}")
    return failures


def main(argv: list[str]) -> int:
    """Lint otto's docs; print the failures and return the exit status."""
    if argv:
        print("usage: lint_docs_citations.py", file=sys.stderr)
        return 2
    failures = check(scan(ROOT / "docs", ROOT), EXEMPT)
    for line in failures:
        print(line)
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
