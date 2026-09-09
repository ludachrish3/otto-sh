"""The public-API golden snapshot: ``otto.__all__`` + every deep import the docs teach.

``scripts/api_snapshot.py`` is the machinery; this pins its two outputs — the
committed golden (``tests/unit/api_snapshot/public_api.txt``) and the
resolution guarantee (every line in it must actually import).

This does NOT re-pin `_LAZY_EXPORTS` vs `__all__` pairing — that is
``tests/unit/config/test_lazy_exports.py::test_otto_lazy_exports_table_is_paired_with_all``.
What is missing there, and what this file exists for, is a COMMITTED diff
surface (so a name change is visible in review) and the "which deep import
paths are sanctioned" question, which this repo answers as: exactly the ones
the user docs teach.
"""

import importlib.util

from tests._fixtures.paths import PROJECT_ROOT

_MODULE_PATH = PROJECT_ROOT / "scripts" / "api_snapshot.py"


def _load():
    spec = importlib.util.spec_from_file_location("api_snapshot", _MODULE_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


mod = _load()


def test_surface_matches_the_committed_golden():
    """The live surface must equal ``public_api.txt`` exactly.

    A name added/removed/renamed in ``otto.__all__``, or a doc page that
    starts/stops teaching a deep import, changes this set — and the failure
    message names exactly what changed so the fix (usually `make
    api-snapshot`) is obvious.
    """
    lines, failures = mod.compute_surface()
    assert not failures, f"documented imports failed to resolve: {failures}"
    expected = mod.read_golden()
    added = sorted(set(lines) - set(expected))
    removed = sorted(set(expected) - set(lines))
    assert lines == expected, (
        "public API surface drifted from tests/unit/api_snapshot/public_api.txt "
        f"— run `make api-snapshot` and review the diff.\n"
        f"  added:   {added}\n"
        f"  removed: {removed}"
    )


def test_every_golden_line_resolves():
    """Each committed line must still import — a golden that can go stale is worthless."""
    unresolved = []
    for line in mod.read_golden():
        module_name, _, name = line.partition(":")
        error = mod._resolve(module_name, name)
        if error is not None:
            unresolved.append(f"{line}: {error}")
    assert not unresolved, unresolved


def test_extractor_finds_a_known_documented_path():
    """The docs walk must not silently go vacuous.

    ``docs/library/writing-suites.md`` teaches ``from otto.suite import
    OttoSuite`` in a real fenced example; if the walk ever stopped finding
    anything (wrong root, wrong glob, a fence-parsing regression that eats
    every block), this is the canary.
    """
    lines, failures = mod.documented_deep_imports()
    assert not failures, failures
    assert "otto.suite:OttoSuite" in lines


def test_surface_has_a_floor():
    """A gross under-count means the docs walk broke, not that the API shrank.

    Uses the same ``SURFACE_FLOOR`` constant ``--update`` refuses under, so
    the test and the script can't drift apart on what "too small" means.
    """
    lines, failures = mod.compute_surface()
    assert not failures, failures
    assert len(lines) >= mod.SURFACE_FLOOR, (
        f"surface has only {len(lines)} lines — the docs walk may be broken"
    )


def test_shape_canary_finds_every_fence_shape(tmp_path):
    """Every code-block shape the docs actually use must be found — not just the common ones.

    A synthetic tree teaches the SAME symbol (``otto.suite:OttoSuite``) in
    seven different shapes: a plain ```` ```python ```` fence and a
    blank-line-then-4-space indent (already worked before this test existed),
    a ``~~~python`` tilde fence, a ```` ```python ```` fence indented inside a
    list item (the live, otto-import-free shape at
    ``docs/library/sessions.md:116`` — this is what would go quiet if that
    page ever gained a real import inside it), MyST's
    ```` ```{code-block} python ```` directive, and a ```` ```python ````
    fence nested inside a ```` ```{note} ```` container at both equal and
    greater backtick counts (MyST's prescribed nesting needs more backticks
    on the container, but same-count nesting shows up too and must not eat
    the inner fence).

    Checked at the ``_markdown_candidates`` level (not just "is the symbol in
    the final surface") so a shape that silently drops its statement can't
    hide behind another shape that still works and dedupes to the same line.
    """
    docs = tmp_path / "docs"
    docs.mkdir()
    (docs / "shapes.md").write_text(
        "# Shape canary\n"
        "\n"
        "Plain fence:\n"
        "\n"
        "```python\n"
        "from otto.suite import OttoSuite\n"
        "```\n"
        "\n"
        "Indented block:\n"
        "\n"
        "    from otto.suite import OttoSuite\n"
        "\n"
        "Tilde fence:\n"
        "\n"
        "~~~python\n"
        "from otto.suite import OttoSuite\n"
        "~~~\n"
        "\n"
        "Fence indented inside a list item:\n"
        "\n"
        "- bullet text:\n"
        "\n"
        "  ```python\n"
        "  from otto.suite import OttoSuite\n"
        "  ```\n"
        "\n"
        "MyST code-block directive:\n"
        "\n"
        "```{code-block} python\n"
        "from otto.suite import OttoSuite\n"
        "```\n"
        "\n"
        "Nested in an equal-count container:\n"
        "\n"
        "```{note}\n"
        "```python\n"
        "from otto.suite import OttoSuite\n"
        "```\n"
        "```\n"
        "\n"
        "Nested in a wider container:\n"
        "\n"
        "````{note}\n"
        "```python\n"
        "from otto.suite import OttoSuite\n"
        "```\n"
        "````\n",
        encoding="utf-8",
    )

    candidates = mod._markdown_candidates(docs / "shapes.md")
    hits = [c for c in candidates if "OttoSuite" in c[1]]
    assert len(hits) == 7, f"expected 7 shapes to be found, got {len(hits)}: {hits}"


def test_multiline_parenthesized_import_resolves_in_markdown_and_pycon(tmp_path):
    """A parenthesized multi-line import must not raise "'(' was never closed".

    Both the plain-fence form (bare continuation lines) and the doctest form
    (``... `` continuation prompts) must join back into one statement before
    ``ast.parse`` sees it.
    """
    docs = tmp_path / "docs"
    docs.mkdir()
    (docs / "paren.md").write_text(
        "# Parenthesized import\n"
        "\n"
        "Plain fenced form:\n"
        "\n"
        "```python\n"
        "from otto.host import (\n"
        "    UnixHost,\n"
        "    register_transfer_backend,\n"
        ")\n"
        "```\n"
        "\n"
        "Doctest form:\n"
        "\n"
        "```{doctest}\n"
        ">>> from otto.host import (\n"
        "...     UnixHost,\n"
        "...     register_transfer_backend,\n"
        "... )\n"
        ">>> UnixHost is not None\n"
        "True\n"
        "```\n",
        encoding="utf-8",
    )

    lines, failures = mod.documented_deep_imports(docs_root=docs)

    assert not failures, failures
    assert set(lines) == {"otto.host:UnixHost", "otto.host:register_transfer_backend"}


def test_update_refuses_to_write_when_resolution_failures_exist(monkeypatch, tmp_path):
    """``--update`` must not commit a golden alongside a broken documented import."""
    docs = tmp_path / "docs"
    docs.mkdir()
    (docs / "broken.md").write_text(
        "```python\nfrom otto.utils import DoesNotExistAtAll\n```\n", encoding="utf-8"
    )
    golden = tmp_path / "golden.txt"
    monkeypatch.setattr(mod, "DOCS_ROOT", docs)
    monkeypatch.setattr(mod, "GOLDEN_PATH", golden)

    exit_code = mod.main(["--update"])

    assert exit_code == 1
    assert not golden.exists()


def test_update_refuses_to_write_a_golden_below_the_floor(monkeypatch, tmp_path):
    """``--update`` must not commit a suspiciously small golden.

    A surface below the floor means a broken docs walk, not a smaller API.
    """
    empty_docs = tmp_path / "docs"
    empty_docs.mkdir()
    golden = tmp_path / "golden.txt"
    monkeypatch.setattr(mod, "DOCS_ROOT", empty_docs)
    monkeypatch.setattr(mod, "GOLDEN_PATH", golden)

    exit_code = mod.main(["--update"])

    assert exit_code == 1
    assert not golden.exists()


def test_import_line_ignores_a_name_that_merely_starts_with_otto():
    """``otto_something`` must be ignored, not reported as a broken `otto` import.

    ``otto(?:\\.\\w+)*`` can't consume ``_something`` (no dot precedes it), so
    the pattern fails at the required ``\\s+import`` right after ``otto`` —
    unlike a bracketed ``otto[\\w.]*``, which would greedily eat ``_something``
    and then "succeed" by treating an unrelated package as an otto path.
    """
    assert mod.IMPORT_LINE.match("from otto_something import Thing") is None
    assert mod.IMPORT_LINE.match("import otto_something") is None
    assert mod.IMPORT_LINE.match("from otto.utils import Status") is not None


def test_red_a_removed_public_name_fails_the_surface_check(monkeypatch):
    """RED-proof (a): drop a name from ``otto.__all__`` and the golden comparison must catch it."""
    import otto

    # CommandResult is exported only via __all__ — no doc page also teaches
    # `from otto import CommandResult`, so removing it here is guaranteed to
    # actually shrink the surface (unlike a name the docs redundantly cover).
    trimmed = [name for name in otto.__all__ if name != "CommandResult"]
    assert trimmed != list(otto.__all__)  # sanity: it really was there
    monkeypatch.setattr(otto, "__all__", trimmed)

    lines, failures = mod.compute_surface()
    assert not failures
    expected = mod.read_golden()
    assert lines != expected
    assert "otto:CommandResult" in set(expected) - set(lines)


def test_red_a_broken_documented_import_is_reported_with_file_and_line(tmp_path):
    """RED-proof (b): a fence that imports a nonexistent symbol is a named finding, not a skip."""
    docs = tmp_path / "docs"
    docs.mkdir()
    (docs / "example.md").write_text(
        "# Example\n\nSome prose.\n\n```python\nfrom otto.utils import DoesNotExistAtAll\n```\n"
    )

    lines, failures = mod.documented_deep_imports(docs_root=docs)

    assert lines == []
    assert len(failures) == 1
    assert "example.md:6" in failures[0]
    assert "DoesNotExistAtAll" in failures[0]


def test_red_an_empty_docs_root_finds_no_known_path(monkeypatch, tmp_path):
    """RED-proof (c): if the docs walk is pointed at nothing, the canary test must fail.

    Same assertion as ``test_extractor_finds_a_known_documented_path``, aimed at
    an empty tree instead of the real docs/ — proving that test can actually
    go red rather than passing by construction.
    """
    empty_docs = tmp_path / "docs"
    empty_docs.mkdir()
    monkeypatch.setattr(mod, "DOCS_ROOT", empty_docs)

    lines, failures = mod.documented_deep_imports()

    assert not failures
    assert lines == []
    assert "otto.suite:OttoSuite" not in lines
