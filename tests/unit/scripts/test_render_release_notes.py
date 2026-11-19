"""``scripts/render_release_notes.py``: one release-notes page per release.

The renderer joins git-cliff's grouping to ``release_events``' verdicts, so the
tests drive the real git-cliff over a throwaway history under ``tmp_path``
(``tests._fixtures.gitrepo.TmpGitRepo``), never the dev repo. The history
carries the checkout's own ``cliff.toml``, because the renderer reads the
config the repository it renders holds.
"""

import html
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

import pytest

from scripts import render_release_notes as rn
from scripts.lint_docs_citations import is_generated
from tests._fixtures.docs_conf import assert_conf_renders
from tests._fixtures.gitrepo import TmpGitRepo
from tests._fixtures.paths import CHECKOUT_ROOT

pytestmark = pytest.mark.interpreter_agnostic

URL = rn.repo_url()

EXAMPLES = (
    "refactor(api)!: the public interfaces are keyword-only\n\n"
    "BREAKING CHANGE: public callables take keyword arguments after their\n"
    "subjects, so a positional call past the subject raises TypeError:\n"
    '  expect(cond, "why")        -> expect(cond, msg="why")\n'
    '  Ref("pkg.mod:Name")        -> Ref(target="pkg.mod:Name")\n'
    "Records are keyword-only. The CLI follows:\n"
    "  otto host <id> load FILE NAME        -> load FILE --name NAME\n"
    "ls is unchanged for <id>.\n\n"
    "Assisted-by: Claude Opus 5.5"
)


@dataclass
class History:
    root: Path
    sha: "dict[str, str]"


def _seed(root: Path) -> TmpGitRepo:
    repo = TmpGitRepo(root, dates="2026-01-01T00:00:00Z")
    repo.write("cliff.toml", (CHECKOUT_ROOT / "cliff.toml").read_text(encoding="utf-8"))
    repo.commit("chore(release): seed the history")
    return repo


class _Writer:
    """Commits on *repo*, one day apart, so every release has its own date."""

    def __init__(self, repo: TmpGitRepo) -> None:
        self.repo = repo
        self.day = 1
        self.sha: dict[str, str] = {}

    def __call__(self, name: str, message: str) -> str:
        self.day += 1
        self.repo.dates = f"2026-01-{self.day:02d}T00:00:00Z"
        self.sha[name] = self.repo.commit(message, allow_empty=True)
        return self.sha[name]

    def tag(self, name: str) -> None:
        self.repo.git("tag", name)


@pytest.fixture(scope="module")
def history(tmp_path_factory: pytest.TempPathFactory) -> History:
    repo = _seed(tmp_path_factory.mktemp("history"))
    c = _Writer(repo)
    c("first", "feat(cli): the first command")
    c.tag("v0.1.0")
    c("a", "fix(host): a")
    c("b", "fix(gate): b")
    c("c", "feat(cli): c")
    c(
        "multi",
        "feat(api)!: drop the old loader\n\nWhy it went.\n\n"
        "BREAKING CHANGE: the loader is gone;\ncall load() instead.\n\n"
        "A second paragraph of migration advice.\n\nAssisted-by: Claude Opus 5.5",
    )
    c(
        "two",
        "fix(api)!: two breaks at once\n\n"
        "BREAKING CHANGE: first break text\nBREAKING CHANGE: second break text",
    )
    c("chore", "chore(cli)!: rename the cache directory\n\nBREAKING CHANGE: the cache moved.")
    c("maint", "chore(gate)!: maintainer break\n\nBREAKING CHANGE: the gate moved.")
    c("ci", "ci(cli)!: skipped ci break\n\nBREAKING CHANGE: the ci text.")
    c(
        "footerless",
        "feat(host)!: footerless break\n\nThe prose of the footerless body.\n\n"
        "BREAKING: run the migration first.\nAssisted-by: Claude Opus 5.5",
    )
    c("angle", "fix(host): label runs <host>/<product> and `<host>` stays (#790)")
    c("flag", "fix(host): reboot --wait returns once the host is back")
    c("examples", EXAMPLES)
    c("hidden", "chore(gate): x")
    c("target", "feat(cli): d")
    c.tag("v0.2.0")
    c(
        "corr",
        "fix(cli)!: record that d broke callers\n\n"
        f"BREAKING CHANGE: d changed its output.\nCorrects: {c.sha['target']} feat(cli): d",
    )
    c(
        "corr_hidden",
        "fix(host)!: record that x broke callers\n\n"
        f"BREAKING CHANGE: x renamed a file.\nCorrects: {c.sha['hidden']} chore(gate): x",
    )
    c.tag("v0.9.0")
    c("docs", "docs(cli): only documentation")
    c.tag("v0.10.0")
    c("unreleased", "feat(cli): unreleased work")
    return History(repo.root, c.sha)


