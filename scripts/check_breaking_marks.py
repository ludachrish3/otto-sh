#!/usr/bin/env python3
"""Refuse a commit that deletes a golden line from the public-API surface unmarked.

``scripts/api_snapshot.py`` pins otto's public-API surface -- ``otto.__all__``,
every deep import the docs teach, and every ``Host`` protocol method's
parameter names -- to ``tests/unit/api_snapshot/public_api.txt``. Deleting a
line from that golden is, by construction, a public-API break: a caller who
used the removed name or passed the removed parameter now has nothing to
call. ``make release``'s version comes from git-cliff's conventional-commit
census (``scripts/release_bump.py``), which can only see what a commit
SUBJECT and BODY say -- an unmarked break still ships as a patch bump. This
script closes that gap: for every commit in a range, a golden-line deletion
that the commit didn't mark ``type(scope)!:`` in the subject, or with a
``BREAKING CHANGE:``/``BREAKING-CHANGE:`` token matched ANYWHERE in the body
(not just as a trailer on its own line -- the same classifier
``scripts/release_bump.py::is_breaking_commit`` already uses for the version
census), is refused.

RULE: a commit that deletes or renames a public symbol or a ``Host`` protocol
parameter must be marked breaking, regardless of how small it looks. A
rename is a deletion plus an addition; under this rule that is still a `!`
commit -- there is no "it's just a rename" escape hatch. Additions never
fail this check on their own.

Not every golden line is library surface, though. ``otto:<name>`` (an
``otto.__all__`` export) and ``otto.host.host:Host.<method>(...)`` (a protocol
signature) ARE the library: their removal is a break, always, with no escape
hatch. Every other ``<module>:<name>`` / bare ``<module>:`` line records only
that some user-doc fence TEACHES that import path -- so a docs edit that stops
teaching it removes a golden line without touching the library at all. A
docs-only change must not count as a break when the underlying library is
unchanged: such a removal is refused only if the path no longer RESOLVES at
the range's tip (``import <module>``, plus ``getattr(module, name)`` for a
non-bare line). If it still resolves, it is reported as "docs-only, still
importable" and needs no mark.

Resolution runs in a subprocess (``sys.executable -c ...``), so a module that
crashes on import cannot take the checker down and nothing already loaded in
the checker's own process can make a dead path look alive; any non-zero exit
means "does not resolve".

Because resolution imports from the ``--repo`` tree as it stands on disk
(that tree's ``src/`` goes first on the child's path), the range must END AT
``HEAD`` -- otherwise the answer would describe a tree that is not the one the
range's tip names. The default ``origin/main..HEAD`` satisfies this; a range
ending anywhere else is refused with exit 2.

Settings/lab keys are OUT OF SCOPE here (a later schema-diff gate); this
script only ever reads the api_snapshot golden.

Usage, from the repo root::

    python scripts/check_breaking_marks.py origin/main..HEAD
    python scripts/check_breaking_marks.py --golden PATH --repo PATH <range>

*range* must be a two-dot ``A..B`` revision range -- a bare ref is refused
before any git call, since a single ref names a checkout point, not the set
of commits to scan -- and its END must be ``HEAD``.

Exit codes: 0 every deleting commit in range is marked (or the range is
empty); 1 at least one is not; 2 the range doesn't parse, or git could not
resolve it (an unknown ref, a repo with no ``origin``, ...) -- distinct from
1 so a caller never mistakes "we couldn't even look" for "we looked and it
was clean".
"""

import argparse
import os
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_GOLDEN = Path("tests") / "unit" / "api_snapshot" / "public_api.txt"

sys.path.insert(0, str(REPO_ROOT))
from scripts.release_bump import is_breaking_commit  # noqa: E402 -- path set up above

RULE = (
    "RULE: a commit that deletes or renames a public name, deep-import path, or "
    "Host protocol parameter must be marked breaking -- `type(scope)!:` in the "
    "subject or a `BREAKING CHANGE:`/`BREAKING-CHANGE:` footer -- even when the "
    "deletion looks small. A rename is a deletion plus an addition, so it is "
    "marked too; there is no separate escape hatch."
)


class RangeError(Exception):
    """The commit range given on the command line doesn't parse or resolve."""


def _git_env() -> dict[str, str]:
    """Return the ambient environment, with global/system git config neutered.

    Only ``GIT_CONFIG_GLOBAL``/``GIT_CONFIG_SYSTEM`` are overridden -- unlike
    ``tests/_fixtures/gitrepo.py``'s hermetic harness (which also pins
    identity and ``HOME`` for commits it creates), this script only ever
    reads an existing repository, so the rest of the caller's environment
    (``PATH``, credentials for a private remote, ...) must pass through
    untouched. Neutering config alone is enough to stop a developer's
    ``diff.external``/textconv from rewriting the ``-``/``+`` lines this
    script parses.
    """
    return {**os.environ, "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_SYSTEM": "/dev/null"}


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(  # noqa: S603 -- fixed argv, no shell; `repo`/`args` are ours
        ["git", *args],  # noqa: S607 -- `git` via PATH
        cwd=repo,
        capture_output=True,
        text=True,
        check=True,
        env=_git_env(),
    ).stdout


