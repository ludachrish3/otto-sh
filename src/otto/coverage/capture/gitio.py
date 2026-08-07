"""Thin subprocess wrappers around the git plumbing coverage needs.

Everything is synchronous and side-effect-free on the repo (read-only
commands only).  Callers pass the sut repo root; a non-repo raises
:class:`GitUnavailableError` with a clean message — specifically
:class:`NotAGitRepoError`, one of the three subclasses every failure here is
classified into (:class:`GitMissingError`, :class:`NotAGitRepoError`,
:class:`GitCommandFailedError`) so a caller can dispatch on the TYPE instead
of matching the message text.
"""

import subprocess
from dataclasses import dataclass
from pathlib import Path

from ...errors import OttoError


class GitUnavailableError(OttoError, RuntimeError):
    """Raised when git cannot answer (not a repo / git missing).

    The family root, and still what a caller that does not care WHY should
    catch. Every raise from the two runners below is one of the three
    subclasses; only :func:`log_walk_u0`'s join-mismatch (a data-integrity
    failure of a git call that SUCCEEDED) raises this class directly.
    """


class GitMissingError(GitUnavailableError):
    """No ``git`` executable on PATH — an environment error, not a repo answer.

    Never means "absent from the repo": a caller that folds this into a
    None/False "not found" answer reports every path as new.
    """


class NotAGitRepoError(GitUnavailableError):
    """The command's cwd is not inside a git work tree."""


class GitCommandFailedError(GitUnavailableError):
    """git ran inside a work tree and exited outside its ``ok_codes``.

    The ordinary "git says no" outcome: an unresolvable rev, a path absent
    at that rev, a sha not in the object database.
    """


_CONFIG_PIN: list[str] = ["-c", "diff.mnemonicprefix=false"]
"""Global git option, prepended before the subcommand name.

``diff.mnemonicprefix`` has no per-invocation flag (unlike the settings in
``_DIFF_FLAG_PIN``), so it must be pinned with ``-c``. Left at its
ambient default (a common dotfiles setting turns it on), ``git diff``/``git
log -p`` emit ``c/``/``w/`` path prefixes instead of ``a/``/``b/``, which
silently defeats every ``a/``/``b/``-prefix-stripping parser in this
package (:func:`~otto.coverage.capture.treediff.strip_side`,
:func:`~otto.coverage.attribution.parse_commit_diff`) — every parsed path
comes out wrong (e.g. ``w/f.c`` instead of ``f.c``), so
``_apply_worktree_overlay`` silently skips every file and an uncommitted
edit inherits the previous committer's ticket (final-review blocker 1).
"""

_DIFF_FLAG_PIN: list[str] = ["--no-color", "--no-ext-diff", "--no-show-signature"]
"""Sub-command flags, appended after the subcommand name (log/diff).

Each has a direct flag form, so a flag pins it rather than another ``-c``
— preferred per the review, and it reads as plumbing options alongside the
``-w``/``-U0``/``-M`` this module already passes. Real reproductions from
the review, all against a repo-local or ``~/.gitconfig`` setting this
module must not trust:

- ``diff.external`` set to any program makes the diff subprocess hard-fail
  (``GitUnavailableError``) instead of running git's own diff engine —
  ``--no-ext-diff`` disallows the external helper outright.
- ``log.showSignature = true`` on a repo with even one signed commit
  interleaves gpg/ssh signature-check text (e.g. ``No signature``) into
  the NUL-delimited metadata stream :func:`log_walk_u0` parses, corrupting
  that commit's sha field — with every commit signed this empties the
  metadata table entirely and the walk silently degrades to "no history
  found" (every line reads as the sentinel
  :data:`~otto.coverage.attribution.UNCOMMITTED`).
  ``--no-show-signature`` suppresses the check.
- ``color.ui`` forcing ANSI escapes into piped output is the same class of
  risk (not independently reproduced, but free to close off alongside the
  other two); ``--no-color`` pins it.
"""


