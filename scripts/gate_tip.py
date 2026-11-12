#!/usr/bin/env python3
"""The landing's gate: run the oracle's plan, stamp each lane by tree, publish the status.

`make gate-tip` asks the oracle (``gate_scope.plan``) for the landing's tip over
merge-base(base, HEAD)..HEAD, where the base is the remote's main, fetched, unless
``--base`` names one. It runs every STAMPED lane in order and writes a stamp per lane
keyed by the committed tree; lanes some stamp already satisfies are skipped. LIVE lanes
(the history checks) are never stamped: the pre-push hook (``--pre-push``) runs them over
the landing range, after it finds every pushed branch's lanes stamped and refuses any push
to main.

The bed lease: the runner takes it itself, for its own pid, immediately before the first
bed lane (a goal in ``gate_scope.BED_GOALS``) it has to run. A tip of static lanes only,
or one whose bed lanes are all stamped already, never touches the bed. ``run`` is the
lease's one owner: a walk that ends, green or red, releases it before anything is
published; an interrupt whose lanes are gone reaps the bed, then releases; an interrupt
whose lanes would not drain keeps it (the bed is not idle: ``make bed-reap`` once it is).

The status: a green run posts the ``local-gate`` commit status on HEAD, whose
description (``local_gate_status.expected``) names the tree, the base and the stamped
lanes; a red run flips a success HEAD already carries to failure. Neither ever changes
the exit status. ``--status`` (``make gate-status``) posts it for a tree gated earlier
and then admits the pull request's run (its ``gate-admission`` job).

The store: ``<git common dir>/otto-gate-stamps/<tree>/<slug>-<8 hex>.json``,
one file per (tree, lane key). A rebase keeps a stamp whose patch replays ("carry").

Every worktree shares the store, so a run deletes only what cannot still be true:
before a lane runs, a corrupt or misnamed file at the gated tree's own path, and
every stamp for the lane whose tree is pruned, whoever wrote it, with every red
marker without a base on a pruned tree. Every other stamp stays, so a landing gated
earlier, here or in a peer, keeps its evidence. A lane that goes red leaves a red
marker at its (tree, key) path: never evidence, and the lookup stops there, so no
stamp of another tree is promoted to a tree that went red. Beside it, as
``<marker stem>.patch``, git writes the patch base..tree the lane went red on, so the
carry can still replay the marker once its tree is pruned; a marker with a base
stays when its tree is pruned. Only the lane's next outcome on that tree replaces a
marker, and its patch goes with it (or a hand deletes both).

The store lock (a flock on ``<store>/.lock``) serializes peers. Writers hold it
exclusively: every stamp or marker write, invalidate's load-then-delete,
promotion's write (its scan runs unlocked; the write re-validates the exact path and
the marker snapshot under the lock), and a status post from its re-validation to the
post's return (``gh``'s timeout bounds that hold). A check that judges the store
(the push check, ``--status``) holds it shared from its first read to its verdict,
so it judges one state of the store. No lane runs under either, and no holder takes
the other in the same process.
"""

import argparse
import hashlib
import json
import os
import platform
import re
import signal
import sys
import tempfile
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, TextIO, TypeVar

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))
from scripts import gate_scope  # noqa: E402
from scripts.bed_lease import LEASE_BUDGET_KNOBS  # noqa: E402
from scripts.gate_scope import (  # noqa: E402
    LIVE_GOALS,  # noqa: F401 -- the runner's goal census reads the classes through this module
    STAMPED_GOALS,  # noqa: F401 -- likewise
    UnknownGoalError,
    goals,
    is_bed,
    is_live,  # noqa: F401 -- the stamp tests classify the oracle's lines through this module
    lane_key,
)
from scripts.gate_stamp_common import (  # noqa: E402
    ZERO_SHA,
    GateError,
    _git,
    blocking_untracked,
    dirt_refusals,
    fetch_main,
    junit_snapshot_or_none,
    junit_written,
    nproc,
    remote_slug,
    run_git,
    stamps_root,
    store_lock,
    test_roots,
    tree_hash,
    tree_state,
    utc_now,
    write_atomically,
)
from scripts.local_gate_status import (  # noqa: E402
    GhRunner,
    admit_pr_run,
    expected,
    head_status,
    post_status,
    run_gh,
)
from scripts.process_group import UndrainedError, run_awaiting_descendants  # noqa: E402

STORE = "otto-gate-stamps"
FORMAT = 1
_STR_FIELDS = ("tree", "command", "base", "written_at", "checkout")
_RED_FIELDS = ("tree", "command", "written_at", "checkout")


def slug(key: str) -> str:
    """Return a readable, unique file stem: 40 folded characters plus 8 hex of the key's digest."""
    folded = re.sub(r"[^A-Za-z0-9.-]", "_", key[:40])
    return f"{folded}-{hashlib.sha256(key.encode()).hexdigest()[:8]}"


def stamp_path(root: Path, tree: str, key: str) -> Path:
    """Return where the (tree, key) stamp lives under *root*."""
    return root / tree / f"{slug(key)}.json"


def sidecar(path: Path) -> Path:
    """Return where the red marker at *path* keeps the patch it went red on."""
    return path.with_suffix(".patch")


@dataclass(frozen=True)
class Stamp:
    """One lane's evidence for one tree, as read from the store."""

    tree: str
    command: str
    base: str
    written_at: str
    checkout: str
    promoted_from: "str | None"
    path: Path
    timing: "dict | None" = None
    """What the lane cost (``lane_timing``); None when it did not run here, or predates it."""


def _read_record(path: Path) -> "dict | None":
    """Return the JSON object at *path* when it is one of this FORMAT, else None.

    The store's only file read: every reader parses what this returned, so each file is
    read once whatever it turns out to be.
    """
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict) or data.get("format") != FORMAT:
        return None
    return data


def read_stamp(path: Path) -> "Stamp | None":
    """Return the stamp at *path*, or None when it is unreadable.

    Unreadable: not JSON, not an object, another format, a missing field, or a field of
    the wrong type — a crashed write or an older format is never evidence.
    """
    return _stamp_from(_read_record(path), path)


def _stamp_from(data: "dict | None", path: Path) -> "Stamp | None":
    """Return the stamp *data* (read from *path*) records, or None."""
    if data is None or "red" in data:  # a red marker is never a stamp
        return None
    if any(not isinstance(data.get(name), str) for name in _STR_FIELDS):
        return None
    promoted = data.get("promoted_from")
    if promoted is not None and not isinstance(promoted, str):
        return None
    timing = _timing_from(data)
    try:
        goals(data["command"])  # an unbalanced quote, or a goal no longer known, is not evidence
    except ValueError:  # UnknownGoalError and shlex's ValueError alike
        return None
    return Stamp(
        tree=data["tree"],
        command=data["command"],
        base=data["base"],
        written_at=data["written_at"],
        checkout=data["checkout"],
        promoted_from=promoted,
        path=path,
        timing=timing,
    )


def _timing_from(data: dict) -> "dict | None":
    """Return the record's ``timing`` object, or None when it is not one with an int ``elapsed_s``.

    A report, never evidence: a malformed one is dropped, and the stamp stays readable.
    """
    timing = data.get("timing")
    if not isinstance(timing, dict) or not isinstance(timing.get("elapsed_s"), int):
        return None
    return dict(timing)


def write_stamp(
    root: Path,
    *,
    tree: str,
    key: str,
    base: str,
    checkout: str,
    promoted_from: "str | None" = None,
    timing: "dict | None" = None,
) -> Path:
    """Write the (tree, key) stamp atomically and return its path."""
    record = {
        "format": FORMAT,
        "tree": tree,
        "command": key,
        "base": base,
        "written_at": utc_now(),
        "checkout": checkout,
    }
    if promoted_from:
        record["promoted_from"] = promoted_from
    if timing is not None:
        record["timing"] = timing
    path = stamp_path(root, tree, key)
    write_atomically(path, json.dumps(record, indent=2, sort_keys=True) + "\n")
    sidecar(path).unlink(missing_ok=True)  # a red marker's patch goes with the marker
    return path


@dataclass(frozen=True)
class Red:
    """A red marker: the lane for ``command`` went red on ``tree``. Never evidence."""

    tree: str
    command: str
    written_at: str
    checkout: str
    path: Path
    base: "str | None" = None
    """The base commit the lane went red against; None for a marker written without one."""
    timing: "dict | None" = None
    """What the red lane cost (``lane_timing``); None when it never ran (a framing error)."""


def _red_from(data: "dict | None", path: Path) -> "Red | None":
    """Return the red marker *data* (read from *path*) records, or None."""
    if data is None or data.get("red") is not True:
        return None
    if any(not isinstance(data.get(name), str) for name in _RED_FIELDS):
        return None
    base = data.get("base")
    if base is not None and not isinstance(base, str):
        return None
    return Red(
        tree=data["tree"],
        command=data["command"],
        written_at=data["written_at"],
        checkout=data["checkout"],
        path=path,
        base=base,
        timing=_timing_from(data),
    )


