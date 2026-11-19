"""The release events: marked commits plus corrections, one model for every reader.

Three readers decide what a release contains, and they must agree:
``scripts/check_breaking_marks.py`` (does this commit carry a mark?),
``scripts/release_bump.py`` (which commits force the bump?) and git-cliff,
through ``cliff.toml`` (the census and the release notes). git-cliff is a
Rust binary that reads commit messages itself, so "one model" means the two
Python readers import THIS module, and this module parses a message the way
git-cliff does. ``tests/unit/scripts/test_release_events.py`` drives the real
git-cliff over a corpus of message shapes and pins the agreement.

What git-cliff counts, mirrored here:

* **A header** is the message's first line. It is conventional when it reads
  ``type:``, ``type!:``, ``type(scope):`` or ``type(scope)!:`` followed by a
  description, where the type is any run of characters other than
  whitespace, parentheses, ``!`` and ``:`` (``feat.x`` and ``fé`` are types)
  and a scope is non-empty with no parentheses inside. ``feat()!:`` is not
  conventional, so it marks nothing.
* **The second line** must be empty, or absent; a line of only whitespace is
  not empty. A header continued on its second line makes the whole message
  unconventional.
* **A footer block** starts at the first body paragraph whose FIRST line is a
  footer (``Token: value``, ``Token:value`` or ``Token #value``, where the
  token is ``BREAKING CHANGE`` or a run of the type's characters, so
  ``See.also`` and ``**Note**`` are tokens). From there on, every line that
  starts with a token opens a new footer, and every other line continues the
  previous one. A footer with nothing after its separator takes the next
  non-blank line as its value, whatever that line holds, so a ``Refs:`` line
  above a ``BREAKING CHANGE: x`` line is one ``Refs`` footer and no mark. A block
  whose last footer has no value at all makes the message unconventional. A
  ``BREAKING CHANGE:`` line inside a prose paragraph before the block is
  prose, not a mark. Paragraphs are separated by blank lines, and a line
  holding only whitespace is blank here.
* **An unconventional message** has no mark and no footers. git-cliff still
  keeps it when a ``commit_parsers`` row matches its raw text.
* **A mark** is ``!`` in a conventional header, or a ``BREAKING CHANGE`` /
  ``BREAKING-CHANGE`` footer. It counts only if ``cliff.toml``'s
  ``commit_parsers`` keep the commit: git-cliff drops a skipped commit
  (``ci``, ``test``, ``style``, ``chore(release)``, anything unconventional)
  before it computes the census, so a ``test(api)!:`` mark bumps nothing.
* **Remediation** is the full text of a commit's breaking footers, each with
  its continuation lines up to the next footer line, and an empty one that
  takes a trailer as its value remediates nothing. A marked commit with no
  remediation is listed in ``Events.unremediated``, never refused. A breaking
  footer whose next footer is a wrapped prose line (``exists: ...``, a token
  that is neither in ``TRAILER_TOKENS`` nor trailer-shaped) is cut short, as
  git-cliff cuts it: ``Events.cut_remediation`` lists it.

``MAINTAINER_SCOPES`` is the home of contributing.md's "maintainer tooling is
never breaking": a commit whose every scope is in it is maintainer-only, and a
counted mark on one is listed in ``Events.hidden_marks``.

A **correction** repairs a mark that reached ``main`` missing. It is an
ordinary marked commit that also carries one ``Corrects: <full sha> <original
subject>`` footer per commit it repairs. A release that contains it takes the
bump, because the correction itself is marked, and the notes list the original
subject under the correction's own entry (``cliff.toml`` renders the footer).
Published history is never rewritten, so this is the only repair.

A correction is refused when:

* its ``Corrects:`` value is not a full 40-hex sha, a space and a subject;
* a ``Corrects:`` line sits outside the footer block, where git-cliff never
  reads it;
* the correcting commit carries no mark git-cliff counts;
* the target is not a commit in the repository;
* the target is not an ancestor of the correcting commit (a cherry-pick onto a
  line of history that never shipped the target);
* the quoted subject differs from the target's own first line;
* another commit in the history of the range's tip validly corrects the same
  target with the same kind. Then EVERY such correction is refused, wherever
  it sits: no correction is "first", so a commit's verdict depends only on
  the valid claimants in that history, never on the order history is read
  in, the parent order of a merge, or where the checked range starts.

A target that already carried a mark may still be corrected: one commit can
carry a second, unmarked break. Each correction has a KIND, and duplicates are
keyed by target and kind. Today there is one kind, ``breaking``: stability
tiers are per namespace and nothing is experimental yet, so a missed promotion
acknowledgement (the future second kind, which bumps nothing) cannot occur.
No lifecycle (deprecation, tombstone) exists yet either, so no missing mark is
a lifecycle breach today; when the lifecycle lands, a breach is reported by
its own check and never accepted as a correction
(``todo/590-api-stability-visible.md``).
"""

