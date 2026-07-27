# Per-Ticket Coverage Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Attribute every covered source line to the ticket(s) whose commit last touched it, and surface that as a searchable `#/tickets` page, a file-page gutter, a report-wide ticket context, and a `tickets.json` export.

**Architecture:** Attribution comes from **one** `git log -p -U0 -w -M --first-parent` subprocess whose hunks are replayed backward from HEAD using the existing `LineRemapper`, not from `git blame` (which has no batch mode and costs ~267 filesystem ops per file — see spec §3.1). Ticket ids are parsed out of commit messages by a configured regex. The result lands on `LineRecord.ticket: list[str]`, flows through `store.json` v5 into the SPA data chunks, and is rendered by a new page plus additions to the existing file page.

**Tech Stack:** Python 3.10+ (stdlib `re`, `subprocess`), pydantic settings specs, Typer CLI, React 19 + TypeScript + wouter (classic-IIFE covapp bundle), Vitest, Playwright.

**Spec:** `docs/superpowers/specs/2026-07-26-per-ticket-coverage-design.md` — read §2 (rulings), §3 (engine), §6.3 (ticket context) before starting.

## Global Constraints

- **Never `from __future__ import annotations`** — it breaks Sphinx nitpicky `-W`.
- **`models/` must NEVER import `config/`.** The pydantic spec validates at settings-parse time; report-time code re-reads the raw dict (the `report_config.py` pattern).
- **Prefer `list` over `tuple` in public APIs**; callables return dataclasses.
- **Typer rejects `Union` types** — only `Optional[X]`.
- **No `@pytest.mark.skip` on host-down**; this feature is hostless, so no bed access is needed at all.
- **Per-task gate:** `pytest <the task's test paths> -v` then `nox -s lint`. **Final gate only:** `make coverage`, `nox -s typecheck`, `nox -s dashboard`, `make docs`.
- **`ty` runs only at `nox -s typecheck`** — budget a typecheck round after any `src/` edits.
- **Browser tests:** bare `pytest tests/e2e/...` runs **chromium only**. The real matrix is `nox -s dashboard`.
- **`pytest` does NOT build the web dist** — run `make web` after any `web/src/` change or you are testing a stale bundle.
- **Commits:** this plan runs in a worktree, so self-commit is fine. Conventional prefix + an `Assisted-by: Claude Opus 5 (1M context) <noreply@anthropic.com>` trailer. Never `git push`.
- **Store format is exact-match loud-fail** — no migration shims, ever.
- Report stays **byte-identical to today** when `[coverage.tickets]` is absent.

---

## File Structure

**Create:**

- `src/otto/coverage/tickets.py` — `TicketSpec`: compiled pattern, `extract()`, `url_for()`.
- `src/otto/coverage/attribution.py` — commit-diff parser, backward replay, worktree overlay.
- `src/otto/coverage/ticket_export.py` — the `tickets.json` writer.
- `web/src/covapp/pages/TicketsPage.tsx` + `.test.tsx`
- `web/src/covapp/tickets.ts` + `.test.ts` — ticket-scoped stat recomputation (the denominator filter).
- `tests/unit/cov/test_tickets.py`, `test_attribution.py`, `test_ticket_export.py`
- `tests/integration/cov/test_attribution_oracle.py` — the `git blame` oracle.
- `docs/architecture/coverage/attribution.md`

**Modify:**

- `src/otto/coverage/capture/treediff.py` — promote `_unquote`/`_strip_side` to public (Task 3).
- `src/otto/coverage/capture/gitio.py` — add `log_walk_u0()`.
- `src/otto/models/settings.py` — `CoverageTicketsSpec`.
- `src/otto/coverage/report_config.py` — `load_ticket_spec()`.
- `src/otto/coverage/store/model.py` — v5.
- `src/otto/coverage/reporter.py` — run attribution, annotate store.
- `src/otto/coverage/renderer/spa_data.py` — ticket payloads.
- `src/otto/cli/cov.py`, `src/otto/cli/test.py` — `--tickets-json` / `--cov-tickets-json`.
- `web/src/covapp/{types.ts,App.tsx,focus.tsx,pages/FilePage.tsx,pages/DirectoryPage.tsx,chrome/AppShell.tsx}`
- `todo/coverage_roadmap.md`, `todo/TODO.md` — Task 12 cleanup.

---

### Task 1: Ticket config and id extraction

**Files:**
- Create: `src/otto/coverage/tickets.py`
- Modify: `src/otto/models/settings.py`
- Modify: `src/otto/coverage/report_config.py`
- Test: `tests/unit/cov/test_tickets.py`

**Interfaces:**
- Consumes: nothing (first task).
- Produces: `TicketSpec` (frozen dataclass) with `pattern: str`, `url: str | None`, `extract(message: str) -> list[str]`, `url_for(ticket_id: str) -> str | None`; `build_ticket_spec(pattern, url) -> TicketSpec` (raises `TicketConfigError`); `load_ticket_spec(cov_config: dict[str, Any]) -> TicketSpec | None`; exception `TicketConfigError`.

- [ ] **Step 1: Write the failing tests**

```python
# tests/unit/cov/test_tickets.py
import pytest

from otto.coverage.tickets import TicketConfigError, build_ticket_spec, load_ticket_spec


def test_extract_returns_whole_match_as_display_id():
    spec = build_ticket_spec(r"#(?P<num>[0-9]+)", "https://gh/x/issues/{num}")
    assert spec.extract("fix arp #1204") == ["#1204"]


def test_url_uses_named_group_not_whole_match():
    """GitHub's display id is `#1204` but its URL takes only `1204`."""
    spec = build_ticket_spec(r"#(?P<num>[0-9]+)", "https://gh/x/issues/{num}")
    assert spec.url_for("#1204") == "https://gh/x/issues/1204"


def test_whole_match_available_to_url_as_zero():
    spec = build_ticket_spec(r"[A-Z]{2,10}-[0-9]+", "https://jira/browse/{0}")
    assert spec.url_for("PROJ-412") == "https://jira/browse/PROJ-412"


def test_multi_ticket_commit_yields_all_ids_deduped_in_order():
    spec = build_ticket_spec(r"[A-Z]{2,10}-[0-9]+", None)
    ids = spec.extract("PROJ-412 and PROJ-388\n\nalso PROJ-412 again")
    assert ids == ["PROJ-412", "PROJ-388"]


def test_no_match_yields_empty_list():
    spec = build_ticket_spec(r"[A-Z]{2,10}-[0-9]+", None)
    assert spec.extract("chore: bump deps") == []


def test_url_none_yields_none():
    spec = build_ticket_spec(r"[A-Z]{2,10}-[0-9]+", None)
    assert spec.url_for("PROJ-412") is None


def test_bad_regex_fails_loud_at_build():
    with pytest.raises(TicketConfigError, match="not a valid regular expression"):
        build_ticket_spec(r"([A-Z]+", None)


def test_url_naming_unknown_group_fails_loud_at_build():
    """A template naming a group the pattern lacks must not become a render-time KeyError."""
    with pytest.raises(TicketConfigError, match="unknown group 'key'"):
        build_ticket_spec(r"#(?P<num>[0-9]+)", "https://x/{key}")


def test_load_returns_none_when_block_absent():
    assert load_ticket_spec({}) is None
    assert load_ticket_spec({"report": {"high": 90}}) is None


def test_load_builds_spec_from_raw_dict():
    spec = load_ticket_spec({"tickets": {"pattern": r"#(?P<n>[0-9]+)", "url": "u/{n}"}})
    assert spec is not None
    assert spec.extract("see #7") == ["#7"]


def test_load_without_pattern_fails_loud():
    with pytest.raises(TicketConfigError, match="requires a 'pattern'"):
        load_ticket_spec({"tickets": {"url": "u/{n}"}})
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/unit/cov/test_tickets.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'otto.coverage.tickets'`

- [ ] **Step 3: Implement `tickets.py`**

```python
# src/otto/coverage/tickets.py
"""``[coverage.tickets]`` runtime resolution — commit-message ticket ids.

Mirrors :mod:`otto.coverage.report_config`: the pydantic spec
(:class:`otto.models.settings.CoverageTicketsSpec`) validates the block at
settings-parse time; this module re-reads the raw dict at report time and
compiles the pattern.
"""

import re
import string
from dataclasses import dataclass
from typing import Any


class TicketConfigError(ValueError):
    """``[coverage.tickets]`` is malformed — raised loud, never rendered."""


@dataclass(frozen=True)
class TicketSpec:
    """A compiled ticket-id pattern plus an optional tracker URL template."""

    pattern: str
    url: str | None
    _regex: re.Pattern[str]

    def extract(self, message: str) -> list[str]:
        """Return every ticket id in *message*, deduped, in first-seen order.

        The id is the **whole match**, so the gutter shows what the commit
        actually wrote (``#1204``, not ``1204``).
        """
        seen: list[str] = []
        for match in self._regex.finditer(message):
            if match.group(0) not in seen:
                seen.append(match.group(0))
        return seen

    def url_for(self, ticket_id: str) -> str | None:
        """Render the tracker URL for *ticket_id*, or None when unconfigured.

        The template formats over the pattern's **named groups** plus ``{0}``
        for the whole match, so a URL can consume only part of the id.
        """
        if self.url is None:
            return None
        match = self._regex.fullmatch(ticket_id)
        if match is None:
            return None
        fields: dict[str, str] = {"0": match.group(0)}
        fields.update({k: v or "" for k, v in match.groupdict().items()})
        return self.url.format(**fields)


def _url_field_names(url: str) -> list[str]:
    return [name for _, name, _, _ in string.Formatter().parse(url) if name]


def build_ticket_spec(pattern: str, url: str | None) -> TicketSpec:
    """Compile *pattern* and validate *url*'s field names against it."""
    try:
        regex = re.compile(pattern)
    except re.error as exc:
        raise TicketConfigError(
            f"[coverage.tickets] pattern is not a valid regular expression: {exc}"
        ) from exc
    if url is not None:
        known = set(regex.groupindex) | {"0"}
        for name in _url_field_names(url):
            if name not in known:
                raise TicketConfigError(
                    f"[coverage.tickets] url references unknown group {name!r}; "
                    f"pattern defines {sorted(regex.groupindex)}"
                )
    return TicketSpec(pattern=pattern, url=url, _regex=regex)


