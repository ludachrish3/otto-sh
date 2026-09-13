"""OptionsSource and the declaring-class rule: what a body's options are built from.

Two sources, one rule. A CLI dispatch hands over the flat parsed kwargs; an
``ensure`` fixture hands over the suite's options INSTANCE. Both end in the
same pydantic construction, and the instance path shares a value only when
the field is inherited from the SAME class object on both sides — matching by
name would let an unrelated field with a familiar spelling leak into an install.
"""

from typing import Annotated

import pytest
import typer

from otto import options
from otto.params import OptionsSource, declaring_class, shared_field_values


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

    def test_same_name_different_class_does_not_flow(self) -> None:
        assert shared_field_values(InstallOpts, Unrelated(lab_env="x")) == {}

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

    def test_instance_mode_shares_by_declaring_class_and_defaults_the_rest(self) -> None:
        built = OptionsSource.from_instance(SuiteOpts(lab_env="x")).build(InstallOpts)
        assert (built.lab_env, built.ensure) == ("x", False)

    def test_instance_mode_with_none_builds_defaults(self) -> None:
        built = OptionsSource.from_instance(None).build(InstallOpts)
        assert (built.lab_env, built.ensure) == ("staging", False)

    def test_validation_failure_names_the_field(self) -> None:
        from pydantic import Field

        @options
        class Strict:
            retries: Annotated[int, typer.Option(help="r")] = Field(default=3, ge=0)

        with pytest.raises(typer.BadParameter, match="retries"):
            OptionsSource.from_kwargs({"retries": -1}).build(Strict)
