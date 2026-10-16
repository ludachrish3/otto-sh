#!/usr/bin/env python3
"""The coverage stamp: proof that ``make coverage`` passed on a committed tree.

`make release` used to run `nox -s tests_all-3.10`: the selection `make
coverage` had already gated on the same tree (minus the browser lane, under
a lower floor), for a quarter of an hour a release. The stamp lets the release
skip that session when the evidence exists, and run `make coverage` itself
when it does not (owner ruling Q5, #566) — a release never trusts a stamp it
cannot verify.

What a stamp attests, and what `check` verifies: every leg of `make coverage`
passed, on the tree `git rev-parse HEAD^{tree}` names, under the PRIMARY
interpreter (the Makefile's `NOX_PRIMARY`, passed as `--primary major.minor`;
a venv rebuilt on another CPython proves nothing about the primary and is
refused at `start` and at `check` alike), at the gate the Makefile declared
(threshold, the `-m` selection, the dashboard browsers), against the built
web assets the Python legs import. Each is a field of the stamp; `check`
compares every one to the tree and environment it runs in, and names each
mismatch — the `tree` field included, so a stamp file that holds another
tree's record is not evidence for the tree its name claims.

It is a COMMITTED-tree contract, the same one `scripts/gate_fresh.py` makes:
a tree hash names what is committed, so a run over a modified or staged
tracked file, or over an untracked file under `src/` or a test root (the
live checkout imports it; the committed tree has no such file), proves
nothing about any commit and is not stamped. The legs still run — only the
stamp is withheld, and `start` says why. An untracked file anywhere else
(`tests/_fixtures/`, `scripts/`, a note at the root) is listed in the
output but does not block: the legs do not collect it as a test, yet they
may well import it, so add it before you gate. The same holds for a run
with ANY variable on the make command line (the release's own `BUMP=`,
`NEW_VERSION=` and `BUDGET_APPROVE=` excepted): the Makefile passes
`--overridden NAME` for each name whose `$(origin)` is `command line`, read
from `.VARIABLES`, to `start` and to `check` alike, because the
stamp describes the declared gate, not a custom one. Ambient
`PYTEST_ADDOPTS` is cleared by the Makefile for the stamped legs, so an
environment override cannot narrow the selection the stamp names.

Where it lives: `<git common dir>/otto-coverage-stamps/<tree-hash>.json`,
shared by every worktree of the clone (`reports/` is per checkout and
gitignored), so a tree gated in a worktree releases from the main checkout.

Lifecycle, driven by the Makefile (`make coverage`, `make release`):

  start   — the first thing `make coverage` runs, after the web dist rules
            (so the assets it digests are the ones the legs import): deletes
            any stamp for the tree (make aborts at the first failing leg, so
            "a failed leg deletes the stamp" is only implementable as
            delete-at-start), then records the tree, the dirt, the
            interpreter, the assets digest and any refusal in
            `reports/coverage-stamp.pending.json`.
  finish  — the last thing `make coverage` runs, so it runs only when every
            leg passed: re-reads the tree, the dirt, the interpreter and the
            assets, requires all of them to equal the start's, and writes the
            stamp atomically (temp + rename). A refusal at the start, or
            anything that changed under the run, means no stamp.
  check   — the first thing `make release` runs, and its exit status is the
            release's instruction. 0: a stamp for HEAD's tree exists and
            every field matches — trust it. 1: no stamp, or one that does
            not match (tree, assets, gate, an unreadable file) — the
            release runs `make coverage` and checks again. 2: a refusal no
            stamp can cure (a dirty tree, a foreign interpreter, a
            command-line variable) — the release stops at once instead of
            running a forty-minute `make coverage` that could not be
            stamped. Every problem is printed, whichever status wins.
"""

import argparse
import hashlib
import json
import os
import platform
import subprocess
import sys
import tempfile
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import tomli

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))
from scripts.gate_fresh import (  # noqa: E402 -- path set up above
    GateFreshError,
    TreeState,
    tree_state,
)

STAMPS_DIR = "otto-coverage-stamps"
"""Under the clone's common git dir: one file per stamped tree, shared by its worktrees."""

PENDING = Path("reports") / "coverage-stamp.pending.json"
"""Per checkout (``reports/`` is gitignored): what ``start`` saw, for ``finish`` to compare."""

