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
fail this check on their own. The one exception: a ``Host`` protocol line
whose old parameter list survives, verbatim and in order, as a strict
PREFIX of a new one for the same method is a WIDENING, not a break -- a
caller that only ever passed the old keywords still resolves against the
new signature -- so a trailing parameter addition needs no mark; a reorder,
rename, or shortened list still does, because the checker is line-based and
cannot otherwise tell a widening from a rename. The golden carries
parameter NAMES only, so a widened shape is not trusted on its own: every
appended parameter is looked up in the LIVE signature at HEAD, and one that
is mandatory there is a break after all -- a caller who omits it no longer
resolves.

Not every golden line is library surface, though. ``otto:<name>`` (an
``otto.__all__`` export) and ``otto.host.host:Host.<method>(...)`` (a protocol
signature) ARE the library: their removal is a break needing a mark, with no
escape hatch beyond the widening exception above. Every other
``<module>:<name>`` / bare ``<module>:`` line records only
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

The rules above are for a v1 golden. Each commit is judged by the schema of
the golden in its PARENT (``scripts/api_lines.py`` reads both ends). For a v2
golden (the API dump, dump spec
``docs/superpowers/specs/2026-10-05-api-dump-design.md``), each commit's dump
is regenerated from that commit's own archive and lock. A committed dump that
differs, or that cannot be regenerated, is refused, marked or not. The
parent's committed dump and the commit's are compared by the dump spec's §4
rules: a breaking finding needs a mark. The producer schema may never decrease
along any parent edge. Going from a v2 parent back to a v1 golden is a rollback;
deleting the golden is refused too (the next commit could add a v1 golden
under the v1 rules), and so is a merge that drops the v2 golden one of its
parents carries; no mark excuses any of them. A v1-era merge (no v2
golden at the merge or any parent) is still skipped, as the v1 rules always
did; a v2-era merge is judged against its first parent. Once
either end is v2, ``api/public.toml`` must exist and parse at the commit --
no mark excuses its absence, and deleting the golden does not switch that
off. A v2 -> v2 commit's namespaces are diffed against its parent's; a
deletion is refused and nothing else is compared.

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
from dataclasses import dataclass, field
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_GOLDEN = Path("tests") / "unit" / "api_snapshot" / "public_api.txt"
DEFAULT_MANIFEST = Path("api") / "public.toml"
SCHEMA_V1 = 1
SCHEMA_V2 = 2

sys.path.insert(0, str(REPO_ROOT))
from scripts import api_compat, api_lines, api_records, api_regen  # noqa: E402 -- path set up above
from scripts.api_manifest import (  # noqa: E402 -- path set up above
    ManifestError,
    Namespace,
    manifest_breaks,
    parse_manifest,
)
from scripts.api_resolver_env import resolver_env as _resolver_env  # noqa: E402
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


def _blob_at(repo: Path, rev: str, rel: str) -> "bytes | None":
    """Return the committed bytes of *rel* at *rev*, or None when *rev* has no such file.

    Bytes, never text: :func:`_git`'s text mode translates CRLF line endings to
    LF, which would make a CRLF hand edit of a dump read back as the canonical
    LF dump and pass a byte-exact freshness check.
    """
    proc = subprocess.run(  # noqa: S603 -- fixed argv, no shell; `repo`/`rel` are ours
        ["git", "cat-file", "blob", f"{rev}:{rel}"],  # noqa: S607 -- `git` via PATH
        cwd=repo,
        capture_output=True,
        check=False,
        env=_git_env(),
    )
    return proc.stdout if proc.returncode == 0 else None


