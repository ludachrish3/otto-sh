"""Owner-scoped product lifecycle, ``cleanup``, and ``is_clean``.

The owner filter is what keeps one repo's default actions off another repo's
products on a shared host: every lifecycle verb takes ``owner=None`` (the
pre-owner behavior — all products) and narrows to ``Product.owner == owner``
when asked. ``is_installed``'s empty-list rule applies to the FILTERED list,
which is the one place vacuous truth would flip an answer.

``cleanup`` is strictly more than ``uninstall``: products first (with their
logs), then dev tools, then the toolchain's declared tools — each removed at
``dest/name``, the path ``install_toolchain_tools`` renames them to.
``is_clean`` is the matching question, and it asks the device rather than
inferring: under a dry run it refuses instead of reporting a host clean nobody
looked at.

Doubles come from ``tests/unit/host/conftest.py``; the products and tools here
are local scripts that record their phases into a shared list.
"""

from pathlib import Path

import pytest

from otto.host.dev_tool import DevTool
from otto.host.product import Product
from otto.host.toolchain import Toolchain, ToolchainTool
from otto.result import CommandNotRunError, Result
from otto.utils import Status
from tests.conftest import active_context


class _ScriptedProduct(Product):
    """Product recording ``name:phase`` into a shared list, with a fixed install state."""

    def __init__(self, name, owner=None, events=None, installed=False, fail_on=None):
        self.name = name
        self.owner = owner
        self.events = events if events is not None else []
        self.installed = installed
        self.fail_on = fail_on

    def _record(self, phase):
        self.events.append(f"{self.name}:{phase}")
        ok = self.fail_on != phase
        return Result(Status.Success if ok else Status.Failed, msg=f"{self.name} {phase} failed")

    async def stage(self, host):
        del host
        return self._record("stage")

    async def install(self, host):
        del host
        return self._record("install")

    async def uninstall(self, host):
        del host
        return self._record("uninstall")

    async def is_installed(self, host):
        del host
        return self.installed


class _ScriptedTool(DevTool):
    """Dev tool recording ``name:phase``, with a fixed install state."""

    def __init__(self, name, owner=None, events=None, installed=False, fail_on=None):
        self.name = name
        self.owner = owner
        self.events = events if events is not None else []
        self.installed = installed
        self.fail_on = fail_on

    def _record(self, phase):
        self.events.append(f"{self.name}:{phase}")
        ok = self.fail_on != phase
        return Result(Status.Success if ok else Status.Failed, msg=f"{self.name} {phase} failed")

    async def stage(self, host):
        del host
        return self._record("stage")

    async def install(self, host):
        del host
        return self._record("install")

    async def uninstall(self, host):
        del host
        return self._record("uninstall")

    async def is_installed(self, host):
        del host
        return self.installed


def _tool(name="gdb", source="/tc/bin/gdb", dest="/usr/local/bin"):
    return ToolchainTool(name=name, source=Path(source), dest=Path(dest))


# =========================================================================== #
# Owner filter on the product lifecycle
# =========================================================================== #


@pytest.mark.asyncio
async def test_install_owner_filter_touches_only_owned_products(recording_host):
    events = []
    recording_host.products = [
        _ScriptedProduct("a", owner="acme", events=events),
        _ScriptedProduct("b", owner="other", events=events),
    ]
    assert (await recording_host.install(owner="acme")).is_ok
    assert "a:install" in events
    assert "b:install" not in events
    # The stage half is filtered too, not just the install half.
    assert "b:stage" not in events


@pytest.mark.asyncio
async def test_stage_owner_filter_touches_only_owned_products(recording_host):
    events = []
    recording_host.products = [
        _ScriptedProduct("a", owner="acme", events=events),
        _ScriptedProduct("b", owner="other", events=events),
    ]
    assert (await recording_host.stage(owner="acme")).is_ok
    assert events == ["a:stage"]


