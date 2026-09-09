"""Structural guard: every host field has exactly one home (spec 2026-09-09)."""

import ast
import dataclasses
import inspect
import typing
from pathlib import Path

import pytest

from otto.host.docker_host import DockerContainerHost
from otto.host.embedded_host import EmbeddedHost, ZephyrHost
from otto.host.host import BaseHost, Host
from otto.host.local_host import LocalHost
from otto.host.remote_host import RemoteHost
from otto.host.unix_host import UnixHost

BASES = (BaseHost, RemoteHost)
LEAVES = (UnixHost, EmbeddedHost, ZephyrHost, LocalHost, DockerContainerHost)


@pytest.mark.parametrize("base", BASES)
def test_bases_are_kw_only_unslotted_dataclasses(base):
    assert dataclasses.is_dataclass(base)
    assert "__slots__" not in vars(base), "a slotted base strips __dict__ from every family"
    positional = [f.name for f in dataclasses.fields(base) if f.init and not f.kw_only]
    assert positional == ([] if base is BaseHost else ["ip"])


@pytest.mark.parametrize(
    ("leaf", "positional"),
    [
        (UnixHost, ["ip", "creds"]),
        (EmbeddedHost, ["ip"]),
        (ZephyrHost, ["ip"]),
        (LocalHost, []),
        (
            DockerContainerHost,
            ["parent", "container_id", "project", "service", "compose_project"],
        ),
    ],
)
def test_leaf_positional_constructor_shape(leaf, positional):
    params = [
        p.name
        for p in inspect.signature(leaf).parameters.values()
        if p.kind is inspect.Parameter.POSITIONAL_OR_KEYWORD
    ]
    assert params == positional


@pytest.mark.parametrize("leaf", LEAVES)
def test_resources_default_factory_is_frozenset(leaf):
    """A plain str would iterate as its CHARACTERS at the gate; the factory is
    the one place that rejects it (kept from the retired contract sweep)."""
    by_name = {f.name: f for f in dataclasses.fields(leaf)}
    assert by_name["resources"].default_factory is frozenset


# Value-policy overrides a leaf may carry (spec §3.4). A base field
# re-declared in a leaf that is NOT listed here fails test 3; a stale entry
# here fails it too.
OVERRIDES: dict[type, frozenset[str]] = {
    UnixHost: frozenset({"creds", "os_name"}),
    EmbeddedHost: frozenset(
        {"has_bash", "os_type", "term", "transfer", "valid_terms", "valid_transfers"}
    ),
    ZephyrHost: frozenset({"command_frame", "os_name", "os_type"}),
    LocalHost: frozenset({"name"}),
    DockerContainerHost: frozenset({"name"}),
}
POSITIONAL_OVERRIDES = {UnixHost: {"creds"}}


def _is_class_var(annotation: object) -> bool:
    if isinstance(annotation, str):
        return annotation.startswith("ClassVar")
    return typing.get_origin(annotation) is typing.ClassVar or annotation is typing.ClassVar


def _own_annotations(cls: type) -> dict[str, object]:
    # inspect.get_annotations, never cls.__annotations__ (PEP 649 on 3.14).
    return inspect.get_annotations(cls)


@pytest.mark.parametrize("base", BASES)
def test_bases_carry_no_bare_instance_annotation(base):
    """On a dataclass a bare annotation IS a field, so the only way this can
    fail is a ClassVar-less annotation that dataclasses skipped — which cannot
    happen — or a future base that is not a dataclass. Kept as the statement
    of the rule."""
    fields = {f.name for f in dataclasses.fields(base)}
    bare = [
        name
        for name, ann in _own_annotations(base).items()
        if not _is_class_var(ann) and name not in fields
    ]
    assert bare == []


