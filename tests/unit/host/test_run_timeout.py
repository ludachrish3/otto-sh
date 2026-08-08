"""Tests for the cumulative timeout parameter of BaseHost.run().

The deadline-based budget distributes a single timeout across sequential
commands: each command receives the remaining time, and fast commands
donate surplus to slower ones.
"""

import asyncio
import contextlib
import os
from unittest.mock import AsyncMock, patch

import pytest

from otto.host.local_host import LocalHost
from otto.host.login_proxy import Cred
from otto.host.unix_host import UnixHost
from otto.logger.mode import LogMode
from otto.result import CommandResult
from otto.utils import Status

# ---------------------------------------------------------------------------
# Unit tests (mocked — fast, deterministic)
# ---------------------------------------------------------------------------


@pytest.fixture
def host() -> UnixHost:
    """Bare UnixHost, no connections established."""
    return UnixHost(
        ip="10.0.0.1", element="box", creds=[Cred(login="user", password="pass")], log=LogMode.QUIET
    )


class TestRunTimeout:
    """Unit tests for deadline-based timeout propagation."""

    @pytest.mark.asyncio
    async def test_no_timeout_uses_the_default_budget(self, host: UnixHost):
        """Without an explicit timeout, the list form budgets DEFAULT_COMMAND_TIMEOUT."""
        from otto.host.host import DEFAULT_COMMAND_TIMEOUT

        ok = CommandResult(status=Status.Success, value="hi", command="echo hi", retcode=0)
        with patch.object(host, "_run_one", new_callable=AsyncMock, return_value=ok) as mock:
            await host.run(["echo hi"])
        actual = mock.call_args.kwargs["timeout"]
        # List form = cumulative budget, so this is just under the full default.
        assert 0 < actual <= DEFAULT_COMMAND_TIMEOUT
        assert actual > DEFAULT_COMMAND_TIMEOUT - 1.0

    @pytest.mark.asyncio
    async def test_timeout_passes_remaining_to_run_one(self, host: UnixHost):
        """With a timeout, each _run_one receives the remaining budget."""
        ok = CommandResult(status=Status.Success, value="ok", command="cmd", retcode=0)
        with patch.object(host, "_run_one", new_callable=AsyncMock, return_value=ok) as mock:
            await host.run(["cmd1", "cmd2"], timeout=10.0)

        assert mock.call_count == 2
        # First call should get ~10s, second should get slightly less
        first_timeout = mock.call_args_list[0].kwargs["timeout"]
        second_timeout = mock.call_args_list[1].kwargs["timeout"]
        assert first_timeout > 9.0  # nearly full budget
        assert second_timeout > 0  # still has remaining time
        assert first_timeout > second_timeout  # budget decreases

    @pytest.mark.asyncio
    async def test_budget_exhausted_skips_remaining_commands(self, host: UnixHost):
        """When the budget runs out, remaining commands are skipped."""

        async def slow_cmd(cmd, **kwargs):
            # Simulate a command that takes nearly all the budget
            await asyncio.sleep(0.08)
            return CommandResult(status=Status.Success, value="ok", command=cmd, retcode=0)

        with patch.object(host, "_run_one", new_callable=AsyncMock, side_effect=slow_cmd):
            result = await host.run(
                ["slow1", "slow2", "skipped"],
                timeout=0.1,
            )

        # First two might run; third should be skipped with Status.Error
        assert result.status == Status.Error
        skipped = [r for r in result if "budget exhausted" in r.value]
        assert len(skipped) >= 1

    @pytest.mark.asyncio
    async def test_fast_commands_donate_surplus(self, host: UnixHost):
        """Fast commands leave surplus for later commands."""
        call_timeouts: list[float] = []

        async def track_timeout(cmd, **kwargs):
            call_timeouts.append(kwargs.get("timeout"))
            return CommandResult(status=Status.Success, value="ok", command=cmd, retcode=0)

        with patch.object(host, "_run_one", new_callable=AsyncMock, side_effect=track_timeout):
            await host.run(["fast1", "fast2", "fast3"], timeout=5.0)

        # All three should get nearly the full budget since each is instant
        assert len(call_timeouts) == 3
        for t in call_timeouts:
            assert t > 4.5, f"Expected > 4.5s remaining, got {t}"


# ---------------------------------------------------------------------------
# Integration tests (LocalHost — real session, real shell)
# ---------------------------------------------------------------------------


