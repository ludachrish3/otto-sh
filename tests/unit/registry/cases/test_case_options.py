"""Conformance cases for the options registry (a raw record seam with a decorator)."""

import functools
import sys

from otto.params import OPTIONS, OptionsEntry, options, options_key, register_options
from otto.registry import Ref
from tests.unit.registry import conformance

COVERS = ["otto.params:OPTIONS"]


def _lazy_module(tmp_path, monkeypatch) -> str:
    """A module defining an options class, on the path but not yet imported."""
    (tmp_path / "case_lazy_opts.py").write_text(
        "from otto import options\n@options\nclass Opts:\n    flag: bool = False\n"
    )
    monkeypatch.syspath_prepend(str(tmp_path))
    monkeypatch.delitem(sys.modules, "case_lazy_opts", raising=False)
    return "case_lazy_opts:Opts"


def _reexport_package(tmp_path, monkeypatch) -> str:
    """``reexport_pkg``, whose ``Real`` is a re-export of ``reexport_pkg.defining:Real``."""
    pkg = tmp_path / "reexport_pkg"
    pkg.mkdir()
    (pkg / "defining.py").write_text(
        "from otto import options\n@options\nclass Real:\n    x: int = 1\n"
    )
    (pkg / "__init__.py").write_text("from .defining import Real\n")
    monkeypatch.syspath_prepend(str(tmp_path))
    monkeypatch.delitem(sys.modules, "reexport_pkg", raising=False)
    monkeypatch.delitem(sys.modules, "reexport_pkg.defining", raising=False)
    return "reexport_pkg:Real"


def test_options_raw_case(tmp_path, monkeypatch):
    lazy = _lazy_module(tmp_path, monkeypatch)
    reexport = _reexport_package(tmp_path, monkeypatch)
    conformance.assert_raw_registry(
        OPTIONS,
        make=lambda i: (
            "pkg.m:C",
            OptionsEntry(target=Ref("pkg.m:C"), verbs=("run",) if i % 2 else ("test",)),
        ),
        invalid=("pkg.b:C", OptionsEntry(target=Ref("pkg.a:C"), verbs=("run",))),
        lazy=(lazy, OptionsEntry(target=Ref(lazy), verbs=("run",))),
        wrongly_typed_lazy=(reexport, OptionsEntry(target=Ref(reexport), verbs=("run",))),
    )


def test_options_wrapper_case():
    conformance.assert_wrapper_matches_raw(
        OPTIONS,
        via_wrapper=functools.partial(register_options, "pkg.m:C", verbs=["run"]),
        record_for=lambda: OptionsEntry(target=Ref("pkg.m:C"), verbs=("run",)),
    )


def test_the_decorator_credits_the_decorating_module():
    class Decorated:
        flag: bool = False

    built = conformance.from_module("case_decorating.init", options(verbs=["run"]), Decorated)
    assert OPTIONS.origin(options_key(built)) == "case_decorating.init"
    assert OPTIONS.peek(options_key(built)) == OptionsEntry(target=built, verbs=("run",))
