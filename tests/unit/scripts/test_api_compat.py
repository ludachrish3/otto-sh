"""``scripts/api_compat.py``: the dump spec's §4 rules, proved through the real producer."""

import inspect
import itertools

import pytest

from scripts import api_compat, api_records
from scripts.api_manifest import Format
from tests._fixtures.api_dump import dump_of, run_child_json, write_tree

pytestmark = pytest.mark.interpreter_agnostic

_PRE = "import abc, dataclasses, enum, functools\nfrom typing import Protocol\n"


def _dump(root, body, names, extra=None):
    src = write_tree(
        root, {"otto/__init__.py": f"__all__ = {names!r}\n{_PRE}{body}", **(extra or {})}
    )
    return dump_of(src, ["otto"])


def _findings(tmp_path, before, after, names, extra_before=None, extra_after=None):
    old = _dump(tmp_path / "a", before, names, extra_before)
    new = _dump(tmp_path / "b", after, names, extra_after)
    return api_compat.compare_dumps(old, new)


# A callable instance whose __call__ is decorated with functools.wraps: its call kind
# is read from the decorated function the wraps chain ends at.
_WRAPPED_CALL = (
    "def deco(fn):\n"
    "    @functools.wraps(fn)\n"
    "    def inner(*a, **k):\n        return fn(*a, **k)\n"
    "    return inner\n"
    "class R:\n    @deco\n    {define} __call__(self, x): pass\n"
    "r = R()\n"
)

