"""``scripts/api_agreement.py``: declared namespaces agree with what the code binds at runtime."""

import subprocess

import pytest

from scripts import api_agreement
from scripts.api_agreement import (
    AgreementError,
    StaticSites,
    agreement_failures,
    declaration_from,
    literal_all_names,
    namespace_reports,
)

pytestmark = pytest.mark.interpreter_agnostic


def _write(root, rel, text):
    path = root / "src" / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


@pytest.fixture
def fixture_tree(tmp_path):
    _write(tmp_path, "otto/__init__.py", '__all__ = ["Thing"]\nfrom .impl import Thing\n')
    _write(tmp_path, "otto/impl.py", "class Thing:\n    pass\n")
    _write(tmp_path, "otto/alias.py", '__all__ = ["Thing"]\nfrom .impl import Thing\n')
    _write(tmp_path, "otto/twin.py", '__all__ = ["Thing"]\nclass Thing:\n    pass\n')
    _write(
        tmp_path,
        "otto/tconly.py",
        "from typing import TYPE_CHECKING\nif TYPE_CHECKING:\n    from .impl import Thing\n"
        '__all__ = ["Thing"]\n',
    )
    _write(tmp_path, "otto/computed.py", '__all__ = list(["Thing"])\nfrom .impl import Thing\n')
    _write(tmp_path, "otto/dup.py", '__all__ = ["Thing", "Thing"]\nfrom .impl import Thing\n')
    _write(tmp_path, "otto/boom.py", 'raise RuntimeError("boom at import")\n')
    _write(tmp_path, "otto/noall.py", "def f():\n    pass\n")
    _write(tmp_path, "otto/exits.py", "raise SystemExit(0)\n")
    _write(
        tmp_path,
        "otto/lazybroken/__init__.py",
        '__all__ = ["X"]\ndef __getattr__(name):\n    import otto.no_such_module\n',
    )
    _write(
        tmp_path,
        "otto/lazyexit/__init__.py",
        '__all__ = ["X"]\ndef __getattr__(name):\n    raise SystemExit(0)\n',
    )
    _write(tmp_path, "otto/badall.py", "__all__ = 5\n")
    _write(
        tmp_path,
        "otto/augmented.py",
        '__all__ = ["Thing"]\n__all__ += ["other"]\nfrom .impl import Thing\nother = 1\n',
    )
    for twin in ("aliases_a", "aliases_b"):
        _write(
            tmp_path,
            f"otto/{twin}.py",
            "from collections.abc import Callable\nfrom typing import Literal\n"
            f'__all__ = ["Handler", "Mode", "Ints"]\n'
            f"Handler = Callable[[int], {twin!r}]\n"
            f'Mode = Literal["{twin}"]\n'
            f"Ints = list[{twin!r}]\n",
        )
    # One ordinary instance (no class/function metadata), re-exported by an eager
    # facade, an eager facade that first replaces it, and lazy-table facades.
    _write(tmp_path, "otto/inst_impl.py", "X = object()\n")
    _write(tmp_path, "otto/inst_other.py", "X = object()\nY = object()\n")
    _write(tmp_path, "otto/inst_swap.py", "import otto.inst_impl\notto.inst_impl.X = object()\n")
    _write(tmp_path, "otto/inst_fa.py", '__all__ = ["X"]\nfrom .inst_impl import X\n')
    _write(
        tmp_path,
        "otto/inst_fb.py",
        '__all__ = ["X"]\nfrom . import inst_swap\nfrom .inst_impl import X\n',
    )
    lazy_tables = (
        "from typing import TYPE_CHECKING\n"
        "if TYPE_CHECKING:\n"
        "    from otto.inst_other import X, Y\n"
        '__all__ = ["X", "Y"]\n'
        '_LAZY_ATTRS = {"X": "otto.inst_impl"}\n'
        '_LAZY_EXPORTS = {"Y": ("..inst_impl", "X")}\n'
        "def __getattr__(name):\n"
        "    import importlib\n"
        "    if name in _LAZY_ATTRS:\n"
        "        return getattr(importlib.import_module(_LAZY_ATTRS[name]), name)\n"
        "    module, attr = _LAZY_EXPORTS[name]\n"
        "    return getattr(importlib.import_module(module, __name__), attr)\n"
    )
    _write(tmp_path, "otto/inst_lazy_ok/__init__.py", lazy_tables)
    _write(tmp_path, "otto/inst_lazy/__init__.py", "from .. import inst_swap\n" + lazy_tables)
    # Same-spelled names, different static sites: two instances, and two closures
    # one factory makes (their runtime ``module:qualname`` is the same).
    _write(
        tmp_path,
        "otto/factory.py",
        "def build():\n    def inner():\n        pass\n    return inner\n",
    )
    for twin in ("inst_twin_a", "inst_twin_b"):
        _write(
            tmp_path,
            f"otto/{twin}.py",
            '__all__ = ["X", "Handler"]\nfrom .factory import build\n'
            "X = object()\nHandler = build()\n",
        )
    return tmp_path