_NO_INDEX_FLAG_PIN: list[str] = ["--no-color", "--no-ext-diff"]
"""``--no-index``-safe subset of ``_DIFF_FLAG_PIN``.

``git diff --no-index`` parses its options with a **restricted** parser
and rejects ``--no-show-signature`` outright (``error: unknown option``,
rc 128/129) even though plain ``git diff --no-show-signature`` accepts it
— so ``_DIFF_FLAG_PIN`` cannot be reused verbatim here. Dropping it costs
nothing: ``--no-index`` compares two paths and never displays a commit,
so there is no signature to suppress.
"""


def _pin(subcommand: str, *rest: str) -> list[str]:
    """Build a git argv for *subcommand* immune to hostile ambient config.

    Every porcelain command this module runs against the SUT repo goes
    through this so ``diff.mnemonicprefix``, ``diff.external``,
    ``log.showSignature``, and ``color.ui`` — the SUT repo's local config
    and the invoking user's ``~/.gitconfig`` alike — can never silently
    change what a caller parses back out of stdout. See ``_CONFIG_PIN``
    and ``_DIFF_FLAG_PIN`` for the specific reproductions this closes.
    :func:`diff_no_index_u0` / :func:`diff_no_index_dir_u0` use
    ``_pin_no_index`` instead — same protection, restricted flag set.
    """
    return [*_CONFIG_PIN, subcommand, *_DIFF_FLAG_PIN, *rest]


def _pin_no_index(*rest: str) -> list[str]:
    """Build a ``git diff --no-index`` argv immune to hostile ambient config.

    These two calls run outside any repository, on throwaway anchor files
    rather than the SUT tree, and were originally left unpinned on the
    reasoning that no repo config could therefore reach them. That
    reasoning is wrong — **git still loads the invoking user's global
    ``~/.gitconfig``** — and two common settings each corrupt the output
    silently (pinned by
    ``tests/unit/cov/test_anchor.py::TestHostileGlobalGitConfig``):

    - ``color.ui = always`` wraps every line in ANSI escapes, so the
      ``diff --git``/``---``/``@@`` prefix matching in
      :func:`~otto.coverage.capture.treediff.parse_multifile_u0` and
      :func:`~otto.coverage.capture.remap.parse_u0_hunks` matches nothing.
    - ``diff.mnemonicPrefix = true`` emits ``1/``/``2/`` path prefixes
      here — *different letters* from the ``c/``/``w/`` a repo diff emits,
      so ``_CONFIG_PIN``'s repo-side reproduction does not describe this
      one — and :func:`diff_no_index_dir_u0`'s ``<dir>/``-prefixed lookup
      then misses every file.

    Both corruptions are silent in the same way: an unparseable diff
    yields no hunks, and :class:`~otto.coverage.anchor.AnchorResolver`
    reads "no hunks" as "absent from the ``-w`` diff, therefore
    whitespace-only" — so a changed file is reported verbatim and stale
    manual coverage stays valid. ``diff.external`` fails loudly instead
    (rc 128), which is merely wrong rather than dangerous, and
    ``--no-ext-diff`` closes it off alongside the other two.
    """
    return [*_CONFIG_PIN, "diff", "--no-index", *_NO_INDEX_FLAG_PIN, *rest]


_WORK_TREE_PROBE: list[str] = ["rev-parse", "--is-inside-work-tree"]
"""Argv of the failure-path classification probe; see :func:`_translate_failure`."""


def _inside_work_tree(cwd: Path) -> bool:
    """Ask git whether *cwd* is inside a work tree (failure paths only).

    Goes through :func:`_run_raw` like every other call in this module, NOT
    straight to ``subprocess``: ``_run_raw`` is the chokepoint the spawn-budget
    guards monkeypatch (``tests/unit/cov/test_git_spawn_budget.py``,
    ``test_attribution.py``'s anti-vacuity control), and a spawn that bypasses
    it is a spawn those guards cannot see — the instrument has to keep
    counting every process, including the ones on failure paths.

    ``ok_codes`` admits 128 (and 1) so that "not a repo" is a normal ANSWER
    here rather than a failure that would re-enter classification. Runs only
    after a primary call already failed, so the success path the budget bounds
    is untouched.
    """
    try:
        out = _run_raw(_WORK_TREE_PROBE, cwd, (0, 1, 128))
    except GitUnavailableError:
        # git vanished between the primary call and this probe, or exited with
        # an rc nobody anticipated. Either way the probe cannot answer, and
        # the primary failure's own story is the better one to keep.
        return True
    # rc 0 with "false" means cwd is inside a .git DIRECTORY rather than a work
    # tree, which is what the question asks; anything else (rc 128, empty) is a
    # no.
    return out.strip() == b"true"


