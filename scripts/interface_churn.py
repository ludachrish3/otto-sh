"""Report how much of otto's interface each release changed. A measurement, never a gate.

One row per window, where a window is two consecutive revisions (by default
every release tag HEAD contains, then ``HEAD``). ``docs/contributing.md``,
"Measuring interface churn", is the home of what each column means and what
the report leaves out; in short:

* the recorded surface comes from the public-API golden at each end. Two v2
  goldens go through ``api_compat.compare_dumps``, the comparison ``make
  check-breaking`` runs on each commit; two v1 goldens compare by line;
* the marked commits are those ``release_events`` counts as breaking, by
  today's ``cliff.toml``;
* ``--unrecorded`` approximates the flags and environment variables, which no
  golden records, by their spellings in ``src/otto``.

Exit codes: 0 reported (or no window to report), 2 a usage error or a revision
git cannot resolve.
"""

import argparse
import itertools
import re
import subprocess
import sys
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))
from otto.config.env import is_otto_env_var  # noqa: E402 -- path set up above
from scripts import api_compat, api_lines, api_records, release_events  # noqa: E402
from scripts.check_breaking_marks import (  # noqa: E402 -- path set up above
    DEFAULT_GOLDEN,
    golden_text_at,
    repo_relative,
)
from scripts.release_events import _git  # noqa: E402 -- path set up above

SOURCE_PATHSPEC = "src/otto/*.py"
_FLAG = re.compile(r"""(?<=["'/])--[a-z][a-z0-9-]+(?=["'/ =])""")
_ENV_CANDIDATE = re.compile(r"\bOTTO[A-Z0-9_]+\b")
_NOT_COMPARABLE = "-"


@dataclass
class Recorded:
    """What two goldens say about a window: the older surface and what happened to it."""

    surface: int
    broken: "list[str]"
    added: int
    findings: "list[str] | None" = None

    @property
    def churn(self) -> float:
        """The share of the older golden's sites that broke, in percent."""
        return 100 * len(self.broken) / self.surface if self.surface else 0.0


@dataclass
class Spellings:
    """One unrecorded surface across a window: spellings gone, new, and at the newer end."""

    removed: "list[str]"
    added: "list[str]"
    total: int


@dataclass
class Window:
    """One pair of consecutive revisions and everything measured between them."""

    old: str
    new: str
    commits: int
    marked: int
    recorded: "Recorded | None"
    unrecorded: "dict[str, Spellings]" = field(default_factory=dict)


def release_tags(repo: Path, tip: str = "HEAD") -> "list[str]":
    """Return the release tags reachable from *tip*, oldest version first."""
    glob = release_events.release_tag_glob()
    out = _git(repo, "tag", "--list", glob, "--sort=version:refname", "--merged", tip)
    return out.stdout.split()


def default_revs(repo: Path) -> "list[str]":
    """Return the release tags ``HEAD`` contains, then ``HEAD`` when it is past the last one."""
    tags = release_tags(repo)
    if not tags:
        return ["HEAD"]
    ahead = _git(repo, "rev-list", "--count", f"{tags[-1]}..HEAD").stdout.strip()
    return [*tags, "HEAD"] if ahead != "0" else tags


def _compare_v2(old: str, new: str) -> "Recorded | None":
    try:
        before, after = api_records.parse_dump(old), api_records.parse_dump(new)
    except api_records.DumpError:
        return None
    findings = api_compat.compare_dumps(before, after)
    old_sites, new_sites = api_compat.sites(before), api_compat.sites(after)
    named = {api_compat.site_of(f) for f in findings}
    # A finding names a site of the older golden, or an input only the newer one has
    # (a new required input): that one breaks the binding it was added to.
    broken = {site if site in old_sites else new_sites[site] for site in named}
    # A removed binding is one finding, and everything it owned went with it.
    gone = before.bindings.keys() - after.bindings.keys()
    broken |= {site for site, owner in old_sites.items() if owner in gone}
    return Recorded(
        surface=len(old_sites),
        broken=sorted(broken),
        added=len(new_sites.keys() - old_sites.keys()),
        findings=findings,
    )


def _compare_v1(old: str, new: str) -> Recorded:
    before, after = set(api_lines.data_lines(old)), set(api_lines.data_lines(new))
    return Recorded(surface=len(before), broken=sorted(before - after), added=len(after - before))


def compare_goldens(old: "str | None", new: "str | None") -> "Recorded | None":
    """Return what the goldens *old* and *new* record of a window, or None when not comparable.

    Two v2 dumps go through :func:`api_compat.compare_dumps`. Two v1 goldens compare by
    line: a removed or reworded line is a broken site. A missing golden, a dump that does
    not parse, and the v1-to-v2 cutover are not comparable.
    """
    if old is None or new is None:
        return None
    schemas = (api_lines.schema_of(old), api_lines.schema_of(new))
    if schemas == (2, 2):
        return _compare_v2(old, new)
    return _compare_v1(old, new) if schemas == (1, 1) else None


def _spellings(repo: Path, rev: str) -> "dict[str, set[str]]":
    """Return the flag and environment-variable spellings in ``src/otto`` at *rev*."""
    grep = _git(
        repo,
        "grep",
        "-hE",
        "-e",
        "--[a-z]",
        "-e",
        "OTTO[A-Z_]",
        rev,
        "--",
        SOURCE_PATHSPEC,
        check=False,  # 1 is "no match": a revision from before src/otto has neither
    )
    return {
        "flags": set(_FLAG.findall(grep.stdout)),
        "env": {name for name in _ENV_CANDIDATE.findall(grep.stdout) if is_otto_env_var(name)},
    }


