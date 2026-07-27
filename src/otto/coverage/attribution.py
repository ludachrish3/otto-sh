"""Per-line ticket attribution: one log walk, replayed backward from HEAD.

This is ``git blame``'s algorithm with blame's process model removed — see
the design spec §3. Blame spawns one process per file and re-opens the pack
indexes each time (~267 filesystem operations per file, which dominates on
NFS-backed checkouts). This module instead runs a **bounded, two-pass** log
walk: a cheap ``--name-status`` discovery pass finds every historical name
a covered file was ever known by (so a rename's pre-move history is not
silently dropped — see ``_expand_historical_paths``), then a single
``-p`` patch walk restricted to that *expanded* path set replays every
hunk backward from HEAD. Measured on otto-sh (740 first-parent commits,
189 covered files): 3,519 filesystem operations against 12,519 for an
unrestricted whole-repo walk and ~37,233 for ``git blame`` — a pathspec
restricted to only the *current* file names (no discovery pass) is
cheaper still, but is correctness-broken rather than merely slower (spec
§3.1's correction).
"""

import logging
from dataclasses import dataclass, field
from pathlib import Path

from .capture import gitio
from .capture.remap import Hunk, LineRemapper, parse_u0_hunks
from .capture.treediff import strip_side, unquote
from .tickets import TicketSpec

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class CommitFileDiff:
    """One file's slice of one commit's diff, keyed by its **new**-side path."""

    new_path: str
    old_path: str | None  # None = created by this commit
    hunks: list[Hunk] = field(default_factory=list)


def parse_commit_diff(diff_text: str) -> dict[str, CommitFileDiff]:
    """Parse one commit's ``-U0`` diff into ``{new_path: CommitFileDiff}``.

    Unlike :func:`~otto.coverage.capture.treediff.parse_multifile_u0`, file
    **creations are kept** (with ``old_path=None``): a commit that creates a
    file must claim every line it added, or those lines fall through
    unattributed. Deletions are dropped — they have no new-side lines.
    """
    out: dict[str, CommitFileDiff] = {}
    section: list[str] = []

    def flush() -> None:
        if not section:
            return
        old: str | None = None
        new: str | None = None
        saw_new_marker = False
        in_hunks = False
        for line in section:
            if line.startswith("@@ "):
                in_hunks = True
            if in_hunks:
                continue
            if line.startswith("rename from "):
                old = unquote(line[len("rename from ") :])
            elif line.startswith("rename to "):
                new = unquote(line[len("rename to ") :])
            elif line.startswith("--- "):
                old = strip_side(line[4:], "a/")
            elif line.startswith("+++ "):
                new = strip_side(line[4:], "b/")
                saw_new_marker = True
        if new is None:
            return  # deletion (new side /dev/null) or unparsable
        hunks = parse_u0_hunks("\n".join(section)) if saw_new_marker else []
        out[new] = CommitFileDiff(new_path=new, old_path=old, hunks=hunks)

    for line in diff_text.splitlines():
        if line.startswith("diff --git "):
            flush()
            section = [line]
        elif section:
            section.append(line)
    flush()
    return out


UNCOMMITTED = ""
"""Sentinel sha for working-tree lines that were never committed."""

NO_TICKET = "(no ticket)"
"""Display ticket id for a line whose owning commit named no ticket (spec §4/§6.1/§7).

Assigned by ``CoverageReporter._annotate_tickets``
for any attributed line whose :func:`attribute_tickets` id list came back
empty for a real (non-:data:`UNCOMMITTED`) sha — never by this module
directly, so :func:`attribute_tickets`'s own return value keeps meaning
"a real commit named no ticket" (pinned by
``test_commit_with_no_ticket_yields_empty_id_list``) rather than pre-empting
the reporter's sentinel assignment.
"""

UNCOMMITTED_TICKET = "(uncommitted)"
"""Display ticket id for a working-tree line that was never committed.

Same assignment split as :data:`NO_TICKET`: this module's :data:`UNCOMMITTED`
sha sentinel is the raw signal, and the reporter turns it into this display
id. The two are deliberately distinct constants — :data:`UNCOMMITTED` keys
:func:`attribute_lines`'s sha map, this one is a ticket id string that can
flow through the store, the export, and the SPA like any other id.
"""

_RESERVED_TICKET_IDS = frozenset({NO_TICKET, UNCOMMITTED_TICKET})
"""Display strings reserved for the two synthetic rows.

A user's ``[coverage.tickets].pattern`` could in principle match one of
these literally inside a real commit message (e.g. a commit whose subject
happens to contain the text ``(no ticket)``). Letting that match through
unfiltered would silently merge a real commit into the synthetic bucket
``CoverageReporter._annotate_tickets`` builds
separately — indistinguishable from a genuine sentinel-owned line. See
``_extract_real_tickets``.
"""


