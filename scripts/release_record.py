"""The release record: what one ``make release`` run has proven, and what a tag may rest on.

``reports/release/record.json`` is written by the release run and read by
``make release-tag``. It holds one of four states, each written by one step:

``pending``
    The run has started (``write_pending``). Written first, over whatever an
    earlier run left, so a certificate never outlives the run that earned it.
``certified``
    The run's tip passed its gates (``certify``): the commit, the tree and the
    version the gates certified. Needs the record ``pending`` by the same run.
``landed``
    The release pull request is open and its head is the certified one
    (``mark_landed``). Needs ``certified`` by the same run.
``tip-certified``
    A recertifying run found main already carrying the untagged bump and
    certified main's own tip (``tip_certified``). Needs ``certified`` by the
    same run.

``release-tag`` accepts ``certified``, ``landed`` and ``tip-certified``
(``TAGGABLE``) and refuses ``pending``, which is a run that never finished.

A run is identified by its pid AND the kernel's start time of that pid
(``RunIdentity``): a pid alone is reused, so a record naming a dead run's pid
could be taken for a live run's. The liveness question itself is
``process_group.alive``, the one predicate the bed lease shares.

Importing the module needs only the stdlib and ``tomli``; ``mode`` alone loads
``packaging`` (lazily), and runs under ``uv run``, never in CI.
"""

import argparse
import json
import sys
from dataclasses import asdict, dataclass
from pathlib import Path

import tomli

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))  # run by path, only scripts/ is on sys.path
from scripts import process_group  # noqa: E402
from scripts.gate_stamp_common import (  # noqa: E402
    GateError,
    _git,
    at_main_tip,
    tree_hash,
    utc_now,
    write_atomically,
)

RECORD = Path("reports/release/record.json")
PENDING, CERTIFIED, LANDED, TIP_CERTIFIED = "pending", "certified", "landed", "tip-certified"
TAGGABLE = [CERTIFIED, LANDED, TIP_CERTIFIED]


class ReleaseError(RuntimeError):
    """The release record cannot be written or the release cannot proceed."""


@dataclass(frozen=True)
class RunIdentity:
    """A process told apart from a later one that reuses its pid."""

    pid: int
    pid_started: int


@dataclass(frozen=True)
class Record:
    """One release record: the state, the run that wrote it and what it certified."""

    state: str
    run: RunIdentity
    head: "str | None"
    tree: "str | None"
    version: "str | None"
    url: "str | None"
    written_at: str


def identity(pid: int) -> RunIdentity:
    """Return *pid*'s identity; refused when its start time cannot be read."""
    started = process_group.process_start(pid)
    if started is None:
        raise ReleaseError(
            f"cannot read the start time of pid {pid}; the release needs a readable identity"
        )
    return RunIdentity(pid, started)


def read(repo: Path) -> "Record | None":
    """Return the record, or None when no run has written one."""
    path = repo / RECORD
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text())
        return Record(**{**data, "run": RunIdentity(**data["run"])})
    except (ValueError, TypeError, KeyError) as exc:
        raise ReleaseError(f"{path} is not a release record: {exc}") from exc


def _write(
    repo: Path,
    state: str,
    run: RunIdentity,
    *,
    head: "str | None" = None,
    tree: "str | None" = None,
    version: "str | None" = None,
    url: "str | None" = None,
) -> None:
    record = Record(state, run, head, tree, version, url, utc_now())
    write_atomically(repo / RECORD, json.dumps(asdict(record), indent=2) + "\n")


def _require(repo: Path, run: RunIdentity, state: str) -> Record:
    """Return the current record, which must be *state* and written by *run*."""
    current = read(repo)
    if current is None or current.state != state or current.run != run:
        found = "no record" if current is None else f"{current.state} by {current.run}"
        raise ReleaseError(f"the record must be {state} by {run}; it is {found}")
    return current


def write_pending(repo: Path, run: RunIdentity) -> None:
    """Start *run*'s record, discarding whatever an earlier run certified."""
    _write(repo, PENDING, run)


def certify(repo: Path, run: RunIdentity, *, head: str, tree: str, version: str) -> None:
    """Record what *run*'s gates certified; the record must be its own ``pending``."""
    _require(repo, run, PENDING)
    _write(repo, CERTIFIED, run, head=head, tree=tree, version=version)


def mark_landed(repo: Path, run: RunIdentity, *, url: str) -> None:
    """Record the release pull request of *run*'s certificate."""
    current = _require(repo, run, CERTIFIED)
    _write(
        repo,
        LANDED,
        run,
        head=current.head,
        tree=current.tree,
        version=current.version,
        url=url,
    )


