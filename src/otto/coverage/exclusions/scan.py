"""One lexical pass per source file, shared by every rule kind.

Markers live inside comments, so the source lines themselves are never
stripped; the lexer only tracks block-comment state well enough to know
that a ``#if`` inside ``/* ... */`` is not a directive. A directive's own
comments ARE dropped from its stored condition — a macro named in
``#ifdef PROD /* not DEBUG_LOG */`` is a remark, not a reference.
"""

import re
from dataclasses import dataclass, field

from .parity import references_positively
from .rules import ExclusionRule, MarkerRule, PreprocessorRule, RegexRule

_DIRECTIVE_RE = re.compile(r"^\s*#\s*(if|ifdef|ifndef|elif|else|endif)\b(.*)$")
_OPENERS = ("if", "ifdef", "ifndef")


@dataclass(frozen=True)
class Directive:
    """One preprocessor directive, continuations already joined."""

    lineno: int
    end_lineno: int
    keyword: str
    condition: str
    depth: int


@dataclass
class LexedSource:
    """Physical lines plus every directive found outside a block comment."""

    lines: list[str] = field(default_factory=list)
    directives: list[Directive] = field(default_factory=list)


@dataclass
class ExclusionMap:
    """Line numbers a file's rules remove, split by stat."""

    lines: set[int] = field(default_factory=set)
    branch_lines: set[int] = field(default_factory=set)


def _strip_block_comments(lines: list[str]) -> list[bool]:
    """Return, per line, whether the line STARTS inside a block comment."""
    starts_in_comment: list[bool] = []
    in_comment = False
    for line in lines:
        starts_in_comment.append(in_comment)
        index = 0
        while index < len(line):
            if in_comment:
                end = line.find("*/", index)
                if end == -1:
                    break
                in_comment = False
                index = end + 2
                continue
            begin = line.find("/*", index)
            line_comment = line.find("//", index)
            if line_comment != -1 and (begin == -1 or line_comment < begin):
                break
            if begin == -1:
                break
            in_comment = True
            index = begin + 2
    return starts_in_comment


def _strip_comments(text: str) -> str:
    """Remove ``/* ... */`` spans and any ``// ...`` tail from *text*.

    Trailing comments on directives are ordinary in embedded C
    (``#endif /* CONFIG_FOO */``), and a macro name mentioned in one is a
    remark, not a reference. Leaving it in ``condition`` would let the
    macro-parity form match a comment and exclude a production arm.

    An unterminated ``/*`` swallows the rest of the directive; the lexer's
    own block-comment state already knows the following lines are inside
    the comment.
    """
    kept: list[str] = []
    index = 0
    while index < len(text):
        block = text.find("/*", index)
        line = text.find("//", index)
        if line != -1 and (block == -1 or line < block):
            kept.append(text[index:line])
            return "".join(kept)
        if block == -1:
            kept.append(text[index:])
            break
        kept.append(text[index:block])
        end = text.find("*/", block + 2)
        if end == -1:
            break
        index = end + 2
    return "".join(kept)


def _physical_lines(source: str) -> "list[str]":
    r"""Split *source* into lines the way gcov counts them: on ``\n``, only.

    ``str.splitlines`` also breaks on form feed, vertical tab, ``\x1c``-``\x1e``
    and the Unicode line separators. A form feed is ordinary in GNU-style C —
    the kernel uses them as page separators, and a compiler sees plain
    whitespace — so one ``\f`` would number every following line one past the
    number gcov recorded, for every rule kind. ``apply.py`` deletes records BY
    line number, which turns that shift into coverage deleted off the wrong
    lines rather than a mis-coloured report.

    A trailing newline terminates the last line rather than opening an empty
    one, and a ``\r`` ahead of it belongs to the line ending, not to the line's
    text: a rule anchored with ``$`` has to match a CRLF file the same way.
    """
    lines = source.split("\n")
    if lines and lines[-1] == "":
        lines.pop()
    return [line.removesuffix("\r") for line in lines]


def lex_source(source: str) -> LexedSource:
    """Lex *source* into physical lines plus normalized directives."""
    lines = _physical_lines(source)
    starts_in_comment = _strip_block_comments(lines)

    directives: list[Directive] = []
    depth = 0
    index = 0
    while index < len(lines):
        if starts_in_comment[index]:
            index += 1
            continue
        match = _DIRECTIVE_RE.match(lines[index])
        if match is None:
            index += 1
            continue

        keyword = match.group(1)
        rest = match.group(2)
        start = index
        while rest.rstrip().endswith("\\") and index + 1 < len(lines):
            rest = rest.rstrip()[:-1]
            index += 1
            rest += " " + lines[index]
        condition = " ".join(_strip_comments(rest).split())

        if keyword == "endif":
            depth = max(0, depth - 1)
            directives.append(Directive(start + 1, index + 1, keyword, "", depth))
        elif keyword in _OPENERS:
            directives.append(Directive(start + 1, index + 1, keyword, condition, depth))
            depth += 1
        else:  # elif / else — same depth as their opener
            directives.append(
                Directive(start + 1, index + 1, keyword, condition, max(0, depth - 1))
            )
        index += 1

    return LexedSource(lines=lines, directives=directives)


def normalized_directive(directive: Directive) -> str:
    """Render a directive back to ``#<keyword> <condition>`` for matching."""
    return f"#{directive.keyword} {directive.condition}".rstrip()


