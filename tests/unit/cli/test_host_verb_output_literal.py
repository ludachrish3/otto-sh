"""A host verb's returned device text prints exactly as the device wrote it (#697).

``otto host <id> read-file`` returns the file's text, and the leaf render
printed it through Rich with markup on: ``image:[sha256:abc]`` came out as
``image:``, ``[red]b[/red]`` lost its tags, and a lone ``[/x]`` raised. The
text is the device's, not otto's markup, so a host verb's payload is printed
literally. Driven through the real dispatch seam the CLI composes.
"""

import pytest
import typer

from otto.cli.expose import HostGroup
from otto.result import Result
from otto.utils import Status, cli_exposed
from tests._fixtures.dispatch import DispatchRunner

_DEVICE_TEXT = (
    "image:[sha256:abc]\na [red]b[/red] c\nd [link=x]e[/link] f\ng [x:y] h\nk [/x] l"
    "\nroot:x:0:0:root:/root:/bin/bash"
    "\n" + " ".join(f"word{i}" for i in range(40))  # 230+ chars: must not be wrapped at 80
)


def _entry(cls: type) -> object:
    from otto.host.os_profile import HostClassEntry
    from otto.host.specs import UnixHostSpec

    return HostClassEntry(cls, UnixHostSpec)  # type: ignore[arg-type] — a double, not a RemoteHost


class _Host:
    id = "h1"

    @cli_exposed
    async def read_file(self) -> str:
        return _DEVICE_TEXT

    @cli_exposed
    async def report(self) -> Result:
        return Result(Status.Success, value=_DEVICE_TEXT)

    async def close(self) -> None:
        pass


def _app(monkeypatch) -> typer.Typer:
    import otto.host.os_profile as op

    monkeypatch.setattr(op, "HOST_CLASSES", {"h": _entry(_Host)})
    monkeypatch.setattr("otto.cli.expose.host_class_for_id", lambda hid: _Host)
    app = typer.Typer(name="host", cls=HostGroup)
    host = _Host()

    @app.callback(invoke_without_command=True)
    def main(ctx: typer.Context, host_id: str = typer.Argument("")):
        if ctx.resilient_parsing:
            return
        ctx.obj = host

    return app


@pytest.mark.parametrize("verb", ["read-file", "report"])
def test_a_host_verbs_text_payload_prints_verbatim(monkeypatch, verb):
    r = DispatchRunner().invoke(_app(monkeypatch), ["h1", verb])
    assert r.exit_code == 0, r.output
    for line in _DEVICE_TEXT.splitlines():
        assert line in r.output, (line, r.output)
    assert "\\[" not in r.output, r.output