def _translate_failure(
    args: list[str], cwd: Path | None, proc: "subprocess.CompletedProcess[bytes]"
) -> GitUnavailableError:
    """Classify a non-ok git exit into the right :class:`GitUnavailableError`.

    The discriminator is a second git call, NOT the stderr text. git
    translates its own messages, so a ``"not a git repository" in str(e)``
    test — what every caller of this module used to do — silently stops
    discriminating under any non-English ``LC_MESSAGES``, and the fallback
    that follows is then chosen for the wrong reason. ``rev-parse
    --is-inside-work-tree`` answers the same question in an exit code.

    A ``cwd`` of ``None`` (the ``--no-index`` diffs) has no directory to ask
    about, so it can only be a command failure.

    The probe never classifies ITSELF (``args != _WORK_TREE_PROBE``), and that
    guard is load-bearing rather than tidy: the probe runs through
    ``_run_raw``, so a probe exiting outside its own ``ok_codes`` would arrive
    back here and run the probe again, forever.
    """
    stderr = proc.stderr.decode(errors="replace")
    message = f"git {' '.join(args)} failed (rc={proc.returncode}): {stderr.strip()}"
    if cwd is not None and args != _WORK_TREE_PROBE and not _inside_work_tree(cwd):
        return NotAGitRepoError(message)
    return GitCommandFailedError(message)


def _spawn_failure(cwd: Path | None) -> GitUnavailableError:
    """Classify a ``FileNotFoundError`` raised while SPAWNING git.

    Two unrelated causes land on that one exception, and only one of them is
    about git: a missing executable, or a *cwd* that does not exist —
    ``subprocess`` fails the chdir before it ever execs. The second used to be
    reported as "git executable not found", which was merely misleading while
    every caller swallowed it, and became a propagating lie once
    :func:`blob_sha` started re-raising :class:`GitMissingError`.
    """
    if cwd is not None and not cwd.is_dir():
        return NotAGitRepoError(f"{cwd} is not a directory")
    return GitMissingError("git executable not found")


def _run_raw(args: list[str], cwd: Path | None, ok_codes: tuple[int, ...] = (0,)) -> bytes:
    """Run git and return raw stdout bytes; translate subprocess errors uniformly."""
    try:
        proc = subprocess.run(  # noqa: S603
            ["git", *args],  # noqa: S607
            cwd=cwd,
            capture_output=True,
            text=False,
            check=False,
        )
    except FileNotFoundError as e:
        raise _spawn_failure(cwd) from e
    if proc.returncode not in ok_codes:
        raise _translate_failure(args, cwd, proc)
    return proc.stdout


def _run(args: list[str], cwd: Path | None, ok_codes: tuple[int, ...] = (0,)) -> str:
    return _run_raw(args, cwd, ok_codes).decode()


def _run_raw_input(
    args: list[str], cwd: Path | None, stdin: bytes, ok_codes: tuple[int, ...] = (0,)
) -> bytes:
    """Run git with *stdin* piped in; mirrors ``_run_raw``'s error translation.

    A separate helper (not an optional param on ``_run_raw``) so that
    ``_run_raw``'s signature — and the positional-forwarding monkeypatches
    committed spawn-count tests rely on — never changes.
    """
    try:
        proc = subprocess.run(  # noqa: S603
            ["git", *args],  # noqa: S607
            cwd=cwd,
            input=stdin,
            capture_output=True,
            text=False,
            check=False,
        )
    except FileNotFoundError as e:
        raise _spawn_failure(cwd) from e
    if proc.returncode not in ok_codes:
        raise _translate_failure(args, cwd, proc)
    return proc.stdout


def head_commit(repo_root: Path) -> str:
    """Return the current HEAD commit SHA."""
    return _run(["rev-parse", "HEAD"], repo_root).strip()


def rev_parse_commit(repo_root: Path, rev: str) -> str:
    """Resolve *rev* (full/abbreviated sha, ref) to a full commit sha.

    ``^{commit}`` peels tags and rejects non-commit objects; ``--verify``
    makes an unknown or ambiguous *rev* a loud :class:`GitCommandFailedError`
    instead of echoing the input back.
    """
    return _run(["rev-parse", "--verify", f"{rev}^{{commit}}"], repo_root).strip()