def read_entry(path: Path) -> "Stamp | Red | None":
    """Read *path* once and return the stamp or the red marker it holds, or None."""
    data = _read_record(path)
    return _stamp_from(data, path) or _red_from(data, path)


def write_red(
    root: Path,
    *,
    tree: str,
    key: str,
    checkout: str,
    base: "str | None" = None,
    timing: "dict | None" = None,
    repo: "Path | None" = None,
) -> Path:
    """Write the red marker for (tree, key) atomically at the stamp's own path; return it.

    With a *base* and the *repo* holding it, the patch *base*..*tree* is written after the
    marker, by git (:func:`write_patch`), to the marker's :func:`sidecar`: what keeps the marker
    decidable once its tree is pruned. Any other marker drops a sidecar left at its path.
    """
    path = stamp_path(root, tree, key)
    record = {
        "format": FORMAT,
        "red": True,
        "tree": tree,
        "command": key,
        "written_at": utc_now(),
        "checkout": checkout,
    }
    if base is not None:
        record["base"] = base
    if timing is not None:
        record["timing"] = timing
    write_atomically(path, json.dumps(record, indent=2, sort_keys=True) + "\n")
    _write_sidecar(repo, base, tree, sidecar(path))
    return path


def _write_sidecar(repo: "Path | None", base: "str | None", tree: str, patch: Path) -> None:
    """Write the patch *base*..*tree* to *patch* by an atomic rename, or remove *patch*.

    Removed when there is no *repo* or *base*, or git cannot write a non-empty patch (the
    marker then replays from its tree while the tree exists).
    """
    if repo is None or base is None:
        patch.unlink(missing_ok=True)
        return
    patch.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=patch.parent, prefix=".patch-")
    os.close(fd)
    try:
        try:
            written = write_patch(repo, base, tree, Path(tmp))
        except GateError:  # no git to spawn: the marker is written all the same
            written = False
        if written:
            Path(tmp).replace(patch)
        else:
            patch.unlink(missing_ok=True)
    finally:
        Path(tmp).unlink(missing_ok=True)


def _red_reason(red: Red) -> str:
    return f"the lane went red on this tree ({red.written_at} in {red.checkout})"


@dataclass(frozen=True)
class Mismatch:
    """A stamp file whose name is not its command's, and the command the file claims."""

    path: Path
    claimed: str


@dataclass(frozen=True)
class Store:
    """Everything one read of the store found: the stamps, the red markers, and the rest."""

    stamps: "list[Stamp]"
    unreadable: "list[Path]"
    mismatched: "list[Mismatch]"
    red: "list[Red]"


def _identity_holds(stamp: Stamp, tree_dir: Path) -> "tuple[bool, bool]":
    """Return (tree matches its directory, file name matches its command)."""
    return stamp.tree == tree_dir.name, stamp.path.name == f"{slug(stamp.command)}.json"


def marker_snapshot(store: Store, key: str) -> "dict[Path, tuple[int, int, int]]":
    """Return every red marker of *key* in *store*: its path, and (mtime_ns, size, inode).

    A marker that vanishes while it is read maps to ``(-1, -1, -1)``. Two snapshots differ
    when a marker was added, removed or rewritten between them, whatever it now holds.
    The triple relies on every writer going through ``write_atomically``: its rename gives
    the file a fresh inode, so a same-size rewrite within one mtime tick is still seen; the
    blind spot left is an inode reused across two rewrites inside one tick.
    """
    seen: "dict[Path, tuple[int, int, int]]" = {}
    for red in store.red:
        if red.command != key:
            continue
        try:
            st = red.path.stat()
        except FileNotFoundError:
            seen[red.path] = (-1, -1, -1)
            continue
        seen[red.path] = (st.st_mtime_ns, st.st_size, st.st_ino)
    return seen


def load_store(root: Path) -> Store:
    """Read every file in the store once.

    A file that is not a stamp, or whose ``tree`` is not the directory it sits in, is
    unreadable; a stamp whose file name is not its command's is mismatched (a digest
    collision, or a hand edit). Neither is ever a candidate or a witness; the lookup
    names them only when nothing else satisfies. A red marker at its own (tree, key) path is
    neither: it lands in ``red``, never a candidate or a witness either. Only ``*.json``
    files are entries: a marker's patch :func:`sidecar` is read with its marker, never here.
    """
    stamps: "list[Stamp]" = []
    unreadable: "list[Path]" = []
    mismatched: "list[Mismatch]" = []
    red: "list[Red]" = []
    if root.is_dir():
        for tree_dir in sorted(p for p in root.iterdir() if p.is_dir()):
            for file in sorted(tree_dir.glob("*.json")):
                stamp = read_entry(file)
                if isinstance(stamp, Red):
                    if file == stamp_path(root, stamp.tree, stamp.command):
                        red.append(stamp)
                    else:
                        unreadable.append(file)
                    continue
                if stamp is None:
                    unreadable.append(file)
                    continue
                tree_ok, name_ok = _identity_holds(stamp, tree_dir)
                if not tree_ok:
                    unreadable.append(file)
                elif not name_ok:
                    mismatched.append(Mismatch(file, stamp.command))
                else:
                    stamps.append(stamp)
    return Store(stamps, unreadable, mismatched, red)


def exact_stamp(root: Path, tree: str, key: str) -> "Stamp | None":
    """Return the stamp at (tree, key)'s own path when it is one for exactly this tree and key."""
    path = stamp_path(root, tree, key)
    stamp = read_stamp(path)
    if stamp is None or stamp.tree != tree or stamp.command != key:
        return None
    return stamp


@dataclass(frozen=True)
class Lookup:
    """What the store holds for one (tree, key): the stamp, or why none counts.

    ``store`` is the Store this lookup loaded or was given — None after an exact hit — so a
    caller asking about many keys hands it to the next call and the history is read once.
    """

    stamp: "Stamp | None"
    reason: str
    store: "Store | None" = None
    promotion: str = ""
    """How another tree's stamp came to count: ``"carried"`` (across a rebase,
    :func:`_carry`); ``""`` for an exact hit or no stamp."""
    carry_missed: bool = False
    """True when the carry tried a candidate and none carried: ``reason`` is the carry's."""


CARRY_CAP = 8
"""How many of a key's newest other-base stamps the carry tries."""


@dataclass
class CarryContext:
    """What lets a lookup carry a bed stamp across a rebase, and the answers it memoizes.

    ``base`` is the landing's base now (the plan's ``base_sha``); the memos are keyed by
    ``replays_to``'s, ``gate_scope.plan``'s and ``is_ancestor``'s arguments, so one context
    handed to every lookup of a run asks git and the oracle each question once.
    """

    repo: Path
    base: str
    replays: "dict[tuple[str, str, str], str | None]" = field(default_factory=dict)
    plans: "dict[tuple[str, str], gate_scope.Plan | None]" = field(default_factory=dict)
    ancestry: "dict[tuple[str, str], bool | None]" = field(default_factory=dict)
    patches: "dict[tuple[Path, str], str | None]" = field(default_factory=dict)

    def behind(self, old: str) -> "bool | None":
        """Return :func:`is_ancestor` of *old* and ``base``, memoized."""
        if (old, self.base) not in self.ancestry:
            self.ancestry[(old, self.base)] = is_ancestor(self.repo, old, self.base)
        return self.ancestry[(old, self.base)]

    def replayed(self, base0: str, tree0: str) -> "str | None":
        """Return :func:`replays_to` of (*base0*, *tree0*) onto ``base``, memoized."""
        asked = (base0, tree0, self.base)
        if asked not in self.replays:
            self.replays[asked] = replays_to(self.repo, base0, tree0, self.base)
        return self.replays[asked]

    def replayed_red(self, red: "Red") -> "str | None":
        """Return the tree *red*'s patch makes on ``base``: from its sidecar, else its tree.

        Memoized; ``None`` when neither the sidecar nor the tree can be replayed.
        """
        patch = sidecar(red.path)
        if not patch.is_file():
            return self.replayed(red.base or "", red.tree)
        asked = (patch, self.base)
        if asked not in self.patches:
            self.patches[asked] = replay_patch(self.repo, patch, self.base)
        return self.patches[asked]

    def delta_plan(self, base0: str) -> "gate_scope.Plan | None":
        """Return the plan for main's delta *base0*..``base``, memoized; None when it fails."""
        asked = (base0, self.base)
        if asked not in self.plans:
            try:
                self.plans[asked] = gate_scope.plan(self.repo, base0, self.base)
            except (gate_scope.GateScopeError, UnknownGoalError, GateError):
                self.plans[asked] = None
        return self.plans[asked]


