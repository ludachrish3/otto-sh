"""The ``local-gate`` commit status: what the local gate posts and CI's first job checks.

A green local gate on a landing's tip is recorded as a GitHub commit status
named ``local-gate`` on the tip commit. Its description is ``v1 tree=<tree>
base=<base> lanes=<digest>``: the committed tree that was gated, the ``main``
the diff was taken against, and a digest of the lane keys the gate-scope
oracle named for that diff. The status is an attestation, not a proof: it says
the author's machine ran the tip lanes on that tree, and CI's ``gate-admission``
job recomputes the same description from the pushed head and refuses a mismatch.

This file is the only writer and the only reader of that status, and of the
CI verdict the release consults: ``post`` and ``fail`` write it, ``await`` and
``admit`` verify it, ``ci-verdict`` reads the run it gates. It is stdlib only
because CI runs it with ``uv run --no-project --with tomli python``; the
``await`` command imports ``scripts.gate_scope`` lazily for the same reason.
"""

import argparse
import hashlib
import json
import re
import subprocess
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

CONTEXT = "local-gate"
"""The commit-status context the local gate posts."""

VERSION = "v1"
DIGEST_LEN = 12
DESCRIPTION_LEN = 113
"""Length of a formatted description (GitHub caps a status description at 140)."""

ADMISSION_JOB = "gate-admission"
WORKFLOW = "ci.yml"
EXCLUDED_FROM_VERDICT = [CONTEXT, ADMISSION_JOB]
"""Required contexts the CI verdict does not read from the run: the status and its verifier."""

CLI_SLEEP: "Callable[[float], None]" = time.sleep
"""The sleep the CLI commands wait with. Tests replace this name, never ``time.sleep``
itself: ``subprocess`` polls a child with ``time.sleep`` too (Python 3.10's ``Popen.wait``),
so a patched global records the fake ``gh``'s own waits beside the command's."""

REPO_ROOT = Path(__file__).resolve().parent.parent

REQUIRED_CONTEXTS_FILE = ".github/required_contexts.json"

_DESCRIPTION_RE = re.compile(
    rf"^{VERSION} tree=([0-9a-f]{{40}}) base=([0-9a-f]{{40}}) lanes=([0-9a-f]{{{DIGEST_LEN}}})$"
)
_UNPARSABLE = "gh returned unparsable JSON"
_RUN_FIELDS = "databaseId,status,conclusion,url,createdAt"
_REMEDY = "run make gate-tip on this head, then make gate-status"


@dataclass(frozen=True)
class Description:
    """What a ``local-gate`` status says it gated."""

    tree: str
    base: str
    lane_digest: str


def lane_digest(keys: "list[str]") -> str:
    """Return the short digest of the lane keys a gate ran (or a plan names)."""
    return hashlib.sha256("\n".join(keys).encode()).hexdigest()[:DIGEST_LEN]


def format_description(d: Description) -> str:
    """Return the status description text for *d*."""
    return f"{VERSION} tree={d.tree} base={d.base} lanes={d.lane_digest}"


def parse_description(text: str) -> "Description | None":
    """*text* as a :class:`Description`, or ``None`` when it is not a ``v1`` description."""
    match = _DESCRIPTION_RE.match(text)
    return Description(*match.groups()) if match else None


def expected(plan) -> Description:  # noqa: ANN001 -- a gate_scope plan; this module imports none
    """Return the description a green gate of *plan* posts and an admission recomputes."""
    return Description(plan.tree, plan.base_sha, lane_digest(plan.stamped))


@dataclass(frozen=True)
class GhResult:
    """One ``gh`` invocation's outcome."""

    status: int
    stdout: str
    stderr: str


GhRunner = Callable[["list[str]"], GhResult]
"""Runs ``gh <args>``; the tests substitute a fake."""


GH_TIMEOUT = 30.0
"""Seconds one ``gh`` call may take: a post under the store lock holds it this long at most."""


