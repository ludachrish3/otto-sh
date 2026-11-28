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
    TABLE.register(name=name, entry=Rec(value))


def unmarked_register_widget(name: str, value: object) -> None:
    """The same wrapper without the mark."""
    TABLE.register(name=name, entry=Rec(value))


def widget(name: str):
    """A registering decorator factory whose inner closure is marked."""

    @registration_boundary
    def register(cls: type) -> type:
        TABLE.register(name=name, entry=Rec(cls))
        return cls

    return register


def test_a_direct_register_credits_its_immediate_caller():
    _from_module("pkg.init", TABLE.register, name="w", entry=Rec(1))
    assert TABLE.origin(name="w") == "pkg.init"


def test_a_marked_wrapper_is_transparent():
    """Mutation: drop the marker (the unmarked twin below shows what that credits)."""
    _from_module("pkg.init", register_widget, "w", 1)
    assert TABLE.origin(name="w") == "pkg.init"


def test_an_unmarked_wrapper_is_credited_itself():
    _from_module("pkg.init", unmarked_register_widget, "w", 1)
    assert TABLE.origin(name="w") == __name__


def test_a_marked_decorator_credits_the_decorated_module():
    code = compile("@widget('w')\nclass Decorated:\n    pass\n", "<pkg.init>", "exec")
    exec(code, {"__name__": "pkg.init", "widget": widget})  # noqa: S102
    assert TABLE.origin(name="w") == "pkg.init"


def test_a_helper_wrapping_a_wrapper_is_credited():
    """An unmarked third-party helper around a marked wrapper is the registrant."""
    scope = {"__name__": "helpers.reg", "register_widget": register_widget}
    exec(  # noqa: S102
        compile("def helper(name, value):\n    register_widget(name, value)\n", "<h>", "exec"),
        scope,
    )
    _from_module("pkg.init", scope["helper"], "w", 1)
    assert TABLE.origin(name="w") == "helpers.reg"


def test_otto_builtins_imported_first_by_a_test_are_otto_s():
    """A test is often the first importer of an otto module that registers at import."""
    module = types.ModuleType("otto.fake_builtins")
    module.TABLE = TABLE
    module.Rec = Rec
    with reg.loading_test_files():
        exec(  # noqa: S102
            compile(
                "TABLE.register(name='builtin', entry=Rec(1))\n", "<otto.fake_builtins>", "exec"
            ),
            module.__dict__,
        )
    assert TABLE.origin(name="builtin") == "otto.fake_builtins"
    assert "otto.fake_builtins" not in sys.modules


def test_the_project_comes_from_the_registering_project_marker():
    with reg.registering_project("a"):
        TABLE.register(name="inside", entry=Rec(1))
    TABLE.register(name="outside", entry=Rec(2))
    assert TABLE.project(name="inside") == "a"
    assert TABLE.project(name="outside") is None


def test_an_otto_registration_inside_a_projects_init_import_has_no_owner():
    """otto's built-ins are otto's even when a project's init import first imports them.

    The marker is set (a project's init import is running), and only the
    registrant differs: the otto module records no project, the project's
    module records it.
    """
    with reg.registering_project("acme"):
        _from_module("otto.fake_builtins", TABLE.register, name="builtin", entry=Rec(1))
        _from_module("acme_init", TABLE.register, name="plugin", entry=Rec(2))
    assert TABLE.project(name="builtin") is None
    assert TABLE.project(name="plugin") == "acme"


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


def test_validation_reads_the_owner_the_entry_is_stored_under():
    """Fail-first: a ``validate`` hook and the stored stamp agree on who owns a registration.

    Under the same attributed registrations (an otto module and a project's
    module, both inside the project's init import), the hook reads
    ``Proposed.owner`` and a wrapper reads ``registration_owner()``; each
    answers what ``Registry.project`` records.
    """
    seen: "dict[str, str | None]" = {}
    table: "Registry[Rec]" = Registry(
        "gadget",
        entry=Rec,
        register_hint="register_gadget()",
        validate=lambda name, _entry, proposed: seen.__setitem__(name, proposed.owner),
    )
    wrapped: "dict[str, str | None]" = {}

    @registration_boundary
    def register_gadget(name: str) -> None:
        wrapped[name] = reg.registration_owner()
        table.register(name=name, entry=Rec(name))

    with reg.registering_project("acme"):
        _from_module("otto.fake_builtins", register_gadget, "builtin")
        _from_module("acme_init", register_gadget, "plugin")

    stored = {name: table.project(name=name) for name in ("builtin", "plugin")}
    assert stored == {"builtin": None, "plugin": "acme"}
    assert seen == stored
    assert wrapped == stored
