#!/usr/bin/env python3
"""The landing's gate: stamp each lane the oracle names, and refuse a push without the stamps.

`make gate-tip` asks the oracle (scripts/gate_scope.py) for the tip list over
origin/main..HEAD, runs every STAMPED lane in order and writes a stamp per lane
keyed by the committed tree; the pre-push check (``missing_stamps``) recomputes
the tip list for the real pushed range and names every stamped lane without a
satisfying stamp. LIVE lanes — the history checks and docs — are never stamped:
the push runs them itself over the pushed range (scripts/gate_fresh.py).

The store: ``<git common dir>/otto-gate-stamps/<tree>/<slug>-<8 hex>.json``,
one file per (tree, lane key). A measured matrix (MEASURED_OUTPUTS) committed
exactly as a run produced it keeps every stamp of that run ("promotion").

Every worktree shares the store, so a run deletes only what cannot still be true:
before a lane runs, a corrupt or misnamed file at the gated tree's own path, and
every stamp or red marker for the lane whose tree is pruned, whoever wrote it.
Every other stamp stays, so a landing gated earlier, here or in a peer, keeps its
evidence. A lane that goes red leaves a red marker at its (tree, key) path: never
evidence, and the lookup stops there, so no stamp of another tree is promoted to
a tree that went red. Only the lane's next outcome on that tree replaces it.

The store lock (a flock on ``<store>/.lock``) serializes peers. Writers hold it
exclusively: every stamp or marker write, invalidate's load-then-delete, and
promotion's lookup-then-write. The pre-push check holds it shared from its first
read to its verdict, so it judges one state of the store: no peer's marker or
stamp lands while it reads, spawns git, and promotes. No lane runs under either,
and no holder takes the other in the same process.
"""

import argparse
import contextlib
import fcntl
import hashlib
import json
import os
import re
import shlex
import subprocess
import sys
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import TextIO

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))
from scripts import gate_scope  # noqa: E402
from scripts.gate_stamp_common import (  # noqa: E402
    ZERO_SHA,
    GateFreshError,
    TreeState,
    _git,
    blob_sha,
    blocking_untracked,
    dirt_refusals,
    stamps_root,
    test_roots,
    tree_hash,
    tree_state,
    write_atomically,
)

LIVE_GOALS = ["check-breaking", "lint-arch", "check-python", "docs", "gate-fresh"]
"""History-dependent (RANGE_TARGETS) or run by the push itself; never stamped."""
STAMPED_GOALS = [
    "coverage",
    "tests_hostless-3.14",
    "conformance-bed",
    "docs-captures-check",
    "stability-unit",
    "stability-unix",
    "stability-tunnel",
    "stability-embedded",
    "kmodcov",
    "dashboard",
    "dashboard-soak",
    "busybox",
    "chaos",
    "chaos-embedded",
    "quick",
    "quick-guards",
    "check-ts",
    "coverage-ts",
    "ruff",
]
"""Every goal the oracle can emit for a tip that the runner stamps (the census pins the set)."""
MEASURED_OUTPUTS = ["schemas/support_matrix.json", "schemas/kmodcov_matrix.json"]
"""Files a lane rewrites with what it measured; the Makefile says review and commit the diff."""
STORE = "otto-gate-stamps"
LOCK_FILE = ".lock"
"""The store lock's file under the store root (a dotfile, never a tree directory)."""
FORMAT = 1
_STR_FIELDS = ("tree", "command", "base", "written_at", "checkout")
_RED_FIELDS = ("tree", "command", "written_at", "checkout")


class UnknownGoalError(ValueError):
    """The oracle emitted a line this runner does not know how to classify."""


def goals(line: str) -> "list[str]":
    """Return the goals of one oracle line: make targets, the nox session, or the uv tool."""
    words = shlex.split(line)
    if words[:1] == ["make"]:
        found = [w for w in words[1:] if "=" not in w]
    elif (words[:2] == ["nox", "-s"] and words[2:3]) or (
        words[:2] == ["uv", "run"] and words[2:3] in (["ruff"], ["ty"])
    ):
        found = [words[2]]
    else:
        raise UnknownGoalError(f"the oracle emitted a lane this runner does not know: {line}")
    unknown = [g for g in found if g not in LIVE_GOALS and g not in STAMPED_GOALS]
    if not found or unknown:
        raise UnknownGoalError(f"the oracle emitted a lane this runner does not know: {line}")
    return found