@pytest.mark.asyncio
async def test_owner_none_keeps_the_pre_owner_behavior(recording_host):
    # Kills: a filter that treats None as a sentinel owner value and matches
    # only products with no owner — every existing caller would go silent.
    events = []
    recording_host.products = [
        _ScriptedProduct("a", owner="acme", events=events),
        _ScriptedProduct("b", owner=None, events=events),
    ]
    assert (await recording_host.stage()).is_ok
    assert events == ["a:stage", "b:stage"]


@pytest.mark.asyncio
async def test_is_installed_with_owner_and_no_owned_products_is_false(recording_host):
    # Mirrors the empty-products rule: nothing that could be installed is not
    # "installed". Kills: all([]) vacuous truth on the FILTERED list, which
    # would report a repo installed on every host it never deployed to.
    recording_host.products = [_ScriptedProduct("b", owner="other", installed=True)]
    assert await recording_host.is_installed(owner="acme") is False


@pytest.mark.asyncio
async def test_is_installed_ignores_another_repos_missing_product(recording_host):
    # Kills: the filter being dropped on the read verbs — repo A would report
    # "not installed" because repo B has not deployed here.
    recording_host.products = [
        _ScriptedProduct("a", owner="acme", installed=True),
        _ScriptedProduct("b", owner="other", installed=False),
    ]
    assert await recording_host.is_installed(owner="acme") is True
    assert await recording_host.is_installed() is False


@pytest.mark.asyncio
async def test_is_uninstalled_carries_the_owner_through(recording_host):
    recording_host.products = [
        _ScriptedProduct("a", owner="acme", installed=True),
        _ScriptedProduct("b", owner="other", installed=False),
    ]
    assert await recording_host.is_uninstalled(owner="acme") is False
    assert await recording_host.is_uninstalled(owner="other") is True


# =========================================================================== #
# cleanup
# =========================================================================== #


@pytest.mark.asyncio
async def test_cleanup_uninstalls_then_removes_tools(recording_host, tmp_path):
    events = []
    recording_host.products = [_ScriptedProduct("app", events=events)]
    recording_host.dev_tools = [_ScriptedTool("gdbserver", events=events)]
    recording_host.toolchain = Toolchain(tools=[_tool(dest="/d")])
    with active_context(output_dir=tmp_path):
        result = await recording_host.cleanup()
    assert result.is_ok
    # Kills: removing tooling out from under a product that is still installed.
    assert events.index("app:uninstall") < events.index("gdbserver:uninstall")
    assert any("rm -f" in c and "/d/gdb" in c for c in recording_host.exec_calls)


@pytest.mark.asyncio
async def test_cleanup_removes_the_installed_name_not_the_source_basename(recording_host, tmp_path):
    """The installed path is ``dest/name`` — what ``install_toolchain_tools`` renamed to.

    Kills: removing ``dest/source.name``, which leaves the renamed artifact on
    the host forever and reports success doing it.
    """
    recording_host.toolchain = Toolchain(
        tools=[_tool(name="gdb", source="/tc/bin/arm-none-eabi-gdb", dest="/usr/local/bin")]
    )
    with active_context(output_dir=tmp_path):
        assert (await recording_host.cleanup()).is_ok
    assert recording_host.exec_calls == ["rm -f /usr/local/bin/gdb"]


@pytest.mark.asyncio
async def test_cleanup_is_best_effort_and_reports_the_first_failure(recording_host, tmp_path):
    """Kills: returning at the first failure, which strands everything after it."""
    events = []
    recording_host.products = [_ScriptedProduct("app", events=events, fail_on="uninstall")]
    recording_host.dev_tools = [_ScriptedTool("gdbserver", events=events)]
    recording_host.toolchain = Toolchain(tools=[_tool(dest="/d")])
    with active_context(output_dir=tmp_path):
        result = await recording_host.cleanup()
    assert not result.is_ok
    assert result.msg == "app uninstall failed"
    assert "gdbserver:uninstall" in events
    assert any("/d/gdb" in c for c in recording_host.exec_calls)