def _decode(data: bytes) -> str:
    """Decode committed bytes as strict UTF-8, with no newline translation."""
    return data.decode("utf-8")


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
PROTOCOL_LINE_MODULE = api_lines.HOST_MODULE
PROTOCOL_LINE_PREFIX = api_lines.HOST_PREFIX

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
    """Return the commit shas in *rev_range*, merges included, oldest first.

    :func:`main` still skips a v1-era merge (neither the merge nor any parent
    carries a v2 golden), as before the dump; a v2-era merge is judged against
    its first parent.

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
        out = _git(repo, "rev-list", "--reverse", rev_range)
    except subprocess.CalledProcessError as exc:
        stderr = (exc.stderr or str(exc)).strip()
        raise RangeError(f"cannot resolve range {rev_range!r}: {stderr}") from exc
    return [line.strip() for line in out.splitlines() if line.strip()]


def commit_subject_body(repo: Path, sha: str) -> "tuple[str, str]":
    """Return (subject, body) for *sha*."""
    out = _git(repo, "log", "-1", "--format=%s%x1f%b", sha)
    subject, _, body = out.rstrip("\n").partition("\x1f")
    return subject, body


def _golden_rel(repo: Path, golden: Path) -> str:
    """Return *golden* as a repo-relative POSIX path, or as given when it is not under *repo*."""
    try:
        return golden.resolve().relative_to(repo.resolve()).as_posix()
    except ValueError:
        return str(golden)


def golden_bytes_at(repo: Path, rev: str, golden: Path) -> "bytes | None":
    """Return the golden's committed bytes at *rev*, or None when *rev* has no such file."""
    return _blob_at(repo, rev, _golden_rel(repo, golden))


def golden_text_at(repo: Path, rev: str, golden: Path) -> "str | None":
    """Return the golden's text at *rev* (strict UTF-8, newlines kept), or None when absent."""
    data = golden_bytes_at(repo, rev, golden)
    return None if data is None else _decode(data)


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
    diff = _git(repo, "diff-tree", "-p", "--root", sha, "--", _golden_rel(repo, golden))
    removed = []
    for line in diff.splitlines():
        if not line.startswith("-") or line.startswith("---"):
            continue
        content = line[1:]
        if not content or content.startswith("#"):
            continue
        removed.append(content)
    return removed


def added_golden_lines(repo: Path, sha: str, golden: Path) -> "list[str]":
    """Return the golden data lines *sha* adds relative to its parent.

    Mirrors :func:`removed_golden_lines`: same ``git diff-tree -p --root``
    call (already run once per commit for the removals; a second pass here
    keeps the two collectors independent and equally simple to read), same
    header/comment/file-marker skipping, but collecting ``+`` lines instead
    of ``-`` ones.
    """
    diff = _git(repo, "diff-tree", "-p", "--root", sha, "--", _golden_rel(repo, golden))
    added = []
    for line in diff.splitlines():
        if not line.startswith("+") or line.startswith("+++"):
            continue
        content = line[1:]
        if not content or content.startswith("#"):
            continue
        added.append(content)
    return added


_parse_protocol_line = api_lines.parse_host_line


def _protocol_method(line: str) -> str:
    """Return the ``Host.<method>`` part of a protocol *line* (``""`` if it isn't one)."""
    parsed = _parse_protocol_line(line)
    return "" if parsed is None else parsed[0]


def widening_appended_params(removed: str, added: "list[str]") -> "list[str] | None":
    """Return the parameters APPENDED to *removed* by a widening line in *added*.

    A widening is a same-method line in *added* whose parameter list starts
    with *removed*'s, verbatim and in the same order, and is strictly
    longer -- i.e. only trailing parameters were appended; the appended tail
    is what comes back. A reorder, a rename, or a shorter list is NOT a
    widening (``None``), because a caller who only ever passed the
    parameters *removed* names would no longer resolve against those
    shapes. Pure textual line-diffing cannot otherwise distinguish "grew a
    trailing keyword" from "renamed a parameter" -- both delete the old
    line and add a new one -- so this is the one place that actually
    compares the two parameter lists.

    The appended tail is returned rather than a bare yes/no because the
    golden carries parameter NAMES only: whether an appended parameter is
    optional is not on the line, and only the live signature at HEAD can
    say (see :func:`appended_params_without_defaults`).
    """
    parsed_removed = _parse_protocol_line(removed)
    if parsed_removed is None:
        return None
    removed_method, removed_params = parsed_removed
    for candidate in added:
        parsed_candidate = _parse_protocol_line(candidate)
        if parsed_candidate is None:
            continue
        candidate_method, candidate_params = parsed_candidate
        if candidate_method != removed_method:
            continue
        if (
            len(candidate_params) > len(removed_params)
            and candidate_params[: len(removed_params)] == removed_params
        ):
            return candidate_params[len(removed_params) :]
    return None