import functools
import itertools
import os
import re
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

import tomli

REPO_ROOT = Path(__file__).resolve().parent.parent
CLIFF_CONFIG = REPO_ROOT / "cliff.toml"

CORRECTS = "Corrects"
BREAKING = "breaking"
BREAKING_TOKENS = ["BREAKING CHANGE", "BREAKING-CHANGE"]
TRAILER_TOKENS = [
    "Assisted-by",
    "Co-Authored-By",
    "Signed-off-by",
    "Refs",
    "Closes",
    "Fixes",
    "Resolves",
    CORRECTS,
]
"""The footers that are bookkeeping, not prose: a published commit message omits them."""

_TRAILER_KEYS = frozenset(token.casefold() for token in TRAILER_TOKENS)


def is_trailer(token: str) -> bool:
    """Whether *token* is one of :data:`TRAILER_TOKENS`, in any case (``Co-authored-by``)."""
    return token.casefold() in _TRAILER_KEYS


CORRECTION_RULE = (
    "RULE: a correction is a marked commit (`type(scope)!:` and a `BREAKING CHANGE:` "
    "footer, with a type the changelog keeps) whose footer block carries "
    "`Corrects: <full sha> <original subject>` for an ancestor it repairs, quoting "
    "that commit's subject exactly; each target is corrected once. See "
    "docs/contributing.md, Branching and commits."
)

_TYPE = r"[^\s()!:]+"
_HEADER = re.compile(
    rf"^{_TYPE}(?:\((?P<scope>[^()\r\n]+)\))?(?P<bang>!)?:[ \t]*(?P<description>\S.*)"
)
_FOOTER = re.compile(rf"^(?P<token>BREAKING CHANGE|{_TYPE})(?P<separator>:| #)(?P<value>.*)$")
# A token git itself would write as a trailer (`Docs:`, `See-Also:`): capitalised words
# joined by hyphens. A footer token of any other shape (`exists:`) is a wrapped line.
_TRAILER_SHAPE = re.compile(r"^[A-Z][A-Za-z0-9]*(-[A-Za-z0-9]+)*$")
_CORRECTS_VALUE = re.compile(r"^(?P<target>[0-9a-f]{40}) (?P<subject>\S.*)$")
# A blank line holds only Unicode White_Space, as Rust's `char::is_whitespace`
# reads it: Python's `\s` also matches the ASCII separators \x1c-\x1f, Rust does not.
_BLANK = re.compile(r"[^\S\x1c-\x1f]*")

_FIELD = "\x1f"
_RECORD = "\x1e"
_FORMAT = f"--format=%H{_FIELD}%B{_RECORD}"