ASSET_DIRS = ("src/otto/_webassets/monitor/dist", "src/otto/_webassets/covapp")
"""The built web assets the Python legs import — gitignored, so outside the tree hash."""

FORMAT = 1


class StampError(RuntimeError):
    """A git command the stamp needs failed — never a verdict."""


def _git(repo: Path, *args: str) -> str:
    try:
        result = subprocess.run(  # noqa: S603
            ["git", *args],  # noqa: S607
            cwd=repo,
            capture_output=True,
            text=True,
            check=False,
        )
    except OSError as exc:  # FileNotFoundError included: no git on PATH
        raise StampError(f"git is not available: {exc}") from exc
    if result.returncode != 0:
        raise StampError(f"git {' '.join(args)} failed in {repo}: {result.stderr.strip()}")
    return result.stdout


def tree_hash(repo: Path) -> str:
    """``HEAD^{tree}``: the committed content, whatever the commit or branch."""
    return _git(repo, "rev-parse", "HEAD^{tree}").strip()


def stamps_dir(repo: Path) -> Path:
    """Return the clone's shared stamp directory (under the git common dir)."""
    common = _git(repo, "rev-parse", "--path-format=absolute", "--git-common-dir").strip()
    return Path(common) / STAMPS_DIR


def stamp_path(repo: Path, tree: str) -> Path:
    """Where *tree*'s stamp lives."""
    return stamps_dir(repo) / f"{tree}.json"


def test_roots(repo: Path) -> "list[str]":
    """Pyproject's ``testpaths``: the roots the live checkout collects from."""
    data = tomli.loads((repo / "pyproject.toml").read_text())
    return list(data["tool"]["pytest"]["ini_options"]["testpaths"])


def blocking_untracked(untracked: "list[str]", roots: "list[str]") -> "list[str]":
    """Select the untracked paths the live checkout would import: under ``src/`` or a test root."""
    prefixes = tuple(f"{root.rstrip('/')}/" for root in ("src", *roots))
    return [path for path in untracked if path.startswith(prefixes)]


def assets_digest(repo: Path) -> str:
    """Hash every built asset (relative path and bytes, sorted); an absent dir is its own value."""
    digest = hashlib.sha256()
    for rel in ASSET_DIRS:
        root = repo / rel
        if not root.is_dir():
            digest.update(f"{rel}: absent\0".encode())
            continue
        for path in sorted(p for p in root.rglob("*") if p.is_file()):
            digest.update(f"{path.relative_to(repo)}\0".encode())
            digest.update(path.read_bytes())
            digest.update(b"\0")
    return digest.hexdigest()


@dataclass(frozen=True)
class Gate:
    """What the Makefile declares for the run — the fields a stamp must match at ``check``.

    ``primary`` is the interpreter the gate is defined on (``major.minor``);
    the running interpreter must be it, at ``start`` and at ``check``.
    ``workers`` is recorded for the reader and never compared: it changes how
    long the run takes, not what it gates.
    """

    threshold: str
    selection: str
    browsers: str
    primary: str
    workers: str = ""


def major_minor(version: str) -> str:
    """``3.12.4`` -> ``3.12``."""
    return ".".join(version.split(".")[:2])


def python_refusal(version: str, primary: str) -> "list[str]":
    """Why *version* cannot stand for the primary interpreter; empty when it is the primary."""
    if major_minor(version) == primary:
        return []
    return [
        f"python {version} is not the primary {primary}: this venv's gate is not the declared one"
    ]


def refusals(state: TreeState, roots: "list[str]", overridden: "list[str]") -> "list[str]":
    """Why this run cannot be stamped; empty when it can."""
    why: "list[str]" = []
    if state.tracked_dirty:
        why.append(
            "tracked files are modified or staged (the committed tree does not hold them): "
            + ", ".join(state.tracked_dirty)
        )
    blocking = blocking_untracked(state.untracked, roots)
    if blocking:
        why.append(
            "untracked files under src/ or a test root (the live checkout imports them; "
            "the committed tree has no such file): " + ", ".join(blocking)
        )
    if overridden:
        why.append(
            "came from the command line, so the run is not the declared gate: "
            + ", ".join(overridden)
        )
    return why


def _say(text: str) -> None:
    print(f"coverage-stamp: {text}", flush=True)