def is_widened_protocol_line(removed: str, added: "list[str]") -> bool:
    """Return True iff *removed* is a ``Host`` protocol line WIDENED by one of *added*.

    The line-shape half of the widening exception; see
    :func:`widening_appended_params`, whose answer this reduces to a yes/no.
    A widened SHAPE is not yet a safe widening -- the appended parameters
    must also be optional at HEAD.
    """
    return widening_appended_params(removed, added) is not None


_SIGNATURE_SOURCE = """import importlib, inspect, sys
module = importlib.import_module(sys.argv[1])
obj = module
for part in sys.argv[2].split("."):
    obj = getattr(obj, part)
params = inspect.signature(obj).parameters
for name in sys.argv[3:]:
    param = params.get(name)
    if param is not None and param.default is inspect.Parameter.empty:
        print(name)
"""


def appended_params_without_defaults(method: str, appended: "list[str]", cwd: Path) -> "list[str]":
    """Return which of *appended* are REQUIRED on ``Host.<method>`` at HEAD.

    The golden pins parameter names, not defaults, so a widened line and a
    line that grew a MANDATORY parameter look identical on the page -- yet
    the second breaks every existing caller. The live signature is the only
    place that answer exists, so it is read from the tree under *cwd*, in a
    SUBPROCESS with that tree's ``src/`` first on the path, exactly as
    :func:`path_resolves` does: this script never imports the repo into its
    own process.

    A parameter the live signature does not carry AT ALL is not reported.
    Only positive evidence -- "this name is there, and it is mandatory" --
    classifies a line as breaking; a golden that has drifted from the
    source, or a signature the child could not read (an import that
    crashes, ``inspect`` refusing what it was handed), leaves the widening
    standing rather than inventing a break out of a failed lookup.
    """
    argv = [sys.executable, "-c", _SIGNATURE_SOURCE, PROTOCOL_LINE_MODULE, method, *appended]
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
        return []
    if proc.returncode != 0:
        return []
    return [line.strip() for line in proc.stdout.splitlines() if line.strip()]


def _host_verdict(line: str, added: "list[str]", repo: Path) -> "tuple[str, str]":
    """Judge a removed *line* as a ``Host`` protocol widening, if it is one.

    Returns ``("widened", message)`` for a trailing-parameter widening whose
    appended parameters are all optional at HEAD, ``("breaking", message)``
    for one that appended a REQUIRED parameter, and ``("other", "")`` when no
    line in *added* widens *line*. The v1 and v2 paths share it.
    """
    appended = widening_appended_params(line, added) if is_library_line(line) else None
    required = (
        appended_params_without_defaults(_protocol_method(line), appended, repo) if appended else []
    )
    if required:
        return (
            "breaking",
            (
                f"{line} -- widened with a REQUIRED parameter "
                f"{', '.join(required)}; a caller omitting it now fails "
                f"-- mark the commit breaking"
            ),
        )
    if appended is not None:
        return "widened", f"widened {line} -- trailing parameter(s) added; no mark needed"
    return "other", ""


def manifest_at(repo: Path, rev: str, manifest: Path) -> "dict[str, Namespace]":
    """Parse the manifest as committed at *rev*; ManifestError when absent or malformed."""
    rel = _golden_rel(repo, manifest)
    data = _blob_at(repo, rev, rel)
    if data is None:
        raise ManifestError(f"{rel} does not exist")
    try:
        text = _decode(data)
    except UnicodeDecodeError as exc:
        raise ManifestError(f"{rel} is not UTF-8: {exc}") from exc
    return parse_manifest(text)


HOST_BINDING = "otto.host:Host"