@pytest.mark.asyncio
async def test_cleanup_forwards_the_log_flags_to_uninstall(recording_host, tmp_path):
    # Kills: flags accepted at the cleanup seam and dropped one level down —
    # the per-repo walk turns debug logs off exactly this way.
    seen = {}

    async def _uninstall(get_product_logs=True, get_debug_logs=True, owner=None):
        seen.update(product=get_product_logs, debug=get_debug_logs, owner=owner)
        return Result(Status.Success)

    recording_host.uninstall = _uninstall
    with active_context(output_dir=tmp_path):
        assert (await recording_host.cleanup(get_debug_logs=False)).is_ok
    assert seen == {"product": True, "debug": False, "owner": None}


@pytest.mark.asyncio
async def test_cleanup_declines_under_a_dry_run_instead_of_raising(recording_host, tmp_path):
    """The toolchain removal's result is reported WHOLE, never repacked.

    ``Result(removal.status, msg=removal.value)`` reads ``value`` on a
    ``NotRunResult``, which RAISES ``CommandNotRunError`` — a dry run would
    traceback out of cleanup instead of declining. Kills that repack.
    """
    recording_host.toolchain = Toolchain(tools=[_tool(dest="/d")])
    with active_context(dry_run=True, output_dir=tmp_path):
        result = await recording_host.cleanup(get_product_logs=False, get_debug_logs=False)
    assert result.status is Status.NotRun
    assert not result.is_ok


# =========================================================================== #
# is_clean
# =========================================================================== #


@pytest.mark.asyncio
async def test_is_clean_true_with_nothing_declared(recording_host):
    assert await recording_host.is_clean() is True


@pytest.mark.asyncio
async def test_is_clean_false_while_a_product_is_installed(recording_host):
    recording_host.products = [_ScriptedProduct("app", installed=True)]
    assert await recording_host.is_clean() is False


@pytest.mark.asyncio
async def test_is_clean_false_while_dev_tool_installed(recording_host):
    # Kills: is_clean = is_uninstalled alone (ignoring the tool lifecycles).
    recording_host.products = []
    recording_host.dev_tools = [_ScriptedTool("gdbserver", installed=True)]
    assert await recording_host.is_clean() is False


@pytest.mark.asyncio
async def test_is_clean_false_while_a_toolchain_tool_is_present(recording_host):
    recording_host.toolchain = Toolchain(tools=[_tool(dest="/d")])
    recording_host.script_exec(ok=True)  # `test -e /d/gdb` finds it
    assert await recording_host.is_clean() is False
    assert recording_host.exec_calls == ["test -e /d/gdb"]


@pytest.mark.asyncio
async def test_is_clean_true_when_the_toolchain_tool_is_gone(recording_host):
    recording_host.toolchain = Toolchain(tools=[_tool(dest="/d")])
    recording_host.script_exec(ok=False)  # `test -e /d/gdb` says no
    assert await recording_host.is_clean() is True


@pytest.mark.asyncio
async def test_is_clean_refuses_under_a_dry_run(recording_host):
    """A dry run asked the device nothing; "clean" would be a fabricated fact."""
    recording_host.toolchain = Toolchain(tools=[_tool(dest="/d")])
    with active_context(dry_run=True), pytest.raises(CommandNotRunError):
        await recording_host.is_clean()


# =========================================================================== #
# The host-global toolchain pair (remove_toolchain_tools / toolchain_tools_absent)
#
# Extracted from cleanup/is_clean because the orchestrator needs exactly these
# two steps ON THEIR OWN: one toolchain serves every owner on a host, so the
# per-repo walk must not touch it and the lab-level verb sweeps it once. Two
# copies of "which path is the tool at" is the drift this extraction prevents,
# so the tests below pin BOTH the helpers and the fact that cleanup/is_clean
# go through them.
# =========================================================================== #