# Conventional-commit scopes that name maintainer tooling. A commit whose every
# scope is here is maintainer-only: contributing.md's "maintainer tooling is
# never breaking" rule, and the release notes leave it out. A new maintainer
# scope is added here. An unscoped commit is user-facing, and so are ``docs``,
# ``e2e`` and ``test``, which names the ``otto test`` verb; the repository's own
# harness is ``tests``.
MAINTAINER_SCOPES: "frozenset[str]" = frozenset(
    {
        "agents",
        "bed",
        "budget",
        "canary",
        "changelog",
        "chaos",
        "ci",
        "contributing",
        "deps-dev",
        "feedback",
        "fixtures",
        "gate",
        "gate-fresh",
        "gates",
        "gc-freeze",
        "githook",
        "githooks",
        "hermeticity",
        "hostless",
        "import-budget",
        "integration",
        "lab",
        "lab-health",
        "labs",
        "lanes",
        "lease",
        "lint",
        "lint-arch",
        "make",
        "makefile",
        "matrix",
        "measurements",
        "nightly",
        "nox",
        "plan",
        "plans",
        "quality",
        "release",
        "scripts",
        "spec",
        "specs",
        "stability",
        "submodule",
        "tasks",
        "test-harness",
        "testbed",
        "tests",
        "todo",
        "ty",
        "unit",
        "vagrant",
    }
)


@dataclass
class Footer:
    """One footer git-cliff would parse.

    *value* is the first line of its value, which correction validation reads.
    *text* is the whole value: continuation lines up to the next footer line,
    blank lines and indentation kept, trailing blank lines stripped.
    *swallowed* is True when the footer had nothing after its separator and
    took a next line that is itself footer-shaped (an ``Assisted-by:``
    trailer, say) as its value. *separator* is ``:`` or `` #``. *start* is
    the index of its token's body line, *value_line* that of the line its
    value came from: *start* itself, unless the value was empty there.
    """

    token: str
    value: str
    text: str = field(default="", compare=False)
    swallowed: bool = field(default=False, compare=False)
    separator: str = field(default=":", compare=False)
    start: int = field(default=0, compare=False)
    value_line: int = field(default=0, compare=False)


@dataclass
class Commit:
    """A commit as every reader sees it: full sha, header line, and the rest.

    *separated* is False when the second line is not empty (a line of only
    whitespace counts as text), which git-cliff reads as an unconventional
    message.
    """

    sha: str
    subject: str
    body: str
    separated: bool = True


@dataclass
class Correction:
    """One ``Corrects:`` footer: *commit* repairs the mark *target* lacked."""

    commit: str
    target: str
    original: str
    kind: str = BREAKING

    @property
    def key(self) -> "tuple[str, str]":
        """What a duplicate is judged by: the target and the correction kind."""
        return (self.target, self.kind)


@dataclass
class Event:
    """A marked commit in a release, with the corrections it carries."""

    commit: Commit
    corrections: "list[Correction]" = field(default_factory=list)

    def describe(self) -> str:
        """``<sha> <subject>``, then one indented line per corrected commit."""
        lines = [f"{self.commit.sha[:12]} {self.commit.subject}"]
        lines += [f"    corrects {c.target[:12]} {c.original}" for c in self.corrections]
        return "\n".join(lines)


@dataclass
class Refusal:
    """Why one commit's correction cannot stand. No mark excuses it."""

    commit: Commit
    reason: str


@dataclass
class CutRemediation:
    """A breaking footer that a wrapped line, *line* (``exists:``), ends early."""

    commit: Commit
    line: str


@dataclass
class Events:
    """The validated release events of a range, every refused correction, and the doubts.

    *unremediated*, *hidden_marks* and *cut_remediation* are not refusals: the
    release still bumps and ships. Only the mark checker refuses a new one.
    """

    events: "list[Event]" = field(default_factory=list)
    refusals: "list[Refusal]" = field(default_factory=list)
    unremediated: "list[Commit]" = field(default_factory=list)
    hidden_marks: "list[Commit]" = field(default_factory=list)
    """Counted breaking, yet maintainer-only: it bumps the version and shows on no page."""
    cut_remediation: "list[CutRemediation]" = field(default_factory=list)