def host_conversion_breaks(
    line: str, old_names: "list[str]", params: "list[api_records.Param]"
) -> "tuple[list[str], list[str]]":
    """Judge one v1 ``Host`` line against its dump member, under v1's projection (dump spec §8).

    v1 recorded keyword names in signature order (positional-only and ``*args``
    left out, ``**kwargs`` as ``"**"``). Every recorded name must still be
    accepted, the recorded names the signature still takes positionally must be
    exactly its leading positional slots in v1's order (a new positional-only or
    positional-or-keyword parameter may only be appended after them), and a new
    parameter needs a default. v1 recorded no positional-only parameter, so none
    can be shown to have existed before: every required positional-only
    parameter counts as new. Keyword-only order is not compared (D-4), and a
    kind change v1 never recorded carries no retroactive obligation.
    """
    projected = ["**" if p.kind == "VK" else p.name for p in params if p.kind in ("PK", "KO", "VK")]
    breaking = [
        f"{line} -- keyword {name} is no longer accepted"
        for name in old_names
        if name not in projected
    ]
    slots = [p.name for p in params if p.kind in ("PO", "PK")]
    kept = [n for n in old_names if n in {p.name for p in params if p.kind == "PK"}]
    if slots[: len(kept)] != kept:
        breaking.append(f"{line} -- positional order changed")
    breaking += [
        f"{line} -- new required parameter {p.name}"
        for p in params
        if p.default is None
        and (p.kind == "PO" or (p.kind in ("PK", "KO") and p.name not in old_names))
    ]
    info = [] if breaking or projected == old_names else [f"widened {line}; no mark needed"]
    return breaking, info


def conversion_breaks(
    v1_lines: "list[str]",
    dump: "api_records.Dump",
    namespaces: "dict[str, Namespace]",
    manifest_label: str = DEFAULT_MANIFEST.as_posix(),
) -> "tuple[list[str], list[str]]":
    """Return (breaking, info) for the v1 -> v2 commit (spec 1 §6, dump spec §8).

    Root and deep lines must reappear as ``name`` records; resolvability no
    longer excuses a dropped line. A bare module line needs its namespace in the
    manifest. A ``Host`` line maps to ``member otto.host:Host.<method>``.
    """
    breaking: list[str] = []
    info: list[str] = []
    host_members = dump.members_of(HOST_BINDING)
    for line in v1_lines:
        kind = api_lines.v1_kind(line)
        module, _, name = line.partition(":")
        if kind == "host":
            method, old_names = api_lines.parse_host_line(line)
            member = host_members.get(method.removeprefix(api_lines.HOST_PREFIX))
            if member is None or member.fields[0] not in ("method", "classmethod", "staticmethod"):
                breaking.append(
                    f"{line} -- no {HOST_BINDING}.{method.removeprefix('Host.')} method"
                )
                continue
            more_breaking, more_info = host_conversion_breaks(
                line, old_names, api_records.parse_call(member).params or []
            )
            breaking += more_breaking
            info += more_info
        elif kind in ("root", "deep"):
            if f"{module}:{name}" not in dump.bindings:
                breaking.append(f"{line} -- not carried into the v2 golden")
        elif kind == "bare":
            if module not in namespaces:
                breaking.append(f"{line} -- no namespace entry for {module} in {manifest_label}")
        else:
            breaking.append(line)
    return breaking, info


@dataclass
class Verdict:
    """What one commit did to the golden.

    A breaking mark excuses ``breaking``; ``info`` is printed and needs
    nothing; ``refused`` holds failures that no mark can excuse.
    """

    breaking: "list[str]" = field(default_factory=list)
    info: "list[str]" = field(default_factory=list)
    refused: "list[str]" = field(default_factory=list)


