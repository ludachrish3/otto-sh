"""``scripts/api_docs_blocks.py``: ordered code blocks from docs pages and example files."""

import pytest

from scripts.api_docs_blocks import (
    literal_includes,
    markdown_blocks,
    python_blocks,
    rst_blocks,
    teaching_corpus,
)
from tests._fixtures.paths import PROJECT_ROOT

pytestmark = pytest.mark.interpreter_agnostic


def test_markdown_blocks_come_in_page_order_with_their_language(tmp_path):
    page = tmp_path / "p.md"
    page.write_text(
        "```{note}\n```python\nimport otto\n```\n```\n"
        "\n```{testsetup}\nfrom otto import Status\n```\n"
        "\n```bash\notto run\n```\n",
        encoding="utf-8",
    )
    got = [(b.lang, [t for _, t in b.lines]) for b in markdown_blocks(page)]
    assert ("python", ["import otto"]) in got
    assert ("{testsetup}", ["from otto import Status"]) in got
    assert ("bash", ["otto run"]) in got
    starts = [b.lines[0][0] for b in markdown_blocks(page) if b.lines]
    assert starts == sorted(starts)


def test_markdown_display_fences_stay_suppressed(tmp_path):
    page = tmp_path / "p.md"
    page.write_text("````markdown\n```python\nfrom otto.x import Y\n```\n````\n", encoding="utf-8")
    assert [b for b in markdown_blocks(page) if b.lang == "python"] == []


def test_rst_blocks_name_doctest_and_setup_directives(tmp_path):
    page = tmp_path / "p.rst"
    page.write_text(
        ".. doctest::\n\n   >>> import otto\n\n"
        ".. testsetup::\n\n   from otto import Status\n\n"
        ".. code-block:: python\n\n   from otto.host import UnixHost\n\n"
        "Plain literal::\n\n   from otto import Result\n",
        encoding="utf-8",
    )
    assert [b.lang for b in rst_blocks(page)] == ["{doctest}", "{testsetup}", "python", ""]


def test_python_blocks_split_file_doctests_and_literal_blocks(tmp_path):
    src = tmp_path / "ex.py"
    src.write_text(
        '"""Example.\n\n'
        ">>> from otto.host import parse_one\n"
        ">>> parse_one\n"
        "<function ...>\n\n"
        "Register it::\n\n"
        "    from otto.host import register_host_class\n"
        '"""\n\n'
        "from otto import Status\n",
        encoding="utf-8",
    )
    blocks = python_blocks(src)
    assert [b.lang for b in blocks] == ["py-file", "py-doctest", "py-literal"]
    assert blocks[1].lines[0] == (3, ">>> from otto.host import parse_one")
    assert [t.strip() for _, t in blocks[2].lines] == ["from otto.host import register_host_class"]


def test_literal_includes_resolve_relative_and_docs_rooted_targets(tmp_path):
    docs = tmp_path / "docs"
    (docs / "guide").mkdir(parents=True)
    page = docs / "guide" / "p.md"
    page.write_text(
        "```{literalinclude} ../examples/a.py\n```\n:::{literalinclude} /examples/b.py\n:::\n",
        encoding="utf-8",
    )
    got = literal_includes(page, docs)
    assert got == [
        (1, (docs / "examples" / "a.py").resolve()),
        (3, (docs / "examples" / "b.py").resolve()),
    ]


def test_teaching_corpus_covers_the_boundary_and_skips_archives():
    corpus = teaching_corpus(PROJECT_ROOT)
    rel = {p.relative_to(PROJECT_ROOT).as_posix() for p in corpus}
    assert "README.md" in rel
    assert "docs/cookbook/python-library.md" in rel
    assert "src/otto/examples/lab_repository.py" in rel
    assert any(r.startswith("docs/examples/") and r.endswith(".py") for r in rel)
    assert not any("superpowers" in r or "_build" in r for r in rel)
    assert "src/otto/host/host.py" not in rel  # a function doctest elsewhere is not teaching (§3)


def test_markdown_eval_rst_code_block_keeps_its_page_line_number(tmp_path):
    page = tmp_path / "p.md"
    page.write_text(
        "# Title\n\n"
        "```{eval-rst}\n"
        ".. code-block:: python\n\n"
        "   from otto.host import UnixHost\n"
        "```\n",
        encoding="utf-8",
    )
    blocks = markdown_blocks(page)
    assert [b.lang for b in blocks] == ["python"]
    assert blocks[0].lines == [(6, "   from otto.host import UnixHost")]