# ---------------------------------------------------------------------------
# The message parse, mirroring git-cliff.
# ---------------------------------------------------------------------------


def _footer_block_start(lines: "list[str]") -> "int | None":
    """Index of the line that opens *lines*' footer block, or None when it has none.

    A paragraph starts at the body's first line or after a blank line, and the
    block at the first paragraph whose first line is a footer.
    """
    return next(
        (
            i
            for i, line in enumerate(lines)
            if (i == 0 or _BLANK.fullmatch(lines[i - 1])) and _FOOTER.match(line)
        ),
        None,
    )


def _parse_footers(body: str) -> "list[Footer] | None":
    """Return the footers git-cliff parses out of *body*, or None when it cannot.

    None means the block ends in a footer with no value on any later line,
    which git-cliff reads as an unconventional message. Inside the block every
    footer-shaped line opens a footer, and every other line continues the
    one above.
    """
    lines = body.split("\n")
    start = _footer_block_start(lines)
    if start is None:
        return []
    offset, lines = start, lines[start:]
    found = []
    i = 0
    # The block opens on a footer line, and each footer's scan stops at the next one.
    while i < len(lines) and (match := _FOOTER.match(lines[i])):
        first = i
        swallowed = False
        if _BLANK.fullmatch(match["value"]):
            # An empty value takes the next non-blank line, footer-shaped or not.
            i += 1
            while i < len(lines) and _BLANK.fullmatch(lines[i]):
                i += 1
            if i == len(lines):
                return None
            value = lines[i].strip()
            swallowed = _FOOTER.match(lines[i]) is not None
        else:
            value = match["value"].strip()
        end = i + 1
        while end < len(lines) and not _FOOTER.match(lines[end]):
            end += 1
        text = "\n".join([value, *lines[i + 1 : end]]).rstrip()
        found.append(
            Footer(
                match["token"],
                value,
                text,
                swallowed,
                match["separator"],
                offset + first,
                offset + i,
            )
        )
        i = end
    return found


def footers(body: str) -> "list[Footer]":
    """Return the footers git-cliff parses out of *body*, in order (none if it cannot)."""
    return _parse_footers(body) or []


def is_conventional(subject: str, body: str, *, separated: bool = True) -> bool:
    """Whether git-cliff parses the message as a conventional commit at all."""
    return separated and _HEADER.match(subject) is not None and _parse_footers(body) is not None


def commit_footers(commit: Commit) -> "list[Footer]":
    """Return the footers git-cliff reads from *commit*: none from an unconventional one."""
    if not is_conventional(commit.subject, commit.body, separated=commit.separated):
        return []
    return footers(commit.body)


def remediation(commit: Commit) -> "list[str]":
    """Return the full text of each breaking footer *commit* carries, in order.

    An empty breaking footer that took a trailer as its value remediates
    nothing, and neither does a blank one.
    """
    return [
        f.text
        for f in commit_footers(commit)
        if f.token in BREAKING_TOKENS and not f.swallowed and f.text.strip()
    ]


def description(subject: str) -> str:
    """Return the header's text after ``type(scope)!:``; an unconventional *subject* as is."""
    header = _HEADER.match(subject)
    return header["description"].rstrip() if header else subject


def commit_scopes(subject: str) -> "list[str]":
    """Return the header's scopes (a comma list, each stripped), or none."""
    header = _HEADER.match(subject)
    if header is None or header["scope"] is None:
        return []
    return [scope.strip() for scope in header["scope"].split(",")]


def is_maintainer_only(commit: Commit) -> bool:
    """Whether *commit* is scoped, and every scope is in :data:`MAINTAINER_SCOPES`."""
    scopes = commit_scopes(commit.subject)
    return bool(scopes) and all(scope in MAINTAINER_SCOPES for scope in scopes)


