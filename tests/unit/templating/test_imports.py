"""The hermetic import callback: only the roots, never a protected file, always the library."""

import hashlib
import json
import os
import subprocess
import sys
from dataclasses import replace
from pathlib import Path

import pytest

from otto.config.corpus_snapshot import Unreadable
from otto.templating import imports
from otto.templating.errors import RenderError, TemplatingError
from otto.templating.imports import LIBRARY_NAME, ImportTrace, library_digest, make_callback
from otto.templating.render import render

from ._render_helpers import (
    IMPORT_KEYWORDS,
    IN_ROOT_FORMS,
    OUT_OF_ROOT_FORMS,
    STUB_LIBRARY,
    policy,
    protecting,
    record_opens,
    req,
    req_importing,
    root_with,
)


def test_importstr_and_importbin_are_byte_exact(tmp_path: Path) -> None:
    lib = root_with(tmp_path, {"blob.bin": bytes(range(256)), "a.libsonnet": b"{x: 1}\n"})
    out = json.loads(
        render(
            req(
                tmp_path,
                "{s: importstr 'a.libsonnet', b: importbin 'blob.bin', "
                "c: (import 'a.libsonnet').x}",
                roots=(lib,),
            )
        ).text
    )
    assert out == {"s": "{x: 1}\n", "b": list(range(256)), "c": 1}