def load_ticket_spec(cov_config: dict[str, Any]) -> TicketSpec | None:
    """Build a :class:`TicketSpec` from a raw ``[coverage]`` dict, or None.

    Returning None is the "feature absent" signal: no walk, no page, no
    gutter, and a report byte-identical to one built before this feature.
    """
    tickets = cov_config.get("tickets")
    if not tickets:
        return None
    pattern = tickets.get("pattern")
    if not pattern:
        raise TicketConfigError("[coverage.tickets] requires a 'pattern' key")
    return build_ticket_spec(pattern, tickets.get("url"))
```

- [ ] **Step 4: Add the pydantic spec**

In `src/otto/models/settings.py`, alongside `CoverageReportSpec`:

```python
@options
class CoverageTicketsSpec:
    """``[coverage.tickets]`` — commit-message ticket attribution."""

    pattern: str
    """Regex whose whole match is the ticket id; named groups feed ``url``."""

    url: str | None = None
    """Tracker URL template formatted over ``pattern``'s named groups plus ``{0}``."""
```

Register it on the coverage settings model exactly as `report: CoverageReportSpec | None` is registered — grep for `CoverageReportSpec` and mirror every site.

- [ ] **Step 5: Re-export from `report_config.py`**

Append to `src/otto/coverage/report_config.py`:

```python
from .tickets import TicketSpec, load_ticket_spec  # noqa: F401 — re-export for reporter
```

- [ ] **Step 6: Run tests to verify they pass**

Run: `pytest tests/unit/cov/test_tickets.py -v`
Expected: 11 passed

- [ ] **Step 7: Lint**

Run: `nox -s lint`
Expected: pass

- [ ] **Step 8: Commit**

```bash
git add src/otto/coverage/tickets.py src/otto/coverage/report_config.py \
        src/otto/models/settings.py tests/unit/cov/test_tickets.py
git commit -m "feat(cov): [coverage.tickets] config and commit-message id extraction

Assisted-by: Claude Opus 5 (1M context) <noreply@anthropic.com>"
```

---

### Task 2: Streaming `git log` walk

**Files:**
- Modify: `src/otto/coverage/capture/gitio.py`
- Test: `tests/unit/cov/test_gitio_log_walk.py` (create)

**Interfaces:**
- Consumes: Task 1 nothing; uses gitio's existing `_run_raw`.
- Produces: `CommitDiff` frozen dataclass (`sha: str`, `subject: str`, `body: str`, `diff_text: str`); `log_walk_u0(repo_root: Path, relpaths: list[str], *, first_parent: bool = True) -> list[CommitDiff]`, newest-first; and `diff_worktree_u0(repo_root: Path, relpaths: list[str]) -> str` — **one** diff for the whole file set.

**Budget:** `tests/unit/cov/test_git_spawn_budget.py` pins this module at O(1) git subprocesses, *not* O(files). Both helpers here take a path **list** for that reason — a per-file variant would reintroduce the cost profile this design exists to avoid.

**Why control characters:** the record/field delimiters must not collide with source text carried in `diff_text`. ASCII `\x1e` (record separator) and `\x1f` (unit separator) are the standard choice and effectively never appear in C/C++ sources.

- [ ] **Step 1: Write the failing tests**

```python
# tests/unit/cov/test_gitio_log_walk.py
import subprocess
from pathlib import Path

from otto.coverage.capture import gitio


def _init(tmp_path: Path) -> Path:
    repo = tmp_path / "r"
    repo.mkdir()
    for args in (
        ["init", "-q"],
        ["config", "user.email", "t@example.com"],
        ["config", "user.name", "t"],
    ):
        subprocess.run(["git", *args], cwd=repo, check=True)
    return repo


def _commit(repo: Path, message: str) -> None:
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-q", "-m", message], cwd=repo, check=True)


def test_walk_is_newest_first_and_carries_subject_and_body(tmp_path):
    repo = _init(tmp_path)
    (repo / "a.c").write_text("one\n")
    _commit(repo, "first PROJ-1")
    (repo / "a.c").write_text("one\ntwo\n")
    _commit(repo, "second PROJ-2\n\nbody line")

    walk = gitio.log_walk_u0(repo, ["a.c"])

    assert [c.subject for c in walk] == ["second PROJ-2", "first PROJ-1"]
    assert "body line" in walk[0].body
    assert walk[0].sha != walk[1].sha


def test_diff_text_is_u0_and_parseable(tmp_path):
    repo = _init(tmp_path)
    (repo / "a.c").write_text("one\n")
    _commit(repo, "c1")
    (repo / "a.c").write_text("one\ntwo\n")
    _commit(repo, "c2")

    walk = gitio.log_walk_u0(repo, ["a.c"])

    assert "@@ -1,0 +2 @@" in walk[0].diff_text or "@@ -1 +1,2 @@" in walk[0].diff_text
    assert "diff --git" in walk[0].diff_text


def test_source_containing_delimiters_does_not_split_records(tmp_path):
    """A file literally containing \\x1e must not fabricate extra commits."""
    repo = _init(tmp_path)
    (repo / "a.c").write_text("char sep = '\x1e';\nchar unit = '\x1f';\n")
    _commit(repo, "c1")

    walk = gitio.log_walk_u0(repo, ["a.c"])

    assert len(walk) == 1
    assert walk[0].subject == "c1"


def test_whitespace_only_change_produces_no_hunks(tmp_path):
    """-w is what keeps a reindent from re-attributing a line."""
    repo = _init(tmp_path)
    (repo / "a.c").write_text("int x;\n")
    _commit(repo, "c1")
    (repo / "a.c").write_text("    int x;\n")
    _commit(repo, "c2")

    walk = gitio.log_walk_u0(repo, ["a.c"])

    assert "@@" not in walk[0].diff_text


def test_empty_relpaths_returns_empty(tmp_path):
    repo = _init(tmp_path)
    (repo / "a.c").write_text("x\n")
    _commit(repo, "c1")
    assert gitio.log_walk_u0(repo, []) == []
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/unit/cov/test_gitio_log_walk.py -v`
Expected: FAIL — `AttributeError: module 'otto.coverage.capture.gitio' has no attribute 'log_walk_u0'`

- [ ] **Step 3: Implement the walk**

Append to `src/otto/coverage/capture/gitio.py`:

```python
_REC_SEP = "\x1e"
"""ASCII record separator — delimits commits in the log stream.

A source line could contain anything, so the delimiter must be a byte that
effectively never occurs in text; ``\\x1e``/``\\x1f`` are the standard
choice and are what keeps a file full of quotes and braces from
fabricating commit boundaries.
"""
_FIELD_SEP = "\x1f"


@dataclass(frozen=True)
class CommitDiff:
    """One commit's metadata plus its ``-U0`` diff against its first parent."""

    sha: str
    subject: str
    body: str
    diff_text: str


def log_walk_u0(
    repo_root: Path, relpaths: list[str], *, first_parent: bool = True
) -> list[CommitDiff]:
    """Stream ``git log -p -U0 -w -M`` over *relpaths*, newest commit first.

    One subprocess for the whole file set. ``-w`` mirrors the manual-validity
    contract (a reindent must not re-attribute a line) and ``-M`` follows
    renames; both match :func:`diff_worktree_file_u0`.
    """
    if not relpaths:
        return []
    args = [
        "log",
        f"--format={_REC_SEP}%H{_FIELD_SEP}%s{_FIELD_SEP}%b{_FIELD_SEP}",
        "-p",
        "-U0",
        "-w",
        "-M",
    ]
    if first_parent:
        args.append("--first-parent")
    args += ["--", *relpaths]

    raw = _run(args, repo_root)
    out: list[CommitDiff] = []
    for record in raw.split(_REC_SEP):
        if not record.strip():
            continue
        parts = record.split(_FIELD_SEP, 3)
        if len(parts) < 4:
            continue
        sha, subject, body, diff_text = parts
        out.append(
            CommitDiff(
                sha=sha.strip(), subject=subject, body=body, diff_text=diff_text
            )
        )
    return out


def diff_worktree_u0(repo_root: Path, relpaths: list[str]) -> str:
    """One ``git diff -w -U0 HEAD`` covering *relpaths* — not one per file.

    The per-file sibling :func:`diff_worktree_file_u0` is right for the
    validity pass, which resolves one capture at a time; attribution covers
    the whole store at once and is budgeted at O(1) subprocesses.
    """
    if not relpaths:
        return ""
    return _run(["diff", "-w", "-U0", "HEAD", "--", *relpaths], repo_root)
```

`dataclass` and `Path` are already imported in `gitio.py`; verify before adding duplicates.

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/unit/cov/test_gitio_log_walk.py -v`
Expected: 5 passed

- [ ] **Step 5: Lint**

Run: `nox -s lint`

- [ ] **Step 6: Commit**

```bash
git add src/otto/coverage/capture/gitio.py tests/unit/cov/test_gitio_log_walk.py
git commit -m "feat(cov): single-process git log walk for line attribution

Assisted-by: Claude Opus 5 (1M context) <noreply@anthropic.com>"
```

---

### Task 3: Commit-diff parser that keeps file creations

**Files:**
- Modify: `src/otto/coverage/capture/treediff.py`
- Create: `src/otto/coverage/attribution.py`
- Test: `tests/unit/cov/test_attribution.py`

**Interfaces:**
- Consumes: `Hunk`, `parse_u0_hunks` (`capture/remap.py`); `unquote`, `strip_side` (`capture/treediff.py`, promoted here).
- Produces: `CommitFileDiff` frozen dataclass (`new_path: str`, `old_path: str | None`, `hunks: list[Hunk]`) and `parse_commit_diff(diff_text: str) -> dict[str, CommitFileDiff]` keyed by **new path**.

**Why a second parser:** `parse_multifile_u0` is keyed by *old* path and **drops pure additions** (`treediff.py:74-75`, "no capture is anchored in a file that did not exist at base") — correct for validity, fatal here. A commit that creates a file must claim every line it added; dropping it would leave those lines unattributed and silently bucket them as `(no ticket)`. Attribution also walks backward from HEAD, so it needs new-path keys.

- [ ] **Step 1: Promote the two shared helpers**

