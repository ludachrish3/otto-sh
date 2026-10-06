"""``scripts/api_dump_child.py``: the records the producer writes (dump spec §3)."""

import builtins
import inspect
import sys
import warnings

import pydantic
import pytest

from scripts import api_records
from scripts.api_manifest import Format
from tests._fixtures.api_dump import dump_of, run_child_json, write_tree

pytestmark = pytest.mark.interpreter_agnostic


def _lines(dump, key):
    return [
        api_records.render_record(r)
        for r in sorted(dump.records.values(), key=api_records._sort_key)
        if api_records.binding_of(r) == key
    ]


def _one(tmp_path, body, names, extra=None):
    init = f"__all__ = {names!r}\n" + body
    src = write_tree(tmp_path, {"otto/__init__.py": init, **(extra or {})})
    return dump_of(src, ["otto"])


def test_bindings_are_classified(tmp_path):
    body = (
        "import enum, functools\n"
        "from typing import Callable, Protocol\n"
        "from typing_extensions import TypedDict\n"
        "from . import sub\n"
        "Handler = Callable[[int], None]\n"
        "def f(): pass\n"
        "async def g(a, b): pass\n"
        "bound = functools.partial(g, 1)\n"
        "class Runner:\n    def __call__(self, x): pass\n"
        "runner = Runner()\n"
        "LIMIT = 5\n"
        "class Color(enum.Enum):\n    RED = 1\n"
        "class P(Protocol):\n    def run(self) -> None: ...\n"
        "class TD(TypedDict):\n    a: int\n"
    )
    names = ["sub", "Handler", "f", "bound", "runner", "LIMIT", "Color", "P", "TD", "Runner"]
    dump = _one(tmp_path, body, names, {"otto/sub.py": ""})
    assert dump.bindings == {
        "otto:sub": "module",
        "otto:Handler": "alias",
        "otto:f": "function",
        "otto:bound": "callable",
        "otto:runner": "callable",
        "otto:LIMIT": "value",
        "otto:Color": "enum",
        "otto:P": "protocol",
        "otto:TD": "typeddict",
        "otto:Runner": "class",
    }


def test_function_call_records_every_parameter_kind_and_default(tmp_path):
    dump = _one(tmp_path, "def f(a, /, b, *args, c=1, d='x y', **kw): pass\n", ["f"])
    assert _lines(dump, "otto:f") == [
        "name\totto:f\tfunction",
        'call\totto:f\tsync\tPO:a:- PK:b:- VP:args:- KO:c:I:1 KO:d:S:"x\\u0020y" VK:kw:-',
    ]


def test_coroutine_and_async_generator_callkinds(tmp_path):
    body = "async def c(): pass\nasync def g():\n    yield 1\n"
    dump = _one(tmp_path, body, ["c", "g"])
    assert dump.get("call", "otto:c").fields == ["coroutine", "-"]
    assert dump.get("call", "otto:g").fields == ["asyncgen", "-"]


def test_callable_instance_callkind_comes_from_its_type(tmp_path):
    body = "class R:\n    async def __call__(self, x): pass\nr = R()\n"
    dump = _one(tmp_path, body, ["r"])
    assert dump.get("call", "otto:r").fields == ["coroutine", "PK:x:-"]


@pytest.mark.parametrize(("define", "callkind"), [("def", "sync"), ("async def", "coroutine")])
def test_a_wrapped_instance_call_is_judged_by_the_function_it_wraps(tmp_path, define, callkind):
    # functools.wraps on __call__: the instance has no __wrapped__, its type's __call__ does.
    body = (
        "import functools\n"
        "def deco(fn):\n"
        "    @functools.wraps(fn)\n"
        "    def inner(*a, **k):\n        return fn(*a, **k)\n"
        "    return inner\n"
        f"class R:\n    @deco\n    {define} __call__(self, x): pass\n"
        "r = R()\n"
    )
    dump = _one(tmp_path, body, ["r"])
    assert dump.get("call", "otto:r").fields == [callkind, "PK:x:-"]


def _refusals(tmp_path, body, names):
    src = write_tree(tmp_path, {"otto/__init__.py": f"__all__ = {names!r}\n" + body})
    data = run_child_json(src, ["otto"])
    assert not [r for r in data["records"] if r.startswith("call\t")], data["records"]
    return data["refusals"]


# Each shape below is refused, its sync and its async variant alike: the refusal is
# the observable change, never a guessed call kind.
_REFUSED_SHAPES = {
    "own_wrapped_instance": (
        (
            "import functools\n"
            "class D:\n"
            "    def __init__(self, fn):\n        functools.update_wrapper(self, fn)\n"
            "    def __call__(self, *a, **k):\n        return self.__wrapped__(*a, **k)\n"
            "@D\n{define} r(x, y=1): pass\n"
        ),
        "otto:r: call kind cannot be determined for a callable D instance carrying __wrapped__",
    ),
    "partial_carrying_wrapped": (
        (
            "import functools\n"
            "class Forward:\n"
            "    def __init__(self, fn):\n        self.fn = fn\n"
            "    def __call__(self, *a, **k):\n        return self.fn(*a, **k)\n"
            "{define} fn(x=0): pass\n"
            "r = functools.update_wrapper(functools.partial(Forward(fn)), fn)\n"
        ),
        "otto:r: call kind cannot be determined for a partial carrying __wrapped__",
    ),
    "instance_with_explicit_signature": (
        (
            "import inspect\n"
            "def shape(x): pass\n"
            "class S:\n"
            "    __signature__ = inspect.signature(shape)\n"
            "    {define} __call__(self, x): pass\n"
            "r = S()\n"
        ),
        "otto:r: call kind cannot be determined for a callable S instance carrying __signature__",
    ),
    "unbound_partialmethod": (
        (
            "import functools\n"
            "{define} fn(self, x, y=1): pass\n"
            "class K:\n    pm = functools.partialmethod(fn, y=2)\n"
            "r = K.pm\n"
        ),
        "otto:r: call kind cannot be determined for a function carrying {partialmethod}",
    ),
    "function_like_instance": (
        (
            "{define} fn(x, y=1): pass\n"
            "class FunctionLike:\n"
            "    def __init__(self, f):\n"
            "        self.__code__ = f.__code__\n        self.__defaults__ = f.__defaults__\n"
            "        self.__kwdefaults__ = None\n        self.__annotations__ = {{}}\n"
            "        self.__name__ = 'r'\n"
            "    def __call__(self, *a, **k): pass\n"
            "r = FunctionLike(fn)\n"
        ),
        (
            "otto:r: call kind cannot be determined for a callable FunctionLike instance "
            "carrying __code__"
        ),
    ),
    "call_descriptor_differs_by_access": (
        (
            "import types\n"
            "def on_class(self, x): pass\n"
            "{define} on_instance(self, x): pass\n"
            "class Desc:\n"
            "    def __get__(self, obj, cls):\n"
            "        return on_class if obj is None else types.MethodType(on_instance, obj)\n"
            "class C:\n    __call__ = Desc()\n"
            "r = C()\n"
        ),
        "otto:r: call kind cannot be determined for a callable C instance whose __call__ is a Desc",
    ),
}