def config_value(repo_root: Path, key: str) -> str | None:
    """Return the git config value for *key* as seen from *repo_root*, or None.

    Runs in *repo_root* so the repo's own local config wins — a caller's
    process CWD (which may be a different repo, or no repo) never leaks into
    the answer. rc=1 (key unset) is a normal outcome, not an error.
    """
    out = _run(["config", key], repo_root, ok_codes=(0, 1)).strip()
    return out or None


def rev_list_first_parent(repo_root: Path) -> list[str]:
    """Every first-parent commit sha reachable from HEAD, newest first.

    One process for the whole list; callers index it to answer "is commit A
    at/before commit B on the mainline" without per-pair subprocesses.
    """
    return _run(["rev-list", "--first-parent", "HEAD"], repo_root).split()


def is_dirty(repo_root: Path) -> bool:
    """Return True if there are uncommitted changes."""
    return bool(_run(["status", "--porcelain"], repo_root).strip())


def blob_sha(repo_root: Path, relpath: Path, rev: str = "HEAD") -> str | None:
    """Return the SHA of a blob at a path/revision, or None if not found.

    The ``./`` prefix makes git resolve the path against ``repo_root``
    (our cwd) — a bare ``REV:<path>`` resolves against the repo toplevel,
    which is wrong whenever ``repo_root`` is a subdirectory of a larger
    repo (e.g. a sut checked out inside another project).
    """
    try:
        return _run(["rev-parse", f"{rev}:./{relpath.as_posix()}"], repo_root).strip()
    except (NotAGitRepoError, GitMissingError):
        # An unusable git ENVIRONMENT is not "this path is absent at this
        # rev". Folding either into None makes every file look new (and
        # git-missing used to do exactly that, silently).
        raise
    except GitCommandFailedError:
        return None


def hash_object(repo_root: Path, path: Path) -> str:
    """Return the SHA1 hash of a file object."""
    return _run(["hash-object", str(path)], repo_root).strip()


def hash_objects(repo_root: Path, paths: list[Path]) -> list[str]:
    """Hash many files in one spawn via ``git hash-object --stdin-paths``.

    Output order matches *paths* order. Reads paths from stdin instead of
    argv so a whole capture's worth of files costs one process instead of
    one per file (spec §9 batched fallback); applies the same
    attribute-driven filters as :func:`hash_object`, so results are
    identical file-for-file.
    """
    stdin = "".join(f"{p}\n" for p in paths).encode()
    out = _run_raw_input(["hash-object", "--stdin-paths"], repo_root, stdin)
    return out.decode().splitlines()


def blob_exists(repo_root: Path, sha: str) -> bool:
    """Return True if a blob exists in the repository."""
    try:
        _run(["cat-file", "-e", sha], repo_root)
    except (NotAGitRepoError, GitMissingError):
        # See :func:`blob_sha`: an unusable git environment must not read as
        # "this blob is absent".
        raise
    except GitCommandFailedError:
        return False
    return True


def blobs_exist(repo_root: Path, shas: list[str]) -> set[str]:
    """Return the subset of *shas* present in the object database, in one spawn.

    ``git cat-file --batch-check`` answers one line per input sha: ``<sha>
    <type> <size>`` when present, ``<sha> missing`` when absent.
    """
    stdin = "".join(f"{sha}\n" for sha in shas).encode()
    out = _run_raw_input(["cat-file", "--batch-check"], repo_root, stdin).decode()
    present: set[str] = set()
    for line in out.splitlines():
        if not line.endswith("missing"):
            present.add(line.split()[0])
    return present


def cat_blob(repo_root: Path, sha: str) -> bytes:
    """Return the contents of a blob."""
    return _run_raw(["cat-file", "blob", sha], repo_root)


_BATCH_HEADER_FIELDS = 3  # "<sha> <type> <size>"; a miss is "<sha> missing" (2 fields)


