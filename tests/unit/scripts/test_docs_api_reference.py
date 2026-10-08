"""``scripts/docs_api_reference.py``: one documented home per public object.

The rules keep the split API reference building under ``-W``: an object two
declared namespaces hold is indexed once, every other holder links to it, an
Internals page leaves it out, and a link written against its defining module
still resolves. Stable is never marked.
"""

import types
import typing

import pytest
from typing_extensions import override

from scripts.api_manifest import Namespace
from scripts.docs_api_reference import (
    Home,
    annotated_metadata,
    annotated_metadata_text,
    build_reference,
    choose_home,
    data_sources,
    defining_module,
    documenting_module,
    drop_annotated_marker,
    drop_annotated_markers,
    factory_default,
    field_factories,
    internal_note,
    module_docstring,
    object_default,
    owner_module,
    provisional_notice,
    public_target,
    python_defaults,
    qualify_relative_roles,
    replace_defaults,
    shows_value,
    skip_member,
    stability_banner,
    trackable,
)

pytestmark = pytest.mark.interpreter_agnostic


def _ns(*names: str) -> "dict[str, Namespace]":
    return {name: Namespace(name, 1, "provisional") for name in names}


def test_the_root_is_home_only_to_what_nothing_else_holds():
    assert choose_home(["otto", "otto.lab"], "otto.config.fleet") == "otto.lab"
    assert choose_home(["otto"], "otto.cli") == "otto"


def test_a_holder_above_the_defining_module_wins():
    assert choose_home(["otto", "otto.host", "otto.result"], "otto.result") == "otto.result"


def test_otherwise_the_deepest_holder_wins_then_the_first_by_name():
    assert (
        choose_home(["otto.host", "otto.host.transfer"], "collections.abc") == "otto.host.transfer"
    )
    assert choose_home(["otto.link", "otto.docker"], None) == "otto.docker"


def test_values_unrelated_names_can_share_are_not_tracked():
    assert not trackable(30)
    assert not trackable("utf-8")
    assert not trackable(("a", "b"))
    assert not trackable(str)
    assert not trackable(types)
    assert trackable(object())
    assert trackable(list[int])


def test_defining_module_reads_code_objects_only():
    assert defining_module(choose_home) == "scripts.docs_api_reference"
    assert defining_module(Home) == "scripts.docs_api_reference"
    assert defining_module(Home.path) == "scripts.docs_api_reference"
    assert defining_module([1, 2]) is None


def test_an_object_held_twice_is_documented_once_and_listed_at_the_other():
    from otto.host import transfer
    from otto.result import CommandResult

    ref = build_reference(_ns("otto", "otto.host", "otto.host.transfer"))
    assert ref.homes[id(transfer.TransferProgressHandler)] == Home(
        "otto.host.transfer", "TransferProgressHandler"
    )
    assert ref.reexports["otto.host"]["TransferProgressHandler"] == (
        "otto.host.transfer.TransferProgressHandler"
    )
    assert ref.homes[id(CommandResult)] == Home("otto.host", "CommandResult")
    assert ref.reexports["otto"]["CommandResult"] == "otto.host.CommandResult"


def test_an_internals_page_skips_what_a_public_page_documents():
    from otto.host import transfer

    ref = build_reference(_ns("otto.host", "otto.host.transfer"))
    handler = transfer.TransferProgressHandler
    internals = {"ignore-module-all": True}
    assert skip_member(ref, "otto.host.transfer.progress", "module", handler, internals) is True
    assert skip_member(ref, "otto.host.transfer", "module", handler, {}) is None
    assert skip_member(ref, "otto.host", "module", handler, {}) is True
    assert skip_member(ref, "otto.host", "class", handler, {}) is None
    assert skip_member(ref, "otto.host.session", "module", object(), internals) is None


def test_stable_is_never_marked():
    assert stability_banner("otto.host", "stable") == []
    banner = stability_banner("otto.host", "provisional")
    assert banner[0] == ".. admonition:: Provisional"
    assert "otto-stability-provisional" in banner[1]
    assert ":doc:`/api/stability`" in "\n".join(banner)