@pytest.mark.parametrize("define", ["def", "async def"])
@pytest.mark.parametrize("shape", sorted(_REFUSED_SHAPES))
def test_a_callable_outside_the_supported_shapes_is_refused(tmp_path, shape, define):
    body, refusal = _REFUSED_SHAPES[shape]
    pm = "_partialmethod" if sys.version_info < (3, 13) else "__partialmethod__"
    refusals = _refusals(tmp_path, body.format(define=define), ["r"])
    assert refusals == [refusal.format(partialmethod=pm)]


@pytest.mark.parametrize("define", ["def", "async def"])
def test_a_class_is_sync_whatever_its_wrapped_points_at(tmp_path, define):
    body = (
        f"{define} target(x, y=1): pass\n"
        "class C:\n    __wrapped__ = target\n    def __init__(self, x, y=1): pass\n"
    )
    dump = _one(tmp_path, body, ["C"])
    assert dump.get("call", "otto:C").fields[0] == "sync"


# Defaults whose __eq__ misbehaves. The same-target check compares ENCODED params,
# so none of these __eq__ ever runs: each default is recorded, never refused.
_ODD_DEFAULTS = (
    "class NotSelf:\n    def __eq__(self, other):\n        return False\n"
    "    __hash__ = object.__hash__\n"
    "class Exits:\n    def __eq__(self, other):\n        raise SystemExit(7)\n"
    "    __hash__ = object.__hash__\n"
    "class Raises:\n    def __eq__(self, other):\n        raise RuntimeError('eq')\n"
    "    __hash__ = object.__hash__\n"
)


@pytest.mark.parametrize(
    ("default", "encoded"),
    [
        ("float('nan')", "F:nan"),
        ("NotSelf()", "O"),
        ("Exits()", "O"),
        ("Raises()", "O"),
    ],
)
def test_a_default_is_recorded_whatever_its_eq_does(tmp_path, default, encoded):
    body = (
        "import functools\n"
        + _ODD_DEFAULTS
        + f"def f(x={default}): pass\n"
        + "def g(x, y): pass\n"
        + f"p = functools.partial(g, y={default})\n"
        + f"class R:\n    def __call__(self, x={default}): pass\n"
        + "r = R()\n"
    )
    dump = _one(tmp_path, body, ["f", "p", "r"])
    assert dump.get("call", "otto:f").fields == ["sync", f"PK:x:{encoded}"]
    assert dump.get("call", "otto:p").fields == ["sync", f"PK:x:- KO:y:{encoded}"]
    assert dump.get("call", "otto:r").fields == ["sync", f"PK:x:{encoded}"]


def test_a_binding_whose_introspection_raises_system_exit_is_refused(tmp_path):
    body = (
        "class R:\n"
        "    @property\n"
        "    def __signature__(self):\n        raise SystemExit(7)\n"
        "    def __call__(self): pass\n"
        "r = R()\n"
        "class C:\n    s = staticmethod(R())\n    def kept(self): pass\n"
        "LIMIT = 5\n"
    )
    src = write_tree(tmp_path, {"otto/__init__.py": '__all__ = ["r", "C", "LIMIT"]\n' + body})
    data = run_child_json(src, ["otto"])
    assert data["refusals"] == [
        "otto:C.s: cannot be recorded: SystemExit: 7",
        "otto:r: cannot be recorded: SystemExit: 7",
    ]
    assert "name\totto:LIMIT\tvalue" in data["records"]
    assert "member\totto:C.kept\tmethod\tsync\t-" in data["records"]


@pytest.mark.parametrize(
    "spoil",
    ["r.__signature__ = 42", "r.__wrapped__ = r"],
    ids=["signature-not-a-signature", "wrapped-loop"],
)
@pytest.mark.parametrize("define", ["def", "async def"])
def test_a_callable_without_a_readable_signature_is_refused_not_a_value(tmp_path, spoil, define):
    body = f"class R:\n    {define} __call__(self, x): pass\nr = R()\n{spoil}\nLIMIT = 5\n"
    src = write_tree(tmp_path, {"otto/__init__.py": '__all__ = ["r", "LIMIT"]\n' + body})
    data = run_child_json(src, ["otto"])
    assert data["refusals"] == ["otto:r: callable without a readable signature"]
    assert not [r for r in data["records"] if "otto:r\t" in r]
    assert "name\totto:LIMIT\tvalue" in data["records"]


@pytest.mark.parametrize("define", ["def", "async def"])
def test_a_partial_over_a_class_is_sync_with_the_partials_params(tmp_path, define):
    body = (
        "import functools\n"
        f"{define} target(x, y=2): pass\n"
        "class C:\n    __wrapped__ = target\n    def __init__(self, x, y=2): pass\n"
        "p = functools.partial(C, x=1)\n"
    )
    dump = _one(tmp_path, body, ["p"])
    assert dump.bindings["otto:p"] == "callable"
    assert dump.get("call", "otto:p").fields == ["sync", "KO:x:I:1 KO:y:I:2"]


def test_a_partial_over_a_class_with_a_metaclass_call_is_refused(tmp_path):
    body = (
        "import functools\n"
        "class Meta(type):\n"
        "    def __call__(cls, x=0):\n        return super().__call__()\n"
        "class C(metaclass=Meta):\n    pass\n"
        "p = functools.partial(C, x=1)\n"
    )
    assert _refusals(tmp_path, body, ["p"]) == [
        "otto:p: constructor routed through metaclass Meta.__call__ cannot be recorded"
    ]


