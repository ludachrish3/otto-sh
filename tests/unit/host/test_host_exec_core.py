"""`BaseHost.exec` as the shared core of `run` and `exec` (spec section 1.1, D9-D11):
normalisation, elevation, refusal ordering, ambient identity, and the
one-command differential against `run(cmd).only`."""

import pytest

from otto.host.host import BaseHost, ShellCommand
from otto.logger.mode import LogMode
from otto.result import CommandResult
from otto.utils import Status
from tests.conftest import active_context


class _Recording(BaseHost):
    """The smallest concrete host: records what `_exec_one` and `_run_one` receive."""

    def __init__(self, ambient: str | None = None) -> None:
        # `name` is a plain dataclass field on `BaseHost` (its own generated
        # `__init__` assigns it), not a property — so it is set the ordinary
        # way here rather than overridden as a descriptor, which would clash
        # with that assignment.
        super().__init__()
        self.name = "rec"
        self.exec_calls: list[dict] = []
        self.run_calls: list[dict] = []
        self.refuse_exec_expects_calls = 0
        self._ambient = ambient

    def _refuse_exec_expects(self) -> None:
        self.refuse_exec_expects_calls += 1

    def _ambient_user(self) -> str | None:
        return self._ambient

    async def _exec_one(
        self, cmd, timeout, log=LogMode.NORMAL, user=None, *, expects=None, needs_shell=False
    ):
        self.exec_calls.append(
            {
                "cmd": cmd,
                "timeout": timeout,
                "log": log,
                "user": user,
                "expects": expects,
                "needs_shell": needs_shell,
            }
        )
        return CommandResult(status=Status.Success, value="out", command=cmd, retcode=0)

    async def _run_one(self, cmd, timeout, expects=None, log=LogMode.NORMAL, user=None):
        self.run_calls.append(
            {"cmd": cmd, "timeout": timeout, "log": log, "user": user, "expects": expects}
        )
        return CommandResult(status=Status.Success, value="out", command=cmd, retcode=0)


class TestAmbientIdentity:
    @pytest.mark.asyncio
    async def test_exec_inherits_the_ambient_user(self):
        host = _Recording(ambient="root")
        await host.exec("id")
        assert host.exec_calls[0]["user"] == "root"

    @pytest.mark.asyncio
    async def test_an_explicit_user_wins_over_the_ambient_one(self):
        host = _Recording(ambient="root")
        await host.exec("id", user="bob")
        assert host.exec_calls[0]["user"] == "bob"

    @pytest.mark.asyncio
    async def test_no_ambient_user_means_none(self):
        host = _Recording()
        await host.exec("id")
        assert host.exec_calls[0]["user"] is None

    @pytest.mark.asyncio
    async def test_the_ambient_user_is_refused_where_a_named_one_would_be(self):
        class _Refusing(_Recording):
            def _refuse_exec_user(self, user):
                raise NotImplementedError(f"no users here: {user}")

        host = _Refusing(ambient="root")
        with pytest.raises(NotImplementedError, match="no users here: root"):
            await host.exec("id")