def is_live(line: str) -> bool:
    """Return whether any goal of *line* is live (run by the push, never stamped)."""
    return any(g in LIVE_GOALS for g in goals(line))


_RANGE = re.compile(r"\s+RANGE=\S+$")


def lane_key(line: str) -> str:
    """Return the oracle line minus its RANGE decoration: base-independent by construction."""
    return _RANGE.sub("", line)


def slug(key: str) -> str:
    """Return a readable, unique file stem: 40 folded characters plus 8 hex of the key's digest."""
    folded = re.sub(r"[^A-Za-z0-9.-]", "_", key[:40])
    return f"{folded}-{hashlib.sha256(key.encode()).hexdigest()[:8]}"


def stamp_path(root: Path, tree: str, key: str) -> Path:
    """Return where the (tree, key) stamp lives under *root*."""
    return root / tree / f"{slug(key)}.json"


@dataclass(frozen=True)
class Stamp:
    """One lane's evidence for one tree, as read from the store."""

    tree: str
    command: str
    base: str
    measured: "dict[str, str]"
    written_at: str
    checkout: str
    promoted_from: "str | None"
    path: Path


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
    measured = data.get("measured")
    if not isinstance(measured, dict) or any(
        not isinstance(k, str) or not isinstance(v, str) for k, v in measured.items()
    ):
        return None
    promoted = data.get("promoted_from")
    if promoted is not None and not isinstance(promoted, str):
        return None
    try:
        goals(data["command"])  # an unbalanced quote, or a goal no longer known, is not evidence
    except ValueError:  # UnknownGoalError and shlex's ValueError alike
        return None
    return Stamp(
        tree=data["tree"],
        command=data["command"],
        base=data["base"],
        measured=dict(measured),
        written_at=data["written_at"],
        checkout=data["checkout"],
        promoted_from=promoted,
        path=path,
    )


def write_stamp(
    root: Path,
    *,
    tree: str,
    key: str,
    base: str,
    measured: "dict[str, str]",
    checkout: str,
    promoted_from: "str | None" = None,
) -> Path:
    """Write the (tree, key) stamp atomically and return its path."""
    record = {
        "format": FORMAT,
        "tree": tree,
        "command": key,
        "base": base,
        "measured": measured,
        "written_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "checkout": checkout,
    }
    if promoted_from:
        record["promoted_from"] = promoted_from
    path = stamp_path(root, tree, key)
    write_atomically(path, json.dumps(record, indent=2, sort_keys=True) + "\n")
    return path


@dataclass(frozen=True)
class Red:
    """A red marker: the lane for ``command`` went red on ``tree``. Never evidence."""

    tree: str
    command: str
    written_at: str
    checkout: str
    path: Path


def _red_from(data: "dict | None", path: Path) -> "Red | None":
    """Return the red marker *data* (read from *path*) records, or None."""
    if data is None or data.get("red") is not True:
        return None
    if any(not isinstance(data.get(name), str) for name in _RED_FIELDS):
        return None
    return Red(
        tree=data["tree"],
        command=data["command"],
        written_at=data["written_at"],
        checkout=data["checkout"],
        path=path,
    )


def read_entry(path: Path) -> "Stamp | Red | None":
    """Read *path* once and return the stamp or the red marker it holds, or None."""
    data = _read_record(path)
    return _stamp_from(data, path) or _red_from(data, path)


@contextlib.contextmanager
def store_lock(root: Path, *, shared: bool = False) -> Iterator[None]:
    """Hold the store's lock (flock on ``<root>/.lock``): exclusive, or *shared* for readers.

    A writer (exclusive) creates the store and the lock file if missing, and waits for every
    other holder. A reader (shared) waits only for a writer; with no store there is nothing
    to read, so it takes no lock and creates nothing. Only for short store steps, never
    while a lane runs, and never nested: two flocks of one process on two open files of the
    same lock can deadlock.
    """
    if shared and not root.is_dir():
        yield
        return
    root.mkdir(parents=True, exist_ok=True)
    with (root / LOCK_FILE).open("a") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_SH if shared else fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def write_red(root: Path, *, tree: str, key: str, checkout: str) -> Path:
    """Write the red marker for (tree, key) atomically at the stamp's own path; return it."""
    record = {
        "format": FORMAT,
        "red": True,
        "tree": tree,
        "command": key,
        "written_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "checkout": checkout,
    }
    path = stamp_path(root, tree, key)
    write_atomically(path, json.dumps(record, indent=2, sort_keys=True) + "\n")
    return path


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