class TestRunTimeoutIntegration:
    """Integration tests using real LocalHost shell sessions."""

    @pytest.mark.asyncio
    async def test_all_commands_complete_within_budget(self):
        """Fast commands all complete when given a generous budget."""
        host = LocalHost()
        try:
            result = await host.run(
                ["echo one", "echo two", "echo three"],
                timeout=10.0,
            )
            assert result.status == Status.Success
            assert len(result) == 3
            assert all(r.status == Status.Success for r in result)
            assert "one" in result[0].value
            assert "two" in result[1].value
            assert "three" in result[2].value
        finally:
            await host.close()

    @pytest.mark.asyncio
    async def test_slow_command_times_out_and_session_recovers(self):
        """A slow command triggers timeout recovery, and the session stays usable."""
        host = LocalHost()
        try:
            # sleep 10 will exceed the 0.1s budget — _run_one's wait_for fires,
            # triggers Ctrl+C recovery, returns Status.Error
            result = await host.run(
                ["sleep 10", "echo after"],
                timeout=0.1,
            )
            assert result[0].status == Status.Error
            assert "timed out" in result[0].value.lower()

            # 'echo after' should be skipped (budget exhausted) or timed out
            if len(result) > 1:
                assert result[1].status == Status.Error

            # Session should still be healthy — verify by running another command
            result2 = (await host.run("echo recovered")).only
            assert result2.status == Status.Success
            assert "recovered" in result2.value
        finally:
            await host.close()

    @pytest.mark.asyncio
    async def test_no_timeout_preserves_default_behavior(self):
        """Without timeout, run behaves exactly as before."""
        host = LocalHost()
        try:
            result = await host.run(["echo hello", "echo world"])
            assert result.status == Status.Success
            assert len(result) == 2
        finally:
            await host.close()

    @pytest.mark.asyncio
    async def test_surplus_time_donated_to_later_commands(self):
        """Donation survives the real path: the last command is GRANTED the surplus.

        Asserts the allocation, not the race. The earlier shape ran a real
        ``sleep 0.1`` against a 0.5s budget and asserted only that everything
        succeeded, which was wrong in both directions: it false-failed on a
        loaded CI runner (the granted budget there was 99.2% of the 0.5s —
        donation had worked; the machine just needed more than 0.4s to
        fork+exec a sleep), and it could not fail on the regression it names,
        because an even split still grants 0.5/3 = 0.167s and ``sleep 0.1``
        finishes inside that.

        The budget handed to each command IS the observable property, so spy
        the seam that receives it. ``_run_one`` still runs for real — real
        shell, real fork+exec, real output — and the generous budget makes the
        sleep a 100x margin instead of a 4.8x race.

        Neither assertion below is a wall-clock bound, which is the point: a
        first cut asserted ``granted[2] > budget - 1.0``, and that is the old
        defect in miniature — it fails iff the first two commands burn a whole
        second, roughly a 7x margin on a cold worker whose first subprocess
        spawn lands here, against the 4.8x inflation that produced the
        original flake. The two that replaced it are load-INVARIANT and
        load-SAFE respectively, and both are needed: each catches a regression
        the other misses (see the comments at each).

        Scope: this observes ALLOCATION only. That ``_run_one`` then honours
        the timeout it was handed is a different property, pinned by
        ``test_slow_command_times_out_and_session_recovers`` and
        ``test_reap_is_bounded_when_process_ignores_sigterm``; the per-command
        ``min(sc.timeout, remaining)`` cap is pinned in test_shell_command.py.
        Don't delete those thinking this test subsumes them.
        """
        budget = 10.0
        host = LocalHost()
        loop = asyncio.get_running_loop()
        granted: list[tuple[float, float]] = []
        run_one = host._run_one

        async def spy(cmd, **kwargs):
            # Delegates rather than substitutes (unlike the AsyncMock doubles
            # above) — the point is to observe the real path, not replace it.
            granted.append((loop.time(), kwargs["timeout"]))
            return await run_one(cmd, **kwargs)

        try:
            with patch.object(host, "_run_one", new=spy):
                result = await host.run(
                    ["echo fast1", "echo fast2", "sleep 0.1 && echo done"],
                    timeout=budget,
                )

            assert len(granted) == 3
            # Donation, stated exactly: each command is granted whatever is
            # LEFT of the shared budget. An even split grants budget/3 and is
            # red here. Load-INVARIANT, not merely tolerant — a slower machine
            # moves the observed clock and the expected grant together, so the
            # margin never erodes. The tolerance covers only the microseconds
            # between the product reading the clock and this spy reading it
            # (no await separates them), not machine speed.
            first_clock, _ = granted[0]
            for clock, grant in granted:
                expected = budget - (clock - first_clock)
                assert grant == pytest.approx(expected, abs=0.1), (
                    f"grant {grant} is not the remaining budget {expected} "
                    f"— donation regressed (an even split would grant {budget / 3})"
                )
            # ...and the deadline is real, so the grants strictly SHRINK. This
            # is the one the check above cannot make: grants that ignore
            # elapsed time entirely (all three == budget) sit inside the
            # tolerance whenever the commands are fast. Load-SAFE: it needs
            # time to advance, not work to finish inside a bound, so load
            # makes it more true rather than less.
            assert granted[0][1] > granted[1][1] > granted[2][1]

            assert result.status == Status.Success
            assert len(result) == 3
            assert all(r.status == Status.Success for r in result)
            assert "done" in result[2].value
        finally:
            await host.close()