def _pruned(repo: Path, tree: str) -> bool:
    try:
        _git(repo, "cat-file", "-e", f"{tree}^{{tree}}")
    except GateError:
        return True
    return False


_OBJECT_NAME = re.compile(r"[0-9a-f]{40}|[0-9a-f]{64}")


def _pruned_trees(repo: Path, trees: "list[str]") -> "set[str]":
    """Return which of *trees* the object store no longer holds, in ONE git spawn.

    ``git cat-file --batch-check`` answers every ``<tree>^{tree}`` line on stdin in order;
    a line that does not say ``tree`` is pruned. A name that is not a full hex object id
    is pruned without asking, so nothing but object ids ever reaches git's stdin.
    """
    names = sorted(set(trees))
    asked = [t for t in names if _OBJECT_NAME.fullmatch(t)]
    pruned = {t for t in names if t not in asked}
    if not asked:
        return pruned
    out = _git(repo, "cat-file", "--batch-check", stdin="".join(f"{t}^{{tree}}\n" for t in asked))
    answers = out.splitlines()
    if len(answers) != len(asked):
        raise GateError(f"git cat-file --batch-check answered {len(answers)} of {len(asked)} trees")
    for tree, answer in zip(asked, answers, strict=True):
        if answer.split()[1:2] != ["tree"]:  # "<name> missing", or another object type
            pruned.add(tree)
    return pruned


def is_ancestor(repo: Path, old: str, new: str) -> "bool | None":
    """Say whether commit *old* is an ancestor of *new*: ``None`` when git cannot tell.

    ``git merge-base --is-ancestor`` exits 0 for yes and 1 for no; any other status (an
    unknown object, a corrupt store) or a failure to spawn git is not an answer.
    """
    try:
        status = run_git(repo, "merge-base", "--is-ancestor", old, new).returncode
    except GateError:
        return None
    return {0: True, 1: False}.get(status)


def replays_to(repo: Path, base0: str, tree0: str, base1: str) -> "str | None":
    """Return the tree that *tree0*'s change over *base0* makes when applied onto *base1*.

    The patch ``base0..tree0`` (binary blobs armoured, full object ids, no rename
    detection) is applied with ``git apply --cached`` to *base1* read into a throwaway
    index (its own ``GIT_INDEX_FILE`` in a fresh temporary directory), and that index's
    ``git write-tree`` is the answer; the working tree and the real index are never
    touched. A clean rebase of the landing reproduces this tree exactly; any amendment
    of the landing does not. ``git apply`` takes a hunk only where its context still
    matches, so a base that edited inside a hunk's context gives ``None`` even when a
    3-way rebase merges it cleanly: the conservative miss.

    The patch travels as a FILE in the same temporary directory (``diff-tree --output``,
    then ``git apply <file>``), never through Python: decoding it as text would fold
    every CRLF to LF and replace undecodable bytes, so an amendment that only drops a CR
    or rewrites such bytes would replay equal to the amended tree, a false match.

    An empty patch, an unknown object, any step that exits non-zero, a failure to spawn
    git and a failure of the temporary directory itself all give ``None``; this never
    raises.
    """

    def steps(scratch: Path) -> "str | None":
        patch = scratch / "patch"
        return (
            apply_patch(repo, patch, base1, scratch)
            if write_patch(repo, base0, tree0, patch)
            else None
        )

    return _in_scratch(steps)


def replay_patch(repo: Path, patch: Path, base1: str) -> "str | None":
    """Return the tree the patch file *patch* (:func:`write_patch`'s) makes applied onto *base1*.

    :func:`replays_to`'s second half, for a patch written earlier (a red marker's sidecar):
    the same throwaway index, the same ``None`` for anything that fails; this never raises.
    """
    return _in_scratch(lambda scratch: apply_patch(repo, patch, base1, scratch))


def _in_scratch(steps: "Callable[[Path], str | None]") -> "str | None":
    """Run *steps* in a fresh temporary directory; ``None`` when it or git fails."""
    try:
        with tempfile.TemporaryDirectory(prefix="gate-replay-") as scratch:
            return steps(Path(scratch))
    except (OSError, GateError):
        return None


def write_patch(repo: Path, base0: str, tree0: str, out: Path) -> bool:
    """Have git write the patch *base0*..*tree0* to the file *out*; return whether it is one.

    Binary blobs armoured, full object ids, no rename detection: the patch
    :func:`apply_patch` applies. False when git fails or the patch is empty. Only a failure
    to spawn git raises (:class:`GateError`).
    """
    diff = run_git(
        repo,
        "diff-tree",
        "-r",
        "-p",
        "--binary",
        "--full-index",
        "--no-renames",
        f"--output={out}",
        base0,
        tree0,
    )
    return diff.returncode == 0 and out.is_file() and out.stat().st_size > 0


def apply_patch(repo: Path, patch: Path, base1: str, scratch: Path) -> "str | None":
    """Apply the patch file *patch* onto *base1* in a throwaway index in *scratch*; its tree.

    ``None`` when any step exits non-zero; only a failure to spawn git raises.
    """
    env = {**os.environ, "GIT_INDEX_FILE": str(scratch / "index")}
    if run_git(repo, "read-tree", base1, env=env).returncode != 0:
        return None
    applied = run_git(repo, "apply", "--cached", "--whitespace=nowarn", str(patch), env=env)
    if applied.returncode != 0:
        return None
    written = run_git(repo, "write-tree", env=env)
    if written.returncode != 0:
        return None
    return written.stdout.strip() or None


def _exact_entry(root: Path, tree: str, key: str) -> "Stamp | Red | None":
    """Return the stamp or red marker at (tree, key)'s own path when it is for exactly them."""
    entry = read_entry(stamp_path(root, tree, key))
    if entry is not None and entry.tree == tree and entry.command == key:
        return entry
    return None


EntryT = TypeVar("EntryT", Stamp, Red)


def _newest_first(entries: "list[EntryT]") -> "list[EntryT]":
    """Return the store *entries* newest first; equal times fall back to the path, descending."""
    return sorted(entries, key=lambda e: (e.written_at, str(e.path)), reverse=True)


def _carry(ctx: CarryContext, store: Store, tree: str, key: str) -> "tuple[Stamp | None, str]":
    """Return a stamp of *key* carried to *tree* across a rebase onto ``ctx.base``.

    A candidate is one of the newest :data:`CARRY_CAP` stamps of *key* on another tree and
    another base, newest first. It carries when its base is behind ``ctx.base``, its patch
    replays onto ``ctx.base`` to exactly *tree* (:func:`replays_to`), no red marker of *key*
    replays to *tree* too, and main's delta from its base to ``ctx.base`` has no bed lane.
    The first candidate that replays scans EVERY red marker of *key*, once: one without a
    base blocks only its own tree; one whose base is not behind ``ctx.base`` is skipped; one
    whose base or patch cannot be placed refuses the carry, as does one that replays to
    *tree* (the lane went red on this patch on an earlier base). An oracle that fails for
    the delta counts as a bed lane.

    Returns ``(stamp, "")`` on a hit; ``(None, reason)`` on a miss, the reason the NEWEST
    tried candidate's first failed condition (a red marker's refusal ends the scan and is
    the reason); and ``(None, "")`` when there was no candidate.
    """
    candidates = _newest_first(
        [s for s in store.stamps if s.command == key and s.tree != tree and s.base != ctx.base]
    )[:CARRY_CAP]
    reason = ""
    scanned = False
    here = ctx.base[:12]
    for c in candidates:
        if ctx.behind(c.base) is not True:
            reason = reason or f"base {c.base[:12]} is not behind {here}"
            continue
        if ctx.replayed(c.base, c.tree) != tree:
            reason = reason or f"the patch differs on {here}"
            continue
        if not scanned:
            scanned = True
            refusal = _red_refusal(ctx, store, tree, key)
            if refusal:
                return None, refusal
        plan = ctx.delta_plan(c.base)
        if plan is None or plan.bed:
            classes = "unknown" if plan is None else ", ".join(plan.classes)
            reason = reason or f"main's delta {c.base[:12]}..{here} reaches the bed: {classes}"
            continue
        return c, ""
    return None, reason


def _red_refusal(ctx: CarryContext, store: Store, tree: str, key: str) -> str:
    """Return why a red marker of *key* refuses carrying a stamp to *tree*, or "".

    Every marker of *key* is asked, newest first: the scan has no cap. A marker replays from
    the patch beside it (its :func:`sidecar`) when it has one, else from its tree; one with
    neither cannot be replayed and refuses.
    """
    for red in _newest_first([r for r in store.red if r.command == key]):
        if red.base is None:
            continue  # a marker without a base blocks only its own tree
        behind = ctx.behind(red.base)
        if behind is False:
            continue
        if behind is None:
            return f"a red marker's base {red.base[:12]} cannot be placed; no carry"
        replayed = ctx.replayed_red(red)
        if replayed == tree:
            return f"the lane went red on this patch at tree {red.tree[:12]} after a rebase"
        if replayed is None:
            return f"a red marker for this key cannot be replayed (tree {red.tree[:12]}); no carry"
    return ""