def _apply_worktree_overlay(
    repo_root: Path, frontier: dict[str, dict[int, int]], attributed: dict[str, dict[int, str]]
) -> None:
    """Claim uncommitted lines and rebase the rest into HEAD coordinates.

    Without this, a line edited but not yet committed would inherit the
    ticket of whoever last committed it — crediting a stranger's ticket with
    code they did not write.

    **One** diff for every file, never one per file: this module is pinned at
    O(1) git subprocesses by ``test_git_spawn_budget.py``.
    """
    diff_text = gitio.diff_worktree_u0(repo_root, list(frontier))
    if not diff_text.strip():
        return
    parsed = parse_commit_diff(diff_text)
    for relpath in list(frontier):
        fd = parsed.get(relpath)
        if fd is None:
            continue
        remapper = LineRemapper(fd.hunks)
        survivors: dict[int, int] = {}
        for head_line, cur in frontier[relpath].items():
            mapped = remapper.new_to_old(cur)
            if mapped is None:
                attributed[relpath][head_line] = UNCOMMITTED
            else:
                survivors[head_line] = mapped
        frontier[relpath] = survivors


_RENAME_RECORD_FIELDS = 3  # "R<score>\told\tnew"


def parse_rename_records(block: str) -> dict[str, str]:
    r"""Parse one commit's ``--name-status -M`` block into ``{new_path: old_path}``.

    Only rename records (``R<score>\told\tnew``) carry the path-identity
    information the discovery pass needs; add/modify/delete lines have no
    old side and are dropped.
    """
    renames: dict[str, str] = {}
    for line in block.splitlines():
        if not line.startswith("R"):
            continue
        parts = line.split("\t")
        if len(parts) == _RENAME_RECORD_FIELDS:
            renames[unquote(parts[2])] = unquote(parts[1])
    return renames


def _expand_historical_paths(repo_root: Path, covered: set[str], *, first_parent: bool) -> set[str]:
    """Expand *covered* HEAD paths to every historical name they were ever known by.

    This is the discovery pass that lets the patch walk stay pathspec-
    restricted without losing rename correctness. A plain ``git log --
    <covered paths>`` cannot find a covered file's pre-rename name: git's
    pathspec-restricted diff engine only pairs a rename when *both* sides
    are already in the pathspec — confirmed with real ``git log``/``git
    diff`` output, restricting to the new side alone renders the rename as
    a fresh creation, the exact bug this module used to have when
    ``_fetch_walk`` restricted to ``list(line_counts)`` directly. So this
    walk cannot itself be pathspec-restricted; what keeps it cheap is
    dropping ``-p`` (:func:`~otto.coverage.capture.gitio.name_status_walk_u0`)
    — a whole-history ``--name-status`` walk costs a small fraction of a
    full ``-p`` walk's payload for the same commit count.

    Replays the rename graph exactly like :func:`attribute_lines`'s
    frontier, but tracking path identity only (no line numbers): each
    covered path starts as its own "current name". Walking commits
    newest -> oldest, whenever a commit's rename set has the current name
    on its *new* side, the old side is added to the result and becomes the
    current name for the next (older) commit — so a file renamed twice
    pulls in both ancestors, transitively, and a file that never existed
    under another name adds nothing beyond itself.
    """
    historical = set(covered)
    current = {p: p for p in covered}
    for block in gitio.name_status_walk_u0(repo_root, first_parent=first_parent):
        renames = parse_rename_records(block)
        if not renames:
            continue
        for head_path, cur in list(current.items()):
            old = renames.get(cur)
            if old is not None:
                historical.add(old)
                current[head_path] = old
    return historical


def _fetch_walk(
    repo_root: Path, line_counts: dict[str, int], *, first_parent: bool
) -> list[gitio.CommitDiff]:
    """Fetch the log walk backing both public entry points.

    Bounded, not unrestricted: a ``--name-status`` discovery pass
    (``_expand_historical_paths``) first finds every historical name
    any covered file was ever known by, and the ``-p`` patch walk is then
    restricted to that **expanded** set rather than to ``.`` — smaller
    payload and fewer filesystem operations than walking the whole repo,
    bounded by covered-file churn instead of total repo churn, while still
    matching the ``git blame`` oracle on renames (spec §3.1). This is still
    a fixed number of subprocesses regardless of file count — one more for
    discovery, plus :func:`~otto.coverage.capture.gitio.log_walk_u0`'s own
    two — the property ``test_git_subprocess_count_is_constant_in_file_
    count`` pins.
    """
    covered = {p for p, n in line_counts.items() if n > 0}
    if not covered:
        return []
    historical = _expand_historical_paths(repo_root, covered, first_parent=first_parent)
    return gitio.log_walk_u0(repo_root, sorted(historical), first_parent=first_parent)