def validate_range(rev_range: str) -> None:
    """Raise :class:`RangeError` unless *rev_range* is a two-dot ``A..B`` range.

    A bare ref (``HEAD``, ``main``) parses as a single revision to git, not a
    range of commits to scan -- refusing it here, before any subprocess runs,
    gives a caller a clear "you passed a ref, not a range" instead of git's
    own (correct but easy to misread) single-commit ``rev-list`` output.
    """
    if ".." not in rev_range:
        raise RangeError(
            f"{rev_range!r} is not a commit range: expected two dots, e.g. origin/main..HEAD"
        )


LIBRARY_MODULE = "otto"
PROTOCOL_LINE_MODULE = "otto.host.host"
PROTOCOL_LINE_PREFIX = "Host."

_RESOLVE_SOURCE = """import importlib, sys
module = importlib.import_module(sys.argv[1])
if len(sys.argv) > 2:
    getattr(module, sys.argv[2])
"""


def is_library_line(line: str) -> bool:
    """Return True when *line* pins the LIBRARY surface, not a documented path.

    Two shapes are library surface, and only these two: ``otto:<name>`` (a
    name in ``otto.__all__``) and ``otto.host.host:Host.<method>(...)`` (a
    protocol signature). A line with no ``:`` at all is an unrecognised shape
    and counts as library surface too -- the conservative side, since the
    documented-import branch is the only one that can excuse a removal.
    """
    module, sep, name = line.partition(":")
    if not sep:
        return True
    if module == LIBRARY_MODULE and name:
        return True
    return module == PROTOCOL_LINE_MODULE and name.startswith(PROTOCOL_LINE_PREFIX)


def _resolver_env(cwd: Path) -> dict[str, str]:
    """Return the resolver subprocess's environment, with *cwd*'s ``src/`` first.

    otto is a src-layout project installed into a virtualenv, so a bare
    interpreter imports it through the venv's ``.pth`` file -- which names ONE
    absolute source directory, not "wherever you happen to be". Running the
    child with ``cwd`` set to the tree under test therefore proves nothing:
    from a worktree it would still answer for the primary checkout. Putting
    that tree's ``src/`` on ``PYTHONPATH`` does prove it, because
    ``PYTHONPATH`` precedes site-packages on ``sys.path``. Any inherited
    ``PYTHONPATH`` is kept, after ours.
    """
    src = str(Path(cwd) / "src")
    inherited = os.environ.get("PYTHONPATH", "")
    return {**os.environ, "PYTHONPATH": src + os.pathsep + inherited if inherited else src}


