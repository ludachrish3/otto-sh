"""``otto host <id> install`` tells the user which products it passed over (#367).

An installed product is neither staged nor installed, and the one line that
says so must reach the terminal: the dispatch seam the CLI composes runs the
verb, and the production console handler renders what the user sees.
"""

import io

import pytest
import typer
from rich.console import Console

from otto.cli.expose import HostGroup
from otto.host.local_host import LocalHost
from otto.host.product import Product
from otto.logger import management
from otto.result import Result
from otto.utils import Status
from tests._fixtures.dispatch import DispatchRunner


class _Product(Product):
    def __init__(self, name, installed=False):
        self.name = name
        self.installed = installed
        self.calls: list[str] = []

    async def stage(self, host):
        self.calls.append("stage")
        return Result(Status.Success)

    async def install(self, host):
        self.calls.append("install")
        return Result(Status.Success)

    async def uninstall(self, host):
        self.calls.append("uninstall")
        return Result(Status.Success)

    async def is_installed(self, host):
        return self.installed


@pytest.fixture
def console(monkeypatch):
    """The production console handler, rendering into a buffer instead of the terminal."""
    management.reset()
    buf = io.StringIO()
    monkeypatch.setattr(management, "CONSOLE", Console(file=buf, width=300, force_terminal=False))
    management.install_console("INFO")
    yield buf
    management.reset()


def _app(monkeypatch, host) -> typer.Typer:
    import otto.host.os_profile as op
    from otto.host.os_profile import HostClassEntry
    from otto.host.specs import UnixHostSpec

    entry = HostClassEntry(LocalHost, UnixHostSpec)  # type: ignore[arg-type] — a local host
    monkeypatch.setattr(op, "HOST_CLASSES", {"h": entry})
    monkeypatch.setattr("otto.cli.expose.host_class_for_id", lambda hid: LocalHost)
    app = typer.Typer(name="host", cls=HostGroup)

    @app.callback(invoke_without_command=True)
    def main(ctx: typer.Context, host_id: str = typer.Argument("")):
        if ctx.resilient_parsing:
            return
        ctx.obj = host

    return app


def test_host_install_prints_the_skip_line_and_installs_only_the_missing(monkeypatch, console):
    done, fresh = _Product("a", installed=True), _Product("b")
    host = LocalHost()
    host.products = [done, fresh]

    r = DispatchRunner().invoke(_app(monkeypatch, host), [host.id, "install"])

    assert r.exit_code == 0, r.output
    lines = [line.rstrip() for line in console.getvalue().splitlines()]
    assert sum(line.endswith("a: skipped — already installed") for line in lines) == 1, lines
    assert any(line.endswith("b: installed") for line in lines), lines
    assert not any("[bold]" in line for line in lines), lines
    assert done.calls == []
    assert fresh.calls == ["stage", "install"]