def test_markdown_eval_rst_doctest_is_a_doctest_block_in_page_order(tmp_path):
    page = tmp_path / "p.md"
    page.write_text(
        "```python\nimport otto\n```\n\n"
        "- item\n\n"
        "  ```{eval-rst}\n"
        "  .. doctest::\n\n"
        "     >>> from otto import Status\n"
        "  ```\n\n"
        "```bash\notto run\n```\n",
        encoding="utf-8",
    )
    got = [(b.lang, b.lines) for b in markdown_blocks(page)]
    assert got == [
        ("python", [(2, "import otto")]),
        ("{doctest}", [(10, "     >>> from otto import Status")]),
        ("bash", [(14, "otto run")]),
    ]


def test_markdown_colon_fences_yield_blocks_like_their_backtick_equivalents(tmp_path):
    page = tmp_path / "p.md"
    page.write_text(
        ":::{code-block} python\nfrom otto.x import Y\n:::\n\n"
        ":::{doctest}\n>>> from otto import Status\n:::\n\n"
        "::::{note}\nProse.\n:::{code-block} python\nimport otto.z\n:::\n::::\n\n"
        ":::{warning}\n```python\nfrom otto.w import V\n```\n:::\n",
        encoding="utf-8",
    )
    got = [(b.lang, b.lines) for b in markdown_blocks(page)]
    assert ("python", [(2, "from otto.x import Y")]) in got
    assert ("{doctest}", [(6, ">>> from otto import Status")]) in got
    assert ("python", [(12, "import otto.z")]) in got
    assert ("{note}", [(10, "Prose.")]) in got
    assert ("python", [(18, "from otto.w import V")]) in got


def test_markdown_colon_fence_without_a_directive_is_a_container_not_code(tmp_path):
    # MyST renders a non-directive colon fence as a div of Markdown: its prose is not code,
    # but a code fence inside it still is.
    page = tmp_path / "p.md"
    page.write_text(
        ":::\nfrom otto.x import Y\n```python\nimport otto\n```\n:::\n", encoding="utf-8"
    )
    got = [(b.lang, b.lines) for b in markdown_blocks(page)]
    assert ("python", [(4, "import otto")]) in got
    assert not any(lang in {"", "python"} and (2, "from otto.x import Y") in ln for lang, ln in got)


def test_rst_doctest_block_without_a_directive_is_a_doctest_block(tmp_path):
    page = tmp_path / "p.rst"
    page.write_text(
        "Intro.\n\n>>> from otto.x import Y\n>>> Y\n1\n\nMore prose.\n",
        encoding="utf-8",
    )
    got = [(b.lang, b.lines) for b in rst_blocks(page)]
    assert got == [("{doctest}", [(3, ">>> from otto.x import Y"), (4, ">>> Y"), (5, "1")])]


def test_rst_doctest_block_mid_paragraph_is_prose(tmp_path):
    page = tmp_path / "p.rst"
    page.write_text("A paragraph that goes on\n>>> and is still prose.\n", encoding="utf-8")
    assert rst_blocks(page) == []


def test_markdown_eval_rst_doctest_block_without_a_directive(tmp_path):
    page = tmp_path / "p.md"
    page.write_text("```{eval-rst}\n>>> from otto.x import Y\n```\n", encoding="utf-8")
    got = [(b.lang, b.lines) for b in markdown_blocks(page)]
    assert got == [("{doctest}", [(2, ">>> from otto.x import Y")])]


def test_python_docstring_rst_directives_use_the_rst_lang_mapping(tmp_path):
    src = tmp_path / "ex.py"
    src.write_text(
        '"""Example.\n\n'
        ".. code-block:: python\n\n"
        "    from otto.x import Y\n\n"
        ".. testsetup::\n\n"
        "    from otto import Status\n\n"
        ".. doctest::\n\n"
        "    >>> Status\n\n"
        ">>> from otto.host import parse_one\n\n"
        "Literal::\n\n"
        "    from otto.host import register_host_class\n"
        '"""\n',
        encoding="utf-8",
    )
    got = [(b.lang, b.lines) for b in python_blocks(src)[1:]]
    assert got == [
        ("python", [(5, "    from otto.x import Y")]),
        ("{testsetup}", [(9, "    from otto import Status")]),
        ("{doctest}", [(13, "    >>> Status")]),
        ("py-doctest", [(15, ">>> from otto.host import parse_one")]),
        ("py-literal", [(19, "    from otto.host import register_host_class")]),
    ]