def test_module_docstrings_get_the_mark_their_half_needs():
    ref = build_reference(_ns("otto", "otto.host"))
    internal = ["Unix host."]
    module_docstring(ref, "otto.host.unix_host", {"ignore-module-all": True}, internal)
    assert internal[: len(internal_note("otto.host.unix_host"))] == internal_note(
        "otto.host.unix_host"
    )
    assert internal[-1] == "Unix host."

    root = ["The root."]
    module_docstring(ref, "otto", {}, root)
    assert root[0] == ".. admonition:: Provisional"
    assert ".. rubric:: Also exported here" in root
    assert "* :py:obj:`CommandResult <otto.host.CommandResult>`" in root

    undeclared = ["Text."]
    module_docstring(ref, "otto.host.unix_host", {}, undeclared)
    assert undeclared == ["Text."]


def test_a_relative_role_is_spelled_out_against_its_defining_module():
    lines = [
        "Raises :class:`AppShellTimeoutError` on a stall,",
        "see :class:`the error <AppShellTimeoutError>` and :class:`~AppShellTimeoutError`.",
        "Calls :meth:`cmd`, returns :class:`otto.result.Result`, uses :class:`Path`.",
        "Subclass :class:`AppShell`.",
    ]
    got = qualify_relative_roles(lines, "otto", "otto.host.app_shell")
    assert got[0] == (
        "Raises :class:`AppShellTimeoutError <otto.host.app_shell.AppShellTimeoutError>` "
        "on a stall,"
    )
    assert got[1] == (
        "see :class:`the error <otto.host.app_shell.AppShellTimeoutError>` and "
        ":class:`AppShellTimeoutError <otto.host.app_shell.AppShellTimeoutError>`."
    )
    assert got[2] == lines[2]  # a member, an absolute target, a stdlib import: untouched
    assert got[3] == lines[3]  # bound where it is documented: untouched
    assert qualify_relative_roles(lines, "otto.host.app_shell", "otto.host.app_shell") == lines


def test_a_relative_data_role_is_spelled_out_against_its_defining_module():
    lines = ["Defaults to :data:`DEFAULT_COMMAND_TIMEOUT` and :data:`~DEFAULT_COMMAND_TIMEOUT`."]
    got = qualify_relative_roles(lines, "otto.host", "otto.host.host")
    assert got == [
        (
            "Defaults to :data:`DEFAULT_COMMAND_TIMEOUT <otto.host.host.DEFAULT_COMMAND_TIMEOUT>` "
            "and :data:`DEFAULT_COMMAND_TIMEOUT <otto.host.host.DEFAULT_COMMAND_TIMEOUT>`."
        )
    ]
    # a plain value under a class role (a typing alias, say) is not a constant: untouched
    other = ["Typed :class:`DEFAULT_COMMAND_TIMEOUT`."]
    assert qualify_relative_roles(other, "otto.host", "otto.host.host") == other


def test_an_entry_is_documented_under_the_module_its_path_names():
    assert documenting_module("otto.coverage.CoverageReporter.report") == "otto.coverage"
    assert documenting_module("otto.coverage.reporter.CollectionInputs") == "otto.coverage.reporter"
    assert documenting_module("otto.no_such_module.Thing") is None


def test_reexported_data_is_looked_up_where_it_is_bound():
    assert data_sources("otto.init", "AREA_NAMES")[0] == "otto.init.areas"
    assert "otto.host.transfer.base" in data_sources("otto.host.transfer", "NcPortStrategy")
    assert "otto.utils" in data_sources("otto.models", "MIN_INTERVAL_SECONDS")
    assert data_sources("otto.init", "no_such_name") == []


def test_a_value_is_shown_unless_its_repr_is_the_default_one():
    from otto.registry import Registry

    assert shows_value(["a", "b"])
    assert shows_value(frozenset({"x"}))
    assert shows_value(30)
    assert not shows_value(object())
    assert not shows_value(Registry("thing", register_hint="register_thing()"))
    assert not shows_value({"disk": object()})
    assert not shows_value([1, object()])

    class _BrokenRepr:
        @override
        def __repr__(self) -> str:
            raise RuntimeError("no repr")

    assert not shows_value(_BrokenRepr())


def test_an_attribute_takes_the_module_of_the_class_that_holds_it():
    assert owner_module("otto.tunnel.TunnelCheckReport.dest_proof") == "otto.tunnel.check"
    assert owner_module("otto.lab.Lab.component_names") == "otto.config.lab"
    assert owner_module("otto.no_such_module.Thing.attr") is None


