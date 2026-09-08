#!/usr/bin/env python3
"""Decide the release version: git-cliff's census, raised (never lowered) by a human.

Otto is pre-1.0 and follows the Cargo/npm 0.x convention: a breaking (`!`)
commit bumps MINOR while major stays 0, and everything else bumps PATCH.
`cliff.toml`'s `[bump]` section makes `git-cliff --bumped-version` compute
that census from the conventional-commit history since the last tag.

Before this script existed, `make release` defaulted to `BUMP=patch`
unconditionally, and a human had to remember to pass `BUMP=minor` by hand
whenever a breaking change shipped -- a patch release once shipped an
unmarked break. Now the census decides: with no override, the census wins;
`BUMP=` may only ask for something AT LEAST as high as the census (a lower
request is refused, with the offending commits named); `NEW_VERSION=` is the
prerelease escape hatch and is never refused, only warned about, since a
prerelease's exact number is the human's call.

Pure decision logic lives in ``decide_bump`` (and its helpers), which take
already-computed strings and never touch a subprocess -- that is what makes
the module's own tests fast and hostless. ``main`` is the thin CLI shim that
gathers those strings via git-cliff, bump-my-version and ``git log``, prints
the chosen version to stdout (for the Makefile to capture) and everything
else to stderr, and exits 1 on a refusal.

Usage, from the repo root, mirroring how the Makefile's `release` recipe
invokes it::

    BUMP=minor uv run python scripts/release_bump.py
    NEW_VERSION=0.4.0rc1 uv run python scripts/release_bump.py
"""

import argparse
import os
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

from packaging.version import Version

# Matches a Conventional Commits subject marked breaking: `type!:` or
# `type(scope)!:`. Deliberately simple -- it only has to agree with
# cliff.toml's own `breaking_always_bump_major`-adjacent behaviour closely
# enough to name commits in a refusal message, not to re-implement git-cliff.
_BREAKING_SUBJECT = re.compile(r"^[A-Za-z]+(?:\([^)]*\))?!:")

# Conventional Commits allows either spelling of the breaking-change footer
# token: "BREAKING CHANGE:" and "BREAKING-CHANGE:" are declared synonyms.
_BREAKING_FOOTER = re.compile(r"BREAKING[ -]CHANGE:")


def is_breaking_commit(subject: str, body: str) -> bool:
    """Whether *subject*/*body* mark a Conventional Commits breaking change."""
    return bool(_BREAKING_SUBJECT.match(subject)) or bool(_BREAKING_FOOTER.search(body))


def offending_commits(commits: "list[tuple[str, str, str]]") -> "list[str]":
    """Render the ``sha subject`` lines a refusal message names, in the given order.

    *commits* is a list of ``(short_sha, subject, body)`` triples, oldest
    calling convention matching ``git log``'s default newest-first order --
    the caller decides the order, this only filters.
    """
    return [
        f"{sha} {subject}" for sha, subject, body in commits if is_breaking_commit(subject, body)
    ]


def bump_floor(last_tag: str, census: str) -> str:
    """Which ``BUMP=`` value the census (*last_tag* -> *census*) corresponds to."""
    old, new = Version(last_tag), Version(census)
    if new.major != old.major:
        return "major"
    if new.minor != old.minor:
        return "minor"
    return "patch"


@dataclass
class BumpDecision:
    """What ``release_bump`` decided, and why -- injectable for tests."""

    decision: str  # "census" | "honoured" | "refused" | "prerelease-warning"
    version: str
    message: str


