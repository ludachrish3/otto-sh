"""Glob matching for ``kind = "path"`` rules.

Python's floor here is 3.10, so ``PurePath.full_match`` (3.13+) is
unavailable and ``pathspec`` is not a dependency. This is the in-house
replacement: segment-aware, with ``**`` crossing directory boundaries.
"""

import re
from pathlib import Path

from .rules import PathRule


def glob_to_regex(pattern: str) -> "re.Pattern[str]":
    """Compile a ``/``-separated glob into an anchored regex.

    ``**`` matches across separators, ``*`` and ``?`` do not.
    """
    segments = pattern.split("/")
    parts: list[str] = []
    for index, segment in enumerate(segments):
        last = index == len(segments) - 1
        if segment == "**":
            parts.append(".*" if last else "(?:[^/]+/)*")
            continue
        piece = "".join(
            "[^/]*" if c == "*" else "[^/]" if c == "?" else re.escape(c) for c in segment
        )
        parts.append(piece if last else piece + "/")
    return re.compile(r"\A" + "".join(parts) + r"\Z")


def path_rule_matches(path: Path, root: Path, rule: PathRule) -> bool:
    """Report whether *path* matches any of *rule*'s globs.

    A glob beginning with ``/`` is matched against the absolute path.
    Every other glob is matched against *path* relative to *root*; a file
    outside *root* can therefore only be named by an absolute glob.
    """
    absolute = str(path)
    try:
        relative = str(path.relative_to(root))
    except ValueError:
        relative = None
    for raw, pattern in zip(rule.raw_patterns, rule.patterns, strict=True):
        subject = absolute if raw.startswith("/") else relative
        if subject is not None and pattern.match(subject):
            return True
    return False