def _v1_verdict(repo: Path, sha: str, golden: Path) -> Verdict:
    """Judge a v1 -> v1 commit by its golden diff, under the rules in this module's docstring."""
    removed = removed_golden_lines(repo, sha, golden)
    if not removed:
        return Verdict()
    added = added_golden_lines(repo, sha, golden)
    breaking, docs_only, widened = [], [], []
    for line in removed:
        kind, message = _host_verdict(line, added, repo)
        if kind == "breaking":
            breaking.append(message)
        elif kind == "widened":
            widened.append(message)
        elif is_library_line(line) or not path_resolves(line, repo):
            breaking.append(line)
        else:
            docs_only.append(f"removed {line} -- docs-only, still importable; no mark needed")
    return Verdict(breaking=breaking, info=widened + docs_only)


def evaluate_commit(
    repo: Path,
    sha: str,
    parent: "str | None",
    golden: Path,
    manifest: Path,
    merged: "list[str] | None" = None,
    env: "api_regen.DepsEnv | None" = None,
) -> Verdict:
    """Judge *sha* against *parent* by the schema of the golden at each end.

    The PARENT's schema picks the rules (spec §6). v1 -> v1 keeps the v1
    diff rules. v2 -> v2 first proves the commit's dump fresh (regenerated from the
    commit itself, dump spec §5) and then compares the parent's dump with it by
    the dump spec's §4 rules. v2 -> v1 is a
    rollback and v2 -> absent a deletion, and no mark excuses either: a
    marked deletion would let the next commit add a v1 golden under the v1
    rules. A deletion is refused and nothing else is compared.
    v1 -> v2 is the conversion of dump spec §8: every v1 line must be carried.

    A merge is judged against its first parent, but the parents it *merged*
    count too: if any of them carries a v2 golden and the merge does not,
    the merge left v2, and no mark excuses that either.

    The v2 era is active when EITHER end is v2, and then *manifest* is read
    at the commit: a missing or malformed one is refused, marked or not --
    deleting the golden does not switch the manifest off. When the commit
    keeps a v2 golden over a v2 parent, the parent's namespaces are diffed
    against the commit's; a deletion is refused and nothing else is compared.
    A v1 -> v1 commit never reads the manifest.
    """
    before = golden_text_at(repo, parent, golden) if parent else None
    after_bytes = golden_bytes_at(repo, sha, golden)
    after = None if after_bytes is None else _decode(after_bytes)
    before_schema = api_lines.schema_of(before) if before is not None else None
    after_schema = api_lines.schema_of(after) if after is not None else None
    if before_schema == SCHEMA_V2 and after_schema == SCHEMA_V1:
        return Verdict(refused=["rollback from api-snapshot v2 to v1"])
    if after_schema != SCHEMA_V2 and _carries_v2_golden(repo, merged or [], golden):
        return Verdict(refused=["merge leaves api-snapshot v2: a merged parent carries it"])
    if SCHEMA_V2 not in (before_schema, after_schema):
        return _v1_verdict(repo, sha, golden)
    refused: list[str] = []
    if before_schema == SCHEMA_V2 and after is None:
        refused.append("v2 golden deleted: once the golden is v2, its only next state is v2")
    try:
        after_ns = manifest_at(repo, sha, manifest)
    except ManifestError as exc:
        label = _golden_rel(repo, manifest)
        refused.append(f"{label} missing or malformed at {sha[:12]}: {exc}")
        return Verdict(refused=refused)
    if after is None or after_bytes is None:
        return Verdict(refused=refused)
    refused += _dump_refusals(
        repo, sha, [p for p in [parent, *(merged or [])] if p], after_bytes, golden, manifest, env
    )
    if refused:
        return Verdict(refused=refused)
    after_dump = api_records.parse_dump(after)
    if parent and before_schema == SCHEMA_V2:
        try:
            before_dump = api_records.parse_dump(before or "")
        except api_records.DumpError as exc:
            return Verdict(refused=[f"the parent's dump does not parse: {exc}"])
        breaking = api_compat.group_findings(api_compat.compare_dumps(before_dump, after_dump))
        try:
            before_ns = manifest_at(repo, parent, manifest)
        except ManifestError:
            before_ns = {}
        return Verdict(breaking=breaking + manifest_breaks(before_ns, after_ns))
    breaking, info = conversion_breaks(
        api_lines.data_lines(before or ""),
        after_dump,
        after_ns,
        manifest_label=_golden_rel(repo, manifest),
    )
    return Verdict(breaking=breaking, info=info)