def load_store(root: Path) -> Store:
    """Read every file in the store once.

    A file that is not a stamp, or whose ``tree`` is not the directory it sits in, is
    unreadable; a stamp whose file name is not its command's is mismatched (a digest
    collision, or a hand edit). Neither is ever a candidate or a witness; the lookup
    names them only when nothing else satisfies. A red marker at its own (tree, key) path is
    neither: it lands in ``red``, never a candidate or a witness either.
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


def witnesses(stamps: "list[Stamp]", tree: str) -> "dict[str, set[str]]":
    """Return, per measured path, every blob some stamp under *tree* recorded for it."""
    out: "dict[str, set[str]]" = {}
    for stamp in stamps:
        if stamp.tree == tree:
            for path, blob in stamp.measured.items():
                out.setdefault(path, set()).add(blob)
    return out


def satisfies(repo: Path, stamp: Stamp, tree: str, witnessed: "dict[str, set[str]]") -> bool:
    """Return whether *stamp* is evidence for *tree*.

    True for the same tree, or for a tree that differs from the stamp's only by measured
    outputs whose blobs are among *witnessed* (what some stamp of the stamp's tree recorded).
    A failing ``diff-tree`` (a pruned tree) is False, never an exception.
    """
    if stamp.tree == tree:
        return True
    try:
        raw = _git(repo, "diff-tree", "-r", "-z", "--name-only", stamp.tree, tree)
    except GateFreshError:
        return False
    changed = [p for p in raw.split("\0") if p]
    for path in changed:
        if path not in MEASURED_OUTPUTS or path not in witnessed:
            return False
        try:
            blob = _git(repo, "rev-parse", f"{tree}:{path}").strip()
        except GateFreshError:
            return False
        if blob not in witnessed[path]:
            return False
    return True


@dataclass(frozen=True)
class Lookup:
    """What the store holds for one (tree, key): the stamp, or why none counts.

    ``store`` is the Store this lookup loaded or was given — None after an exact hit — so a
    caller asking about many keys hands it to the next call and the history is read once.
    """

    stamp: "Stamp | None"
    reason: str
    store: "Store | None" = None


def _pruned(repo: Path, tree: str) -> bool:
    try:
        _git(repo, "cat-file", "-e", f"{tree}^{{tree}}")
    except GateFreshError:
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
        raise GateFreshError(
            f"git cat-file --batch-check answered {len(answers)} of {len(asked)} trees"
        )
    for tree, answer in zip(asked, answers, strict=True):
        if answer.split()[1:2] != ["tree"]:  # "<name> missing", or another object type
            pruned.add(tree)
    return pruned


def lookup(
    repo: Path, root: Path, tree: str, key: str, store: "Store | None" = None, base: str = ""
) -> Lookup:
    """Find a stamp satisfying (tree, key): the exact file first, then every promoted candidate.

    A red marker for (tree, key) ends the search, whether the exact read or the store scan
    finds it: the lane went red on this tree, so no other tree's stamp is promoted to it.
    Nothing that fails ends the search; the reason names the best evidence that did NOT count,
    in this order: a stamp of a tree that differs beyond the measured outputs; a command
    mismatch at the exact path; an unreadable file (corrupt, wrong-tree, or a pruned tree); a
    same-goal stamp for a different path list (with the remote's base when *base* is given);
    "no stamp".
    """
    exact_path = stamp_path(root, tree, key)
    entry = read_entry(exact_path)  # one read; on a hit the store is not touched
    if entry is not None and entry.tree == tree and entry.command == key:
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
        witnessed = witnesses(store.stamps, candidate.tree)
        if witnessed and satisfies(repo, candidate, tree, witnessed):
            return Lookup(candidate, "", store)
        nearest = candidate
    if nearest is not None:
        return Lookup(
            None,
            f"stamped for tree {nearest.tree[:12]}, which differs from the tip's "
            f"beyond the measured outputs (stamped in {nearest.checkout}, "
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


def find_stamp(repo: Path, root: Path, tree: str, key: str) -> "Stamp | None":
    """Return the stamp satisfying (tree, key), or None."""
    return lookup(repo, root, tree, key).stamp


def invalidate(repo: Path, root: Path, key: str, *, tree: str) -> "list[Path]":
    """Delete, for *key*, only the store files that cannot still be true; return their paths.

    Those are a file at (*tree*, *key*)'s own path that is unreadable or claims another
    command, and every stamp or red marker for *key* whose tree is pruned, whoever wrote it.
    Everything else stays. That includes *tree*'s own stamp, which the lane's outcome
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
        gone = [e.path for e in entries if e.tree in pruned]
        gone += [p for p in store.unreadable if p == here]
        gone += [m.path for m in store.mismatched if m.path == here]
        for path in gone:
            path.unlink(missing_ok=True)
    return gone


@dataclass(frozen=True)
class RunOutcome:
    """What a runner saw: the exit status and the captured output ("" for a shell line)."""

    status: int
    output: str


Runner = Callable[["list[str] | str", Path, "dict[str, str]"], RunOutcome]
"""Run a shell line (inherit stdio, return "") or an argv (captured, stderr merged) in cwd."""

_CHECK_USAGE = 2
_STRIPPED = ("MAKEFLAGS", "MFLAGS", "PYTEST_ADDOPTS", "BASE")
COVERAGE_GATE_OPTIONS = ["--threshold", "--selection", "--browsers", "--primary"]
"""The declared coverage gate's option sequence (the Makefile's COVERAGE_STAMP_GATE, pinned)."""


def lane_env(environ: "dict[str, str]") -> "dict[str, str]":
    """Return the environment a lane runs in: the caller's, minus make's and pytest's knobs."""
    return {k: v for k, v in environ.items() if k not in _STRIPPED}


def _default_runner(cmd: "list[str] | str", cwd: Path, env: "dict[str, str]") -> RunOutcome:
    """Run *cmd* for real: a shell line inherits stdio, an argv is captured with stderr merged."""
    if isinstance(cmd, str):
        completed = subprocess.run(  # noqa: S602 -- the oracle's own lines
            cmd, shell=True, cwd=cwd, env=env, check=False
        )
        return RunOutcome(completed.returncode, "")
    completed = subprocess.run(  # noqa: S603
        cmd,
        cwd=cwd,
        env=env,
        check=False,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    return RunOutcome(completed.returncode, completed.stdout)


@dataclass
class Options:
    """What one `gate-tip` invocation was asked to do."""

    repo: Path
    base: str = "origin/main"
    force: bool = False
    dry_run: bool = False
    overridden: "list[str]" = field(default_factory=list)
    runner: Runner = _default_runner
    out: TextIO = field(default_factory=lambda: sys.stdout)


class FramingError(GateFreshError):
    """The declared coverage gate holds a value the line framing cannot carry."""


def _say(opts: Options, text: str) -> None:
    print(f"gate-tip: {text}", file=opts.out, flush=True)


def _tip_list(repo: Path, base: str) -> "tuple[str, list[str]]":
    """Return (merge-base sha, the oracle's tip list over merge-base..HEAD)."""
    merge_base = _git(repo, "merge-base", base, "HEAD").strip()
    head = _git(repo, "rev-parse", "HEAD").strip()
    return merge_base, gate_scope.tip_commands(repo, merge_base, head)


def declared_coverage_gate(opts: Options, env: "dict[str, str]") -> "list[str]":
    """Ask make for the declared coverage gate, one argv element per line, and check the framing."""
    outcome = opts.runner(["make", "-s", "coverage-gate-args"], opts.repo, env)
    if outcome.status != 0:
        raise GateFreshError("make -s coverage-gate-args failed:\n" + outcome.output)
    text = outcome.output
    lines = text.split("\n")
    if lines and lines[-1] == "":
        lines.pop()
    if len(lines) != 2 * len(COVERAGE_GATE_OPTIONS) or lines[0::2] != COVERAGE_GATE_OPTIONS:
        raise FramingError(
            "cannot frame the declared coverage gate as "
            + " ".join(f"{o} <value>" for o in COVERAGE_GATE_OPTIONS)
            + " (a value holds a newline, or the Makefile's COVERAGE_STAMP_GATE changed shape): "
            + repr(text)
        )
    return lines


def coverage_check(opts: Options, env: "dict[str, str]", gate_args: "list[str]") -> int:
    """Run the coverage stamp's own check for HEAD's tree, show its output, return 0/1/2."""
    argv = ["uv", "run", "python", "scripts/coverage_stamp.py", "check", *gate_args]
    outcome = opts.runner(argv, opts.repo, env)
    text = outcome.output
    if text:
        opts.out.write(text if text.endswith("\n") else text + "\n")
    return outcome.status


def _measured_dirt(state: TreeState) -> "tuple[list[str], list[str]]":
    """Split tracked dirt into measured outputs and everything else."""
    measured = [p for p in state.tracked_dirty if p in MEASURED_OUTPUTS]
    other = [p for p in state.tracked_dirty if p not in MEASURED_OUTPUTS]
    return measured, other


def _run_lane(opts: Options, line: str, env: "dict[str, str]") -> int:
    """Run one stamped lane; the coverage lane goes through the coverage stamp's check first."""
    if goals(line) == ["coverage"]:
        gate_args = declared_coverage_gate(opts, env)
        status = coverage_check(opts, env, gate_args)
        if status == _CHECK_USAGE:
            return _CHECK_USAGE
        if status == 0 and not opts.force:
            return 0
        status = opts.runner(line, opts.repo, env).status
        if status != 0:
            return status
        return coverage_check(opts, env, gate_args)
    return opts.runner(line, opts.repo, env).status


@dataclass
class _Run:
    """What one `run` knows while it walks the lanes."""

    opts: Options
    merge_base: str
    root: Path
    tree: str
    roots: "list[str]"
    env: "dict[str, str]"
    stamped: int = 0
    already: int = 0
    live: int = 0
    red: int = 0
    stopped: bool = False
    missing: "list[str]" = field(default_factory=list)


def _already_stamped(ctx: _Run, label: str, key: str) -> bool:
    """Report (and, outside a dry run, promote) a lane some stamp already satisfies for the tree."""
    if ctx.opts.force:
        return False
    if ctx.opts.dry_run:
        with store_lock(ctx.root, shared=True):
            found = find_stamp(ctx.opts.repo, ctx.root, ctx.tree, key)
    else:
        with store_lock(ctx.root):  # a peer's red marker cannot land between lookup and write
            found = find_stamp(ctx.opts.repo, ctx.root, ctx.tree, key)
            if found is not None and found.tree != ctx.tree:
                write_stamp(
                    ctx.root,
                    tree=ctx.tree,
                    key=key,
                    base=ctx.merge_base,
                    measured={},
                    checkout=str(ctx.opts.repo),
                    promoted_from=found.tree,
                )
    if found is None:
        return False
    _say(ctx.opts, f"{label} … already stamped")
    ctx.already += 1
    return True


def _before_lane(ctx: _Run, label: str) -> bool:
    """Check the tree before a lane; say why and return False when the lane cannot run."""
    state = tree_state(ctx.opts.repo)
    measured, _other = _measured_dirt(state)
    if measured:
        _say(
            ctx.opts,
            f"commit the measured outputs ({', '.join(measured)}) and run "
            "make gate-tip again; the stamps already written stay valid for that commit",
        )
        ctx.stopped = True
        return False
    why = dirt_refusals(state, ctx.roots)
    if why:
        _say(
            ctx.opts,
            f"{label} … NOT STAMPED (the tree is dirty before the lane: " + "; ".join(why) + ")",
        )
        ctx.red += 1
        return False
    return True


def _mark_red(ctx: _Run, key: str) -> None:
    """Count a red lane and leave its red marker, so nothing promotes to this tree for it.

    A lane that could not be framed (a GateFreshError before it ran) leaves one too.
    """
    with store_lock(ctx.root):
        write_red(ctx.root, tree=ctx.tree, key=key, checkout=str(ctx.opts.repo))
    ctx.red += 1


def _after_lane(ctx: _Run, label: str, line: str, key: str, status: int, elapsed: int) -> bool:
    """Judge a finished lane and stamp it on green; return False when the run must stop."""
    repo = ctx.opts.repo
    after = tree_state(repo)
    measured, other = _measured_dirt(after)
    if status != 0:
        for path in measured:
            _say(
                ctx.opts,
                f"the lane failed and still rewrote {path}: restore it "
                f"(git checkout -- {path}) or review it before the next run",
            )
        _say(ctx.opts, f"{label} … RED (exit {status}, {elapsed} s)")
        _mark_red(ctx, key)
        return False
    blocking = blocking_untracked(after.untracked, ctx.roots)
    if tree_hash(repo) != ctx.tree or other or blocking:
        what = ", ".join(other + blocking) or "the tree hash moved"
        _say(ctx.opts, f"{label} … NOT STAMPED (the tree changed under the lane: {what})")
        ctx.red += 1
        return False
    blobs = {path: blob_sha(repo, path) for path in measured}
    with store_lock(ctx.root):
        write_stamp(
            ctx.root,
            tree=ctx.tree,
            key=key,
            base=ctx.merge_base,
            measured=blobs,
            checkout=str(repo),
        )
    for path in measured:
        _say(ctx.opts, f"{line} rewrote {path}: review and commit, or restore it")
    _say(ctx.opts, f"{label} … stamped ({elapsed} s)")
    ctx.stamped += 1
    return True


def _stamp_lane(ctx: _Run, label: str, line: str, key: str) -> bool:
    """Run one stamped lane and stamp it on green; return False when the run must stop."""
    if not _before_lane(ctx, label):
        return False
    invalidate(ctx.opts.repo, ctx.root, key, tree=ctx.tree)
    _say(ctx.opts, f"{label} …")
    started = time.monotonic()
    try:
        status = _run_lane(ctx.opts, line, ctx.env)
    except GateFreshError as exc:
        _say(ctx.opts, f"{label} … RED ({exc})")
        _mark_red(ctx, key)
        return False
    return _after_lane(ctx, label, line, key, status, int(time.monotonic() - started))


def _walk(ctx: _Run, kinds: "list[tuple[str, bool]]") -> None:
    """Visit each lane in order: name live ones, skip stamped ones, list or run the rest."""
    for index, (line, live) in enumerate(kinds, 1):
        label = f"[{index}/{len(kinds)}] {line}"
        if live:
            _say(ctx.opts, f"{label} … live at push")
            ctx.live += 1
            continue
        key = lane_key(line)
        if _already_stamped(ctx, label, key):
            continue
        if ctx.opts.dry_run:
            _say(ctx.opts, f"{label} … missing")
            ctx.missing.append(line)
            continue
        if not _stamp_lane(ctx, label, line, key):
            break


def run(opts: Options) -> int:
    """Run the oracle's stamped lanes for HEAD and stamp each on green; see the module docstring."""
    repo = opts.repo
    if opts.overridden:
        _say(
            opts,
            "refusing — came from the command line, so this run is not the declared gate: "
            + ", ".join(opts.overridden),
        )
        return 2
    roots = test_roots(repo)
    why = dirt_refusals(tree_state(repo), roots)
    if why:
        _say(opts, "refusing — " + "; ".join(why))
        return 2
    try:
        merge_base, lines = _tip_list(repo, opts.base)
        kinds = [(line, is_live(line)) for line in lines]
    except UnknownGoalError as exc:
        _say(opts, f"refusing — {exc}")
        return 2
    count = int(_git(repo, "rev-list", "--count", f"{merge_base}..HEAD").strip())
    _say(opts, f"base {opts.base} (merge-base {merge_base[:9]}), {count} commit(s) to land")
    ctx = _Run(
        opts=opts,
        merge_base=merge_base,
        root=stamps_root(repo, STORE),
        tree=tree_hash(repo),
        roots=roots,
        env=lane_env(dict(os.environ)),
    )
    _walk(ctx, kinds)
    _say(
        opts,
        f"{ctx.stamped} stamped, {ctx.already} already stamped, "
        f"{ctx.live} live at push, {ctx.red} red",
    )
    if opts.dry_run:
        return 1 if ctx.missing else 0
    return 1 if ctx.red or ctx.stopped else 0


@dataclass(frozen=True)
class Missing:
    """One stamped lane the pushed tree has no evidence for, and why."""

    lane: str
    reason: str


@dataclass(frozen=True)
class Verdict:
    """What the pre-push check decided: the missing lanes and the refusal text (empty = push)."""

    missing: "list[Missing]"
    lines: "list[str]"


def _ancestry_refusal(repo: Path, remote: str, remote_sha: str, tip_sha: str) -> "str | None":
    """Return the refusal line when remote_sha cannot be compared with tip_sha, else None."""
    if remote_sha == ZERO_SHA:
        return (
            f"gate-tip: refusing to push {tip_sha[:12]}: {remote} has no main to compare "
            "against; this check has no first-push mode (git push --no-verify)"
        )
    try:
        _git(repo, "cat-file", "-e", f"{remote_sha}^{{commit}}")
    except GateFreshError:
        return (
            f"gate-tip: {remote}'s main ({remote_sha[:12]}) is not in this clone: "
            f"git fetch {remote} main, then push again"
        )
    try:
        _git(repo, "merge-base", "--is-ancestor", remote_sha, tip_sha)
    except GateFreshError:
        return (
            f"gate-tip: not a fast-forward of {remote}'s main ({remote_sha[:12]}): "
            "fetch, rebase, re-gate"
        )
    return None


def missing_stamps(repo: Path, remote: str, remote_sha: str, tip_sha: str) -> Verdict:
    """Return the stamped lanes of the oracle's tip list over remote_sha..tip_sha the tree lacks."""
    short = tip_sha[:12]
    refusal = _ancestry_refusal(repo, remote, remote_sha, tip_sha)
    if refusal is not None:
        return Verdict([], [refusal])
    tree = tree_hash(repo, tip_sha)
    root = stamps_root(repo, STORE)
    try:
        lines = gate_scope.tip_commands(repo, remote_sha, tip_sha)
    except gate_scope.GateScopeError as exc:  # the oracle's own git errors derive from Exception
        raise GateFreshError(
            f"the oracle could not classify {remote_sha[:12]}..{short}: {exc}"
        ) from exc
    # lookup loads the store once, at the first lane without exact evidence; with no store
    # there is nothing to scan (and nothing a peer writes mid-check is promoted from)
    store: "Store | None" = None if root.is_dir() else Store([], [], [], [])
    missing: "list[Missing]" = []
    try:
        with store_lock(root, shared=True):  # one store state, from the first read to the verdict
            for line in lines:
                if is_live(line):
                    continue
                found = lookup(repo, root, tree, lane_key(line), store, base=remote_sha)
                store = found.store
                if found.stamp is None:
                    missing.append(Missing(line, found.reason))
    except UnknownGoalError as exc:
        raise GateFreshError(str(exc)) from exc
    if not missing:
        return Verdict([], [])
    out = [
        (
            f"gate-tip: refusing to push {short} to main — its gate is not stamped on tree "
            f"{tree[:12]} (range {remote_sha[:12]}..{short}):"
        )
    ]
    out += [f"  {m.lane}: {m.reason}" for m in missing]
    out.append(
        f"run: git fetch {remote} main && make gate-tip BASE=FETCH_HEAD   "
        f"(on commit {short}), then push again."
    )
    out.append(
        "git push --no-verify skips this check; CI then runs only its own lanes — "
        "never make coverage or the beds."
    )
    return Verdict(missing, out)


def main(argv: "list[str] | None" = None) -> int:
    """Parse the CLI and run, list, or check; return the script's status."""
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--repo", default=".")
    parser.add_argument("--base", default="origin/main")
    parser.add_argument("--force", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--overridden", action="append", default=[], metavar="NAME")
    parser.add_argument("--check", nargs=2, metavar=("REMOTE_SHA", "TIP_SHA"))
    parser.add_argument("--remote", default="origin")
    args = parser.parse_args(argv)
    repo = Path(args.repo).resolve()
    try:
        if args.check:
            verdict = missing_stamps(repo, args.remote, args.check[0], args.check[1])
            if verdict.lines:
                print("\n".join(verdict.lines), file=sys.stderr)
                return 1
            print(f"gate-tip: every stamped lane for {args.check[1][:12]} is stamped")
            return 0
        return run(
            Options(
                repo=repo,
                base=args.base,
                force=args.force,
                dry_run=args.dry_run,
                overridden=args.overridden,
            )
        )
    except (GateFreshError, gate_scope.GateScopeError) as exc:
        print(f"gate-tip: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
