"""Builders the render, import and freshness tests share.

Every request renders an entry ``e.jsonnet`` written under the test's
``tmp_path``, with a stub reserved library standing in for the packaged
``otto.libsonnet``, so these tests never depend on the library's contents.
:func:`policy` serves another reserved mapping when asked, which is how the
library's own tests serve the packaged one.
"""

import builtins
import io
import os
from collections.abc import Callable, Mapping
from pathlib import Path
from types import MappingProxyType
from typing import Any

import pytest

from otto.inventory import InventoryView
from otto.inventory.config import ProtectedPaths
from otto.inventory.registry import InventoryEnv
from otto.templating.imports import LIBRARY_NAME, ImportPolicy
from otto.templating.render import RenderLimits, RenderRequest

STUB_LIBRARY = b"{ greet(name): 'hi ' + name }\n"
"""What the stub reserved library serves for ``import 'otto.libsonnet'``."""

STUB_RESERVED: Mapping[str, bytes] = MappingProxyType({LIBRARY_NAME: STUB_LIBRARY})

NOTHING_PROTECTED = ProtectedPaths(files=frozenset())
"""A real catalog that happens to protect nothing: unlike ``NO_CATALOG``, a render runs under it."""


def catalogued_view(**fields: Any) -> InventoryView:
    """A hand-built view holding *fields*, with the :data:`NOTHING_PROTECTED` catalog.

    A view no builder made has no catalog (``NO_CATALOG``) and renders
    nothing; a test that renders through a hand-built view says so here.
    """
    return InventoryView(protected=lambda: NOTHING_PROTECTED, **fields)


def hand_built_env(root: Path) -> InventoryEnv:
    """The env of a table compiled on its own at *root*: a real catalog of its own paths.

    An inventory with no env has no catalog and renders nothing; a test that
    renders a hand-built templated inventory passes this.
    """
    return InventoryEnv(root, "hand-built")


def policy(
    roots: "tuple[Path, ...]" = (),
    protected: ProtectedPaths = NOTHING_PROTECTED,
    reserved: Mapping[str, bytes] = STUB_RESERVED,
) -> ImportPolicy:
    """Return an import policy over *roots*, serving *reserved* (the stub library by default)."""
    return ImportPolicy(roots=roots, protected=protected, reserved=reserved)


def req(
    tmp_path: Path,
    src: str,
    *,
    tla: "Mapping[str, object] | None" = None,
    roots: "tuple[Path, ...]" = (),
    limits: "RenderLimits | None" = None,
    protected: ProtectedPaths = NOTHING_PROTECTED,
) -> RenderRequest:
    """Write *src* to ``tmp_path/e.jsonnet`` and return the request that renders it."""
    entry = tmp_path / "e.jsonnet"
    entry.write_text(src)
    return RenderRequest(
        entry=entry, tla=tla, policy=policy(roots, protected), limits=limits or RenderLimits()
    )


def root_with(tmp_path: Path, files: Mapping[str, bytes], *, name: str = "lib") -> Path:
    """Create the import root ``tmp_path/<name>`` holding *files* (relative names)."""
    root = tmp_path / name
    root.mkdir(exist_ok=True)
    for rel, data in files.items():
        target = root / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
    return root


def protecting(root: Path, names: "set[str]") -> ProtectedPaths:
    """Return the catalog protecting each of *names* under *root*."""
    return ProtectedPaths(files=frozenset((root / n).resolve() for n in names))


IN_ROOT_FORMS = ("relative", "dotdot", "absolute", "symlink")
"""Spellings of a protected file inside the root."""
OUT_OF_ROOT_FORMS = ("symlink-out", "symlinked-dir-out")
"""Spellings that reach an unprotected file outside every root through a link."""
IMPORT_KEYWORDS = ("import", "importstr", "importbin")


def req_importing(
    tmp_path: Path, form: str, *, target: str, protected: "set[str]", keyword: str = "import"
) -> RenderRequest:
    """Return a request whose entry imports *target* through *form* with *keyword*.

    The in-root forms reach the root's *target*: ``relative`` names it
    plainly; ``dotdot`` climbs out of the root and back in; ``absolute`` names
    its absolute path; ``symlink`` imports an alias whose link points at it.
    The out-of-root forms reach an unprotected *target* in a directory
    beside the root: ``symlink-out`` through a file link in the root,
    ``symlinked-dir-out`` through a directory link in the root.
    """
    secret = b'{"token": "s3cret"}'
    lib = root_with(tmp_path, {target: secret, "ok.libsonnet": b"{}"})
    outside = root_with(tmp_path, {target: secret}, name="outside")
    (lib / "alias.json").symlink_to(lib / target)
    (lib / "out.json").symlink_to(outside / target)
    (lib / "outdir").symlink_to(outside, target_is_directory=True)
    spelled = {
        "relative": target,
        "dotdot": f"../{lib.name}/{target}",
        "absolute": str(lib / target),
        "symlink": "alias.json",
        "symlink-out": "out.json",
        "symlinked-dir-out": f"outdir/{target}",
    }[form]
    return req(
        tmp_path,
        f"[import 'ok.libsonnet', {keyword} '{spelled}']",
        roots=(lib,),
        protected=protecting(lib, protected),
    )


def record_opens(monkeypatch: pytest.MonkeyPatch) -> Callable[[], "list[Path]"]:
    """Record every path opened through ``open``, ``io.open`` or ``os.open`` from now on."""
    opened: list[Path] = []
    real_open = io.open
    real_os_open = os.open

    def note(file: object) -> None:
        if isinstance(file, (str, bytes, os.PathLike)):
            opened.append(Path(os.fsdecode(file)))

    def recording_open(file: object, *args: object, **kwargs: object) -> object:
        note(file)
        return real_open(file, *args, **kwargs)  # ty: ignore[no-matching-overload]

    def recording_os_open(path: object, *args: object, **kwargs: object) -> int:
        note(path)
        return real_os_open(path, *args, **kwargs)  # ty: ignore[invalid-argument-type]

    monkeypatch.setattr(io, "open", recording_open)
    monkeypatch.setattr(builtins, "open", recording_open)
    monkeypatch.setattr(os, "open", recording_os_open)
    return lambda: list(opened)


def count_evaluations(monkeypatch: pytest.MonkeyPatch) -> Callable[[], int]:
    """Count every call into the Jsonnet evaluator from now on."""
    from otto.templating import render

    calls: list[Path] = []
    real = render._evaluate

    def counting(binding: Any, entry: Path, *args: Any) -> str:
        calls.append(entry)
        return real(binding, entry, *args)

    monkeypatch.setattr(render, "_evaluate", counting)
    return lambda: len(calls)