class TestValidateTimeout:
    """The entry-point validator rejects values asyncio.wait_for misreads."""

    def test_default_is_thirty_seconds(self):
        from otto.host.host import DEFAULT_COMMAND_TIMEOUT

        assert DEFAULT_COMMAND_TIMEOUT == 30.0

    @pytest.mark.parametrize("good", [0, 0.0, 0.5, 30.0, 3600, float("inf")])
    def test_accepts_non_negative_numbers_and_inf(self, good):
        from otto.host.host import _validate_timeout

        assert _validate_timeout(good) == float(good)

    # `None`/str/bool are deliberately invalid per the annotation; tests/ is
    # excluded from ty (pyproject.toml [tool.ty.src] exclude), so passing them
    # here needs no suppression.
    @pytest.mark.parametrize("bad", [None, "30", True, False, [1]])
    def test_rejects_non_numbers(self, bad):
        from otto.host.host import _validate_timeout

        with pytest.raises(TypeError, match="timeout must be a number"):
            _validate_timeout(bad)

    def test_rejects_nan(self):
        from otto.host.host import _validate_timeout

        with pytest.raises(ValueError, match="must not be NaN"):
            _validate_timeout(float("nan"))

    @pytest.mark.parametrize("bad", [-1, -0.001, float("-inf")])
    def test_rejects_negatives_including_neg_inf(self, bad):
        from otto.host.host import _validate_timeout

        with pytest.raises(ValueError, match="must be >= 0"):
            _validate_timeout(bad)


