"""Every ``{literalinclude}`` under docs/getting-started/ resolves (spec §4 tier 1).

Sphinx ``-W`` also refuses a missing file or marker, but only when the docs
build runs; this guard runs in the unit lane, names the page and the marker,
and is testable against a scratch tree so it is known to fail.
"""

import re
from pathlib import Path

from tests._fixtures.paths import PROJECT_ROOT

_DOCS = PROJECT_ROOT / "docs"
_PAGES = _DOCS / "getting-started"
_INCLUDE = re.compile(
    r"^[ \t]*(?:```|:::)\{literalinclude\}[ \t]+(?P<target>\S+)[ \t]*\n"
    r"(?P<opts>(?:[ \t]*:[\w-]+:.*\n)*)",
    re.MULTILINE,
)
_OPT = re.compile(r":(?P<key>[\w-]+):\s*(?P<value>.*)")


def _unquote(value: str) -> str:
    """Strip one layer of matching quotes, the way MyST's option tokenizer does.

    A directive-option value that starts with ``#`` or ``"`` is not literal
    Markdown text -- MyST's fenced-directive options are tokenized like YAML
    flow scalars, so an unquoted ``#`` opens a comment and an unquoted ``"``
    opens a quoted string. A marker that must itself begin with either
    character (a TOML comment anchor, a JSON key) has to be wrapped in quotes
    in the page source, and MyST strips that wrapper before the value ever
    reaches the directive. This mirrors that stripping so the guard checks
    the same string Sphinx will.
    """
    if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
        return value[1:-1]
    return value


def check_includes(pages_root: Path) -> list[str]:
    problems = []
    for page in sorted(pages_root.rglob("*.md")):
        for m in _INCLUDE.finditer(page.read_text()):
            target = (page.parent / m["target"]).resolve()
            where = f"{page.relative_to(pages_root.parent)}: {m['target']}"
            if not target.is_file():
                problems.append(f"{where}: file not found")
                continue
            opts = {o["key"]: _unquote(o["value"].strip()) for o in _OPT.finditer(m["opts"])}
            text = target.read_text()
            for key in ("start-after", "end-before"):
                marker = opts.get(key)
                if marker is None:
                    continue
                hits = text.count(marker)
                if hits != 1:
                    problems.append(
                        f"{where}: {key} {marker!r} found {hits} times (need exactly 1)"
                    )
    return problems


def test_scratch_tree_reports_every_kind_of_problem(tmp_path):
    (tmp_path / "pages").mkdir()
    (tmp_path / "data.txt").write_text("# doc: begin a\nx\n# doc: end a\n# doc: begin a\n")
    (tmp_path / "pages" / "p.md").write_text(
        "```{literalinclude} ../data.txt\n"
        ":start-after: # doc: begin a\n"
        ":end-before: # doc: end a\n"
        "```\n"
        "```{literalinclude} ../missing.txt\n```\n"
        "```{literalinclude} ../data.txt\n:start-after: # doc: begin zz\n```\n"
    )
    problems = check_includes(tmp_path / "pages")
    assert [p.split(": ", 1)[1] for p in problems] == [
        "../data.txt: start-after '# doc: begin a' found 2 times (need exactly 1)",
        "../missing.txt: file not found",
        "../data.txt: start-after '# doc: begin zz' found 0 times (need exactly 1)",
    ]


def test_indented_and_colon_fences_are_also_checked(tmp_path):
    (tmp_path / "pages").mkdir()
    (tmp_path / "data.txt").write_text("# doc: begin a\nx\n# doc: end a\n")
    (tmp_path / "pages" / "p.md").write_text(
        "- an indented include, e.g. inside a list item:\n"
        "\n"
        "  ```{literalinclude} ../data.txt\n"
        "  :start-after: # doc: begin zz\n"
        "  ```\n"
        "\n"
        ":::{literalinclude} ../missing.txt\n"
        ":::\n"
    )
    problems = check_includes(tmp_path / "pages")
    assert [p.split(": ", 1)[1] for p in problems] == [
        "../data.txt: start-after '# doc: begin zz' found 0 times (need exactly 1)",
        "../missing.txt: file not found",
    ]


def test_quoted_markers_are_unquoted_before_matching(tmp_path):
    (tmp_path / "pages").mkdir()
    (tmp_path / "data.toml").write_text("# doc: begin a\nx = 1\n# doc: end a\n")
    (tmp_path / "data.json").write_text('{"_doc_begin": "a", "x": 1, "_doc_end": "a"}\n')
    (tmp_path / "pages" / "p.md").write_text(
        "```{literalinclude} ../data.toml\n"
        ':start-after: "# doc: begin a"\n'
        ':end-before: "# doc: end a"\n'
        "```\n"
        "```{literalinclude} ../data.json\n"
        ':start-after: \'"_doc_begin": "a"\'\n'
        ':end-before: \'"_doc_end": "a"\'\n'
        "```\n"
    )
    assert check_includes(tmp_path / "pages") == []


def test_the_real_pages_resolve():
    assert _PAGES.is_dir(), "docs/getting-started/ is the hub directory (Task 6)"
    assert check_includes(_PAGES) == []