def test_a_consistent_alias_passes(fixture_tree):
    reports = namespace_reports(["otto", "otto.alias"], fixture_tree)
    assert agreement_failures(["otto", "otto.alias"], reports) == []


def test_declaration_from_carries_the_private_member_map(fixture_tree):
    reports = namespace_reports(["otto"], fixture_tree)
    decl = declaration_from(
        reports, assume_dir_when_missing=False, private={"otto:Thing": {"_hook"}}
    )
    assert decl.underscore_members("otto", "Thing") == {"_hook"}
    assert decl.underscore_members("otto", "Missing") is None


def test_the_child_imports_the_fixture_tree_not_the_installed_otto(fixture_tree):
    reports = namespace_reports(["otto"], fixture_tree)
    assert reports["otto"].origin == str(fixture_tree / "src" / "otto" / "__init__.py")
    assert reports["otto"].is_package


def test_namespace_that_raises_on_import_is_reported_by_name(fixture_tree):
    reports = namespace_reports(["otto.boom"], fixture_tree)
    (failure,) = agreement_failures(["otto.boom"], reports)
    assert failure.startswith("otto.boom: cannot import: RuntimeError: boom at import")


@pytest.mark.parametrize(
    ("namespace", "message"),
    [
        ("otto.tconly", "otto.tconly:Thing is in __all__ but not bound at runtime"),
        ("otto.computed", "otto.computed: __all__ is not a literal list of strings"),
        ("otto.dup", "otto.dup: __all__ repeats ['Thing']"),
        ("otto.noall", "otto.noall: has no __all__"),
        ("otto.augmented", "otto.augmented: runtime __all__ differs from its literal"),
    ],
)
def test_agreement_failures(fixture_tree, namespace, message):
    reports = namespace_reports([namespace], fixture_tree)
    assert message in agreement_failures([namespace], reports)


def test_one_broken_namespace_cannot_hide_the_others(fixture_tree):
    """A crash at import, in a lazy ``__getattr__``, or on reading ``__all__`` stays local."""
    names = ["otto", "otto.boom", "otto.exits", "otto.lazybroken", "otto.lazyexit", "otto.badall"]
    reports = namespace_reports(names, fixture_tree)
    failures = agreement_failures(names, reports)

    assert reports["otto"].error == ""
    assert reports["otto"].all == ["Thing"]
    assert not [f for f in failures if f.startswith(("otto:", "otto: "))]
    assert "otto.exits: cannot import: SystemExit: 0" in failures
    assert (
        "otto.lazybroken:X is in __all__ but not bound at runtime "
        "(ModuleNotFoundError: No module named 'otto.no_such_module')"
    ) in failures
    assert "otto.lazyexit:X is in __all__ but not bound at runtime (SystemExit: 0)" in failures
    (badall,) = [f for f in failures if f.startswith("otto.badall:")]
    assert "TypeError" in badall


def test_a_same_spelled_different_class_is_not_an_alias(fixture_tree):
    reports = namespace_reports(["otto", "otto.twin"], fixture_tree)
    assert agreement_failures(["otto", "otto.twin"], reports) == []


def test_distinct_type_aliases_of_one_generic_are_not_one_defining_site(fixture_tree):
    """``Callable[...]``, ``Literal[...]`` and ``list[...]`` aliases forward ``__qualname__``.

    Each alias reports its generic's ``module:qualname`` (``collections.abc:Callable``),
    so two different aliases would look like one site bound to two objects. An alias
    is not defined anywhere; it has no site to judge.
    """
    names = ["otto.aliases_a", "otto.aliases_b"]
    reports = namespace_reports(names, fixture_tree)
    assert agreement_failures(names, reports) == []


def test_one_defining_site_bound_to_two_objects_fails(fixture_tree):
    reports = namespace_reports(["otto", "otto.alias"], fixture_tree)
    site, _ = reports["otto.alias"].sites["Thing"]
    reports["otto.alias"].sites["Thing"] = [site, -1]  # same site, different object
    (failure,) = agreement_failures(["otto", "otto.alias"], reports)
    assert "otto.impl:Thing is exported as different objects" in failure