class TestTimedOutFlag:
    """Every timeout path marks the result, so callers need no string matching."""

    def test_defaults_to_false(self):
        r = CommandResult(status=Status.Success, value="", command="x", retcode=0)
        assert r.timed_out is False

    @pytest.mark.asyncio
    async def test_local_exec_timeout_sets_flag(self):
        host = LocalHost(log=LogMode.QUIET)
        try:
            # `exec` (not a bare `sleep 10`) so dash execs sleep in place of
            # the shell instead of forking it as a child: proc.terminate()
            # then kills the actual sleep directly. A bare `sleep 10` leaves
            # dash's forked grandchild running past the timeout, holding the
            # stdout pipe open for the full 10s and tripping the suite's
            # asyncio-transport-leak detector (tests/_fixtures/_transport_leaks.py)
            # on an unrelated later test.
            result = await host.exec("exec sleep 10", timeout=0.1)
        finally:
            await host.close()
        assert result.status == Status.Error
        assert result.timed_out is True
        assert "timed out" in result.value

    @pytest.mark.asyncio
    async def test_budget_exhausted_skip_sets_flag(self, host: UnixHost):
        async def slow_cmd(cmd, **kwargs):
            await asyncio.sleep(0.08)
            return CommandResult(status=Status.Success, value="ok", command=cmd, retcode=0)

        with patch.object(host, "_run_one", new_callable=AsyncMock, side_effect=slow_cmd):
            results = await host.run(["slow1", "slow2", "skipped"], timeout=0.1)

        skipped = [r for r in results if "budget exhausted" in r.value]
        assert skipped, "expected at least one skipped command"
        assert all(r.timed_out for r in skipped)

    @pytest.mark.asyncio
    async def test_reap_is_bounded_when_process_ignores_sigterm(self, monkeypatch):
        """proc.terminate() can be ignored; the post-terminate reap must not hang.

        The spawned command traps and drops SIGTERM (so `proc.terminate()` has
        no effect) and is `exec`'d in place — a single process, not forked —
        so a `proc.kill()` cleanup can't leave an orphaned child behind (see
        `test_local_exec_timeout_sets_flag` above for what happens when a
        command DOES fork). `_EXEC_REAP_TIMEOUT` is monkeypatched down so the
        test stays fast; the outer `asyncio.wait_for` bounds the whole call so
        a regression to an unbounded reap FAILS this test instead of hanging
        the suite.

        Also asserts the real OS process is actually gone once the call
        returns: giving up on the bounded reap without escalating to SIGKILL
        would leave the ignored-SIGTERM process running with our pipes still
        held open -- the same leak class as `test_local_exec_timeout_sets_flag`
        above, but reachable from production code via a plain `LocalHost.exec()`
        timeout, not just a badly-chosen test command.
        """
        import otto.host.local_host as local_host_mod

        monkeypatch.setattr(local_host_mod, "_EXEC_REAP_TIMEOUT", 0.2)

        real_create_subprocess_shell = asyncio.create_subprocess_shell
        spawned: list[asyncio.subprocess.Process] = []

        async def capturing_create_subprocess_shell(*args, **kwargs):
            proc = await real_create_subprocess_shell(*args, **kwargs)
            spawned.append(proc)
            return proc

        # LocalHost doesn't expose the Process it creates, and proc.terminate()
        # is exactly what this test proves is ineffective here — so cleanup
        # (and the liveness check below) has no route to the real OS process
        # except capturing it ourselves.
        monkeypatch.setattr(asyncio, "create_subprocess_shell", capturing_create_subprocess_shell)

        host = LocalHost(log=LogMode.QUIET)
        try:
            result = await asyncio.wait_for(
                host.exec(
                    'exec python3 -c "import signal, time; '
                    'signal.signal(signal.SIGTERM, signal.SIG_IGN); time.sleep(30)"',
                    timeout=0.1,
                ),
                timeout=5.0,
            )
            assert result.status == Status.Error
            assert result.timed_out is True

            assert spawned, "expected the subprocess-shell spawn to be captured"
            pid = spawned[0].pid
            with pytest.raises(ProcessLookupError):
                os.kill(pid, 0)
        finally:
            for proc in spawned:
                if proc.returncode is None:
                    proc.kill()
                    with contextlib.suppress(Exception):
                        await asyncio.wait_for(proc.wait(), timeout=2.0)
            await host.close()


class TestExecTemplate:
    """exec() validates once in BaseHost and delegates to _exec_one."""

    @pytest.mark.asyncio
    async def test_exec_forwards_the_default(self, host: UnixHost):
        from otto.host.host import DEFAULT_COMMAND_TIMEOUT

        ok = CommandResult(status=Status.Success, value="", command="x", retcode=0)
        with patch.object(host, "_exec_one", new_callable=AsyncMock, return_value=ok) as mock:
            await host.exec("x")
        assert mock.await_args.kwargs["timeout"] == DEFAULT_COMMAND_TIMEOUT

    @pytest.mark.asyncio
    async def test_exec_rejects_bad_timeout_before_dispatch(self, host: UnixHost):
        with (
            patch.object(host, "_exec_one", new_callable=AsyncMock) as mock,
            pytest.raises(ValueError, match="must be >= 0"),
        ):
            await host.exec("x", timeout=-1)
        mock.assert_not_awaited()

    def test_no_subclass_overrides_exec(self):
        """exec is final; family behavior belongs in _exec_one."""
        from otto.host.docker_host import DockerContainerHost
        from otto.host.embedded_host import EmbeddedHost
        from otto.host.host import BaseHost
        from otto.host.local_host import LocalHost

        for cls in (LocalHost, UnixHost, EmbeddedHost, DockerContainerHost):
            assert "exec" not in vars(cls), f"{cls.__name__} must override _exec_one, not exec"
            assert "_exec_one" in vars(cls), f"{cls.__name__} must implement _exec_one"
            assert BaseHost.exec is cls.exec