In `src/otto/coverage/capture/treediff.py`, rename `_unquote` → `unquote` and `_strip_side` → `strip_side` (both bodies unchanged), and update their call sites inside that file. Importing an underscore-prefixed name across modules is the alternative, and it is worse.

Run: `grep -rn "_unquote\|_strip_side" src/ tests/` and update every hit.

- [ ] **Step 2: Write the failing tests**

```python
# tests/unit/cov/test_attribution.py
from otto.coverage.attribution import parse_commit_diff

_MODIFY = """diff --git a/src/a.c b/src/a.c
--- a/src/a.c
+++ b/src/a.c
@@ -3 +3 @@
-old
+new
"""

_CREATE = """diff --git a/src/new.c b/src/new.c
new file mode 100644
--- /dev/null
+++ b/src/new.c
@@ -0,0 +1,3 @@
+a
+b
+c
"""

_RENAME = """diff --git a/src/old.c b/src/new.c
similarity index 98%
rename from src/old.c
rename to src/new.c
--- a/src/old.c
+++ b/src/new.c
@@ -7 +7 @@
-x
+y
"""

_DELETE = """diff --git a/src/gone.c b/src/gone.c
deleted file mode 100644
--- a/src/gone.c
+++ /dev/null
@@ -1,2 +0,0 @@
-a
-b
"""


def test_modification_keyed_by_new_path_with_hunks():
    parsed = parse_commit_diff(_MODIFY)
    assert set(parsed) == {"src/a.c"}
    fd = parsed["src/a.c"]
    assert fd.old_path == "src/a.c"
    assert [h.new_start for h in fd.hunks] == [3]


def test_file_creation_is_kept_with_old_path_none():
    """The regression parse_multifile_u0 would cause: a created file dropped."""
    parsed = parse_commit_diff(_CREATE)
    assert set(parsed) == {"src/new.c"}
    assert parsed["src/new.c"].old_path is None


def test_rename_records_both_sides():
    parsed = parse_commit_diff(_RENAME)
    assert set(parsed) == {"src/new.c"}
    assert parsed["src/new.c"].old_path == "src/old.c"


def test_deletion_is_dropped():
    """A file deleted by this commit has no new-side lines to attribute."""
    assert parse_commit_diff(_DELETE) == {}


def test_multiple_files_in_one_commit():
    parsed = parse_commit_diff(_MODIFY + _CREATE)
    assert set(parsed) == {"src/a.c", "src/new.c"}


def test_empty_diff_is_empty():
    assert parse_commit_diff("") == {}
```

- [ ] **Step 3: Run tests to verify they fail**

Run: `pytest tests/unit/cov/test_attribution.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'otto.coverage.attribution'`

- [ ] **Step 4: Implement the parser**

```python
# src/otto/coverage/attribution.py
"""Per-line ticket attribution: one log walk, replayed backward from HEAD.

This is ``git blame``'s algorithm with blame's process model removed — see
the design spec §3. Blame spawns one process per file and re-opens the pack
indexes each time (~267 filesystem operations per file, which dominates on
NFS-backed checkouts); a single ``git log`` walk costs ~9.5 and scales
sublinearly.
"""

from dataclasses import dataclass, field

from .capture.remap import Hunk, parse_u0_hunks
from .capture.treediff import strip_side, unquote


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
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `pytest tests/unit/cov/test_attribution.py -v`
Expected: 6 passed

- [ ] **Step 6: Confirm the promotion broke nothing**

Run: `pytest tests/unit/cov -v`
Expected: all pass (the `unquote`/`strip_side` rename is internal).

- [ ] **Step 7: Lint and commit**

```bash
nox -s lint
git add src/otto/coverage/attribution.py src/otto/coverage/capture/treediff.py \
        tests/unit/cov/test_attribution.py
git commit -m "feat(cov): commit-diff parser keyed by new path, keeping file creations

Assisted-by: Claude Opus 5 (1M context) <noreply@anthropic.com>"
```

---

### Task 4: Backward replay and worktree overlay

**Files:**
- Modify: `src/otto/coverage/attribution.py`
- Test: `tests/unit/cov/test_attribution.py` (extend)

**Interfaces:**
- Consumes: `CommitDiff`/`log_walk_u0` (Task 2), `parse_commit_diff` (Task 3), `TicketSpec` (Task 1), `LineRemapper` + `diff_worktree_file_u0` (existing).
- Produces: `UNCOMMITTED: str = ""` sentinel; `attribute_lines(repo_root: Path, line_counts: dict[str, int], *, first_parent: bool = True, walk: list[gitio.CommitDiff] | None = None) -> dict[str, dict[int, str]]` mapping relpath → {HEAD line number → commit sha}; `attribute_tickets(repo_root, line_counts, spec, *, first_parent=True) -> tuple[dict[str, dict[int, list[str]]], dict[str, list[str]]]` returning (relpath → line → ticket ids, ticket id → commit shas).

`walk` exists so `attribute_tickets` can fetch the log **once** and share it across both passes; calling `log_walk_u0` twice would double this module's subprocess count for no benefit.

**The algorithm.** The frontier starts as every line of every file, in HEAD coordinates. Walking newest→oldest, a frontier line whose `new_to_old()` is `None` sits inside that commit's changed hunk — so that commit last touched it, and it leaves the frontier. Survivors are rewritten into the parent's coordinates and the walk continues. `LineRemapper.new_to_old` already returns exactly this `None`; no new remap logic is written.

- [ ] **Step 1: Write the failing tests**

```python
# append to tests/unit/cov/test_attribution.py
import subprocess
from pathlib import Path

import pytest

from otto.coverage.attribution import UNCOMMITTED, attribute_lines, attribute_tickets
from otto.coverage.tickets import build_ticket_spec


def _repo(tmp_path: Path) -> Path:
    repo = tmp_path / "r"
    repo.mkdir()
    for args in (
        ["init", "-q"],
        ["config", "user.email", "t@example.com"],
        ["config", "user.name", "t"],
    ):
        subprocess.run(["git", *args], cwd=repo, check=True)
    return repo


def _commit(repo: Path, message: str) -> str:
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-q", "-m", message], cwd=repo, check=True)
    return subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=repo, capture_output=True, text=True, check=True
    ).stdout.strip()


def test_each_line_attributed_to_the_commit_that_last_touched_it(tmp_path):
    repo = _repo(tmp_path)
    (repo / "a.c").write_text("one\ntwo\n")
    first = _commit(repo, "c1")
    (repo / "a.c").write_text("one\nCHANGED\n")
    second = _commit(repo, "c2")

    got = attribute_lines(repo, {"a.c": 2})

    assert got["a.c"] == {1: first, 2: second}


def test_created_file_attributes_all_lines_to_its_creating_commit(tmp_path):
    repo = _repo(tmp_path)
    (repo / "seed.c").write_text("s\n")
    _commit(repo, "seed")
    (repo / "new.c").write_text("a\nb\nc\n")
    created = _commit(repo, "add new.c")

    got = attribute_lines(repo, {"new.c": 3})

    assert got["new.c"] == {1: created, 2: created, 3: created}


def test_whitespace_only_edit_does_not_reattribute(tmp_path):
    repo = _repo(tmp_path)
    (repo / "a.c").write_text("int x;\n")
    first = _commit(repo, "c1")
    (repo / "a.c").write_text("    int x;\n")
    _commit(repo, "reindent")

    got = attribute_lines(repo, {"a.c": 1})

    assert got["a.c"] == {1: first}


def test_rename_is_followed_across_M(tmp_path):
    repo = _repo(tmp_path)
    (repo / "old.c").write_text("".join(f"line{i}\n" for i in range(20)))
    first = _commit(repo, "c1")
    subprocess.run(["git", "mv", "old.c", "new.c"], cwd=repo, check=True)
    _commit(repo, "move it")

    got = attribute_lines(repo, {"new.c": 20})

    assert got["new.c"][1] == first


def test_uncommitted_edit_gets_the_sentinel_not_the_previous_committer(tmp_path):
    repo = _repo(tmp_path)
    (repo / "a.c").write_text("one\ntwo\n")
    first = _commit(repo, "c1")
    (repo / "a.c").write_text("one\nDIRTY\n")

    got = attribute_lines(repo, {"a.c": 2})

    assert got["a.c"] == {1: first, 2: UNCOMMITTED}


def test_tickets_map_lines_and_collect_commits(tmp_path):
    repo = _repo(tmp_path)
    (repo / "a.c").write_text("one\ntwo\n")
    _commit(repo, "seed PROJ-1")
    (repo / "a.c").write_text("one\nCHANGED\n")
    sha = _commit(repo, "fix PROJ-2 and PROJ-3")

    spec = build_ticket_spec(r"[A-Z]{2,10}-[0-9]+", None)
    lines, commits = attribute_tickets(repo, {"a.c": 2}, spec)

    assert lines["a.c"][1] == ["PROJ-1"]
    assert lines["a.c"][2] == ["PROJ-2", "PROJ-3"]
    assert commits["PROJ-2"] == [sha]


def test_commit_with_no_ticket_yields_empty_id_list(tmp_path):
    repo = _repo(tmp_path)
    (repo / "a.c").write_text("one\n")
    _commit(repo, "chore: bump")

    spec = build_ticket_spec(r"[A-Z]{2,10}-[0-9]+", None)
    lines, _ = attribute_tickets(repo, {"a.c": 1}, spec)

    assert lines["a.c"][1] == []


def test_untracked_file_is_entirely_uncommitted(tmp_path):
    """git knows nothing about it, so no commit can own its lines."""
    repo = _repo(tmp_path)
    (repo / "a.c").write_text("one\n")
    _commit(repo, "c1")
    (repo / "new.c").write_text("x\ny\n")  # never added

    got = attribute_lines(repo, {"a.c": 1, "new.c": 2})

    assert got["new.c"] == {1: UNCOMMITTED, 2: UNCOMMITTED}


