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

from scripts import api_lines
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


def _unresolved(text: str) -> "list[str]":
    """Return one message per golden entry of *text* that does not resolve.

    A v1 golden is read line by line, ``<module>:<name>``. A v2 golden (the API
    dump) is parsed whole; every ``name`` record's ``ns:dotted`` key must resolve
    by ``getattr`` along the dots. A dump that does not parse is one message.
    """
    if api_lines.schema_of(text) == 2:
        from scripts import api_records

        try:
            dump = api_records.parse_dump(text)
        except api_records.DumpError as exc:
            return [f"the dump does not parse: {exc}"]
        unresolved = []
        for key in sorted(dump.bindings):
            module_name, _, name = key.partition(":")
            error = mod._resolve(module_name, name)
            if error is not None:
                unresolved.append(f"{key}: {error}")
        return unresolved
    unresolved = []
    for line in api_lines.data_lines(text):
        module_name, _, name = line.partition(":")
        error = mod._resolve(module_name, name)
        if error is not None:
            unresolved.append(f"{line}: {error}")
    return unresolved


def test_every_golden_line_resolves():
    """Each committed line must still import — a golden that can go stale is worthless."""
    unresolved = _unresolved(mod.GOLDEN_PATH.read_text(encoding="utf-8"))
    assert not unresolved, unresolved


V2_FIXTURE = (
    "# api-snapshot v2\n"
    "# producer-schema 1\n"
    "name\totto.tls:os_trust_session\tfunction\n"
    "call\totto.tls:os_trust_session\tsync\tKO:timeout:F:0x1.e000000000000p+4\n"
)


def test_v2_name_records_resolve_through_their_namespace():
    """Spec §6: the every-entry-resolves guarantee carries over to the dump's ``name`` records."""
    assert _unresolved(V2_FIXTURE) == []


def test_v2_resolution_reports_a_dead_name_and_an_unparseable_dump():
    dead = V2_FIXTURE + "name\totto.tls:NoSuchThingAtAll\tvalue\n"
    (message,) = _unresolved(dead)
    assert message.startswith("otto.tls:NoSuchThingAtAll: ")
    (broken,) = _unresolved(V2_FIXTURE + "bogus\n")
    assert broken.startswith("the dump does not parse: ")


def test_v1_resolution_reads_a_module_name_pair():
    expected = (
        "not_a_module_at_all:: cannot import 'not_a_module_at_all': "
        "No module named 'not_a_module_at_all'"
    )
    assert _unresolved("# v1\notto:CommandResult\nnot_a_module_at_all:\n") == [expected]


def test_extractor_finds_a_known_documented_path():
    """The docs walk must not silently go vacuous.

    ``docs/cookbook/python-library.md`` teaches ``from otto.suite import
    run_tests`` in a real fenced example; if the walk ever stopped finding
    anything (wrong root, wrong glob, a fence-parsing regression that eats
    every block), this is the canary.
    """
    lines, failures = mod.documented_deep_imports()
    assert not failures, failures
    assert "otto.suite:run_tests" in lines


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

    A synthetic tree teaches the SAME symbol (``otto.suite:run_tests``) in
    seven different shapes: a plain ```` ```python ```` fence and a
    blank-line-then-4-space indent (already worked before this test existed),
    a ``~~~python`` tilde fence, a ```` ```python ```` fence indented inside a
    list item (the live, otto-import-free shape at
    ``docs/cookbook/sessions.md:116`` — this is what would go quiet if that
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
        "from otto.suite import run_tests\n"
        "```\n"
        "\n"
        "Indented block:\n"
        "\n"
        "    from otto.suite import run_tests\n"
        "\n"
        "Tilde fence:\n"
        "\n"
        "~~~python\n"
        "from otto.suite import run_tests\n"
        "~~~\n"
        "\n"
        "Fence indented inside a list item:\n"
        "\n"
        "- bullet text:\n"
        "\n"
        "  ```python\n"
        "  from otto.suite import run_tests\n"
        "  ```\n"
        "\n"
        "MyST code-block directive:\n"
        "\n"
        "```{code-block} python\n"
        "from otto.suite import run_tests\n"
        "```\n"
        "\n"
        "Nested in an equal-count container:\n"
        "\n"
        "```{note}\n"
        "```python\n"
        "from otto.suite import run_tests\n"
        "```\n"
        "```\n"
        "\n"
        "Nested in a wider container:\n"
        "\n"
        "````{note}\n"
        "```python\n"
        "from otto.suite import run_tests\n"
        "```\n"
        "````\n",
        encoding="utf-8",
    )

    candidates = mod._markdown_candidates(docs / "shapes.md")
    hits = [c for c in candidates if "run_tests" in c[1]]
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


def test_host_protocol_lines_cover_a_known_verb():
    """``put`` must show up with its real, documented parameter names."""
    lines = mod.host_protocol_lines()
    assert (
        "otto.host.host:Host.put(src_files, dest_dir, mode, user, show_progress, "
        "recursive, concurrent)" in lines
    )