def cat_blobs(repo_root: Path, shas: list[str]) -> dict[str, bytes]:
    r"""Fetch many blobs' contents in one spawn via ``git cat-file --batch``.

    The batch stream is ``<sha> <type> <size>\n`` followed by exactly
    *size* content bytes and a trailing ``\n``, repeated per input sha
    (a sha git can't find instead emits ``<sha> missing\n``, skipped
    here). Duplicate shas in *shas* are fine — later entries just
    overwrite earlier ones in the returned dict.
    """
    stdin = "".join(f"{sha}\n" for sha in shas).encode()
    out = _run_raw_input(["cat-file", "--batch"], repo_root, stdin)
    result: dict[str, bytes] = {}
    pos = 0
    while pos < len(out):
        nl = out.index(b"\n", pos)
        header = out[pos:nl].decode()
        pos = nl + 1
        parts = header.split()
        sha = parts[0]
        if len(parts) != _BATCH_HEADER_FIELDS:  # "<sha> missing"
            continue
        size = int(parts[2])
        result[sha] = out[pos : pos + size]
        pos += size + 1  # skip the trailing newline after the content
    return result


def diff_worktree_file_u0(repo_root: Path, relpath: Path) -> str:
    """Return unified diff (U0, whitespace-insensitive) of HEAD vs worktree file.

    ``-w`` (``--ignore-all-space``) suppresses whitespace-only line
    modifications so a reformat/reindent does not invalidate manual
    coverage anchored to the untouched code. Safe for the line remapper:
    a whitespace-only modification is 1 line -> 1 line and shifts no
    counts, so hiding it loses no hunk-offset information (unlike
    ``--ignore-blank-lines``, which would hide count-changing hunks). The
    SUTs are C/C++, where intra-string whitespace is not coverage-
    relevant, so the one behavioural case ``-w`` also equates (spacing
    inside a string literal) is an accepted, immaterial false-valid.

    Pinned against hostile ambient config (``_pin``) — pre-existing
    exposure shared with :func:`diff_tree_u0`, closed in the same wave as
    the newer walk commands below since it was a one-line change.
    """
    return _run(_pin("diff", "-w", "-U0", "HEAD", "--", relpath.as_posix()), repo_root)


def diff_no_index_u0(path_a: Path, path_b: Path) -> str:
    """Return unified diff (U0, whitespace-insensitive) between two files outside a repo.

    ``-w`` matches :func:`diff_worktree_file_u0` so the report-time anchor
    chain (base_commit blob vs current file) ignores whitespace-only edits the
    same way the dirty-tree remap does. ``git diff --no-index`` exits 1
    when the files differ — that is success here; with ``-w`` a
    whitespace-only difference exits 0 with empty output (hunkless), which
    the remapper treats as verbatim.

    Pinned against hostile *global* git config (``_pin_no_index``) —
    running outside a repo does not exempt it from ``~/.gitconfig``.
    """
    return _run(_pin_no_index("-w", "-U0", str(path_a), str(path_b)), cwd=None, ok_codes=(0, 1))


def diff_no_index_dir_u0(dir_a: Path, dir_b: Path) -> str:
    """Return a unified diff (U0, whitespace-insensitive) between two sibling dirs.

    One spawn answers every changed file's diff for the batched blob-
    fallback index (spec §9 amendment) instead of one ``--no-index`` spawn
    per file, mirroring :func:`diff_no_index_u0`'s ``-w``/exit-code
    contract. *dir_a* and *dir_b* must be siblings (share a parent) —
    passing their bare names with ``cwd=dir_a.parent`` makes git report
    paths as ``<dir_a.name>/<rel>`` / ``<dir_b.name>/<rel>`` (after its own
    standard ``a/``/``b/`` prefix, which
    :func:`~otto.coverage.capture.treediff.parse_multifile_u0` already
    strips); the caller strips the remaining ``<dir_a.name>/`` prefix to
    recover capture-relative paths.

    Pinned against hostile *global* git config (``_pin_no_index``).
    This is the call ``diff.mnemonicPrefix`` breaks: ``1/``/``2/`` prefixes
    survive ``strip_side``'s ``a/``/``b/`` strip, so the ``<dir_a.name>/``
    lookup above misses every file and each one is read as unchanged.
    """
    return _run(
        _pin_no_index("-w", "-U0", dir_a.name, dir_b.name),
        cwd=dir_a.parent,
        ok_codes=(0, 1),
    )


