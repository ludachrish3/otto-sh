"""@cli_exposed carries an output_dir flag (default True)."""

from otto.utils import cli_exposed


def test_default_output_dir_true() -> None:
    @cli_exposed
    async def verb(self: object) -> None: ...

    assert getattr(verb, "__cli_output_dir__") is True  # noqa: B009 — dunder set by decorator


def test_output_dir_false_when_declared() -> None:
    @cli_exposed(output_dir=False)
    async def verb(self: object) -> None: ...

    assert getattr(verb, "__cli_output_dir__") is False  # noqa: B009 — dunder set by decorator