def test_a_defining_path_resolves_to_the_home():
    ref = build_reference(_ns("otto.host", "otto.host.transfer"))
    assert public_target(ref, "otto.host.transfer.progress.make_transfer_progress") == (
        "otto.host.transfer.make_transfer_progress"
    )
    assert public_target(ref, "make_transfer_progress", "otto.host.transfer.progress") == (
        "otto.host.transfer.make_transfer_progress"
    )
    assert public_target(ref, "otto.host.transfer.make_transfer_progress") is None
    assert public_target(ref, "otto.no_such_module.thing") is None


def test_an_internal_package_reexport_resolves_to_its_defining_module():
    ref = build_reference(_ns("otto.host"))
    assert public_target(ref, "otto.check.Verdict") == "otto.check.verdict.Verdict"
    assert public_target(ref, "otto.check.TUNNEL_TOOLS") == "otto.check.fingerprint.TUNNEL_TOOLS"


def test_the_notice_shows_before_1_0_only():
    assert provisional_notice("0.16.1")
    assert provisional_notice("0.17.0rc1")
    assert provisional_notice("1.0.0") is None


_M = "<otto.utils._Exclude object at 0xe3938782aa00>"


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("(x: ~typing.Annotated[bool, " + _M + "] = True)", "(x: bool = True)"),
        (
            "(e: ~types.Annotated[tuple[str | ~re.Pattern[str], str] | None, " + _M + "] | None)",
            "(e: tuple[str | ~re.Pattern[str], str] | None | None)",
        ),
        ("(x: list[~typing.Annotated[int, " + _M + "]], y: int)", "(x: list[int], y: int)"),
        (
            "(a: Annotated[int, " + _M + "], b: ~pathlib.Annotated[float, " + _M + "])",
            "(a: int, b: float)",
        ),
        (
            "(x: ~typing.Annotated[int, ~otto.utils.Arg(help=a, b), " + _M + "])",
            "(x: ~typing.Annotated[int, ~otto.utils.Arg(help=a, b)])",
        ),
        ("~typing.Annotated[int, " + _M + "]", "int"),
    ],
)
def test_the_marker_s_annotated_renders_as_its_type(text, expected):
    assert drop_annotated_marker(text, _M) == expected


@pytest.mark.parametrize(
    "text",
    [
        "(x: ~typing.Annotated[int, ~otto.utils.Arg(help=Octal [bits], e.g. 755)])",
        "(x: ~typing.Annotated[int, <otto.utils._Other object at 0x1>] = 1)",
        "(x=" + _M + ")",
        "",
    ],
)
def test_other_metadata_and_a_bare_marker_stay(text):
    assert drop_annotated_marker(text, _M) == text


def test_sphinx_s_own_signature_of_an_excluded_parameter_renders_its_type():
    """The text autodoc hands the hook, from a real ``Annotated[T, Exclude]`` parameter."""
    import inspect
    from typing import Annotated

    from sphinx.util.inspect import stringify_signature

    from otto.utils import Arg, Exclude

    def put(
        src: Annotated[str, Arg(help="Local file(s).")],
        show_progress: Annotated[bool, Exclude] = True,
    ) -> Annotated[int, Exclude]:
        raise NotImplementedError

    text = stringify_signature(inspect.signature(put))
    assert repr(Exclude) in text
    rewritten = drop_annotated_marker(text, repr(Exclude))
    assert rewritten.endswith("show_progress: bool = True) -> int"), rewritten
    assert "Arg(" in rewritten
    assert "_Exclude" not in rewritten


_OPT = "~otto.utils.Opt(help=Octal [bits], e.g. 755, min=None)"


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("(x: ~typing.Annotated[int, " + _OPT + "] = 1)", "(x: int = 1)"),
        (
            "(a: ~typing.Annotated[int, " + _OPT + "], b: ~typing.Annotated[bool, " + _M + "])",
            "(a: int, b: bool)",
        ),
        (
            "(u: ~types.Annotated[str | None, " + _OPT + "] | None = None)",
            "(u: str | None | None = None)",
        ),
        (
            "(x=" + _OPT + ", y: list[~typing.Annotated[int, " + _M + "]])",
            "(x=" + _OPT + ", y: list[int])",
        ),
        ("(x: int)", "(x: int)"),
    ],
)
def test_every_marker_s_annotated_renders_as_its_type_whatever_its_text_holds(text, expected):
    assert drop_annotated_markers(text, [_OPT, _M]) == expected