def tip_certified(repo: Path, run: RunIdentity, *, head: str, tree: str, version: str) -> None:
    """Record that *run* certified main's own tip, the bump already on it."""
    _require(repo, run, CERTIFIED)
    _write(repo, TIP_CERTIFIED, run, head=head, tree=tree, version=version)


def current_version(repo: Path) -> str:
    """Return ``[tool.bumpversion].current_version`` from the repo's pyproject."""
    with (repo / "pyproject.toml").open("rb") as handle:
        return str(tomli.load(handle)["tool"]["bumpversion"]["current_version"])


def remote_has_tag(repo: Path, remote: str, tag: str) -> bool:
    """Whether *remote* carries ``refs/tags/<tag>``."""
    return bool(_git(repo, "ls-remote", "--tags", remote, f"refs/tags/{tag}").strip())


def mode(repo: Path, remote: str, census: str) -> str:
    """Return ``release`` (bump and land) or ``recertify`` (main holds the bump, untagged).

    A current version already tagged on the remote is released; the census then
    decides the next one. An untagged current version equal to the census is main
    carrying this release's bump already; a census below it (or unparsable) means
    there is nothing to release.
    """
    # Lazy: importing this module must need only the stdlib and tomli.
    from packaging.version import InvalidVersion, Version

    current = current_version(repo)
    if remote_has_tag(repo, remote, f"v{current}"):
        return "release"
    refusal = ReleaseError(
        f"the census names {census!r} but main already carries the untagged bump "
        f"to {current}; nothing to release"
    )
    try:
        wanted, have = Version(census), Version(current)
    except InvalidVersion as exc:
        raise refusal from exc
    if wanted == have:
        return "recertify"
    if wanted > have:
        return "release"
    raise refusal


def landing(repo: Path, remote: str) -> str:
    """Return how a certified run lands: ``tip-certified`` or ``pull-request``.

    ``tip-certified`` when HEAD is *remote*'s main (a recertification with nothing
    to merge), ``pull-request`` otherwise; a fetch that fails raises ``GateError``,
    never a guess.
    """
    return TIP_CERTIFIED if at_main_tip(repo, remote) else "pull-request"


def read_fields(record: "Record | None", fields: "list[str]") -> "list[str]":
    """Return *fields* (dotted, e.g. ``run.pid``) of *record*, ``""`` for a None value.

    Every field is ``""`` when there is no record; a field the record does not
    declare is refused.
    """
    known = asdict(Record(PENDING, RunIdentity(0, 0), None, None, None, None, ""))
    values = None if record is None else asdict(record)
    out: list[str] = []
    for field in fields:
        shape: object = known
        value: object = values
        for part in field.split("."):
            if not isinstance(shape, dict) or part not in shape:
                raise ReleaseError(f"the record has no field {field}")
            shape = shape[part]
            value = value.get(part) if isinstance(value, dict) else None
        out.append("" if value is None else str(value))
    return out


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="release_record", description=__doc__)
    parser.add_argument("--repo", type=Path, default=Path.cwd())
    sub = parser.add_subparsers(dest="command", required=True)
    for name in ("pending", "certify", "landed", "tip-certified"):
        verb = sub.add_parser(name)
        verb.add_argument("--pid", type=int, required=True)
        if name in ("certify", "tip-certified"):
            verb.add_argument("--version", required=True)
        if name == "landed":
            verb.add_argument("--url", required=True)
    verb = sub.add_parser("mode")
    verb.add_argument("--remote", required=True)
    verb.add_argument("--census", required=True)
    verb = sub.add_parser("landing")
    verb.add_argument("--remote", required=True)
    verb = sub.add_parser("read")
    verb.add_argument(
        "--fields",
        help="comma-separated (dotted) fields, one value per line, empty for none",
    )
    return parser


def _run(args: argparse.Namespace) -> int:
    repo: Path = args.repo
    command: str = args.command
    if command == "read":
        record = read(repo)
        if args.fields is None:
            print(json.dumps(None if record is None else asdict(record), indent=2))
        else:
            print("\n".join(read_fields(record, args.fields.split(","))))
    elif command == "mode":
        print(mode(repo, args.remote, args.census))
    elif command == "landing":
        print(landing(repo, args.remote))
    else:
        run = identity(args.pid)
        if command == "pending":
            write_pending(repo, run)
        elif command == "landed":
            mark_landed(repo, run, url=args.url)
        else:
            head, tree = _git(repo, "rev-parse", "HEAD").strip(), tree_hash(repo)
            write = certify if command == "certify" else tip_certified
            write(repo, run, head=head, tree=tree, version=args.version)
    return 0


def main(argv: "list[str] | None" = None) -> int:
    """Run one verb; a refusal prints ``release-record: <reason>`` and exits 1."""
    args = _parser().parse_args(argv)
    try:
        return _run(args)
    except (ReleaseError, GateError) as exc:
        print(f"release-record: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