def lookup(
    repo: Path,
    root: Path,
    tree: str,
    key: str,
    store: "Store | None" = None,
    base: str = "",
    carry: "CarryContext | None" = None,
) -> Lookup:
    """Find a stamp satisfying (tree, key): the exact file first, then every promoted candidate.

    A red marker for (tree, key) ends the search, whether the exact read or the store scan
    finds it: the lane went red on this tree, so no other tree's stamp is promoted to it.
    With a *carry* context (whose ``base`` is *base*) and a bed key, a stamp no candidate
    satisfies may still be carried across a rebase (:func:`_carry`), which answers one of
    three ways: a hit; a miss with a reason (``carry_missed``, reported before the older
    reasons below); or nothing tried (no candidate: the older reasons follow).
    Nothing that fails ends the search; the reason names the best evidence that did NOT count,
    in this order: why the carry refused (a base not behind *base*, a patch that differs on
    it, a red marker on this patch or one that cannot be placed or replayed, main's delta
    reaching the bed); a stamp of another tree; a command
    mismatch at the exact path; an unreadable file (corrupt, wrong-tree, or a pruned tree); a
    same-goal stamp for a different path list (with the remote's base when *base* is given);
    "no stamp".
    """
    if carry is not None and carry.base != base:
        raise ValueError(f"the carry context's base {carry.base!r} is not the lookup's {base!r}")
    exact_path = stamp_path(root, tree, key)
    entry = _exact_entry(root, tree, key)  # one read; on a hit the store is not touched
    if entry is not None:
        if isinstance(entry, Red):
            return Lookup(None, _red_reason(entry), store)
        return Lookup(entry, "", store)
    if store is None:
        store = load_store(root)
    # a marker the scan holds ends the search too: a peer's red may land after the exact read,
    # or come in a store the caller loaded earlier
    marker = next((r for r in store.red if r.tree == tree and r.command == key), None)
    if marker is not None:
        return Lookup(None, _red_reason(marker), store)
    file_name = exact_path.name
    unreadable = False
    nearest: "Stamp | None" = None
    for candidate in store.stamps:
        if candidate.command != key or candidate.tree == tree:
            continue
        if _pruned(repo, candidate.tree):
            unreadable = True
            continue
        nearest = candidate
    if carry is not None and is_bed(key):
        carried, why = _carry(carry, store, tree, key)
        if carried is not None or why:
            hit = carried is not None
            return Lookup(carried, why, store, "carried" if hit else "", carry_missed=not hit)
    if nearest is not None:
        return Lookup(
            None,
            f"stamped for tree {nearest.tree[:12]}, which differs from the tip's "
            f"(stamped in {nearest.checkout}, "
            f"base {nearest.base[:12]})",
            store,
        )
    # the file at the key's own path records another command: a misnamed file, or a genuine
    # digest collision (then it IS a valid stamp of that other key, and sits in store.stamps)
    claimed = next((m.claimed for m in store.mismatched if m.path == exact_path), None)
    if claimed is None:
        claimed = next((s.command for s in store.stamps if s.path == exact_path), None)
    if claimed is not None:
        return Lookup(
            None, f"command mismatch (the file at the key's path records {claimed!r})", store
        )
    if unreadable or any(p.name == file_name for p in store.unreadable):
        return Lookup(None, "unreadable stamp", store)
    goal = goals(key)
    other = [s for s in store.stamps if goals(s.command) == goal]
    if other:
        versus = f" vs the remote's {base[:12]}" if base else ""
        return Lookup(
            None, f"stamped for a different path list (base {other[-1].base[:12]}{versus})", store
        )
    return Lookup(None, "no stamp", store)


def find_stamp(
    repo: Path,
    root: Path,
    tree: str,
    key: str,
    *,
    base: str = "",
    carry: "CarryContext | None" = None,
) -> "Stamp | None":
    """Return the stamp satisfying (tree, key), or None; *carry* as :func:`lookup` takes it."""
    return lookup(repo, root, tree, key, base=base, carry=carry).stamp


def invalidate(repo: Path, root: Path, key: str, *, tree: str) -> "list[Path]":
    """Delete, for *key*, only the store files that cannot still be true; return their paths.

    Those are a file at (*tree*, *key*)'s own path that is unreadable or claims another
    command, every stamp for *key* whose tree is pruned, whoever wrote it, and every red
    marker for *key* without a base whose tree is pruned (it blocks only its own tree, which
    is gone). A deleted file's :func:`sidecar` goes with it. Everything else stays: a red
    marker with a base is evidence against its PATCH, so it stays when its tree is pruned
    (its sidecar replays it; without one the carry refuses it as one that cannot be
    replayed). That includes *tree*'s own stamp, which the lane's outcome
    overwrites, and every other tree's stamp, whichever checkout wrote it: a landing gated
    earlier, here or in a peer, keeps its evidence. A red marker on a live tree stays until
    the lane's next outcome on its tree replaces it, so an interrupted re-run keeps it. The
    load and the deletes hold the store lock, so a peer's write cannot land between them;
    the pruned check under it is one git spawn for every tree the key's files name.
    """
    with store_lock(root):
        store = load_store(root)
        here = stamp_path(root, tree, key)
        entries: "list[Stamp | Red]" = [e for e in [*store.stamps, *store.red] if e.command == key]
        pruned = _pruned_trees(repo, [e.tree for e in entries]) if entries else set()
        gone = [e.path for e in entries if e.tree in pruned and not _decidable_red(e)]
        gone += [p for p in store.unreadable if p == here]
        gone += [m.path for m in store.mismatched if m.path == here]
        for path in gone:
            path.unlink(missing_ok=True)
            sidecar(path).unlink(missing_ok=True)
    return gone


def _decidable_red(entry: "Stamp | Red") -> bool:
    """Return whether *entry* is a red marker with a base: evidence against its patch."""
    return isinstance(entry, Red) and entry.base is not None


@dataclass(frozen=True)
class RunOutcome:
    """What a runner saw: the exit status and the captured output ("" for a shell line)."""

    status: int
    output: str


Runner = Callable[["list[str] | str", Path, "dict[str, str]"], RunOutcome]
"""Run a shell line (inherit stdio, return "") or an argv (captured, stderr merged) in cwd."""

_STRIPPED = ("MAKEFLAGS", "MFLAGS", "PYTEST_ADDOPTS", "BASE")
LEASE_PURPOSE = "gate-tip"
"""The purpose the runner's own lease record names (``make bed-status`` shows it)."""
CANONICAL_REMOTE = "origin"
"""The remote whose main a landing is planned against: the CLI's default, and the one the
Makefile's ``gate-tip`` and ``gate-status`` pass (pinned); a push elsewhere is told so."""


def lane_env(environ: "dict[str, str]") -> "dict[str, str]":
    """Return the lane environment: the caller's, minus make's and pytest's knobs.

    Also minus the gate's own lease budget (the gate queues once; lanes re-enter).
    """
    return {k: v for k, v in environ.items() if k not in _STRIPPED and k not in LEASE_BUDGET_KNOBS}


def _default_runner(cmd: "list[str] | str", cwd: Path, env: "dict[str, str]") -> RunOutcome:
    """Run *cmd* for real: a shell line inherits stdio, an argv is captured with stderr merged.

    Through :func:`scripts.process_group.run_awaiting_descendants`, the lifecycle
    both gates share: a lane runs in a session of its own, the runner returns
    only when nothing the lane spawned is alive, and an interrupt is relayed to
    the lane's group and waited out before it propagates (a second one kills
    the group; see :func:`run` for what the gate then does).
    """
    shell = isinstance(cmd, str)
    result = run_awaiting_descendants(
        cmd, cwd=cwd, env=env, shell=shell, capture=not shell, label="gate-tip"
    )
    return RunOutcome(result.status, result.output)


@dataclass
class Options:
    """What one `gate-tip` (or `--status`) invocation was asked to do."""

    repo: Path
    base: "str | None" = None
    """The landing's base; None is the remote's main, fetched (a dry run reads it unfetched)."""
    remote: str = CANONICAL_REMOTE
    force: bool = False
    dry_run: bool = False
    overridden: "list[str]" = field(default_factory=list)
    runner: Runner = _default_runner
    out: TextIO = field(default_factory=lambda: sys.stdout)
    lab: "Path | None" = None
    """The cell's lab file (the Makefile's LAB_FILE): the bed a bed lane leases and reaps."""
    gh: GhRunner = run_gh
    """Runs ``gh``; bounded by ``GH_TIMEOUT``, so a hung post holds the store lock that long."""


