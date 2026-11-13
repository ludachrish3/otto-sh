#!/usr/bin/env python3
"""The issue a red run on ``main`` files: which jobs failed, on what, and since when.

``ci.yml`` and ``nightly.yml`` each end in a ``report-failure`` job that opens
an issue when a run on ``main`` fails. This script writes that issue's title
and body for both. It names every job that did not pass with its first
failing line, the last green run of the same workflow, and the commits in
between. For a landing (``--before``, the push's previous head) it also says
whether the landing's diff holds each failing test file: the question
docs/contributing.md's "When main goes red" asks before anything is reverted.

It always writes a report. A call to GitHub or to git that fails leaves its
part of the body saying so; an issue that is never filed is the worst
outcome a reporter has.

Stdlib only: it runs on the runner's own Python with nothing installed, so
it still reports when the dependency install is what broke. That is why it
reads the diff with plain ``git`` and not through
``scripts.gate_scope.changes_between``, whose module needs ``tomli``; it
passes the same ``--no-renames``, so a renamed file is named the same way.

Usage: the ``report-failure`` job of either workflow, which needs ``GH_TOKEN``
and a full-depth checkout.
"""

import argparse
import json
import re
import subprocess
import sys
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))
from scripts.local_gate_status import run_gh  # noqa: E402 -- path set up above

NOT_PASSED = ["failure", "timed_out", "cancelled"]
"""The job conclusions the report lists. ``skipped`` is a job a failed ``needs`` never started."""

TITLE_JOBS = 4
"""Job names a title carries before it says "and N more"."""

TITLE_LIMIT = 200
"""Characters a title may run to. GitHub refuses one over 256, and a matrix
leg's name carries its matrix values, so four names alone can pass that."""

QUOTED_LINES = 5
"""Failing lines the body quotes per job; the rest are counted."""

LINE_LIMIT = 500
"""Characters of a failing line the body quotes: an assertion can carry a whole page."""

COMMIT_LIMIT = 40
"""Commits the body lists; a longer range says how many more there are."""

LOG_TIMEOUT = 120.0
"""Seconds one job's log download may take: a matrix leg's log runs to megabytes."""

RULE_ANCHOR = "docs/contributing.md#when-main-goes-red"

_TIMESTAMP = re.compile(r"^\d{4}-\d{2}-\d{2}T[\d:.]+Z ")
_ANSI = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")
_PYTEST_FAILURE = re.compile(r"^(?:FAILED|ERROR) (\S+?\.py)(?:::|\s|$)")
_ANNOTATION = "##[error]"

GhApi = Callable[..., str]
"""``gh_api(endpoint, jq=None, paginate=False)``: the response text, or ``OSError``."""


def gh_api(
    endpoint: str,
    jq: "str | None" = None,
    *,
    paginate: bool = False,
    timeout: float = LOG_TIMEOUT,
) -> str:
    """Return ``gh api <endpoint>``'s output, filtered by *jq* if given.

    *paginate* follows every page, for a listing that must be whole (a run's
    jobs). Never for a query that wants the newest row of a long history: it
    would walk the history.
    """
    args = ["api", endpoint]
    if paginate:
        args.append("--paginate")
    if jq is not None:
        args += ["--jq", jq]
    result = run_gh(args, timeout=timeout)
    if result.status != 0:
        raise OSError(f"gh api {endpoint}: {result.stderr.strip() or result.status}")
    return result.stdout


def _lines(log: str) -> "list[str]":
    """Return the log's lines as the job printed them: no Actions timestamp, no colour."""
    return [_ANSI.sub("", _TIMESTAMP.sub("", line)).rstrip() for line in log.splitlines()]


def failing_lines(log: str) -> "list[str]":
    """Return the failing lines of a job's log, first first; none when none is recognised.

    Every pytest summary line (``FAILED path::test - why``, ``ERROR path - why``),
    each once. A job with none reports its first ``##[error]`` annotation alone,
    which is what a lint, a build or a timeout leaves.
    """
    lines = _lines(log)
    failures = list(dict.fromkeys(line for line in lines if _PYTEST_FAILURE.match(line)))
    if failures:
        return failures
    for line in lines:
        if line.startswith(_ANNOTATION):
            return [line.removeprefix(_ANNOTATION)]
    return []


def failing_file(failure: str) -> "str | None":
    """Return the file a pytest failure line names, or None for any other line."""
    matched = _PYTEST_FAILURE.match(failure)
    return matched.group(1) if matched else None


def _quote(lines: "list[str]") -> str:
    """*lines* as a fenced block they cannot close: no mention notifies, no fence ends early."""
    shown = []
    for line in lines[:QUOTED_LINES]:
        safe = line.replace("```", "'''")
        shown.append(safe[:LINE_LIMIT] + " …" if len(safe) > LINE_LIMIT else safe)
    if len(lines) > QUOTED_LINES:
        shown.append(f"… and {len(lines) - QUOTED_LINES} more failing lines in the log")
    return "```text\n" + "\n".join(shown) + "\n```"


def _named(paths: "list[str]") -> str:
    return ", ".join(f"`{path}`" for path in paths)


