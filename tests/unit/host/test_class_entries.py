"""A ``class = "pkg.mod:Sub"`` entry builds the named DeclaredProduct subclass from its keys."""

from pathlib import Path

import pytest

from otto.declared import DeclaredEntry
from otto.host import shell_kind
from otto.host.declared_product import DeclaredProduct
from otto.host.dev_tool import DEV_TOOL_KIND_BUILDER, DEV_TOOL_KINDS
from otto.host.product import PRODUCT_KIND_BUILDER, PRODUCT_KINDS
from tests._fixtures import class_products

MOD = "tests._fixtures.class_products"


class _Host:
    id = "h1"
    default_dest_dir = Path("/opt/stage")
    cached_login_home = None


def _entry(cls, *, seam="products", match=None, **params):
    params.setdefault("artifact", "build/fw.bin")
    return DeclaredEntry(
        name="fw",
        kind=None,
        seam=seam,
        owner="r1",
        base_dir=Path("/sut"),
        params=params,
        cls=cls,
        match=match or {},
    )


def test_a_class_entry_builds_the_named_subclass_with_the_shell_keys():
    built = PRODUCT_KIND_BUILDER.build(
        [_entry(f"{MOD}:OverridesInstall", stage_dir="/opt/fw", check="test -f x")], _Host()
    )[0]
    assert type(built) is class_products.OverridesInstall
    assert (built.artifact, built.stage_dir, built.check_cmd) == (
        Path("/sut/build/fw.bin"),
        Path("/opt/fw"),
        "test -f x",
    )
    assert (built.kind, built.origin, built.owner) == (f"{MOD}:OverridesInstall", "declared", "r1")


def test_a_dev_tools_class_entry_lands_through_the_dev_tool_registry():
    built = DEV_TOOL_KIND_BUILDER.build(
        [_entry(f"{MOD}:OverridesInstall", seam="dev_tools")], _Host()
    )[0]
    assert type(built) is class_products.OverridesInstall


def test_subclass_fields_are_set_from_the_entry_by_annotation():
    built = PRODUCT_KIND_BUILDER.build(
        [
            _entry(
                f"{MOD}:WithFields",
                slot=3,
                label="rev2",
                strict=True,
                extra_dir="/var/x",
                tags=["a", "b"],
                whatever={"k": 1},
            )
        ],
        _Host(),
    )[0]
    assert (built.slot, built.label, built.strict) == (3, "rev2", True)
    assert built.extra_dir == Path("/var/x")
    assert built.tags == ["a", "b"]
    assert built.whatever == {"k": 1}  # an annotation the mapping does not know passes through


def test_a_subclass_field_with_a_default_may_be_omitted():
    built = PRODUCT_KIND_BUILDER.build([_entry(f"{MOD}:WithFields")], _Host())[0]
    assert (built.slot, built.label, built.tags) == (0, "none", [])


@pytest.mark.parametrize(
    ("params", "fragment"),
    [
        ({"slot": "3"}, r"'slot' must be an integer, got '3'"),
        ({"slot": True}, r"'slot' must be an integer, got True"),
        ({"label": 3}, r"'label' must be a string, got 3"),
        ({"strict": "yes"}, r"'strict' must be a bool, got 'yes'"),
        ({"extra_dir": 7}, r"'extra_dir' must be a path string, got 7"),
        ({"tags": "a"}, r"'tags' must be a list of strings, got 'a'"),
    ],
)
def test_a_subclass_field_of_the_wrong_shape_is_refused_naming_entry_and_field(params, fragment):
    with pytest.raises(
        ValueError, match=rf"\[\[products\]\] 'fw': class '{MOD}:WithFields': {fragment}"
    ):
        PRODUCT_KIND_BUILDER.build([_entry(f"{MOD}:WithFields", **params)], _Host())


def test_an_unknown_key_is_refused_listing_the_classs_valid_keys():
    with pytest.raises(ValueError, match="unknown param") as e:
        PRODUCT_KIND_BUILDER.build([_entry(f"{MOD}:WithFields", bogus=1)], _Host())
    text = str(e.value)
    assert text.startswith(
        f"[[products]] 'fw': class '{MOD}:WithFields' got unknown param(s): ['bogus']; valid: "
    )
    assert (
        "artifact, stage_dir, install, uninstall, check, cov_dir, debug_log_globs, instrumented"
        in text
    )
    assert text.endswith(", slot, label, strict, extra_dir, tags, whatever")


def test_string_annotations_and_a_classvar_do_not_defeat_field_mapping():
    built = PRODUCT_KIND_BUILDER.build(
        [_entry("tests._fixtures.class_products_future:Stringy", slot=4)], _Host()
    )[0]
    assert (built.slot, built.LIMIT) == (4, 3)


def test_an_init_false_field_is_never_settable_from_the_entry():
    ok = PRODUCT_KIND_BUILDER.build([_entry(f"{MOD}:WithComputed", slot=2)], _Host())[0]
    assert (ok.slot, ok.computed) == (2, 0)
    with pytest.raises(ValueError, match="unknown param") as e:
        PRODUCT_KIND_BUILDER.build([_entry(f"{MOD}:WithComputed", computed=1)], _Host())
    assert str(e.value).endswith(", slot")