BREAKING = [
    ("po_to_ko", "def f(x, /): pass\n", "def f(*, x): pass\n", ["f"], "otto:f: x narrowed"),
    (
        "po_to_pk_with_kw",
        "def f(x, /, **kw): pass\n",
        "def f(x, **kw): pass\n",
        ["f"],
        "keyword collision",
    ),
    (
        "optional_before_args",
        "def f(a, *args): pass\n",
        "def f(a, b=0, *args): pass\n",
        ["f"],
        "capture",
    ),
    ("ko_before_args", "def f(*args, x=0): pass\n", "def f(x=0, *args): pass\n", ["f"], "capture"),
    (
        "kw_removed_despite_kw",
        "def f(*, x=0, **kw): pass\n",
        "def f(**kw): pass\n",
        ["f"],
        "otto:f: keyword x removed",
    ),
    ("default_removed", "def f(x=1): pass\n", "def f(x): pass\n", ["f"], "x lost its default"),
    ("default_changed", "def f(x=1): pass\n", "def f(x=2): pass\n", ["f"], "x default I:1 -> I:2"),
    ("default_to_opaque", "def f(x=1): pass\n", "def f(x=object()): pass\n", ["f"], "x default"),
    (
        "positional_inserted",
        "def f(a, b): pass\n",
        "def f(a, c, b): pass\n",
        ["f"],
        "positional slot 2 is now c, was b",
    ),
    ("pk_to_po", "def f(x): pass\n", "def f(x, /): pass\n", ["f"], "x narrowed to positional-only"),
    ("new_required", "def f(a): pass\n", "def f(a, b): pass\n", ["f"], "new required parameter b"),
    ("varargs_removed", "def f(*args): pass\n", "def f(): pass\n", ["f"], "*args removed"),
    (
        "sync_to_async",
        "def f(): pass\n",
        "async def f(): pass\n",
        ["f"],
        "call kind sync -> coroutine",
    ),
    (
        "coroutine_to_asyncgen",
        "async def f(): pass\n",
        "async def f():\n    yield 1\n",
        ["f"],
        "call kind coroutine -> asyncgen",
    ),
    (
        "asyncgen_to_coroutine",
        "async def f():\n    yield 1\n",
        "async def f(): pass\n",
        ["f"],
        "call kind asyncgen -> coroutine",
    ),
    (
        "coroutine_to_sync",
        "async def f(): pass\n",
        "def f(): pass\n",
        ["f"],
        "call kind coroutine -> sync",
    ),
    (
        "sync_to_asyncgen",
        "def f(): pass\n",
        "async def f():\n    yield 1\n",
        ["f"],
        "call kind sync -> asyncgen",
    ),
    (
        "asyncgen_to_sync",
        "async def f():\n    yield 1\n",
        "def f(): pass\n",
        ["f"],
        "call kind asyncgen -> sync",
    ),
    (
        "instance_sync_to_async",
        "class R:\n    def __call__(self): pass\nr = R()\n",
        "class R:\n    async def __call__(self): pass\nr = R()\n",
        ["r"],
        "call kind sync -> coroutine",
    ),
    (
        "wrapped_call_sync_to_async",
        _WRAPPED_CALL.format(define="def"),
        _WRAPPED_CALL.format(define="async def"),
        ["r"],
        "call kind sync -> coroutine",
    ),
    (
        "partial_break",
        "def g(a, b): pass\np = functools.partial(g, 1)\n",
        "def g(a, c): pass\np = functools.partial(g, 1)\n",
        ["p"],
        "positional slot 1 is now c",
    ),
    (
        "getter_lost",
        "class C:\n    @property\n    def x(self): return 1\n",
        "class C:\n    x = property(None, lambda s, v: None)\n",
        ["C"],
        "otto:C.x: lost its getter",
    ),
    (
        "deleter_lost",
        (
            "class C:\n    @property\n    def x(self): return 1\n"
            "    @x.deleter\n    def x(self): pass\n"
        ),
        "class C:\n    @property\n    def x(self): return 1\n",
        ["C"],
        "lost its deleter",
    ),
    (
        "member_via_private_base",
        "class _B:\n    def m(self): pass\nclass C(_B): pass\n",
        "class _B: pass\nclass C(_B): pass\n",
        ["C"],
        "otto:C.m: removed",
    ),
    (
        "nested_method",
        "class O:\n    class I:\n        def go(self, a): pass\n",
        "class O:\n    class I:\n        def go(self): pass\n",
        ["O"],
        "otto:O.I.go: positional slot 1 (a) removed",
    ),
    (
        "enum_method",
        "class E(enum.Enum):\n    A = 1\n    def d(self, x): pass\n",
        "class E(enum.Enum):\n    A = 1\n    def d(self): pass\n",
        ["E"],
        "otto:E.d:",
    ),
    (
        "setitem_deleted",
        "class C:\n    def __setitem__(self, k, v): pass\n",
        "class C: pass\n",
        ["C"],
        "otto:C.__setitem__: removed",
    ),
    (
        "member_to_classref",
        "class X: pass\nclass O: pass\nO.S = X\n",
        "class X: pass\nclass O: pass\nO.S = O\n",
        ["O"],
        "otto:O.S: kind changed class -> classref=otto:O",
    ),
    (
        "public_init_false_field_removed",
        "@dataclasses.dataclass\nclass D:\n    z: int = dataclasses.field(default=0, init=False)\n",
        "@dataclasses.dataclass\nclass D: pass\n",
        ["D"],
        "otto:D.z: removed",
    ),
    (
        "exception_loses_valueerror",
        "class E(ValueError): pass\n",
        "class E(Exception): pass\n",
        ["E"],
        "no longer a subclass of @builtin:builtins.ValueError",
    ),
    (
        "abc_first_abstract",
        "class A(abc.ABC):\n    def m(self): pass\n",
        "class A(abc.ABC):\n    @abc.abstractmethod\n    def m(self): pass\n",
        ["A"],
        "otto:A: new obligation m",
    ),
    (
        "inherited_turns_abstract",
        "class B(abc.ABC):\n    def m(self): pass\nclass A(B): pass\n",
        "class B(abc.ABC):\n    @abc.abstractmethod\n    def m(self): pass\nclass A(B): pass\n",
        ["A", "B"],
        "otto:A: new obligation m",
    ),
    (
        "protocol_member_added_with_default",
        "class P(Protocol):\n    def a(self): ...\n",
        "class P(Protocol):\n    def a(self): ...\n    def b(self): return 1\n",
        ["P"],
        "otto:P: new obligation b",
    ),
    (
        "protocol_attribute_removed",
        "class P(Protocol):\n    x: int\n",
        "class P(Protocol): pass\n",
        ["P"],
        "otto:P.x: removed",
    ),
    (
        "enum_alias_deleted",
        "class E(enum.Enum):\n    A = 1\n    B = 1\n",
        "class E(enum.Enum):\n    A = 1\n",
        ["E"],
        "otto:E: enum member B removed",
    ),
    (
        "zero_flag_deleted",
        "class F(enum.Flag):\n    NONE = 0\n    A = 1\n",
        "class F(enum.Flag):\n    A = 1\n",
        ["F"],
        "enum member NONE removed",
    ),
    (
        "enum_value_via_constant",
        "V = 1\nclass E(enum.Enum):\n    A = V\n",
        "V = 2\nclass E(enum.Enum):\n    A = V\n",
        ["E"],
        "otto:E: enum member A value I:1 -> I:2",
    ),
    (
        "auto_reorder",
        "class E(enum.Enum):\n    A = enum.auto()\n    B = enum.auto()\n",
        "class E(enum.Enum):\n    B = enum.auto()\n    A = enum.auto()\n",
        ["E"],
        "enum member A value",
    ),
    (
        "enum_default_value",
        "class M(enum.Enum):\n    A = 1\ndef f(m=M.A): pass\n",
        "class M(enum.Enum):\n    A = 2\ndef f(m=M.A): pass\n",
        ["M", "f"],
        "otto:f: m default",
    ),
    ("binding_removed", "def f(): pass\ndef g(): pass\n", "def f(): pass\n", ["f"], None),
    (
        "builtin_constructor_changed",
        "class E(str): pass\n",
        "class E(int): pass\n",
        ["E"],
        "constructor builtins.str -> builtins.int",
    ),
]