def test_git_subprocess_count_is_constant_in_file_count(tmp_path, monkeypatch):
    """O(1) subprocesses, not O(files) — the guard `test_git_spawn_budget.py`
    already enforces for the validity pass. A per-file worktree diff or a
    second log walk would reintroduce exactly the cost profile this design
    rejected `git blame` for."""
    repo = _repo(tmp_path)
    for n in range(25):
        (repo / f"f{n}.c").write_text(f"line {n}\n")
    _commit(repo, "seed PROJ-1")

    calls: list[list[str]] = []
    real = gitio._run_raw
    monkeypatch.setattr(
        gitio, "_run_raw", lambda args, cwd, **kw: (calls.append(args), real(args, cwd, **kw))[1]
    )

    spec = build_ticket_spec(r"[A-Z]{2,10}-[0-9]+", None)
    attribute_tickets(repo, {f"f{n}.c": 1 for n in range(25)}, spec)

    assert len(calls) <= 2, f"expected <=2 git spawns for 25 files, got {len(calls)}: {calls}"
```

Add `from otto.coverage.capture import gitio` to this test module's imports.

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/unit/cov/test_attribution.py -v -k "attributed or created or whitespace or rename or uncommitted or tickets or no_ticket"`
Expected: FAIL — `ImportError: cannot import name 'attribute_lines'`

- [ ] **Step 3: Implement replay and overlay**

Append to `src/otto/coverage/attribution.py`:

```python
from pathlib import Path

from .capture import gitio
from .capture.remap import LineRemapper
from .tickets import TicketSpec

UNCOMMITTED = ""
"""Sentinel sha for working-tree lines that were never committed."""


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
        walk = gitio.log_walk_u0(repo_root, list(line_counts), first_parent=first_parent)

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


def attribute_tickets(
    repo_root: Path,
    line_counts: dict[str, int],
    spec: TicketSpec,
    *,
    first_parent: bool = True,
) -> tuple[dict[str, dict[int, list[str]]], dict[str, list[str]]]:
    """Attribute lines, then resolve each commit to its ticket ids.

    Returns ``(relpath -> line -> ticket ids, ticket id -> commit shas)``.
    A line whose commit names no ticket maps to an empty list.
    """
    # One walk, shared by both passes — a second call would double this
    # module's git subprocess count for no benefit.
    walk = gitio.log_walk_u0(repo_root, list(line_counts), first_parent=first_parent)
    by_sha = attribute_lines(repo_root, line_counts, first_parent=first_parent, walk=walk)
    tickets_of = {c.sha: spec.extract(f"{c.subject}\n{c.body}") for c in walk}

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
    return lines, commits
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/unit/cov/test_attribution.py -v`
Expected: 13 passed

- [ ] **Step 5: Lint and commit**

```bash
nox -s lint
git add src/otto/coverage/attribution.py tests/unit/cov/test_attribution.py
git commit -m "feat(cov): backward hunk replay attributing lines to commits and tickets

Assisted-by: Claude Opus 5 (1M context) <noreply@anthropic.com>"
```

---

### Task 5: `git blame` oracle test

**Files:**
- Create: `tests/integration/cov/test_attribution_oracle.py`

**Interfaces:**
- Consumes: `attribute_lines` (Task 4), `RepoTimeline` (`tests/_fixtures/_repo_timeline.py`).
- Produces: nothing consumed downstream.

**Why:** replaying hunks is a reimplementation of blame's algorithm, so it gets blame as a standing oracle. Equality is asserted on **linear** histories only — `--first-parent` diverges from blame by design on merge histories (spec §2), so that divergence gets its own separate pin asserting the *intended* answer rather than being allowed to fail.

- [ ] **Step 1: Read the harness API**

Run: `sed -n '1,60p' tests/_fixtures/_repo_timeline.py`

Use its existing helpers for building commits. If it exposes no "commit N edits" primitive, drive `subprocess` directly in this test file — do not modify the harness.

- [ ] **Step 2: Write the oracle test**

```python
# tests/integration/cov/test_attribution_oracle.py
"""`git blame` is the oracle for the replay engine, never the engine itself."""

import subprocess
from pathlib import Path

import pytest

from otto.coverage.attribution import attribute_lines

pytestmark = pytest.mark.integration


def _blame_shas(repo: Path, relpath: str, line_count: int) -> dict[int, str]:
    out = subprocess.run(
        ["git", "blame", "-w", "-M", "--porcelain", "--", relpath],
        cwd=repo,
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    shas: dict[int, str] = {}
    for line in out.splitlines():
        parts = line.split()
        if len(parts) >= 3 and len(parts[0]) == 40 and parts[0].isalnum():
            try:
                shas[int(parts[2])] = parts[0]
            except ValueError:
                continue
    return {n: shas[n] for n in range(1, line_count + 1) if n in shas}


def _repo(tmp_path: Path) -> Path:
    repo = tmp_path / "oracle"
    repo.mkdir()
    for args in (
        ["init", "-q"],
        ["config", "user.email", "t@example.com"],
        ["config", "user.name", "t"],
    ):
        subprocess.run(["git", *args], cwd=repo, check=True)
    return repo


def _commit(repo: Path, message: str) -> None:
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-q", "-m", message], cwd=repo, check=True)


def test_replay_matches_blame_on_a_linear_history(tmp_path):
    repo = _repo(tmp_path)
    body = [f"line {i}\n" for i in range(30)]
    (repo / "a.c").write_text("".join(body))
    _commit(repo, "seed")

    for step, idx in enumerate((3, 17, 4, 28, 11)):
        body[idx] = f"edited at step {step}\n"
        (repo / "a.c").write_text("".join(body))
        _commit(repo, f"edit {step}")

    body.insert(10, "inserted\n")
    (repo / "a.c").write_text("".join(body))
    _commit(repo, "insert")

    count = len(body)
    assert attribute_lines(repo, {"a.c": count})["a.c"] == _blame_shas(repo, "a.c", count)


def test_replay_matches_blame_across_a_rename(tmp_path):
    repo = _repo(tmp_path)
    body = [f"line {i}\n" for i in range(25)]
    (repo / "old.c").write_text("".join(body))
    _commit(repo, "seed")
    subprocess.run(["git", "mv", "old.c", "new.c"], cwd=repo, check=True)
    _commit(repo, "rename")
    body[5] = "post-rename edit\n"
    (repo / "new.c").write_text("".join(body))
    _commit(repo, "edit after rename")

    count = len(body)
    assert attribute_lines(repo, {"new.c": count})["new.c"] == _blame_shas(
        repo, "new.c", count
    )


def test_first_parent_attributes_to_the_merge_not_the_topic_commit(tmp_path):
    """The documented, intended divergence from blame (spec §2)."""
    repo = _repo(tmp_path)
    (repo / "a.c").write_text("base\n")
    _commit(repo, "seed")
    subprocess.run(["git", "checkout", "-q", "-b", "topic"], cwd=repo, check=True)
    (repo / "a.c").write_text("base\ntopic line\n")
    _commit(repo, "wip")
    subprocess.run(["git", "checkout", "-q", "master"], cwd=repo, check=False)
    subprocess.run(["git", "checkout", "-q", "main"], cwd=repo, check=False)
    subprocess.run(
        ["git", "merge", "--no-ff", "-q", "-m", "Merge PR PROJ-9", "topic"],
        cwd=repo,
        check=True,
    )

    merge_sha = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=repo, capture_output=True, text=True, check=True
    ).stdout.strip()

    got = attribute_lines(repo, {"a.c": 2})

    assert got["a.c"][2] == merge_sha
    assert got["a.c"][2] != _blame_shas(repo, "a.c", 2)[2]
```

- [ ] **Step 3: Run the oracle**

Run: `pytest tests/integration/cov/test_attribution_oracle.py -v`
Expected: 3 passed. If the linear cases fail, the replay engine is wrong — fix Task 4, never the oracle.

- [ ] **Step 4: Commit**

```bash
git add tests/integration/cov/test_attribution_oracle.py
git commit -m "test(cov): pin replay attribution against git blame as oracle

Assisted-by: Claude Opus 5 (1M context) <noreply@anthropic.com>"
```

---

### Task 6: Store v5

**Files:**
- Modify: `src/otto/coverage/store/model.py`
- Test: `tests/unit/cov/test_model.py` (extend)

**Interfaces:**
- Consumes: nothing.
- Produces: `STORE_FORMAT_VERSION = 5`; `LineRecord.ticket: list[str]` (default `[]`); `TicketRecord` dataclass (`id: str`, `url: str | None`, `commits: list[str]`); `CoverageStore.tickets: dict[str, TicketRecord]`, serialized under the top-level `"tickets"` key.

- [ ] **Step 1: Write the failing tests**

```python
# append to tests/unit/cov/test_model.py
import json

import pytest

from otto.coverage.store.model import (
    STORE_FORMAT_VERSION,
    CoverageStore,
    LineRecord,
    TicketRecord,
)


def test_format_is_five():
    assert STORE_FORMAT_VERSION == 5


def test_line_ticket_defaults_to_empty_list():
    assert LineRecord(line_number=1).ticket == []


def test_empty_ticket_list_is_omitted_from_json(tmp_path):
    """The v4 `is not None` guard serialized a meaningless empty string."""
    store = CoverageStore(tier_order=["unit"])
    rec = store.get_or_create_file(tmp_path / "a.c")
    rec.get_or_create_line(1)
    path = tmp_path / "store.json"
    store.save(path)
    assert "ticket" not in json.loads(path.read_text())["files"][0]["lines"]["1"]


def test_ticket_list_round_trips(tmp_path):
    store = CoverageStore(tier_order=["unit"])
    rec = store.get_or_create_file(tmp_path / "a.c")
    rec.get_or_create_line(1).ticket = ["PROJ-1", "PROJ-2"]
    store.tickets["PROJ-1"] = TicketRecord(id="PROJ-1", url="u/1", commits=["abc"])
    path = tmp_path / "store.json"
    store.save(path)

    loaded = CoverageStore.load(path)

    assert next(loaded.files()).lines[1].ticket == ["PROJ-1", "PROJ-2"]
    assert loaded.tickets["PROJ-1"].url == "u/1"
    assert loaded.tickets["PROJ-1"].commits == ["abc"]


def test_merge_unions_tickets_without_duplicates():
    a = LineRecord(line_number=1, ticket=["PROJ-1"])
    b = LineRecord(line_number=1, ticket=["PROJ-2", "PROJ-1"])
    a.merge(b)
    assert a.ticket == ["PROJ-1", "PROJ-2"]


def test_v4_store_is_rejected_loud(tmp_path):
    path = tmp_path / "store.json"
    path.write_text(json.dumps({"format": 4, "files": [], "runs": []}))
    with pytest.raises(ValueError, match="v5"):
        CoverageStore.load(path)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/unit/cov/test_model.py -v -k "five or ticket or v4"`