def start(repo: Path, gate: Gate, overridden: "list[str]") -> int:
    """Drop the tree's old stamp and record what this run sees; always exit 0."""
    tree = tree_hash(repo)
    previous = stamp_path(repo, tree)
    if previous.exists():
        previous.unlink()
        _say(f"removed the previous stamp for tree {tree[:12]}; this run decides anew")
    state = tree_state(repo)
    python = platform.python_version()
    roots = test_roots(repo)
    why = refusals(state, roots, overridden) + python_refusal(python, gate.primary)
    blocking = blocking_untracked(state.untracked, roots)
    listed = [p for p in state.untracked if p not in blocking]
    pending = {
        "format": FORMAT,
        "tree": tree,
        "tracked_dirty": state.tracked_dirty,
        "untracked": state.untracked,
        "python": python,
        "assets": assets_digest(repo),
        "refusals": why,
        "gate": asdict(gate),
    }
    (repo / PENDING).parent.mkdir(parents=True, exist_ok=True)
    (repo / PENDING).write_text(json.dumps(pending, indent=2, sort_keys=True) + "\n")
    if why:
        _say(f"tree {tree[:12]} will NOT be stamped (the legs still run):")
        for reason in why:
            print(f"  {reason}", flush=True)
    else:
        _say(f"tree {tree[:12]}: stamping when every leg passes")
    if listed:
        _say(
            "untracked files not in the stamped tree (outside src/ and the test roots): "
            + ", ".join(listed)
        )
    return 0