def _attribution(failures: "list[str]", landing_files: "list[str] | None", rule_url: str) -> str:
    """Say whether the landing's diff holds any file the job's failures name.

    Every failing file is judged, and one in the diff decides: an older test
    that also failed in the same job must not hide the landing's own.
    """
    if not failures:
        return "No failing line could be read from this job's log: open it."
    paths = list(dict.fromkeys(p for p in map(failing_file, failures) if p is not None))
    if not paths:
        return (
            "This line names no test file, so the report cannot say whether the landing "
            f"caused it. Read the log, then follow [When main goes red]({rule_url})."
        )
    if landing_files is None:
        return (
            f"This landing's diff could not be read, so the report cannot say whether it "
            f"holds {_named(paths)}. Check, then follow [When main goes red]({rule_url})."
        )
    held = [path for path in paths if path in landing_files]
    if held:
        verb = "is" if len(held) == 1 else "are"
        return (
            f"{_named(held)} {verb} in this landing's diff. [When main goes red]({rule_url}): "
            "the landing's author opens a revert pull request now, and fixes forward afterwards."
        )
    verb = "is" if len(paths) == 1 else "are"
    return (
        f"{_named(paths)} {verb} not in this landing's diff. If the landing did not change "
        "the code under test either, this is a test that fails only sometimes: `main` is not "
        f"reverted, and the test's cause is fixed ([When main goes red]({rule_url}))."
    )


def _title(prefix: str, names: "list[str]") -> str:
    """Return *prefix* with as many of *names* as fit, and a count of the rest.

    At most ``TITLE_JOBS`` names and ``TITLE_LIMIT`` characters: an issue
    whose title GitHub refuses is an issue that is never filed.
    """
    if not names:
        return prefix
    shown: "list[str]" = []
    for name in names[:TITLE_JOBS]:
        rest = len(names) - len(shown) - 1
        suffix = f" and {rest} more" if rest else ""
        if len(f"{prefix}: {', '.join([*shown, name])}{suffix}") > TITLE_LIMIT:
            break
        shown.append(name)
    rest = len(names) - len(shown)
    if not shown:
        return f"{prefix}: {rest} job" + ("s" if rest > 1 else "")
    return f"{prefix}: {', '.join(shown)}" + (f" and {rest} more" if rest else "")


@dataclass(frozen=True)
class Run:
    """The failed run, as the workflow knows it before anything is asked of GitHub."""

    title: str
    """The issue title's prefix, e.g. ``CI failed on main``."""
    heading: str
    """The body's first line."""
    run_url: str
    sha: str
    rule_url: str
    """Where "When main goes red" is published."""


def render(
    run: Run,
    *,
    failed: "list[dict]",
    attribute: bool,
    landing_files: "list[str] | None",
    commits: "list[str] | None",
    last_green: "dict[str, str] | None",
    notes: str,
) -> "dict[str, str]":
    """Return the issue's ``title`` and ``body``.

    *failed* holds one ``{"name", "url", "failures"}`` per job that did not
    pass, ``failures`` being its failing lines. *attribute* is true for a
    landing: each failure is then judged against *landing_files*, and
    ``None`` there means the diff could not be read. *commits* are the
    one-line commits the run is the first to test (``None``: unreadable).
    *notes* is the workflow's own standing guidance.
    """
    out = [run.heading, "", f"- Workflow run: {run.run_url}", f"- Commit: {run.sha}"]
    if last_green is None:
        out.append("- Last green run of this workflow on `main`: no earlier green run was found")
    else:
        out.append(
            f"- Last green run of this workflow on `main`: `{last_green['sha'][:12]}` "
            f"({last_green['created']}) {last_green['url']}"
        )
    out += ["", "## Jobs that did not pass", ""]
    if not failed:
        out += ["The run's jobs could not be read: open the workflow run above.", ""]
    for job in failed:
        out += [f"### {job['name']}", "", job["url"], ""]
        if job["failures"]:
            out += [_quote(job["failures"]), ""]
        if attribute:
            out += [_attribution(job["failures"], landing_files, run.rule_url), ""]
        elif not job["failures"]:
            out += ["No failing line could be read from this job's log: open it.", ""]
    out += [
        "## Commits this landing brought" if attribute else "## Commits since the last green run",
        "",
    ]
    if commits is None:
        out.append("The commit range could not be read.")
    elif not commits:
        out.append("None: the run before this one tested the same commit.")
    else:
        out += [f"- `{line.replace('`', chr(39))}`" for line in commits[:COMMIT_LIMIT]]
        if len(commits) > COMMIT_LIMIT:
            out.append(f"- and {len(commits) - COMMIT_LIMIT} more")
    if notes.strip():
        out += ["", "## Notes", "", notes.strip()]
    return {
        "title": _title(run.title, [job["name"] for job in failed]),
        "body": "\n".join(out) + "\n",
    }


