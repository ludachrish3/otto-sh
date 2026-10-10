"""Attribution: the engine credits the first frame outside itself and every marked wrapper."""

import sys
import types
from dataclasses import dataclass

from otto import registry as reg
from otto.registry import ClassEntry, Registry, registration_boundary


@dataclass(frozen=True)
class Rec:
    value: object


TABLE: "Registry[Rec]" = Registry("widget", entry=Rec, register_hint="register_widget()")
"""A module-level table; the root isolation fixture empties it after every test."""


def _from_module(module: str, fn, *args, **kwargs):
    """Call *fn* from a frame whose module is *module* (attribution is by frame)."""
    code = compile("result = fn(*args, **kwargs)", f"<{module}>", "exec")
    scope = {"__name__": module, "fn": fn, "args": args, "kwargs": kwargs}
    exec(code, scope)  # noqa: S102 — a synthetic caller module, the attribution under test
    return scope["result"]


@registration_boundary
def register_widget(name: str, value: object) -> None:
    """A marked wrapper, as every ``register_*`` wrapper is."""
    TABLE.register(name, Rec(value))


def unmarked_register_widget(name: str, value: object) -> None:
    """The same wrapper without the mark."""
    TABLE.register(name, Rec(value))


def widget(name: str):
    """A registering decorator factory whose inner closure is marked."""

    @registration_boundary
    def register(cls: type) -> type:
        TABLE.register(name, Rec(cls))
        return cls

    return register


def test_a_direct_register_credits_its_immediate_caller():
    _from_module("pkg.init", TABLE.register, "w", Rec(1))
    assert TABLE.origin("w") == "pkg.init"


def test_a_marked_wrapper_is_transparent():
    """Mutation: drop the marker (the unmarked twin below shows what that credits)."""
    _from_module("pkg.init", register_widget, "w", 1)
    assert TABLE.origin("w") == "pkg.init"


def test_an_unmarked_wrapper_is_credited_itself():
    _from_module("pkg.init", unmarked_register_widget, "w", 1)
    assert TABLE.origin("w") == __name__


def test_a_marked_decorator_credits_the_decorated_module():
    code = compile("@widget('w')\nclass Decorated:\n    pass\n", "<pkg.init>", "exec")
    exec(code, {"__name__": "pkg.init", "widget": widget})  # noqa: S102
    assert TABLE.origin("w") == "pkg.init"


def test_a_helper_wrapping_a_wrapper_is_credited():
    """An unmarked third-party helper around a marked wrapper is the registrant."""
    scope = {"__name__": "helpers.reg", "register_widget": register_widget}
    exec(  # noqa: S102
        compile("def helper(name, value):\n    register_widget(name, value)\n", "<h>", "exec"),
        scope,
    )
    _from_module("pkg.init", scope["helper"], "w", 1)
    assert TABLE.origin("w") == "helpers.reg"


def test_otto_builtins_imported_first_by_a_test_are_otto_s():
    """A test is often the first importer of an otto module that registers at import."""
    module = types.ModuleType("otto.fake_builtins")
    module.TABLE = TABLE
    module.Rec = Rec
    with reg.loading_test_files():
        exec(  # noqa: S102
            compile("TABLE.register('builtin', Rec(1))\n", "<otto.fake_builtins>", "exec"),
            module.__dict__,
        )
    assert TABLE.origin("builtin") == "otto.fake_builtins"
    assert "otto.fake_builtins" not in sys.modules


def test_the_repo_comes_from_the_registering_repo_marker():
    with reg.registering_repo("a"):
        TABLE.register("inside", Rec(1))
    TABLE.register("outside", Rec(2))
    assert TABLE.repo("inside") == "a"
    assert TABLE.repo("outside") is None


def test_registry_defined_in_is_the_constructing_module():
    built = _from_module(
        "tests.synthetic.tables", Registry, "x", entry=ClassEntry, register_hint="x"
    )
    assert built.defined_in == "tests.synthetic.tables"
    sub = _from_module("tests.synthetic.tables", reg.Subscription, "hook", register_hint="x")
    assert sub.defined_in == "tests.synthetic.tables"
    view = _from_module(
        "tests.synthetic.tables",
        reg.RegistryView,
        "view",
        register_hint="x",
        sources=[],
        derive=list,
    )
    assert view.defined_in == "tests.synthetic.tables"
    backends = _from_module(
        "tests.synthetic.tables",
        reg.BackendRegistry,
        "backend",
        register_hint="x",
        error=ValueError,
        describe_parse_error=str,
        result=lambda name, obj: None,
    )
    assert backends.defined_in == "tests.synthetic.tables"
    assert TABLE.defined_in == __name__