# A descriptor that makes an instance's __class__ claim another type: isinstance
# trusts the claim, the real type() does not.
_CLAIMS = (
    "class Claims:\n    def __init__(self, cls):\n        self.cls = cls\n"
    "    def __get__(self, obj, owner):\n        return self.cls\n"
)


def test_a_default_claiming_to_be_a_bool_is_opaque_and_its_bool_never_runs(tmp_path):
    body = (
        _CLAIMS
        + "class FakeBool:\n    __class__ = Claims(bool)\n"
        + "    def __bool__(self):\n        raise SystemExit(9)\n"
        + "def f(x=FakeBool()): pass\n"
    )
    dump = _one(tmp_path, body, ["f"])
    assert dump.get("call", "otto:f").fields == ["sync", "PK:x:O"]


@pytest.mark.parametrize("define", ["def", "async def"])
def test_a_callable_claiming_to_be_a_module_is_refused(tmp_path, define):
    body = (
        "import types\n"
        + _CLAIMS
        + f"class R:\n    __class__ = Claims(types.ModuleType)\n    {define} __call__(self): pass\n"
        + "r = R()\nr.__signature__ = 42\n"
    )
    assert _refusals(tmp_path, body, ["r"]) == ["otto:r: callable without a readable signature"]


@pytest.mark.parametrize(("define", "callkind"), [("def", "sync"), ("async def", "coroutine")])
def test_a_callable_claiming_to_be_an_alias_is_recorded_by_its_real_call(
    tmp_path, define, callkind
):
    body = (
        "import types\n"
        + _CLAIMS
        + "class R:\n    __class__ = Claims(types.GenericAlias)\n    __origin__ = list\n"
        + f"    {define} __call__(self, x): pass\n"
        + "r = R()\n"
    )
    dump = _one(tmp_path, body, ["r"])
    assert dump.bindings["otto:r"] == "callable"
    assert dump.get("call", "otto:r").fields == [callkind, "PK:x:-"]


# The generated test: every stack of up to three wrappers over a leaf of KNOWN call
# kind. The expected answer is built from the construction alone -- the leaf's kind
# and the parameter edit each supported wrapper makes -- never from the producer's
# resolver or from inspect.
_GEN_PRELUDE = (
    "import functools, inspect, types\n"
    "def _sync(a, b, c=1): pass\n"
    "async def _coro(a, b, c=1): pass\n"
    "async def _gen(a, b, c=1):\n    yield 1\n"
    "class _Cls:\n    def __init__(self, a, b, c=1): pass\n"
    "class _Receiver:\n    pass\n"
    # the supported wrappers
    "def _wraps(x):\n    return functools.wraps(x)(lambda *a, **k: x(*a, **k))\n"
    "def _bind(x):\n    return types.MethodType(x, _Receiver())\n"
    "def _partial(x):\n    return functools.partial(x, c=2)\n"
    "def _inst(x):\n    return type('Inst', (), {'__call__': x})()\n"
    # the refused wrappers
    "class _Own:\n"
    "    def __init__(self, x):\n        functools.update_wrapper(self, x)\n"
    "    def __call__(self, *a, **k): pass\n"
    "def _own(x):\n    return _Own(x)\n"
    "class _Fwd:\n"
    "    def __init__(self, x):\n        self.x = x\n"
    "    def __call__(self, *a, **k): pass\n"
    "def _wrapped_partial(x):\n"
    "    return functools.update_wrapper(functools.partial(_Fwd(x)), x)\n"
    "def _sig(x):\n"
    "    f = functools.wraps(x)(lambda *a, **k: x(*a, **k))\n"
    "    f.__signature__ = inspect.signature(_sync)\n"
    "    return f\n"
    "def _pm(x):\n    return type('PM', (), {'m': functools.partialmethod(x)}).m\n"
    "class _FunctionLike:\n"
    "    def __init__(self, x):\n"
    "        self.__code__ = getattr(x, '__code__', _sync.__code__)\n"
    "        self.__defaults__ = (1,)\n        self.__kwdefaults__ = None\n"
    "        self.__annotations__ = {}\n        self.__name__ = 'f'\n"
    "    def __call__(self, *a, **k): pass\n"
    "def _flike(x):\n    return _FunctionLike(x)\n"
    "def _static(x):\n    return staticmethod(x)\n"
    "class _Desc:\n"
    "    def __init__(self, x):\n        self.x = x\n"
    "    def __get__(self, obj, cls):\n        return self.x\n"
    "def _desc_call(x):\n    return type('DescCall', (), {'__call__': _Desc(x)})()\n"
)
# leaf -> (its call kind, its form); a class is always sync
_GEN_LEAVES = {
    "_sync": ("sync", "function"),
    "_coro": ("coroutine", "function"),
    "_gen": ("asyncgen", "function"),
    "_Cls": ("sync", "class"),
}
_GEN_LEAF_PARAMS = ["PK:a:-", "PK:b:-", "PK:c:I:1"]
_GEN_SUPPORTED = ["_wraps", "_bind", "_partial", "_inst"]
_GEN_REFUSED = ["_own", "_wrapped_partial", "_sig", "_pm", "_flike", "_static", "_desc_call"]


def _expected(stack, form):
    """Return the params field a stack (innermost wrapper first) records, or None if refused.

    Over a function, ``_wraps`` keeps a function, ``_bind`` and ``_inst`` drop the
    receiver, and ``_partial`` binds ``c=2``, which makes ``c`` keyword-only; a
    ``_partial`` also applies over a class, a bound method or another partial.
    Every other placement, and every refused wrapper anywhere, refuses the stack.
    """
    params = list(_GEN_LEAF_PARAMS)
    for layer in stack:
        if layer == "_wraps" and form == "function":
            continue
        if layer in ("_bind", "_inst") and form == "function":
            form, params = layer, params[1:]
        elif layer == "_partial" and form in ("function", "class", "_bind", "_partial"):
            form = "_partial"
            params = ["KO:c:I:2" if p.split(":")[1] == "c" else p for p in params]
        else:
            return None
    return " ".join(params) or "-"