@pytest.mark.parametrize(
    ("before", "after", "names", "expected"),
    [pytest.param(b, a, n, e, id=i) for i, b, a, n, e in BREAKING if e is not None],
)
def test_breaking_change_is_found(tmp_path, before, after, names, expected):
    findings = _findings(tmp_path, before, after, names)
    assert any(expected in f for f in findings), findings


def test_removed_binding_is_found(tmp_path):
    old = _dump(tmp_path / "a", "def f(): pass\ndef g(): pass\n", ["f", "g"])
    new = _dump(tmp_path / "b", "def f(): pass\n", ["f"])
    assert api_compat.compare_dumps(old, new) == ["otto:g: removed"]


def test_kind_change_is_found(tmp_path):
    old = _dump(tmp_path / "a", "def f(): pass\n", ["f"])
    new = _dump(tmp_path / "b", "class f: pass\n", ["f"])
    assert api_compat.compare_dumps(old, new) == ["otto:f: kind changed function -> class"]


PYDANTIC = "from pydantic import AliasChoices, BaseModel, ConfigDict, Field\n"
INPUT_BREAKS = [
    (
        "renamed_keeping_alias",
        "class M(BaseModel):\n    a: int = Field(alias='x')\n",
        "class M(BaseModel):\n    b: int = Field(alias='x')\n",
        'otto:M input S:"a": removed',
    ),
    (
        "default_changed",
        "class M(BaseModel):\n    x: int = 0\n",
        "class M(BaseModel):\n    x: int = 1\n",
        'otto:M input S:"x": default I:0 -> I:1',
    ),
    (
        "route_removed",
        "class M(BaseModel):\n    x: int = Field(0, validation_alias=AliasChoices('a', 'b'))\n",
        "class M(BaseModel):\n    x: int = Field(0, validation_alias=AliasChoices('a'))\n",
        'route S:"b" removed',
    ),
    (
        "validate_by_name_off",
        (
            "class M(BaseModel):\n    model_config = ConfigDict(validate_by_name=True)\n"
            "    x: int = Field(0, alias='a')\n"
        ),
        "class M(BaseModel):\n    x: int = Field(0, alias='a')\n",
        'route S:"x" removed',
    ),
    (
        "became_required",
        "class M(BaseModel):\n    x: int = 0\n",
        "class M(BaseModel):\n    x: int\n",
        "became required",
    ),
    (
        "new_required_typeddict_key",
        "from typing_extensions import TypedDict\nclass M(TypedDict):\n    a: int\n",
        "from typing_extensions import TypedDict\nclass M(TypedDict):\n    a: int\n    b: int\n",
        'otto:M input S:"b": new required input',
    ),
]