def _say(opts: Options, text: str) -> None:
    print(f"gate-tip: {text}", file=opts.out, flush=True)


def resolve_base(opts: Options) -> str:
    """Return the landing's base: ``--base`` when given, else the remote's main.

    A dry run reads ``<remote>/main`` as the clone last fetched it and fetches nothing;
    a real run fetches it (:func:`gate_stamp_common.fetch_main`, the one base resolver).
    """
    if opts.base:
        return opts.base
    if opts.dry_run:
        return _git(opts.repo, "rev-parse", f"{opts.remote}/main").strip()
    return fetch_main(opts.repo, opts.remote)


def range_plan(repo: Path, base: str, tip: str) -> gate_scope.Plan:
    """Return the oracle's plan for merge-base(base, tip)..tip: a landing, judged once.

    The runner (*tip* HEAD) and the push check (*tip* a pushed sha) both plan through it.
    """
    merge_base = _git(repo, "merge-base", base, tip).strip()
    return gate_scope.plan(repo, merge_base, _git(repo, "rev-parse", tip).strip())


def _plan(opts: Options) -> gate_scope.Plan:
    """Return the oracle's plan for merge-base(base, HEAD)..HEAD."""
    return range_plan(opts.repo, resolve_base(opts), "HEAD")


def lane_timing(
    repo: Path, started_at: str, before: "dict[str, list[int]] | None", elapsed: int
) -> dict:
    """Return what one lane cost, as the stamp records it under ``timing``.

    ``started``/``finished`` are UTC ISO 8601 (wall clock), ``elapsed_s`` whole seconds
    of the monotonic clock (so a clock step cannot bend it; it may differ from the two
    wall stamps by a second), ``interpreter`` the Python ``gate_tip.py`` itself ran under
    (the project venv; a lane's own interpreters are the lane's business), ``nproc`` the
    CPUs it may run on, and ``junit`` the reports under ``reports/junit`` that are new or
    rewritten since *before* (the snapshot taken as the lane started; empty when either
    snapshot could not be taken). A report, never evidence: the push check never
    reads it.
    """
    return {
        "started": started_at,
        "finished": utc_now(),
        "elapsed_s": elapsed,
        "interpreter": platform.python_version(),
        "nproc": nproc(),
        "junit": junit_written(before, junit_snapshot_or_none(repo)),
    }


@dataclass(frozen=True)
class Row:
    """One lane's line in the timing table: its outcome, and its elapsed when it ran."""

    lane: str
    outcome: str
    elapsed: "int | None" = None
    written_at: str = ""


def _elapsed_cell(elapsed: "int | None") -> str:
    return f"{elapsed} s" if elapsed is not None else ""


def _table(rows: "list[Row]", *, dated: bool = False) -> "list[str]":
    """Render *rows* as the timing table: elapsed, outcome, [written at,] lane."""
    width = max((len(r.outcome) for r in rows), default=0)
    lines = []
    for row in rows:
        when = f"{row.written_at}  " if dated else ""
        lines.append(f"  {_elapsed_cell(row.elapsed):>8}  {row.outcome:<{width}}  {when}{row.lane}")
    return lines


@dataclass
class Lease:
    """The bed lease this run holds: the cell it is on, and the pid it is held for."""

    cell: Any
    pid: int


@dataclass
class _Run:
    """What one `run` knows while it walks the lanes."""

    opts: Options
    plan: gate_scope.Plan
    root: Path
    roots: "list[str]"
    env: "dict[str, str]"
    carry: CarryContext
    """What every lookup of this run carries with: one context, for the plan's base."""
    lease: "Lease | None" = None
    stamped: int = 0
    already: int = 0
    live: int = 0
    red: int = 0
    missing: "list[str]" = field(default_factory=list)
    red_keys: "list[str]" = field(default_factory=list)
    """The lanes that left a red marker in this run: what a failure status names."""
    rows: "list[Row]" = field(default_factory=list)

    @property
    def tree(self) -> str:
        """The tree every stamp of this run is keyed by: the plan's tip tree."""
        return self.plan.tree


def _new_run(opts: Options, plan: gate_scope.Plan) -> _Run:
    """Return the walk's state for *plan*, before any lane."""
    return _Run(
        opts=opts,
        plan=plan,
        root=stamps_root(opts.repo, STORE),
        roots=test_roots(opts.repo),
        env=lane_env(dict(os.environ)),
        carry=CarryContext(opts.repo, plan.base_sha),
    )


def _acquire_lease(ctx: _Run) -> None:
    """Take the bed lease for this process, on the cell ``--lab`` names; GateError when not.

    The runner's own pid holds it: every lane it spawns descends from it, so a lane's
    own acquire re-enters the hold. ``bed_lease`` loads otto, so it is imported here,
    never on the hook's path.
    """
    if ctx.opts.lab is None:
        raise GateError("--lab is required to run a bed lane")
    from scripts import bed_lease

    pid = os.getpid()
    try:
        cell = bed_lease.load_cell(ctx.opts.lab, bed_lease.root_from(None))
        status = bed_lease.acquire(cell, pid, LEASE_PURPOSE)
    except bed_lease.UsageError as exc:
        raise GateError(str(exc)) from exc
    if status != 0:
        raise GateError("the bed lease was not acquired")
    ctx.lease = Lease(cell, pid)


def _release_lease(ctx: _Run) -> None:
    """Release the lease this run holds (after the lease's own drain) and forget it."""
    lease, ctx.lease = ctx.lease, None
    if lease is None:
        return
    from scripts import bed_lease

    try:
        bed_lease.release(lease.cell, lease.pid, force=False)
    except bed_lease.UsageError as exc:
        _say(ctx.opts, f"the bed lease was not released ({exc}): see make bed-status")


class ReapInterrupted(KeyboardInterrupt):
    """A second interrupt cut the reap short: the lease is kept, and the run said so."""


def _interrupted(ctx: _Run) -> int:
    """After an interrupt whose lanes are gone: reap the bed and release, when a lease is held."""
    if ctx.lease is None:
        _say(ctx.opts, "interrupted before any bed lane; no lease was taken, nothing to reap")
        return EXIT_INTERRUPTED
    _say(ctx.opts, "interrupted; the lanes are gone: reaping the bed and releasing the lease")
    from scripts import bed_reap

    # Detached before the reap: whatever escapes it (a second Ctrl-C included) leaves the
    # lease kept, never released by run()'s finally over a bed the reap did not clean.
    lease, ctx.lease = ctx.lease, None
    try:
        bed_reap.reap(lease.cell, lease.pid)
    except Exception as exc:  # noqa: BLE001 -- any failure leaves the bed unknown: keep the lease
        _say(ctx.opts, f"the reap failed ({exc}); the lease is kept — run make bed-reap")
        return EXIT_INTERRUPTED
    except KeyboardInterrupt as exc:
        _say(
            ctx.opts,
            "interrupted during the reap; the lease is kept and the bed is not reaped "
            "— run make bed-reap",
        )
        raise ReapInterrupted from exc
    except BaseException:
        _say(ctx.opts, "the reap did not finish; the lease is kept — run make bed-reap")
        raise
    ctx.lease = lease
    _release_lease(ctx)
    return EXIT_INTERRUPTED


def _already_stamped(ctx: _Run, label: str, key: str) -> bool:
    """Report (and, outside a dry run, promote) a lane some stamp already satisfies for the tree.

    A dry run previews a carry (``would carry``) and writes nothing. A bed lane the carry
    tried and missed says why before it runs.
    """
    if ctx.opts.force:
        return False
    repo, root, tree, base = ctx.opts.repo, ctx.root, ctx.tree, ctx.plan.base_sha
    store: "Store | None" = None
    seen: "dict[Path, tuple[int, int, int]]" = {}
    # (a) the exact path; on a miss, the store and its markers' metadata, read as one state
    with store_lock(root, shared=True):
        entry = _exact_entry(root, tree, key)
        if entry is None:
            store = load_store(root)
            seen = marker_snapshot(store, key)
    if isinstance(entry, Stamp):
        return _report_stamped(ctx, label, key, "already stamped")
    if isinstance(entry, Red):
        return False
    # (b) the scan, its replays and plans, with no lock held
    found = lookup(repo, root, tree, key, store, base=base, carry=ctx.carry)
    if found.stamp is None:
        if found.carry_missed:
            _say(ctx.opts, f"{label} … no carry: {found.reason}")
        return False
    if found.stamp.tree == tree:
        return _report_stamped(ctx, label, key, "already stamped")
    if ctx.opts.dry_run:
        if found.promotion == "carried":
            said = f"would carry (promoted from {found.stamp.tree[:12]})"
        else:
            said = "already stamped"
        return _report_stamped(ctx, label, key, said)
    # (c) the write, after the exact path and the markers are re-validated under the lock
    with store_lock(root):
        entry = _exact_entry(root, tree, key)
        if isinstance(entry, Red):
            return False
        if isinstance(entry, Stamp):
            return _report_stamped(ctx, label, key, "already stamped")
        if marker_snapshot(load_store(root), key) != seen:
            _say(ctx.opts, f"{label} … carry abandoned (a red marker changed during the scan)")
            return False
        write_stamp(
            root,
            tree=tree,
            key=key,
            base=base,
            checkout=str(repo),
            promoted_from=found.stamp.tree,
        )
    return _report_stamped(ctx, label, key, _promoted_line(found.stamp, found.promotion, base))