def _write_atomically(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as handle:
            handle.write(text)
        Path(tmp).replace(path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def finish(repo: Path) -> int:
    """Stamp the tree when nothing was refused and nothing changed; always exit 0."""
    pending_path = repo / PENDING
    if not pending_path.exists():
        _say(
            "no pending record: this run did not start through `coverage_stamp.py start`; "
            "nothing stamped"
        )
        return 0
    pending = json.loads(pending_path.read_text())
    pending_path.unlink()
    if pending["refusals"]:
        _say(f"tree {pending['tree'][:12]} not stamped:")
        for reason in pending["refusals"]:
            print(f"  {reason}", flush=True)
        return 0
    tree = tree_hash(repo)
    state = tree_state(repo)
    if (tree, state.tracked_dirty, state.untracked) != (
        pending["tree"],
        pending["tracked_dirty"],
        pending["untracked"],
    ):
        _say(
            f"the tree changed during the run (start {pending['tree'][:12]}, now {tree[:12]}, "
            f"dirt {state.tracked_dirty + state.untracked}); not stamped"
        )
        return 0
    python = platform.python_version()
    if python != pending["python"]:
        _say(
            f"the python changed during the run (start {pending['python']}, now {python}); "
            "not stamped"
        )
        return 0
    assets = assets_digest(repo)
    if assets != pending["assets"]:
        _say("the built web assets changed during the run; not stamped")
        return 0
    stamp = {
        "format": FORMAT,
        "tree": tree,
        "python": python,
        "assets": assets,
        "gate": pending["gate"],
        "written_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "checkout": str(repo),
    }
    path = stamp_path(repo, tree)
    _write_atomically(path, json.dumps(stamp, indent=2, sort_keys=True) + "\n")
    _say(f"stamped tree {tree[:12]} ({path})")
    return 0


def check(repo: Path, gate: Gate, overridden: "list[str]") -> int:
    """Exit 0 only when HEAD's tree carries a stamp matching every field; else say why.

    The status tells the release what to do: 0 trust the stamp; 1 there is
    none for HEAD's tree, or it does not match — run ``make coverage`` and
    check again; 2 a refusal no stamp can cure (dirt, a foreign interpreter,
    a command-line variable in *overridden*) — stop now. 2 wins over 1, and
    every problem is printed either way.
    """
    tree = tree_hash(repo)
    path = stamp_path(repo, tree)
    problems: "list[str]" = []
    stamp: "dict[str, Any] | None" = None  # None = nothing to compare; {} is a (bad) stamp
    if not path.exists():
        problems.append(
            f"no stamp for tree {tree[:12]}: `make coverage` has not passed on this committed tree"
        )
    else:
        try:
            loaded = json.loads(path.read_text())
        except (OSError, ValueError) as exc:
            problems.append(f"stamp unreadable: {exc}")
        else:
            if isinstance(loaded, dict):
                stamp = loaded
            else:
                problems.append(
                    f"stamp unreadable: expected a JSON object, found {type(loaded).__name__}"
                )
    if stamp is not None:
        if stamp.get("format") != FORMAT:
            problems.append(f"stamp format {stamp.get('format')!r} is not {FORMAT}")
        if stamp.get("tree") != tree:
            problems.append(f"tree: stamped {str(stamp.get('tree'))[:12]}, HEAD is {tree[:12]}")
        if stamp.get("python") != platform.python_version():
            problems.append(
                f"python: stamped {stamp.get('python')}, running {platform.python_version()}"
            )
        if stamp.get("assets") != assets_digest(repo):
            problems.append("assets: the built web assets differ from the stamped ones")
        recorded = stamp.get("gate", {})
        if not isinstance(recorded, dict):
            problems.append("stamp unreadable: gate is not an object")
        else:
            for name in ("threshold", "selection", "browsers"):
                if recorded.get(name) != getattr(gate, name):
                    problems.append(
                        f"{name}: stamped {recorded.get(name)!r}, "
                        f"declared now {getattr(gate, name)!r}"
                    )
    state = tree_state(repo)
    incurable = refusals(state, test_roots(repo), overridden) + python_refusal(
        platform.python_version(), gate.primary
    )
    if incurable:
        _say(f"tree {tree[:12]} cannot be released from here; `make coverage` would not stamp it:")
        for problem in [*problems, *incurable]:
            print(f"  {problem}", flush=True)
        return 2
    if problems:
        _say(f"no valid stamp for tree {tree[:12]}:")
        for problem in problems:
            print(f"  {problem}", flush=True)
        return 1
    found = stamp or {}  # no problems ⇒ a stamp was read; `or {}` only narrows the type
    _say(
        f"tree {tree[:12]} passed `make coverage` at {found.get('written_at', 'unknown')} "
        f"(python {found.get('python', 'unknown')}); the release skips its primary nox session"
    )
    return 0


def _gate_options(parser: argparse.ArgumentParser, *, workers: bool) -> None:
    parser.add_argument("--threshold", required=True, help="the Makefile's COVERAGE_THRESHOLD")
    parser.add_argument(
        "--selection", required=True, help="the `-m` selection every coverage-python leg shares"
    )
    parser.add_argument("--browsers", required=True, help="the Makefile's DASHBOARD_BROWSERS")
    parser.add_argument("--primary", required=True, help="the Makefile's NOX_PRIMARY (major.minor)")
    if workers:
        parser.add_argument(
            "--workers", required=True, help="the Makefile's COVERAGE_WORKERS (recorded only)"
        )


def _overridden_option(parser: argparse.ArgumentParser, consequence: str) -> None:
    parser.add_argument(
        "--overridden",
        action="append",
        default=[],
        metavar="NAME",
        help=f"a variable that came from the make command line ({consequence})",
    )


def main(argv: "list[str] | None" = None) -> int:
    """Run one verb (``start``, ``finish``, ``check``) and return its exit status."""
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--repo", default=".", help="the checkout (default: the current directory)")
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    verbs = parser.add_subparsers(dest="verb", required=True)
    start_parser = verbs.add_parser(
        "start", parents=[common], help="begin a stamped `make coverage` run"
    )
    _gate_options(start_parser, workers=True)
    _overridden_option(start_parser, "the run is then not stamped")
    verbs.add_parser(
        "finish", parents=[common], help="stamp the tree, if every leg passed and nothing changed"
    )
    check_parser = verbs.add_parser(
        "check",
        parents=[common],
        help="exit 0 if HEAD's tree carries a valid stamp, 1 to run `make coverage`, 2 to stop",
    )
    _gate_options(check_parser, workers=False)
    _overridden_option(check_parser, "the release is then refused, status 2")
    args = parser.parse_args(argv)
    repo = Path(args.repo).resolve()
    try:
        if args.verb == "start":
            gate = Gate(args.threshold, args.selection, args.browsers, args.primary, args.workers)
            return start(repo, gate, args.overridden)
        if args.verb == "finish":
            return finish(repo)
        gate = Gate(args.threshold, args.selection, args.browsers, args.primary)
        return check(repo, gate, args.overridden)
    except (StampError, GateFreshError) as exc:
        print(f"coverage-stamp: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