@pytest.mark.parametrize(
    ("before", "after", "expected"), [pytest.param(b, a, e, id=i) for i, b, a, e in INPUT_BREAKS]
)
def test_input_break_is_found(tmp_path, before, after, expected):
    findings = _findings(tmp_path, PYDANTIC + before, PYDANTIC + after, ["M"])
    assert any(expected in f for f in findings), findings


SAFE = [
    ("optional_ko_added", "def f(a): pass\n", "def f(a, *, b=0): pass\n", ["f"]),
    ("optional_positional_appended", "def f(a): pass\n", "def f(a, b=0): pass\n", ["f"]),
    ("keyword_interception_d6", "def f(**kw): pass\n", "def f(x=0, **kw): pass\n", ["f"]),
    ("keyword_interception_ko", "def f(**kw): pass\n", "def f(*, x=0, **kw): pass\n", ["f"]),
    ("po_rename", "def f(x, /): pass\n", "def f(y, /): pass\n", ["f"]),
    ("collector_rename", "def f(*args, **kw): pass\n", "def f(*items, **opts): pass\n", ["f"]),
    ("true_widening", "def f(x, /): pass\n", "def f(x): pass\n", ["f"]),
    ("ko_reorder", "def f(*, a=0, b=0): pass\n", "def f(*, b=0, a=0): pass\n", ["f"]),
    ("default_added", "def f(x): pass\n", "def f(x=0): pass\n", ["f"]),
    (
        "setter_added",
        "class C:\n    @property\n    def x(self): return 1\n",
        (
            "class C:\n    @property\n    def x(self): return 1\n"
            "    @x.setter\n    def x(self, v): pass\n"
        ),
        ["C"],
    ),
    ("new_member", "class C: pass\n", "class C:\n    def m(self): pass\n", ["C"]),
    ("new_binding", "def f(): pass\n", "def f(): pass\ndef g(): pass\n", ["f"]),
    (
        "concrete_method_on_abc",
        "class A(abc.ABC):\n    @abc.abstractmethod\n    def m(self): ...\n",
        "class A(abc.ABC):\n    @abc.abstractmethod\n    def m(self): ...\n    def n(self): pass\n",
        ["A"],
    ),
    (
        "obligation_dropped",
        "class A(abc.ABC):\n    @abc.abstractmethod\n    def m(self): ...\n",
        "class A(abc.ABC):\n    def m(self): pass\n",
        ["A"],
    ),
    (
        "private_base_relocation",
        "class _B:\n    def m(self): pass\nclass C(_B): pass\n",
        "class _B2:\n    def m(self): pass\nclass _B(_B2): pass\nclass C(_B): pass\n",
        ["C"],
    ),
    (
        "underscore_helper_changed",
        "class C:\n    def _h(self, a): pass\n",
        "class C:\n    def _h(self): pass\n",
        ["C"],
    ),
    (
        "private_init_false_storage",
        (
            "@dataclasses.dataclass\nclass D:\n"
            "    _c: dict = dataclasses.field(default_factory=dict, init=False)\n"
        ),
        (
            "@dataclasses.dataclass\nclass D:\n"
            "    _c: list = dataclasses.field(default_factory=list, init=False)\n"
        ),
        ["D"],
    ),
    (
        "enum_member_added",
        "class E(enum.Enum):\n    A = 1\n",
        "class E(enum.Enum):\n    A = 1\n    B = 2\n",
        ["E"],
    ),
    (
        "enum_alias_added",
        "class E(enum.Enum):\n    A = 1\n",
        "class E(enum.Enum):\n    A = 1\n    B = 1\n",
        ["E"],
    ),
    ("constant_value", "LIMIT = 5\n", "LIMIT = 6\n", ["LIMIT"]),
    ("annotation_changed", "def f(x: int): pass\n", "def f(x: str): pass\n", ["f"]),
    ("cycle_terminates", "class O: pass\nO.S = O\n", "class O: pass\nO.S = O\n", ["O"]),
]


@pytest.mark.parametrize(
    ("before", "after", "names"), [pytest.param(b, a, n, id=i) for i, b, a, n in SAFE]
)
def test_safe_change_has_no_finding(tmp_path, before, after, names):
    assert _findings(tmp_path, before, after, names) == []