def message_without_trailers(commit: Commit) -> str:
    """Return *commit*'s body without its :data:`TRAILER_TOKENS` footers, stripped.

    A trailer's own line goes, with the next line when the trailer was empty
    and took that line, not itself footer-shaped, as its value. Every other
    line stays verbatim: lines wrapped below a trailer and footer-shaped notes
    (``BREAKING:``, ``Docs:``) included. An unconventional commit has no
    footers, so its whole body stays.
    """
    lines = commit.body.split("\n")
    drop: set[int] = set()
    for footer in commit_footers(commit):
        if is_trailer(footer.token):
            drop.add(footer.start)
            if footer.value_line != footer.start and not footer.swallowed:
                drop.add(footer.value_line)
    return "\n".join(line for n, line in enumerate(lines) if n not in drop).strip()


def _is_cut(footer: Footer) -> bool:
    """Whether *footer* is a wrapped prose line git-cliff opened as a footer."""
    token = footer.token
    known = is_trailer(token) or token in BREAKING_TOKENS
    return not known and _TRAILER_SHAPE.match(token) is None


def cut_remediation(commit: Commit) -> "list[CutRemediation]":
    """Return each breaking footer of *commit* that the very next footer cuts short."""
    found = commit_footers(commit)
    return [
        CutRemediation(commit, f"{after.token}{after.separator}")
        for footer, after in itertools.pairwise(found)
        if footer.token in BREAKING_TOKENS and _is_cut(after)
    ]


@dataclass
class _Parser:
    pattern: "re.Pattern[str]"
    skip: bool


@functools.cache
def _cliff_rules(config: Path) -> "tuple[list[_Parser], bool]":
    """Read ``[git].commit_parsers`` and ``protect_breaking_commits`` from *config*.

    Refuses (``ValueError``) a config this module does not mirror -- a parser
    that matches on anything but ``message``, or split or non-conventional
    parsing -- so a ``cliff.toml`` edit cannot silently make the readers
    disagree.
    """
    git = tomli.loads(config.read_text(encoding="utf-8"))["git"]
    if not git.get("conventional_commits", False) or git.get("split_commits", False):
        raise ValueError(f"{config}: release_events mirrors conventional, unsplit commits only")
    parsers = []
    for row in git.get("commit_parsers", []):
        unknown = set(row) - {"message", "group", "skip", "default_scope"}
        if unknown or "message" not in row:
            raise ValueError(
                f"{config}: commit_parsers row {row} uses keys release_events does not mirror"
            )
        parsers.append(_Parser(re.compile(row["message"]), bool(row.get("skip", False))))
    return parsers, bool(git.get("protect_breaking_commits", False))


@functools.cache
def release_tag_glob(config: Path = CLIFF_CONFIG) -> str:
    """Return the pattern that selects release tags: ``[git].tag_pattern`` in *config*.

    git-cliff reads it as a regular expression and git as a glob (``git
    describe --match``, ``git tag --list``). ``cliff.toml`` writes it so that
    both select otto's ``v<digit>...`` tags, and every reader that asks git for
    a release tag takes the pattern from here.
    """
    return tomli.loads(config.read_text(encoding="utf-8"))["git"]["tag_pattern"]


def kept_by_cliff(message: str, *, breaking: bool, config: "Path | None" = None) -> bool:
    """Whether git-cliff keeps the commit whose raw *message* this is (first parser wins)."""
    parsers, protect = _cliff_rules(config or CLIFF_CONFIG)
    if breaking and protect:
        return True
    for parser in parsers:
        if parser.pattern.search(message):
            return not parser.skip
    return True


def is_breaking_commit(subject: str, body: str, *, separated: bool = True) -> bool:
    """Whether git-cliff's census counts *subject*/*body* as a breaking change.

    *separated* is :attr:`Commit.separated`: whether a blank line, or nothing,
    follows the header.
    """
    header = _HEADER.match(subject)
    if header is None or not is_conventional(subject, body, separated=separated):
        return False
    breaking = bool(header["bang"]) or any(f.token in BREAKING_TOKENS for f in footers(body))
    message = f"{subject}\n\n{body}" if body else subject
    return breaking and kept_by_cliff(message, breaking=True)