def _unrecorded(repo: Path, old: str, new: str) -> "dict[str, Spellings]":
    before, after = _spellings(repo, old), _spellings(repo, new)
    return {
        name: Spellings(
            sorted(before[name] - after[name]), sorted(after[name] - before[name]), len(after[name])
        )
        for name in before
    }


def measure(
    repo: Path, old: str, new: str, golden: Path = DEFAULT_GOLDEN, *, unrecorded: bool = False
) -> Window:
    """Measure the window from *old* to *new*."""
    commits = release_events.read_commits(repo, f"{old}..{new}")
    marked = [c for c in commits if release_events.counts_as_breaking(c)]
    recorded = compare_goldens(golden_text_at(repo, old, golden), golden_text_at(repo, new, golden))
    return Window(
        old,
        new,
        len(commits),
        len(marked),
        recorded,
        _unrecorded(repo, old, new) if unrecorded else {},
    )


def _row(window: Window) -> "list[str]":
    rec = window.recorded
    cells = [f"{window.old}..{window.new}", str(window.commits), str(window.marked)]
    if rec is None:
        cells += [_NOT_COMPARABLE] * 6
    else:
        findings = rec.findings
        cells += [
            str(rec.surface),
            str(len(rec.broken)),
            f"{rec.churn:.2f}%",
            _NOT_COMPARABLE if findings is None else str(len(findings)),
            _NOT_COMPARABLE if findings is None else str(len(api_compat.group_findings(findings))),
            str(rec.added),
        ]
    for surface in window.unrecorded.values():
        cells.append(f"-{len(surface.removed)} +{len(surface.added)} ={surface.total}")
    return cells


def _table(rows: "list[list[str]]") -> "list[str]":
    widths = [max(len(row[i]) for row in rows) for i in range(len(rows[0]))]
    return [
        "  ".join(
            cell.ljust(width) if i == 0 else cell.rjust(width)
            for i, (cell, width) in enumerate(zip(row, widths, strict=True))
        )
        for row in rows
    ]


def _detail(window: Window) -> "list[str]":
    out = [f"{window.old}..{window.new}"]
    rec = window.recorded
    if rec is not None and rec.broken:
        spaces = Counter(api_compat.namespace_of(site) for site in rec.broken)
        out.append("  broken sites by namespace:")
        out += [f"    {count:4d}  {name}" for name, count in spaces.most_common()]
        if rec.findings is None:  # two v1 goldens: the comparison is by line
            out += ["  lines gone or reworded:", *(f"    {line}" for line in rec.broken)]
        else:
            distinct = api_compat.group_findings(rec.findings)
            out += ["  distinct findings:", *(f"    {finding}" for finding in distinct)]
    for name, surface in window.unrecorded.items():
        if surface.removed:
            out.append(f"  {name} gone: {' '.join(surface.removed)}")
    return out if len(out) > 1 else []


def render(windows: "list[Window]", *, detail: bool = False) -> str:
    """Render *windows* as a table, then each window's detail when *detail*."""
    header = [
        "window",
        "commits",
        "marked",
        "surface",
        "broken",
        "churn",
        "findings",
        "distinct",
        "added",
    ]
    if windows and windows[0].unrecorded:
        header += [f"{name} (approx.)" for name in windows[0].unrecorded]
    lines = _table([header, *(_row(w) for w in windows)])
    if detail:
        for window in windows:
            block = _detail(window)
            lines += ["", *block] if block else []
    return "\n".join(lines) + "\n"


def main(argv: "list[str] | None" = None) -> int:
    """Print the interface-churn report for the given revisions."""
    parser = argparse.ArgumentParser(description=(__doc__ or "").splitlines()[0])
    parser.add_argument(
        "revs",
        nargs="*",
        help="two or more revisions, oldest first; each consecutive pair is a window "
        "(default: the release tags HEAD contains, then HEAD)",
    )
    parser.add_argument("--repo", type=Path, default=REPO_ROOT, help="the repository to read")
    parser.add_argument(
        "--golden",
        type=Path,
        default=DEFAULT_GOLDEN,
        help="the public-API golden, relative to --repo unless absolute "
        f"(default: {DEFAULT_GOLDEN})",
    )
    parser.add_argument(
        "--unrecorded",
        action="store_true",
        help="also approximate the flags and otto's environment variables, from src/otto at "
        "each revision",
    )
    parser.add_argument(
        "--detail",
        action="store_true",
        help="after the table, list each window's broken sites by namespace and its findings",
    )
    args = parser.parse_args(argv)
    if len(args.revs) == 1:
        parser.error("a window needs two revisions: pass at least two, oldest first")
    if repo_relative(args.repo, args.golden) is None:
        # git reads the golden from each revision, so a file outside the repository has
        # no history: every window would read as "no golden", indistinguishable from true.
        parser.error(f"--golden must be a path inside --repo: {args.golden}")
    try:
        revs = args.revs or default_revs(args.repo)
        if len(revs) < 2:  # noqa: PLR2004 -- a window is a pair
            why = "no release tag yet" if revs == ["HEAD"] else "HEAD is the only release tag"
            print(f"interface-churn: no window to report: {why}")
            return 0
        windows = [
            measure(args.repo, old, new, args.golden, unrecorded=args.unrecorded)
            for old, new in itertools.pairwise(revs)
        ]
    except subprocess.CalledProcessError as exc:
        print(f"interface-churn: {(exc.stderr or str(exc)).strip()}", file=sys.stderr)
        return 2
    # After the windows: measuring them is what refuses a revision git cannot resolve.
    if all(golden_text_at(args.repo, rev, args.golden) is None for rev in revs):
        print(
            f"interface-churn: no revision given has the golden {args.golden}: "
            "the recorded columns are empty",
            file=sys.stderr,
        )
    sys.stdout.write(render(windows, detail=args.detail))
    return 0


if __name__ == "__main__":
    sys.exit(main())