def _promoted_line(stamp: Stamp, promotion: str, base: str) -> str:
    """Return the say text for *stamp*, of another tree, promoted to the tip's (onto *base*)."""
    t0 = stamp.tree[:12]
    if promotion == "carried":
        b0, b1 = stamp.base[:12], base[:12]
        return (
            f"already stamped (promoted from {t0}: the patch replays onto {b1}, "
            f"main's delta {b0}..{b1} has no bed lane)"
        )
    raise ValueError(f"unknown promotion {promotion!r}")


def _report_stamped(ctx: _Run, label: str, key: str, said: str) -> bool:
    """Say *said* for a lane a stamp satisfies, count it, and return True."""
    _say(ctx.opts, f"{label} … {said}")
    ctx.already += 1
    ctx.rows.append(Row(key, "already stamped"))
    return True


def _before_lane(ctx: _Run, label: str, key: str) -> bool:
    """Check the tree before a lane; say why and return False when the lane cannot run."""
    state = tree_state(ctx.opts.repo)
    why = dirt_refusals(state, ctx.roots)
    if why:
        _say(
            ctx.opts,
            f"{label} … NOT STAMPED (the tree is dirty before the lane: " + "; ".join(why) + ")",
        )
        ctx.red += 1
        ctx.rows.append(Row(key, "not stamped"))
        return False
    return True


def _measure(
    ctx: _Run, label: str, started_at: str, before: "dict[str, list[int]] | None", elapsed: int
) -> "dict | None":
    """Return the lane's timing, or None when measuring fails: the verdict never waits on it."""
    try:
        return lane_timing(ctx.opts.repo, started_at, before, elapsed)
    except Exception as exc:  # noqa: BLE001 -- a report must never decide a lane
        _say(ctx.opts, f"{label} … timing not recorded ({exc!r})")
        return None


def _mark_red(ctx: _Run, key: str, timing: "dict | None") -> None:
    """Count a red lane and leave its red marker, so nothing promotes to this tree for it."""
    with store_lock(ctx.root):
        write_red(
            ctx.root,
            tree=ctx.tree,
            key=key,
            checkout=str(ctx.opts.repo),
            base=ctx.plan.base_sha,
            timing=timing,
            repo=ctx.opts.repo,
        )
    ctx.red += 1
    ctx.red_keys.append(key)
    ctx.rows.append(Row(key, "red", timing["elapsed_s"] if timing else None))


def _after_lane(ctx: _Run, label: str, key: str, status: int, timing: "dict | None") -> bool:
    """Judge a finished lane and stamp it on green; return False when the run must stop.

    *timing* is what the lane cost, or None when measuring it failed (``_measure``).
    """
    repo = ctx.opts.repo
    after = tree_state(repo)
    dirty = after.tracked_dirty
    elapsed = timing["elapsed_s"] if timing else None
    if status != 0:
        _say(ctx.opts, f"{label} … RED (exit {status}, {_elapsed_cell(elapsed) or 'not timed'})")
        _mark_red(ctx, key, timing)
        return False
    blocking = blocking_untracked(after, ctx.roots)
    if tree_hash(repo) != ctx.tree or dirty or blocking:
        what = ", ".join(dirty + blocking) or "the tree hash moved"
        _say(ctx.opts, f"{label} … NOT STAMPED (the tree changed under the lane: {what})")
        ctx.red += 1
        ctx.rows.append(Row(key, "not stamped", elapsed))
        return False
    with store_lock(ctx.root):
        write_stamp(
            ctx.root,
            tree=ctx.tree,
            key=key,
            base=ctx.plan.base_sha,
            checkout=str(repo),
            timing=timing,
        )
    _say(ctx.opts, f"{label} … stamped ({_elapsed_cell(elapsed) or 'not timed'})")
    ctx.stamped += 1
    ctx.rows.append(Row(key, "stamped", elapsed))
    return True


def _stamp_lane(ctx: _Run, label: str, key: str) -> bool:
    """Run one stamped lane and stamp it on green; return False when the run must stop.

    The lease is taken here, after the tree check passed, before the first bed lane that
    actually runs.
    """
    if not _before_lane(ctx, label, key):
        return False
    if ctx.lease is None and is_bed(key):
        _acquire_lease(ctx)
    invalidate(ctx.opts.repo, ctx.root, key, tree=ctx.tree)
    _say(ctx.opts, f"{label} …")
    started_at, before, started = utc_now(), junit_snapshot_or_none(ctx.opts.repo), time.monotonic()
    status = ctx.opts.runner(key, ctx.opts.repo, ctx.env).status
    elapsed = int(time.monotonic() - started)
    timing = _measure(ctx, label, started_at, before, elapsed)
    return _after_lane(ctx, label, key, status, timing)


def _walk(ctx: _Run) -> None:
    """Visit each lane in order: name live ones, skip stamped ones, list or run the rest.

    The lease is taken before the first bed lane that actually runs (``_stamp_lane``),
    never for a lane already stamped or one a dirty tree refuses, and never in a dry run.
    """
    plan = ctx.plan
    total = len(plan.live) + len(plan.suspended) + len(plan.stamped)
    for index, line in enumerate(plan.live, 1):
        _say(ctx.opts, f"[{index}/{total}] {line} … live at push")
        ctx.live += 1
        ctx.rows.append(Row(lane_key(line), "live at push"))
    for index, key in enumerate(plan.suspended, len(plan.live) + 1):
        # named by the oracle, held back by the owner (gate_scope.SUSPENDED_LANES): never run,
        # never stamped, never demanded by the push hook
        _say(ctx.opts, f"[{index}/{total}] {key} … suspended ({gate_scope.SUSPENDED_LANES[key]})")
        ctx.rows.append(Row(key, "suspended"))
    first = len(plan.live) + len(plan.suspended) + 1
    for index, key in enumerate(plan.stamped, first):
        label = f"[{index}/{total}] {key}"
        if _already_stamped(ctx, label, key):
            continue
        if ctx.opts.dry_run:
            _say(ctx.opts, f"{label} … missing")
            ctx.missing.append(key)
            ctx.rows.append(Row(key, "missing"))
            continue
        if not _stamp_lane(ctx, label, key):
            break


def run(opts: Options) -> int:
    """Run the plan's lanes for HEAD, stamp each on green, publish; see the module docstring.

    The lease's one owner. A walk that returns, green or red (or raises anything but
    ``UndrainedError``), releases it before anything is published. An interrupt that
    escapes the walk (its lanes are drained) reaps the bed and then releases, and the exit
    is ``EXIT_INTERRUPTED``. An ``UndrainedError`` propagates with the lease kept.
    """
    repo = opts.repo
    if opts.overridden:
        _say(
            opts,
            "refusing — came from the command line, so this run is not the declared gate: "
            + ", ".join(opts.overridden),
        )
        return 2
    why = dirt_refusals(tree_state(repo), test_roots(repo))
    if why:
        _say(opts, "refusing — " + "; ".join(why))
        return 2
    try:
        plan = _plan(opts)  # the oracle classifies every line as it plans
    except UnknownGoalError as exc:
        _say(opts, f"refusing — {exc}")
        return 2
    count = int(_git(repo, "rev-list", "--count", f"{plan.base_sha}..HEAD").strip())
    base = opts.base or f"{opts.remote}'s main"
    _say(opts, f"merge-base {plan.base_sha[:9]} with {base}, {count} commit(s) to land")
    ctx = _new_run(opts, plan)
    undrained = False
    try:
        _walk(ctx)
    except KeyboardInterrupt:
        return _interrupted(ctx)
    except UndrainedError:
        undrained = True  # the bed is not idle: the lease stays, for `make bed-reap` to follow
        raise
    finally:
        if ctx.lease is not None and not undrained:
            _release_lease(ctx)
    _say(
        opts,
        f"{ctx.stamped} stamped, {ctx.already} already stamped, "
        f"{ctx.live} live at push, {ctx.red} red",
    )
    if ctx.rows:
        _say(opts, "timings (elapsed, outcome, lane)")
        for row in _table(ctx.rows):
            print(row, file=opts.out, flush=True)
    if opts.dry_run:
        return 1 if ctx.missing else 0
    if ctx.red:
        if ctx.red_keys:
            _publish_failure(ctx)
        return 1
    _publish_success(opts, plan)
    return 0