@pytest.mark.asyncio
async def test_remove_toolchain_tools_removes_each_declared_name(recording_host):
    recording_host.toolchain = Toolchain(
        tools=[
            _tool(name="gdb", source="/tc/bin/arm-none-eabi-gdb", dest="/usr/local/bin"),
            _tool(name="gcov", source="/tc/bin/gcov", dest="/opt/bin"),
        ]
    )
    assert (await recording_host.remove_toolchain_tools()).is_ok
    assert recording_host.exec_calls == ["rm -f /usr/local/bin/gdb", "rm -f /opt/bin/gcov"]


@pytest.mark.asyncio
async def test_remove_toolchain_tools_is_best_effort_and_reports_the_first_failure(recording_host):
    # Kills: returning at the first failure, which strands every later tool.
    recording_host.toolchain = Toolchain(
        tools=[_tool(name="gdb", dest="/d"), _tool(name="gcov", dest="/d")]
    )
    recording_host.script_exec(ok=False)  # the gdb removal refuses
    result = await recording_host.remove_toolchain_tools()
    assert not result.is_ok
    assert recording_host.exec_calls == ["rm -f /d/gdb", "rm -f /d/gcov"]


@pytest.mark.asyncio
async def test_remove_toolchain_tools_with_no_tools_is_a_noop_success(recording_host):
    assert (await recording_host.remove_toolchain_tools()).is_ok
    assert recording_host.exec_calls == []


@pytest.mark.asyncio
async def test_remove_toolchain_tools_declines_under_a_dry_run(recording_host):
    """The removal result is reported WHOLE — a repack reads ``value`` and raises."""
    recording_host.toolchain = Toolchain(tools=[_tool(dest="/d")])
    with active_context(dry_run=True):
        result = await recording_host.remove_toolchain_tools()
    assert result.status is Status.NotRun


@pytest.mark.asyncio
async def test_cleanup_removes_the_toolchain_through_the_extracted_helper(recording_host, tmp_path):
    # Kills: a re-inlined rm -f loop in cleanup. The orchestrator's host-global
    # sweep calls remove_toolchain_tools(), so a second copy here would drift
    # from it the moment either changes.
    calls = []

    async def _removal():
        calls.append("remove")
        return Result(Status.Success)

    recording_host.remove_toolchain_tools = _removal
    recording_host.toolchain = Toolchain(tools=[_tool(dest="/d")])
    with active_context(output_dir=tmp_path):
        assert (await recording_host.cleanup()).is_ok
    assert calls == ["remove"]
    assert recording_host.exec_calls == []  # nothing removed behind the helper's back


@pytest.mark.asyncio
async def test_toolchain_tools_absent_answers_the_device(recording_host):
    recording_host.toolchain = Toolchain(tools=[_tool(dest="/d")])
    recording_host.script_exec(ok=True)  # `test -e /d/gdb` finds it
    assert await recording_host.toolchain_tools_absent() is False
    assert recording_host.exec_calls == ["test -e /d/gdb"]

    recording_host.script_exec(ok=False)  # …and now it does not
    assert await recording_host.toolchain_tools_absent() is True


@pytest.mark.asyncio
async def test_toolchain_tools_absent_refuses_under_a_dry_run(recording_host):
    """Same refusal as ``is_clean``: a dry run asked the device nothing."""
    recording_host.toolchain = Toolchain(tools=[_tool(dest="/d")])
    with active_context(dry_run=True), pytest.raises(CommandNotRunError):
        await recording_host.toolchain_tools_absent()


@pytest.mark.asyncio
async def test_is_clean_asks_the_toolchain_question_through_the_extracted_helper(recording_host):
    # Kills: a re-inlined `test -e` loop — the orchestrator's host-global
    # cleanliness sweep asks toolchain_tools_absent(), and two copies of the
    # probe drift apart (which path, and whether a refusal raises).
    calls = []

    async def _absent():
        calls.append("absent")
        return False

    recording_host.toolchain_tools_absent = _absent
    recording_host.toolchain = Toolchain(tools=[_tool(dest="/d")])
    assert await recording_host.is_clean() is False
    assert calls == ["absent"]
    assert recording_host.exec_calls == []