def path_resolves(line: str, cwd: Path) -> bool:
    """Return True when *line*'s ``<module>[:<name>]`` still imports, in a SUBPROCESS.

    A fresh ``sys.executable``, never this process: a module that raises (or
    exits) on import cannot crash the checker, and nothing the checker has
    already imported can make a dead path look alive. Any non-zero exit -- an
    ImportError, an AttributeError, a hard ``sys.exit`` at module scope --
    reads as "does not resolve". The tree under *cwd* has its ``src/`` put
    FIRST on the child's path (see :func:`_resolver_env`), so the answer
    describes that tree and not whichever one the interpreter's virtualenv
    happens to be wired to.
    """
    module, _, name = line.partition(":")
    argv = [sys.executable, "-c", _RESOLVE_SOURCE, module]
    if name:
        argv.append(name)
    try:
        proc = subprocess.run(  # noqa: S603 -- fixed argv, no shell; the parts are ours
            argv,
            cwd=cwd,
            capture_output=True,
            text=True,
            check=False,
            timeout=120,
            env=_resolver_env(cwd),
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return proc.returncode == 0


def require_range_ends_at_head(repo: Path, rev_range: str) -> None:
    """Raise :class:`RangeError` unless *rev_range*'s end resolves to ``HEAD``.

    The documented-import branch answers "does this path still resolve?" by
    importing from the ``--repo`` tree as it stands on disk, so a range whose
    tip is some other commit would be judged against the wrong tree. An empty
    end (``A..``) is git's own shorthand for ``HEAD`` and passes.

    Both sides are peeled with ``^{commit}``: ``rev-parse`` on an ANNOTATED
    tag yields the tag OBJECT's sha, which never equals the commit's, so an
    unpeeled comparison would refuse a range ending at a tag that IS ``HEAD``.
    """
    end = rev_range.partition("..")[2].lstrip(".")
    if not end:
        return
    try:
        end_sha = _git(repo, "rev-parse", f"{end}^{{commit}}").strip()
        head_sha = _git(repo, "rev-parse", "HEAD^{commit}").strip()
    except subprocess.CalledProcessError as exc:
        stderr = (exc.stderr or str(exc)).strip()
        raise RangeError(f"cannot resolve range {rev_range!r}: {stderr}") from exc
    if end_sha != head_sha:
        raise RangeError(
            f"range {rev_range!r} must end at HEAD: this check imports documented "
            f"paths from the checked-out tree, and {end!r} ({end_sha[:12]}) is not "
            f"HEAD ({head_sha[:12]})."
        )


def commits_in_range(repo: Path, rev_range: str) -> "list[str]":
    """Return the non-merge commit shas in *rev_range*, oldest first.

    Raises :class:`RangeError` (never lets a :class:`subprocess.CalledProcessError`
    escape) when git cannot resolve *rev_range* -- an unknown ref, or a
    ``origin/...`` name in a repo with no ``origin`` remote, both of which
    otherwise surface as an unhandled traceback here and, since
    `check-breaking` is a prerequisite of `lint-arch`, in `make lint-python`
    too.
    """
    validate_range(rev_range)
    require_range_ends_at_head(repo, rev_range)
    try:
        out = _git(repo, "rev-list", "--reverse", "--no-merges", rev_range)
    except subprocess.CalledProcessError as exc:
        stderr = (exc.stderr or str(exc)).strip()
        raise RangeError(f"cannot resolve range {rev_range!r}: {stderr}") from exc
    return [line.strip() for line in out.splitlines() if line.strip()]


def commit_subject_body(repo: Path, sha: str) -> "tuple[str, str]":
    """Return (subject, body) for *sha*."""
    out = _git(repo, "log", "-1", "--format=%s%x1f%b", sha)
    subject, _, body = out.rstrip("\n").partition("\x1f")
    return subject, body


def removed_golden_lines(repo: Path, sha: str, golden: Path) -> "list[str]":
    """Return the golden data lines *sha* removes relative to its parent.

    ``git diff-tree -p --root`` rather than ``git diff <sha>^ <sha>``: the
    latter dies on a root commit (``<sha>^`` doesn't exist), while
    ``--root`` makes ``diff-tree`` diff a root commit against the empty tree
    -- i.e. report it as pure additions, which is exactly right (a root
    commit cannot DELETE a line that never existed) -- and leaves every
    other commit's diff unchanged.

    A ``git diff`` ``-`` line, with the ``-`` stripped, skipping the diff's own
    ``---``/``+++`` file headers, ``diff-tree``'s leading commit-sha line, and
    any golden ``#`` header/comment line -- only DATA-line deletions are a
    public-API break.
    """
    try:
        golden_rel = golden.resolve().relative_to(repo.resolve()).as_posix()
    except ValueError:
        golden_rel = str(golden)
    diff = _git(repo, "diff-tree", "-p", "--root", sha, "--", golden_rel)
    removed = []
    for line in diff.splitlines():
        if not line.startswith("-") or line.startswith("---"):
            continue
        content = line[1:]
        if not content or content.startswith("#"):
            continue
        removed.append(content)
    return removed


def main(argv: "list[str]") -> int:
    """Scan a commit range for an unmarked public-API-golden deletion."""
    parser = argparse.ArgumentParser(
        description=__doc__.splitlines()[0],
        epilog="range must be a two-dot A..B revision range ENDING AT HEAD, e.g. "
        "origin/main..HEAD -- documented-import paths are checked by importing "
        "them from the --repo tree as it stands on disk.",
    )
    parser.add_argument(
        "range",
        help="a two-dot git revision range ENDING AT HEAD, e.g. origin/main..HEAD (not a bare ref)",
    )
    parser.add_argument(
        "--golden",
        type=Path,
        default=DEFAULT_GOLDEN,
        help="path to the api_snapshot golden, relative to --repo unless absolute "
        f"(default: {DEFAULT_GOLDEN})",
    )
    parser.add_argument(
        "--repo",
        type=Path,
        default=REPO_ROOT,
        help="repository to scan (default: this script's own repo root)",
    )
    args = parser.parse_args(argv)

    try:
        shas = commits_in_range(args.repo, args.range)
    except RangeError as exc:
        print(str(exc), file=sys.stderr)
        return 2

    violations = 0
    for sha in shas:
        removed = removed_golden_lines(args.repo, sha, args.golden)
        if not removed:
            continue
        breaking, docs_only = [], []
        for line in removed:
            if is_library_line(line) or not path_resolves(line, args.repo):
                breaking.append(line)
            else:
                docs_only.append(line)
        for line in docs_only:
            print(f"info: {sha[:12]} removed {line} -- docs-only, still importable; no mark needed")
        if not breaking:
            continue
        subject, body = commit_subject_body(args.repo, sha)
        if is_breaking_commit(subject, body):
            continue
        violations += 1
        print(f"commit {sha} {subject}")
        for line in breaking:
            print(f"  - {line}")
        print()

    if violations:
        print(RULE)
        return 1

    print(f"check-breaking-marks: OK ({len(shas)} commit(s) scanned)")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
