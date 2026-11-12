"""Tag ``main``'s tip once the release pull request merged: ``make release-tag``.

``make release`` certifies a release in ``reports/release/record.json`` and
lands it through a pull request (``scripts/release_record.py``). After the
maintainer rebase-merges that pull request, this script tags ``main``'s tip,
and the tag push fires ``.github/workflows/release.yml``. It refuses unless,
in this order:

1. the record is one ``release-tag`` accepts (``release_record.TAGGABLE``),
   judged before anything touches the remote: a pending record is a run that
   never finished;
2. ``main``'s tip tree is the record's tree (a rebase-merge changes the
   commits, never the tree, so another tree means something else landed) and
   its ``pyproject.toml`` carries the record's version;
3. ``main``'s CI on that tip is green, read through
   ``local_gate_status.await_verdict``, the one waiter on the verdict the
   release workflow's guard reads too, up to a ceiling while the run is pending;
4. ``main`` did not move during the wait: CI certifies a commit, not a tree,
   so a new tip with the same tree is still an unchecked commit.

The tag's name and message are ``[tool.bumpversion]``'s ``tag_name`` and
``tag_message`` (:func:`tag_for`), read from the same ``pyproject.toml`` at
main's tip that carries the version, so the format has one home. A local tag of
that name, left by a run whose push failed, is pushed again when it names main's
tip with that message, and refused otherwise. Importing the module needs only
the stdlib and ``tomli``.

Usage, as the Makefile's ``release-tag`` recipe runs it::

    uv run --locked python scripts/release_tag.py --remote origin
"""

import argparse
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

import tomli

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))  # run by path, only scripts/ is on sys.path
from scripts import release_record  # noqa: E402
from scripts.gate_stamp_common import (  # noqa: E402
    GateError,
    _git,
    fetch_main,
    remote_slug,
    tree_hash,
)
from scripts.local_gate_status import (  # noqa: E402
    REQUIRED_CONTEXTS_FILE,
    GhRunner,
    RequiredContextsError,
    await_verdict,
    ci_refusal,
    required_contexts_under,
    run_gh,
)

WAIT_MINUTES = 70
"""The CI wait's ceiling: the longest required job's own timeout is 60 minutes."""

INTERVAL_S = 10
"""Seconds between two reads of the CI verdict."""


def _bumpversion_at(repo: Path, sha: str) -> dict:
    """Return the ``[tool.bumpversion]`` table of ``pyproject.toml`` at *sha*."""
    try:
        return tomli.loads(_git(repo, "show", f"{sha}:pyproject.toml"))["tool"]["bumpversion"]
    except (tomli.TOMLDecodeError, KeyError, TypeError) as exc:
        raise release_record.ReleaseError(
            f"pyproject.toml at {sha[:12]} has no readable [tool.bumpversion] table ({exc!r})"
        ) from exc


def tag_for(config: dict, version: str) -> "list[str]":
    """Return ``[tag_name, tag_message]`` of *version*, from a ``[tool.bumpversion]`` table."""
    try:
        formats = [config["tag_name"], config["tag_message"]]
    except KeyError as exc:
        raise release_record.ReleaseError(
            f"[tool.bumpversion] declares no {exc.args[0]}; the tag's format lives there"
        ) from exc
    try:
        return [text.format(new_version=version) for text in formats]
    except KeyError as exc:
        raise release_record.ReleaseError(
            "[tool.bumpversion] tag_name/tag_message uses an unknown placeholder "
            f"{{{exc.args[0]}}}; only {{new_version}} is substituted"
        ) from exc


@dataclass(frozen=True)
class _LocalTag:
    """A tag already in the checkout: the commit it names and its message."""

    commit: str
    message: str


def _local_tag(repo: Path, name: str) -> "_LocalTag | None":
    """Return the local tag *name*, or None: a tag a failed push left behind."""
    ref = f"refs/tags/{name}"
    if not _git(repo, "for-each-ref", "--format=%(refname)", ref).strip():
        return None
    return _LocalTag(
        _git(repo, "rev-parse", f"{ref}^{{commit}}").strip(),
        _git(repo, "for-each-ref", "--format=%(contents)", ref).strip(),
    )