def _failed_jobs(gh: GhApi, repo: str, run_id: str) -> "list[dict]":
    try:
        listing = gh(
            f"repos/{repo}/actions/runs/{run_id}/jobs?per_page=100", ".jobs[]", paginate=True
        )
        jobs = [json.loads(line) for line in listing.splitlines() if line.strip()]
    except (OSError, ValueError):
        return []
    failed = []
    for job in jobs:
        conclusion = job.get("conclusion")
        if conclusion not in NOT_PASSED:
            continue
        try:
            failures = failing_lines(gh(f"repos/{repo}/actions/jobs/{job['id']}/logs"))
        except OSError:
            failures = []
        name = job["name"] if conclusion == "failure" else f"{job['name']} ({conclusion})"
        failed.append({"name": name, "url": job.get("html_url", ""), "failures": failures})
    return failed


def _last_green(
    gh: GhApi, repo: str, run_id: str, workflow: str, event: str
) -> "dict[str, str] | None":
    """Return the newest green run of *workflow* on ``main`` from before run *run_id*.

    Before, not merely newest: a failed run re-run after a later one went
    green would otherwise name a baseline that is ahead of it. The bound is
    the failed run's creation time, which a re-run keeps; when it cannot be
    read the newest green run stands in.
    """
    try:
        created = gh(f"repos/{repo}/actions/runs/{run_id}", ".created_at").strip()
    except OSError:
        created = ""
    # `branch=main` alone also matches a pull request from a fork's own `main`.
    endpoint = (
        f"repos/{repo}/actions/workflows/{workflow}/runs?branch=main&status=success&per_page=1"
        + (f"&event={event}" if event else "")
        + (f"&created=%3C{created}" if created else "")
    )
    try:
        run = json.loads(gh(endpoint, ".workflow_runs[0]"))
        return {"sha": run["head_sha"], "url": run["html_url"], "created": run["created_at"]}
    except (OSError, ValueError, KeyError, TypeError):
        return None


def collect(gh: GhApi, *, repo: str, run_id: str, workflow: str, event: str = "") -> dict:
    """Ask GitHub for the run's jobs that did not pass and the workflow's last green run.

    *event* narrows the green run to one trigger (``push`` for a landing); empty
    takes any run of the workflow on ``main``.

    Every call may fail without failing the report: no job list is an empty
    ``failed``, an unreadable log is a job with no failing line, and no green
    run is ``None``.
    """
    return {
        "failed": _failed_jobs(gh, repo, run_id),
        "last_green": _last_green(gh, repo, run_id, workflow, event),
    }


def _git(checkout: Path, *args: str) -> "list[str] | None":
    """Return the output lines of ``git <args>`` in *checkout*, or None when git refused.

    Its own spawn, for the module's reason: the gate's ``run_git`` lives in
    ``scripts.gate_stamp_common``, which needs ``tomli``. Like it, undecodable
    bytes are replaced, so a strange path cannot cost the report.
    """
    try:
        done = subprocess.run(  # noqa: S603 -- fixed argv, no shell
            ["git", *args],  # noqa: S607
            cwd=checkout,
            capture_output=True,
            text=True,
            errors="replace",
            check=False,
        )
    except OSError:
        return None
    return done.stdout.splitlines() if done.returncode == 0 else None


def main(argv: "list[str] | None" = None) -> int:
    """Write the issue's title and body; always 0, so the issue is always filed."""
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--workflow", required=True, help="the workflow file, e.g. ci.yml")
    parser.add_argument("--title", required=True, help="the title's prefix")
    parser.add_argument("--heading", required=True, help="the body's first line")
    parser.add_argument("--repo", required=True, help="owner/name")
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--run-url", required=True)
    parser.add_argument("--sha", required=True, help="the commit the run tested")
    parser.add_argument("--before", help="a push's previous head: attribute over before..sha")
    parser.add_argument("--checkout", type=Path, default=REPO_ROOT)
    parser.add_argument("--notes", type=Path, help="a file of standing guidance to append")
    parser.add_argument("--title-out", type=Path, required=True)
    parser.add_argument("--body-out", type=Path, required=True)
    args = parser.parse_args(argv)

    got = collect(
        gh_api,
        repo=args.repo,
        run_id=args.run_id,
        workflow=args.workflow,
        event="push" if args.before else "",
    )
    landing_files = None
    if args.before:
        base = args.before
        landing_files = _git(
            args.checkout, "diff", "--name-only", "--no-renames", f"{base}..{args.sha}"
        )
    else:
        base = got["last_green"]["sha"] if got["last_green"] else None
    commits = _git(args.checkout, "log", "--format=%h %s", f"{base}..{args.sha}") if base else None
    report = render(
        Run(
            title=args.title,
            heading=args.heading,
            run_url=args.run_url,
            sha=args.sha,
            rule_url=f"https://github.com/{args.repo}/blob/main/{RULE_ANCHOR}",
        ),
        failed=got["failed"],
        attribute=bool(args.before),
        landing_files=landing_files,
        commits=commits,
        last_green=got["last_green"],
        notes=args.notes.read_text(encoding="utf-8") if args.notes else "",
    )
    args.title_out.write_text(report["title"] + "\n", encoding="utf-8")
    args.body_out.write_text(report["body"], encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main())
