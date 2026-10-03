"""One log line per product outcome in the host's lifecycle loops.

A real ``otto run install`` over several hosts and products used to print only
the hooks' own command echoes, so nobody could tell which product on which host
passed, was skipped, or stopped the run. These tests pin the line each loop adds
and, just as load-bearing, that the loops' control flow did not change.
"""

import logging

import pytest

from otto.host.dev_tool import DevTool
from otto.host.local_host import LocalHost
from otto.host.product import Product
from otto.result import Result
from otto.utils import Status


class _Product(Product):
    """A product whose verbs return scripted results, one per verb name."""

    def __init__(self, name, **results):
        self.name = name
        self.results = results
        self.calls: list[str] = []

    def _answer(self, verb):
        self.calls.append(verb)
        return self.results.get(verb, Result(Status.Success))

    async def stage(self, host):
        return self._answer("stage")

    async def install(self, host):
        return self._answer("install")

    async def uninstall(self, host):
        return self._answer("uninstall")

    async def is_installed(self, host):
        return True


class _Tool(DevTool):
    def __init__(self, name, **results):
        self.name = name
        self.results = results
        self.calls: list[str] = []

    def _answer(self, verb):
        self.calls.append(verb)
        return self.results.get(verb, Result(Status.Success))

    async def stage(self, host):
        return self._answer("stage")

    async def install(self, host):
        return self._answer("install")

    async def uninstall(self, host):
        return self._answer("uninstall")

    async def is_installed(self, host):
        return True


def _host(*, products=(), dev_tools=()):
    host = LocalHost()
    host.products = list(products)
    host.dev_tools = list(dev_tools)
    return host


def _lines(caplog) -> list[tuple[int, str]]:
    """``(level, text after the @host preamble)`` for the host logger's records."""
    out = []
    for rec in caplog.records:
        if rec.name != "otto.host.host":
            continue
        text = rec.getMessage()
        assert "| " in text, text
        out.append((rec.levelno, text.split("| ", 1)[1]))
    return out


@pytest.fixture
def caplog_host(caplog):
    with caplog.at_level(logging.INFO, logger="otto.host.host"):
        yield caplog


@pytest.mark.asyncio
async def test_install_logs_staged_then_installed_per_product_in_order(caplog_host):
    host = _host(products=[_Product("a"), _Product("b")])

    result = await host.install()

    assert result.status is Status.Success
    assert _lines(caplog_host) == [
        (logging.INFO, "a: staged"),
        (logging.INFO, "b: staged"),
        (logging.INFO, "a: installed"),
        (logging.INFO, "b: installed"),
    ]


@pytest.mark.asyncio
async def test_line_carries_the_host_preamble(caplog_host):
    host = _host(products=[_Product("a")])

    await host.install(stage_only=True)

    [record] = [r for r in caplog_host.records if r.name == "otto.host.host"]
    assert record.getMessage().startswith(f"[bold]@{host.name}")
    assert record.host is host  # type: ignore[attr-defined]


@pytest.mark.asyncio
async def test_a_skipped_install_says_why(caplog_host):
    skipped = Result(Status.Skipped, msg="already there")
    host = _host(products=[_Product("a"), _Product("b", install=skipped)])

    result = await host.install()

    assert result.status is Status.Success
    install_phase = _lines(caplog_host)[2:]  # after the two staged lines
    assert install_phase == [
        (logging.INFO, "a: installed"),
        (logging.INFO, "b: skipped — already there"),
    ]


@pytest.mark.asyncio
async def test_a_skipped_result_without_a_message_is_just_skipped(caplog_host):
    host = _host(products=[_Product("a", install=Result(Status.Skipped))])

    await host.install()

    assert (logging.INFO, "a: skipped") in _lines(caplog_host)


@pytest.mark.asyncio
async def test_a_failed_install_warns_and_still_short_circuits(caplog_host):
    failure = Result(Status.Error, msg="boom")
    a, b, c = _Product("a"), _Product("b", install=failure), _Product("c")
    host = _host(products=[a, b, c])

    result = await host.install()

    assert result is failure  # returned whole, as before
    assert c.calls == ["stage"]  # staged, but never installed
    # Staging walks all three, then install logs a's success and b's failure,
    # and nothing at all for c.
    assert _lines(caplog_host) == [
        (logging.INFO, "a: staged"),
        (logging.INFO, "b: staged"),
        (logging.INFO, "c: staged"),
        (logging.INFO, "a: installed"),
        (logging.WARNING, "b: error — boom"),
    ]