Expected: FAIL — `assert 4 == 5`

- [ ] **Step 3: Implement v5**

In `src/otto/coverage/store/model.py`:

1. `STORE_FORMAT_VERSION = 5`, and extend its docstring: *"Version 5 turns the reserved per-line ``ticket`` slot into a list (a commit may name several tickets) and adds a top-level ``tickets`` table."*
2. Add the record:

```python
@dataclass
class TicketRecord:
    """One ticket surfaced by commit-message attribution."""

    id: str
    url: str | None = None
    commits: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-serialisable dict representation of this ticket."""
        return {"id": self.id, "url": self.url, "commits": list(self.commits)}
```

3. `LineRecord.ticket: list[str] = field(default_factory=list)` — replacing `str | None`.
4. In `LineRecord.merge`, replace the first-set-wins block with a union:

```python
        for ticket_id in other.ticket:
            if ticket_id not in self.ticket:
                self.ticket.append(ticket_id)
```

5. In `FileRecord._line_to_dict`, replace the `is not None` guard with `if rec.ticket:`.
6. In `CoverageStore.__init__`, add `self.tickets: dict[str, TicketRecord] = {}`.
7. In `save()`, add `"tickets": {k: v.to_dict() for k, v in self.tickets.items()}`.
8. In `load()`, restore both:

```python
        for tid, td in (data.get("tickets") or {}).items():
            store.tickets[tid] = TicketRecord(
                id=td["id"], url=td.get("url"), commits=list(td.get("commits") or [])
            )
```

and `ticket=list(ld.get("ticket") or [])` in the `LineRecord(...)` construction.

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/unit/cov -v`
Expected: all pass. Fixture stores pinned at v4 must be regenerated — grep `tests/` for `"format": 4` and bump.

- [ ] **Step 5: Lint and commit**

```bash
nox -s lint
git add src/otto/coverage/store/model.py tests/
git commit -m "feat(cov)!: store v5 — per-line ticket list and ticket table

Assisted-by: Claude Opus 5 (1M context) <noreply@anthropic.com>"
```

---

### Task 7: Reporter wiring

**Files:**
- Modify: `src/otto/coverage/reporter.py`
- Modify: `src/otto/cli/cov.py` (pass the spec through)
- Test: `tests/unit/cov/test_cov.py` (extend)

**Interfaces:**
- Consumes: `load_ticket_spec` (Task 1), `attribute_tickets` (Task 4), `TicketRecord` (Task 6).
- Produces: `CoverageReporter` accepts `ticket_spec: TicketSpec | None = None`; when set, populates `LineRecord.ticket` and `store.tickets` before rendering.

- [ ] **Step 1: Write the failing tests**

Rather than reconstructing the reporter's full fixture, test `_annotate_tickets` directly against a real repo — it is the only new behavior, and it keeps the test independent of how the reporter is assembled:

```python
# append to tests/unit/cov/test_cov.py
import subprocess

from otto.coverage.store.model import CoverageStore
from otto.coverage.tickets import build_ticket_spec


def _repo_with_source(tmp_path):
    repo = tmp_path / "sut"
    repo.mkdir()
    for args in (
        ["init", "-q"],
        ["config", "user.email", "t@example.com"],
        ["config", "user.name", "t"],
    ):
        subprocess.run(["git", *args], cwd=repo, check=True)
    (repo / "a.c").write_text("one\ntwo\n")
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "seed PROJ-7"], cwd=repo, check=True)
    return repo


def _store_for(repo):
    store = CoverageStore(tier_order=["unit"])
    record = store.get_or_create_file(repo / "a.c")
    record.get_or_create_line(1)
    record.get_or_create_line(2)
    return store


def test_reporter_annotates_lines_with_tickets(tmp_path):
    repo = _repo_with_source(tmp_path)
    store = _store_for(repo)
    reporter = _reporter_with(ticket_spec=build_ticket_spec(r"[A-Z]{2,10}-[0-9]+", None))

    reporter._annotate_tickets(store, repo)

    assert next(store.files()).lines[1].ticket == ["PROJ-7"]
    assert store.tickets["PROJ-7"].commits


def test_reporter_without_spec_leaves_tickets_empty(tmp_path):
    repo = _repo_with_source(tmp_path)
    store = _store_for(repo)
    reporter = _reporter_with(ticket_spec=None)

    reporter._annotate_tickets(store, repo)

    assert all(line.ticket == [] for line in next(store.files()).lines.values())
    assert store.tickets == {}