def _generated(depth=3):
    """Every stack of 1..depth wrappers over each leaf: ``(name, expr, kind, params)``."""
    layers = _GEN_SUPPORTED + _GEN_REFUSED
    stacks = [[]]
    out = []
    for _ in range(depth):
        stacks = [[*s, layer] for s in stacks for layer in layers]
        for leaf, (kind, form) in _GEN_LEAVES.items():
            for stack in stacks:
                # A partialmethod over a staticmethod unwraps it to a plain partial,
                # so that stack is not the refused shape it is built from.
                if "_static,_pm" in ",".join(stack):
                    continue
                expr = leaf
                for layer in stack:
                    expr = f"{layer}({expr})"
                out.append((f"c{len(out)}", expr, kind, _expected(stack, form)))
    return out


def test_every_generated_composition_records_its_leaf_kind_or_is_refused(tmp_path):
    cases = _generated()
    names = [name for name, _, _, _ in cases]
    body = _GEN_PRELUDE + "".join(f"{name} = {expr}\n" for name, expr, _, _ in cases)
    src = write_tree(tmp_path, {"otto/__init__.py": f"__all__ = {names!r}\n" + body})
    data = run_child_json(src, ["otto"])
    calls = {
        rec.key: rec.fields
        for rec in map(api_records.parse_record, data["records"])
        if rec.kind == "call"
    }
    refused = dict(r.split(": ", 1) for r in data["refusals"])
    supported_kinds, refused_count, supported_over_a_class = set(), 0, 0
    for name, expr, kind, params in cases:
        key = f"otto:{name}"
        if params is None:
            assert key not in calls, (expr, calls.get(key))
            reason = refused.get(key, "")
            assert reason.startswith("call kind cannot be determined for "), (expr, reason)
            refused_count += 1
        else:
            assert key not in refused, (expr, refused[key])
            assert calls[key] == [kind, params], expr
            supported_kinds.add(kind)
            supported_over_a_class += "_Cls" in expr
    assert supported_kinds == {"sync", "coroutine", "asyncgen"}
    assert supported_over_a_class > 0
    assert refused_count > 0
    assert len(refused) == refused_count


def test_wrapped_async_function_is_a_coroutine(tmp_path):
    body = (
        "import functools\n"
        "def deco(fn):\n"
        "    @functools.wraps(fn)\n"
        "    def inner(*a, **k):\n        return fn(*a, **k)\n"
        "    return inner\n"
        "@deco\nasync def h(x): pass\n"
    )
    dump = _one(tmp_path, body, ["h"])
    assert dump.get("call", "otto:h").fields == ["coroutine", "PK:x:-"]


def test_partial_reads_the_wrapped_function(tmp_path):
    body = "import functools\nasync def g(a, b): pass\np = functools.partial(g, 1)\n"
    dump = _one(tmp_path, body, ["p"])
    assert dump.get("call", "otto:p").fields == ["coroutine", "PK:b:-"]


def test_builtin_derived_exception_records_builtin_constructor(tmp_path):
    dump = _one(tmp_path, "class Oops(ValueError):\n    pass\n", ["Oops"])
    assert _lines(dump, "otto:Oops") == [
        "name\totto:Oops\tclass",
        (
            "mro\totto:Oops\t@builtin:builtins.ValueError @builtin:builtins.Exception "
            "@builtin:builtins.BaseException"
        ),
        "call\totto:Oops\tsync\t@builtin:builtins.ValueError",
        "abstract\totto:Oops\t-",
    ]


def test_str_subclass_records_builtin_str(tmp_path):
    dump = _one(tmp_path, "class OsType(str):\n    pass\n", ["OsType"])
    assert dump.get("call", "otto:OsType").fields == ["sync", "@builtin:builtins.str"]


@pytest.mark.parametrize("base", ["list", "complex"])
def test_a_readable_builtin_constructor_is_still_builtin(tmp_path, base):
    # The signature of these subclasses IS readable on every interpreter, so the
    # @builtin record comes from the class's structure, never from a failed read.
    inspect.signature(type("Probe", (getattr(builtins, base),), {}))
    dump = _one(tmp_path, f"class Bag({base}):\n    pass\n", ["Bag"])
    assert dump.get("call", "otto:Bag").fields == ["sync", f"@builtin:builtins.{base}"]


def test_a_metaclass_call_is_refused(tmp_path):
    body = (
        "class Meta(type):\n"
        "    def __call__(cls, x=0):\n        return super().__call__()\n"
        "class Inherits(Meta):\n    pass\n"
        "class C(metaclass=Meta):\n    pass\n"
        "class D(metaclass=Inherits):\n    pass\n"
    )
    src = write_tree(tmp_path, {"otto/__init__.py": '__all__ = ["C", "D"]\n' + body})
    data = run_child_json(src, ["otto"])
    assert data["refusals"] == [
        "otto:C: constructor routed through metaclass Meta.__call__ cannot be recorded",
        "otto:D: constructor routed through metaclass Meta.__call__ cannot be recorded",
    ]
    assert not [r for r in data["records"] if r.startswith("call\t")]


def test_a_metaclass_without_its_own_call_is_not_refused(tmp_path):
    body = (
        "import abc, enum\n"
        "from typing import Protocol\n"
        "from pydantic import BaseModel\n"
        "class Meta(abc.ABCMeta):\n"
        "    def __new__(mcs, name, bases, ns):\n"
        "        return super().__new__(mcs, name, bases, ns)\n"
        "class C(metaclass=Meta):\n    def __init__(self, x): pass\n"
        "class A(abc.ABC):\n    pass\n"
        "class M(BaseModel):\n    a: int\n"
        "class P(Protocol):\n    def run(self) -> None: ...\n"
        "class E(enum.Enum):\n    A = 1\n"
    )
    dump = _one(tmp_path, body, ["C", "A", "M", "P", "E"])
    assert dump.get("call", "otto:C").fields == ["sync", "PK:x:-"]
    assert set(dump.bindings) == {"otto:C", "otto:A", "otto:M", "otto:P", "otto:E"}


def test_plain_class_has_an_empty_call(tmp_path):
    dump = _one(tmp_path, "class P:\n    pass\n", ["P"])
    assert dump.get("call", "otto:P").fields == ["sync", "-"]
    assert dump.get("mro", "otto:P").fields == ["-"]