def counts_as_breaking(commit: Commit) -> bool:
    """Whether git-cliff's census counts *commit* as a breaking change."""
    return is_breaking_commit(commit.subject, commit.body, separated=commit.separated)


# ---------------------------------------------------------------------------
# Reading commits.
# ---------------------------------------------------------------------------


def _git(repo: Path, *args: str, check: bool = True) -> "subprocess.CompletedProcess[str]":
    """Run git in *repo* with global and system config neutered; read-only."""
    return subprocess.run(  # noqa: S603 -- fixed argv, no shell; the parts are ours
        ["git", *args],  # noqa: S607 -- `git` via PATH
        cwd=repo,
        capture_output=True,
        text=True,
        check=check,
        env={**os.environ, "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_SYSTEM": "/dev/null"},
    )


def _parse_log(out: str) -> "list[Commit]":
    commits = []
    for raw in out.split(_RECORD):
        record = raw.strip("\n")
        if not record:
            continue
        sha, _, message = record.partition(_FIELD)
        subject, _, rest = message.partition("\n")
        # Exactly empty: git-cliff reads a second line of only whitespace as text.
        separated = rest.split("\n", 1)[0] == ""
        commits.append(Commit(sha, subject, rest.strip("\n"), separated))
    return commits


def read_commits(repo: Path, rev_range: str) -> "list[Commit]":
    """Return every commit in *rev_range*, merges included, oldest first, topologically."""
    return _parse_log(_git(repo, "log", "--reverse", "--topo-order", _FORMAT, rev_range).stdout)


def read_commit(repo: Path, rev: str) -> Commit:
    """Return the one commit *rev* names."""
    return _parse_log(_git(repo, "log", "--no-walk", _FORMAT, rev).stdout)[0]


def _range_tip(rev_range: str) -> str:
    """Return the revision *rev_range*'s history ends at: ``B`` of ``A..B``, or a bare one."""
    _, dots, tip = rev_range.partition("..")
    if not dots:
        return rev_range
    return tip.lstrip(".") or "HEAD"


# ---------------------------------------------------------------------------
# Corrections.
# ---------------------------------------------------------------------------


def _claims(commit: Commit) -> "tuple[list[Correction], list[str]]":
    """Return the corrections *commit*'s footers claim, and why any cannot be read."""
    values = [f.value for f in commit_footers(commit) if f.token == CORRECTS]
    reasons = []
    lines = [m for m in map(_FOOTER.match, commit.body.split("\n")) if m and m["token"] == CORRECTS]
    if len(lines) > len(values):
        reasons.append(
            "a `Corrects:` line sits outside the footer block, where git-cliff never reads "
            "it (or the message is not conventional, or the line is the value of an empty "
            "footer above it); move it into the last paragraph of a conventional message"
        )
    claims = []
    for value in values:
        match = _CORRECTS_VALUE.match(value)
        if match is None:
            reasons.append(f"`Corrects: {value}` is not `<full 40-hex sha> <original subject>`")
        else:
            claims.append(Correction(commit.sha, match["target"], match["subject"]))
    return claims, reasons


def _claim_problem(repo: Path, claim: Correction) -> "str | None":
    """Why *claim* cannot stand on its own (duplicates aside), or None."""
    target = claim.target[:12]
    proc = _git(repo, "merge-base", "--is-ancestor", claim.target, claim.commit, check=False)
    if proc.returncode not in (0, 1):
        return f"Corrects: {target} is not a commit in this repository"
    if proc.returncode == 1:
        return (
            f"Corrects: {target} is not an ancestor of this commit -- a correction "
            "repairs only the history that shipped its target, and a cherry-pick "
            "does not carry the target with it"
        )
    actual = read_commit(repo, claim.target).subject
    if actual != claim.original:
        return (
            f"Corrects: {target} quotes {claim.original!r}, but that commit's subject is {actual!r}"
        )
    return None