def test_new_public_alias_of_an_mro_base_is_not_an_ancestry_removal(tmp_path):
    impl = {"otto/_impl.py": "class Base:\n    pass\n"}
    body = "from ._impl import Base\nclass Child(Base): pass\n"
    old = _dump(tmp_path / "a", body, ["Base", "Child"], impl)
    src = write_tree(
        tmp_path / "b",
        {
            "otto/__init__.py": f"__all__ = ['Base', 'Child']\n{body}",
            "otto/sub.py": "__all__ = ['Base']\nfrom ._impl import Base\n",
            **impl,
        },
    )
    new = dump_of(src, ["otto", "otto.sub"])
    assert new.get("mro", "otto:Child").fields == ["{otto.sub:Base,otto:Base}"]
    assert api_compat.compare_dumps(old, new) == []


def test_new_alias_of_an_enum_used_as_a_default_is_not_a_default_change(tmp_path):
    files = {"otto/_impl.py": "import enum\nclass M(enum.Enum):\n    A = 1\n"}
    body = "from ._impl import M\ndef f(m=M.A): pass\n"
    old = _dump(tmp_path / "a", body, ["M", "f"], files)
    src = write_tree(
        tmp_path / "b",
        {
            "otto/__init__.py": f"__all__ = ['M', 'f']\n{body}",
            "otto/sub.py": "__all__ = ['M']\nfrom ._impl import M\n",
            **files,
        },
    )
    new = dump_of(src, ["otto", "otto.sub"])
    assert api_compat.compare_dumps(old, new) == []


def test_group_findings_merges_identical_findings_across_exposure_paths():
    findings = ["otto.host:Host.run: keyword x removed", "otto:Host.run: keyword x removed"]
    assert api_compat.group_findings(findings) == ["Host.run: keyword x removed [otto, otto.host]"]


def test_a_dataclass_that_becomes_hashable_has_no_finding(tmp_path):
    # eq=True leaves __hash__ = None: hashing switched off, recorded as absent, so
    # frozen=True (which makes the class hashable) only adds __hash__.
    before = "@dataclasses.dataclass\nclass D:\n    x: int\n"
    after = "@dataclasses.dataclass(frozen=True)\nclass D:\n    x: int\n"
    assert _findings(tmp_path, before, after, ["D"]) == []


@pytest.mark.parametrize(
    "after",
    ["class C:\n    __hash__ = None\n", "class C:\n    pass\n"],
    ids=["set_to_none", "deleted"],
)
def test_losing_a_real_hash_method_is_a_removal(tmp_path, after):
    before = "class C:\n    def __hash__(self):\n        return 0\n"
    assert _findings(tmp_path, before, after, ["C"]) == ["otto:C.__hash__: removed"]


def test_a_removed_nested_class_is_one_finding_in_one_namespace(tmp_path):
    # The nested class is a binding AND a member of Outer; both records vanish.
    before = "class Outer:\n    class Inner: pass\n"
    findings = _findings(tmp_path, before, "class Outer: pass\n", ["Outer"])
    assert findings == ["otto:Outer.Inner: removed"]
    assert api_compat.group_findings(findings) == ["Outer.Inner: removed [otto]"]


def _tree_findings(tmp_path, before, after, namespaces):
    old = dump_of(write_tree(tmp_path / "a", before), namespaces)
    new = dump_of(write_tree(tmp_path / "b", after), namespaces)
    return api_compat.compare_dumps(old, new)