def _dump_refusals(
    repo: Path,
    sha: str,
    parents: "list[str]",
    committed: bytes,
    golden: Path,
    manifest: Path,
    env: "api_regen.DepsEnv | None",
) -> "list[str]":
    """Return every reason *sha*'s dump cannot be trusted; no mark excuses any (dump spec §4.4).

    Freshness compares *committed*, the golden's bytes exactly as git holds them,
    with the regenerated dump's UTF-8 bytes, so a dump that differs only in its
    line endings is stale.
    """
    refused = []
    after = _decode(committed)
    schema = api_records.producer_schema_of(after)
    if schema != api_records.PRODUCER_SCHEMA:
        refused.append(
            f"producer-schema {schema} is not this checker's schema {api_records.PRODUCER_SCHEMA}"
        )
    for rev in parents:
        text = golden_text_at(repo, rev, golden)
        older = api_records.producer_schema_of(text) if text is not None else None
        if older is not None and schema is not None and schema < older:
            refused.append(f"producer-schema decreased from {older} (at {rev[:12]}) to {schema}")
    try:
        api_records.parse_dump(after)
    except api_records.DumpError as exc:
        refused.append(f"the dump does not parse: {exc}")
    generated = api_regen.generate_at_commit(
        repo, sha, _golden_rel(repo, manifest), env or api_regen.UvDepsEnv()
    )
    if generated.text is None:
        refused += [f"cannot regenerate the dump at {sha[:12]}: {r}" for r in generated.refusals]
    elif generated.text.encode("utf-8") != committed:
        refused.append(
            "stale dump: the committed golden differs from the dump regenerated from this "
            "commit; run `make api-snapshot` and amend it into this commit"
        )
    return refused


def _carries_v2_golden(repo: Path, revs: "list[str]", golden: Path) -> bool:
    """Return True when the golden at any of *revs* is a v2 golden."""
    for rev in revs:
        text = golden_text_at(repo, rev, golden)
        if text is not None and api_lines.schema_of(text) == SCHEMA_V2:
            return True
    return False


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
        "--manifest",
        type=Path,
        default=DEFAULT_MANIFEST,
        help="path to the namespace manifest, relative to --repo unless absolute "
        f"(default: {DEFAULT_MANIFEST})",
    )
    parser.add_argument(
        "--env",
        choices=["uv", "current"],
        default="uv",
        help="how a v2 commit's dependencies are provided: its own locked env (uv), or this "
        "interpreter (current; tests only)",
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

    env = api_regen.CurrentEnv() if args.env == "current" else api_regen.UvDepsEnv()
    violations = 0
    for sha in shas:
        parents = _git(args.repo, "rev-list", "--parents", "-n", "1", sha).split()[1:]
        parent = parents[0] if parents else None
        # A v1-era merge is still skipped: the v1 rules read `git diff-tree -p`,
        # which shows a merge no diff, so there is nothing to judge. A merge where
        # the merge or any parent carries a v2 golden is judged below, against
        # its first parent, with the parents it merged in counted too.
        if len(parents) > 1 and not _carries_v2_golden(args.repo, [*parents, sha], args.golden):
            continue
        verdict = evaluate_commit(
            args.repo,
            sha,
            parent,
            args.golden,
            args.manifest,
            merged=parents[1:],
            env=env,
        )
        for message in verdict.info:
            print(f"info: {sha[:12]} {message}")
        if not verdict.refused and not verdict.breaking:
            continue
        subject, body = commit_subject_body(args.repo, sha)
        if not verdict.refused and is_breaking_commit(subject, body):
            continue
        violations += 1
        print(f"commit {sha} {subject}")
        for line in verdict.refused:
            print(f"  - refused: {line} -- no breaking mark can excuse this")
        for line in verdict.breaking:
            print(f"  - {line}")
        print()

    if violations:
        print(RULE)
        return 1

    print(f"check-breaking-marks: OK ({len(shas)} commit(s) scanned)")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