def _commit_problems(repo: Path, commit: Commit) -> "tuple[list[Correction], list[str]]":
    """Return what *commit* corrects, and why it cannot stand (other commits aside).

    A commit's corrections stand or fall together: one bad footer refuses them all.
    """
    claims, reasons = _claims(commit)
    breaking = counts_as_breaking(commit)
    if (claims or reasons) and not breaking:
        reasons.append(
            "a correction must carry a mark git-cliff counts: `type(scope)!:` or a "
            "`BREAKING CHANGE:` footer, with a type cliff.toml keeps"
        )
    seen: set[tuple[str, str]] = set()
    for claim in claims:
        problem = _claim_problem(repo, claim)
        if problem is None and claim.key in seen:
            problem = f"Corrects: {claim.target[:12]} appears twice in this commit"
        seen.add(claim.key)
        if problem is not None:
            reasons.append(problem)
    return claims, reasons


def _claimants(repo: Path, tip: str) -> "dict[tuple[str, str], list[str]]":
    """Map each correction key to every commit in *tip*'s history that validly claims it.

    A commit refused for its own fault claims nothing, so it never makes a
    valid correction a duplicate. The answer depends only on *tip*'s history.
    """
    out = _git(
        repo, "log", "--reverse", "--topo-order", "-E", f"--grep=^{CORRECTS}(:| #)", _FORMAT, tip
    ).stdout
    claimants: dict[tuple[str, str], list[str]] = {}
    for commit in _parse_log(out):
        claims, reasons = _commit_problems(repo, commit)
        if reasons:
            continue
        for claim in claims:
            claimants.setdefault(claim.key, []).append(commit.sha)
    return claimants


def _duplicates(
    claims: "list[Correction]", claimants: "dict[tuple[str, str], list[str]]"
) -> "list[str]":
    """Why each of *claims* that another commit also validly claims cannot stand."""
    reasons = []
    for claim in claims:
        others = [sha[:12] for sha in claimants.get(claim.key, []) if sha != claim.commit]
        if others:
            reasons.append(
                f"Corrects: {claim.target[:12]} is also corrected ({claim.kind}) by "
                f"{', '.join(others)}; a target is corrected once per kind, so no "
                "correction of it stands"
            )
    return reasons


def release_events(repo: Path, rev_range: str) -> Events:
    """Return the release events of *rev_range* and every correction refused in it.

    An event is a commit git-cliff's census counts as breaking, with the
    corrections it validly claims. A ``Corrects:`` footer that breaks any rule
    in this module's docstring is refused, and the commit's other corrections
    with it. A refused correction that is marked is still a breaking event,
    because the census still counts its mark. Duplicates are judged against
    every valid claimant in the history of the range's tip, so a commit's
    verdict does not depend on where the range starts.

    ``unremediated`` lists the events with no remediation text,
    ``hidden_marks`` the maintainer-only ones, and ``cut_remediation`` each
    breaking footer a wrapped ``word:`` line ends early. None is a refusal: the
    release still bumps and ships; only the mark checker refuses a new one.
    """
    claimants = _claimants(repo, _range_tip(rev_range))
    result = Events()
    for commit in read_commits(repo, rev_range):
        claims, reasons = _commit_problems(repo, commit)
        if not reasons:
            reasons = _duplicates(claims, claimants)
        result.refusals += [Refusal(commit, reason) for reason in reasons]
        if counts_as_breaking(commit):
            result.events.append(Event(commit, [] if reasons else claims))
            if not remediation(commit):
                result.unremediated.append(commit)
            if is_maintainer_only(commit):
                result.hidden_marks.append(commit)
            result.cut_remediation += cut_remediation(commit)
    return result