EXACT_BREAKING = [
    (
        "ko_required_added",
        "def f(a): pass\n",
        "def f(a, *, b): pass\n",
        ["f"],
        ["otto:f: new required parameter b"],
    ),
    (
        "ko_to_po",
        "def f(*, x): pass\n",
        "def f(x, /): pass\n",
        ["f"],
        ["otto:f: x narrowed from keyword-only to positional-only"],
    ),
    (
        "ko_into_occupied_po_slot",
        "def f(a, /, *, b): pass\n",
        "def f(b): pass\n",
        ["f"],
        [
            (
                "otto:f: keyword-only b moved into positional slot 1, which old calls fill "
                "beside b= (keyword collision)"
            )
        ],
    ),
    (
        "varkw_removed",
        "def f(**kw): pass\n",
        "def f(): pass\n",
        ["f"],
        ["otto:f: **kwargs removed"],
    ),
    (
        "opaque_to_simple",
        "def f(x=object()): pass\n",
        "def f(x=0): pass\n",
        ["f"],
        ["otto:f: x default O -> I:0"],
    ),
    (
        "simple_to_opaque_exact",
        "def f(x=0): pass\n",
        "def f(x=object()): pass\n",
        ["f"],
        ["otto:f: x default I:0 -> O"],
    ),
    (
        "composite_flag_deleted",
        "class F(enum.Flag):\n    R = 1\n    W = 2\n    RW = 3\n",
        "class F(enum.Flag):\n    R = 1\n    W = 2\n",
        ["F"],
        ["otto:F: enum member RW removed"],
    ),
    (
        "facade_retargeted_to_function",
        "class A: pass\n",
        "def A(): pass\n",
        ["A"],
        ["otto:A: kind changed class -> function"],
    ),
    (
        "facade_retargeted_call_shape",
        "class A:\n    def __init__(self, a): pass\n",
        "class A:\n    def __init__(self, a, b): pass\n",
        ["A"],
        ["otto:A: new required parameter b"],
    ),
]


@pytest.mark.parametrize(
    ("before", "after", "names", "expected"),
    [pytest.param(b, a, n, e, id=i) for i, b, a, n, e in EXACT_BREAKING],
)
def test_breaking_change_is_found_exactly(tmp_path, before, after, names, expected):
    assert _findings(tmp_path, before, after, names) == expected


def test_retargeting_one_of_two_facades_names_only_that_namespace(tmp_path):
    impl = {"otto/_impl.py": "class A:\n    pass\ndef g(): pass\n"}
    before = {
        "otto/__init__.py": "__all__ = ['A']\nfrom ._impl import A\n",
        "otto/sub.py": "__all__ = ['A']\nfrom ._impl import A\n",
        **impl,
    }
    after = {**before, "otto/sub.py": "__all__ = ['A']\nfrom ._impl import g as A\n"}
    findings = _tree_findings(tmp_path, before, after, ["otto", "otto.sub"])
    assert findings == ["otto.sub:A: kind changed class -> function"]
    assert api_compat.group_findings(findings) == ["A: kind changed class -> function [otto.sub]"]


SAFE_MORE = [
    (
        "impl_move_same_shape",
        "class A:\n    def m(self, x): pass\n",
        "from ._impl import A\n",
        ["A"],
        {"otto/_impl.py": "class A:\n    def m(self, x): pass\n"},
    ),
    (
        "new_alias_choices_route",
        PYDANTIC
        + "class M(BaseModel):\n    x: int = Field(0, validation_alias=AliasChoices('a'))\n",
        PYDANTIC
        + "class M(BaseModel):\n    x: int = Field(0, validation_alias=AliasChoices('a', 'b'))\n",
        ["M"],
        None,
    ),
    (
        "mixin_concrete_added",
        "class Mix: pass\nclass C(Mix): pass\n",
        "class Mix:\n    def extra(self): pass\nclass C(Mix): pass\n",
        ["C"],
        None,
    ),
    (
        "deleter_added",
        "class C:\n    @property\n    def x(self): return 1\n",
        (
            "class C:\n    @property\n    def x(self): return 1\n"
            "    @x.deleter\n    def x(self): pass\n"
        ),
        ["C"],
        None,
    ),
    (
        "enum_equivalent_value",
        "class E(enum.Enum):\n    A = 1 + 0\n",
        "class E(enum.Enum):\n    A = 1\n",
        ["E"],
        None,
    ),
]


@pytest.mark.parametrize(
    ("before", "after", "names", "extra"),
    [pytest.param(b, a, n, x, id=i) for i, b, a, n, x in SAFE_MORE],
)
def test_more_safe_changes_have_no_finding(tmp_path, before, after, names, extra):
    assert _findings(tmp_path, before, after, names, extra_after=extra) == []


def _fmt_dump(*lines):
    return api_records.parse_dump(
        api_records.render_dump([api_records.parse_record(line) for line in lines])
    )