# The Host protocol's attribute block, as it stands in the tree today. A
# change to that block (adding, removing, or renaming a name) is a CONTRACT
# change and must edit this set on purpose — pinning the size, not just
# subset-checking it, is what keeps this test from going vacuous if the
# block ever shrinks.
PROTOCOL_ATTRIBUTES = frozenset(
    {
        "log",
        "id",
        "name",
        "lab_info",
        "resources",
        "inventory_ref",
        "products",
        "dev_tools",
        "toolchain",
        "debug_log_globs",
        "power_control",
        "source_lab",
        "has_bash",
    }
)


def test_protocol_attributes_have_a_home_on_basehost():
    """The Host protocol's attribute block is the contract; BaseHost is the home.
    Name-for-name, annotation-text-for-annotation-text. `element` is a
    protocol PROPERTY and a BaseHost FIELD — the one sanctioned pairing."""
    proto = {name: ann for name, ann in _own_annotations(Host).items() if not _is_class_var(ann)}
    assert set(proto) == PROTOCOL_ATTRIBUTES, sorted(set(proto) ^ PROTOCOL_ATTRIBUTES)
    base = {f.name: f.type for f in dataclasses.fields(BaseHost)}
    assert set(proto) <= set(base), sorted(set(proto) - set(base))
    for name, ann in proto.items():
        assert str(base[name]).strip("'\"") == str(ann).strip("'\""), name
    assert isinstance(vars(Host)["element"], property)
    assert "element" in base


def _base_field_names(leaf: type) -> set[str]:
    names: set[str] = set()
    for base in leaf.__mro__[1:]:
        if dataclasses.is_dataclass(base):
            names |= {f.name for f in dataclasses.fields(base)}
    return names


@pytest.mark.parametrize("leaf", LEAVES)
def test_leaf_overrides_exactly_the_allowlist(leaf):
    own = set(_own_annotations(leaf)) - {
        n for n, a in _own_annotations(leaf).items() if _is_class_var(a)
    }
    redeclared = own & _base_field_names(leaf)
    assert redeclared == OVERRIDES[leaf], (
        f"{leaf.__name__} re-declares {sorted(redeclared)}; allowlist says "
        f"{sorted(OVERRIDES[leaf])}"
    )
    by_name = {f.name: f for f in dataclasses.fields(leaf)}
    for name in redeclared - POSITIONAL_OVERRIDES.get(leaf, set()):
        assert by_name[name].kw_only or not by_name[name].init, (
            f"{leaf.__name__}.{name}: an override must stay keyword-only"
        )


@pytest.mark.parametrize("leaf", LEAVES)
def test_overrides_carry_no_docstring(leaf):
    """Attribute docstrings do not exist at runtime, so this reads the source:
    an override's AnnAssign must not be followed by a string expression."""
    src = Path(inspect.getsourcefile(leaf)).read_text()
    module = ast.parse(src)
    # Top-level only, and exactly one: a module-level class named after the
    # leaf, not the first same-named ClassDef anywhere (nested/conditional
    # included) that a plain ast.walk would find.
    matches = [n for n in module.body if isinstance(n, ast.ClassDef) and n.name == leaf.__name__]
    assert len(matches) == 1, f"{leaf.__name__}: expected exactly one top-level ClassDef"
    # Deliberately shallow: only the class body's direct statements are
    # scanned, so an AnnAssign nested under `if TYPE_CHECKING:` or similar is
    # not an override this test can see — which is correct, since such an
    # annotation carries no runtime field either.
    body = matches[0].body
    offenders = []
    for i, node in enumerate(body):
        if not (isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name)):
            continue
        if node.target.id not in OVERRIDES[leaf]:
            continue
        nxt = body[i + 1] if i + 1 < len(body) else None
        if (
            isinstance(nxt, ast.Expr)
            and isinstance(nxt.value, ast.Constant)
            and isinstance(nxt.value.value, str)
        ):
            offenders.append(node.target.id)
    assert offenders == [], f"{leaf.__name__}: overrides with a docstring: {offenders}"