def attribute_lines(
    repo_root: Path,
    line_counts: dict[str, int],
    *,
    first_parent: bool = True,
    walk: list[gitio.CommitDiff] | None = None,
) -> dict[str, dict[int, str]]:
    """Map each file's line numbers to the sha of the commit that last touched it.

    *line_counts* is ``{repo-relative posix path: line count at HEAD}``.
    Pass *walk* to reuse an already-fetched log walk instead of spawning a
    second one.
    """
    attributed: dict[str, dict[int, str]] = {p: {} for p in line_counts}
    # relpath -> {original HEAD line -> that line's number in the tree
    # currently under consideration}. Both start equal and diverge as hunks
    # shift lines during the walk.
    frontier: dict[str, dict[int, int]] = {
        p: {n: n for n in range(1, count + 1)} for p, count in line_counts.items() if count > 0
    }
    _apply_worktree_overlay(repo_root, frontier, attributed)
    frontier = {p: live for p, live in frontier.items() if live}
    if not frontier:
        return attributed

    # Frontier keys track the file's path in the tree being walked, which
    # moves backward through renames; head_of maps that back to the HEAD path.
    head_of = {p: p for p in frontier}

    if walk is None:
        walk = _fetch_walk(repo_root, line_counts, first_parent=first_parent)

    for commit in walk:
        if not frontier:
            break
        parsed = parse_commit_diff(commit.diff_text)
        for cur_path in list(frontier):
            fd = parsed.get(cur_path)
            if fd is None:
                continue
            head_path = head_of[cur_path]
            if fd.old_path is None:
                # This commit created the file: it owns every line still live.
                for head_line in frontier[cur_path]:
                    attributed[head_path][head_line] = commit.sha
                del frontier[cur_path]
                del head_of[cur_path]
                continue
            remapper = LineRemapper(fd.hunks)
            survivors: dict[int, int] = {}
            for head_line, cur in frontier[cur_path].items():
                mapped = remapper.new_to_old(cur)
                if mapped is None:
                    attributed[head_path][head_line] = commit.sha
                else:
                    survivors[head_line] = mapped
            del frontier[cur_path]
            del head_of[cur_path]
            if survivors:
                frontier[fd.old_path] = survivors
                head_of[fd.old_path] = head_path

    # Anything the history never explained is not in git at all — an
    # untracked file, or a line the walk could not reach. Treat it as
    # uncommitted rather than silently dropping it from the report.
    for cur_path, live in frontier.items():
        for head_line in live:
            attributed[head_of[cur_path]][head_line] = UNCOMMITTED
    return attributed


def _extract_real_tickets(spec: TicketSpec, message: str, sha: str) -> list[str]:
    """:meth:`TicketSpec.extract`, with any reserved sentinel match dropped.

    A dropped match is logged (not silently kept) so the collision is
    discoverable without pretending the commit named a real ticket. See
    :data:`_RESERVED_TICKET_IDS`.
    """
    ids = spec.extract(message)
    collisions = [i for i in ids if i in _RESERVED_TICKET_IDS]
    if collisions:
        logger.warning(
            "commit %s: ticket pattern matched reserved sentinel id(s) %s; "
            "dropping them rather than merging this commit into the synthetic bucket",
            sha,
            collisions,
        )
    return [i for i in ids if i not in _RESERVED_TICKET_IDS]


def attribute_tickets(
    repo_root: Path,
    line_counts: dict[str, int],
    spec: TicketSpec,
    *,
    first_parent: bool = True,
) -> tuple[dict[str, dict[int, list[str]]], dict[str, list[str]], dict[str, dict[int, str]]]:
    """Attribute lines, then resolve each commit to its ticket ids.

    Returns ``(relpath -> line -> ticket ids, ticket id -> commit shas,
    relpath -> line -> owning sha)``. A line whose commit names no ticket
    maps to an empty id list — indistinguishable, by the first mapping
    alone, from a working-tree line that was never committed (both come
    back as ``[]``, since neither a no-match commit nor :data:`UNCOMMITTED`
    has an entry in ``tickets_of`` below). The third mapping is
    :func:`attribute_lines`'s raw sha result, already computed internally
    either way, returned so a caller can tell the two apart — and assign
    :data:`NO_TICKET` or :data:`UNCOMMITTED_TICKET` accordingly — without a
    second git log walk (see
    ``CoverageReporter._annotate_tickets``).
    """
    # One walk, shared by both passes — a second call would double this
    # module's git subprocess count for no benefit.
    walk = _fetch_walk(repo_root, line_counts, first_parent=first_parent)
    by_sha = attribute_lines(repo_root, line_counts, first_parent=first_parent, walk=walk)
    tickets_of = {c.sha: _extract_real_tickets(spec, f"{c.subject}\n{c.body}", c.sha) for c in walk}

    lines: dict[str, dict[int, list[str]]] = {}
    commits: dict[str, list[str]] = {}
    for relpath, per_line in by_sha.items():
        lines[relpath] = {}
        for lineno, sha in per_line.items():
            ids = list(tickets_of.get(sha, []))
            lines[relpath][lineno] = ids
            for ticket_id in ids:
                if sha not in commits.setdefault(ticket_id, []):
                    commits[ticket_id].append(sha)
    return lines, commits, by_sha