@pytest.mark.parametrize(
    ("old", "new", "expected"),
    [
        (
            'format\tlink\tS:"v1" S:"v2"\tS:"v1"',
            'format\tlink\tS:"v2"\tS:"v1"',
            ['format link: reads no longer has S:"v1"'],
        ),
        (
            "format\tstore\tI:7 I:8\tI:8",
            "format\tstore\tI:7 I:8\t-",
            ["format store: writes no longer has I:8"],
        ),
        ("format\tstore\tI:8\tI:8", None, ["format store: removed"]),
        ("format\tstore\tI:8\tI:8", "format\tstore\tI:8 I:9\tI:8 I:9", []),
        (None, "format\tstore\tI:8\tI:8", []),
        ("format\tstore\tI:8\tI:8", "format\tstore\tI:8\tI:8", []),
    ],
)
def test_format_rules(old, new, expected):
    before = _fmt_dump(*([old] if old else []))
    after = _fmt_dump(*([new] if new else []))
    assert api_compat.compare_dumps(before, after) == expected


def test_a_format_constant_moved_to_another_module_is_not_a_change(tmp_path):
    def dump(files, pointer):
        src = write_tree(tmp_path / pointer.replace(":", "_"), files)
        data = run_child_json(src, ["otto"], formats=[Format("store", pointer, pointer)])
        assert data["refusals"] == []
        records = [api_records.parse_record(r) for r in data["records"]]
        return api_records.parse_dump(api_records.render_dump(records))

    old = dump({"otto/__init__.py": "__all__ = []\n", "otto/a.py": "V = [8]\n"}, "otto.a:V")
    new = dump({"otto/__init__.py": "__all__ = []\n", "otto/b.py": "W = [8]\n"}, "otto.b:W")
    assert api_compat.compare_dumps(old, new) == []


def test_a_new_abstract_class_owes_nothing_but_an_existing_one_does(tmp_path):
    old = _dump(tmp_path / "a", "class C(abc.ABC):\n    def m(self): pass\n", ["C"])
    new = _dump(
        tmp_path / "b",
        "class C(abc.ABC):\n    @abc.abstractmethod\n    def m(self): pass\n"
        "class A(abc.ABC):\n    @abc.abstractmethod\n    def n(self): ...\n",
        ["A", "C"],
    )
    assert "otto:A" not in old.bindings
    assert new.get("abstract", "otto:A").fields == ["n"]
    assert api_compat.compare_dumps(old, new) == ["otto:C: new obligation m for implementers"]


# The call invariant (§4.1), judged by an executable oracle: real functions for
# both sides and every small call the old one accepts. A call breaks when the new
# function refuses it, or when a positional argument lands in another slot (a
# positional-only slot is its position; a positional-or-keyword slot is its name).
# Defaults never change here, so the conservative default rules stay out of scope.
CALL_SHAPES = [
    ("ko_into_po_slot", "a, /, *, b", "b", True),
    ("optional_ko_into_po_slot", "a, /, *, b=0", "b=0", True),
    ("optional_ko_into_optional_po_slot", "a=0, /, *, b=0", "b=0", True),
    ("ko_into_po_slot_beside_args", "a, /, *args, b=0", "b=0, *args", True),
    ("ko_into_po_slot_beside_kwargs", "a, /, *, b, **kw", "b, **kw", True),
    ("ko_into_second_po_slot", "a, c, /, *, b", "a, b, /", True),
    ("ko_into_pk_slot_after_po", "a, c, /, *, b", "a, b", True),
    ("ko_appended_as_pk", "a, /, *, b", "a, /, b", False),
    ("ko_appended_after_pk", "a, *, b", "a, b", False),
    ("ko_ahead_of_pk", "a, *, b", "b, a", True),
    ("ko_to_po", "a, /, *, b", "b, /", True),
    ("ko_to_trailing_po", "a, *, b=0", "a, b=0, /", True),
    ("ko_ahead_of_args", "*args, b=0", "b=0, *args", True),
    ("ko_ahead_of_args_as_po", "a, /, *args, b=0", "b=0, /, *args", True),
    ("pk_to_po", "a", "a, /", True),
    ("pk_to_ko", "a, b", "a, *, b", True),
    ("pk_swapped_with_ko", "a, /, b, *, c", "a, /, c, *, b", True),
    ("po_and_pk_swapped", "a, /, b", "b, a", True),
    ("po_to_ko", "a, /", "*, a", True),
    ("po_widened", "a, /", "a", False),
    ("po_widened_and_renamed", "a, /", "b", False),
    ("po_pair_reordered", "a, b, /", "b, a", False),
    ("po_widened_beside_args", "a, /, *args", "a, *args", False),
    ("po_widened_beside_kwargs", "a, /, **kw", "a, **kw", True),
    ("po_widened_beside_args_and_kwargs", "a, /, *args, **kw", "a, *args, **kw", True),
    ("optional_po_widened_beside_ko", "a=0, /, *, b=0", "a=0, /, b=0", False),
    ("keyword_interception", "**kw", "x=0, **kw", False),
    ("optional_before_args", "*args", "a=0, *args", True),
    ("positional_inserted", "a, b=0", "a, c=0, b=0", True),
    ("new_required_ko", "a", "a, *, b", True),
]