class TestTheSharedCore:
    @pytest.mark.asyncio
    async def test_a_shell_command_carries_its_own_expects_timeout_and_log(self):
        host = _Recording()
        sc = ShellCommand("cat", expects=[("more", " ")], timeout=3.0, log=LogMode.QUIET)
        await host.exec(sc)
        call = host.exec_calls[0]
        assert call["cmd"] == "cat"
        assert call["expects"] == [("more", " ")]
        assert call["timeout"] == 3.0
        assert call["log"] is LogMode.QUIET
        assert call["needs_shell"] is True, "a prompt needs a pty"

    @pytest.mark.asyncio
    async def test_call_level_expects_are_the_default_a_shell_command_inherits(self):
        host = _Recording()
        await host.exec(ShellCommand("cat"), expects=("more", " "))
        assert host.exec_calls[0]["expects"] == [("more", " ")]

    @pytest.mark.asyncio
    async def test_a_plain_command_needs_no_shell(self):
        host = _Recording()
        await host.exec("id")
        call = host.exec_calls[0]
        assert call["expects"] is None
        assert call["needs_shell"] is False

    @pytest.mark.asyncio
    async def test_sudo_rewrites_the_command_and_always_needs_a_shell(self):
        class _Elevating(_Recording):
            prepared = 0

            def __init__(self, ambient=None):
                super().__init__(ambient)
                self.elevated_as: list[str | None] = []

            async def _prepare_elevation(self):
                self.prepared += 1

            def _elevate(self, cmd, user=None):
                self.elevated_as.append(user)
                return f"sudo -S {cmd}", [("assword", "pw\n")]

        host = _Elevating()
        await host.exec("id", sudo=True)
        call = host.exec_calls[0]
        assert host.prepared == 1
        assert call["cmd"] == "sudo -S id"
        assert call["expects"] == [("assword", "pw\n")]
        assert call["needs_shell"] is True
        assert host.elevated_as == [None], "no user: the session's own identity elevates"

        # The EFFECTIVE user reaches the elevation seam, so a family that
        # authenticates as the user it switched to answers with that user's
        # password rather than the login user's.
        switched = _Elevating(ambient="alice")
        await switched.exec("id", sudo=True)
        assert switched.elevated_as == ["alice"]
        await switched.exec("id", sudo=True, user="bob")
        assert switched.elevated_as[-1] == "bob"

    @pytest.mark.asyncio
    async def test_a_family_that_cannot_elevate_refuses_above_the_dry_run_arm(self):
        class _NoSudo(_Recording):
            def _refuse_exec_sudo(self):
                raise NotImplementedError("no sudo on this family")

        host = _NoSudo()
        with pytest.raises(NotImplementedError, match="no sudo on this family"):
            await host.exec("id", sudo=True)
        with active_context(dry_run=True), pytest.raises(NotImplementedError, match="no sudo"):
            await host.exec("id", sudo=True)
        assert host.exec_calls == []

    @pytest.mark.asyncio
    async def test_dry_run_declines_the_rewritten_command_without_calling_exec_one(self):
        class _Elevating(_Recording):
            async def _prepare_elevation(self):
                pass

            def _elevate(self, cmd, user=None):
                return f"sudo -S {cmd}", []

        host = _Elevating()
        with active_context(dry_run=True):
            result = await host.exec("id", sudo=True)
        assert result.status is Status.NotRun
        assert result.command == "sudo -S id", "the decline announces what WOULD have run"
        assert host.exec_calls == []

    @pytest.mark.asyncio
    async def test_the_refuse_exec_expects_seam_is_called_when_a_shell_command_carries_expects(
        self,
    ):
        host = _Recording()
        sc = ShellCommand("cat", expects=[("more", " ")])
        await host.exec(sc)
        assert host.refuse_exec_expects_calls == 1

    @pytest.mark.asyncio
    async def test_the_refuse_exec_expects_seam_is_not_called_when_expects_is_empty(self):
        host = _Recording()
        await host.exec("id")
        assert host.refuse_exec_expects_calls == 0

    @pytest.mark.asyncio
    async def test_a_non_sequence_expects_is_refused_by_name(self):
        """The positional order of `exec` changed on this branch — it is now
        `(cmd, expects, timeout, log, sudo, user)`, so a 0.15.0 caller's
        `exec(cmd, 10.0)` binds the timeout to `expects`. Refuse it here,
        naming the parameter, rather than passing a float down the run and
        failing deep in the pooled session where nothing names the cause.
        """
        host = _Recording()
        with pytest.raises(TypeError, match="expects"):
            await host.exec("id", 10.0)
        with pytest.raises(TypeError, match="expects"):
            await host.exec("id", expects="password")
        with pytest.raises(TypeError, match="expects"):
            await host.exec("id", expects=[("assword",)])
        assert host.exec_calls == []

        # The shapes the parameter really takes still pass: a scalar pair, a
        # list of pairs, a tuple of pairs, and nothing at all.
        await host.exec("id", expects=("assword", "pw\n"))
        await host.exec("id", expects=[("assword", "pw\n")])
        await host.exec("id", expects=(("a", "1"), ("b", "2")))
        await host.exec("id")
        assert [c["expects"] for c in host.exec_calls] == [
            [("assword", "pw\n")],
            [("assword", "pw\n")],
            [("a", "1"), ("b", "2")],
            None,
        ]

    @pytest.mark.asyncio
    async def test_the_timeout_is_validated_before_anything_runs(self):
        host = _Recording()
        with pytest.raises(ValueError, match="timeout must be >= 0"):
            await host.exec("id", timeout=-1)
        assert host.exec_calls == []

    @pytest.mark.asyncio
    async def test_the_timeout_is_validated_before_elevation_is_prepared(self):
        """`run` validates timeout, then prepares elevation (host.py:1583,1596);
        `exec` must match — `_prepare_elevation` can pay up to eleven probes /
        `_RESOLVE_BUDGET_S` (30s) against an unresponsive host, so a bad timeout
        must refuse before any of that cost is paid, not after."""

        class _Elevating(_Recording):
            async def _prepare_elevation(self):
                raise AssertionError("_prepare_elevation must not run before timeout validation")

        host = _Elevating()
        with pytest.raises(ValueError, match="timeout must be >= 0"):
            await host.exec("id", timeout=-1, sudo=True)
        assert host.exec_calls == []


class TestOneCommandDifferential:
    """Spec §1.1 as a test: for one plain command, `exec(cmd)` and `run(cmd).only`
    hand the family the same command, timeout, log and expects.

    THE INPUT TUPLE IS THE ASSERTION. The result comparison below is
    double-fixed — `_Recording`'s two runners return the same canned
    `CommandResult` — so it can only fail if `exec` or `run` mangles a result
    on the way back, never if they disagree about what to run. What the
    parametrisation really exercises is the call each verb composes: the
    `ShellCommand` case is the one that can come apart, because its own
    expects/timeout/log have to override the call-level defaults identically
    on both paths.
    """

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "cmd",
        ["id", ShellCommand("cat", expects=[("more", " ")], timeout=2.0, log=LogMode.QUIET)],
    )
    async def test_exec_and_run_hand_the_family_the_same_call(self, cmd):
        host = _Recording()
        exec_result = await host.exec(cmd)
        run_result = (await host.run(cmd)).only
        e, r = host.exec_calls[0], host.run_calls[0]
        assert (e["cmd"], e["timeout"], e["expects"]) == (r["cmd"], r["timeout"], r["expects"])
        assert e["log"] == r["log"]
        assert (exec_result.status, exec_result.retcode, exec_result.value) == (
            run_result.status,
            run_result.retcode,
            run_result.value,
        )
