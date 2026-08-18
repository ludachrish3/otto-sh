"""Declared toolchain tools, dev-tool installation, and the ``install_tools`` dispatcher.

Three seams meet here:

* ``ToolchainTool`` — the lab-declarable unit a toolchain installs (name,
  source, dest, owner, mode).
* ``install_dev_tools`` / ``install_toolchain_tools`` — the two kinds, each
  with its own lifecycle.
* ``install_tools`` — the dispatcher whose *defaults* are the contract: dev
  tools on, toolchain off.

The host under test is the shared ``recording_host`` double (see
``tests/unit/host/conftest.py``), so every assertion here is about the calls
``BaseHost`` actually makes, not about a mock's configuration.
"""

from pathlib import Path

import pytest

from otto.host.dev_tool import DevTool
from otto.host.toolchain import Toolchain, ToolchainTool
from otto.result import CommandNotRunError, Result
from otto.utils import Status
from tests.conftest import active_context


class _ScriptedTool(DevTool):
    """Dev tool that appends ``name:phase`` to a shared list and can fail one phase."""

    def __init__(self, name, events, fail_on=None):
        self.name = name
        self.owner = None
        self.events = events
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
        return self.fail_on is None


def _recording_coro(calls, label):
    """Build an async no-op that appends *label* to *calls* and succeeds."""

    async def _run(*_args, **_kwargs):
        calls.append(label)
        return Result(Status.Success)

    return _run


# =========================================================================== #
# install_toolchain_tools
# =========================================================================== #


@pytest.mark.asyncio
async def test_install_toolchain_tools_puts_then_chowns(recording_host):
    host = recording_host
    host.toolchain = Toolchain(
        tools=[ToolchainTool(name="gdb", source=Path("/tc/bin/gdb"), dest=Path("/usr/local/bin"))]
    )
    result = await host.install_toolchain_tools()
    assert result.is_ok
    # Kills: an implementation that only puts (skipping ownership) or only
    # execs (skipping transfer).
    assert host.put_calls == [(Path("/tc/bin/gdb"), Path("/usr/local/bin"), "755")]
    assert any("chown root" in cmd and "gdb" in cmd for cmd in host.exec_calls)


@pytest.mark.asyncio
async def test_install_toolchain_tools_chowns_the_installed_path_as_root(recording_host):
    """The chown targets ``dest/name`` and runs elevated.

    Kills: chowning the *source* path (a local path, meaningless on the host)
    or the destination *directory* (which would hand the whole directory over),
    and kills dropping the ``as_user("root")`` elevation that makes chown
    possible in a root-owned directory at all.
    """
    host = recording_host
    host.toolchain = Toolchain(
        tools=[
            ToolchainTool(
                name="libfoo.so",
                source=Path("/build/out/libfoo.so"),
                dest=Path("/usr/lib"),
                user="sqluser",
            )
        ]
    )
    assert (await host.install_toolchain_tools()).is_ok
    assert host.exec_calls == ["chown sqluser /usr/lib/libfoo.so"]
    assert host.as_user_calls == ["root"]


@pytest.mark.asyncio
async def test_install_toolchain_tools_quotes_the_declared_user(recording_host):
    """*user* is lab-declared data, so it is quoted exactly as the path is.

    Kills: interpolating ``tool.user`` bare. A declared user carrying a space
    makes ``chown`` a three-argument command that chowns the wrong file (and
    one carrying a metacharacter makes it two commands) — the same hazard the
    path is already quoted against, on the other half of the same line.
    """
    recording_host.toolchain = Toolchain(
        tools=[
            ToolchainTool(
                name="libfoo.so",
                source=Path("/build/out/libfoo.so"),
                dest=Path("/usr/lib"),
                user="app user",
            )
        ]
    )
    assert (await recording_host.install_toolchain_tools()).is_ok
    assert recording_host.exec_calls == ["chown 'app user' /usr/lib/libfoo.so"]


@pytest.mark.asyncio
async def test_install_toolchain_tools_honors_per_tool_mode(recording_host):
    """A tool's declared mode reaches ``put`` — kills a hard-coded 755."""
    recording_host.toolchain = Toolchain(
        tools=[
            ToolchainTool(name="gdbinit", source=Path("/tc/gdbinit"), dest=Path("/etc"), mode="644")
        ]
    )
    assert (await recording_host.install_toolchain_tools()).is_ok
    assert recording_host.put_calls == [(Path("/tc/gdbinit"), Path("/etc"), "644")]