def census_guard(
    last_tag: str, census: "str | None", *, census_error: "str | None" = None
) -> "str | None":
    """``None`` if *census* is a real version that advances past *last_tag*, else why not.

    Three distinct conditions, each with its own message -- collapsing them
    (e.g. via ``census or ""``) previously let "git-cliff exited non-zero"
    and "git-cliff exited 0 but found nothing releasable" print the same
    "printed nothing" text, which is wrong for the first:

    * ``census is None`` -- git-cliff (the subprocess) failed outright, e.g.
      its Semver parser rejects a ``vX.Y.ZrcN`` tag left by a previous
      prerelease. *census_error* carries the subprocess's own stderr/repr so
      the refusal names the real reason instead of guessing.
    * ``census == ""`` -- git-cliff ran fine and reported nothing releasable
      since *last_tag*.
    * ``census`` is a real version that does not exceed *last_tag* -- stuck
      (e.g. at an rc tag with no further commits, git-cliff echoes the rc
      tag itself back as the census).

    Either would otherwise let the no-override (census) or ``BUMP=`` path
    silently re-tag an existing version -- ``main()`` would emit
    ``NEW_VERSION=<last_tag>``, and the Makefile would proceed straight to
    ``git-cliff --tag`` and ``bump-my-version --new-version`` with it.
    Checked in ``decide_bump`` before the census/``BUMP=`` branching, since
    any of the three would otherwise crash the comparison those branches
    depend on. The ``NEW_VERSION=`` path reuses this message as a WARNING
    rather than a refusal -- see ``decide_bump``.
    """
    if census is None:
        detail = f": {census_error}" if census_error else " (no error detail captured)"
        return f"REFUSED: git-cliff could not compute a census{detail}."
    if not census:
        return (
            "REFUSED: git-cliff --bumped-version printed nothing -- there is nothing "
            f"releasable since v{last_tag}."
        )
    if Version(census) <= Version(last_tag):
        return (
            f"REFUSED: the conventional-commit census v{census} did not advance past "
            f"the last tag v{last_tag} -- there is nothing releasable, or the census is stuck."
        )
    return None


def decide_bump(
    *,
    last_tag: str,
    census: "str | None",
    census_error: "str | None" = None,
    bump: "str | None" = None,
    bump_computed: "str | None" = None,
    new_version: "str | None" = None,
    offending: "list[str]" = (),
) -> BumpDecision:
    """Compare the census against a human's ``BUMP=``/``NEW_VERSION=`` override.

    ``NEW_VERSION`` (the prerelease path) takes priority over ``BUMP`` when
    both are set -- a stray ``BUMP=`` left in the environment must not fight
    an explicit version. Only a ``BUMP=`` below the census is ever refused; a
    ``NEW_VERSION=`` below the census is a warning, because a prerelease
    version is the human's call by design.

    *census* is ``None`` when git-cliff (the subprocess) failed outright --
    e.g. its Semver parser rejects a `vX.Y.ZrcN` tag left by a previous
    prerelease -- with *census_error* naming why, from the subprocess's own
    stderr. ``census_guard`` turns *census*/*census_error* into one message
    covering all three unusable states (unavailable, empty, stuck at
    *last_tag*); see its docstring.

    For the census/``BUMP=`` branches, a guard hit is refused outright --
    there is nothing for a human's override to raise. The ``NEW_VERSION=``
    branch is different: a guard hit there is downgraded to a WARNING and
    *new_version* is honoured without a census comparison, since the
    docstring/docs promise NEW_VERSION= is "never refused, only warned" --
    the WARNING text is the guard's own message with its ``REFUSED: ``
    prefix stripped, since an honoured decision must never say REFUSED.
    """
    guard_message = census_guard(last_tag, census, census_error=census_error)

    if new_version is not None:
        if guard_message is not None:
            reason = guard_message.removeprefix("REFUSED: ")
            return BumpDecision(
                "honoured",
                new_version,
                f"WARNING: {reason} Honouring NEW_VERSION={new_version} without a "
                "census comparison (prerelease versions are the human's call).",
            )
        new_v = Version(new_version)
        census_v = Version(census)
        if new_v < census_v:
            return BumpDecision(
                "prerelease-warning",
                new_version,
                f"WARNING: NEW_VERSION={new_version} is lower than the conventional-commit "
                f"census v{census}; proceeding anyway (prerelease versions are the human's call).",
            )
        return BumpDecision(
            "honoured",
            new_version,
            f"targeting NEW_VERSION={new_version} (census: v{census})",
        )

    if guard_message is not None:
        return BumpDecision("refused", census or "", guard_message)

    census_v = Version(census)

    if bump is not None:
        computed_v = Version(bump_computed)
        if computed_v < census_v:
            floor = bump_floor(last_tag, census)
            named = "\n".join(f"  {c}" for c in offending) or "  (none found -- check LAST_TAG)"
            return BumpDecision(
                "refused",
                census,
                f"REFUSED: BUMP={bump} resolves to v{bump_computed}, lower than the "
                f"conventional-commit census v{census}. Commits since {last_tag} forcing at "
                f"least a {floor} bump:\n{named}\nBUMP={floor} is the floor.",
            )
        return BumpDecision(
            "honoured",
            bump_computed,
            f"targeting BUMP={bump} -> v{bump_computed} (census: v{census})",
        )

    return BumpDecision("census", census, f"targeting the census v{census}")


# ---------------------------------------------------------------------------
# Subprocess glue. Each function is a single external call so CLI-entry tests
# can monkeypatch them individually rather than shelling out for real.
# ---------------------------------------------------------------------------


