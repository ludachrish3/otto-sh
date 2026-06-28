"""Markers and metadata for signature-driven CLI exposure."""

from otto.utils import Arg, Exclude, Opt, cli_exposed


def test_markers_are_plain_data():
    a = Arg(variadic=True, elem_type=str, help="cmds")
    assert a.variadic is True and a.elem_type is str and a.help == "cmds"
    o = Opt(help="timeout")
    assert o.elem_type is None and o.help == "timeout"
    # Exclude is a reusable singleton — re-import proves identity, not just self-equality
    from otto.utils import Exclude as _E2

    assert Exclude is _E2
    assert type(Exclude).__name__ == "_Exclude"


def test_cli_exposed_records_success_message():
    @cli_exposed(name="put", success="Transfer complete.")
    async def put(self): ...

    assert put.__cli_name__ == "put"
    assert put.__cli_success__ == "Transfer complete."


def test_cli_exposed_success_defaults_none():
    @cli_exposed
    async def run(self): ...

    assert run.__cli_success__ is None