def test_a_colonless_class_path_is_refused_naming_the_entry_and_the_path():
    with pytest.raises(ValueError, match=r"\[\[products\]\] 'fw': class 'nocolon' — "):
        PRODUCT_KIND_BUILDER.build([_entry("nocolon")], _Host())


def test_a_required_subclass_field_left_unset_is_refused_naming_it():
    with pytest.raises(
        ValueError,
        match=rf"\[\[products\]\] 'fw': class '{MOD}:RequiredField' requires a 'channel' key",
    ):
        PRODUCT_KIND_BUILDER.build([_entry(f"{MOD}:RequiredField")], _Host())


def test_an_import_failure_is_refused_naming_the_entry_and_the_path():
    with pytest.raises(
        ValueError,
        match=r"\[\[products\]\] 'fw': class 'no_such_pkg.mod:Thing' — "
        r"No module named 'no_such_pkg'",
    ):
        PRODUCT_KIND_BUILDER.build([_entry("no_such_pkg.mod:Thing")], _Host())


def test_a_missing_attribute_is_refused_naming_the_entry_and_the_path():
    with pytest.raises(ValueError, match=rf"\[\[products\]\] 'fw': class '{MOD}:Nope' — .*Nope"):
        PRODUCT_KIND_BUILDER.build([_entry(f"{MOD}:Nope")], _Host())


@pytest.mark.parametrize("attr", ["NotAProduct", "not_a_class"])
def test_something_that_is_not_a_declared_product_subclass_is_refused(attr):
    with pytest.raises(
        ValueError,
        match=rf"\[\[products\]\] 'fw': class '{MOD}:{attr}' is not a DeclaredProduct subclass",
    ):
        PRODUCT_KIND_BUILDER.build([_entry(f"{MOD}:{attr}")], _Host())


def test_a_class_entry_fails_every_ingest_even_when_the_match_misses():
    entry = _entry("no_such_pkg.mod:Thing", match={"id": "matches-no-host"})
    with pytest.raises(ValueError, match=r"No module named 'no_such_pkg'"):
        PRODUCT_KIND_BUILDER.build([entry], _Host())


def test_a_non_subclass_fails_every_ingest_even_when_the_match_misses():
    entry = _entry(f"{MOD}:NotAProduct", match={"id": "matches-no-host"})
    with pytest.raises(ValueError, match=r"is not a DeclaredProduct subclass"):
        PRODUCT_KIND_BUILDER.build([entry], _Host())


def test_the_shell_kind_and_the_class_route_build_the_same_product_from_the_same_keys():
    params = {
        "stage_dir": "/opt/fw",
        "install": "make {name}",
        "check": "test -f {cov_dir}/x",
        "cov_dir": "/var/cov",
        "debug_log_globs": ["*.log"],
        "instrumented": False,
    }
    via_kind = DeclaredEntry(
        name="fw",
        kind="shell",
        seam="products",
        owner="r1",
        base_dir=Path("/sut"),
        params={"artifact": "build/fw.bin", **params},
    )
    via_class = _entry("otto.host.declared_product:DeclaredProduct", **params)
    a = PRODUCT_KIND_BUILDER.build([via_kind], _Host())[0]
    b = PRODUCT_KIND_BUILDER.build([via_class], _Host())[0]
    assert type(a) is type(b) is DeclaredProduct
    assert {k: v for k, v in vars(a).items() if k not in ("kind", "source_entry")} == {
        k: v for k, v in vars(b).items() if k not in ("kind", "source_entry")
    }
    assert (a.kind, b.kind) == ("shell", "otto.host.declared_product:DeclaredProduct")


def test_class_entry_is_the_registries_class_factory():
    assert PRODUCT_KIND_BUILDER.class_factory() is shell_kind.class_entry
    assert DEV_TOOL_KIND_BUILDER.class_factory() is shell_kind.class_entry


def test_resolve_class_is_the_registries_class_resolver():
    assert PRODUCT_KIND_BUILDER.class_resolver() is shell_kind.resolve_class
    assert DEV_TOOL_KIND_BUILDER.class_resolver() is shell_kind.resolve_class


def test_the_registries_record_the_module_that_built_them():
    # The kind registries are plain Registry constructions in their seam modules,
    # so the engine records each seam module as the owner, not otto.declared.
    assert PRODUCT_KINDS.defined_in == "otto.host.product"
    assert DEV_TOOL_KINDS.defined_in == "otto.host.dev_tool"


def test_a_class_entry_without_an_artifact_is_refused_naming_the_class():
    entry = DeclaredEntry(
        name="fw",
        kind=None,
        seam="products",
        owner="r1",
        base_dir=Path("/sut"),
        params={},
        cls=f"{MOD}:OverridesInstall",
    )
    with pytest.raises(
        ValueError,
        match=rf"\[\[products\]\] 'fw': class '{MOD}:OverridesInstall' requires an 'artifact'",
    ):
        PRODUCT_KIND_BUILDER.build([entry], _Host())
