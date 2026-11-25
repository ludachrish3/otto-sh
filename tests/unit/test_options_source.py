"""OptionsSource and the one rule that decides what a body's options are built from: the name.

A CLI dispatch hands over the flat parsed kwargs; a library caller or an
``ensure`` fixture hands over an options INSTANCE. On both paths a class takes
the values whose field NAME it carries (``init=True`` fields only), whichever
class declared them, and both end in the same pydantic construction.
"""

import dataclasses
from typing import Annotated

import pytest
import typer
from pydantic import Field

from otto import options
from otto.params import (
    OptionsSource,
    OptionsValidationError,
    build_options,
    declaring_class,
    shared_field_values,
)


@options
class Base:
    lab_env: Annotated[str, typer.Option(help="env")] = "staging"


@options
class InstallOpts(Base):
    ensure: Annotated[bool, typer.Option(help="ensure")] = False


@options
class SuiteOpts(Base):
    firmware: Annotated[str, typer.Option(help="fw")] = "latest"


@options
class Unrelated:
    lab_env: Annotated[str, typer.Option(help="env, but not Base's")] = "other"


@dataclasses.dataclass
class Needs:
    target: str


@options
class Strict:
    level: int = Field(default=1, ge=1)


@dataclasses.dataclass
class Texty:
    level: str = "low"


@dataclasses.dataclass
class Derived:
    lab_env: str = "staging"
    computed: str = dataclasses.field(default="unset", init=False)


@dataclasses.dataclass
class Carrier:
    lab_env: str = "x"
    computed: str = "leaked"


class TestDeclaringClass:
    def test_inherited_field_names_the_base(self) -> None:
        assert declaring_class(InstallOpts, "lab_env") is Base

    def test_own_field_names_the_subclass(self) -> None:
        assert declaring_class(InstallOpts, "ensure") is InstallOpts

    def test_unknown_field_raises_keyerror(self) -> None:
        with pytest.raises(KeyError, match="nope"):
            declaring_class(InstallOpts, "nope")


class TestSharedFieldValues:
    def test_same_declaring_class_flows(self) -> None:
        assert shared_field_values(InstallOpts, SuiteOpts(lab_env="x")) == {"lab_env": "x"}

    def test_same_name_different_class_flows(self) -> None:
        assert shared_field_values(InstallOpts, Unrelated(lab_env="x")) == {"lab_env": "x"}

    def test_an_init_false_field_never_travels_by_name(self) -> None:
        # Derived.computed is init=False: no value may be handed to it, and a
        # source's init=False field is not an input either.
        assert shared_field_values(Derived, Carrier()) == {"lab_env": "x"}
        built = OptionsSource.from_instance(Carrier()).build(Derived)
        assert (built.lab_env, built.computed) == ("x", "unset")
        assert shared_field_values(Carrier, Derived(lab_env="y")) == {"lab_env": "y"}

    def test_none_source_is_empty(self) -> None:
        assert shared_field_values(InstallOpts, None) == {}

    def test_non_dataclass_source_is_empty(self) -> None:
        assert shared_field_values(InstallOpts, object()) == {}

    def test_a_bare_class_source_is_empty_not_its_defaults(self) -> None:
        # `is_dataclass` answers True for a CLASS too, so without the
        # isinstance(source, type) arm this would read SuiteOpts.lab_env's
        # default off the class and report it as a value a caller chose.
        assert shared_field_values(InstallOpts, SuiteOpts) == {}


class TestOptionsSource:
    def test_kwargs_mode_picks_only_the_classes_fields(self) -> None:
        source = OptionsSource.from_kwargs({"lab_env": "x", "ensure": True, "firmware": "v2"})
        built = source.build(InstallOpts)
        assert (built.lab_env, built.ensure) == ("x", True)

    def test_instance_mode_matches_by_name_and_defaults_the_rest(self) -> None:
        built = OptionsSource.from_instance(Unrelated(lab_env="x")).build(InstallOpts)
        assert (built.lab_env, built.ensure) == ("x", False)

    def test_instance_mode_hands_back_the_instance_for_its_own_class(self) -> None:
        inst = SuiteOpts(lab_env="x")
        assert OptionsSource.from_instance(inst).build(SuiteOpts) is inst

    def test_a_class_not_offered_takes_only_given_names(self) -> None:
        src = OptionsSource.from_kwargs(
            {"lab_env": "x", "ensure": True}, given=frozenset({"ensure"}), offered=[InstallOpts]
        )
        assert src.build(InstallOpts).lab_env == "x"  # offered: everything parsed
        assert src.build(Unrelated).lab_env == "other"  # not offered, not given: its default

    def test_a_rejected_value_names_both_classes(self) -> None:
        with pytest.raises(OptionsValidationError, match=r"Strict.*level.*Texty"):
            OptionsSource.from_instance(Texty(level="high")).build(Strict)

    def test_a_prepared_instance_is_handed_back_unless_the_own_instance_shares_a_name(
        self,
    ) -> None:
        ready = InstallOpts(lab_env="ready")
        src = OptionsSource.from_kwargs({}, instances={InstallOpts: ready})
        assert src.build(InstallOpts) is ready
        own = dataclasses.replace(src, instance=Unrelated(lab_env="own"))  # shares lab_env
        assert own.build(InstallOpts).lab_env == "own"

    def test_a_usage_error_message_is_unchanged_without_suppliers(self) -> None:
        with pytest.raises(OptionsValidationError, match=r"^target: Field required$"):
            build_options(Needs, {})

    def test_instance_mode_with_none_builds_defaults(self) -> None:
        built = OptionsSource.from_instance(None).build(InstallOpts)
        assert (built.lab_env, built.ensure) == ("staging", False)

    def test_validation_failure_names_the_field(self) -> None:
        @options
        class Strict:
            retries: Annotated[int, typer.Option(help="r")] = Field(default=3, ge=0)

        # The library's own error, not typer.BadParameter: OptionsSource.build
        # is a library entrypoint, and the CLI translates it at its boundary.
        with pytest.raises(OptionsValidationError, match="retries"):
            OptionsSource.from_kwargs({"retries": -1}).build(Strict)

    def test_a_rejection_keeps_todays_text_with_no_supplier_or_its_own_class(self) -> None:
        @options
        class Retrying:
            retries: int = Field(default=3, ge=0)

        text = r"^retries: Input should be greater than or equal to 0$"
        with pytest.raises(OptionsValidationError, match=text):
            build_options(Retrying, {"retries": -1})
        with pytest.raises(OptionsValidationError, match=text):
            build_options(Retrying, {"retries": -1}, suppliers={"retries": Retrying})