```

Add a `_reporter_with(**kwargs)` helper to this module that builds a `CoverageReporter` with the minimum arguments its constructor requires plus the given overrides — copy the argument list from the nearest existing `CoverageReporter(` construction in this file.

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/unit/cov/test_cov.py -v -k ticket`
Expected: FAIL — `TypeError: unexpected keyword argument 'ticket_spec'`

- [ ] **Step 3: Implement the wiring**

In `reporter.py`, add `ticket_spec` to the reporter's constructor/config dataclass beside `thresholds`, then after the store is fully merged and before rendering:

```python
    def _annotate_tickets(self, store: CoverageStore, repo_root: Path) -> None:
        """Attribute every stored line to the ticket(s) that last touched it."""
        if self.ticket_spec is None:
            return
        line_counts = {
            path.relative_to(repo_root).as_posix(): max(rec.lines)
            for rec in store.files()
            for path in [rec.path]
            if rec.lines and rec.path.is_relative_to(repo_root)
        }
        if not line_counts:
            return
        per_line, commits = attribute_tickets(repo_root, line_counts, self.ticket_spec)
        for rec in store.files():
            if not rec.path.is_relative_to(repo_root):
                continue
            for lineno, ids in per_line.get(rec.path.relative_to(repo_root).as_posix(), {}).items():
                if ids and lineno in rec.lines:
                    rec.lines[lineno].ticket = list(ids)
        for ticket_id, shas in commits.items():
            store.tickets[ticket_id] = TicketRecord(
                id=ticket_id, url=self.ticket_spec.url_for(ticket_id), commits=shas
            )
```

Call `self._annotate_tickets(store, repo_root)` from `run()` immediately before the render step. Import `attribute_tickets` **inside the method**, matching how `SpaRenderer` is deferred for the import budget.

In `cli/cov.py`, load the spec next to `load_report_thresholds(cov_config)` and pass it through.

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/unit/cov -v`

- [ ] **Step 5: Lint and commit**

```bash
nox -s lint
git add src/otto/coverage/reporter.py src/otto/cli/cov.py tests/unit/cov/test_cov.py
git commit -m "feat(cov): run ticket attribution during report generation

Assisted-by: Claude Opus 5 (1M context) <noreply@anthropic.com>"
```

---

### Task 8: `tickets.json` export

**Files:**
- Create: `src/otto/coverage/ticket_export.py`
- Modify: `src/otto/cli/cov.py`, `src/otto/cli/test.py`
- Test: `tests/unit/cov/test_ticket_export.py`

**Interfaces:**
- Consumes: `CoverageStore` (Task 6).
- Produces: `TICKET_EXPORT_FORMAT = 1`; `build_ticket_export(store, *, project, otto_version, generated) -> dict[str, Any]`; `write_ticket_export(store, path, *, project, otto_version, generated) -> None`; `group_ranges(lines: list[int]) -> list[list[int]]`.

**This is otto's first public export**, so it versions independently of the store and must be deterministic — a machine-readable file that reorders between runs is useless in CI diffs.

- [ ] **Step 1: Write the failing tests**

```python
# tests/unit/cov/test_ticket_export.py
import json

import pytest

from otto.coverage.store.model import CoverageStore, TicketRecord
from otto.coverage.ticket_export import (
    TICKET_EXPORT_FORMAT,
    build_ticket_export,
    group_ranges,
    write_ticket_export,
)


def _store(tmp_path):
    store = CoverageStore(tier_order=["unit"])
    rec = store.get_or_create_file(tmp_path / "a.c")
    for n in (1, 2, 3, 4):
        line = rec.get_or_create_line(n)
        line.ticket = ["PROJ-1"]
    rec.lines[1].hits.add("unit", 1)
    rec.lines[4].hits.add("unit", 1)
    store.tickets["PROJ-1"] = TicketRecord(id="PROJ-1", url="u/1", commits=["abc"])
    return store


def test_group_ranges_collapses_runs_and_keeps_singletons():
    assert group_ranges([142, 143, 144, 204, 219, 220]) == [[142, 144], [204, 204], [219, 220]]


def test_group_ranges_empty():
    assert group_ranges([]) == []


def test_export_has_its_own_format_version(tmp_path):
    payload = build_ticket_export(
        _store(tmp_path), project="p", otto_version="0.8.0", generated="2026-07-26T00:00:00Z"
    )
    assert payload["format"] == TICKET_EXPORT_FORMAT == 1


def test_export_counts_and_missing_ranges(tmp_path):
    payload = build_ticket_export(
        _store(tmp_path), project="p", otto_version="0.8.0", generated="2026-07-26T00:00:00Z"
    )
    ticket = payload["tickets"][0]
    assert ticket["id"] == "PROJ-1"
    assert ticket["url"] == "u/1"
    assert ticket["lines"] == {"owned": 4, "covered": 2, "uncovered": 2}
    assert ticket["files"][0]["missing"] == [[2, 3]]


def test_export_is_byte_deterministic(tmp_path):
    """Ordering regressions are invisible to field-by-field assertions."""
    store = _store(tmp_path)
    a, b = tmp_path / "a.json", tmp_path / "b.json"
    for path in (a, b):
        write_ticket_export(
            store, path, project="p", otto_version="0.8.0", generated="2026-07-26T00:00:00Z"
        )
    assert a.read_bytes() == b.read_bytes()


def test_export_without_tickets_fails_loud(tmp_path):
    """An empty file would read as 'no uncovered ticket work'."""
    with pytest.raises(ValueError, match=r"\[coverage.tickets\]"):
        build_ticket_export(
            CoverageStore(tier_order=["unit"]),
            project="p",
            otto_version="0.8.0",
            generated="2026-07-26T00:00:00Z",
        )
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/unit/cov/test_ticket_export.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'otto.coverage.ticket_export'`

- [ ] **Step 3: Implement the export**

```python
# src/otto/coverage/ticket_export.py
"""``tickets.json`` — otto's first public coverage export.

Versioned independently of ``store.json``: the store may be reshaped freely
for the renderer's benefit, while this file has consumers otto does not
control. Output is deterministic so it diffs cleanly in CI.
"""

import json
from pathlib import Path
from typing import Any

from .store.model import CoverageStore

TICKET_EXPORT_FORMAT = 1
"""``tickets.json`` schema version. Independent of ``STORE_FORMAT_VERSION``."""


def group_ranges(lines: list[int]) -> list[list[int]]:
    """Collapse sorted line numbers into inclusive ``[start, end]`` ranges."""
    out: list[list[int]] = []
    for line in sorted(lines):
        if out and line == out[-1][1] + 1:
            out[-1][1] = line
        else:
            out.append([line, line])
    return out


def build_ticket_export(
    store: CoverageStore, *, project: str, otto_version: str, generated: str
) -> dict[str, Any]:
    """Build the ``tickets.json`` payload from an attributed store."""
    if not store.tickets:
        raise ValueError(
            "no ticket data in this report — [coverage.tickets] must be configured "
            "for --tickets-json"
        )
    per_ticket: dict[str, dict[str, list[int]]] = {}
    covered_of: dict[str, dict[str, list[int]]] = {}
    # Per-tier counts accumulate in this same pass. Recomputing them inside
    # the per-ticket loop below would re-walk every line once per ticket per
    # tier — O(tickets x tiers x lines) on a store with hundreds of tickets.
    per_tier_of: dict[str, dict[str, int]] = {}
    for rec in store.files():
        display = rec.path.as_posix()
        for lineno, line in rec.lines.items():
            for ticket_id in line.ticket:
                per_ticket.setdefault(ticket_id, {}).setdefault(display, []).append(lineno)
                if line.hits.is_hit():
                    covered_of.setdefault(ticket_id, {}).setdefault(display, []).append(lineno)
                tiers = per_tier_of.setdefault(ticket_id, {})
                for tier in store.tier_order:
                    if line.hits.is_hit(tier):
                        tiers[tier] = tiers.get(tier, 0) + 1

    tickets: list[dict[str, Any]] = []
    total_owned = total_covered = 0
    for ticket_id in sorted(per_ticket):
        files: list[dict[str, Any]] = []
        owned = covered = 0
        for display in sorted(per_ticket[ticket_id]):
            owned_lines = sorted(per_ticket[ticket_id][display])
            hit_lines = set(covered_of.get(ticket_id, {}).get(display, []))
            missing = [n for n in owned_lines if n not in hit_lines]
            owned += len(owned_lines)
            covered += len(hit_lines)
            files.append(
                {
                    "path": display,
                    "owned": len(owned_lines),
                    "covered": len(hit_lines),
                    "missing": group_ranges(missing),
                }
            )
        total_owned += owned
        total_covered += covered
        record = store.tickets.get(ticket_id)
        per_tier = {
            tier: per_tier_of.get(ticket_id, {}).get(tier, 0) for tier in store.tier_order
        }
        tickets.append(
            {
                "id": ticket_id,
                "url": record.url if record else None,
                "commits": sorted(record.commits) if record else [],
                "lines": {"owned": owned, "covered": covered, "uncovered": owned - covered},
                "per_tier": per_tier,
                "files": files,
            }
        )

    return {
        "format": TICKET_EXPORT_FORMAT,
        "generated": generated,
        "otto_version": otto_version,
        "project": project,
        "traversal": "first-parent",
        "thresholds": store.thresholds.to_dict(),
        "tiers": list(store.tier_order),
        "totals": {
            "owned": total_owned,
            "covered": total_covered,
            "uncovered": total_owned - total_covered,
        },
        "tickets": tickets,
    }


def write_ticket_export(
    store: CoverageStore, path: Path, *, project: str, otto_version: str, generated: str
) -> None:
    """Write the ``tickets.json`` payload to *path*."""
    payload = build_ticket_export(
        store, project=project, otto_version=otto_version, generated=generated
    )
    path.write_text(json.dumps(payload, indent=2, sort_keys=False) + "\n")
```

- [ ] **Step 4: Add the CLI flags**

In `src/otto/cli/cov.py`'s `report` command, beside `--dir`:

```python
    tickets_json: Annotated[
        Path | None,
        typer.Option(
            "--tickets-json",
            help=(
                "Also write a machine-readable per-ticket coverage summary to this "
                "path. Requires [coverage.tickets]."
            ),
        ),
    ] = None,
```

Thread it to the reporter and call `write_ticket_export` after the store is built. Mirror as `--cov-tickets-json` in `src/otto/cli/test.py`, following exactly how `--cov-report-dir` is threaded. Document both in each module's docstring option list (`cli/test.py:96` shows the format).

- [ ] **Step 5: Run tests to verify they pass**

Run: `pytest tests/unit/cov/test_ticket_export.py -v`
Expected: 7 passed

- [ ] **Step 6: Lint and commit**

```bash
nox -s lint
git add src/otto/coverage/ticket_export.py src/otto/cli/cov.py src/otto/cli/test.py \
        tests/unit/cov/test_ticket_export.py
git commit -m "feat(cov): --tickets-json export with an independently versioned schema

Assisted-by: Claude Opus 5 (1M context) <noreply@anthropic.com>"
```

---

### Task 9: SPA data payloads

**Files:**
- Modify: `src/otto/coverage/renderer/spa_data.py`
- Modify: `web/src/covapp/types.ts`
- Test: `tests/unit/cov/test_spa_data.py` (confirm name via `ls tests/unit/cov`)

**Interfaces:**
- Consumes: store v5 (Task 6).
- Produces: `IndexPayload["tickets"]: list[TicketSummary]` where `TicketSummary = {id, url, owned, covered, uncovered, per_tier: dict[str, int], chunk: str}`; per-ticket chunks at `cov_data/tickets/<mangled>.js` calling `window.__OTTO_COV_TICKET__(id, payload)`; `LineJson.ticket: string[]`.

- [ ] **Step 1: Write the failing tests**

```python
# append to tests/unit/cov/test_spa_data.py
from otto.coverage.store.model import CoverageStore, TicketRecord
from otto.coverage.renderer.spa_data import build_index_payload, emit_chunks


def _ticket_store(tmp_path):
    """Two lines owned by PROJ-1, one of them hit."""
    store = CoverageStore(tier_order=["unit"])
    record = store.get_or_create_file(tmp_path / "a.c")
    first = record.get_or_create_line(1)
    first.ticket = ["PROJ-1"]
    first.hits.add("unit", 1)
    second = record.get_or_create_line(2)
    second.ticket = ["PROJ-1"]
    store.tickets["PROJ-1"] = TicketRecord(id="PROJ-1", url="u/1", commits=["abc"])
    return store


def test_index_payload_carries_ticket_summaries(tmp_path):
    payload = build_index_payload(
        _ticket_store(tmp_path), project_name="P", prefix=tmp_path, stamp="S"
    )
    assert payload["tickets"] == [
        {
            "id": "PROJ-1",
            "url": "u/1",
            "owned": 2,
            "covered": 1,
            "uncovered": 1,
            "per_tier": {"unit": 1},
            "chunk": payload["tickets"][0]["chunk"],
        }
    ]
    assert payload["tickets"][0]["chunk"]


def test_ticket_chunks_are_emitted_per_ticket(tmp_path):
    out = tmp_path / "report"
    out.mkdir()
    emit_chunks(
        _ticket_store(tmp_path),
        out,
        project_name="P",
        prefix=tmp_path,
        extra_markers=None,
        stamp="S",
    )
    chunks = sorted((out / "cov_data" / "tickets").iterdir())
    assert len(chunks) == 1
    assert chunks[0].read_text().startswith("window.__OTTO_COV_TICKET__(")


def test_no_tickets_emits_empty_list_and_no_chunk_dir(tmp_path):
    store = CoverageStore(tier_order=["unit"])
    store.get_or_create_file(tmp_path / "a.c").get_or_create_line(1)
    out = tmp_path / "report"
    out.mkdir()
    payload = build_index_payload(store, project_name="P", prefix=tmp_path, stamp="S")
    emit_chunks(
        store, out, project_name="P", prefix=tmp_path, extra_markers=None, stamp="S"
    )
    assert payload["tickets"] == []
    assert not (out / "cov_data" / "tickets").exists()


def test_line_json_carries_ticket_ids_and_omits_empty(tmp_path):
    out = tmp_path / "report"
    out.mkdir()
    emit_chunks(
        _ticket_store(tmp_path),
        out,
        project_name="P",
        prefix=tmp_path,
        extra_markers=None,
        stamp="S",
    )
    text = next((out / "cov_data" / "files").iterdir()).read_text()
    assert '"ticket": ["PROJ-1"]' in text

    plain = CoverageStore(tier_order=["unit"])
    plain.get_or_create_file(tmp_path / "b.c").get_or_create_line(1)
    out2 = tmp_path / "report2"
    out2.mkdir()
    emit_chunks(
        plain, out2, project_name="P", prefix=tmp_path, extra_markers=None, stamp="S"
    )
    assert '"ticket"' not in next((out2 / "cov_data" / "files").iterdir()).read_text()
```

If `build_index_payload`'s keyword names differ from the above, copy them verbatim from the nearest existing call in this file — they are `project_name`, `prefix`, `stamp` at time of writing.

- [ ] **Step 2: Run to verify failure**

Run: `pytest tests/unit/cov/test_spa_data.py -v -k ticket`
Expected: FAIL — `KeyError: 'tickets'`

- [ ] **Step 3: Implement the payloads**

In `spa_data.py`:

1. In `_line_to_json`, emit `"ticket": list(lr.ticket)` only when non-empty.
2. Add a ticket rollup builder mirroring `_build_run_contrib`:

```python
def _build_ticket_summaries(
    store: CoverageStore, prefix: Path | None
) -> tuple[list[dict[str, Any]], dict[str, dict[str, Any]]]:
    """Return (index summaries, per-ticket chunk payloads).

    Summaries are small and ride ``index.js``; the missing-line detail is
    deferred to a per-ticket chunk so boot cost stays constant regardless of
    how many tickets a mature repo yields.
    """
    owned: dict[str, dict[str, list[int]]] = {}
    hits: dict[str, dict[str, list[int]]] = {}
    # Accumulated in the same pass — see ticket_export.build_ticket_export
    # for why this is not recomputed per ticket.
    per_tier_of: dict[str, dict[str, int]] = {}
    for record in store.files():
        display = _display_path(record, prefix)
        for lineno, line in record.lines.items():
            for ticket_id in line.ticket:
                owned.setdefault(ticket_id, {}).setdefault(display, []).append(lineno)
                if line.hits.is_hit():
                    hits.setdefault(ticket_id, {}).setdefault(display, []).append(lineno)
                tiers = per_tier_of.setdefault(ticket_id, {})
                for tier in store.tier_order:
                    if line.hits.is_hit(tier):
                        tiers[tier] = tiers.get(tier, 0) + 1

    summaries: list[dict[str, Any]] = []
    chunks: dict[str, dict[str, Any]] = {}
    for ticket_id in sorted(owned):
        rec = store.tickets.get(ticket_id)
        total = sum(len(v) for v in owned[ticket_id].values())
        covered = sum(len(v) for v in hits.get(ticket_id, {}).values())
        chunk = mangle_path(Path(ticket_id))
        summaries.append(
            {
                "id": ticket_id,
                "url": rec.url if rec else None,
                "owned": total,
                "covered": covered,
                "uncovered": total - covered,
                "per_tier": {
                    tier: per_tier_of.get(ticket_id, {}).get(tier, 0)
                    for tier in store.tier_order
                },
                "chunk": chunk,
            }
        )
        files = []
        for display in sorted(owned[ticket_id]):
            hit_set = set(hits.get(ticket_id, {}).get(display, []))
            missing = [n for n in sorted(owned[ticket_id][display]) if n not in hit_set]
            files.append(
                {
                    "path": display,
                    "owned": len(owned[ticket_id][display]),
                    "covered": len(hit_set),
                    "missing": _group_ranges(missing),
                }
            )
        chunks[chunk] = {"id": ticket_id, "files": files}
    return summaries, chunks
```

3. Add `_group_ranges` by importing the shared implementation — `from ..ticket_export import group_ranges as _group_ranges`. **The UI and the JSON export must use one range implementation** so they cannot drift.
4. Add `"tickets": summaries` to the index payload, and in `emit_chunks` write each chunk as `window.__OTTO_COV_TICKET__({json.dumps(chunk_id)}, {json.dumps(payload)});` into `cov_data/tickets/`. Skip the directory entirely when there are no tickets.
5. In `web/src/covapp/types.ts`, add `ticket?: string[]` to `LineJson`, and:

```ts
/** One ticket's index-level rollup (`IndexPayload["tickets"]`). */
export interface TicketSummary {
  id: string;
  url: string | null;
  owned: number;
  covered: number;
  uncovered: number;
  per_tier: Record<string, number>;
  /** Chunk id — key into `cov_data/tickets/<chunk>.js`. */
  chunk: string;
}

/** One ticket's deferred detail chunk. */
export interface TicketChunk {
  id: string;
  files: { path: string; owned: number; covered: number; missing: [number, number][] }[];
}
```

and `tickets: TicketSummary[]` on `IndexPayload`.

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/unit/cov -v`

- [ ] **Step 5: Lint and commit**

```bash
nox -s lint
git add src/otto/coverage/renderer/spa_data.py web/src/covapp/types.ts tests/unit/cov/
git commit -m "feat(cov): emit ticket rollups and per-ticket chunks for the SPA

Assisted-by: Claude Opus 5 (1M context) <noreply@anthropic.com>"
```

---

### Task 10: Tickets page

**Files:**
- Create: `web/src/covapp/pages/TicketsPage.tsx`, `web/src/covapp/pages/TicketsPage.test.tsx`
- Modify: `web/src/covapp/App.tsx`, `web/src/covapp/chrome/AppShell.tsx`

**Interfaces:**
- Consumes: `TicketSummary`, `TicketChunk` (Task 9); `StatsCard` (`chrome/StatsCard.tsx`, already parameterized via `keyColumnLabel` and generic `rows`).
- Produces: route `#/tickets`.

- [ ] **Step 1: Write the failing tests**

```tsx
// web/src/covapp/pages/TicketsPage.test.tsx
import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it } from "vitest";

import { TicketsPage } from "./TicketsPage";
import { makeIndex } from "../testUtils";  // confirm the helper's real name first

const INDEX = makeIndex({
  tickets: [
    { id: "PROJ-1", url: "u/1", owned: 100, covered: 90, uncovered: 10,
      per_tier: { unit: 90 }, chunk: "PROJ-1" },
    { id: "PROJ-2", url: null, owned: 50, covered: 5, uncovered: 45,
      per_tier: { unit: 5 }, chunk: "PROJ-2" },
  ],
});

describe("TicketsPage", () => {
  it("sorts by uncovered descending so the worst-tested work is first", () => {
    render(<TicketsPage index={INDEX} />);
    const ids = screen.getAllByTestId("ticket-id").map((n) => n.textContent);
    expect(ids).toEqual(["PROJ-2", "PROJ-1"]);
  });

  it("filters rows by the search box", async () => {
    render(<TicketsPage index={INDEX} />);
    await userEvent.type(screen.getByPlaceholderText(/search tickets/i), "PROJ-1");
    expect(screen.getAllByTestId("ticket-id").map((n) => n.textContent)).toEqual(["PROJ-1"]);
  });

  it("links a ticket that has a url and leaves one without as plain text", () => {
    render(<TicketsPage index={INDEX} />);
    const rows = screen.getAllByTestId("ticket-row");
    expect(within(rows[1]).getByRole("link")).toHaveAttribute("href", "u/1");
    expect(within(rows[0]).queryByRole("link")).toBeNull();
  });

  it("states that rows overlap and do not sum to the card above", () => {
    render(<TicketsPage index={INDEX} />);
    expect(screen.getByText(/overlap/i)).toBeInTheDocument();
  });

  it("renders an empty state when no tickets are attributed", () => {
    render(<TicketsPage index={makeIndex({ tickets: [] })} />);
    expect(screen.getByText(/no ticket data/i)).toBeInTheDocument();
  });
});
```

- [ ] **Step 2: Run to verify failure**

Run: `cd web && npx vitest run src/covapp/pages/TicketsPage.test.tsx`
Expected: FAIL — cannot resolve `./TicketsPage`

- [ ] **Step 3: Implement the page**

Build `TicketsPage.tsx` with: the `StatsCard` (rows = tiers, scoped to all attributed lines) above; a search `<input>`; a sortable table whose rows carry `data-testid="ticket-row"` and whose id cell carries `data-testid="ticket-id"`; default sort `uncovered` descending; the overlap caption; and an empty state. On row expand, load `cov_data/tickets/<chunk>.js` by injecting a `<script>` tag and reading the value delivered to `window.__OTTO_COV_TICKET__` — **not** `fetch`, which does not work on `file://`. Follow how `FilePage.tsx` loads its per-file chunk and mirror it exactly.

Register the route in `App.tsx` beside `#/runs`:

```tsx
        <Route path="/tickets">
          <TicketsPage index={index} />
        </Route>
```

Add a nav entry in `AppShell.tsx` next to the existing Runs link.

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd web && npx vitest run src/covapp/pages/TicketsPage.test.tsx`
Expected: 5 passed

- [ ] **Step 5: Build and commit**

```bash
make web
nox -s lint
git add web/src/covapp/
git commit -m "feat(cov): #/tickets page with search, sorting and missing-line detail

Assisted-by: Claude Opus 5 (1M context) <noreply@anthropic.com>"
```

---

### Task 11: File-page gutter and line anchors

**Files:**
- Modify: `web/src/covapp/pages/FilePage.tsx`, `web/src/covapp/pages/FilePage.test.tsx`

**Interfaces:**
- Consumes: `LineJson.ticket` (Task 9), `parseHashQuery` (`focus.tsx`).
- Produces: `?lines=<a>-<b>` deep-link contract used by Task 10's range links.

- [ ] **Step 1: Write the failing tests**

```tsx
// append to web/src/covapp/pages/FilePage.test.tsx — uses this file's existing
// makeChunk / makeFileIndex / mockChunkLoad helpers verbatim.

function chunkWithTickets(tickets: Record<number, string[]>) {
  const chunk = makeChunk();
  for (const [lineno, ids] of Object.entries(tickets)) {
    const line = chunk.lines[Number(lineno)];
    if (line) line.ticket = ids;
  }
  return chunk;
}

it("renders a ticket chip in the gutter", async () => {
  mockChunkLoad({ resolve: chunkWithTickets({ 1: ["PROJ-1"] }) });
  render(<FilePage index={makeFileIndex()} path="a.c" />);
  expect(await screen.findByTestId("ticket-gutter-1")).toHaveTextContent("PROJ-1");
});

it("collapses multiple tickets to first plus an overflow count", async () => {
  mockChunkLoad({ resolve: chunkWithTickets({ 1: ["PROJ-1", "PROJ-2", "PROJ-3"] }) });
  render(<FilePage index={makeFileIndex()} path="a.c" />);
  const gutter = await screen.findByTestId("ticket-gutter-1");
  expect(gutter).toHaveTextContent("PROJ-1");
  expect(gutter).toHaveTextContent("+2");
});

it("renders no gutter at all when no line carries a ticket", async () => {
  mockChunkLoad({ resolve: makeChunk() });
  render(<FilePage index={makeFileIndex()} path="a.c" />);
  await screen.findByTestId("code-view");
  expect(screen.queryByTestId(/^ticket-gutter-/)).toBeNull();
});

it("highlights the range named by ?lines=", async () => {
  window.location.hash = "#/coverage/a.c?lines=2-3";
  mockChunkLoad({ resolve: makeChunk() });
  render(<FilePage index={makeFileIndex()} path="a.c" />);
  await screen.findByTestId("code-view");
  expect(screen.getByTestId("line-row-2")).toHaveAttribute("data-highlighted", "true");
  expect(screen.getByTestId("line-row-3")).toHaveAttribute("data-highlighted", "true");
  expect(screen.getByTestId("line-row-1")).not.toHaveAttribute("data-highlighted", "true");
});

it("highlights a single line for a bare ?lines=", async () => {
  window.location.hash = "#/coverage/a.c?lines=2";
  mockChunkLoad({ resolve: makeChunk() });
  render(<FilePage index={makeFileIndex()} path="a.c" />);
  await screen.findByTestId("code-view");
  expect(screen.getByTestId("line-row-2")).toHaveAttribute("data-highlighted", "true");
  expect(screen.getByTestId("line-row-1")).not.toHaveAttribute("data-highlighted", "true");
});

it("ignores a malformed ?lines= instead of throwing", async () => {
  window.location.hash = "#/coverage/a.c?lines=banana";
  mockChunkLoad({ resolve: makeChunk() });
  render(<FilePage index={makeFileIndex()} path="a.c" />);
  await screen.findByTestId("code-view");
  expect(screen.queryByTestId(/^line-row-.*/)).not.toHaveAttribute("data-highlighted", "true");
});
```

`FilePage`'s prop names and the `code-view` / `line-row-N` testids must match what the component already exposes — read the top of this test file and the component before writing, and adjust the selectors rather than inventing new ones.

- [ ] **Step 2: Run to verify failure**

Run: `cd web && npx vitest run src/covapp/pages/FilePage.test.tsx`
Expected: FAIL on the five new cases

- [ ] **Step 3: Implement**

Add a leading gutter `<td data-testid={"ticket-gutter-" + n}>` rendered only when some line in the file carries a ticket (keeping today's layout byte-identical for reports without attribution). Render `ids[0]` plus `+{ids.length - 1}` when there is more than one, linking via the summary's `url` when present.

Parse `?lines=` with `parseHashQuery()`, accepting `A-B` and bare `A`; stamp `data-highlighted="true"` on rows in range and `scrollIntoView({ block: "center" })` the first one in an effect. Guard against a malformed value (non-numeric, reversed range) by ignoring it rather than throwing.

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd web && npx vitest run src/covapp/pages/FilePage.test.tsx`

- [ ] **Step 5: Build and commit**

```bash
make web
git add web/src/covapp/pages/
git commit -m "feat(cov): ticket gutter and ?lines= deep links on the file page

Assisted-by: Claude Opus 5 (1M context) <noreply@anthropic.com>"
```

---

### Task 12: Ticket context (denominator filter)

**Files:**
- Create: `web/src/covapp/tickets.ts`, `web/src/covapp/tickets.test.ts`
- Modify: `web/src/covapp/focus.tsx`, `pages/DirectoryPage.tsx`, `pages/FilePage.tsx`, `chrome/AppShell.tsx`

**Interfaces:**
- Consumes: `TicketSummary` (Task 9), `parseHashQuery`/`setHashQuery` (`focus.tsx`).
- Produces: `?ticket=<id>` param; `scopeTreeToTicket(node: DirNode, ticketLines: Record<string, number[]>, ticketHits: Record<string, number[]>): DirNode | null` — the third argument is what makes the recomputed denominator possible.

**Diverges from run-focus deliberately** (spec §6.3): run-focus dims non-participating rows because "how much of this code did that run prove" needs the code on screen; ticket context **hides** them because "where is my work" treats everything else as noise. The file page still dims rather than hides — you cannot read code with lines removed from the middle of it.

- [ ] **Step 1: Write the failing tests**

```ts
// web/src/covapp/tickets.test.ts
import { describe, expect, it } from "vitest";

import { scopeTreeToTicket } from "./tickets";

const TREE = {
  name: "", dirs: [
    { name: "src", dirs: [], files: [
      { name: "a.c", path: "src/a.c", chunk: "a", stats: {} as never },
      { name: "b.c", path: "src/b.c", chunk: "b", stats: {} as never },
    ], stats: {} as never },
    { name: "vendor", dirs: [], files: [
      { name: "z.c", path: "vendor/z.c", chunk: "z", stats: {} as never },
    ], stats: {} as never },
  ], files: [], stats: {} as never,
} as never;

describe("scopeTreeToTicket", () => {
  it("keeps only files the ticket touched", () => {
    const scoped = scopeTreeToTicket(TREE, { "src/a.c": [1, 2] }, { "src/a.c": [1] })!;
    expect(scoped.dirs.map((d) => d.name)).toEqual(["src"]);
    expect(scoped.dirs[0].files.map((f) => f.name)).toEqual(["a.c"]);
  });

  it("drops directories left empty rather than showing hollow rows", () => {
    const scoped = scopeTreeToTicket(TREE, { "src/a.c": [1] }, {})!;
    expect(scoped.dirs.find((d) => d.name === "vendor")).toBeUndefined();
  });

  it("returns null when the ticket touched nothing in this subtree", () => {
    expect(scopeTreeToTicket(TREE, { "other/x.c": [1] }, {})).toBeNull();
  });

  it("recomputes percentages against the ticket's lines only", () => {
    // 400 coverable lines, the ticket owns 12, 6 of those are hit:
    // the answer is 6/12, never 6/400.
    const big = {
      name: "", dirs: [], files: [
        { name: "big.c", path: "big.c", chunk: "big",
          stats: { lines: { total: 400, hit: 380 }, branches: { total: 0, hit: 0 },
                   flags: { stale: 0, aging: 0, excluded: 0 }, ctx_lines: {} } },
      ], stats: {} as never,
    } as never;
    const owned = Array.from({ length: 12 }, (_, i) => i + 1);
    const scoped = scopeTreeToTicket(big, { "big.c": owned }, { "big.c": owned.slice(0, 6) })!;
    expect(scoped.files[0].stats.lines).toEqual({ total: 12, hit: 6 });
  });
});
```

- [ ] **Step 2: Run to verify failure**

Run: `cd web && npx vitest run src/covapp/tickets.test.ts`
Expected: FAIL — cannot resolve `./tickets`

- [ ] **Step 3: Implement**

Write `scopeTreeToTicket` as a pure recursive filter returning a new tree with recomputed `stats` (denominator = the ticket's lines in that file), or `null` when nothing survives. Add `ticket` to the focus context in `focus.tsx` as a second, independent param — it must **compose** with `ctx`, never clear it. Wire `DirectoryPage` to render the scoped tree plus a hidden-count row (`"{n} files hidden · 1 ticket pinned"`), and `AppShell` to show a second dismissable chip. In `FilePage`, dim lines not owned by the pinned ticket while leaving them rendered.

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd web && npx vitest run src/covapp/`

- [ ] **Step 5: Build and commit**

```bash
make web
git add web/src/covapp/
git commit -m "feat(cov): ticket context narrows the report to one ticket's lines

Assisted-by: Claude Opus 5 (1M context) <noreply@anthropic.com>"
```

---

### Task 13: Browser tests, docs, and todo cleanup

**Files:**
- Modify: the coverage browser test module (find with `grep -rln "report_browser\|covapp" tests/e2e/`)
- Modify: `docs/guide/coverage*.md`, `docs/architecture/coverage/index.md`, `docs/reference/cli*.md`
- Create: `docs/architecture/coverage/attribution.md`
- Modify: `todo/coverage_roadmap.md`, `todo/TODO.md`

- [ ] **Step 1: Add browser pins**

Add Playwright tests: `#/tickets` renders rows and the stats card; typing in the search box filters; expanding a row loads its chunk and shows missing ranges; clicking a range navigates to the file page with the span highlighted; pinning a ticket hides non-participating tree rows and shows the hidden-count row.

Run: `nox -s dashboard`
Expected: full matrix green (chromium, firefox, webkit). A bare `pytest` run here proves only chromium.

- [ ] **Step 2: Write the docs**

- Guide: a per-ticket section covering `[coverage.tickets]` config, the `--first-parent` ruling, the overlap caveat, and the NFS/fs-ops rationale for the log walk.
- **A documented `tickets.json` schema with its compatibility policy** — otto's first public export contract; state that `format` versions independently of the store.
- `docs/architecture/coverage/attribution.md`: the walk, the backward replay, why not blame.
- CLI reference: `--tickets-json`, `--cov-tickets-json`.
- Regenerate screenshots for the tickets page and for a pinned ticket context, matching how the three existing SPA pages are captured.

- [ ] **Step 3: Clean the todo files**

- **Delete** the "Git Blame Annotation" section from `todo/coverage_roadmap.md` — it documents opt-in, batching, and caching work for a correlator that never existed (spec §10).
- Delete the "Per-Ticket Coverage Breakdown" section (now shipped).
- Remove "per-ticket coverage report" from `todo/TODO.md`.

- [ ] **Step 4: Full gates**

```bash
make coverage            # full suite
nox -s typecheck         # ty runs only here
nox -s lint
nox -s dashboard         # real browser matrix
make docs                # zero warnings, clean build
```

All must pass. `make docs` must be a **clean** rebuild — incremental Sphinx misses broken `:doc:` refs in docstrings.

- [ ] **Step 5: Commit**

```bash
git add docs/ tests/ todo/
git commit -m "docs(cov): per-ticket guide, tickets.json schema, and todo cleanup

Assisted-by: Claude Opus 5 (1M context) <noreply@anthropic.com>"
```

---

## Self-Review Notes

**Spec coverage:** §2 rulings → Tasks 1, 4, 5. §3 engine → Tasks 2–4. §4 config → Task 1. §5 store/chunks → Tasks 6, 9. §6.1 tickets page → Task 10. §6.2 gutter + anchors → Task 11. §6.3 ticket context → Task 12. §7 JSON export → Task 8. §8 testing → distributed, oracle in Task 5. §9 rollout → Task 13.

**Deliberate risk callouts:**
- Task 3 exists **only** because `parse_multifile_u0` drops file creations. Reusing it would silently bucket every line of every newly-created file as `(no ticket)` — a wrong answer that still renders.
- Task 4's `attribute_tickets` walks the log twice (once for lines, once for messages). This is two subprocesses total, not two per file, so it does not compromise the fs-op budget; collapse it only if profiling says so.
- Task 9 step 3 imports `group_ranges` from `ticket_export` on purpose: the UI and the JSON export must not grow separate range implementations.