def test_mro_names_public_bases_by_all_their_bindings_and_skips_private_ones(tmp_path):
    files = {
        "otto/_impl.py": "class _Base:\n    def shared(self): pass\nclass Base(_Base):\n    pass\n",
        "otto/sub.py": '__all__ = ["Base"]\nfrom ._impl import Base\n',
    }
    body = "from ._impl import Base\nclass Child(Base):\n    pass\n"
    dump = _one(tmp_path, body, ["Base", "Child"], files)
    src = tmp_path / "src"
    dump = dump_of(src, ["otto", "otto.sub"])
    assert dump.get("mro", "otto:Child").fields == ["{otto.sub:Base,otto:Base}"]
    assert dump.get("mro", "otto:Base").fields == ["-"]
    # members of the private base are flattened into both
    assert "shared" in dump.members_of("otto:Base")
    assert "shared" in dump.members_of("otto:Child")


def test_enum_default_records_class_set_and_value(tmp_path):
    body = "import enum\nclass Mode(enum.Enum):\n    A = 'a'\ndef f(m=Mode.A): pass\n"
    dump = _one(tmp_path, body, ["Mode", "f"])
    assert dump.get("call", "otto:f").fields == ["sync", 'PK:m:E:{otto:Mode}:A=S:"a"']


def test_undeclared_enum_default_is_refused(tmp_path):
    src = write_tree(
        tmp_path,
        {
            "otto/__init__.py": '__all__ = ["f"]\nimport enum\n'
            "class Mode(enum.Enum):\n    A = 1\ndef f(m=Mode.A): pass\n"
        },
    )
    data = run_child_json(src, ["otto"])
    assert any("otto:f" in r and "declare the enum" in r for r in data["refusals"]), data


def test_namespace_import_failure_is_refused(tmp_path):
    src = write_tree(tmp_path, {"otto/__init__.py": "__all__ = []\n", "otto/bad.py": "1/0\n"})
    data = run_child_json(src, ["otto", "otto.bad"])
    assert data["refusals"] == ["otto.bad: cannot import: ZeroDivisionError: division by zero"]


def test_namespace_without_all_is_refused_unless_assume_dir(tmp_path):
    src = write_tree(tmp_path, {"otto/__init__.py": "def f(): pass\n"})
    assert run_child_json(src, ["otto"])["refusals"] == ["otto: has no __all__"]
    data = run_child_json(src, ["otto"], assume_dir=True)
    assert data["refusals"] == []
    assert "name\totto:f\tfunction" in data["records"]


def test_unreadable_function_signature_is_refused(tmp_path):
    src = write_tree(
        tmp_path, {"otto/__init__.py": '__all__ = ["f"]\ndef f(): pass\nf.__signature__ = 5\n'}
    )
    data = run_child_json(src, ["otto"])
    assert any(r.startswith("otto:f: no readable signature") for r in data["refusals"]), data


def test_a_namespace_that_prints_does_not_corrupt_the_report(tmp_path):
    src = write_tree(tmp_path, {"otto/__init__.py": '__all__ = []\nprint("hello")\n'})
    assert run_child_json(src, ["otto"])["refusals"] == []


def test_members_methods_properties_fields_and_attributes(tmp_path):
    body = (
        "import dataclasses, functools\n"
        "@dataclasses.dataclass\n"
        "class D:\n"
        "    x: int\n"
        "    y: int = 0\n"
        "    _hidden: int = 1\n"
        "    z: int = dataclasses.field(default=2, init=False)\n"
        "    KIND = 'd'\n"
        "    def go(self, fast=False): pass\n"
        "    @classmethod\n    def make(cls, n): pass\n"
        "    @staticmethod\n    def util(n): pass\n"
        "    @property\n    def size(self): return 1\n"
        "    @functools.cached_property\n    def slow(self): return 1\n"
        "    def __len__(self): return 0\n"
        "    def _helper(self): pass\n"
    )
    dump = _one(tmp_path, body, ["D"])
    assert _lines(dump, "otto:D") == [
        "name\totto:D\tclass",
        "mro\totto:D\t-",
        "call\totto:D\tsync\tPK:x:- PK:y:I:0 PK:_hidden:I:1",
        "member\totto:D.KIND\tattribute",
        # the dataclass decorator generates __eq__ (spec §3.4); its __hash__ = None
        # switches hashing off, which is no member at all
        "member\totto:D.__eq__\tmethod\tsync\tPK:other:-",
        "member\totto:D.__len__\tmethod\tsync\t-",
        "member\totto:D.go\tmethod\tsync\tPK:fast:B:false",
        "member\totto:D.make\tclassmethod\tsync\tPK:n:-",
        "member\totto:D.size\tproperty:g--",
        "member\totto:D.slow\tproperty:g--",
        "member\totto:D.util\tstaticmethod\tsync\tPK:n:-",
        "member\totto:D.x\tfield",
        "member\totto:D.y\tfield",
        "member\totto:D.z\tfield",
        "abstract\totto:D\t-",
    ]


def test_a_dunder_set_to_none_is_absent_and_hides_the_inherited_one(tmp_path):
    body = (
        "class A:\n    def __hash__(self):\n        return 0\n"
        "class B(A):\n    __hash__ = None\n"
        "class C:\n    __hash__ = None\n    label = None\n"
    )
    dump = _one(tmp_path, body, ["A", "B", "C"])
    assert list(dump.members_of("otto:A")) == ["__hash__"]
    assert dump.members_of("otto:B") == {}
    # only a supported dunder is absent when None; a public attribute is recorded
    assert {n: r.fields for n, r in dump.members_of("otto:C").items()} == {"label": ["attribute"]}


def test_method_with_only_varargs_keeps_them(tmp_path):
    dump = _one(tmp_path, "class C:\n    def m(*args): pass\n", ["C"])
    assert dump.members_of("otto:C")["m"].fields == ["method", "sync", "VP:args:-"]


def test_property_capabilities(tmp_path):
    body = (
        "class C:\n"
        "    @property\n    def v(self): return 1\n"
        "    @v.setter\n    def v(self, x): pass\n"
        "    @v.deleter\n    def v(self): pass\n"
    )
    dump = _one(tmp_path, body, ["C"])
    assert dump.members_of("otto:C")["v"].fields == ["property:gsd"]


def test_unclassifiable_descriptor_is_refused(tmp_path):
    body = "class Desc:\n    def __get__(self, obj, cls): return 1\nclass C:\n    weird = Desc()\n"
    src = write_tree(tmp_path, {"otto/__init__.py": '__all__ = ["C"]\n' + body})
    data = run_child_json(src, ["otto"])
    assert any(r.startswith("otto:C.weird: unclassifiable member") for r in data["refusals"])