def run_gh(args: "list[str]", *, timeout: float = GH_TIMEOUT) -> GhResult:
    """Run ``gh`` with *args*, bounded by *timeout* seconds."""
    try:
        done = subprocess.run(  # noqa: S603
            ["gh", *args],  # noqa: S607
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except FileNotFoundError:
        return GhResult(127, "", "gh: not found on PATH")
    except subprocess.TimeoutExpired:
        return GhResult(124, "", f"gh timed out after {timeout:g}s")
    return GhResult(done.returncode, done.stdout, done.stderr)


def poll(
    predicate: "Callable[[], bool]",
    *,
    timeout_s: float,
    interval: float,
    sleep: "Callable[[float], None]" = time.sleep,
    clock: "Callable[[], float]" = time.monotonic,
) -> bool:
    """Probe *predicate* until it holds or *timeout_s* seconds have passed; True once it held.

    The deadline is read from *clock*, so a slow probe (a ``gh`` call can take
    seconds) spends the budget too; *interval* seconds are slept between two
    probes. A *timeout_s* of 0 probes once and never sleeps.

    This is not ``otto.utils.wait_for``: CI's admission job runs this module
    with ``uv run --no-project``, where ``otto`` is not importable.
    """
    deadline = clock() + timeout_s
    while True:
        if predicate():
            return True
        if clock() >= deadline:
            return False
        sleep(interval)


@dataclass(frozen=True)
class PostOutcome:
    """Whether a status was posted, and the remedy in words when it was not."""

    ok: bool
    message: str
    no_commit: bool


def post_status(
    slug: str, sha: str, desc: Description, *, state: str, gh: GhRunner = run_gh
) -> PostOutcome:
    """Post the ``local-gate`` status for *sha* in *state* (``success`` or ``failure``)."""
    result = gh(
        [
            "api",
            "-X",
            "POST",
            f"repos/{slug}/statuses/{sha}",
            "-f",
            f"state={state}",
            "-f",
            f"context={CONTEXT}",
            "-f",
            f"description={format_description(desc)}",
        ]
    )
    if result.status == 0:
        return PostOutcome(True, "", False)
    if "No commit found" in result.stderr + result.stdout:
        return PostOutcome(False, "push the branch, then make gate-status", True)
    return PostOutcome(False, result.stderr.strip(), False)


def head_status(slug: str, sha: str, *, gh: GhRunner = run_gh) -> "Description | None":
    """Return the description of *sha*'s successful ``local-gate`` status, or ``None``.

    ``None`` also covers a sha GitHub does not know yet (404 or 422: the endpoint can lag a
    push). Any other ``gh`` failure (permissions, auth, network, no ``gh``) raises ``GateError``
    carrying ``gh``'s words, so it never reads as "nobody gated".
    """
    result = gh(["api", f"repos/{slug}/commits/{sha}/status"])
    if result.status != 0:
        if "HTTP 404" in result.stderr or "HTTP 422" in result.stderr:
            return None
        from scripts.gate_stamp_common import GateError  # lazy: stdlib-only at import time

        raise GateError(result.stderr.strip() or f"gh exited {result.status}")
    try:
        statuses = json.loads(result.stdout).get("statuses", [])
    except (ValueError, AttributeError):
        return None
    for status in statuses:
        if status.get("context") == CONTEXT and status.get("state") == "success":
            return parse_description(status.get("description", ""))
    return None


def _mismatches(posted: Description, want: Description) -> "list[str]":
    fields = [("tree", "tree"), ("base", "base"), ("lane digest", "lane_digest")]
    return [name for name, attr in fields if getattr(posted, attr) != getattr(want, attr)]


def await_status(
    slug: str,
    sha: str,
    want: Description,
    *,
    gh: GhRunner = run_gh,
    timeout_s: float = 300.0,
    interval: float = 10.0,
    sleep: "Callable[[float], None]" = time.sleep,
    clock: "Callable[[], float]" = time.monotonic,
) -> "list[str]":
    """``[]`` when *sha* carries a ``local-gate`` success matching *want*, else the remedy lines."""
    found: list[Description] = []

    def probe() -> bool:
        posted = head_status(slug, sha, gh=gh)
        if posted is not None:
            found.append(posted)
        return posted is not None

    poll(probe, timeout_s=timeout_s, interval=interval, sleep=sleep, clock=clock)
    if not found:
        return [
            f"gate-admission: no local-gate success on {sha[:12]} after {timeout_s:g}s",
            _REMEDY,
        ]
    differing = _mismatches(found[-1], want)
    if not differing:
        return []
    head = f"gate-admission: local-gate on {sha[:12]} does not match this head: "
    return [head + ", ".join(differing), _REMEDY]


@dataclass(frozen=True)
class Verdict:
    """What CI's push run says about the required jobs."""

    green: bool
    pending: bool
    run_url: str
    detail: "list[str]"
    unread: bool = False
    """No run was read: none exists yet, or ``gh`` could not list or view it (``detail`` says)."""


def required_contexts(path: Path) -> "list[str]":
    """Return the required check names, from the committed JSON list at *path*."""
    return json.loads(path.read_text())


@dataclass(frozen=True)
class _Fetched:
    """A ``gh`` JSON answer: *data* when it parsed, else *error* in one line."""

    data: "dict | None"
    error: str


def _gh_json(result: GhResult) -> "_Fetched":
    """Parse a gh result's stdout as JSON, or name why it cannot be."""
    if result.status != 0:  # a failure is never an empty error, which reads as "no run"
        return _Fetched(None, result.stderr.strip() or f"gh exited {result.status}")
    try:
        return _Fetched({"value": json.loads(result.stdout or "[]")}, "")
    except ValueError:
        return _Fetched(None, _UNPARSABLE)


def _newest_run(
    slug: str,
    sha: str,
    event: str,
    fields: str,
    *,
    gh: GhRunner,
    timeout_s: float,
    interval: float,
    sleep: "Callable[[float], None]",
    clock: "Callable[[], float]",
) -> "_Fetched":
    """Return the newest ``ci.yml`` run of *event* on *sha* (as ``data``), polling until one exists.

    ``data`` is ``None`` when no run appeared. ``error`` is then empty when the
    listing answered with no run, and names the failure when the last lookup
    failed (``gh``'s complaint, or ``_UNPARSABLE`` for an answer that is not a list).
    """
    found = _Fetched(None, "")

    def probe() -> bool:
        nonlocal found
        listing = ["run", "list", "--repo", slug, "--workflow", WORKFLOW, "--event", event]
        fetched = _gh_json(gh([*listing, "--commit", sha, "--json", fields, "--limit", "20"]))
        runs = None if fetched.data is None else fetched.data["value"]
        if fetched.data is None:
            found = fetched
        elif not isinstance(runs, list):
            found = _Fetched(None, _UNPARSABLE)
        elif runs:
            found = _Fetched(max(runs, key=lambda run: run.get("createdAt", "")), "")
        else:
            found = _Fetched(None, "")
        return found.data is not None

    poll(probe, timeout_s=timeout_s, interval=interval, sleep=sleep, clock=clock)
    return found


def _view_run(slug: str, run_id: object, *, gh: GhRunner) -> "_Fetched":
    """Return the run's ``jobs``/``url``/``status`` JSON as ``data``, or gh's complaint."""
    fetched = _gh_json(
        gh(["run", "view", "--repo", slug, str(run_id), "--json", "jobs,url,status"])
    )
    value = (fetched.data or {}).get("value")
    if isinstance(value, dict):
        return _Fetched(value, "")
    return _Fetched(None, fetched.error or _UNPARSABLE)


def ci_verdict(
    slug: str,
    sha: str,
    required: "list[str]",
    *,
    gh: GhRunner = run_gh,
    appear_s: float = 110.0,
    interval: float = 10.0,
    sleep: "Callable[[float], None]" = time.sleep,
    clock: "Callable[[], float]" = time.monotonic,
) -> Verdict:
    """Judge the newest ``ci.yml`` push run on *sha* by its required jobs only.

    Waits up to *appear_s* seconds for the run to exist; a listing that fails
    is no verdict, its cause in ``detail``.
    """
    picked = _newest_run(
        slug,
        sha,
        "push",
        _RUN_FIELDS,
        gh=gh,
        timeout_s=appear_s,
        interval=interval,
        sleep=sleep,
        clock=clock,
    )
    run = picked.data
    if run is None:
        detail = [f"no ci.yml push run on {sha[:12]}"]
        detail += [picked.error] if picked.error else []
        return Verdict(False, False, "", detail, unread=True)
    fetched = _view_run(slug, run["databaseId"], gh=gh)
    view = fetched.data
    if view is None:
        return Verdict(False, False, run.get("url", ""), [fetched.error], unread=True)
    jobs = {job["name"]: job for job in view.get("jobs", [])}
    run_done = view.get("status") == "completed"
    detail: list[str] = []
    pending = False
    for name in (n for n in required if n not in EXCLUDED_FROM_VERDICT):
        job = jobs.get(name)
        if job is None:
            if run_done:
                detail.append(f"{name}: no job")
            else:
                pending = True
        elif job.get("status") != "completed":
            pending = True
        elif job.get("conclusion") != "success":
            detail.append(f"{name}: {job.get('conclusion')}")
    return Verdict(not detail and not pending, pending, view.get("url", run.get("url", "")), detail)


def await_verdict(
    slug: str,
    sha: str,
    required: "list[str]",
    *,
    wait_s: float,
    interval: float,
    gh: GhRunner = run_gh,
    sleep: "Callable[[float], None]" = time.sleep,
    clock: "Callable[[], float]" = time.monotonic,
) -> Verdict:
    """Read :func:`ci_verdict` until it settles or *wait_s* seconds pass; the last verdict read.

    The one waiter on CI's verdict. Its own loop is the only wait (each read is
    one probe, ``appear_s=0``), so no read overruns the deadline. A run not read
    yet (absent, or ``gh`` failing) is waited for like a pending one; at the
    deadline a still-pending verdict comes back pending, an unread one ``unread``
    with the cause in ``detail``.
    """
    latest: list[Verdict] = []

    def settled() -> bool:
        latest[:] = [ci_verdict(slug, sha, required, gh=gh, appear_s=0, sleep=sleep, clock=clock)]
        return not (latest[0].pending or latest[0].unread)

    poll(settled, timeout_s=wait_s, interval=interval, sleep=sleep, clock=clock)
    return latest[0]


def admit_pr_run(
    slug: str,
    sha: str,
    *,
    gh: GhRunner = run_gh,
    wait_s: float = 600.0,
    interval: float = 10.0,
    sleep: "Callable[[float], None]" = time.sleep,
    clock: "Callable[[], float]" = time.monotonic,
) -> int:
    """Re-run a failed ``gate-admission`` job of *sha*'s pull-request run; the exit code."""
    picked = _newest_run(
        slug,
        sha,
        "pull_request",
        "databaseId,status,url,createdAt",
        gh=gh,
        timeout_s=0,
        interval=0,
        sleep=sleep,
        clock=clock,
    )
    if picked.error:
        print(f"gate-status: cannot list the pull request's runs: {picked.error}", file=sys.stderr)
        return 1
    run = picked.data
    if run is None:
        print("no pull request run yet")
        return 0
    conclusion: list[str] = []
    failed: list[str] = []

    def admission_done() -> bool:
        fetched = _view_run(slug, run["databaseId"], gh=gh)
        if fetched.data is None:  # a failed read stops the wait: it is not "not done yet"
            failed.append(fetched.error)
            return True
        for job in fetched.data.get("jobs", []):
            if job["name"] == ADMISSION_JOB and job.get("status") == "completed":
                conclusion.append(job.get("conclusion") or "")
                return True
        return False

    if not poll(admission_done, timeout_s=wait_s, interval=interval, sleep=sleep, clock=clock):
        print(f"gate-admission did not finish: {run.get('url', '')}", file=sys.stderr)
        return 1
    if failed:
        print(f"gate-status: cannot read the pull request's run: {failed[-1]}", file=sys.stderr)
        return 1
    if conclusion[-1] == "success":
        return 0
    rerun = gh(["run", "rerun", "--repo", slug, str(run["databaseId"]), "--failed"])
    if rerun.status != 0:
        print(rerun.stderr.strip(), file=sys.stderr)
        return 1
    return 0


def _post_command(args: argparse.Namespace, state: str) -> int:
    outcome = post_status(
        args.repo_slug, args.sha, Description(args.tree, args.base, args.digest), state=state
    )
    if not outcome.ok:
        print(outcome.message, file=sys.stderr)
    return 0 if outcome.ok else 1


def _await_command(args: argparse.Namespace) -> int:
    sys.path.insert(0, str(REPO_ROOT))  # run by path, only scripts/ is on sys.path
    try:
        from scripts import gate_scope  # lazy: only this command needs the classifier
        from scripts.gate_stamp_common import GateError
    except ImportError as err:
        print(f"gate-admission: cannot load the oracle: {err}", file=sys.stderr)
        return 1
    try:
        want = expected(gate_scope.plan(Path(args.repo), args.base, args.sha))
    # git could not answer, or the oracle met a lane it does not classify (gate_tip's
    # pre-push check refuses that in one line too); any other error is a bug and propagates
    except (GateError, gate_scope.GateScopeError, gate_scope.UnknownGoalError) as err:
        print(f"gate-admission: cannot compute the expected description: {err}", file=sys.stderr)
        return 1
    try:
        lines = await_status(
            args.repo_slug,
            args.sha,
            want,
            timeout_s=args.attempts * args.interval,
            interval=args.interval,
        )
    except GateError as err:
        print(f"gate-admission: cannot read the local-gate status: {err}", file=sys.stderr)
        return 1
    if lines:
        print("\n".join(lines), file=sys.stderr)
    return 1 if lines else 0


def _ci_verdict_command(args: argparse.Namespace) -> int:
    required = required_contexts(Path(args.required))
    verdict = await_verdict(
        args.repo_slug,
        args.sha,
        required,
        wait_s=args.wait_minutes * 60,
        interval=10.0,
        sleep=CLI_SLEEP,
    )
    print("\n".join(verdict.detail))
    print(verdict.run_url)
    return 0 if verdict.green else 1


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=(__doc__ or "").splitlines()[0])
    commands = parser.add_subparsers(dest="command", required=True)
    for name in ("post", "fail"):
        sub = commands.add_parser(name)
        for flag in ("--repo-slug", "--sha", "--tree", "--base", "--digest"):
            sub.add_argument(flag, required=True)
    wait = commands.add_parser("await")
    for flag in ("--repo-slug", "--sha", "--base"):
        wait.add_argument(flag, required=True)
    wait.add_argument("--repo", default=".")
    wait.add_argument("--attempts", type=int, default=30)
    wait.add_argument("--interval", type=float, default=10.0)
    admit = commands.add_parser("admit")
    for flag in ("--repo-slug", "--sha"):
        admit.add_argument(flag, required=True)
    verdict = commands.add_parser("ci-verdict")
    for flag in ("--repo-slug", "--sha"):
        verdict.add_argument(flag, required=True)
    verdict.add_argument("--required", default=REQUIRED_CONTEXTS_FILE)
    verdict.add_argument("--wait-minutes", type=float, default=0)
    return parser


def main(argv: "list[str] | None" = None) -> int:
    """Run the command line: post, fail, await, admit, ci-verdict."""
    args = _parser().parse_args(argv)
    if args.command == "post":
        return _post_command(args, "success")
    if args.command == "fail":
        return _post_command(args, "failure")
    if args.command == "await":
        return _await_command(args)
    if args.command == "admit":
        return admit_pr_run(args.repo_slug, args.sha)
    return _ci_verdict_command(args)


if __name__ == "__main__":
    sys.exit(main())