@pytest.mark.asyncio
async def test_install_toolchain_tools_renames_when_the_declared_name_differs(recording_host):
    """A declared name that differs from the source basename is an explicit ``mv``.

    THE DISCRIMINATING CASE, and the only one where the two candidate
    implementations disagree: no transfer backend renames — every one writes
    ``dest_dir / src.name`` — so after ``put`` the artifact is at
    ``/usr/local/bin/arm-linux-gnueabihf-gdb``. An implementation that chowns
    ``dest / tool.name`` without moving anything is chowning a path that does
    not exist, and leaves the tool both misnamed and un-owned. Every other
    fixture in this file sets ``name == source.name``, where both
    implementations look identical — which is exactly why this test exists.
    """
    host = recording_host
    host.toolchain = Toolchain(
        tools=[
            ToolchainTool(
                name="gdb",
                source=Path("/tc/bin/arm-linux-gnueabihf-gdb"),
                dest=Path("/usr/local/bin"),
            )
        ]
    )
    assert (await host.install_toolchain_tools()).is_ok
    # Order matters: the rename must precede the chown, or the chown addresses
    # a path that is not there yet.
    assert host.exec_calls == [
        "mv /usr/local/bin/arm-linux-gnueabihf-gdb /usr/local/bin/gdb",
        "chown root /usr/local/bin/gdb",
    ]


@pytest.mark.asyncio
async def test_install_toolchain_tools_does_not_rename_when_the_name_matches(recording_host):
    """No ``mv`` when there is nothing to rename — kills an unconditional move."""
    host = recording_host
    host.toolchain = Toolchain(
        tools=[ToolchainTool(name="gdb", source=Path("/tc/bin/gdb"), dest=Path("/usr/local/bin"))]
    )
    assert (await host.install_toolchain_tools()).is_ok
    assert host.exec_calls == ["chown root /usr/local/bin/gdb"]


@pytest.mark.asyncio
async def test_install_toolchain_tools_stops_when_the_rename_fails(recording_host):
    """A failed ``mv`` never chowns — kills taking ownership of the wrong path."""
    host = recording_host
    host.toolchain = Toolchain(
        tools=[
            ToolchainTool(name="gdb", source=Path("/tc/arm-gdb"), dest=Path("/usr/local/bin")),
        ]
    )
    host.script_exec("mv: cannot stat", ok=False)
    result = await host.install_toolchain_tools()
    assert not result.is_ok
    assert result.value == "mv: cannot stat"
    assert host.exec_calls == ["mv /usr/local/bin/arm-gdb /usr/local/bin/gdb"]


@pytest.mark.asyncio
async def test_install_toolchain_tools_declines_before_elevating_under_dry_run(recording_host):
    """A dry run returns the transfer's NotRun decline; it never reaches elevation.

    The verb's dry-run safety is PURELY an ordering property: ``put`` declines
    with a ``NotRun`` result the loop returns on, while ``as_user`` does not
    decline at all — it raises ``CommandNotRunError``. So hoisting the
    elevation above the transfer (or out around the loop) swaps a clean
    decline for a traceback while every other test here still passes. This
    injects the hostile condition rather than inheriting it: the double's
    ``put`` and ``as_user`` carry the same two shapes the real host does.
    """
    host = recording_host
    host.toolchain = Toolchain(
        tools=[ToolchainTool(name="gdb", source=Path("/tc/bin/gdb"), dest=Path("/usr/local/bin"))]
    )
    with active_context(dry_run=True):
        result = await host.install_toolchain_tools()
    assert result.status is Status.NotRun
    assert not result.is_ok
    # The announcement happened, the elevation and the commands did not.
    assert len(host.put_calls) == 1
    assert host.as_user_calls == []
    assert host.exec_calls == []


@pytest.mark.asyncio
async def test_recording_host_as_user_refuses_under_dry_run(recording_host):
    """The double's elevation really does raise — so the test above can discriminate.

    A guard is worthless if the condition it depends on is inert. This asserts
    the injected hostile shape itself: were ``as_user`` to quietly succeed
    under a dry run, the ordering test above would pass against a hoisted
    elevation too.
    """
    with active_context(dry_run=True), pytest.raises(CommandNotRunError):
        async with recording_host.as_user("root"):
            pytest.fail("the dry-run elevation must refuse before the body runs")


@pytest.mark.asyncio
async def test_install_toolchain_tools_with_no_tools_is_noop_success(recording_host):
    result = await recording_host.install_toolchain_tools()
    assert result.is_ok
    assert recording_host.put_calls == []


@pytest.mark.asyncio
async def test_install_toolchain_tools_stops_at_the_first_failed_chown(recording_host):
    """A failed chown ends the walk and its own result is what comes back.

    Kills: continuing to the next tool after a failure (which would report the
    last tool's success as the whole verb's answer), and kills discarding the
    failing command's result — the CLI's exit code is built from its retcode.
    """
    host = recording_host
    host.toolchain = Toolchain(
        tools=[
            ToolchainTool(name="gdb", source=Path("/tc/gdb"), dest=Path("/usr/local/bin")),
            ToolchainTool(name="strace", source=Path("/tc/strace"), dest=Path("/usr/local/bin")),
        ]
    )
    host.script_exec("chown: operation not permitted", ok=False)
    result = await host.install_toolchain_tools()
    assert not result.is_ok
    assert result.value == "chown: operation not permitted"
    assert len(host.put_calls) == 1
    assert host.exec_calls == ["chown root /usr/local/bin/gdb"]