@pytest.mark.parametrize("keyword", IMPORT_KEYWORDS)
@pytest.mark.parametrize("form", IN_ROOT_FORMS)
def test_protected_file_is_refused_before_open(
    form: str, keyword: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    request = req_importing(
        tmp_path, form, target="creds.json", protected={"creds.json"}, keyword=keyword
    )
    opened = record_opens(monkeypatch)
    with pytest.raises(RenderError, match="refused") as caught:
        render(request)
    assert not [p for p in opened() if p.name == "creds.json"]
    assert "s3cret" not in str(caught.value)


@pytest.mark.parametrize("keyword", IMPORT_KEYWORDS)
@pytest.mark.parametrize("form", OUT_OF_ROOT_FORMS)
def test_a_link_out_of_the_roots_is_refused_before_open(
    form: str, keyword: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    request = req_importing(tmp_path, form, target="secret.json", protected=set(), keyword=keyword)
    opened = record_opens(monkeypatch)
    with pytest.raises(RenderError, match=r"refused \(outside the import roots\)") as caught:
        render(request)
    assert not [p for p in opened() if p.name == "secret.json"]
    assert "s3cret" not in str(caught.value)


def _swap(path: Path, swap: str, protected: Path) -> None:
    """Replace *path* with a symlink or a hardlink to *protected*, or with a FIFO."""
    path.unlink()
    if swap == "symlink":
        path.symlink_to(protected)
    elif swap == "hardlink":
        path.hardlink_to(protected)
    else:
        os.mkfifo(path)


@pytest.mark.parametrize("swap", ["symlink", "hardlink", "fifo"])
def test_a_file_swapped_after_the_check_is_refused(
    swap: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The file changes between the checks and the open: the descriptor's identity refuses it."""
    lib = root_with(tmp_path, {"a.libsonnet": b"{}", "creds.json": b'{"token": "s3cret"}'})
    real_open = imports._open_candidate

    def swapping_open(path: Path) -> int:
        if path.name == "a.libsonnet":
            _swap(path, swap, lib / "creds.json")
        return real_open(path)  # a FIFO opens at once (O_NONBLOCK), never blocks

    monkeypatch.setattr(imports, "_open_candidate", swapping_open)
    request = req(
        tmp_path,
        "importstr 'a.libsonnet'",
        roots=(lib,),
        protected=protecting(lib, {"creds.json"}),
    )
    with pytest.raises(RenderError, match=r"refused \(it changed after it was checked\)") as caught:
        render(request)
    assert "s3cret" not in str(caught.value)


def test_a_protected_file_swapped_in_before_the_stat_is_refused_by_its_descriptor(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The swap lands after the path checks and before the stat, so only the open file's
    identity (``contains_stat``) can refuse it."""
    lib = root_with(tmp_path, {"a.libsonnet": b"{}", "creds.json": b'{"token": "s3cret"}'})
    real_stat = imports._stat_candidate

    def swapping_stat(path: Path) -> object:
        if path.name == "a.libsonnet":
            _swap(path, "hardlink", lib / "creds.json")
        return real_stat(path)

    monkeypatch.setattr(imports, "_stat_candidate", swapping_stat)
    request = req(
        tmp_path,
        "importstr 'a.libsonnet'",
        roots=(lib,),
        protected=protecting(lib, {"creds.json"}),
    )
    with pytest.raises(RenderError, match=r"refused \(a protected file\)") as caught:
        render(request)
    assert "s3cret" not in str(caught.value)


def test_an_unstattable_candidate_is_refused_not_skipped(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    first = root_with(tmp_path, {"a.libsonnet": b"'first'"}, name="first")
    second = root_with(tmp_path, {"a.libsonnet": b"'second'"}, name="second")
    real_stat = imports._stat_candidate

    def unstattable_first(path: Path) -> object:
        if path.parent == first.resolve():
            return Unreadable(PermissionError(13, "Permission denied"))
        return real_stat(path)

    monkeypatch.setattr(imports, "_stat_candidate", unstattable_first)
    with pytest.raises(RenderError, match=r"'a\.libsonnet'.*: unreadable \(.*Permission denied"):
        render(req(tmp_path, "import 'a.libsonnet'", roots=(first, second)))


@pytest.mark.parametrize("keyword", ["importstr", "importbin"])
def test_a_refused_raw_import_still_fails_the_render(keyword: str, tmp_path: Path) -> None:
    lib = root_with(tmp_path, {"creds.json": b'{"token": "s3cret"}'})
    request = req(
        tmp_path,
        f"{{a: {keyword} 'creds.json'}}",
        roots=(lib,),
        protected=protecting(lib, {"creds.json"}),
    )
    with pytest.raises(RenderError) as caught:
        render(request)
    assert str(caught.value) == (
        f"{request.entry}: import 'creds.json' from {tmp_path}: refused (a protected file)"
    )
    assert "s3cret" not in str(caught.value)


def test_a_refusal_names_the_import_and_its_reason(tmp_path: Path) -> None:
    request = req_importing(tmp_path, "relative", target="creds.json", protected={"creds.json"})
    with pytest.raises(RenderError) as caught:
        render(request)
    assert caught.value.origin == request.entry
    assert caught.value.line == 1
    assert str(caught.value) == (
        f"{request.entry}:1: import 'creds.json' from {tmp_path}: refused (a protected file)"
    )


def test_an_import_outside_every_root_is_refused(tmp_path: Path) -> None:
    lib = root_with(tmp_path, {"a.libsonnet": b"{}"})
    (tmp_path / "outside.libsonnet").write_text("{}")
    with pytest.raises(RenderError, match=r"refused \(outside the import roots\)"):
        render(req(tmp_path, "import '../outside.libsonnet'", roots=(lib,)))


def test_an_absolute_import_is_refused(tmp_path: Path) -> None:
    lib = root_with(tmp_path, {"a.libsonnet": b"{}"})
    with pytest.raises(RenderError, match=r"refused \(an absolute import path\)"):
        render(req(tmp_path, f"import '{lib / 'a.libsonnet'}'", roots=(lib,)))


def test_entry_directory_is_not_an_import_root(tmp_path: Path) -> None:
    (tmp_path / "sibling.libsonnet").write_text("{}")
    lib = root_with(tmp_path, {"a.libsonnet": b"{}"})
    for roots in ((), (lib,)):
        with pytest.raises(RenderError, match=r"'sibling\.libsonnet'.*not found"):
            render(req(tmp_path, "import 'sibling.libsonnet'", roots=roots))


def test_an_import_from_a_root_file_resolves_beside_it_first(tmp_path: Path) -> None:
    lib = root_with(
        tmp_path,
        {
            "sub/a.libsonnet": b"import 'b.libsonnet'",
            "sub/b.libsonnet": b"'beside'",
            "b.libsonnet": b"'root'",
        },
    )
    assert json.loads(render(req(tmp_path, "import 'sub/a.libsonnet'", roots=(lib,))).text) == (
        "beside"
    )


def test_the_roots_are_searched_in_order(tmp_path: Path) -> None:
    first = root_with(tmp_path, {"only-second.libsonnet": b"'x'"}, name="first")
    (first / "only-second.libsonnet").unlink()
    (first / "both.libsonnet").write_text("'first'")
    second = root_with(
        tmp_path,
        {"both.libsonnet": b"'second'", "only-second.libsonnet": b"'second'"},
        name="second",
    )
    src = "[import 'both.libsonnet', import 'only-second.libsonnet']"
    out = render(req(tmp_path, src, roots=(first, second)))
    assert json.loads(out.text) == ["first", "second"]


_FRESH_PROCESS_RENDER = """
import sys
from pathlib import Path

assert "_jsonnet" not in sys.modules and "_gojsonnet" not in sys.modules
from otto.inventory.config import ProtectedPaths
from otto.templating.errors import RenderError
from otto.templating.imports import ImportPolicy
from otto.templating.render import RenderRequest, render

policy = ImportPolicy(roots=(), protected=ProtectedPaths(files=frozenset()), reserved={})
try:
    render(RenderRequest(entry=Path(sys.argv[1]), tla=None, policy=policy))
except RenderError as e:
    print(e)
else:
    print("RENDERED")
"""


def test_jsonnet_path_is_ignored(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    ambient = root_with(tmp_path, {"x.libsonnet": b"{}"}, name="ambient")
    monkeypatch.setenv("JSONNET_PATH", str(ambient))
    request = req(tmp_path, "import 'x.libsonnet'")
    with pytest.raises(RenderError, match=r"'x\.libsonnet'.*not found"):
        render(request)
    # A process whose binding is first imported with JSONNET_PATH already set.
    # -I ignores every PYTHON* variable, PYTHONPYCACHEPREFIX included, so -B
    # keeps the child from writing bytecode into the editable src tree.
    child = subprocess.run(
        [sys.executable, "-I", "-B", "-c", _FRESH_PROCESS_RENDER, str(request.entry)],
        env={**os.environ, "JSONNET_PATH": str(ambient)},
        capture_output=True,
        text=True,
        check=True,
    )
    assert "not found under the import roots" in child.stdout, child.stdout + child.stderr
    assert "RENDERED" not in child.stdout


def test_reserved_library_is_importable_with_no_roots(tmp_path: Path) -> None:
    out = render(req(tmp_path, "(import 'otto.libsonnet').greet('a')"))
    assert json.loads(out.text) == "hi a"
    # A root's own otto.libsonnet never shadows the reserved one.
    lib = root_with(tmp_path, {LIBRARY_NAME: b"{ greet(name): 'shadowed' }"})
    out = render(req(tmp_path, "(import 'otto.libsonnet').greet('a')", roots=(lib,)))
    assert json.loads(out.text) == "hi a"
    assert out.read == ((tmp_path / "e.jsonnet").resolve(),)


def test_the_reserved_library_imports_only_reserved_names(tmp_path: Path) -> None:
    lib = root_with(tmp_path, {"helper.libsonnet": b"'from the user root'"})
    reserved = {
        LIBRARY_NAME: b"{ helper: import 'helper.libsonnet', "
        b"extra: import 'otto-extra.libsonnet' }",
        "otto-extra.libsonnet": b"'reserved'",
    }
    request = req(tmp_path, "(import 'otto.libsonnet').extra", roots=(lib,))
    served = replace(request, policy=replace(request.policy, reserved=reserved))
    assert json.loads(render(served).text) == "reserved"
    served.entry.write_text("(import 'otto.libsonnet').helper")
    with pytest.raises(
        RenderError, match=r"refused \(the reserved library imports only reserved names\)"
    ):
        render(served)


def test_the_callback_records_each_file_it_reads(tmp_path: Path) -> None:
    lib = root_with(tmp_path, {"a.libsonnet": b"{}", "creds.json": b'{"token": "s3cret"}'})
    trace = ImportTrace()
    serve = make_callback(policy((lib,), protecting(lib, {"creds.json"})), trace)
    found = serve(f"{tmp_path}/", "a.libsonnet")
    assert found == (str((lib / "a.libsonnet").resolve()), b"{}")
    assert serve(f"{tmp_path}/", LIBRARY_NAME)[1] == STUB_LIBRARY
    # A refusal never raises into the binding: it serves a stand-in that fails.
    found_here, stand_in = serve(f"{tmp_path}/", "creds.json")
    assert found_here == "<refused>/creds.json"
    assert b"refused (a protected file)" in stand_in
    assert b"s3cret" not in stand_in
    assert trace.failure == f"import 'creds.json' from {tmp_path}: refused (a protected file)"
    assert trace.read == [(lib / "a.libsonnet").resolve()]


def test_library_digest_is_the_packaged_librarys_sha256(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    package = tmp_path / "package"
    package.mkdir()
    (package / LIBRARY_NAME).write_bytes(STUB_LIBRARY)
    monkeypatch.setattr(imports.resources, "files", lambda _package: package)
    imports.reserved_library.cache_clear()
    try:
        assert imports.reserved_library() == {LIBRARY_NAME: STUB_LIBRARY}
        assert library_digest() == hashlib.sha256(STUB_LIBRARY).hexdigest()
    finally:
        imports.reserved_library.cache_clear()


def test_a_missing_packaged_library_is_a_templating_error(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(imports.resources, "files", lambda _package: tmp_path)
    imports.reserved_library.cache_clear()
    try:
        with pytest.raises(TemplatingError, match=r"otto\.libsonnet is missing"):
            imports.reserved_library()
        with pytest.raises(TemplatingError, match=r"otto\.libsonnet is missing"):
            library_digest()
    finally:
        imports.reserved_library.cache_clear()