@dataclass(frozen=True)
class Missing:
    """One stamped lane the tree has no evidence for, and why."""

    lane: str
    reason: str


def missing_stamps(
    repo: Path, plan: gate_scope.Plan, store: "Store | None" = None
) -> "list[Missing]":
    """Return the plan's stamped lanes its tree has no satisfying stamp for.

    Lock-free: a caller that needs one store state holds the store lock around it (shared
    to judge, exclusive to publish). A red marker is never evidence.
    """
    root = stamps_root(repo, STORE)
    # lookup loads the store once, at the first lane without exact evidence; with no store
    # there is nothing to scan (and nothing a peer writes mid-check is promoted from)
    if store is None and not root.is_dir():
        store = Store([], [], [], [])
    missing: "list[Missing]" = []
    for key in plan.stamped:
        found = lookup(repo, root, plan.tree, key, store, base=plan.base_sha)
        store = found.store
        if found.stamp is None:
            missing.append(Missing(key, found.reason))
    return missing


def refusal_lines(
    plan: gate_scope.Plan, missing: "list[Missing]", *, ref: str, who: str
) -> "list[str]":
    """Return the refusal for *ref*: the unstamped lanes, the one command, the bypass."""
    tip, tree, base = plan.tip_sha[:12], plan.tree[:12], plan.base_sha[:12]
    return [
        (
            f"{who}: refusing {ref} ({tip}) — its gate is not stamped on tree {tree} "
            f"(range {base}..{tip}):"
        ),
        *(f"  {m.lane}: {m.reason}" for m in missing),
        f"run: make gate-tip   (on commit {tip}), then push again.",
        (
            "git push --no-verify skips this check; the local-gate status then stays absent "
            "and gate-admission holds the pull request's CI."
        ),
    ]


@dataclass(frozen=True)
class PublishOutcome:
    """Whether the ``local-gate`` status was posted, and why not when it was not."""

    posted: bool
    message: str


def _is_red(root: Path, tree: str, key: str) -> bool:
    """Return whether the store holds a red marker for (tree, key), at its own path."""
    return isinstance(_exact_entry(root, tree, key), Red)


def _publish_refusal(
    opts: Options, root: Path, plan: gate_scope.Plan, state: str, red_keys: "list[str]"
) -> str:
    """Return why the store no longer backs a *state* post (under its lock); "" when it does."""
    if state == "success":
        missing = missing_stamps(opts.repo, plan)
        if missing:
            lanes = "; ".join(f"{m.lane}: {m.reason}" for m in missing)
            return f"not every lane is stamped on {plan.tree[:12]}: {lanes}"
        return ""
    if not red_keys:
        return "a failure post names the red lanes"
    gone = [key for key in red_keys if not _is_red(root, plan.tree, key)]
    if gone:
        return f"{', '.join(gone)} no longer red on {plan.tree[:12]}: not posted"
    return ""


def publish(
    opts: Options,
    plan: gate_scope.Plan,
    *,
    state: str,
    slug: str,
    red_keys: "list[str] | None" = None,
) -> PublishOutcome:
    """Post the ``local-gate`` status for the plan's tip on *slug* in *state*, if the store agrees.

    Under the exclusive store lock, from the re-validation to the post's return, so no
    peer's stamp or red marker lands between them (``gh``'s timeout bounds the hold). A
    ``success`` is refused when any stamped lane lacks evidence; a ``failure`` when it names
    no red lane, or unless every lane of *red_keys* still holds its red marker (a later
    green run replaced it).
    """
    root = stamps_root(opts.repo, STORE)
    with store_lock(root):
        refusal = _publish_refusal(opts, root, plan, state, red_keys or [])
        if refusal:
            return PublishOutcome(False, refusal)
        outcome = post_status(slug, plan.tip_sha, expected(plan), state=state, gh=opts.gh)
    return PublishOutcome(outcome.ok, outcome.message)


def _report(opts: Options, plan: gate_scope.Plan, state: str, outcome: PublishOutcome) -> None:
    """Say what publishing did; it never changes the exit status."""
    if outcome.posted:
        what = "local-gate" if state == "success" else f"local-gate {state}"
        _say(opts, f"posted {what} on {plan.tip_sha[:12]}")
    else:
        _say(opts, f"local-gate not posted: {outcome.message}")


def _publish_failure(ctx: _Run) -> None:
    """Flip a success the tip already carries to failure: its gate just went red here."""
    opts = ctx.opts
    try:
        slug = remote_slug(opts.repo, opts.remote)
    except GateError as exc:
        _say(opts, f"local-gate not read: {exc}")
        return
    try:
        posted = head_status(slug, ctx.plan.tip_sha, gh=opts.gh)
    except GateError as exc:
        _say(opts, f"local-gate not read: {exc}")
        return
    if posted is None:
        return
    outcome = publish(opts, ctx.plan, state="failure", slug=slug, red_keys=ctx.red_keys)
    _report(opts, ctx.plan, "failure", outcome)


def _publish_success(opts: Options, plan: gate_scope.Plan) -> None:
    """Post ``local-gate`` for a green run's tip and say what happened."""
    try:
        slug = remote_slug(opts.repo, opts.remote)
    except GateError as exc:
        _report(opts, plan, "success", PublishOutcome(False, str(exc)))
        return
    _report(opts, plan, "success", publish(opts, plan, state="success", slug=slug))


def status_command(opts: Options) -> int:
    """`make gate-status`: post ``local-gate`` for HEAD's stamped tree, then admit its PR run."""
    plan = _plan(opts)
    refusal = stamp_refusal(opts.repo, plan, ref="HEAD", who="gate-status")
    if refusal:
        print("\n".join(refusal), file=sys.stderr)
        return 1
    try:
        slug = remote_slug(opts.repo, opts.remote)
    except GateError as exc:
        print(f"gate-status: local-gate not posted: {exc}", file=sys.stderr)
        return 1
    outcome = publish(opts, plan, state="success", slug=slug)
    if not outcome.posted:
        print(f"gate-status: local-gate not posted: {outcome.message}", file=sys.stderr)
        return 1
    print(f"gate-status: posted local-gate on {plan.tip_sha[:12]}", file=opts.out, flush=True)
    return admit_pr_run(slug, plan.tip_sha, gh=opts.gh)


def stamp_refusal(repo: Path, plan: gate_scope.Plan, *, ref: str, who: str) -> "list[str]":
    """Return why *ref* is refused: the plan's stamped lanes its tree lacks; empty when none.

    Judged under the shared store lock from its first read to its verdict, so no peer's
    marker or stamp lands while it reads. The hook and ``--status`` both ask it.
    """
    with store_lock(stamps_root(repo, STORE), shared=True):
        missing = missing_stamps(repo, plan)
    if not missing:
        return []
    return refusal_lines(plan, missing, ref=ref, who=who)


@dataclass(frozen=True)
class PushedRef:
    """One branch update of a push, as git's pre-push stdin names it."""

    remote_ref: str
    local_sha: str
    remote_sha: str


_PRE_PUSH_FIELDS = 4
MAIN_REF = "refs/heads/main"
_BRANCHES = "refs/heads/"
MAIN_REFUSAL = (
    "gate-tip: main takes no direct push; open a pull request (a non-canonical remote such "
    "as a mirror takes --no-verify: the ruleset, not this hook, protects the canonical main)"
)


def pushed_refs(stdin_text: str) -> "list[PushedRef]":
    """Return the branch updates of a push, in order: tags and deletes are not landings.

    git feeds pre-push one ``<local ref> <local sha> <remote ref> <remote sha>`` line per
    ref. The REMOTE ref decides: ``git push origin feature:main`` updates main from a branch
    of another name. An all-zero local sha is a delete.
    """
    refs: "list[PushedRef]" = []
    for line in stdin_text.splitlines():
        fields = line.split()
        if len(fields) != _PRE_PUSH_FIELDS:
            continue
        _local_ref, local_sha, remote_ref, remote_sha = fields
        if remote_ref.startswith(_BRANCHES) and local_sha != ZERO_SHA:
            refs.append(PushedRef(remote_ref, local_sha, remote_sha))
    return refs


def _is_canonical(repo: Path, remote: str) -> bool:
    """Return whether *remote* (the hook's ``$1``: a name, or a URL) is the canonical remote.

    git hands the hook the URL itself for a URL push, so the canonical remote's URL counts too.
    """
    if remote == CANONICAL_REMOTE:
        return True
    try:
        return remote == _git(repo, "remote", "get-url", CANONICAL_REMOTE).strip()
    except GateError:  # no canonical remote configured: nothing to compare with
        return False


