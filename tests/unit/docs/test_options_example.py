"""The options guide's example still does what the page says.

``docs/setup/options-classes.md`` includes its snippets from
``docs/examples/options/acme_options.py``. This file loads that example by
path and runs its product hook, so the page cannot drift from code that works:
``--debug-level 3`` on a run command reaches ``Agent.install``.

The example registers ``Acme`` (and an instruction) at import; the root
conftest's registry isolation removes them again after each test.
"""

import importlib.util
import re
import sys
import warnings

import pytest

import otto
from otto.context import reset_context, set_context
from otto.params import verbs_for
from otto.result import CommandResult, Result, Results
from otto.utils import Status
from tests._fixtures.paths import CHECKOUT_ROOT

_EXAMPLE = CHECKOUT_ROOT / "docs" / "examples" / "options" / "acme_options.py"
_PAGE = CHECKOUT_ROOT / "docs" / "setup" / "options-classes.md"
_MODULE = "acme_options_example"


class _FakeHost:
    """A host that records what it was asked to run."""

    def __init__(self) -> None:
        self.ran: list[str] = []

    async def run(self, cmd: str) -> Result:
        self.ran.append(cmd)
        return Result(status=Status.Success)


@pytest.fixture
def example():
    """Import the example file by path, as its project's init module would be."""
    spec = importlib.util.spec_from_file_location(_MODULE, _EXAMPLE)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[_MODULE] = module
    try:
        # The example's `ensure` marker is registered by otto's pytest plugin
        # under `otto test`; this suite does not load that plugin.
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", pytest.PytestUnknownMarkWarning)
            spec.loader.exec_module(module)
        yield module
    finally:
        sys.modules.pop(_MODULE, None)


@pytest.mark.asyncio
async def test_the_debug_level_reaches_the_install_hook_from_a_run_flag(example, make_otto_context):
    ctx = make_otto_context()
    binding = set_context(ctx)
    try:
        ctx.bind_verb_options(verb="run", kwargs={"debug_level": 3})
        host = _FakeHost()
        await example.Agent().install(host)
    finally:
        reset_context(binding)
    assert host.ran == ["agent-install --log-level 3"]


@pytest.mark.asyncio
async def test_the_same_read_works_in_a_block_with_no_context(example):
    host = _FakeHost()
    with otto.use_options(example.Acme(debug_level=3)):
        await example.Agent().install(host)
        await example.AcmeHost.restart_agent(host)
    assert host.ran == ["agent-install --log-level 3", "agent-restart --log-level 3"]


@pytest.mark.asyncio
async def test_with_nothing_given_the_hook_reads_the_default(example):
    host = _FakeHost()
    await example.Agent().install(host)
    assert host.ran == ["agent-install --log-level 0"]


class _FleetHost:
    """A fleet host: records each command and answers ``run`` with a real ``Results``.

    ``agent-ctl log-level`` reads back *level*, the level the agent logs at.
    """

    def __init__(self, host_id: str, level: str) -> None:
        self.id = host_id
        self.level = level
        self.ran: list[str] = []

    async def run(self, cmd: str) -> Results:
        self.ran.append(cmd)
        value = self.level if cmd == "agent-ctl log-level" else ""
        item = CommandResult(status=Status.Success, value=value, command=cmd, retcode=0)
        return Results.collect(items=[item])


def _fleet(example, monkeypatch, level: str) -> "list[_FleetHost]":
    hosts = [_FleetHost("router1", level), _FleetHost("router2", level)]
    monkeypatch.setattr(example, "all_hosts", lambda: hosts)
    return hosts


@pytest.mark.asyncio
async def test_the_test_example_checks_every_host_at_the_given_level(example, monkeypatch):
    hosts = _fleet(example, monkeypatch, "3")
    with otto.use_options(example.Acme(debug_level=3)):
        await example.TestAgent().test_agent_logs_at_the_debug_level()
    assert [h.ran for h in hosts] == [["agent-ctl log-level"]] * 2


@pytest.mark.asyncio
async def test_the_test_examples_assertion_fails_on_a_host_at_another_level(example, monkeypatch):
    """The example's own assertion runs: a host logging at 0 under level 3 fails it."""
    _fleet(example, monkeypatch, "0")
    with otto.use_options(example.Acme(debug_level=3)), pytest.raises(AssertionError):
        await example.TestAgent().test_agent_logs_at_the_debug_level()


@pytest.mark.asyncio
async def test_the_instruction_example_sets_the_level_on_every_host(example, monkeypatch):
    hosts = _fleet(example, monkeypatch, "3")
    with otto.use_options(example.Acme(debug_level=3)):
        await example.set_log_level()
    assert [h.ran for h in hosts] == [["agent-ctl log-level 3"]] * 2


def test_the_class_is_registered_for_run_and_test(example):
    assert verbs_for(example.Acme) == ["run", "test"]


def test_every_snippet_marker_the_page_includes_exists_once():
    page = _PAGE.read_text(encoding="utf-8")
    source = _EXAMPLE.read_text(encoding="utf-8")
    assert "../examples/options/acme_options.py" in page
    markers = re.findall(r':(?:start-after|end-before): "(# doc: (?:begin|end) [\w-]+)"', page)
    names = {m.split()[-1] for m in markers}
    assert names == {"project-wide", "product", "host-method", "test", "instruction"}
    for name in names:
        for marker in (f"# doc: begin {name}", f"# doc: end {name}"):
            assert marker in markers, f"the page does not use {marker!r}"
            assert len(re.findall(rf"^\s*{re.escape(marker)}$", source, re.MULTILINE)) == 1, marker


def test_the_validating_fields_block_rejects_a_negative_level_with_the_page_text():
    """The page's ``Field(ge=0)`` class refuses -1 with the message the page prints."""
    from otto.cli.invoke import usage_error_from
    from otto.params import OptionsValidationError, build_options

    page = _PAGE.read_text(encoding="utf-8")
    fences = re.findall(r"^```python\n(.*?)^```$", page, re.MULTILINE | re.DOTALL)
    (source,) = [f for f in fences if "Field(" in f]
    namespace: dict = {}
    exec(compile(source, str(_PAGE), "exec"), namespace)  # noqa: S102
    with pytest.raises(OptionsValidationError, match="debug_level") as caught:
        build_options(namespace["Acme"], {"debug_level": -1})
    shown = "Invalid value: debug_level: Input should be greater than or equal to 0"
    assert shown in page
    assert usage_error_from(caught.value).format_message() == shown


_ADVANCED = CHECKOUT_ROOT / "docs" / "setup" / "options-advanced.md"


def _advanced_fence(needle: str) -> str:
    """The one python fence on the advanced page that contains *needle*."""
    page = _ADVANCED.read_text(encoding="utf-8")
    fences = re.findall(r"^```python\n(.*?)^```$", page, re.MULTILINE | re.DOTALL)
    matching = [f for f in fences if needle in f]
    assert len(matching) == 1, needle
    return matching[0]


def test_the_advanced_pages_block_and_replace_example_runs(example, monkeypatch):
    """The page's `use_options` / `dataclasses.replace` block runs as written."""
    monkeypatch.setitem(sys.modules, "my_lab_instructions", example)
    source = _advanced_fence("dataclasses.replace")
    exec(compile(source, str(_ADVANCED), "exec"), {})  # noqa: S102


def test_the_advanced_pages_dev_example_runs():
    """An instance gives its defaults too: the page's `dev` block runs as written."""
    source = _advanced_fence("class HookOptions")
    exec(compile(source, str(_ADVANCED), "exec"), {})  # noqa: S102