def test_red_a_host_protocol_parameter_rename_moves_the_surface(monkeypatch):
    """RED-proof (d): a Host protocol parameter rename must move the golden line.

    A conforming host that renamed a keyword otto's own call sites pass would
    still type-check against a hand-typed allowlist; reading the signature
    straight off the live ``Host`` class (the same way
    ``otto.testing.conformance_host._keyword_names`` does) is what actually
    catches it.
    """
    import otto.host.host as host_mod

    class FakeHost:
        async def put(
            self,
            src_files,
            dest_dir,
            mode=None,
            renamed_user=None,
            show_progress=True,
        ): ...

    monkeypatch.setattr(host_mod, "Host", FakeHost)

    lines = mod.host_protocol_lines()

    assert (
        "otto.host.host:Host.put(src_files, dest_dir, mode, renamed_user, show_progress)" in lines
    )
    assert not any(
        line.startswith("otto.host.host:Host.put(src_files, dest_dir, mode, user,")
        for line in lines
    )

    surface, failures = mod.compute_surface()
    assert not failures
    expected = mod.read_golden()
    assert set(surface) != set(expected), "a renamed Host protocol parameter must move the surface"


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
    assert "otto.suite:run_tests" not in lines


def _check_against(monkeypatch, tmp_path, golden_lines):
    """Run ``--check`` against a golden holding *golden_lines*; return (exit, output, golden)."""
    golden = tmp_path / "golden.txt"
    golden.write_text("\n".join(golden_lines) + "\n", encoding="utf-8")
    before = golden.read_bytes()
    monkeypatch.setattr(mod, "GOLDEN_PATH", golden)
    return mod.main(["--check"]), before, golden


def test_check_says_the_api_grew_when_lines_were_only_added(monkeypatch, tmp_path, capsys):
    """Growth breaks no caller, and the verdict says so rather than "changed"."""
    current = mod.read_golden()
    exit_code, before, golden = _check_against(monkeypatch, tmp_path, current[1:])

    out = capsys.readouterr().out
    assert exit_code == 1
    assert "public API GREW: 1 line(s) added, nothing removed or changed" in out
    assert "CHANGED" not in out
    assert golden.read_bytes() == before  # it reports; it never records


def test_check_says_the_api_changed_and_names_what_callers_lose(monkeypatch, tmp_path, capsys):
    current = mod.read_golden()
    gone = "otto:NameThatWasRemoved"
    exit_code, before, golden = _check_against(monkeypatch, tmp_path, sorted([*current, gone]))

    out = capsys.readouterr().out
    assert exit_code == 1
    assert "public API CHANGED: 1 line(s) removed or rewritten, 0 added" in out
    assert f"  - {gone}" in out
    assert "GREW" not in out
    assert golden.read_bytes() == before


def _tls_manifest(tmp_path):
    manifest = tmp_path / "public.toml"
    manifest.write_text(
        '[namespaces."otto.tls"]\ntier = 1\nstability = "provisional"\n', encoding="utf-8"
    )
    return manifest


def test_dump_mode_prints_the_dump_of_a_manifest(tmp_path, capsys):
    exit_code = mod.main(["--manifest", str(_tls_manifest(tmp_path))])
    out = capsys.readouterr().out.splitlines()
    assert exit_code == 0
    assert out[:2] == ["# api-snapshot v2", "# producer-schema 1"]
    assert any(line.startswith("name\totto.tls:os_trust_session\t") for line in out)
    assert not any(line.startswith("otto.host.host:Host.") for line in out)


def test_dump_check_compares_against_the_given_golden(tmp_path, capsys):
    manifest, golden = _tls_manifest(tmp_path), tmp_path / "golden.txt"
    assert mod.main(["--manifest", str(manifest), "--golden", str(golden), "--update"]) == 0
    assert golden.read_text(encoding="utf-8").startswith("# api-snapshot v2\n# producer-schema 1\n")
    assert mod.main(["--manifest", str(manifest), "--golden", str(golden), "--check"]) == 0
    golden.write_text(golden.read_text(encoding="utf-8") + "name\totto.tls:Ghost\tvalue\n")
    capsys.readouterr()
    assert mod.main(["--manifest", str(manifest), "--golden", str(golden), "--check"]) == 1
    out = capsys.readouterr().out
    assert "public API CHANGED" in out
    assert "Ghost: removed [otto.tls]" in out


def test_dump_check_calls_a_resorted_golden_stale_but_not_a_change(tmp_path, capsys):
    manifest, golden = _tls_manifest(tmp_path), tmp_path / "golden.txt"
    assert mod.main(["--manifest", str(manifest), "--golden", str(golden), "--update"]) == 0
    header, schema, body = golden.read_text(encoding="utf-8").split("\n", 2)
    records = body.splitlines(keepends=True)
    assert len(records) > 1, (
        "otto.tls must dump more than one record for a re-sort to mean anything"
    )
    golden.write_text(f"{header}\n{schema}\n" + "".join(reversed(records)), encoding="utf-8")
    capsys.readouterr()
    assert mod.main(["--manifest", str(manifest), "--golden", str(golden), "--check"]) == 1
    out = capsys.readouterr().out
    assert "not in canonical order" in out
    assert "CHANGED" not in out