def diff_tree_u0(repo_root: Path, base: str) -> str:
    """Tree-wide ``-U0`` diff of *base* vs the working tree, rename-detecting.

    One subprocess per capture replaces the per-file anchor diffs (spec
    §9). ``-M`` reports renames (spec §8.2: git's tracking is the whole
    policy); ``-w`` matches :func:`diff_worktree_file_u0`'s whitespace
    immunity (spec §8.1); ``--relative -- .`` scopes and re-roots paths
    when ``repo_root`` is nested inside a larger repository.

    Raises:
        GitCommandFailedError: *base* is not a resolvable commit here
            (GC'd after a squash-merge, or absent from a shallow clone).
            Callers fall back to the per-file blob chain — which is why the
            classification matters: a NotAGitRepoError/GitMissingError from
            the same call must NOT take that fallback.

    Pinned against hostile ambient config (``_pin``) — see
    :func:`diff_worktree_file_u0`.
    """
    return _run(
        _pin("diff", "-M", "-w", "-U0", "--relative", base, "--", "."),
        repo_root,
    )


def is_shallow(repo_root: Path) -> bool:
    """Return whether the repo is a shallow clone (affects anchor degradation hints)."""
    return _run(["rev-parse", "--is-shallow-repository"], repo_root).strip() == "true"


_REC_SEP = "\x1e"
"""ASCII record separator — delimits commits in the log stream.

A source line could contain anything, so the delimiter must be a byte that
effectively never occurs in text; ``\\x1e``/``\\x1f`` are the standard
choice and are what keeps a file full of quotes and braces from
fabricating commit boundaries.
"""
_FIELD_SEP = "\x1f"
_COMMIT_FIELDS = 4  # sha, subject, body, diff_text


@dataclass(frozen=True)
class CommitDiff:
    """One commit's metadata plus its ``-U0`` diff against its first parent."""

    sha: str
    subject: str
    body: str
    diff_text: str


def log_walk_u0(
    repo_root: Path, relpaths: list[str], *, first_parent: bool = True
) -> list[CommitDiff]:
    """Stream ``git log -p -U0 -w -M`` over *relpaths*, newest commit first.

    Two O(1) subprocesses: one for metadata (NUL-delimited, safe from control
    chars in subject/body), one for diffs (sha-only in-band, joined by sha).
    ``-w`` mirrors manual-validity contract; ``-M`` follows renames. Both
    calls are pinned against hostile ambient config (``_pin``).

    Raises:
        GitUnavailableError: the two streams' commit shas disagree — a
            sha the diff call found has no metadata entry (or vice versa).
            Both calls walk the same pathspec/``--first-parent`` setting,
            so a healthy repo always joins every commit; a mismatch means
            something corrupted one stream (this is what
            ``log.showSignature`` did before the pin above existed —
            interleaved gpg/ssh signature-check text broke the sha field a
            commit was keyed under) and partial attribution would be
            silently, plausibly wrong, so this raises instead of dropping
            the unjoined commit(s) on the floor.
    """
    if not relpaths:
        return []

    # Call 1: Get metadata with NUL delimiters (commits cannot contain NUL)
    args_meta = ["--format=%H%x00%s%x00%b%x00"]
    if first_parent:
        args_meta.append("--first-parent")
    args_meta += ["--", *relpaths]

    raw_meta = _run(_pin("log", *args_meta), repo_root)

    # Parse metadata: split by \x00, every 3 fields is one commit
    metadata: dict[str, tuple[str, str]] = {}
    if raw_meta:
        fields = raw_meta.split("\x00")
        for i in range(0, len(fields) - 1, 3):
            if i + 2 < len(fields):
                sha = fields[i].strip()
                subject = fields[i + 1]
                body = fields[i + 2]
                if sha:
                    metadata[sha] = (subject, body)

    # Call 2: Get diffs with sha as join key (no metadata, so no control char risk)
    args_diff = [
        f"--format={_REC_SEP}%H{_REC_SEP}",
        "-p",
        "-U0",
        "-w",
        "-M",
    ]
    if first_parent:
        args_diff.append("--first-parent")
    args_diff += ["--", *relpaths]

    raw_diff = _run(_pin("log", *args_diff), repo_root)

    # Parse diffs by splitting on \n\x1e; restore newline for symmetry
    out: list[CommitDiff] = []
    unjoined: list[str] = []
    if raw_diff:
        records_raw = raw_diff.split("\n" + _REC_SEP)
        for idx, record_raw in enumerate(records_raw):
            proc_record = record_raw
            # Restore \x1e prefix if lost (records after first had it consumed by split)
            if not proc_record.startswith(_REC_SEP):
                proc_record = _REC_SEP + proc_record
            if not proc_record.strip():
                continue
            # Parse \x1e<sha>\x1e<diff...>
            proc_record = proc_record[1:]  # Strip leading \x1e
            parts = proc_record.split(_REC_SEP, 1)  # Split into [sha, diff]
            sha = parts[0].strip()
            diff_text = parts[1] if len(parts) > 1 else ""
            # Normalize diff_text: strip leading newlines, restore trailing newline if consumed
            if diff_text:
                diff_text = diff_text.lstrip("\n")  # Remove leading newlines
                # Add back trailing newline consumed by split (all but last record)
                if idx < len(records_raw) - 1 and diff_text and not diff_text.endswith("\n"):
                    diff_text = diff_text + "\n"
            # Look up metadata by sha
            if sha in metadata:
                subject, body = metadata[sha]
                out.append(CommitDiff(sha=sha, subject=subject, body=body, diff_text=diff_text))
            elif sha:
                unjoined.append(sha)
    if unjoined:
        raise GitUnavailableError(
            f"git log metadata/diff join mismatch: {len(unjoined)} commit(s) from the "
            f"diff stream have no matching metadata entry (first: {unjoined[0]!r} of "
            f"{len(unjoined)}); attribution would otherwise silently degrade to partial "
            "results — this usually means ambient git config is polluting one of the "
            "two log streams"
        )
    return out