@pytest.mark.asyncio
async def test_install_toolchain_tools_stops_when_the_transfer_fails(recording_host):
    """A failed put never reaches the chown — kills chowning a file that is not there."""
    host = recording_host
    host.toolchain = Toolchain(
        tools=[ToolchainTool(name="gdb", source=Path("/tc/gdb"), dest=Path("/usr/local/bin"))]
    )

    async def _failing_put(src_files, dest_dir, mode=None):
        host.put_calls.append((src_files, dest_dir, mode))
        return Result(Status.Failed, msg="no such file")

    host.put = _failing_put
    result = await host.install_toolchain_tools()
    assert not result.is_ok
    assert result.msg == "no such file"
    assert host.exec_calls == []


# =========================================================================== #
# install_dev_tools
# =========================================================================== #


@pytest.mark.asyncio
async def test_install_dev_tools_stages_then_installs_each(recording_host):
    events = []
    recording_host.dev_tools = [_ScriptedTool("a", events), _ScriptedTool("b", events)]
    result = await recording_host.install_dev_tools()
    assert result.is_ok
    # Kills: installing before staging, and kills staging every tool before
    # installing any (each tool is carried through both phases in turn).
    assert events == ["a:stage", "a:install", "b:stage", "b:install"]


@pytest.mark.asyncio
async def test_install_dev_tools_first_failure_wins_and_stops_the_walk(recording_host):
    """A failed stage skips that tool's install and every later tool.

    Kills: best-effort continuation, which would install tool ``b`` onto a host
    where ``a`` — possibly its prerequisite — never landed.
    """
    events = []
    recording_host.dev_tools = [
        _ScriptedTool("a", events, fail_on="stage"),
        _ScriptedTool("b", events),
    ]
    result = await recording_host.install_dev_tools()
    assert not result.is_ok
    assert result.msg == "a stage failed"
    assert events == ["a:stage"]


@pytest.mark.asyncio
async def test_install_dev_tools_with_no_tools_is_noop_success(recording_host):
    assert (await recording_host.install_dev_tools()).is_ok


# =========================================================================== #
# install_tools — the dispatcher's defaults ARE the contract
# =========================================================================== #


@pytest.mark.asyncio
async def test_install_tools_defaults_dev_on_toolchain_off(recording_host):
    # Kills: swapped defaults — toolchain transfers are large and rare, dev
    # tools small and common; the spec fixes dev=True, toolchain=False.
    calls = []
    recording_host.install_dev_tools = _recording_coro(calls, "dev")
    recording_host.install_toolchain_tools = _recording_coro(calls, "toolchain")
    await recording_host.install_tools()
    assert calls == ["dev"]


@pytest.mark.asyncio
async def test_install_tools_runs_both_kinds_when_toolchain_is_asked_for(recording_host):
    calls = []
    recording_host.install_dev_tools = _recording_coro(calls, "dev")
    recording_host.install_toolchain_tools = _recording_coro(calls, "toolchain")
    assert (await recording_host.install_tools(toolchain=True)).is_ok
    assert calls == ["dev", "toolchain"]


@pytest.mark.asyncio
async def test_install_tools_dev_false_skips_dev_tools(recording_host):
    calls = []
    recording_host.install_dev_tools = _recording_coro(calls, "dev")
    recording_host.install_toolchain_tools = _recording_coro(calls, "toolchain")
    assert (await recording_host.install_tools(dev=False, toolchain=True)).is_ok
    assert calls == ["toolchain"]


@pytest.mark.asyncio
async def test_install_tools_failed_dev_install_never_starts_the_toolchain(recording_host):
    """Kills: running both kinds unconditionally and returning only the last."""
    calls = []

    async def _failing_dev():
        calls.append("dev")
        return Result(Status.Failed, msg="dev tool exploded")

    recording_host.install_dev_tools = _failing_dev
    recording_host.install_toolchain_tools = _recording_coro(calls, "toolchain")
    result = await recording_host.install_tools(toolchain=True)
    assert not result.is_ok
    assert result.msg == "dev tool exploded"
    assert calls == ["dev"]


# =========================================================================== #
# ToolchainTool declaration defaults
# =========================================================================== #


def test_toolchain_tool_defaults_to_root_owned_executable():
    tool = ToolchainTool(name="gdb", source=Path("/tc/gdb"), dest=Path("/usr/local/bin"))
    assert (tool.user, tool.mode) == ("root", "755")


def test_toolchain_declares_no_tools_by_default():
    # Kills a shared mutable default: two toolchains must not share one list.
    first, second = Toolchain(), Toolchain()
    assert first.tools == []
    first.tools.append(ToolchainTool(name="x", source=Path("/x"), dest=Path("/y")))
    assert second.tools == []