def test_markdown_unclosed_fence_runs_to_end_of_page(tmp_path):
    page = tmp_path / "p.md"
    page.write_text(
        "Prose.\n\n````{note}\n```python\nfrom otto.x import Y\n\nz = 1\n", encoding="utf-8"
    )
    got = [(b.lang, b.lines) for b in markdown_blocks(page)]
    assert ("python", [(5, "from otto.x import Y"), (6, ""), (7, "z = 1")]) in got
    assert ("{note}", []) in got


def test_markdown_unclosed_eval_rst_fence_yields_its_rst_blocks(tmp_path):
    page = tmp_path / "p.md"
    page.write_text("```{eval-rst}\n.. code-block:: python\n\n   import otto.x\n", encoding="utf-8")
    assert [(b.lang, b.lines) for b in markdown_blocks(page)] == [
        ("python", [(4, "   import otto.x")])
    ]


def test_python_blocks_on_a_syntax_error_yield_only_the_whole_file(tmp_path):
    src = tmp_path / "ex.py"
    text = '"""Doc.\n\n>>> import otto\n"""\n\nfrom otto.host import (\n'
    src.write_text(text, encoding="utf-8")
    blocks = python_blocks(src)
    assert [b.lang for b in blocks] == ["py-file"]
    assert [t for _, t in blocks[0].lines] == text.split("\n")[:-1]


def test_python_blocks_parse_an_invalid_escape_under_warnings_as_errors(tmp_path):
    # pytest runs with filterwarnings=error: an invalid escape's SyntaxWarning (a
    # DeprecationWarning before 3.12) must not become a SyntaxError that hides the docstrings.
    src = tmp_path / "ex.py"
    src.write_text('X = "\\d"\n\n\ndef f():\n    """\n    >>> f()\n    """\n', encoding="utf-8")
    assert [b.lang for b in python_blocks(src)] == ["py-file", "py-doctest"]


def test_python_blocks_number_lines_like_the_parser_despite_form_feeds(tmp_path):
    # str.splitlines() also breaks on form feeds and Unicode separators; the parser on \n only.
    src = tmp_path / "ex.py"
    src.write_text('# a\x0cb\n"""Doc.\n\n>>> import otto.x\n"""\n', encoding="utf-8")
    blocks = python_blocks(src)
    assert blocks[0].lines[0] == (1, "# a\x0cb")
    assert len(blocks[0].lines) == 5
    assert [(b.lang, b.lines) for b in blocks[1:]] == [("py-doctest", [(4, ">>> import otto.x")])]


def test_markdown_colon_line_inside_a_backtick_code_fence_is_code(tmp_path):
    page = tmp_path / "p.md"
    page.write_text(
        '```python\nX = """\n:::\n"""\nfrom otto.nope import X\n```\n\nProse.\n',
        encoding="utf-8",
    )
    assert [(b.lang, b.lines) for b in markdown_blocks(page)] == [
        ("python", [(2, 'X = """'), (3, ":::"), (4, '"""'), (5, "from otto.nope import X")])
    ]


def test_markdown_backtick_line_inside_a_colon_code_fence_is_code(tmp_path):
    page = tmp_path / "p.md"
    page.write_text(
        ':::{code-block} python\nX = """\n```\n"""\nfrom otto.nope import X\n:::\n\nProse.\n',
        encoding="utf-8",
    )
    assert [(b.lang, b.lines) for b in markdown_blocks(page)] == [
        ("python", [(2, 'X = """'), (3, "```"), (4, '"""'), (5, "from otto.nope import X")])
    ]


def test_markdown_fence_line_with_info_inside_a_code_fence_opens_nothing(tmp_path):
    page = tmp_path / "p.md"
    page.write_text(
        "````python\ndoc = '''\n```bash\nfrom otto.nope import X\n```\n'''\n````\n",
        encoding="utf-8",
    )
    assert [b.lang for b in markdown_blocks(page)] == ["python"]
    assert (4, "from otto.nope import X") in markdown_blocks(page)[0].lines