def test_sphinx_s_own_signature_of_cli_metadata_parameters_parses_as_python():
    """Autodoc's text for real ``Arg``/``Opt`` parameters, in both typehint formats."""
    import inspect
    from typing import Annotated

    from sphinx.util.inspect import signature_from_str, stringify_signature

    from otto.utils import Arg, Exclude, Opt

    def put(
        src: Annotated[str, Arg(help="Local file(s), e.g. [a, b].")],
        mode: Annotated[int | None, Opt(help="Octal [bits], e.g. 755")] | None = None,
        quiet: Annotated[bool, Exclude] = True,
    ) -> None:
        raise NotImplementedError

    metadata = annotated_metadata(
        [p.annotation for p in inspect.signature(put).parameters.values()]
    )
    assert len(metadata) == 3
    markers = [text for meta in metadata for text in annotated_metadata_text(meta)]
    for unqualified in (True, False):
        text = stringify_signature(
            inspect.signature(put),
            show_return_annotation=False,
            unqualified_typehints=unqualified,
        )
        with pytest.raises(SyntaxError):
            signature_from_str(text)
        rewritten = drop_annotated_markers(text, markers)
        assert rewritten == "(src: str, mode: int | None | None = None, quiet: bool = True)"
        signature_from_str(rewritten)


def test_annotated_metadata_is_found_wherever_the_annotated_is_nested():
    from collections.abc import Callable
    from typing import Annotated

    found = annotated_metadata(
        [
            list[Annotated[int, "a"]] | None,
            Callable[[Annotated[str, "b"]], Annotated[bool, "c", "d"]],
            int,
            "ForwardRef",
        ]
    )
    assert sorted(str(meta) for meta in found) == ["a", "b", "c", "d"]


@pytest.mark.parametrize(
    ("text", "defaults", "expected"),
    [
        (
            "(a: int, b: set[str] = <factory>, *, c: list[int] = <factory>)",
            {"b": ("<factory>", "set()"), "c": ("<factory>", "...")},
            "(a: int, b: set[str] = set(), *, c: list[int] = ...)",
        ),
        (
            "(cls: type[~x.UnixHost] = <class 'x.UnixHost'>, *, e: ~x.Element)",
            {"cls": ("<class 'x.UnixHost'>", "UnixHost")},
            "(cls: type[~x.UnixHost] = UnixHost, *, e: ~x.Element)",
        ),
        ("(f=<function <lambda>>)", {"f": ("<function <lambda>>", "...")}, "(f=...)"),
        ("(b: int = 1)", {"b": ("<factory>", "...")}, "(b: int = 1)"),
        ("(b: int = <factory>)", {"c": ("<factory>", "...")}, "(b: int = <factory>)"),
        ("b: int = <factory>", {"b": ("<factory>", "...")}, "b: int = <factory>"),
    ],
)
def test_replace_defaults_rewrites_only_the_named_default_as_shown(text, defaults, expected):
    assert replace_defaults(text, defaults) == expected


_T = typing.TypeVar("_T")


class _UnixHost:
    """Stands in for a class default; the name is what matters."""


class _Outer:
    class Inner:
        """A nested class: a caller writes ``_Outer.Inner``."""


class _NeedsData:
    """A pydantic ≥2.10 ``default_factory`` may take the validated data."""

    def __init__(self, data: "dict[str, object]") -> None:
        self.size = len(data)


def _hook() -> None:
    return None


def test_a_default_with_a_comma_or_bracket_in_a_string_keeps_every_other_character():
    """Only the rewritten default changes: Sphinx's own quoting and spacing stay byte for byte."""
    import inspect

    from sphinx.util.inspect import stringify_signature

    def f(sep: str = "a,b", cls: type = _UnixHost, pad: str = "x,  y") -> None:
        raise NotImplementedError

    def g(open_: str = "[", cls: type = _UnixHost) -> None:
        raise NotImplementedError

    shown = f"<class '{_UnixHost.__module__}._UnixHost'>"
    for func in (f, g):
        text = stringify_signature(inspect.signature(func), show_return_annotation=False)
        assert shown in text
        rewritten = replace_defaults(text, {"cls": (shown, "_UnixHost")})
        assert rewritten == text.replace(shown, "_UnixHost")
    f_text = stringify_signature(inspect.signature(f), show_return_annotation=False)
    assert replace_defaults(f_text, {"cls": (shown, "_UnixHost")}) == (
        "(sep: str = 'a,b', cls: type = _UnixHost, pad: str = 'x,  y')"
    )