def git_last_tag(cwd: "Path | None" = None) -> str:
    """Return the most recent reachable *version* tag, e.g. ``v0.11.0``.

    ``--match 'v[0-9]*'`` mirrors ``cliff.toml``'s own ``tag_pattern`` so this
    script and git-cliff agree on what a "version tag" is -- without it, a
    non-version tag (e.g. an archive marker) closer to HEAD than the last
    real release would be picked instead, and ``Version()`` would choke on
    it.
    """
    return subprocess.run(
        ["git", "describe", "--tags", "--abbrev=0", "--match", "v[0-9]*"],  # noqa: S607 -- `git` via PATH
        cwd=cwd,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()


def git_cliff_bumped_version(cwd: "Path | None" = None) -> str:
    """Return the conventional-commit census, e.g. ``v0.12.0``."""
    return subprocess.run(
        ["git-cliff", "--bumped-version"],  # noqa: S607 -- a dev dependency on PATH
        cwd=cwd,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()


def bump_my_version_new_version(bump: str, cwd: "Path | None" = None) -> str:
    """Return what ``bump`` (``patch``/``minor``/``major``) resolves to, unapplied."""
    return subprocess.run(  # noqa: S603 -- fixed argv, no shell, `bump` is patch|minor|major
        ["bump-my-version", "show", "new_version", "--increment", bump],  # noqa: S607
        cwd=cwd,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()


_FIELD_SEP = "\x1f"
_RECORD_SEP = "\x1e"


def git_log_commits(last_tag: str, cwd: "Path | None" = None) -> "list[tuple[str, str, str]]":
    """Return ``(short_sha, subject, body)`` for every commit since *last_tag*, newest first."""
    fmt = f"%h{_FIELD_SEP}%s{_FIELD_SEP}%b{_RECORD_SEP}"
    raw = subprocess.run(  # noqa: S603 -- fixed argv, no shell, format/range are ours
        ["git", "log", f"--format={fmt}", f"{last_tag}..HEAD"],  # noqa: S607 -- `git` via PATH
        cwd=cwd,
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    commits: "list[tuple[str, str, str]]" = []
    for raw_record in raw.split(_RECORD_SEP):
        record = raw_record.strip("\n")
        if not record:
            continue
        sha, subject, body = record.split(_FIELD_SEP, 2)
        commits.append((sha, subject, body))
    return commits


def main(argv: "list[str]") -> int:
    """Gather the census and any override, decide, and report.

    Prints the chosen version to stdout on success (what the Makefile
    captures via command substitution) and everything else to stderr; exits
    1 without printing a version on a refusal, so a failed capture never
    silently becomes an empty ``NEW_VERSION``.
    """
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--cwd",
        type=Path,
        default=None,
        help="Repository to operate in (default: the current directory).",
    )
    args = parser.parse_args(argv)

    bump = os.environ.get("BUMP") or None
    new_version = os.environ.get("NEW_VERSION") or None

    last_tag = git_last_tag(cwd=args.cwd).lstrip("v")

    # Unconditional and best-effort: git-cliff can fail outright regardless
    # of which override is in play (e.g. its Semver parser rejects a
    # `vX.Y.ZrcN` tag left by a previous prerelease), and `decide_bump` is
    # what decides what a missing census means for each branch -- a clean
    # refusal for the census/BUMP= branches, a WARNING for NEW_VERSION=.
    # Reporting the failure is `decide_bump`'s message alone (below), not a
    # second print here, so there is one place that explains a missing
    # census rather than two that might disagree.
    census: "str | None"
    census_error: "str | None" = None
    try:
        census = git_cliff_bumped_version(cwd=args.cwd).lstrip("v")
    except subprocess.CalledProcessError as exc:
        census = None
        census_error = (exc.stderr or str(exc)).strip()

    bump_computed = None
    offending: "list[str]" = []
    if new_version is None and bump is not None:
        bump_computed = bump_my_version_new_version(bump, cwd=args.cwd)
        offending = offending_commits(git_log_commits(f"v{last_tag}", cwd=args.cwd))

    decision = decide_bump(
        last_tag=last_tag,
        census=census,
        census_error=census_error,
        bump=bump,
        bump_computed=bump_computed,
        new_version=new_version,
        offending=offending,
    )

    print(decision.message, file=sys.stderr)
    if decision.decision == "refused":
        return 1
    print(decision.version)
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main(sys.argv[1:]))