def _stale_tag(existing: "_LocalTag | None", name: str, message: str, main_sha: str) -> str:
    """Why *existing* cannot be reused as the release's tag, or ``""`` when it can (or is None)."""
    if existing is None:
        return ""
    remedy = f"delete it (git tag -d {name}) and run again"
    if existing.commit != main_sha:
        return (
            f"the local tag {name} points at {existing.commit[:12]}, not main's tip "
            f"{main_sha[:12]}; {remedy}"
        )
    if existing.message != message.strip():
        return (
            f"the local tag {name} carries the message {existing.message!r}, "
            f"not {message!r}; {remedy}"
        )
    return ""


def _refuse(message: str) -> int:
    print(f"release-tag: {message}", file=sys.stderr)
    return 1


def release_tag(
    repo: Path,
    *,
    remote: str,
    required: "list[str]",
    wait_s: int,
    interval: int,
    gh: GhRunner = run_gh,
    sleep: "Callable[[float], None]" = time.sleep,
    clock: "Callable[[], float]" = time.monotonic,
) -> int:
    """Tag *remote*'s main tip for the certified release; the exit code."""
    record = release_record.read(repo)
    if record is None:
        return _refuse("no release record; run make release first")
    if record.state not in release_record.TAGGABLE:
        return _refuse(
            f"the record is {record.state}; the last make release did not finish, run it again"
        )
    slug = remote_slug(repo, remote)
    main_sha = fetch_main(repo, remote)
    tree = tree_hash(repo, main_sha)
    if tree != record.tree:
        return _refuse(
            f"main's tip tree {tree[:12]} is not the certified tree {(record.tree or '')[:12]}: "
            "something else landed; run the recertification (docs/release_process.md)"
        )
    try:
        config = _bumpversion_at(repo, main_sha)
        version = str(config.get("current_version"))
        name, message = tag_for(config, version)
    except release_record.ReleaseError as exc:
        return _refuse(str(exc))
    if version != record.version:
        return _refuse(
            f"main's tip carries version {version}, the record certified {record.version}"
        )
    existing = _local_tag(repo, name)
    stale = _stale_tag(existing, name, message, main_sha)
    if stale:
        return _refuse(stale)
    verdict = await_verdict(
        slug, main_sha, required, wait_s=wait_s, interval=interval, gh=gh, sleep=sleep, clock=clock
    )
    unsettled = ci_refusal(verdict, main_sha, wait_s)
    if unsettled:
        return _refuse(unsettled)
    again = fetch_main(repo, remote)
    if again != main_sha:
        return _refuse(
            f"main moved during the wait ({main_sha[:12]} -> {again[:12]}); the new tip's CI "
            "is unchecked, run make release-tag again once it is green"
        )
    if existing is None:
        _git(repo, "tag", "-a", name, "-m", message, main_sha)
    else:
        print(f"release-tag: reusing the local tag {name} on {main_sha[:12]}")
    _git(repo, "push", remote, f"refs/tags/{name}")
    print(f"release-tag: pushed {name} on {main_sha[:12]}; release.yml is running")
    return 0


def main(argv: "list[str] | None" = None) -> int:
    """CLI entry point; a refusal prints ``release-tag: <reason>`` and exits 1."""
    parser = argparse.ArgumentParser(prog="release_tag", description=__doc__.splitlines()[0])
    parser.add_argument("--repo", type=Path, default=REPO_ROOT, help="the checkout to tag from")
    parser.add_argument("--remote", default="origin", help="the remote whose main is tagged")
    parser.add_argument("--required", type=Path, default=Path(REQUIRED_CONTEXTS_FILE))
    parser.add_argument("--wait-minutes", type=int, default=WAIT_MINUTES)
    parser.add_argument("--interval", type=int, default=INTERVAL_S)
    args = parser.parse_args(argv)
    repo: Path = args.repo.resolve()
    try:
        required = required_contexts_under(repo, args.required)
        return release_tag(
            repo,
            remote=args.remote,
            required=required,
            wait_s=args.wait_minutes * 60,
            interval=args.interval,
        )
    except (release_record.ReleaseError, GateError, RequiredContextsError) as exc:
        return _refuse(str(exc))


if __name__ == "__main__":
    sys.exit(main())