def _refuse(lines: "list[str]") -> int:
    print("\n".join(lines), file=sys.stderr)
    return 1


# The hook runs while git holds the connection to the remote OPEN: git dials the remote
# first (it needs the remote refs to feed the hook on stdin), then runs this, then pushes.
# Every second here is a second an idle SSH channel has to survive, and GitHub closes one
# long before a test suite would finish: the push then fails with "Connection to github.com
# closed by remote host" after the check passed. So the hook runs no lane: it reads stamps
# and runs one history check, seconds, and the lanes ran earlier, in `make gate-tip`.
def pre_push(opts: Options, stdin_text: str) -> int:
    """Judge a push from git's pre-push stdin: 0 lets it through, anything else refuses it.

    No branch update (a tag, a delete): through, before anything is read. Any update of
    ``main``: refused, before any fetch (a pull request lands it). Otherwise every branch's
    tip must carry its plan's stamps, the plan taken over merge-base(the remote's main,
    fetched; the tip)..tip; then the plan's live line (the history check) runs for the ref
    that is the checked-out HEAD, over a working tree that speaks for its commit. Another
    ref's history is CI's ``lint-python`` to check, over the pull request's range.
    """
    refs = pushed_refs(stdin_text)
    if not refs:
        return 0
    if any(ref.remote_ref == MAIN_REF for ref in refs):
        return _refuse([MAIN_REFUSAL])
    repo = opts.repo
    try:
        main_sha = fetch_main(repo, opts.remote)
    except GateError as exc:
        return _refuse([f"gate-tip: {exc}"])
    plans: "list[gate_scope.Plan]" = []
    for ref in refs:
        try:
            plan = range_plan(repo, main_sha, ref.local_sha)
            refusal = stamp_refusal(repo, plan, ref=ref.remote_ref, who="gate-tip")
        except (GateError, gate_scope.GateScopeError, UnknownGoalError) as exc:
            where = f"{ref.remote_ref} ({main_sha[:12]}..{ref.local_sha[:12]})"
            return _refuse([f"gate-tip: {where}: {exc}"])
        if refusal:
            if not _is_canonical(repo, opts.remote):
                refusal.append(
                    f"the push targets {opts.remote}; make gate-tip planned against "
                    f"{CANONICAL_REMOTE}/main"
                )
            return _refuse(refusal)
        plans.append(plan)
    try:
        head = _git(repo, "rev-parse", "HEAD").strip()
        dirt = (
            dirt_refusals(tree_state(repo), test_roots(repo))
            if any(ref.local_sha == head for ref in refs)
            else []
        )
    except GateError as exc:
        return _refuse([f"gate-tip: {exc}"])
    if dirt:
        return _refuse(
            [f"gate-tip: refusing — {why} (the check reads the tree on disk)" for why in dirt]
        )
    checked = False
    for ref, plan in zip(refs, plans, strict=True):
        if ref.local_sha != head:
            _say(
                opts,
                f"{ref.remote_ref} ({ref.local_sha[:12]}) is not the checked-out tip; CI's "
                "lint-python runs check-breaking-marks over the pull request's range",
            )
            continue
        if checked:
            continue  # one HEAD, one plan: its history is checked once
        checked = True
        status = opts.runner(plan.live[0], repo, lane_env(dict(os.environ))).status
        if status != 0:
            return status
    return 0


def timings(repo: Path, root: Path, rev: str) -> "list[str]":
    """Return the stored timing table for *rev*'s tree (a rev, a commit id, or a tree id).

    One row per stamp or red marker the store holds for the tree, oldest first: elapsed
    (blank for a promoted stamp, or one older than the timing field), outcome, when it was
    written, and the lane. Read under the shared lock, like the push check.
    """
    tree = tree_hash(repo, rev)  # ``<tree-id>^{tree}`` is the tree id itself
    with store_lock(root, shared=True):
        store = load_store(root)
    entries: "list[Stamp | Red]" = [e for e in [*store.stamps, *store.red] if e.tree == tree]
    if not entries:
        return [f"gate-tip: no stamps for tree {tree[:12]}"]
    rows: "list[Row]" = []
    for entry in sorted(entries, key=lambda e: e.written_at):
        if isinstance(entry, Red):
            outcome = "red"
        elif entry.promoted_from:
            outcome = "promoted"
        else:
            outcome = "stamped"
        elapsed = entry.timing["elapsed_s"] if entry.timing else None
        rows.append(Row(entry.command, outcome, elapsed, entry.written_at))
    legend = "elapsed, outcome, written at, lane"
    return [
        f"gate-tip: timings for tree {tree[:12]} ({len(rows)} lane(s); {legend})",
        *_table(rows, dated=True),
    ]


EXIT_INTERRUPTED = 130


def _raise_interrupt(_signum: int, _frame: object) -> None:
    """SIGTERM is handled as SIGINT: the same unwind, the same reap and release."""
    raise KeyboardInterrupt


def main(argv: "list[str] | None" = None) -> int:
    """Parse the CLI and run, list, post the status, judge a push, or print a tree's timings.

    An interrupt (Ctrl-C, or SIGTERM) during a lane reaches the lane's group first and is
    waited out (the runner's contract); then, when the run holds the bed lease, the bed is
    reaped and the lease released, and the exit is 130. A second interrupt kills the lane's
    group; a group that does not empty within the kill budget is reported with its pids,
    no reap runs, the lease is kept, and the exit is 130 as well: the bed is reaped once it
    is confirmed idle (``make bed-reap``).
    """
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--repo", default=".")
    parser.add_argument(
        "--base", default=None, help="the landing's base (default, or empty: the remote's main)"
    )
    parser.add_argument("--force", action="store_true")
    parser.add_argument("--overridden", action="append", default=[], metavar="NAME")
    parser.add_argument("--remote", default=CANONICAL_REMOTE)
    parser.add_argument(
        "--lab", type=Path, default=None, metavar="LAB_JSON", help="the cell a bed lane leases"
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--dry-run", action="store_true")
    mode.add_argument(
        "--pre-push",
        action="store_true",
        help="judge a push from git's pre-push stdin (the .githooks/pre-push hook)",
    )
    mode.add_argument(
        "--status",
        action="store_true",
        help="post the local-gate status for HEAD's stamped tree, then admit its PR run",
    )
    mode.add_argument(
        "--timings",
        nargs="?",
        const="HEAD",
        metavar="REV",
        help="print the stored timing table for REV's tree (a rev, commit or tree id; HEAD)",
    )
    args = parser.parse_args(argv)
    repo = Path(args.repo).resolve()
    base = args.base or None  # the Makefile passes an empty --base for "not given"
    try:
        if args.timings is not None:
            print("\n".join(timings(repo, stamps_root(repo, STORE), args.timings or "HEAD")))
            return 0
        if args.pre_push:
            text = sys.stdin.read()
            return _run_interruptible(
                Options(repo=repo, remote=args.remote), lambda opts: pre_push(opts, text)
            )
        if args.status:
            return _run_interruptible(
                Options(repo=repo, base=base, remote=args.remote), status_command
            )
        return _run_interruptible(
            Options(
                repo=repo,
                base=base,
                remote=args.remote,
                force=args.force,
                dry_run=args.dry_run,
                overridden=args.overridden,
                lab=args.lab,
            )
        )
    except (GateError, gate_scope.GateScopeError) as exc:
        print(f"gate-tip: {exc}", file=sys.stderr)
        return 2


def _run_interruptible(opts: Options, body: "Callable[[Options], int] | None" = None) -> int:
    """Run *body* (:func:`run` by default) under the interrupt policy :func:`main` describes.

    :func:`run` owns the lease; ``--status`` (:func:`status_command`) and ``--pre-push``
    (:func:`pre_push`) take none, so an interrupt there is only reported.
    """
    previous = signal.signal(signal.SIGTERM, _raise_interrupt)
    try:
        return (body or run)(opts)
    except ReapInterrupted:
        return EXIT_INTERRUPTED  # run() already said the lease is kept and the bed unreaped
    except KeyboardInterrupt:
        # run() releases the lease on every way out but an undrained one: an interrupt that
        # escapes it came outside the walk (planning, publishing); --status and --pre-push
        # take no lease at all
        _say(opts, "interrupted; no lease is held, nothing to reap")
        return EXIT_INTERRUPTED
    except UndrainedError as exc:
        _say(
            opts,
            f"interrupted twice and {exc}; no reap until they are gone, and a lease held is "
            "kept: run `make bed-reap` once they are gone",
        )
        return EXIT_INTERRUPTED
    finally:
        signal.signal(signal.SIGTERM, previous)


if __name__ == "__main__":
    sys.exit(main())
