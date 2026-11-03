"""The Makefile's recipes, read one way by every test that needs a target's live lines."""

import re


def is_commented_recipe(line: str) -> bool:
    """A recipe line make hands to the shell as a no-op comment.

    ``\\t# ...`` and ``\\t@# ...`` both execute as a shell comment — the leg
    "exists" textually but never runs. The review probe that motivated this:
    prefixing the real coverage-python serial leg with ``\\t# `` left the
    scanner green while the discriminators silently left the gate.
    """
    body = line.lstrip("\t").lstrip()
    if body.startswith("@"):
        body = body[1:].lstrip()
    return body.startswith("#")


def is_say_recipe(line: str) -> bool:
    """A recipe line that only PRINTS — the banner above a leg, not the leg."""
    body = line.lstrip("\t").lstrip()
    if body.startswith("@"):
        body = body[1:].lstrip()
    return body.startswith("$(SAY)")


def makefile_live_recipe(text: str, target: str) -> "list[str]":
    """Recipe lines of *target* that the shell would actually run.

    Commented (``\\t# ``) and ``$(SAY)`` lines are dropped: both are how a
    removed leg most plausibly leaves a trace that still contains its own
    name, which is the annotated-removal trap every scanner over the Makefile
    is built against.
    """
    lines: "list[str]" = []
    in_target = False
    for line in text.splitlines():
        if line.startswith("\t"):
            if in_target and not is_commented_recipe(line) and not is_say_recipe(line):
                lines.append(line)
            continue
        matched = re.match(r"^([A-Za-z0-9_.-]+)\s*:", line)
        if matched:
            in_target = matched.group(1) == target
    return lines