def arm_extent(lexed: LexedSource, index: int) -> range:
    """Physical lines of the arm opened by ``lexed.directives[index]``.

    Runs from the directive's first line through the line before the next
    directive at the same depth or shallower. The opening directive is
    included (display consistency — it is never executable); the terminator
    is not.

    A well-formed file always terminates an arm with a directive at exactly
    the opener's depth, so the ``<=`` is defensive breadth against malformed
    nesting rather than a distinct case: the lexer floors depth at 0, which
    leaves no input on which a strict ``==`` would pick a different
    terminator.
    """
    directive = lexed.directives[index]
    for candidate in lexed.directives[index + 1 :]:
        if candidate.depth <= directive.depth:
            return range(directive.lineno, candidate.lineno)
    return range(directive.lineno, len(lexed.lines) + 1)


def _preprocessor_arm_matches(directive: Directive, rule: PreprocessorRule) -> bool:
    """Report whether this directive's own condition satisfies *rule*.

    ``#else`` and ``#endif`` carry no condition, so no rule can name them —
    which is what keeps a production fallback out of the denominator.
    """
    if directive.keyword in ("else", "endif"):
        return False
    if rule.pattern is not None:
        return rule.pattern.search(normalized_directive(directive)) is not None
    return references_positively(directive.keyword, directive.condition, rule.macros)


def _apply_preprocessor_rule(lexed: LexedSource, rule: PreprocessorRule) -> "set[int]":
    hits: set[int] = set()
    for index, directive in enumerate(lexed.directives):
        if _preprocessor_arm_matches(directive, rule):
            hits.update(arm_extent(lexed, index))
    return hits


def _marker_events(
    text: str, marker_rules: "list[MarkerRule]"
) -> "dict[int, list[tuple[int, str]]]":
    """Per-rule ``(position, kind)`` events, longest token first ACROSS rules.

    Longest-first has to be resolved globally, not per rule. Base ``A``
    derives the start token ``A_START``, which is a substring of base
    ``A_START``'s line token ``A_START_LINE``. Resolving per rule would let
    ``A`` open a block on a line that only ever named ``A_START``'s line
    marker, silently swallowing everything down to the next stop.

    A claim is refused on any OVERLAP, not merely on containment. Bases
    ``A`` and ``START`` are the case: in ``// A_START_LINE``, ``START_LINE``
    claims [5, 15) and ``A_START`` sits at [3, 10), overlapping it without
    being inside it. A containment test passes that token through and
    ``A`` fires a start event, which is the same silent mass exclusion in a
    partial-overlap shape. Their derived token sets do not intersect, so
    ``_check_marker_collisions`` cannot reject the pair at config time —
    this predicate is the only thing standing between them.
    """
    candidates: list[tuple[str, int, str]] = []
    for rule_index, rule in enumerate(marker_rules):
        for kind, token in rule.tokens().items():
            candidates.append((token, rule_index, kind))
    candidates.sort(key=lambda candidate: len(candidate[0]), reverse=True)

    claimed: list[tuple[int, int]] = []
    events: dict[int, list[tuple[int, str]]] = {i: [] for i in range(len(marker_rules))}
    for token, rule_index, kind in candidates:
        start = 0
        while True:
            position = text.find(token, start)
            if position == -1:
                break
            end = position + len(token)
            if not any(position < hi and lo < end for lo, hi in claimed):
                claimed.append((position, end))
                events[rule_index].append((position, kind))
            start = end
    for rule_events in events.values():
        rule_events.sort(key=lambda event: event[0])
    return events


def _apply_marker_rules(lexed: LexedSource, marker_rules: "list[MarkerRule]") -> "list[set[int]]":
    """Return, per rule, the lines it excludes (block bounds inclusive).

    Every marker rule advances together in one pass, because token
    resolution is global (see :func:`_marker_events`); each rule still keeps
    its own block state.
    """
    results: list[set[int]] = [set() for _ in marker_rules]
    in_block = [False] * len(marker_rules)
    for offset, text in enumerate(lexed.lines):
        lineno = offset + 1
        events = _marker_events(text, marker_rules)
        for rule_index in range(len(marker_rules)):
            line_excluded = in_block[rule_index]
            for _position, kind in events[rule_index]:
                if kind == "line":
                    line_excluded = True
                elif kind == "stop":
                    if in_block[rule_index]:
                        in_block[rule_index] = False
                        line_excluded = True
                elif kind == "start":
                    in_block[rule_index] = True
                    line_excluded = True
            if line_excluded:
                results[rule_index].add(lineno)
    return results


def _route(result: ExclusionMap, rule: ExclusionRule, hits: "set[int]") -> None:
    """Add *hits* to whichever set *rule*'s stat names."""
    target = result.branch_lines if rule.stat == "branch" else result.lines
    target |= hits


def scan_source(source: str, rules: "list[ExclusionRule]") -> ExclusionMap:
    """Resolve every non-path rule against *source*.

    Rules union; a line excluded at ``stat="line"`` subsumes any
    branch-level exclusion of the same line (applied in ``apply.py``).
    """
    lexed = lex_source(source)
    result = ExclusionMap()

    marker_rules = [r for r in rules if isinstance(r, MarkerRule)]
    for rule, hits in zip(marker_rules, _apply_marker_rules(lexed, marker_rules), strict=True):
        _route(result, rule, hits)

    for rule in rules:
        if isinstance(rule, PreprocessorRule):
            _route(result, rule, _apply_preprocessor_rule(lexed, rule))
        elif isinstance(rule, RegexRule):
            _route(
                result,
                rule,
                {
                    offset + 1
                    for offset, text in enumerate(lexed.lines)
                    if rule.pattern.search(text)
                },
            )

    return result