def test_a_factory_default_shows_the_call_it_makes_or_a_placeholder():
    from otto.host.options import TelnetOptions

    assert factory_default(list) == "list()"
    assert factory_default(dict) == "dict()"
    assert factory_default(frozenset) == "frozenset()"
    assert factory_default(TelnetOptions) == "TelnetOptions()"
    assert factory_default(lambda: ["ssh"]) == "..."
    assert factory_default(None) == "..."


def test_a_factory_that_needs_an_argument_shows_a_placeholder():
    """pydantic calls a one-argument factory with the data; ``_NeedsData()`` would misstate it."""
    import pydantic
    from sphinx.util.inspect import signature as inspect_signature

    class Model(pydantic.BaseModel):
        model_config = pydantic.ConfigDict(arbitrary_types_allowed=True)
        a: int = 1
        d: _NeedsData = pydantic.Field(default_factory=_NeedsData)

    assert factory_default(_NeedsData) == "..."
    params = inspect_signature(Model).parameters
    assert python_defaults(params, field_factories(Model)) == {"d": ("<factory>", "...")}


class _Box(typing.Generic[_T]):
    """A generic class whose constructor needs an argument."""

    def __init__(self, item: _T) -> None:
        self.item = item


def test_a_parameterized_generic_factory_is_judged_by_its_class():
    """``default_factory=dict[str, Any]`` builds a ``dict``: the subscript changes nothing."""
    assert factory_default(dict[str, str]) == "dict()"
    assert factory_default(list[int]) == "list()"
    assert factory_default(set[str]) == "set()"
    assert factory_default(_Box[int]) == "..."


def test_an_annotated_factory_is_judged_by_the_class_it_wraps():
    """``Annotated[list, m]`` builds a ``list``; to 3.12 its origin ``Annotated`` is a class."""
    assert factory_default(typing.Annotated[list, "m"]) == "list()"
    assert factory_default(typing.Annotated[dict[str, int], "m"]) == "dict()"
    assert factory_default(typing.Annotated[_NeedsData, "m"]) == "..."


def test_a_nested_class_shows_its_qualified_name():
    class Local:
        """Defined inside a function: no name a caller could write."""

    assert object_default(_Outer.Inner) == "_Outer.Inner"
    assert factory_default(_Outer.Inner) == "_Outer.Inner()"
    assert object_default(Local) == "..."
    assert factory_default(Local) == "..."


def test_a_class_or_function_default_shows_its_name():
    from otto.host import UnixHost

    def local_hook() -> None:
        raise NotImplementedError

    assert object_default(UnixHost) == "UnixHost"
    assert object_default(_hook) == "_hook"
    assert object_default(len) == "len"
    assert object_default(local_hook) == "..."
    assert object_default(lambda: None) == "..."
    assert object_default("UnixHost") is None
    assert object_default(30) is None


def test_field_factories_reads_dataclass_and_pydantic_fields():
    import dataclasses

    import pydantic

    @dataclasses.dataclass
    class Plain:
        a: int = 0
        b: list[int] = dataclasses.field(default_factory=list)

    class Model(pydantic.BaseModel):
        a: int = 0
        b: dict[str, int] = pydantic.Field(default_factory=dict)

    @pydantic.dataclasses.dataclass
    class PydanticDataclass:
        b: set[str] = dataclasses.field(default_factory=set)

    assert field_factories(Plain) == {"b": list}
    assert field_factories(Model) == {"b": dict}
    assert field_factories(PydanticDataclass) == {"b": set}
    assert field_factories(Plain()) == {}
    assert field_factories(int) == {}


def test_sphinx_s_own_signature_with_non_python_defaults_parses_once_rewritten():
    """The text autodoc writes for factory, class and lambda defaults, made Python."""
    import dataclasses

    from sphinx.util.inspect import signature, signature_from_str, stringify_signature

    @dataclasses.dataclass
    class Spec:
        cls: type = _Outer.Inner
        names: set[str] = dataclasses.field(default_factory=set)
        hook: object = _hook
        anon: object = lambda: None
        terms: list[str] = dataclasses.field(default_factory=lambda: ["ssh"])
        sep: str = "a,b"

    text = stringify_signature(signature(Spec), show_return_annotation=False)
    with pytest.raises(SyntaxError):
        signature_from_str(text)
    defaults = python_defaults(signature(Spec).parameters, field_factories(Spec))
    rewritten = replace_defaults(text, defaults)
    assert rewritten == (
        "(cls: type = _Outer.Inner, names: set[str] = set(), hook: object = _hook,"
        " anon: object = ..., terms: list[str] = ..., sep: str = 'a,b')"
    )
    signature_from_str(rewritten)