def test_literal_all_names_is_static(tmp_path):
    path = tmp_path / "m.py"
    path.write_text('__all__ = ("a", "b")\n', encoding="utf-8")
    assert literal_all_names(path) == ["a", "b"]
    path.write_text("__all__ = names()\n", encoding="utf-8")
    assert literal_all_names(path) is None


def test_declaration_from_reports(fixture_tree):
    reports = namespace_reports(["otto", "otto.noall"], fixture_tree)
    strict = declaration_from(reports, assume_dir_when_missing=False)
    assert strict.declares("otto", "Thing")
    assert not strict.declares("otto.noall", "f")
    loose = declaration_from(reports, assume_dir_when_missing=True)
    assert loose.declares("otto.noall", "f")


def test_a_replaced_instance_reexported_twice_is_one_site_bound_to_two_objects(fixture_tree):
    """Both facades lead statically to ``otto.inst_impl:X``; the second saw a replacement.

    The object is a plain instance with no ``module:qualname`` of its own, so only
    the static defining site can group the two exports.
    """
    names = ["otto.inst_fa", "otto.inst_fb"]
    reports = namespace_reports(names, fixture_tree)
    assert agreement_failures(names, reports) == [
        "otto.inst_impl:X is exported as different objects: ['otto.inst_fa:X', 'otto.inst_fb:X']"
    ]


def test_same_spelled_names_with_different_static_sites_are_not_aliases(fixture_tree):
    """Spelling, and even a shared runtime ``module:qualname``, never makes an alias."""
    names = ["otto.inst_twin_a", "otto.inst_twin_b"]
    reports = namespace_reports(names, fixture_tree)
    assert agreement_failures(names, reports) == []
    assert reports["otto.inst_twin_a"].static_sites == {
        "X": "otto.inst_twin_a:X",
        "Handler": "otto.inst_twin_a:Handler",
    }


def test_a_lazy_table_reexport_resolves_to_the_eager_site(fixture_tree):
    """``_LAZY_*`` entries (``name -> module`` and ``name -> (module, attr)``) are followed.

    A ``TYPE_CHECKING`` import of the same names from elsewhere is not a binding.
    """
    names = ["otto.inst_fa", "otto.inst_lazy_ok"]
    reports = namespace_reports(names, fixture_tree)
    assert reports["otto.inst_fa"].static_sites == {"X": "otto.inst_impl:X"}
    assert reports["otto.inst_lazy_ok"].static_sites == {
        "X": "otto.inst_impl:X",
        "Y": "otto.inst_impl:X",
    }
    assert agreement_failures(names, reports) == []


def test_a_lazy_reexport_of_a_replaced_instance_is_one_site_bound_to_two_objects(fixture_tree):
    names = ["otto.inst_fa", "otto.inst_lazy"]
    reports = namespace_reports(names, fixture_tree)
    assert agreement_failures(names, reports) == [
        (
            "otto.inst_impl:X is exported as different objects: "
            "['otto.inst_fa:X', 'otto.inst_lazy:X', 'otto.inst_lazy:Y']"
        )
    ]


def test_the_static_scan_reads_a_module_with_an_invalid_escape(tmp_path):
    # pytest runs with filterwarnings=error: the escape's warning must not become a SyntaxError.
    path = _write(tmp_path, "otto/esc.py", '__all__ = ["PATTERN"]\nPATTERN = "\\d"\n')
    assert StaticSites(tmp_path / "src").site("otto.esc", "PATTERN") == "otto.esc:PATTERN"
    assert literal_all_names(path) == ["PATTERN"]


def test_a_child_that_times_out_is_an_agreement_error(tmp_path, monkeypatch):
    def hangs(argv, **kwargs):
        raise subprocess.TimeoutExpired(argv, kwargs["timeout"])

    monkeypatch.setattr(api_agreement.subprocess, "run", hangs)
    with pytest.raises(AgreementError, match="timed out after 300"):
        namespace_reports(["otto"], tmp_path)


def test_a_child_that_prints_no_report_is_an_agreement_error(tmp_path, monkeypatch):
    def silent(argv, **kwargs):
        return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")

    monkeypatch.setattr(api_agreement.subprocess, "run", silent)
    with pytest.raises(AgreementError, match="no report"):
        namespace_reports(["otto"], tmp_path)