@pytest.fixture(autouse=True)
def _floor(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(rn, "FIRST_RELEASE", "0.1.0")


def _section(page: str, heading: str) -> str:
    """The text under ``## heading`` up to the next ``## `` heading."""
    match = re.search(rf"^## {heading}\n(.*?)(?=^## |\Z)", page, re.MULTILINE | re.DOTALL)
    assert match, f"no '## {heading}' in:\n{page}"
    return match.group(1)


def _entry(page: str, sha: str) -> str:
    """The breaking entry labelled with *sha*, up to the next label or heading."""
    pattern = rf"^\(rn-{sha[:12]}\)=\n(.*?)(?=^\(rn-|^## |\Z)"
    match = re.search(pattern, page, re.MULTILINE | re.DOTALL)
    assert match, f"no entry rn-{sha[:12]} in:\n{page}"
    return match.group(1)


def test_features_and_fixes_are_listed_maintainer_ones_dropped(history: History) -> None:
    page = rn.render(history.root)["0.2.0.md"]
    fixes = _section(page, "Bug fixes")
    assert "- **host**: a" in fixes
    assert "**gate**: b" not in page
    assert "- **cli**: c" in _section(page, "New features")


def test_breaking_entry_carries_full_multi_paragraph_remediation(history: History) -> None:
    entry = _entry(rn.render(history.root)["0.2.0.md"], history.sha["multi"])
    assert "### api: drop the old loader" in entry
    assert "the loader is gone;\ncall load() instead.\n\nA second paragraph" in entry
    assert "Assisted-by" not in entry


def test_two_footers_render_in_order(history: History) -> None:
    entry = _entry(rn.render(history.root)["0.2.0.md"], history.sha["two"])
    assert entry.index("first break text") < entry.index("second break text")


def test_a_breaking_chore_is_in_the_breaking_section_only(history: History) -> None:
    page = rn.render(history.root)["0.2.0.md"]
    assert "rename the cache directory" in _section(page, "Breaking changes")
    assert "rename the cache directory" not in _section(page, "New features")
    assert "rename the cache directory" not in _section(page, "Bug fixes")


def test_a_maintainer_break_is_absent(history: History) -> None:
    page = rn.render(history.root)["0.2.0.md"]
    assert "maintainer break" not in page
    assert history.sha["maint"][:12] not in page


def test_a_skipped_ci_break_is_absent(history: History) -> None:
    pages = rn.render(history.root)
    assert not any("skipped ci break" in text for text in pages.values())


def test_footerless_break_shows_the_note_and_collapsed_body_without_trailers(
    history: History,
) -> None:
    entry = _entry(rn.render(history.root)["0.2.0.md"], history.sha["footerless"])
    assert (
        "*No upgrade instructions were recorded for this change; "
        "the commit message below describes it.*"
    ) in entry
    assert ":collapsible:" in entry
    assert "The prose of the footerless body." in entry
    # A footer-shaped note is the author's text; only trailers go.
    assert "BREAKING: run the migration first." in entry
    assert "Assisted-by" not in entry


def test_a_correction_and_its_back_reference(history: History) -> None:
    pages = rn.render(history.root)
    corr = _entry(pages["0.9.0.md"], history.sha["corr"])
    target = history.sha["target"]
    assert f"Marks [{target[:12]}]({URL}/commit/{target}) (feat(cli): d) as breaking." in corr
    assert (
        "- **cli**: d\n"
        f"  - A later release ({{ref}}`0.9.0 <rn-{history.sha['corr'][:12]}>`) "
        "marked this change as breaking.\n"
    ) in pages["0.2.0.md"]
    releases = {r.version: r for r in rn.read_releases(history.root)}
    entry = next(e for e in releases["0.2.0"].entries if e.commit.sha == target)
    assert entry.corrected_by == [f"0.9.0#rn-{history.sha['corr'][:12]}"]
    # The target page's breaking count never changes retroactively.
    assert "5 breaking changes" in pages["0.2.0.md"]


def test_a_correction_of_a_hidden_target_has_no_back_reference(history: History) -> None:
    pages = rn.render(history.root)
    hidden = history.sha["hidden"]
    assert f"Marks [{hidden[:12]}]" in _entry(pages["0.9.0.md"], history.sha["corr_hidden"])
    assert history.sha["corr_hidden"][:12] not in pages["0.2.0.md"]
    assert hidden[:12] not in pages["0.2.0.md"]


def test_angle_brackets_render_as_text(history: History) -> None:
    page = rn.render(history.root)["0.2.0.md"]
    assert "label runs &lt;host>/&lt;product> and `<host>` stays (#790)" in page
    assert "unchanged for &lt;id>." in page


def test_footer_text_is_body_text_not_a_table_cell(history: History) -> None:
    breaking = _section(rn.render(history.root)["0.2.0.md"], "Breaking changes")
    table_end = breaking.index("```\n", breaking.index("{list-table}"))
    assert breaking.index("the loader is gone;") > table_end
    assert not any("loader is gone" in line for line in breaking[:table_end].splitlines())


def test_indented_examples_become_a_code_block(history: History) -> None:
    entry = _entry(rn.render(history.root)["0.2.0.md"], history.sha["examples"])
    fences = re.findall(r"^```text\n(.*?)^```$", entry, re.MULTILINE | re.DOTALL)
    assert fences == [
        (
            'expect(cond, "why")        -> expect(cond, msg="why")\n'
            'Ref("pkg.mod:Name")        -> Ref(target="pkg.mod:Name")\n'
        ),
        "otto host <id> load FILE NAME        -> load FILE --name NAME\n",
    ]
    assert "raises TypeError:\n\n```text" in entry
    assert "```\n\nRecords are keyword-only." in entry


def test_unreleased_commits_after_the_last_tag_are_ignored(history: History) -> None:
    pages = rn.render(history.root)
    assert not any("unreleased work" in text for text in pages.values())
    assert history.sha["unreleased"][:12] not in "".join(pages.values())


def test_an_empty_release_says_no_user_visible_changes(history: History) -> None:
    page = rn.render(history.root)["0.10.0.md"]
    assert "*No user-visible changes in this release.*" in page
    assert "## " not in page


def test_a_single_empty_section_says_none(history: History) -> None:
    page = rn.render(history.root)["0.9.0.md"]
    assert _section(page, "New features").strip() == "*None in this release.*"


def test_release_order_is_newest_first_by_history(history: History) -> None:
    index = rn.render(history.root)["index.md"]
    assert index.index("0.10.0") < index.index("0.9.0") < index.index("0.2.0")
    assert [r.version for r in rn.read_releases(history.root)] == [
        "0.10.0",
        "0.9.0",
        "0.2.0",
        "0.1.0",
    ]


def test_the_index_links_the_changelog_and_names_the_first_page(history: History) -> None:
    index = rn.render(history.root)["index.md"]
    assert index.startswith("# Release notes\n")
    assert "with what to do about each break, where one was recorded." in index
    assert f"{URL}/blob/main/CHANGELOG.md" in index
    assert "0.1.0" in index


def test_every_page_carries_the_generated_header_the_docs_linters_honour(
    history: History,
) -> None:
    pages = rn.render(history.root)
    assert all(is_generated(text, ".md") for text in pages.values()), sorted(pages)


def test_a_stray_v_tag_is_refused_by_name(tmp_path: Path) -> None:
    repo = _seed(tmp_path / "stray")
    c = _Writer(repo)
    c("one", "feat(cli): one")
    c.tag("v0.1.0")
    c("two", "feat(cli): two")
    c.tag("pre-v-x")
    with pytest.raises(rn.RenderError, match="pre-v-x"):
        rn.render(repo.root)


def test_no_tagged_release_is_refused(tmp_path: Path) -> None:
    repo = _seed(tmp_path / "untagged")
    with pytest.raises(rn.RenderError, match="no tagged release"):
        rn.render(repo.root)


def test_a_missing_git_cliff_is_refused(history: History) -> None:
    with pytest.raises(rn.RenderError, match="uv sync"):
        rn.read_releases(history.root, cliff="no-such-git-cliff")


def test_a_shallow_checkout_is_refused(history: History, tmp_path: Path) -> None:
    clone = tmp_path / "shallow"
    TmpGitRepo(tmp_path / "scratch").git(
        "clone", "-q", "--depth", "1", f"file://{history.root}", str(clone)
    )
    with pytest.raises(rn.RenderError, match=r"git fetch --unshallow --tags"):
        rn.render(clone)


def test_main_removes_stale_pages(
    history: History, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(rn, "PAGES_DIR", tmp_path)
    (tmp_path / "0.0.9.md").write_text("stale\n")
    assert rn.main(["--repo", str(history.root)]) == 0
    assert not (tmp_path / "0.0.9.md").exists()
    assert sorted(p.name for p in tmp_path.glob("*.md")) == [
        "0.1.0.md",
        "0.10.0.md",
        "0.2.0.md",
        "0.9.0.md",
        "index.md",
    ]
    assert (tmp_path / ".stamp").is_file()


def _refuse_render(repo: Path) -> "dict[str, str]":
    raise AssertionError(f"re-rendered {repo} although the stamp matched")


def test_stamp_skip_does_not_rerender(
    history: History, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(rn, "PAGES_DIR", tmp_path)
    assert rn.main(["--repo", str(history.root)]) == 0
    monkeypatch.setattr(rn, "render", _refuse_render)
    assert rn.main(["--repo", str(history.root)]) == 0


def test_stamp_skip_rerenders_when_a_page_is_missing(
    history: History, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(rn, "PAGES_DIR", tmp_path)
    assert rn.main(["--repo", str(history.root)]) == 0
    (tmp_path / "0.9.0.md").unlink()
    assert rn.main(["--repo", str(history.root)]) == 0
    assert (tmp_path / "0.9.0.md").is_file()


@pytest.mark.parametrize("stamp", ["", "\n", "digest-only\n", "\xff\xfe"])
def test_a_malformed_stamp_is_stale(
    history: History, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, stamp: str
) -> None:
    monkeypatch.setattr(rn, "PAGES_DIR", tmp_path)
    assert rn.main(["--repo", str(history.root)]) == 0
    whole = (tmp_path / ".stamp").read_text(encoding="utf-8")
    (tmp_path / ".stamp").write_bytes(stamp.encode("latin-1"))
    assert rn.main(["--repo", str(history.root)]) == 0
    assert (tmp_path / ".stamp").read_text(encoding="utf-8") == whole


def test_the_stamp_is_moved_into_place(
    history: History, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    moved: list[str] = []
    replace = Path.replace

    def spy(self: Path, target: Path) -> Path:
        moved.append(f"{self.name} -> {Path(target).name}")
        return replace(self, target)

    monkeypatch.setattr(rn, "PAGES_DIR", tmp_path)
    monkeypatch.setattr(Path, "replace", spy)
    assert rn.main(["--repo", str(history.root)]) == 0
    assert moved == [".stamp.tmp -> .stamp"]
    assert sorted(p.name for p in tmp_path.iterdir() if p.name.startswith(".")) == [".stamp"]


def test_check_writes_nothing(
    history: History, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pages = tmp_path / "pages"
    monkeypatch.setattr(rn, "PAGES_DIR", pages)
    assert rn.main(["--check", "--repo", str(history.root)]) == 0
    assert not pages.exists()


def test_main_reports_a_refusal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(rn, "PAGES_DIR", tmp_path / "pages")
    repo = _seed(tmp_path / "untagged")
    assert rn.main(["--repo", str(repo.root)]) == 1
    assert "no tagged release" in capsys.readouterr().err


def test_releases_below_the_floor_get_no_page(
    history: History, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(rn, "FIRST_RELEASE", "0.9.0")
    pages = rn.render(history.root)
    assert sorted(pages) == ["0.10.0.md", "0.9.0.md", "index.md"]
    assert "start at 0.9.0" in pages["index.md"]
    # A correction whose target has no page still says what it marks.
    assert f"Marks [{history.sha['target'][:12]}]" in pages["0.9.0.md"]


def test_the_floor_compares_versions_not_strings(
    history: History, monkeypatch: pytest.MonkeyPatch
) -> None:
    # As strings, "0.10.0" sorts below "0.9.0"; as versions it is above.
    monkeypatch.setattr(rn, "FIRST_RELEASE", "0.9.0")
    assert "0.10.0.md" in rn.render(history.root)


def test_tag_state_names_every_merged_tag(history: History) -> None:
    state = rn.tag_state(history.root)
    assert [line.split()[0] for line in state.splitlines()] == [
        "refs/tags/v0.1.0",
        "refs/tags/v0.10.0",
        "refs/tags/v0.2.0",
        "refs/tags/v0.9.0",
    ]


# ---------------------------------------------------------------------------
# Escaping commit text.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("a <b> c", "a &lt;b> c"),
        ("keep `<b>` and ``x ` <y>`` here <z>", "keep `<b>` and ``x ` <y>`` here &lt;z>"),
        ("an unmatched ` <b>", "an unmatched ` &lt;b>"),
        ("a `span\nacross lines <b>` stays", "a `span\nacross lines <b>` stays"),
        (
            "use `x\n\n<script>alert(1)</script>\n\nthen ` done",
            "use `x\n\n&lt;script>alert(1)&lt;/script>\n\nthen ` done",
        ),
        (
            "use `x\n  \t\n<b>\n\nthen ` done",
            "use `x\n  \t\n&lt;b>\n\nthen ` done",
        ),
    ],
)
def test_escape_markdown_html(text: str, expected: str) -> None:
    assert rn.escape_markdown_html(text) == expected


def _literal(markdown: str) -> str:
    """The text *markdown* shows as one literal ``text`` fence; fails on anything else."""
    tokens = rn.myst_tokens(markdown)
    assert [(t.type, t.info) for t in tokens] == [("fence", "text")], markdown
    return tokens[0].content


def _code(markdown: str) -> str:
    """The text *markdown* shows as one code span; fails on anything else."""
    tokens = rn.myst_tokens(markdown)
    assert [t.type for t in tokens] == ["paragraph_open", "inline", "paragraph_close"]
    children = tokens[1].children or []
    assert [c.type for c in children] == ["code_inline"], markdown
    return children[0].content


@pytest.mark.parametrize(
    "line",
    ["# not a heading", "(rn-x)=", "::: note", "```python", "~~~", "% a comment", "---", "+++"],
)
def test_a_structure_line_shows_the_whole_remediation_verbatim(line: str) -> None:
    text = f"prose\n{line}\nmore"
    assert _literal(rn.format_remediation(text)) == f"{text}\n"


# Astra's bypasses of the old regex shield, each shown verbatim and whole.
BYPASSES = [
    # An escaped opening backtick: the shield saw a code span, Markdown does not.
    "use \\`x <script>alert(1)</script>` here",
    # A blockquoted fence runs a directive.
    "see:\n\n> ```{raw} html\n> <b>x</b>\n> ```",
    "> ```{include} /etc/passwd\n> ```",
    "x\n\n```{include} /etc/passwd\n```",
    "a {ref}`role` in prose",
    "[x]: https://example.invalid\n\nthe [x] reference",
    "a footnote[^1]\n\n[^1]: here",
    "| a | b |\n|---|---|\n| 1 | 2 |",
    "term\n: definition",
    ":field: value",
    "a [link](ftp://example.invalid) here",
    "an image ![x](https://example.invalid/x.png)",
    "an unclosed fence\n\n```\nswallows the page",
]


@pytest.mark.parametrize("text", BYPASSES)
def test_unplain_remediation_is_shown_whole_and_verbatim(text: str) -> None:
    assert _literal(rn.format_remediation(text)) == f"{text}\n"


@pytest.mark.parametrize(
    "subject",
    [
        "```{include} /etc/passwd",
        "\\`x <script>alert(1)</script>`",
        "# a heading",
        "> a quote",
        "- a list",
        "(rn-x)=",
        "{ref}`x`",
    ],
)
def test_an_unplain_subject_is_one_code_span(subject: str) -> None:
    assert _code(rn.inline_text(subject)) == subject


@pytest.mark.parametrize(
    ("text", "markdown"),
    [
        ("use `--new`, *not* **old**", "use `--new`, *not* **old**"),
        ("see [the docs](https://example.org/x)", "see [the docs](https://example.org/x)"),
        ("label <host>/`<host>`", "label &lt;host>/`<host>`"),
    ],
)
def test_a_plain_subject_renders_as_markdown(text: str, markdown: str) -> None:
    assert rn.inline_text(text) == markdown


def test_plain_remediation_markdown_renders_as_written() -> None:
    text = (
        "Use `--new` instead, *not* `--old`; see [the guide](https://example.org/g).\n\n"
        "- **one** thing\n- another\n\n1. first\n2. second"
    )
    assert rn.format_remediation(text) == text


def test_a_scope_and_subject_cannot_join_into_html(tmp_path: Path) -> None:
    """Each part is plain alone; joined, the scope's backtick would pair with the subject's."""
    repo = _seed(tmp_path / "join")
    c = _Writer(repo)
    c("base", "feat(cli): base")
    c.tag("v0.1.0")
    c("feature", "feat(a`): q `<img src=x onerror=alert(1)>`")
    sha = c("break", "feat(a`)!: q `<img src=x onerror=alert(1)>`\n\nBREAKING CHANGE: none.")
    c.tag("v0.2.0")
    page = rn.render(repo.root)["0.2.0.md"]
    bullets = _section(page, "New features")
    heading = next(line for line in _entry(page, sha).splitlines() if line.startswith("### "))
    assert rn.raw_html(bullets) == []
    assert rn.raw_html(heading) == []
    assert rn.raw_html(page) == [rn.GENERATED + "\n"]
    assert "<img src=x onerror=alert(1)>" in heading


def test_a_page_holding_raw_html_is_refused_by_name(
    history: History, monkeypatch: pytest.MonkeyPatch
) -> None:
    render_page = rn.render_page

    def leaky(release: rn.Release, url: str) -> str:
        page = render_page(release, url)
        return page + "\n<b>leak</b>\n" if release.version == "0.9.0" else page

    monkeypatch.setattr(rn, "render_page", leaky)
    with pytest.raises(rn.RenderError, match=r"0\.9\.0\.md would publish raw HTML.*<b>"):
        rn.render(history.root)


def test_a_subject_cannot_retarget_the_summary_link(tmp_path: Path) -> None:
    repo = _seed(tmp_path / "retarget")
    c = _Writer(repo)
    c("base", "feat(cli): base")
    c.tag("v0.1.0")
    subject = "x](https://example.invalid)"
    sha = c("evil", f"feat(cli)!: {subject}\n\nBREAKING CHANGE: nothing to do.")
    c.tag("v0.2.0")
    page = rn.render(repo.root)["0.2.0.md"]
    table = page[page.index("{list-table}") : page.index("```\n", page.index("{list-table}"))]
    row = next(line for line in table.splitlines() if line.startswith("* - ["))
    tokens = rn.myst_tokens(row[len("* - ") :])
    links = [t.attrs["href"] for t in tokens[1].children or [] if t.type == "link_open"]
    assert links == [f"#rn-{sha[:12]}"]
    assert "example.invalid" not in "".join(links)


def test_a_wrapped_list_item_continues_and_is_no_example() -> None:
    text = (
        "Renamed:\n- RequireRepo / registering_repo / get_registering_repo / .repo(name)\n"
        "  -> RequireProject / registering_project /\n  .project(name)\n- RepoOptions"
        " -> ProjectOptions\n- every repo parameter across the docker,\n"
        "  coverage and monitor APIs -> project"
    )
    out = rn.format_remediation(text)
    assert "```" not in out
    assert out == text


def test_an_indented_run_after_prose_or_a_blank_line_is_an_example() -> None:
    out = rn.format_remediation("- an item\n\n  otto x -> otto y\nprose:\n  otto a -> otto b")
    fences = re.findall(r"^```text\n(.*?)^```$", out, re.MULTILINE | re.DOTALL)
    assert fences == ["otto x -> otto y\n", "otto a -> otto b\n"]


@pytest.mark.parametrize("eol", ["\n", "\r\n", "\r"])
def test_a_stray_backtick_cannot_shield_html_in_a_later_paragraph(eol: str) -> None:
    text = f"use `x{eol}{eol}<script>alert(1)</script>{eol}{eol}then ` done"
    assert "<script>" not in rn.format_remediation(text)
    assert "<script>" not in rn.escape_markdown_html(text)


def test_an_indented_run_holding_a_fence_gets_a_longer_fence() -> None:
    assert rn.format_remediation("x:\n  ```\n  y") == "x:\n\n````text\n```\ny\n````"


# ---------------------------------------------------------------------------
# The rendered pages build.
# ---------------------------------------------------------------------------


def test_a_sphinx_build_renders_the_examples_as_code(history: History, tmp_path: Path) -> None:
    """A real Sphinx build over the pages, wired as ``docs/conf.py`` wires them."""
    src = tmp_path / "src"
    pages = src / rn.PAGES_DOCNAME
    pages.mkdir(parents=True)
    for name, text in rn.render(history.root).items():
        (pages / name).write_text(text, encoding="utf-8")
    (src / "index.md").write_text(
        f"# Root\n\nThe flag --wait stays smart here.\n\n"
        f"```{{toctree}}\n{rn.PAGES_DOCNAME}/index\n```\n",
        encoding="utf-8",
    )
    (src / "conf.py").write_text(
        f"import sys\nsys.path.insert(0, {str(CHECKOUT_ROOT)!r})\n"
        "from scripts.render_release_notes import sphinx_setup\n"
        'extensions = ["myst_parser", "sphinx_immaterial"]\n'
        'html_theme = "sphinx_immaterial"\n'
        f"myst_enable_extensions = {rn.docs_conf_value('myst_enable_extensions')!r}\n"
        f"myst_heading_anchors = {rn.docs_conf_value('myst_heading_anchors')!r}\n"
        "def setup(app):\n    sphinx_setup(app)\n",
        encoding="utf-8",
    )
    proc = subprocess.run(
        [sys.executable, "-m", "sphinx", "-W", "-q", "-b", "html", str(src), str(tmp_path / "out")],
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr
    out = tmp_path / "out"
    page = (out / rn.PAGES_DOCNAME / "0.2.0.html").read_text(encoding="utf-8")
    pres = [
        html.unescape(re.sub(r"<[^>]+>", "", p))
        for p in re.findall(r"<pre>(.*?)</pre>", page, re.DOTALL)
    ]
    assert any('expect(cond, msg="why")' in pre for pre in pres)
    assert "label runs &lt;host&gt;/&lt;product&gt;" in page
    assert "<details" in page
    # Smart quotes are off on the release notes only: a flag keeps its two hyphens.
    assert "reboot --wait returns" in page
    assert "flag \u2013wait stays" in (out / "index.html").read_text(encoding="utf-8")


def test_the_docs_build_wires_the_release_notes_sphinx_setup() -> None:
    conf = (CHECKOUT_ROOT / "docs" / "conf.py").read_text(encoding="utf-8")
    hook = conf[conf.index("\ndef setup(app):") :]
    assert "render_release_notes.sphinx_setup(app)" in hook


# ---------------------------------------------------------------------------
# Wiring: the docs build renders the pages, and stays fresh.
# ---------------------------------------------------------------------------


def test_cliff_toml_and_pyproject_spell_the_same_repository_url() -> None:
    """A guard, not a second owner: cliff.toml's template cannot read pyproject."""
    cliff = (CHECKOUT_ROOT / "cliff.toml").read_text(encoding="utf-8")
    footer = cliff[cliff.index('\nfooter = """') :]
    urls = set(re.findall(r"https://github\.com/[\w.-]+/[\w.-]+", footer))
    assert urls == {URL}, f"cliff.toml's footer spells {sorted(urls)}; pyproject says {URL}"


def test_the_docs_build_renders_the_release_notes_and_fails_when_the_renderer_does() -> None:
    assert_conf_renders("scripts.render_release_notes", "docs/release-notes/index.md")


def test_the_renderers_reach_git_cliff_beside_the_interpreter() -> None:
    """A bare ``.venv/bin/sphinx-build`` has no venv on PATH; the renderer needs git-cliff."""
    conf = (CHECKOUT_ROOT / "docs" / "conf.py").read_text(encoding="utf-8")
    hook = conf[conf.index("def _generate_rendered_pages") :]
    hook = hook[: hook.index("\ndef ")]
    assert "Path(sys.executable).parent" in hook
    assert "env=" in hook


def test_sphinx_srcs_names_the_renderer_its_inputs_and_the_tag_stamp() -> None:
    """``make docs`` re-triggers on a renderer edit or a new tag, not on every commit."""
    makefile = (CHECKOUT_ROOT / "Makefile").read_text(encoding="utf-8")
    srcs = re.search(r"^SPHINX_SRCS :=.*?\n((?:.*\\\n)*.*)", makefile, re.MULTILINE)
    assert srcs, "no SPHINX_SRCS assignment found in the Makefile (guard misparse?)"
    for name in (
        "scripts/render_release_notes.py",
        "scripts/release_events.py",
        "cliff.toml",
        "docs/_build/.release-tags",
    ):
        assert name in srcs.group(0), f"{name} is not in SPHINX_SRCS"
    rule = makefile[makefile.index("\ndocs/_build/.release-tags:") :]
    rule = rule[: rule.index("\n\n")]
    assert "FORCE" in rule.splitlines()[1], "the stamp rule must always run"
    assert "-m scripts.render_release_notes --print-tag-state" in rule
    assert "cmp -s" in rule, "the stamp is rewritten only when the tag state changes"


def test_the_pages_are_git_ignored_and_in_the_reference_toctree() -> None:
    gitignore = (CHECKOUT_ROOT / ".gitignore").read_text(encoding="utf-8").splitlines()
    assert "docs/release-notes/" in gitignore
    index = (CHECKOUT_ROOT / "docs" / "index.rst").read_text(encoding="utf-8")
    assert "release-notes/index" in index