class TestNoUnboundedBranch:
    """Every command goes through wait_for; inf needs no bypass."""

    def test_run_cmds_with_budget_takes_a_plain_float(self):
        import inspect

        from otto.host.host import _run_cmds_with_budget

        ann = inspect.signature(_run_cmds_with_budget).parameters["timeout"].annotation
        assert ann in (float, "float"), f"expected plain float, got {ann!r}"

    @pytest.mark.asyncio
    async def test_infinite_timeout_still_completes(self):
        """inf flows through the same wait_for path as any other value."""
        host = LocalHost(log=LogMode.QUIET)
        try:
            result = await host.exec("echo hi", timeout=float("inf"))
        finally:
            await host.close()
        assert result.status == Status.Success
        assert "hi" in result.value
        assert result.timed_out is False


class TestExpectSurfaceConsistency:
    """expect advertises one default everywhere, and the impls honour it."""

    def test_protocol_and_impls_agree_on_the_default(self):
        """Every caller-facing expect() surface defaults to the same constant.

        _expect_one hooks are deliberately excluded here — they take no
        default of their own (see test_expect_one_hooks_take_no_default), the
        same shape as _exec_one.
        """
        import inspect

        from otto.host.host import DEFAULT_COMMAND_TIMEOUT, BaseHost, Host
        from otto.host.session import HostSession

        surfaces = [
            Host.expect,
            BaseHost.expect,
            HostSession.expect,
        ]
        for fn in surfaces:
            default = inspect.signature(fn).parameters["timeout"].default
            assert default == DEFAULT_COMMAND_TIMEOUT, f"{fn.__qualname__} disagrees: {default}"

    def test_expect_one_hooks_take_no_default(self):
        """_expect_one must not carry its own default — only expect() does.

        A default on the hook is dead code that can only drift, which is
        exactly how expect() came to contradict its own protocol before this
        fix (protocol/template promised 30.0, every hook silently used 10.0).
        """
        import inspect

        from otto.host.docker_host import DockerContainerHost
        from otto.host.embedded_host import EmbeddedHost
        from otto.host.local_host import LocalHost

        for cls in (LocalHost, UnixHost, EmbeddedHost, DockerContainerHost):
            default = inspect.signature(cls._expect_one).parameters["timeout"].default
            assert default is inspect.Parameter.empty, (
                f"{cls.__name__}._expect_one must not declare its own default"
            )

    def test_no_subclass_overrides_expect(self):
        """expect is final; family behavior belongs in _expect_one.

        The hook check is MRO-aware (not ``vars``): since the Tier-1.1 dedup
        the remote families inherit one ``_expect_one`` from ``RemoteHost``,
        and what matters is that the family hook is overridden somewhere below
        ``BaseHost`` — never that each leaf class carries its own copy.
        """
        from otto.host.docker_host import DockerContainerHost
        from otto.host.embedded_host import EmbeddedHost
        from otto.host.host import BaseHost
        from otto.host.local_host import LocalHost

        for cls in (LocalHost, UnixHost, EmbeddedHost, DockerContainerHost):
            assert "expect" not in vars(cls), f"{cls.__name__} must override _expect_one"
            assert cls._expect_one is not BaseHost._expect_one, (
                f"{cls.__name__} must provide _expect_one below BaseHost"
            )
            assert BaseHost.expect is cls.expect

    @pytest.mark.asyncio
    async def test_expect_rejects_bad_timeout_before_dispatch(self, host: UnixHost):
        with (
            patch.object(host, "_expect_one", new_callable=AsyncMock) as mock,
            pytest.raises(ValueError, match="must be >= 0"),
        ):
            await host.expect("prompt", timeout=-1)
        mock.assert_not_awaited()

    def test_all_proxyio_conformers_agree_with_the_protocol(self):
        """Every login_proxy.ProxyIO conformer's expect() matches the protocol's own default.

        ProxyIO.expect(pattern, timeout=...) is a structural contract — its own
        docstring names four conformers: hosts, HostSession instances, the
        raw-session adapter used at session establishment (_SessionProxyIO),
        and the interact bridge adapter (_BridgeProxyIO). privilege.py's
        _HostProxyIO is a fifth (it forwards straight to a host's expect()).
        Asserting against ProxyIO.expect's own signature default — not a
        hardcoded 30.0 — means this keeps holding even if
        DEFAULT_COMMAND_TIMEOUT itself is ever revalued: the protocol and
        every implementation are checked against each other, not against a
        literal that could drift alongside them. This is the regression this
        whole task exists to prevent: a protocol promising one default while
        a conformer silently delivers another.
        """
        import inspect

        from otto.host.host import BaseHost
        from otto.host.interact import _BridgeProxyIO
        from otto.host.login_proxy import ProxyIO
        from otto.host.privilege import _HostProxyIO
        from otto.host.session import HostSession, _SessionProxyIO

        protocol_default = inspect.signature(ProxyIO.expect).parameters["timeout"].default

        conformers = [
            BaseHost.expect,
            HostSession.expect,
            _SessionProxyIO.expect,
            _BridgeProxyIO.expect,
            _HostProxyIO.expect,
        ]
        for fn in conformers:
            default = inspect.signature(fn).parameters["timeout"].default
            assert default == protocol_default, (
                f"{fn.__qualname__} disagrees with ProxyIO.expect: {default} != {protocol_default}"
            )