def test_dump_update_refuses_to_write_when_the_producer_refuses(tmp_path, capsys, monkeypatch):
    from scripts import api_regen

    monkeypatch.setattr(
        api_regen,
        "generate_worktree",
        lambda repo, manifest, assume_dir=False: api_regen.Generated(
            None, ["otto.x: has no __all__"]
        ),
    )
    golden = tmp_path / "golden.txt"
    argv = ["--manifest", str(_tls_manifest(tmp_path)), "--golden", str(golden), "--update"]
    assert mod.main(argv) == 1
    assert not golden.exists()
    assert "FAIL otto.x: has no __all__" in capsys.readouterr().out


def test_dump_report_always_exits_zero_and_counts_refusals(tmp_path, capsys, monkeypatch):
    from scripts import api_regen

    monkeypatch.setattr(
        api_regen,
        "generate_worktree",
        lambda repo, manifest, assume_dir=False: api_regen.Generated(None, ["a", "b"]),
    )
    assert mod.main(["--manifest", str(_tls_manifest(tmp_path)), "--report", "--assume-dir"]) == 0
    assert "api-dump-report: 2 producer refusal(s)" in capsys.readouterr().out


def test_v2_check_says_ok_when_the_golden_matches(tmp_path, capsys):
    manifest, golden = _tls_manifest(tmp_path), tmp_path / "golden.txt"
    assert mod.main(["--manifest", str(manifest), "--golden", str(golden), "--update"]) == 0
    capsys.readouterr()

    assert mod.main(["--manifest", str(manifest), "--golden", str(golden), "--check"]) == 0
    assert capsys.readouterr().out == "api snapshot: OK\n"


def test_v2_check_calls_a_crlf_copy_of_a_fresh_golden_stale(tmp_path, capsys):
    """``--check`` compares bytes, as check-breaking's freshness check does (dump spec §5.1)."""
    manifest, golden = _tls_manifest(tmp_path), tmp_path / "golden.txt"
    assert mod.main(["--manifest", str(manifest), "--golden", str(golden), "--update"]) == 0
    golden.write_bytes(golden.read_bytes().replace(b"\n", chr(13).encode() + b"\n"))
    capsys.readouterr()

    assert mod.main(["--manifest", str(manifest), "--golden", str(golden), "--check"]) == 1
    out = capsys.readouterr().out
    assert "api snapshot: OK" not in out
    assert "bytes differ" in out


def test_v2_check_of_a_missing_golden_is_a_fail_line_not_a_traceback(tmp_path, capsys):
    manifest, golden = _tls_manifest(tmp_path), tmp_path / "absent.txt"

    assert mod.main(["--manifest", str(manifest), "--golden", str(golden), "--check"]) == 1
    (line,) = capsys.readouterr().out.splitlines()
    assert line.startswith(f"FAIL cannot read the golden {golden}: ")


def test_v2_a_failed_namespace_report_is_a_fail_line_not_a_traceback(tmp_path, capsys, monkeypatch):
    from scripts import api_agreement

    def broken(namespaces, repo):
        raise api_agreement.AgreementError("the reporting child died")

    monkeypatch.setattr(api_agreement, "namespace_reports", broken)
    manifest = _tls_manifest(tmp_path)
    for mode in ([], ["--check"], ["--update"]):
        argv = ["--manifest", str(manifest), "--golden", str(tmp_path / "g.txt"), *mode]
        assert mod.main(argv) == 1
        assert capsys.readouterr().out == (
            "FAIL cannot report the declared namespaces: the reporting child died\n"
        )


def test_v2_check_refuses_a_golden_without_the_v2_header(tmp_path, capsys):
    """The checker reads a header-less golden as v1, so ``--check`` must not call it v2."""
    manifest = tmp_path / "public.toml"
    manifest.write_text(
        '[namespaces."otto.tls"]\ntier = 1\nstability = "provisional"\n', encoding="utf-8"
    )
    golden = tmp_path / "golden.txt"
    assert mod.main(["--manifest", str(manifest), "--golden", str(golden), "--update"]) == 0
    golden.write_text(
        golden.read_text(encoding="utf-8").replace("# api-snapshot v2\n", ""), encoding="utf-8"
    )
    capsys.readouterr()

    assert mod.main(["--manifest", str(manifest), "--golden", str(golden), "--check"]) == 1
    assert "not an api-snapshot v2 golden" in capsys.readouterr().out


def test_dump_report_prints_producer_refusals_not_agreement_failures(tmp_path, capsys, monkeypatch):
    from scripts import api_agreement, api_regen

    monkeypatch.setattr(
        api_regen,
        "generate_worktree",
        lambda repo, manifest, assume_dir=False: api_regen.Generated(None, ["producer broke"]),
    )
    monkeypatch.setattr(api_agreement, "agreement_failures", lambda names, reports: ["not agreed"])
    assert mod.main(["--manifest", str(_tls_manifest(tmp_path)), "--report"]) == 0
    out = capsys.readouterr().out
    assert "refusal: producer broke" in out
    assert "not agreed" not in out
    assert "api-dump-report: 1 producer refusal(s)" in out