def test_slots_entries_are_fields(tmp_path):
    body = "class Base:\n    __slots__ = ('a',)\nclass S(Base):\n    __slots__ = ('b', '_c')\n"
    dump = _one(tmp_path, body, ["S"])
    members = {k: r.fields for k, r in dump.members_of("otto:S").items()}
    assert members == {"a": ["field"], "b": ["field"]}


def test_custom_dir_cannot_hide_a_member(tmp_path):
    body = "class C:\n    def visible(self): pass\n    def __dir__(self): return []\n"
    dump = _one(tmp_path, body, ["C"])
    assert "visible" in dump.members_of("otto:C")


def test_pydantic_model_inputs_routes_and_defaults(tmp_path):
    body = (
        "from pydantic import AliasChoices, AliasPath, BaseModel, ConfigDict, Field\n"
        "class M(BaseModel):\n"
        "    model_config = ConfigDict(validate_by_name=True)\n"
        "    a: int\n"
        "    b: int = Field(default=3, alias='bee')\n"
        "    c: int = Field(default=0, validation_alias=AliasChoices('see', AliasPath('p', 0)))\n"
        "    d: list = Field(default_factory=list)\n"
    )
    dump = _one(tmp_path, body, ["M"])
    inputs = {k: r.fields for k, r in dump.inputs_of("otto:M").items()}
    assert inputs == {
        'S:"a"': ['S:"a"', "required", "-", 'S:"a"'],
        'S:"b"': ['S:"b"', "optional", "I:3", 'S:"b" S:"bee"'],
        'S:"c"': ['S:"c"', "optional", "I:0", 'S:"c" S:"see" T:["S:\\"p\\"","I:0"]'],
        'S:"d"': ['S:"d"', "optional", "O", 'S:"d"'],
    }
    assert dump.get("call", "otto:M") is None
    assert {"a", "b", "c", "d"} <= set(dump.members_of("otto:M"))
    assert dump.members_of("otto:M")["a"].fields == ["field"]


def test_pydantic_alias_without_validate_by_name_accepts_only_the_alias(tmp_path):
    body = (
        "from pydantic import BaseModel, Field\n"
        "class M(BaseModel):\n    b: int = Field(alias='bee')\n"
    )
    dump = _one(tmp_path, body, ["M"])
    assert dump.inputs_of("otto:M")['S:"b"'].fields[3] == 'S:"bee"'


@pytest.mark.parametrize(
    "config",
    [
        "populate_by_name=True, validate_by_name=False",
        "populate_by_name=True, validate_by_alias=False",
        "populate_by_name=False, validate_by_alias=False",
        "populate_by_name=True",
        "validate_by_alias=False",
        "validate_by_name=True, validate_by_alias=False",
        "",
    ],
)
def test_pydantic_routes_are_what_the_installed_pydantic_validates(tmp_path, config):
    body = (
        "from pydantic import BaseModel, ConfigDict, Field\n"
        f"class M(BaseModel):\n    model_config = ConfigDict({config})\n"
        "    b: int = Field(alias='bee')\n"
    )
    dump = _one(tmp_path, body, ["M"])
    routes = api_records.split_list(dump.inputs_of("otto:M")['S:"b"'].fields[3])
    # The oracle: which keyword the same model, built here, actually accepts.
    namespace: dict = {}
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")  # populate_by_name is deprecated on newer pydantic
        exec(body, namespace)  # noqa: S102 -- a fixed test body
    accepted = []
    for keyword in ("b", "bee"):
        try:
            namespace["M"](**{keyword: 1})
        except pydantic.ValidationError:
            continue
        accepted.append(f'S:"{keyword}"')
    assert routes == accepted


def test_a_default_factory_is_never_called(tmp_path):
    mark = tmp_path / "factory-was-called"
    body = (
        "import dataclasses\n"
        "from pydantic import BaseModel, Field\n"
        "def boom():\n"
        f"    open({str(mark)!r}, 'w').close()\n"
        "    raise RuntimeError('a default factory was called')\n"
        "class M(BaseModel):\n    tags: list = Field(default_factory=boom)\n"
        "@dataclasses.dataclass\n"
        "class D:\n    tags: list = dataclasses.field(default_factory=boom)\n"
    )
    dump = _one(tmp_path, body, ["M", "D"])
    assert not mark.exists()
    assert dump.inputs_of("otto:M")['S:"tags"'].fields == ['S:"tags"', "optional", "O", 'S:"tags"']
    assert dump.get("call", "otto:D").fields == ["sync", "PK:tags:O"]


def test_unicode_identifiers_round_trip_from_producer_to_parser(tmp_path):
    # chr() keeps the source ASCII here; the fixture module itself holds the letters.
    e_acute, cap_e_acute, x_combining = chr(0xE9), chr(0xC9), "x" + chr(0x301)
    body = (
        "import enum\n"
        f"class Mode(enum.Enum):\n    {cap_e_acute} = 1\n"
        f"def f({e_acute}=0, *, {x_combining}=Mode.{cap_e_acute}): pass\n"
        f"def {x_combining}(): pass\n"
    )
    dump = _one(tmp_path, body, ["Mode", "f", x_combining])
    assert dump.enums_of("otto:Mode")[cap_e_acute].fields == [cap_e_acute, "I:1"]
    assert dump.get("call", "otto:f").fields == [
        "sync",
        f"PK:{e_acute}:I:0 KO:{x_combining}:E:{{otto:Mode}}:{cap_e_acute}=I:1",
    ]
    assert dump.bindings[f"otto:{x_combining}"] == "function"


def test_typeddict_inputs(tmp_path):
    body = (
        "from typing_extensions import NotRequired, TypedDict\n"
        "class TD(TypedDict):\n    a: int\n    b: NotRequired[int]\n"
    )
    dump = _one(tmp_path, body, ["TD"])
    inputs = {k: r.fields for k, r in dump.inputs_of("otto:TD").items()}
    assert inputs == {
        'S:"a"': ['S:"a"', "required", "-", 'S:"a"'],
        'S:"b"': ['S:"b"', "optional", "-", 'S:"b"'],
    }


