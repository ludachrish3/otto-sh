"""Positive typing fixture: every options read is typed as the class it names."""

from typing_extensions import assert_type

import otto
from otto import OttoContext, ProjectActions


@otto.options
class Sample:
    variant: str = "a"


def product_hook() -> None:
    assert_type(otto.get_options(Sample).variant, str)


class Actions(ProjectActions):
    def body(self) -> None:
        assert_type(self.ctx.options(Sample).variant, str)


def test_x(ctx: OttoContext) -> None:
    assert_type(ctx.options(Sample).variant, str)


def decorated_class_kept() -> None:
    assert_type(Sample(variant="x").variant, str)


@otto.options(verbs=["run", "test"])
class Registered:
    level: int = 0


def factory_form_kept() -> None:
    assert_type(Registered(level=1).level, int)