class TestExecBoundsTheWholeCommand:
    """LocalHost.exec's timeout must bound the whole command, not each read.

    Before the fix, ``_exec_subprocess`` wrapped only
    ``proc.stdout.readline()`` in ``asyncio.wait_for(..., timeout=timeout)``,
    inside the drain loop. A command that emits output more often than the
    timeout period never triggers ``TimeoutError``: each individual readline
    comfortably finishes within its own per-line budget, and the loop only
    ends when the command itself exits — so a steadily-chattering command
    (e.g. ``ping``) runs forever regardless of ``timeout``.
    """

    @pytest.mark.asyncio
    async def test_steady_output_command_times_out_promptly(self, monkeypatch):
        """A command that never stops emitting output must still time out.

        Uses ``exec python3 -c '...'`` (not a bare shell ``while`` loop) so
        the periodic-printing process IS the dash-replaced process — the loop
        and its ``time.sleep`` run inside the single execed python3, forking
        no external child. A bare ``while true; do ...; sleep 0.02; done``
        forks a real ``sleep`` grandchild every iteration; if one is still
        alive when ``proc.terminate()`` fires, it keeps the stdout pipe's
        write end open past the parent's exit, and the leaked
        ``_UnixSubprocessTransport`` only surfaces as a ``ResourceWarning``
        much later, in an unrelated test (see
        ``test_local_exec_timeout_sets_flag`` above for the same trap).

        Captures the spawned subprocess so it can be force-killed in
        ``finally`` even if the call under test never returns (proving red
        against the pre-fix code) — otherwise a failed assertion here would
        leave a real, infinitely-looping process running on the dev box.
        The whole call is wrapped in an outer ``asyncio.wait_for`` so a
        regression FAILS this test rather than wedging the suite.
        """
        real_create_subprocess_shell = asyncio.create_subprocess_shell
        spawned: list[asyncio.subprocess.Process] = []

        async def capturing_create_subprocess_shell(*args, **kwargs):
            proc = await real_create_subprocess_shell(*args, **kwargs)
            spawned.append(proc)
            return proc

        monkeypatch.setattr(asyncio, "create_subprocess_shell", capturing_create_subprocess_shell)

        host = LocalHost(log=LogMode.QUIET)
        loop = asyncio.get_event_loop()
        start = loop.time()
        try:
            result = await asyncio.wait_for(
                host.exec(
                    'exec python3 -c "'
                    "import time\n"
                    "while True:\n"
                    "    print('tick', flush=True)\n"
                    "    time.sleep(0.02)\n"
                    '"',
                    timeout=0.2,
                ),
                timeout=5.0,
            )
            elapsed = loop.time() - start
            assert result.status == Status.Error
            assert result.timed_out is True
            assert "timed out" in result.value.lower()
            # Prompt: well under the outer 5.0s bound, not just barely inside
            # it. This IS a real-work-completes-in-time bound (the reap costs
            # ~0.3s here), so it is deliberately loose: it only has to separate
            # the inner 0.2s timeout from the outer 5.0s wait_for, and 4.0
            # does that with ~13x headroom where 2.0 left ~6.5x — thinner than
            # the margin that flaked test_surplus_time_donated_to_later_commands
            # on a loaded runner. Discrimination is unchanged: anything at or
            # above 5.0 would be the outer bound firing, not the inner one.
            assert elapsed < 4.0, f"expected a prompt timeout, took {elapsed:.2f}s"
        finally:
            for proc in spawned:
                if proc.returncode is None:
                    proc.kill()
                    with contextlib.suppress(Exception):
                        await asyncio.wait_for(proc.wait(), timeout=2.0)
            await host.close()