def test_namedtuple_fields_and_call(tmp_path):
    body = "from typing import NamedTuple\nclass N(NamedTuple):\n    a: int\n    b: int = 1\n"
    dump = _one(tmp_path, body, ["N"])
    assert dump.get("call", "otto:N").fields == ["sync", "PK:a:- PK:b:I:1"]
    assert dump.members_of("otto:N")["a"].fields == ["field"]


def test_protocol_requires_and_annotation_only_member(tmp_path):
    body = (
        "from typing import Protocol\n"
        "class P(Protocol):\n    x: int\n    def run(self) -> None: ...\n"
    )
    dump = _one(tmp_path, body, ["P"])
    assert dump.get("requires", "otto:P").fields == ["run x"]
    assert dump.members_of("otto:P")["x"].fields == ["field"]


def test_abstract_records_the_effective_set(tmp_path):
    body = (
        "import abc\n"
        "class A(abc.ABC):\n"
        "    @abc.abstractmethod\n    def put(self): ...\n"
        "    @abc.abstractmethod\n    def get(self): ...\n"
        "class B(A):\n    def get(self): pass\n"
    )
    dump = _one(tmp_path, body, ["A", "B"])
    assert dump.get("abstract", "otto:A").fields == ["get put"]
    assert dump.get("abstract", "otto:B").fields == ["put"]


def test_underscore_obligation_is_refused(tmp_path):
    body = "import abc\nclass A(abc.ABC):\n    @abc.abstractmethod\n    def _open(self): ...\n"
    src = write_tree(tmp_path, {"otto/__init__.py": '__all__ = ["A"]\n' + body})
    data = run_child_json(src, ["otto"])
    assert any("hidden obligation otto:A._open" in r for r in data["refusals"]), data


def test_underscore_protocol_member_is_refused(tmp_path):
    body = "from typing import Protocol\nclass P(Protocol):\n    async def _login(self): ...\n"
    src = write_tree(tmp_path, {"otto/__init__.py": '__all__ = ["P"]\n' + body})
    data = run_child_json(src, ["otto"])
    assert any("hidden obligation otto:P._login" in r for r in data["refusals"]), data


def test_enum_members_values_aliases_and_methods(tmp_path):
    body = (
        "import enum\n"
        "class Perm(enum.Flag):\n"
        "    NONE = 0\n    R = 4\n    W = 2\n    RW = 6\n    READ = 4\n"
        "    def describe(self): return ''\n"
    )
    dump = _one(tmp_path, body, ["Perm"])
    assert _lines(dump, "otto:Perm") == [
        "name\totto:Perm\tenum",
        "mro\totto:Perm\t-",
        "member\totto:Perm.describe\tmethod\tsync\t-",
        "enum\totto:Perm\tNONE\tI:0",
        "enum\totto:Perm\tR\tI:4",
        "enum\totto:Perm\tW\tI:2",
        "enum\totto:Perm\tRW\tI:6",
        "enum\totto:Perm\tREAD\tI:4",
    ]


def test_nested_class_is_its_own_binding_and_a_cycle_is_a_classref(tmp_path):
    body = "class Outer:\n    class Inner:\n        def go(self): pass\nOuter.Self = Outer\n"
    dump = _one(tmp_path, body, ["Outer"])
    assert dump.members_of("otto:Outer")["Inner"].fields == ["class"]
    assert dump.members_of("otto:Outer")["Self"].fields == ["classref=otto:Outer"]
    assert dump.bindings["otto:Outer.Inner"] == "class"
    assert "go" in dump.members_of("otto:Outer.Inner")


def test_private_member_map_for_the_validator(tmp_path):
    body = (
        "import dataclasses\n"
        "class _Base:\n    def _hook(self): pass\n"
        "@dataclasses.dataclass\n"
        "class Host(_Base):\n    _connection_factory: object = None\n"
        "    def _run_put(self): pass\n    def __aenter__(self): pass\n"
    )
    src = write_tree(tmp_path, {"otto/__init__.py": '__all__ = ["Host"]\n' + body})
    data = run_child_json(src, ["otto"])
    assert data["private"]["otto:Host"] == ["_connection_factory", "_hook", "_run_put"]


def test_otto_resolved_outside_the_archive_is_a_provenance_refusal(tmp_path):
    # No otto/__init__.py in the fixture: `import otto` falls through to the
    # real, editable-installed otto, which lies outside this src.
    src = write_tree(tmp_path, {"notes.txt": ""})
    data = run_child_json(src, ["otto"], assume_dir=True)
    assert data["provenance"], data
    assert all("outside" in p for p in data["provenance"])


def test_partial_binding_is_a_callable_on_every_interpreter(tmp_path):
    body = "import functools\ndef g(a, b): pass\np = functools.partial(g, 1)\n"
    dump = _one(tmp_path, body, ["p"])
    assert _lines(dump, "otto:p") == [
        "name\totto:p\tcallable",
        "call\totto:p\tsync\tPK:b:-",
    ]


def test_partial_class_attribute_is_an_attribute_on_every_interpreter(tmp_path):
    body = "import functools\ndef g(a, b): pass\nclass C:\n    p = functools.partial(g, 1)\n"
    dump = _one(tmp_path, body, ["C"])
    assert dump.members_of("otto:C")["p"].fields == ["attribute"]


def test_non_otto_class_binding_has_no_members_and_builtins_stay_builtin(tmp_path):
    body = (
        "import enum\n"
        "OsType = str\n"
        "class Kind(str, enum.Enum):\n    A = 'a'\n"
        "class S(str):\n    pass\n"
    )
    src = write_tree(tmp_path, {"otto/__init__.py": '__all__ = ["OsType", "Kind", "S"]\n' + body})
    data = run_child_json(src, ["otto"])
    assert data["refusals"] == []
    dump = dump_of(src, ["otto"])
    assert dump.members_of("otto:OsType") == {}
    for key in ("otto:Kind", "otto:S"):
        assert "@builtin:builtins.str" in dump.get("mro", key).fields[0]
        assert "{otto:OsType}" not in dump.get("mro", key).fields[0]