def _slot(sig, index):
    kinds = (inspect.Parameter.POSITIONAL_ONLY, inspect.Parameter.POSITIONAL_OR_KEYWORD)
    slots = [p for p in sig.parameters.values() if p.kind in kinds]
    return slots[index] if index < len(slots) else None


def _same_slot(old_sig, new_sig, index):
    was, now = _slot(old_sig, index), _slot(new_sig, index)
    if was is None or now is None:
        return was is now
    return was.kind == inspect.Parameter.POSITIONAL_ONLY or was.name == now.name


def _broken_calls(old_src, new_src):
    """Return each small call the old signature accepts that the new one refuses or reroutes."""
    space = {}
    source = f"def old({old_src}): pass\ndef new({new_src}): pass\n"
    exec(source, space)  # noqa: S102 -- the oracle needs real functions from fixed test text
    old_sig, new_sig = inspect.signature(space["old"]), inspect.signature(space["new"])
    variadic = (inspect.Parameter.VAR_POSITIONAL, inspect.Parameter.VAR_KEYWORD)
    named = {p.name for s in (old_sig, new_sig) for p in s.parameters.values()}
    pool = sorted(
        {n for s in (old_sig, new_sig) for n, p in s.parameters.items() if p.kind not in variadic}
        | {"zz"}
    )
    assert "zz" not in named
    broken = []
    for count in range(len(old_sig.parameters) + 3):
        for size in range(len(pool) + 1):
            for keys in itertools.combinations(pool, size):
                args, kwargs = list(range(count)), dict.fromkeys(keys, 0)
                try:
                    old_sig.bind(*args, **kwargs)
                except TypeError:
                    continue
                try:
                    new_sig.bind(*args, **kwargs)
                except TypeError:
                    broken.append((count, keys))
                    continue
                if not all(_same_slot(old_sig, new_sig, i) for i in range(count)):
                    broken.append((count, keys))
    return broken


@pytest.fixture(scope="module")
def call_shape_findings(tmp_path_factory):
    """Dump every CALL_SHAPES pair once per side; return the findings by case."""
    root = tmp_path_factory.mktemp("call_shapes")
    names = [f"f_{case}" for case, *_ in CALL_SHAPES]
    old = _dump(root / "a", "".join(f"def f_{c}({o}): pass\n" for c, o, _, _ in CALL_SHAPES), names)
    new = _dump(root / "b", "".join(f"def f_{c}({n}): pass\n" for c, _, n, _ in CALL_SHAPES), names)
    found: dict[str, list[str]] = {}
    for finding in api_compat.compare_dumps(old, new):
        found.setdefault(finding.split(":")[1].removeprefix("f_"), []).append(finding)
    return found


@pytest.mark.parametrize(
    ("case", "old_src", "new_src", "breaks"),
    [pytest.param(c, o, n, b, id=c) for c, o, n, b in CALL_SHAPES],
)
def test_a_call_shape_is_found_exactly_when_an_old_call_breaks(
    call_shape_findings, case, old_src, new_src, breaks
):
    broken = _broken_calls(old_src, new_src)
    assert bool(broken) is breaks, broken
    assert bool(call_shape_findings.get(case)) is breaks, (broken, call_shape_findings.get(case))