def name_status_walk_u0(repo_root: Path, *, first_parent: bool = True) -> list[str]:
    """Stream ``git log --name-status -M -w``, one raw block per commit, newest first.

    Deliberately **unrestricted** in pathspec, unlike every other walk in
    this module. A rename's old-side path must already be in the pathspec
    before ``-M`` can pair it — which is exactly the historical name this
    walk exists to *discover*, so it cannot itself be scoped to the paths
    under consideration without reintroducing the same bug
    ``otto.coverage.attribution._expand_historical_paths`` exists to
    fix. What keeps a whole-history walk cheap is dropping ``-p``: the
    payload is a couple of name-status lines per touched path instead of a
    full diff, so this costs a small fraction of :func:`log_walk_u0`'s
    output for the same commit count. ``-M -w`` match :func:`log_walk_u0`
    exactly so the two passes agree on which commits are renames — a
    mismatch would leave the ``-p`` pass short a path it needed.

    Each returned entry is one commit's raw ``--name-status`` text (its
    ``A``/``M``/``D``/``R<score>`` lines, tab-separated); callers pull
    rename records out themselves (``attribution.parse_rename_records``)
    since add/modify/delete lines carry no path-identity information this
    walk needs to expose.
    """
    args = ["log", "--name-status", "-M", "-w", "--format=%x00"]
    if first_parent:
        args.append("--first-parent")
    raw = _run(args, repo_root)
    if not raw:
        return []
    return raw.split("\x00")[1:]


def diff_worktree_u0(repo_root: Path, relpaths: list[str]) -> str:
    """One ``git diff -w -U0 HEAD`` covering *relpaths* — not one per file.

    The per-file sibling :func:`diff_worktree_file_u0` is right for the
    validity pass, which resolves one capture at a time; attribution covers
    the whole store at once and is budgeted at O(1) subprocesses. Pinned
    against hostile ambient config (``_pin``) — a repo-local or
    ``~/.gitconfig`` ``diff.mnemonicprefix = true`` renders ``c/``/``w/``
    path prefixes here instead of ``a/``/``b/``, which silently defeats
    ``_apply_worktree_overlay``'s path lookup and lets an uncommitted edit
    inherit the previous committer's ticket (final-review blocker 1).
    """
    if not relpaths:
        return ""
    return _run(_pin("diff", "-w", "-U0", "HEAD", "--", *relpaths), repo_root)