def test_unencodable_enum_member_value_is_refused(tmp_path):
    body = "import enum\nclass E(enum.Enum):\n    M = object()\n    N = (1, object())\n    OK = 1\n"
    src = write_tree(tmp_path, {"otto/__init__.py": '__all__ = ["E"]\n' + body})
    data = run_child_json(src, ["otto"])
    assert "otto:E.M: enum member value cannot be encoded" in data["refusals"]
    assert "otto:E.N: enum member value cannot be encoded" in data["refusals"]
    enum_lines = [r for r in data["records"] if r.startswith("enum\t")]
    assert enum_lines == ["enum\totto:E\tOK\tI:1"]


def test_protocol_class_itself_is_a_refusal_not_a_crash(tmp_path):
    body = "from typing import Protocol\nLIMIT = 5\n"
    src = write_tree(tmp_path, {"otto/__init__.py": '__all__ = ["Protocol", "LIMIT"]\n' + body})
    data = run_child_json(src, ["otto"])
    assert any(r.startswith("otto:Protocol: cannot be recorded") for r in data["refusals"]), data
    assert "name\totto:LIMIT\tvalue" in data["records"]
    assert "otto:Protocol" not in data["private"]


def test_protocol_class_itself_under_assume_dir(tmp_path):
    body = "from typing import Protocol\nclass Keep:\n    def _x(self): pass\n"
    src = write_tree(tmp_path, {"otto/__init__.py": body})
    data = run_child_json(src, ["otto"], assume_dir=True)
    assert any(r.startswith("otto:Protocol: cannot be recorded") for r in data["refusals"]), data
    assert "name\totto:Keep\tclass" in data["records"]
    assert data["private"]["otto:Keep"] == ["_x"]


def test_a_refused_class_still_reports_its_private_names(tmp_path):
    body = (
        "import abc\n"
        "class C(abc.ABC):\n"
        "    @abc.abstractmethod\n    def _hidden(self): ...\n"
        "    def _helper(self): pass\n"
    )
    src = write_tree(tmp_path, {"otto/__init__.py": '__all__ = ["C"]\n' + body})
    data = run_child_json(src, ["otto"])
    assert any("hidden obligation otto:C._hidden" in r for r in data["refusals"]), data
    assert data["private"]["otto:C"] == ["_abc_impl", "_helper", "_hidden"]


def _formats(tmp_path, versions_py, *formats):
    src = write_tree(
        tmp_path,
        {"otto/__init__.py": "__all__ = []\n", "otto/versions.py": versions_py},
    )
    return run_child_json(src, ["otto"], formats=list(formats))


def test_a_declared_format_is_dumped_with_sorted_encoded_versions(tmp_path):
    data = _formats(
        tmp_path,
        'READS = ["v3", "v1", "v2"]\nWRITES = ["v1", "v3"]\nSTORE = [8]\n',
        Format("link-sentinel", "otto.versions:READS", "otto.versions:WRITES"),
        Format("coverage-store", "otto.versions:STORE", "otto.versions:STORE"),
        Format("check-report", None, "otto.versions:CHECK"),
    )
    assert data["refusals"] == ["format check-report: otto.versions:CHECK does not exist"]
    assert 'format\tlink-sentinel\tS:"v1" S:"v2" S:"v3"\tS:"v1" S:"v3"' in data["records"]
    assert "format\tcoverage-store\tI:8\tI:8" in data["records"]


def test_a_string_version_with_a_slash_is_a_version(tmp_path):
    data = _formats(
        tmp_path,
        'CHECK: list = ["otto-check/1"]\n',
        Format("check-report", None, "otto.versions:CHECK"),
    )
    assert data["refusals"] == []
    assert 'format\tcheck-report\t-\tS:"otto-check/1"' in data["records"]


@pytest.mark.parametrize(
    ("versions_py", "why"),
    [
        ("READS = (1, 2)\n", "is not a literal list"),
        ("READS = list(range(2))\n", "is not a literal list"),
        ("BASE = 1\nREADS = [BASE]\n", "is not a literal list"),
        ("READS = [1, True]\n", "a version is an int or a str"),
        ("READS = [1.5]\n", "a version is an int or a str"),
        ("READS = [1, 1]\n", "duplicate version"),
        ("READS = []\n", "declares no version"),
        ("READS = [1]\nREADS.append(2)\n", "is not a literal list"),
        ("raise RuntimeError('boom')\n", "does not import"),
    ],
)
def test_format_refusals(tmp_path, versions_py, why):
    data = _formats(tmp_path, versions_py, Format("store", "otto.versions:READS", None))
    assert [r for r in data["refusals"] if why in r], data["refusals"]
    assert not [r for r in data["records"] if r.startswith("format\t")]


def test_a_constant_that_is_not_a_literal_names_the_fix(tmp_path):
    data = _formats(tmp_path, "READS = list(range(2))\n", Format("s", "otto.versions:READS", None))
    (msg,) = data["refusals"]
    assert "literal list of int/str assigned in the module the pointer names" in msg


def test_a_re_exported_constant_is_refused(tmp_path):
    src = write_tree(
        tmp_path,
        {
            "otto/__init__.py": "__all__ = []\n",
            "otto/a.py": "READS = [1]\n",
            "otto/b.py": "from otto.a import READS\n",
        },
    )
    data = run_child_json(src, ["otto"], formats=[Format("s", "otto.b:READS", None)])
    assert [r for r in data["refusals"] if "assigned in the module the pointer names" in r]


def test_a_source_with_a_coding_cookie_is_read(tmp_path):
    src = write_tree(tmp_path, {"otto/__init__.py": "__all__ = []\n"})
    mod = src / "otto" / "versions.py"
    mod.write_bytes(b"# -*- coding: latin-1 -*-\n# caf\xe9\nREADS = [1, 2]\n")
    data = run_child_json(src, ["otto"], formats=[Format("s", "otto.versions:READS", None)])
    assert data["refusals"] == []
    assert "format\ts\tI:1 I:2\t-" in data["records"]


def test_a_format_that_cannot_be_recorded_is_a_refusal_not_a_crash(tmp_path):
    # hasattr() lets a non-AttributeError from a module __getattr__ escape _format_versions
    data = _formats(
        tmp_path,
        "def __getattr__(name):\n"
        '    if name == "READS":\n'
        '        raise RuntimeError("boom")\n'
        "    raise AttributeError(name)\n",
        Format("s", "otto.versions:READS", None),
    )
    (msg,) = data["refusals"]
    assert msg.startswith("format s: cannot be recorded")
    assert "boom" in msg
    assert not [r for r in data["records"] if r.startswith("format\t")]