@pytest.mark.asyncio
async def test_a_failed_stage_warns_and_returns_the_failure(caplog_host):
    failure = Result(Status.Failed, msg="no space")
    host = _host(products=[_Product("a", stage=failure), _Product("b")])

    result = await host.install()

    assert result is failure
    assert _lines(caplog_host) == [(logging.WARNING, "a: failed — no space")]


@pytest.mark.asyncio
async def test_stage_logs_staged_and_skipped(caplog_host):
    host = _host(
        products=[_Product("a"), _Product("b", stage=Result(Status.Skipped, msg="unchanged"))]
    )

    result = await host.stage()

    assert result.status is Status.Success
    assert _lines(caplog_host) == [
        (logging.INFO, "a: staged"),
        (logging.INFO, "b: skipped — unchanged"),
    ]


@pytest.mark.asyncio
async def test_uninstall_is_best_effort_and_every_product_gets_a_line(caplog_host):
    failure = Result(Status.Error, msg="stuck")
    a, b = _Product("a", uninstall=failure), _Product("b")
    host = _host(products=[a, b])

    result = await host.uninstall(get_product_logs=False, get_debug_logs=False)

    assert result is failure
    assert a.calls == ["uninstall"]
    assert b.calls == ["uninstall"]
    assert _lines(caplog_host) == [
        (logging.WARNING, "a: error — stuck"),
        (logging.INFO, "b: uninstalled"),
    ]


@pytest.mark.asyncio
async def test_install_dev_tools_names_each_tool(caplog_host):
    host = _host(dev_tools=[_Tool("t1"), _Tool("t2")])

    result = await host.install_dev_tools()

    assert result.status is Status.Success
    assert _lines(caplog_host) == [
        (logging.INFO, "t1: installed"),
        (logging.INFO, "t2: installed"),
    ]


@pytest.mark.asyncio
async def test_install_dev_tools_still_stops_at_the_first_failure(caplog_host):
    failure = Result(Status.Error, msg="nope")
    t1, t2 = _Tool("t1", install=failure), _Tool("t2")
    host = _host(dev_tools=[t1, t2])

    result = await host.install_dev_tools()

    assert result is failure
    assert t2.calls == []
    assert _lines(caplog_host) == [(logging.WARNING, "t1: error — nope")]


@pytest.mark.asyncio
async def test_install_dev_tools_stage_failure_warns_and_stops(caplog_host):
    failure = Result(Status.Failed, msg="no space")
    t1, t2 = _Tool("t1", stage=failure), _Tool("t2")
    host = _host(dev_tools=[t1, t2])

    result = await host.install_dev_tools()

    assert result is failure
    assert t1.calls == ["stage"]
    assert t2.calls == []
    assert _lines(caplog_host) == [(logging.WARNING, "t1: failed — no space")]


@pytest.mark.asyncio
async def test_uninstall_dev_tools_is_best_effort_and_names_each_tool(caplog_host):
    failure = Result(Status.Error, msg="stuck")
    t1, t2 = _Tool("t1", uninstall=failure), _Tool("t2")
    host = _host(dev_tools=[t1, t2])

    result = await host.uninstall_dev_tools()

    assert result is failure
    assert t1.calls == ["uninstall"]
    assert t2.calls == ["uninstall"]
    assert _lines(caplog_host) == [
        (logging.WARNING, "t1: error — stuck"),
        (logging.INFO, "t2: uninstalled"),
    ]


@pytest.mark.asyncio
async def test_markup_in_a_message_is_escaped_not_interpreted(caplog_host):
    host = _host(products=[_Product("a", install=Result(Status.Error, msg="[sudo] denied [/x]"))])

    await host.install()

    [warning] = [r for r in caplog_host.records if r.levelno == logging.WARNING]
    from rich.text import Text

    assert Text.from_markup(warning.getMessage()).plain.endswith("a: error — [sudo] denied [/x]")


@pytest.mark.asyncio
async def test_a_dry_run_decline_adds_no_line(caplog_host):
    """The dry-run preview is its own surface; a declined verb logs nothing here."""
    from otto.result import NotRunResult

    declined = NotRunResult(Status.NotRun, command="x", msg="declined")
    host = _host(products=[_Product("a", stage=declined)])

    result = await host.stage()

    assert result is declined
    assert _lines(caplog_host) == []
