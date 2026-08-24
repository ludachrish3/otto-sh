"""Exclusion rule models and the ``[coverage.exclusions]`` loader.

A rule is a pure value: a compiled matcher plus the stat it removes
(``"line"`` deletes the line and its branches, ``"branch"`` clears only the
branch records). Resolution against source lives in ``scan.py``; mutation of
the store lives in ``apply.py``.
"""

import re
from dataclasses import dataclass, field
from typing import Any

from ..errors import CoverageConfigError

STATS = ("line", "branch")


@dataclass(frozen=True, kw_only=True)
class ExclusionRule:
    """Base for every rule kind.

    ``kw_only`` because subclasses add non-defaulted fields after this
    class's defaulted ``stat``; positional dataclass inheritance would
    reject that ordering.
    """

    stat: str = "line"


@dataclass(frozen=True, kw_only=True)
class MarkerRule(ExclusionRule):
    """A marker family named by its base, deriving members lcov's way."""

    name: str

    def tokens(self) -> dict[str, str]:
        """Return the derived ``line`` / ``start`` / ``stop`` marker strings."""
        infix = "_BR" if self.stat == "branch" else ""
        return {
            "line": f"{self.name}{infix}_LINE",
            "start": f"{self.name}{infix}_START",
            "stop": f"{self.name}{infix}_STOP",
        }


@dataclass(frozen=True, kw_only=True)
class PreprocessorRule(ExclusionRule):
    """Excludes the arm whose own directive matches.

    Exactly one of *pattern* (regex over the normalized directive line) or
    *macros* (positive-reference matching) is set.
    """

    pattern: "re.Pattern[str] | None" = None
    macros: list[str] = field(default_factory=list)


@dataclass(frozen=True, kw_only=True)
class PathRule(ExclusionRule):
    """Whole-file exclusion by glob. ``raw_patterns`` is kept for messages."""

    patterns: "list[re.Pattern[str]]"
    raw_patterns: list[str]


@dataclass(frozen=True, kw_only=True)
class RegexRule(ExclusionRule):
    """Excludes any source line the pattern matches."""

    pattern: "re.Pattern[str]"


BUILTIN_MARKER_RULES: list[MarkerRule] = [
    MarkerRule(stat="line", name="LCOV_EXCL"),
    MarkerRule(stat="branch", name="LCOV_EXCL"),
]
"""lcov's own markers, expressed as ordinary rules.

Carrying these as rules rather than hardcoded tuples is the check on the
family model: if the built-ins needed a special case, the model would be
wrong.
"""


def _compile(pattern: str, where: str) -> "re.Pattern[str]":
    try:
        return re.compile(pattern)
    except re.error as e:
        raise CoverageConfigError(
            f"[coverage.exclusions] {where}: invalid regex {pattern!r} ({e})"
        ) from e


def _check_marker_collisions(rules: list[ExclusionRule]) -> None:
    """Reject two marker families whose derived token sets intersect.

    The built-ins are seeded into the same table rather than skipped: the
    engine always carries them, so a user family deriving one of their
    tokens is the identical ambiguity as two user families colliding — one
    string standing for two different stats. ``LCOV_EXCL_BR`` at
    ``stat = "line"`` is the trap, because it derives exactly the tokens
    the built-in *branch* family owns.

    A built-in has no ``rules[N]`` index, so owners are stored as display
    strings and the built-in side names its family instead of faking one.
    """
    owners: dict[str, str] = {
        token: f"the built-in {builtin.name} {builtin.stat} family"
        for builtin in BUILTIN_MARKER_RULES
        for token in builtin.tokens().values()
    }
    for index, rule in enumerate(rules):
        if not isinstance(rule, MarkerRule):
            continue
        for token in rule.tokens().values():
            previous = owners.get(token)
            if previous is not None:
                raise CoverageConfigError(
                    f"[coverage.exclusions] {previous} and rules[{index}] "
                    f"both derive the marker {token!r}; rename one family"
                )
            owners[token] = f"rules[{index}]"


def _load_one(index: int, raw: dict[str, Any]) -> ExclusionRule:
    where = f"rules[{index}]"
    kind = raw.get("kind")
    stat = raw.get("stat", "line")
    if stat not in STATS:
        raise CoverageConfigError(
            f"[coverage.exclusions] {where}: stat must be one of {list(STATS)}, got {stat!r}"
        )

    if kind == "marker":
        name = str(raw["name"])
        if not name or any(c.isspace() for c in name):
            raise CoverageConfigError(
                f"[coverage.exclusions] {where}: marker name must be a non-empty token "
                f"with no whitespace, got {name!r}. The derived members are searched as "
                "bare substrings, so an empty base yields '_LINE' and matches inside "
                "ordinary identifiers"
            )
        return MarkerRule(stat=stat, name=name)
    if kind == "preprocessor":
        pattern = raw.get("pattern")
        macros = list(raw.get("macros") or [])
        if bool(pattern) == bool(macros):
            raise CoverageConfigError(
                f"[coverage.exclusions] {where}: set exactly one of 'pattern' or 'macros'"
            )
        return PreprocessorRule(
            stat=stat,
            pattern=_compile(pattern, where) if pattern else None,
            macros=macros,
        )
    if kind == "path":
        # Local by necessity, not by style: paths.py imports PathRule from this
        # module at module level, so hoisting this one closes the cycle and a
        # `import otto.coverage.exclusions.rules` first would hit a
        # partially-initialised module. PLC0415 is globally ignored, so lint
        # will never argue either way.
        from .paths import glob_to_regex

        raw_patterns = [str(p) for p in raw["patterns"]]
        return PathRule(
            stat=stat,
            patterns=[glob_to_regex(p) for p in raw_patterns],
            raw_patterns=raw_patterns,
        )
    if kind == "regex":
        return RegexRule(stat=stat, pattern=_compile(str(raw["pattern"]), where))

    raise CoverageConfigError(
        f"[coverage.exclusions] {where}: unknown kind {kind!r} "
        "(expected 'marker', 'preprocessor', 'path' or 'regex')"
    )


def load_exclusion_rules(cov_config: dict[str, Any]) -> list[ExclusionRule]:
    """Parse ``[coverage.exclusions].rules`` into compiled rule objects.

    Raises :class:`~otto.coverage.errors.CoverageConfigError` (a
    ``ValueError``) on any malformed rule, naming the offending index.
    """
    raw_rules = (cov_config.get("exclusions") or {}).get("rules") or []
    rules = [_load_one(index, raw) for index, raw in enumerate(raw_rules)]
    _check_marker_collisions(rules)
    return rules
