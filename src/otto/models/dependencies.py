"""Dependency-entry parsing and constraint satisfiability for ``[dependencies]``.

Pure module (stdlib only): deliberately free of ``otto.config`` imports so
``models.settings`` can use it at validation time — like the duplicated
version regex, ``models/`` must never trigger the config package's
import-time surface.

Constraint semantics: clauses compare zero-padded ``(major, minor, patch)``
integer triples; a Version's ``extra`` tag never participates, and the
grammar rejects extra tags inside clauses to keep that promise enforceable.
"""

import re
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Literal, cast

Op = Literal["==", "!=", ">=", "<=", ">", "<"]

_NORMALIZE_RE = re.compile(r"[-_.]+")
_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
_CLAUSE_RE = re.compile(r"^(==|!=|>=|<=|>|<)\s*(\d+(?:\.\d+){0,2})$")
_NAME_PREFIX_RE = re.compile(r"[A-Za-z0-9._-]*")


def normalize_name(name: str) -> str:
    """PEP-503-style normalization: lowercase, collapse ``[-_.]+`` runs to ``-``."""
    return _NORMALIZE_RE.sub("-", name.lower())


@dataclass(frozen=True)
class DependencyClause:
    """One constraint clause: an operator against a zero-padded version triple."""

    op: Op
    version: tuple[int, int, int]

    def matches(self, key: tuple[int, int, int]) -> bool:
        """Check whether *key* (a Version's major/minor/patch triple) satisfies this clause."""
        if self.op == "==":
            return key == self.version
        if self.op == "!=":
            return key != self.version
        if self.op == ">=":
            return key >= self.version
        if self.op == "<=":
            return key <= self.version
        if self.op == ">":
            return key > self.version
        return key < self.version


@dataclass(frozen=True)
class ParsedDependency:
    """One parsed ``[dependencies]`` entry."""

    raw: str
    """The entry as written (stripped)."""

    name: str
    """Declared project name, pre-normalization."""

    normalized: str
    """PEP-503-normalized name used for matching."""

    constraint: str
    """Clause text after the name (``""`` = any version)."""

    clauses: list[DependencyClause]
    """Parsed comma-ANDed clauses (empty = any version)."""

    required: bool
    """True for a ``required`` entry, False for ``optional``."""


def _name_error(entry: str, name: str) -> ValueError:
    message = (
        f"dependency {entry!r}: name {name!r} must start with a letter or digit "
        "and contain only letters, digits, '.', '_' and '-'"
    )
    # Common typo: "name version" with the operator left out entirely, e.g.
    # "vantage 2.1" — the offending text splits (on any whitespace run) into
    # a name-like prefix and a digit-starting remainder. Point at the fix
    # rather than leaving the user to guess why a plain name string got
    # rejected. `name` can also be "" (e.g. entry ">= 1" with nothing before
    # the operator), where `.split(None, 1)` yields `[]` — slicing off the
    # remainder (rather than unpacking) keeps that case hint-free instead of
    # crashing.
    remainder = name.split(None, 1)[1:]
    if remainder and remainder[0][:1].isdigit():
        message += " (hint: constraints need an operator, e.g. 'name == 1.2')"
    return ValueError(message)


def _invalid_clause_error(entry: str, clause_text: str) -> ValueError:
    return ValueError(
        f"dependency {entry!r}: invalid clause {clause_text!r} "
        "(format: <op> N[.N[.N]] with op one of == != >= <= > <; "
        "extra tags are not allowed in constraints)"
    )


def parse_dependency_entry(entry: str, *, required: bool) -> ParsedDependency:
    """Parse ``"name"`` or ``"name <op> N[.N[.N]], ..."``; raise ``ValueError`` if malformed."""
    text = entry.strip()

    name_match = _NAME_PREFIX_RE.match(text)
    name = name_match.group() if name_match else ""
    rest = text[name_match.end() :].lstrip() if name_match else text.lstrip()

    if not _NAME_RE.match(name):
        raise _name_error(entry, name)

    constraint = ""
    if rest:
        if rest[0] in "=!<>":
            constraint = rest
        elif rest[0].isalnum():
            # Whitespace inside what was meant to be the name, e.g. "my lib >= 1":
            # report the name error against everything before the first operator char.
            op_match = re.search(r"[=!<>]", text)
            bad_name = text[: op_match.start()].strip() if op_match else text
            raise _name_error(entry, bad_name)
        else:
            # A malformed operator token (e.g. "~=", "^="): not a name continuation,
            # not a recognized clause operator either.
            raise _invalid_clause_error(entry, rest)

    clauses: list[DependencyClause] = []
    if constraint:
        for part in (p.strip() for p in constraint.split(",")):
            cm = _CLAUSE_RE.match(part)
            if cm is None:
                raise _invalid_clause_error(entry, part)
            nums = [int(x) for x in cm.group(2).split(".")]
            nums += [0] * (3 - len(nums))
            clauses.append(
                DependencyClause(op=cast("Op", cm.group(1)), version=(nums[0], nums[1], nums[2]))
            )
    return ParsedDependency(
        raw=text,
        name=name,
        normalized=normalize_name(name),
        constraint=constraint,
        clauses=clauses,
        required=required,
    )


def clauses_satisfiable(clauses: Sequence[DependencyClause]) -> bool:
    """Check if at least one version triple satisfies every clause.

    Exactly decidable over integer triples: fold clauses into a lower bound,
    an upper bound, ``==`` pins and ``!=`` exclusions. Empty iff the bounds
    cross, pins conflict (with each other, the bounds, or an exclusion), or
    the bounds confine the range to a finite point set (identical
    major.minor) fully covered by exclusions — an upper-unbounded or
    major/minor-spanning range is infinite, which no finite exclusion set
    can empty.
    """
    lo: tuple[int, int, int] = (0, 0, 0)
    lo_inc = True
    hi: tuple[int, int, int] | None = None
    hi_inc = True
    pins = {c.version for c in clauses if c.op == "=="}
    if len(pins) > 1:
        return False
    exclusions = {c.version for c in clauses if c.op == "!="}
    for c in clauses:
        if c.op in (">", ">="):
            inc = c.op == ">="
            if c.version > lo or (c.version == lo and lo_inc and not inc):
                lo, lo_inc = c.version, inc
        elif c.op in ("<", "<="):
            inc = c.op == "<="
            if hi is None or c.version < hi or (c.version == hi and hi_inc and not inc):
                hi, hi_inc = c.version, inc
    if pins:
        pin = next(iter(pins))
        if pin in exclusions:
            return False
        if pin < lo or (pin == lo and not lo_inc):
            return False
        return not (hi is not None and (pin > hi or (pin == hi and not hi_inc)))
    if hi is not None:
        if lo > hi or (lo == hi and not (lo_inc and hi_inc)):
            return False
        if lo[:2] == hi[:2]:
            start = lo[2] + (0 if lo_inc else 1)
            end = hi[2] - (0 if hi_inc else 1)
            if start > end:
                return False
            if end - start + 1 <= len(exclusions) and all(
                (lo[0], lo[1], p) in exclusions for p in range(start, end + 1)
            ):
                return False
    return True
