"""The freshness rule: which files a render may depend on, and how each is fingerprinted."""

from pathlib import Path

from otto.models.base import JsonFileSource, observed_fingerprint
from otto.templating.freshness import rendering_dependencies
from otto.templating.imports import library_file
from otto.templating.render import render

from ._render_helpers import req, root_with


def test_input_paths_cover_every_reachable_file(tmp_path: Path) -> None:
    lib = root_with(
        tmp_path, {"a.libsonnet": b"import 'sub/b.libsonnet'", "sub/b.libsonnet": b"{}"}
    )
    (lib / "sub" / "empty").mkdir()
    missing = tmp_path / "not-yet"
    entry = tmp_path / "e.jsonnet"

    paths = rendering_dependencies(entries=[entry], roots=[lib, missing])

    library = library_file()
    assert library is not None, "control: a source checkout's library is a plain file"
    assert paths == [
        entry,
        *sorted(
            [
                lib,
                lib / "a.libsonnet",
                lib / "sub",
                lib / "sub" / "b.libsonnet",
                lib / "sub" / "empty",
                missing,
                library,
            ]
        ),
    ]
    # Every file a render reads is among them, without evaluating anything.
    out = render(req(tmp_path, "import 'a.libsonnet'", roots=(lib,)))
    assert {p.resolve() for p in out.read} <= {p.resolve() for p in paths}


def test_input_paths_never_read_beside_the_entry(tmp_path: Path) -> None:
    (tmp_path / "creds.json").write_text("{}")
    lib = root_with(tmp_path, {"a.libsonnet": b"{}"})
    paths = rendering_dependencies(entries=[tmp_path / "e.jsonnet"], roots=[lib])
    assert tmp_path / "creds.json" not in paths


def test_the_library_is_a_dependency_with_no_import_root(tmp_path: Path) -> None:
    """Every render may import ``otto.libsonnet``, so ``import_paths = []`` still lists it."""
    entry = tmp_path / "e.jsonnet"
    assert rendering_dependencies(entries=[entry], roots=[]) == [entry, library_file()]


def test_observed_fingerprint_matches_json_file_source(tmp_path: Path) -> None:
    present = tmp_path / "present.json"
    present.write_text("{}")
    missing = tmp_path / "missing.json"
    loop = tmp_path / "loop.json"
    loop.symlink_to(loop)  # ELOOP: unreadable, never absent

    st = present.stat()
    expected = {
        present: f"{present}|{st.st_mtime_ns}|{st.st_size}",
        missing: f"{missing}|missing",
        loop: f"{loop}|unreadable",
    }
    for path, spelled in expected.items():
        assert observed_fingerprint(path) == spelled
        assert JsonFileSource(path).fingerprint() == spelled
