"""Negative typing fixture: a misspelled field of a read options instance is an error."""

import otto
from otto import ProjectActions


@otto.options
class Sample:
    variant: str = "a"


def product_hook() -> None:
    _ = otto.get_options(Sample).varient  # ty: ignore[missing-attribute]


class Actions(ProjectActions):
    def body(self) -> None:
        _ = self.ctx.options(Sample).varient  # ty: ignore[missing-attribute]
