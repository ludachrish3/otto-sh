"""Unit tests for DockerContainerHost.

These tests use mocked parents so they run without docker, ssh, or any
network. They verify command-shape correctness, two-step staging, and
the placeholder ``container_id == ""`` guard.
"""

from __future__ import annotations

import shlex
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from otto.host.docker_host import DockerContainerHost
from otto.host.errors import HostCommandError
from otto.host.inventory_ref import InventoryRef
from otto.host.login_proxy import Cred
from otto.result import CommandNotRunError, CommandResult, Result
from otto.utils import Status
from tests.conftest import active_context


def _ok(cmd: str = "", out: str = "") -> CommandResult:
    return CommandResult(status=Status.Success, value=out, command=cmd, retcode=0)


def _fail(cmd: str = "", out: str = "boom") -> CommandResult:
    return CommandResult(status=Status.Failed, value=out, command=cmd, retcode=1)


def _sm(result) -> tuple[Status, str]:
    """Unwrap ``(status, msg)`` from a transfer aggregate :class:`~otto.result.Result`."""
    return result.status, result.msg


def _mock_parent(parent_id: str = "test3", *, term: str = "ssh"):
    parent = MagicMock()
    parent.id = parent_id
    parent.name = parent_id
    parent.term = term
    parent.exec = AsyncMock(return_value=_ok())
    parent.put = AsyncMock(return_value=Result(Status.Success, value={}))
    parent.get = AsyncMock(return_value=Result(Status.Success, value={}))
    return parent


def _make_container(
    parent=None, container_id: str = "abc123def456", *, user: str | None = None
) -> DockerContainerHost:
    return DockerContainerHost(
        parent=parent or _mock_parent(),
        container_id=container_id,
        project="repo1",
        service="api",
        compose_project="otto-repo1-vagrant",
        user=user,
    )


def _build_fake_ssh_remote_host():
    """Construct a real UnixHost with an injected fake ConnectionManager.

    Real UnixHost is needed so `isinstance(parent, UnixHost)` passes in
    `_make_session`; the fake ConnectionManager keeps the test offline.
    """
    from otto.host.connections import ConnectionManager
    from otto.host.unix_host import UnixHost

    class FakeConnections(ConnectionManager):
        def __init__(self, *args, **kwargs):  # type: ignore[no-untyped-def]
            self._ssh_conn = MagicMock()  # not awaited in unit tests
            self._sftp_conn = None
            self._ftp_conn = None
            self._telnet_conn = None
            self._name = kwargs.get("name", "fake")
            self._term = kwargs.get("term", "ssh")
            self._hop = None

        async def ssh(self):
            return self._ssh_conn

    return UnixHost(
        ip="10.0.0.1",
        creds=[Cred(login="root", password="x")],
        element="fake_ne",
        term="ssh",
        _connection_factory=FakeConnections,
    )


# ---------------------------------------------------------------------------
# Construction & identity
# ---------------------------------------------------------------------------


def test_id_format():
    h = _make_container(_mock_parent("test3"))
    assert h.id == "test3.repo1.api"


def test_id_lowercased():
    h = DockerContainerHost(
        parent=_mock_parent("TEST3"),
        container_id="abc",
        project="Repo1",
        service="API",
        compose_project="proj",
    )
    assert h.id == "test3.repo1.api"


def test_is_virtual_default():
    assert _make_container().is_virtual is True


def test_container_carries_an_empty_inventory_ref():
    """A container is never an inventory host — not resolved from a record."""
    assert _make_container().inventory_ref == InventoryRef()


# ---------------------------------------------------------------------------
# exec — single command
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_exec_wraps_in_docker_exec():
    parent = _mock_parent()
    h = _make_container(parent)
    parent.exec.return_value = _ok(out="hello")

    result = await h.exec("echo hello")

    assert result.status == Status.Success
    assert result.command == "echo hello"  # caller-visible command, not the wrapper
    parent.exec.assert_awaited_once()
    sent = parent.exec.call_args.args[0]
    assert sent.startswith(f"docker exec -i {h.container_id} sh -c ")
    assert "'echo hello'" in sent or "echo hello" in sent  # quoted


@pytest.mark.asyncio
async def test_exec_quotes_dangerous_chars():
    """Single quotes / spaces / semicolons must be safely escaped via shlex."""
    parent = _mock_parent()
    h = _make_container(parent)
    await h.exec("echo 'hi' ; rm -rf /")
    sent = parent.exec.call_args.args[0]
    # The whole thing must be wrapped so the parent's shell doesn't see
    # ; as a command separator.
    assert "rm -rf /" in sent
    # And the inner ; is not directly exposed at the parent level.
    parent_cmd_after_sh_c = sent.split("sh -c ", 1)[1]
    assert parent_cmd_after_sh_c.startswith("'")  # shlex.quote uses single quotes


@pytest.mark.asyncio
async def test_exec_preserves_every_field_through_the_command_rebuild():
    """`_exec_via_parent` rebuilds the result only to swap `command` back to
    the unwrapped one the caller asked for -- every other field, including
    ones added to `CommandResult` after this rebuild was written (like
    `timed_out`) and pre-existing ones it never enumerated (`msg`), must
    survive unchanged. A field-list reconstruction silently drops both;
    `dataclasses.replace` cannot.
    """
    parent = _mock_parent()
    h = _make_container(parent)
    wrapped_cmd = f"docker exec -i {h.container_id} sh -c 'sleep 10'"
    parent.exec.return_value = CommandResult(
        status=Status.Error,
        value="Command timed out after 0.1s",
        msg="diagnostic detail",
        command=wrapped_cmd,
        retcode=-1,
        timed_out=True,
    )

    result = await h.exec("sleep 10")

    assert result.timed_out is True
    assert result.msg == "diagnostic detail"
    assert result.command == "sleep 10"  # unwrapped, not the docker-exec wrapper
    assert result.command != wrapped_cmd


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("declared", "per_call", "expected_u"),
    [
        (None, None, None),  # neither → no -u
        ("postgres", None, "postgres"),  # declared default
        (None, "root", "root"),  # per-call
        ("postgres", "root", "root"),  # per-call beats declared
    ],
)
async def test_docker_exec_wrapper_effective_user(declared, per_call, expected_u):
    host = _make_container(user=declared)
    wrapped = await host._docker_exec("id", user=host._effective_user(per_call))
    if expected_u is None:
        assert " -u " not in wrapped
    else:
        assert f" -u {expected_u} " in wrapped


@pytest.mark.asyncio
async def test_exec_user_bad_form_refused():
    host = _make_container()
    with pytest.raises(ValueError, match="non-empty string with no whitespace"):
        await host.exec("id", user="a b")


@pytest.mark.asyncio
async def test_exec_threads_per_call_user_to_docker_dash_u():
    """Enter through the real path — ``host.exec()`` — rather than composing
    ``_docker_exec``/``_effective_user`` directly, so a broken hop anywhere
    in ``_exec_one`` → ``_exec_via_parent`` → ``_docker_exec`` shows up here
    even though ``test_docker_exec_wrapper_effective_user`` above (which
    builds its own wrapped string) cannot see it.
    """
    parent = _mock_parent()
    h = _make_container(parent)
    parent.exec.return_value = _ok(out="0")

    await h.exec("id", user="root")

    sent = parent.exec.call_args.args[0]
    assert " -u root " in sent
    assert sent.index(" -u root ") < sent.index(h.container_id)  # -u precedes the container id


@pytest.mark.asyncio
async def test_exec_uses_declared_default_user_when_none_given():
    """Same real path, no per-call user: the container's declared default
    (set at construction — never passed to ``exec()``) must still reach
    docker via ``_effective_user``.
    """
    parent = _mock_parent()
    h = _make_container(parent, user="postgres")
    parent.exec.return_value = _ok(out="0")

    await h.exec("id")

    sent = parent.exec.call_args.args[0]
    assert " -u postgres " in sent
    assert sent.index(" -u postgres ") < sent.index(h.container_id)


# ---------------------------------------------------------------------------
# run — persistent-shell dispatch
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_run_rejects_non_ssh_remote_parent():
    """run() requires an SSH-based UnixHost parent (telnet → NotImplementedError)."""
    parent = _mock_parent(term="telnet")
    h = _make_container(parent)
    with pytest.raises(NotImplementedError, match="SSH-based UnixHost parent"):
        await h.run("pwd")


@pytest.mark.asyncio
async def test_run_rejects_localhost_parent():
    """run() requires a UnixHost parent — LocalHost is rejected."""
    from otto.host.local_host import LocalHost

    h = DockerContainerHost(
        parent=LocalHost(),
        container_id="abc123",
        project="repo1",
        service="api",
        compose_project="otto-repo1-vagrant",
    )
    with pytest.raises(NotImplementedError, match="SSH-based UnixHost parent"):
        await h.run("pwd")


@pytest.mark.asyncio
async def test_run_with_ssh_parent_uses_docker_session():
    """run() against an SSH-based UnixHost parent opens a _DockerSshSession."""
    from otto.host.session import _DockerSshSession

    parent = _build_fake_ssh_remote_host()
    h = _make_container(parent)

    # Patch the factory to return a controllable mock; verify the right session
    # type would be requested without actually opening a docker exec channel.
    real_factory = h._session_mgr._session_factory
    sentinel_session = real_factory()
    assert isinstance(sentinel_session, _DockerSshSession)


@pytest.mark.asyncio
async def test_session_factory_resolves_container_id_lazily():
    """The cid_getter closure reads the host's current container_id at session-open
    time — not the value at construction time. This means a placeholder host
    constructed with `container_id=""` works correctly once `_ensure_running`
    populates the id (e.g., via parent.exec lookup)."""
    parent = _build_fake_ssh_remote_host()
    h = DockerContainerHost(
        parent=parent,
        container_id="",  # placeholder
        project="repo1",
        service="api",
        compose_project="otto-repo1-vagrant",
    )
    # Simulate _ensure_running populating the id post-hoc.
    h.container_id = "resolved_cid_xyz"
    session = h._session_mgr._session_factory()
    assert session._cid_getter() == "resolved_cid_xyz"


# ---------------------------------------------------------------------------
# run(user=...) — the persistent channel binds its user at its OPEN
# ---------------------------------------------------------------------------


def _channel_container(user: str | None = None) -> DockerContainerHost:
    """Container on a real (offline) SSH-based UnixHost parent, so the real
    ``SessionManager`` and the real ``_DockerSshSession`` factory are in play."""
    return _make_container(_build_fake_ssh_remote_host(), user=user)


@contextmanager
def _stubbed_docker_channel():
    """Run the REAL channel machinery with only the transport stubbed.

    The bind seam is made of the session factory (which wires ``user_getter``
    and ``on_open``), ``_DockerSshSession._open`` (which builds the command
    and fires the callback), and ``alive``. All of those run for real here;
    what is replaced is the ssh channel underneath and the marker handshake.
    Tests therefore enter at ``host.run``/``host.send``/``host.open_session``
    and observe what the channel ACTUALLY opened with — a helper that composed
    the open itself could not tell a live binding from a dead one.

    Yields the list of sessions opened, oldest first.
    """
    from otto.host.session import ShellSession, SshSession

    opened: list = []

    async def _fake_transport_open(self):
        return None

    async def _fake_init(self):
        if self._initialized:
            return
        await self._open()  # the real _DockerSshSession._open
        self._initialized = True
        self._alive = True
        opened.append(self)

    async def _fake_run_cmd(self, cmd, **kwargs):
        return _ok(cmd)

    async def _fake_send(self, text):
        return None

    async def _fake_expect(self, pattern, timeout=None):
        return pattern if isinstance(pattern, str) else pattern.pattern

    async def _fake_close(self):
        self._alive = False
        self._initialized = False

    with (
        patch.object(SshSession, "_open", new=_fake_transport_open),
        patch.object(SshSession, "close", new=_fake_close),
        patch.object(ShellSession, "_ensure_initialized", new=_fake_init),
        patch.object(ShellSession, "run_cmd", new=_fake_run_cmd),
        patch.object(ShellSession, "send", new=_fake_send),
        patch.object(ShellSession, "expect", new=_fake_expect),
    ):
        yield opened


@pytest.mark.asyncio
async def test_run_channel_binds_user_at_open():
    host = _channel_container(user="postgres")
    with _stubbed_docker_channel() as opened:
        # the first open binds the declared default...
        await host.run("id")
        assert " -u postgres " in opened[-1]._open_cmd
        assert host._bound_run_user == "postgres"
        assert host._run_channel_is_bound is True
        # ...and a differing per-call user now refuses loudly
        with pytest.raises(RuntimeError, match="persistent run channel is bound to user"):
            await host.run("id", user="root")


@pytest.mark.asyncio
async def test_run_open_command_carries_the_per_call_user():
    """End to end: ``run(user=...)`` reaches the channel's ``docker exec -u``."""
    host = _channel_container()
    with _stubbed_docker_channel() as opened:
        await host.run("id", user="root")
    assert " -u root " in opened[-1]._open_cmd
    assert opened[-1]._open_cmd.index(" -u root ") < opened[-1]._open_cmd.index(host.container_id)
    assert host._bound_run_user == "root"


@pytest.mark.asyncio
async def test_per_call_user_wins_the_bind_over_the_declared_default():
    """Precedence AT THE BIND, not just inside ``_effective_user``."""
    host = _channel_container(user="postgres")
    with _stubbed_docker_channel() as opened:
        await host.run("id", user="root")
    assert " -u root " in opened[-1]._open_cmd
    assert " -u postgres " not in opened[-1]._open_cmd
    assert host._bound_run_user == "root"


@pytest.mark.asyncio
async def test_send_opens_the_channel_and_a_differing_run_then_refuses():
    """``send()`` opens the SAME channel ``run()`` uses, and binds it.

    The bind record has to describe the shell that is actually running, not
    the last thing ``run()`` intended — otherwise this call would "bind" root
    on a channel already running as postgres and answer as postgres.
    """
    host = _channel_container(user="postgres")
    with _stubbed_docker_channel() as opened:
        await host.send("echo hi\n")
        assert len(opened) == 1
        assert " -u postgres " in opened[-1]._open_cmd
        assert host._bound_run_user == "postgres"
        with pytest.raises(
            RuntimeError,
            match=r"bound to user 'postgres' and this call asked for 'root'",
        ):
            await host.run("id", user="root")
        # the refused call left no intent behind: a channel opened after it
        # still opens as the declared user, not as the user that was refused
        assert host._pending_run_user is None


@pytest.mark.asyncio
async def test_send_then_a_plain_run_shares_the_bound_channel():
    host = _channel_container(user="postgres")
    with _stubbed_docker_channel() as opened:
        await host.send("echo hi\n")
        result = await host.run("id")
    assert result.only.status == Status.Success
    assert len(opened) == 1  # reused, not reopened
    assert host._bound_run_user == "postgres"


@pytest.mark.asyncio
async def test_expect_also_binds_the_channel_it_opens():
    """``expect()`` is the third opener of the same channel."""
    host = _channel_container(user="postgres")
    with _stubbed_docker_channel() as opened:
        assert await host.expect("hi", timeout=1.0) == "hi"
        assert len(opened) == 1
        assert " -u postgres " in opened[-1]._open_cmd
        assert host._bound_run_user == "postgres"
        with pytest.raises(RuntimeError, match="persistent run channel is bound to user"):
            await host.run("id", user="root")


@pytest.mark.asyncio
async def test_a_failed_run_before_the_open_leaves_no_bind():
    """``_ensure_running`` raising is a call that never reached a channel.

    Recording the intent here (the first cut did) left a bind that refused the
    next legitimate call even though no channel had ever opened.
    """
    host = _channel_container()
    with _stubbed_docker_channel() as opened:
        down = AsyncMock(side_effect=RuntimeError("container is down"))
        with (
            patch.object(host, "_ensure_running", new=down),
            pytest.raises(RuntimeError, match="container is down"),
        ):
            await host.run("id", user="root")
        assert opened == []
        assert host._run_user_bound is False
        assert host._run_channel_is_bound is False

        await host.run("id", user="postgres")  # no stale refusal
    assert host._bound_run_user == "postgres"
    assert " -u postgres " in opened[-1]._open_cmd


@pytest.mark.asyncio
async def test_an_open_that_raises_leaves_no_bind():
    """Same rule one layer down: the transport, not the intent, is the writer."""
    from otto.host.session import SshSession

    host = _channel_container()
    with _stubbed_docker_channel() as opened:
        with (
            patch.object(
                SshSession, "_open", new=AsyncMock(side_effect=RuntimeError("no channel"))
            ),
            pytest.raises(RuntimeError, match="no channel"),
        ):
            await host.run("id", user="root")
        assert host._run_user_bound is False

        await host.run("id", user="postgres")
    assert host._bound_run_user == "postgres"
    assert " -u postgres " in opened[-1]._open_cmd


@pytest.mark.asyncio
async def test_a_failed_run_leaves_no_pending_user_for_the_next_opener():
    """A ``run(user=...)`` that dies before its channel opens must leave the
    host exactly as it found it.

    The residue is not a cosmetic leak. ``send()`` takes no ``user``, so it
    would open the channel as the user the failed call asked for and never
    obtained — and every plain ``run()`` afterwards would then refuse against
    a binding the caller never asked for and cannot clear without a close.
    """
    host = _channel_container(user="postgres")
    with _stubbed_docker_channel() as opened:
        down = AsyncMock(side_effect=RuntimeError("container is down"))
        with (
            patch.object(host, "_ensure_running", new=down),
            pytest.raises(RuntimeError, match="container is down"),
        ):
            await host.run("id", user="root")
        assert host._pending_run_user is None

        await host.send("echo hi\n")  # no user parameter to correct a stale one
        assert " -u postgres " in opened[-1]._open_cmd
        assert " -u root " not in opened[-1]._open_cmd
        assert host._bound_run_user == "postgres"

        result = await host.run("id")  # and the host is not bricked
    assert result.only.status == Status.Success


@pytest.mark.asyncio
async def test_a_transport_failure_inside_run_cmd_leaves_no_pending_user():
    """Same rule when the attempt dies INSIDE ``run_cmd`` rather than above it."""
    from otto.host.session import SshSession

    host = _channel_container(user="postgres")
    with _stubbed_docker_channel() as opened:
        with (
            patch.object(
                SshSession, "_open", new=AsyncMock(side_effect=RuntimeError("no channel"))
            ),
            pytest.raises(RuntimeError, match="no channel"),
        ):
            await host.run("id", user="root")
        assert host._pending_run_user is None

        await host.send("echo hi\n")
    assert " -u postgres " in opened[-1]._open_cmd
    assert host._bound_run_user == "postgres"


@pytest.mark.asyncio
async def test_a_successful_run_also_drops_its_pending_user():
    """The intent is scoped to the attempt, not held until the next one."""
    host = _channel_container(user="postgres")
    with _stubbed_docker_channel():
        await host.run("id", user="root")
        assert host._pending_run_user is None
        # ...and the live channel's own record is what later openers read
        assert host._bound_run_user == "root"


@pytest.mark.asyncio
async def test_a_handshake_that_fails_after_the_open_leaves_no_live_bind():
    """The record is written by the open, but consulted only while the channel
    is LIVE. A transport that came up and then failed its readiness handshake
    is discarded by ``SessionManager`` — it must not keep refusing calls."""
    from otto.host.session import ShellSession

    host = _channel_container()
    with _stubbed_docker_channel() as opened:

        async def _open_then_fail(self):
            await self._open()  # fires the on-open callback for real
            raise RuntimeError("handshake failed")

        with (
            patch.object(ShellSession, "_ensure_initialized", new=_open_then_fail),
            pytest.raises(RuntimeError, match="handshake failed"),
        ):
            await host.run("id", user="root")

        assert host._run_user_bound is True  # the open really did happen...
        assert host._run_channel_is_bound is False  # ...but nothing is live
        await host.run("id", user="postgres")  # so no stale refusal
    assert host._bound_run_user == "postgres"
    assert " -u postgres " in opened[-1]._open_cmd


@pytest.mark.asyncio
async def test_run_channel_rebinds_after_rebuild():
    host = _channel_container()
    with _stubbed_docker_channel() as opened:
        await host.run("id", user="root")
        assert host._bound_run_user == "root"
        host.rebuild_connections()
        assert host._run_user_bound is False
        assert host._pending_run_user is None
        await host.run("id", user="postgres")  # rebinding after rebuild is allowed
    assert host._bound_run_user == "postgres"
    assert " -u postgres " in opened[-1]._open_cmd


@pytest.mark.asyncio
async def test_run_channel_rebinds_after_close():
    host = _channel_container(user="postgres")
    with _stubbed_docker_channel() as opened:
        await host.run("id")
        await host.close()
        assert host._run_user_bound is False
        assert host._bound_run_user is None
        await host.run("id", user="root")
    assert host._bound_run_user == "root"
    assert " -u root " in opened[-1]._open_cmd


@pytest.mark.asyncio
async def test_a_raising_close_still_forgets_the_binding():
    """A teardown that fails half-way still leaves no channel this host may
    claim to know the user of — otherwise a raising ``close()`` also poisons
    every later ``run(user=...)`` with a refusal it can never clear."""
    host = _channel_container(user="postgres")
    with _stubbed_docker_channel():
        await host.run("id")
        assert host._run_user_bound is True
        boom = AsyncMock(side_effect=RuntimeError("drain failed"))
        with (
            patch.object(host._session_mgr, "close_all", new=boom),
            pytest.raises(RuntimeError, match="drain failed"),
        ):
            await host.close()
        assert host._run_user_bound is False
        assert host._bound_run_user is None
        assert host._pending_run_user is None
        # and the next call is not refused
        result = await host.run("id", user="root")
    assert result.only.status == Status.Success


@pytest.mark.asyncio
async def test_run_same_bound_user_is_fine():
    host = _channel_container(user="postgres")
    with _stubbed_docker_channel() as opened:
        await host.run("id")
        await host.run("id", user="postgres")  # equal to the bound user — no refusal
    assert len(opened) == 1


@pytest.mark.asyncio
async def test_run_binds_none_and_refuses_a_later_named_user():
    """``None`` is itself a binding (the image's USER) — not "unbound"."""
    host = _channel_container()
    with _stubbed_docker_channel() as opened:
        await host.run("id")
        assert " -u " not in opened[-1]._open_cmd
        assert host._run_user_bound is True
        assert host._bound_run_user is None
        with pytest.raises(RuntimeError, match="persistent run channel is bound to user"):
            await host.run("id", user="root")


@pytest.mark.asyncio
async def test_run_sequence_form_threads_user_and_binds_once():
    """The multi-command arm of ``run`` reaches ``_run_one`` through
    ``_run_sc``/``_run_cmds_with_budget`` — a SECOND call site that has to
    carry ``user`` too. Without it the sequence form would silently run on a
    channel opened as the image's user while the single-command form honoured
    the request.
    """
    host = _channel_container()
    with _stubbed_docker_channel() as opened:
        await host.run(["id", "whoami"], user="root")
    assert len(opened) == 1
    assert " -u root " in opened[-1]._open_cmd
    assert host._bound_run_user == "root"


@pytest.mark.asyncio
async def test_run_user_bad_form_refused_before_anything_opens():
    host = _channel_container()
    with (
        _stubbed_docker_channel() as opened,
        pytest.raises(ValueError, match="non-empty string with no whitespace"),
    ):
        await host.run("id", user="a b")
    assert opened == []
    assert host._run_user_bound is False


@pytest.mark.asyncio
async def test_run_dry_run_never_opens_or_binds_the_channel():
    host = _channel_container()
    with _stubbed_docker_channel() as opened, active_context(dry_run=True):
        await host.run("id", user="root")
    assert opened == []
    assert host._run_user_bound is False
    assert host._bound_run_user is None
    assert host._pending_run_user is None


# --- named auxiliary sessions: same user, no bind record -------------------


@pytest.mark.asyncio
async def test_named_session_before_any_run_opens_as_the_declared_user():
    host = _channel_container(user="postgres")
    with _stubbed_docker_channel() as opened:
        await host.open_session("mon")
        assert " -u postgres " in opened[-1]._open_cmd
        # ...and records nothing, so a later run is free to bind whatever it likes
        assert host._run_user_bound is False
        await host.run("id", user="root")
    assert host._bound_run_user == "root"


@pytest.mark.asyncio
async def test_named_session_before_any_run_has_no_user_flag_when_none_declared():
    host = _channel_container()
    with _stubbed_docker_channel() as opened:
        await host.open_session("mon")
    assert " -u " not in opened[-1]._open_cmd
    assert host._run_user_bound is False


@pytest.mark.asyncio
async def test_named_session_after_a_bound_run_opens_as_the_bound_user():
    host = _channel_container(user="postgres")
    with _stubbed_docker_channel() as opened:
        await host.run("id", user="root")
        await host.open_session("mon")
        assert " -u root " in opened[-1]._open_cmd
        # the run channel's own record is untouched by the named open
        assert host._bound_run_user == "root"
        assert host._run_channel_is_bound is True


# ---------------------------------------------------------------------------
# Placeholder (container_id == "") — auto-up behavior
# ---------------------------------------------------------------------------


def _mock_repos(repo_name: str | None = "repo1"):
    """Return a fake repos list optionally including *repo_name*."""
    if repo_name:
        repo = MagicMock()
        repo.name = repo_name
        return [repo]
    return []


@pytest.mark.asyncio
async def test_placeholder_auto_ups_stack(monkeypatch):
    """Accessing a declared-but-down container auto-starts its stack."""
    parent = _mock_parent()  # docker ps returns empty out -> not running
    h = _make_container(parent, container_id="")

    started = _make_container(parent, container_id="freshcid")
    compose_up = AsyncMock(return_value={"api": started})
    monkeypatch.setattr("otto.docker.compose.compose_up", compose_up)
    monkeypatch.setattr("otto.config.get_repos", _mock_repos)
    monkeypatch.setattr("otto.config.get_lab", MagicMock())

    result = await h.exec("echo hi")

    compose_up.assert_awaited_once()
    assert compose_up.call_args.kwargs["build"] is False
    assert compose_up.call_args.kwargs["project_name"] == "otto-repo1-vagrant"
    # Auto-up composes on the container's OWN parent host, not a
    # lab-wide placement: a `test1.repo1.api` container must auto-start on
    # test1, not on whatever host use-case/pin resolution happens to pick.
    # (Latent bug surfaced by the multi-host docker pool — see
    # docker_host.py::_auto_up.)
    assert compose_up.call_args.kwargs["on"] == parent.id
    assert h.container_id == "freshcid"
    assert result.status == Status.Success


@pytest.mark.asyncio
async def test_placeholder_no_repo_raises(monkeypatch):
    """No configured repo to auto-start -> clear 'not running' error."""
    h = _make_container(container_id="")
    monkeypatch.setattr("otto.config.get_repos", lambda: _mock_repos(repo_name=None))
    monkeypatch.setattr("otto.config.get_lab", MagicMock())
    with pytest.raises(RuntimeError, match="not running"):
        await h.exec("echo hi")


@pytest.mark.asyncio
async def test_placeholder_auto_up_failure_raises(monkeypatch):
    """A compose_up failure surfaces as a 'not running' RuntimeError."""
    h = _make_container(container_id="")
    compose_up = AsyncMock(side_effect=RuntimeError("compose boom"))
    monkeypatch.setattr("otto.docker.compose.compose_up", compose_up)
    monkeypatch.setattr("otto.config.get_repos", _mock_repos)
    monkeypatch.setattr("otto.config.get_lab", MagicMock())
    with pytest.raises(RuntimeError, match="not running"):
        await h.exec("echo hi")


@pytest.mark.asyncio
async def test_placeholder_use_case_project_auto_starts_via_deploy(monkeypatch):
    """A placeholder whose ``project`` names a declared use-case (spec §9)
    auto-starts through ``deployment.deploy``, never the legacy per-repo
    ``compose_up`` — the id shape (``<parent>.<usecase>.<service>``) synthesized
    by ``register_declared_container_hosts``'s use-case branch is exactly what
    routes it here (see test_register_declared_use_case_repo_synthesizes_
    usecase_ids in test_compose.py)."""
    parent = _mock_parent()
    h = DockerContainerHost(
        parent=parent,
        container_id="",
        project="integration",
        service="api",
        compose_project="unix-integration-vagrant",
    )
    uc_repo = SimpleNamespace(
        name="a",
        docker_settings=SimpleNamespace(use_cases=[SimpleNamespace(name="integration")]),
    )
    started = _make_container(parent, container_id="freshcid")
    stack = SimpleNamespace(
        hosts={"api": started},
        by_host={parent.id: {"api": started}},
    )
    deploy = AsyncMock(return_value=stack)
    compose_up = AsyncMock()
    monkeypatch.setattr("otto.docker.deployment.deploy", deploy)
    monkeypatch.setattr("otto.docker.compose.compose_up", compose_up)
    monkeypatch.setattr("otto.config.get_repos", lambda: [uc_repo])
    monkeypatch.setattr("otto.config.get_lab", MagicMock())

    result = await h.exec("echo hi")

    deploy.assert_awaited_once_with("integration", build=False)
    compose_up.assert_not_awaited()
    assert h.container_id == "freshcid"
    assert result.status == Status.Success


def _uc_repos(use_case: str = "integration"):
    return [
        SimpleNamespace(
            name="a",
            docker_settings=SimpleNamespace(use_cases=[SimpleNamespace(name=use_case)]),
        )
    ]


@pytest.mark.asyncio
async def test_use_case_auto_up_picks_the_container_of_its_own_parent(monkeypatch):
    """A use-case can span parents that legally declare the same service
    name (_declared_services warns, it does not refuse). `_auto_up` must
    read `stack.by_host[self.parent.id]`, never the flattened `stack.hosts`
    — the flattened map is keyed last-write-wins across every parent, so a
    naive read can hand THIS container (bound to `self.parent` for every
    subsequent docker exec) a container id belonging to a DIFFERENT host."""
    parent_other = _mock_parent("test1")
    parent_mine = _mock_parent("test3")
    h = DockerContainerHost(
        parent=parent_mine,
        container_id="",
        project="integration",
        service="api",
        compose_project="unix-integration-vagrant",
    )
    other_container = _make_container(parent_other, container_id="wrong-parent-cid")
    mine_container = _make_container(parent_mine, container_id="right-parent-cid")
    # The flattened map deliberately carries the OTHER parent's container —
    # exactly what a naive `stack.hosts[self.service]` read would return.
    stack = SimpleNamespace(
        hosts={"api": other_container},
        by_host={
            "test1": {"api": other_container},
            "test3": {"api": mine_container},
        },
    )
    deploy = AsyncMock(return_value=stack)
    monkeypatch.setattr("otto.docker.deployment.deploy", deploy)
    monkeypatch.setattr("otto.config.get_repos", _uc_repos)
    monkeypatch.setattr("otto.config.get_lab", MagicMock())

    await h.exec("echo hi")

    assert h.container_id == "right-parent-cid"


@pytest.mark.asyncio
async def test_use_case_auto_up_hands_a_decline_back_unwrapped(monkeypatch):
    """The dry-run decline pass-through on the use-case route (docker_host.py's
    CommandNotRunError arm) — untested-under-coverage counterpart of the
    legacy route's test_auto_up_hands_a_decline_back_unwrapped in
    test_dry_run.py."""
    parent = _mock_parent()
    h = DockerContainerHost(
        parent=parent,
        container_id="",
        project="integration",
        service="api",
        compose_project="unix-integration-vagrant",
    )
    declined = CommandNotRunError("deploy(integration)", parent.id, "No stack was brought up.")

    async def declining_deploy(*_a, **_kw):
        raise declined

    monkeypatch.setattr("otto.docker.deployment.deploy", declining_deploy)
    monkeypatch.setattr("otto.config.get_repos", _uc_repos)
    monkeypatch.setattr("otto.config.get_lab", MagicMock())

    with pytest.raises(CommandNotRunError) as exc:
        await h._auto_up()

    assert exc.value is declined
    assert "auto-start failed" not in str(exc.value)


@pytest.mark.asyncio
async def test_use_case_auto_up_wraps_a_real_deploy_failure(monkeypatch):
    """The use-case route's wide `except Exception` — a real failure must
    surface as the actionable 'auto-start failed' RuntimeError, exact
    phrase, never a bare pass-through of the underlying error."""
    parent = _mock_parent()
    h = DockerContainerHost(
        parent=parent,
        container_id="",
        project="integration",
        service="api",
        compose_project="unix-integration-vagrant",
    )

    async def failing_deploy(*_a, **_kw):
        raise HostCommandError("docker compose up failed: no such image")

    monkeypatch.setattr("otto.docker.deployment.deploy", failing_deploy)
    monkeypatch.setattr("otto.config.get_repos", _uc_repos)
    monkeypatch.setattr("otto.config.get_lab", MagicMock())

    with pytest.raises(RuntimeError, match="not running, and auto-start failed"):
        await h._auto_up()


@pytest.mark.asyncio
async def test_use_case_auto_up_no_container_for_service_is_refused(monkeypatch):
    """The use-case route's 'did not produce a container for service'
    refusal — deploy succeeds but this parent's service never resolved."""
    parent = _mock_parent("test3")
    h = DockerContainerHost(
        parent=parent,
        container_id="",
        project="integration",
        service="api",
        compose_project="unix-integration-vagrant",
    )
    stack = SimpleNamespace(hosts={}, by_host={})
    deploy = AsyncMock(return_value=stack)
    monkeypatch.setattr("otto.docker.deployment.deploy", deploy)
    monkeypatch.setattr("otto.config.get_repos", _uc_repos)
    monkeypatch.setattr("otto.config.get_lab", MagicMock())

    with pytest.raises(
        RuntimeError, match="did not produce a container for service 'api' on test3"
    ):
        await h._auto_up()


@pytest.mark.asyncio
async def test_concurrent_access_triggers_single_auto_up(monkeypatch):
    """Two concurrent calls against a down container auto-up exactly once."""
    import asyncio

    parent = _mock_parent()
    h = _make_container(parent, container_id="")

    started = _make_container(parent, container_id="freshcid")
    compose_up = AsyncMock(return_value={"api": started})
    monkeypatch.setattr("otto.docker.compose.compose_up", compose_up)
    monkeypatch.setattr("otto.config.get_repos", _mock_repos)
    monkeypatch.setattr("otto.config.get_lab", MagicMock())

    await asyncio.gather(h.exec("echo a"), h.exec("echo b"))

    compose_up.assert_awaited_once()
    assert h.container_id == "freshcid"


# ---------------------------------------------------------------------------
# is_running — side-effect-free liveness probe (issue #139)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_is_running_true_without_probe_when_id_resolved():
    """A container whose id is already known needs no docker round-trip."""
    parent = _mock_parent()
    h = _make_container(parent)  # container_id set
    assert await h.is_running() is True
    parent.exec.assert_not_awaited()


@pytest.mark.asyncio
async def test_is_running_probes_quietly_and_caches_id(monkeypatch):
    """A placeholder probe is one read-only, QUIET `docker ps` — never a compose."""
    from otto.logger.mode import LogMode

    parent = _mock_parent()
    parent.exec = AsyncMock(return_value=_ok(out="abc123\n"))
    h = _make_container(parent, container_id="")
    compose_up = AsyncMock()
    monkeypatch.setattr("otto.docker.compose.compose_up", compose_up)

    assert await h.is_running() is True

    assert h.container_id == "abc123"
    compose_up.assert_not_awaited()
    cmd = parent.exec.call_args.args[0]
    assert cmd.startswith("docker ps -q")
    assert parent.exec.call_args.kwargs.get("log") is LogMode.QUIET


@pytest.mark.asyncio
async def test_is_running_false_when_down_never_composes(monkeypatch):
    """A down placeholder reports False; is_running must NEVER auto-start."""
    parent = _mock_parent()  # docker ps returns empty out -> not running
    h = _make_container(parent, container_id="")
    compose_up = AsyncMock()
    monkeypatch.setattr("otto.docker.compose.compose_up", compose_up)

    assert await h.is_running() is False

    assert h.container_id == ""
    compose_up.assert_not_awaited()


@pytest.mark.asyncio
async def test_put_placeholder_auto_ups(tmp_path, monkeypatch):
    """File transfer against a down container also auto-starts the stack."""
    parent = _mock_parent()
    h = _make_container(parent, container_id="")
    f = tmp_path / "x"
    f.write_text("x")

    started = _make_container(parent, container_id="freshcid")
    compose_up = AsyncMock(return_value={"api": started})
    monkeypatch.setattr("otto.docker.compose.compose_up", compose_up)
    monkeypatch.setattr("otto.config.get_repos", _mock_repos)
    monkeypatch.setattr("otto.config.get_lab", MagicMock())

    status, _ = _sm(await h.put([f], Path("/tmp")))

    compose_up.assert_awaited_once()
    assert status == Status.Success


# ---------------------------------------------------------------------------
# Sessions / send / expect — gated on SSH-based UnixHost parent
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_open_session_rejects_non_remote_parent():
    h = _make_container()  # MagicMock parent
    with pytest.raises(NotImplementedError, match="SSH-based UnixHost parent"):
        await h.open_session("foo")


@pytest.mark.asyncio
async def test_send_rejects_non_remote_parent():
    h = _make_container()
    with pytest.raises(NotImplementedError, match="SSH-based UnixHost parent"):
        await h.send("hi")


@pytest.mark.asyncio
async def test_expect_rejects_non_remote_parent():
    h = _make_container()
    with pytest.raises(NotImplementedError, match="SSH-based UnixHost parent"):
        await h.expect("prompt> ")


# ---------------------------------------------------------------------------
# put / get — two-step staging
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_put_stages_then_docker_cps_then_cleans_up(tmp_path):
    parent = _mock_parent()
    h = _make_container(parent)
    f = tmp_path / "payload.bin"
    f.write_bytes(b"x" * 16)

    status, _ = _sm(await h.put([f], Path("/srv/in")))

    assert status == Status.Success
    parent.put.assert_awaited_once()
    # Verify the calls to exec in order: mkdir, docker cp, rm -rf
    cmds = [c.args[0] for c in parent.exec.call_args_list]
    assert any("mkdir -p" in c for c in cmds), cmds
    assert any("docker cp" in c and h.container_id in c and "/srv/in" in c for c in cmds), cmds
    assert any("rm -rf" in c for c in cmds), cmds
    # The docker cp's duration IS the transfer (scales with file size), so it
    # must be unbounded — never inherit the 30s default.
    cp_call = next(c for c in parent.exec.call_args_list if "docker cp" in c.args[0])
    assert cp_call.kwargs.get("timeout") == float("inf")


@pytest.mark.asyncio
async def test_put_failure_still_cleans_up(tmp_path):
    parent = _mock_parent()
    f = tmp_path / "payload.bin"
    f.write_bytes(b"x")
    h = _make_container(parent)

    # Make docker cp fail; the surrounding mkdir & rm -rf should still both run.
    def exec_side_effect(cmd, *_, **__):
        if "docker cp" in cmd:
            return _fail(cmd, out="cp failed")
        return _ok()

    parent.exec.side_effect = exec_side_effect

    status, msg = _sm(await h.put([f], Path("/srv/in")))
    assert status == Status.Error
    assert "cp failed" in msg
    cmds = [c.args[0] for c in parent.exec.call_args_list]
    assert any("rm -rf" in c for c in cmds), "cleanup must run on failure"


@pytest.mark.asyncio
async def test_get_two_step_via_parent():
    parent = _mock_parent()
    h = _make_container(parent)

    status, _ = _sm(await h.get(Path("/etc/os-release"), Path("./out")))

    assert status == Status.Success
    cmds = [c.args[0] for c in parent.exec.call_args_list]
    assert any("docker cp" in c and h.container_id in c for c in cmds), cmds
    parent.get.assert_awaited_once()
    args, _ = parent.get.call_args
    assert args[1] == Path("./out")
    # The docker cp's duration IS the transfer (scales with file size), so it
    # must be unbounded — never inherit the 30s default.
    cp_call = next(c for c in parent.exec.call_args_list if "docker cp" in c.args[0])
    assert cp_call.kwargs.get("timeout") == float("inf")


@pytest.mark.asyncio
async def test_show_progress_reaches_the_staging_leg():
    """``show_progress=False`` must reach the parent-host transfer, not stop at the container.

    The ``docker cp`` step has no progress to render, but the staging leg is
    an ordinary transfer to/from the parent host that DOES — so a caller
    suppressing bars through the ``Host`` protocol (the coverage fetcher's
    bulk gcda fetch) gets bars from the parent leg unless the flag is
    forwarded. Accepting the keyword and dropping it would pass every
    signature test and still render.
    """
    parent = _mock_parent()
    h = _make_container(parent)

    await h.put(Path("a.bin"), Path("/data"), show_progress=False)
    await h.get(Path("/etc/os-release"), Path("./out"), show_progress=False)

    assert parent.put.call_args.kwargs.get("show_progress") is False
    assert parent.get.call_args.kwargs.get("show_progress") is False


# ---------------------------------------------------------------------------
# put/get user= — docker chown flow
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_put_chowns_landed_files_as_root():
    """No per-call user: the declared service user is the chown target."""
    parent = _mock_parent()
    h = _make_container(parent, user="postgres")

    status, _ = _sm(await h.put(Path("a.bin"), Path("/data")))

    assert status == Status.Success
    cmds = [c.args[0] for c in parent.exec.call_args_list]
    chown_calls = [c for c in cmds if "chown" in c]
    assert len(chown_calls) == 1, cmds
    assert chown_calls[0] == (
        f"docker exec -i -u root {h.container_id} sh -c 'chown postgres /data/a.bin'"
    )


@pytest.mark.asyncio
async def test_put_chown_failure_fails_loudly_per_file():
    """A per-call user beats the (absent) declared default, and a failed
    chown flips the landed file to an error naming the file, the user, and
    the reason — never a silent wrong-ownership success.
    """
    parent = _mock_parent()

    def exec_side_effect(cmd, *_, **__):
        if "chown" in cmd:
            return _fail(cmd, out="chown: changing ownership: Operation not permitted")
        return _ok(cmd)

    parent.exec.side_effect = exec_side_effect
    h = _make_container(parent)

    result = await h.put(Path("a.bin"), Path("/data"), user="1000:1000")

    assert not result.is_ok
    per_file = result.value[Path("a.bin")]
    assert per_file.status == Status.Error
    assert "transferred, but chown to '1000:1000' failed" in per_file.msg
    # dest_path is still carried even though ownership never landed.
    assert per_file.value == Path("/data/a.bin")


@pytest.mark.asyncio
async def test_put_no_user_no_chown():
    """Neither a per-call nor a declared user: no chown runs at all."""
    parent = _mock_parent()
    h = _make_container(parent)

    status, _ = _sm(await h.put(Path("a.bin"), Path("/data")))

    assert status == Status.Success
    cmds = [c.args[0] for c in parent.exec.call_args_list]
    assert not any("chown" in c for c in cmds), cmds


@pytest.mark.asyncio
async def test_get_accepts_and_ignores_user():
    """get(user=...) is accepted for interface uniformity but does nothing —
    reads are ownership-indifferent, so nothing user-flavored ever runs.
    """
    parent = _mock_parent()
    h = _make_container(parent)

    status, _ = _sm(await h.get(Path("/data/a.bin"), Path("/tmp/out"), user="root"))

    assert status == Status.Success
    cmds = [c.args[0] for c in parent.exec.call_args_list]
    assert not any(" -u " in c for c in cmds), cmds


@pytest.mark.asyncio
async def test_put_mode_and_user_chmods_as_root():
    """When both mode and user are given, the chmod ALSO runs as root inside
    the container via the parent — the effective user may not own what
    docker cp just landed, so only root can be relied on to chmod it.
    """
    parent = _mock_parent()
    h = _make_container(parent, user="postgres")

    status, _ = _sm(await h.put(Path("a.bin"), Path("/data"), mode="644"))

    assert status == Status.Success
    cmds = [c.args[0] for c in parent.exec.call_args_list]
    chmod_calls = [c for c in cmds if "chmod" in c]
    assert len(chmod_calls) == 1, cmds
    assert chmod_calls[0] == (
        f"docker exec -i -u root {h.container_id} sh -c 'chmod 644 -- /data/a.bin'"
    )
    chown_calls = [c for c in cmds if "chown" in c]
    assert len(chown_calls) == 1, cmds


@pytest.mark.asyncio
async def test_put_chown_precedes_chmod_to_preserve_setuid():
    """chown MUST run before chmod: chowning a file after it lands its mode
    clears S_ISUID/S_ISGID on most filesystems, which would silently defeat
    a `mode="4755"` + `user=...` put. Pin the ORDER via transcript index —
    a bare call count can't tell "chown then chmod" from "chmod then chown".
    """
    parent = _mock_parent()
    h = _make_container(parent)

    status, _ = _sm(await h.put(Path("a.bin"), Path("/data"), mode="4755", user="app"))

    assert status == Status.Success
    cmds = [c.args[0] for c in parent.exec.call_args_list]
    chown_cmd = f"docker exec -i -u root {h.container_id} sh -c 'chown app /data/a.bin'"
    chmod_cmd = f"docker exec -i -u root {h.container_id} sh -c 'chmod 4755 -- /data/a.bin'"
    assert chown_cmd in cmds, cmds
    assert chmod_cmd in cmds, cmds
    assert cmds.index(chown_cmd) < cmds.index(chmod_cmd), cmds


@pytest.mark.asyncio
async def test_put_validates_user():
    """Entering at the public method: a malformed `user` (here, embedded
    whitespace) is refused with `_validate_user`'s own message."""
    h = _make_container()
    with pytest.raises(ValueError, match="non-empty string with no whitespace"):
        await h.put(Path("a.bin"), Path("/data"), user=" root")


@pytest.mark.asyncio
async def test_get_validates_user():
    h = _make_container()
    with pytest.raises(ValueError, match="non-empty string with no whitespace"):
        await h.get(Path("/data/a.bin"), Path("/tmp/out"), user=" root")


@pytest.mark.asyncio
async def test_put_dry_run_validates_user_before_declining():
    """`_validate_user` sits ABOVE the dry-run arm: a typo'd `user` under a
    dry run raises rather than being folded into a harmless preview."""
    parent = _mock_parent()
    h = _make_container(parent=parent)
    with (
        active_context(dry_run=True),
        pytest.raises(ValueError, match="non-empty string with no whitespace"),
    ):
        await h.put(Path("a.bin"), Path("/data"), user=" root")
    parent.exec.assert_not_awaited()


@pytest.mark.asyncio
async def test_get_dry_run_validates_user_before_declining():
    parent = _mock_parent()
    h = _make_container(parent=parent)
    with (
        active_context(dry_run=True),
        pytest.raises(ValueError, match="non-empty string with no whitespace"),
    ):
        await h.get(Path("/data/a.bin"), Path("/tmp/out"), user=" root")
    parent.exec.assert_not_awaited()


@pytest.mark.asyncio
async def test_login_dry_run_validates_user_before_declining():
    """`_validate_user` sits ABOVE the dry-run arm in `login`, same as
    `put`/`get`: a typo'd `user` under a dry run raises rather than being
    folded into a harmless preview."""
    h = _make_container()
    with (
        patch.object(h, "_login", new_callable=AsyncMock) as mock,
        active_context(dry_run=True),
        pytest.raises(ValueError, match="non-empty string with no whitespace"),
    ):
        await h.login(user=" root")
    mock.assert_not_awaited()


# ---------------------------------------------------------------------------
# login() preconditions
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_login_requires_remote_ssh_parent():
    # Parent is a MagicMock, NOT a UnixHost — the isinstance check should reject it.
    h = _make_container()
    with pytest.raises(NotImplementedError, match="SSH-based parent"):
        await h._login()


# ---------------------------------------------------------------------------
# Dry-run behavior
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_exec_dry_run_declines_without_reaching_the_parent():
    parent = _mock_parent()
    h = _make_container(parent=parent)
    with active_context(dry_run=True):
        result = await h.exec("echo hi")
    parent.exec.assert_not_awaited()
    assert result.status == Status.NotRun
    assert result.command == "echo hi"  # the caller's, not the docker-exec wrapper
    with pytest.raises(CommandNotRunError):
        _ = result.value


@pytest.mark.asyncio
async def test_run_dry_run_skips_session():
    """_run_one declines without opening a session."""
    parent = _mock_parent()
    h = _make_container(parent=parent)
    with active_context(dry_run=True):
        result = await h.run("ls /")
    parent.exec.assert_not_awaited()
    assert result.only.status == Status.NotRun
    assert result.only.command == "ls /"


@pytest.mark.asyncio
async def test_send_dry_run_returns_without_session():
    parent = _mock_parent()
    h = _make_container(parent=parent)
    with active_context(dry_run=True):
        await h.send("some text")
    # No session should be touched — parent is a MagicMock so open_session
    # would raise NotImplementedError if called.
    parent.exec.assert_not_awaited()


@pytest.mark.asyncio
async def test_expect_dry_run_declines_rather_than_reporting_no_match():
    """Was `assert result == ""` — an empty MATCH, which a caller reads as "absent"."""
    parent = _mock_parent()
    h = _make_container(parent=parent)
    with active_context(dry_run=True), pytest.raises(CommandNotRunError) as exc:
        await h.expect("prompt> ")
    assert "prompt> " in str(exc.value)
    # `_expect_one` calls `_ensure_running()`, so this also pins that a dry-run
    # expect never reaches the daemon (and so can never auto-start the stack).
    parent.exec.assert_not_awaited()


@pytest.mark.asyncio
async def test_put_dry_run_skips_transfer(tmp_path):
    parent = _mock_parent()
    h = _make_container(parent=parent)
    f = tmp_path / "x.txt"
    f.write_text("hello")
    with active_context(dry_run=True):
        status, msg = _sm(await h.put([f], Path("/dest")))
    parent.exec.assert_not_awaited()
    parent.put.assert_not_awaited()
    assert status == Status.NotRun
    assert "[DRY RUN]" in msg
    assert "PUT" in msg


@pytest.mark.asyncio
async def test_get_dry_run_skips_transfer():
    parent = _mock_parent()
    h = _make_container(parent=parent)
    with active_context(dry_run=True):
        status, msg = _sm(await h.get(Path("/etc/hosts"), Path("./out")))
    parent.exec.assert_not_awaited()
    parent.get.assert_not_awaited()
    assert status == Status.NotRun
    assert "[DRY RUN]" in msg
    assert "GET" in msg


# ---------------------------------------------------------------------------
# rebuild_connections
# ---------------------------------------------------------------------------


def test_rebuild_connections_swaps_session_mgr():
    parent = _build_fake_ssh_remote_host()
    h = _make_container(parent=parent)
    old = h._session_mgr
    sentinel_mgr = MagicMock()
    with patch.object(h, "_build_session_mgr", return_value=sentinel_mgr):
        h.rebuild_connections()
    assert h._session_mgr is sentinel_mgr
    assert h._session_mgr is not old


# ---------------------------------------------------------------------------
# put / get error returns
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_put_mkdir_failure_returns_error(tmp_path):
    parent = _mock_parent()
    h = _make_container(parent=parent)
    f = tmp_path / "payload.bin"
    f.write_bytes(b"data")

    def exec_side_effect(cmd, *args, **kwargs):
        if "mkdir" in cmd:
            return _fail(cmd, out="Permission denied")
        return _ok()

    parent.exec.side_effect = exec_side_effect

    status, msg = _sm(await h.put([f], Path("/dest")))
    assert status == Status.Error
    assert "failed to create staging dir" in msg


@pytest.mark.asyncio
async def test_put_parent_put_failure_passthrough(tmp_path):
    parent = _mock_parent()
    h = _make_container(parent=parent)
    f = tmp_path / "payload.bin"
    f.write_bytes(b"data")

    parent.put.return_value = Result(
        Status.Error,
        value={f: Result(Status.Error, msg="sftp connection lost")},
        msg="sftp connection lost",
    )

    result = await h.put([f], Path("/dest"))
    assert result.status == Status.Error
    assert "sftp connection lost" in result.msg
    # Failure path must key by the as-passed source paths.
    assert set(result.value.keys()) == {f}
    assert not result.value[f].is_ok


@pytest.mark.asyncio
async def test_put_partial_staging_failure_downgrades_staged_files(tmp_path):
    parent = _mock_parent()
    h = _make_container(parent=parent)
    ok_file = tmp_path / "ok.bin"
    bad_file = tmp_path / "bad.bin"
    ok_file.write_bytes(b"data")
    bad_file.write_bytes(b"data")

    parent.put.return_value = Result(
        Status.Error,
        value={
            ok_file: Result(Status.Success, value=Path("/stage/ok.bin")),
            bad_file: Result(Status.Error, msg="bad.bin: sftp write failed"),
        },
        msg="bad.bin: sftp write failed",
    )

    result = await h.put([ok_file, bad_file], Path("/dest"))
    assert result.status == Status.Error
    # A file that only reached the parent staging dir must NOT read as
    # Success — docker cp never ran, so it never reached the container.
    assert result.value[ok_file].status == Status.Skipped
    assert not result.value[bad_file].is_ok


@pytest.mark.asyncio
async def test_put_docker_cp_failure_returns_error(tmp_path):
    parent = _mock_parent()
    h = _make_container(parent=parent)
    f = tmp_path / "payload.bin"
    f.write_bytes(b"data")

    def exec_side_effect(cmd, *args, **kwargs):
        if "docker cp" in cmd:
            return _fail(cmd, out="no such container")
        return _ok()

    parent.exec.side_effect = exec_side_effect

    status, msg = _sm(await h.put([f], Path("/dest")))
    assert status == Status.Error
    assert "docker cp failed" in msg


@pytest.mark.asyncio
async def test_get_mkdir_failure_returns_error():
    parent = _mock_parent()
    h = _make_container(parent=parent)

    def exec_side_effect(cmd, *args, **kwargs):
        if "mkdir" in cmd:
            return _fail(cmd, out="read-only filesystem")
        return _ok()

    parent.exec.side_effect = exec_side_effect

    status, msg = _sm(await h.get(Path("/etc/os-release"), Path("./out")))
    assert status == Status.Error
    assert "failed to create staging dir" in msg


@pytest.mark.asyncio
async def test_get_docker_cp_failure_returns_error():
    parent = _mock_parent()
    h = _make_container(parent=parent)

    def exec_side_effect(cmd, *args, **kwargs):
        if "docker cp" in cmd:
            return _fail(cmd, out="container not found")
        return _ok()

    parent.exec.side_effect = exec_side_effect

    status, msg = _sm(await h.get(Path("/etc/os-release"), Path("./out")))
    assert status == Status.Error
    assert "docker cp failed" in msg


@pytest.mark.asyncio
async def test_get_mid_batch_docker_cp_failure_keeps_every_source_key(tmp_path):
    """A mid-batch docker-cp failure must still key EVERY as-passed source
    path — including files already copied to parent staging before the
    failing file. Those earlier files never reach the caller (parent.get is
    never invoked and the staging dir is removed in `finally`), so they must
    be downgraded to Skipped rather than omitted; omitting them would make
    ``result.value[first_file]`` raise KeyError."""
    parent = _mock_parent()
    h = _make_container(parent=parent)
    first = Path("/remote/first.bin")
    second = Path("/remote/second.bin")

    def exec_side_effect(cmd, *args, **kwargs):
        if "docker cp" in cmd and "second.bin" in cmd:
            return _fail(cmd, out="no such file or directory")
        return _ok()

    parent.exec.side_effect = exec_side_effect

    result = await h.get([first, second], tmp_path)
    assert result.status == Status.Error
    # Every as-passed source path must be a key — no KeyError on lookup.
    assert set(result.value.keys()) == {first, second}
    assert result.value[first].status == Status.Skipped
    assert not result.value[second].is_ok


# ---------------------------------------------------------------------------
# _login — non-ssh parent rejection + ssh happy path
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_login_rejects_non_ssh_parent():
    """telnet parent raises NotImplementedError (parent is UnixHost but term != ssh)."""
    from otto.host.connections import ConnectionManager
    from otto.host.unix_host import UnixHost

    class FakeTelnetConnections(ConnectionManager):
        def __init__(self, *args, **kwargs):  # type: ignore[no-untyped-def]
            self._ssh_conn = None
            self._sftp_conn = None
            self._ftp_conn = None
            self._telnet_conn = None
            self._name = kwargs.get("name", "fake")
            self._term = "telnet"
            self._hop = None

    telnet_parent = UnixHost(
        ip="10.0.0.1",
        creds=[Cred(login="root", password="x")],
        element="fake_ne",
        term="telnet",
        _connection_factory=FakeTelnetConnections,
    )
    h = _make_container(parent=telnet_parent)
    with pytest.raises(NotImplementedError):
        await h._login()


@pytest.mark.asyncio
async def test_login_ssh_runs_docker_exec():
    """SSH parent: _login calls run_ssh_login with docker exec -it command."""
    parent = _build_fake_ssh_remote_host()
    h = _make_container(parent=parent, container_id="mycontainer123")

    with patch("otto.host.interact.run_ssh_login", new_callable=AsyncMock) as mock_login:
        await h._login()

    mock_login.assert_awaited_once()
    call_kwargs = mock_login.call_args.kwargs
    expected_cmd = f"docker exec -it {shlex.quote(h.container_id)} /bin/sh"
    assert "command" in call_kwargs
    assert expected_cmd in call_kwargs["command"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("declared", "per_call", "expected_u"),
    [
        (None, None, None),  # neither → no -u, image's USER prevails
        ("postgres", None, "postgres"),  # declared default
        ("postgres", "root", "root"),  # per-call beats declared
        (None, "post gres", "post gres"),  # a user needing shlex quoting
    ],
)
async def test_docker_login_carries_effective_user(declared, per_call, expected_u):
    """``_login`` threads the effective user into ``docker exec -u`` — enter
    through ``host._login()`` itself (not by composing ``_effective_user``
    separately) so a broken hop in the real method shows up here.

    Pins the FULL command string, not a substring: ``-u`` must land BEFORE
    the container id (``docker exec`` treats anything after the id as
    in-container argv, so a trailing ``-u`` would be silently swallowed by
    the container's own command rather than selecting a user), and the user
    value must be ``shlex.quote``d (the ``"post gres"`` row catches a
    regression that drops the quoting).
    """
    parent = _build_fake_ssh_remote_host()
    h = _make_container(parent, user=declared)

    with patch("otto.host.interact.run_ssh_login", new_callable=AsyncMock) as mock_login:
        await h._login(per_call)

    cmd = mock_login.call_args.kwargs["command"]
    quoted_cid = shlex.quote(h.container_id)
    if expected_u is None:
        assert cmd == f"docker exec -it {quoted_cid} /bin/sh"
    else:
        assert cmd == f"docker exec -it -u {shlex.quote(expected_u)} {quoted_cid} /bin/sh"


# ---------------------------------------------------------------------------
# Staging-dir cleanup is best-effort (G15: no awaited exec in a bare finally)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_put_cleanup_failure_is_warned_not_raised(tmp_path, caplog):
    """A transfer that WORKED must not fail because the staging rm -rf died."""
    parent = _mock_parent()
    f = tmp_path / "payload.bin"
    f.write_bytes(b"x")
    h = _make_container(parent)

    def exec_side_effect(cmd, *_, **__):
        if "rm -rf" in cmd:
            raise RuntimeError("parent vanished during cleanup")
        return _ok()

    parent.exec.side_effect = exec_side_effect

    with caplog.at_level("WARNING", logger="otto.host.connections"):
        status, _ = _sm(await h.put([f], Path("/srv/in")))

    assert status == Status.Success
    assert any("staging-dir removal teardown failed" in r.message for r in caplog.records)


@pytest.mark.asyncio
async def test_put_cleanup_failure_does_not_mask_the_bodys_error(tmp_path):
    """The finally's rm -rf raising must not replace the transfer's own exception.

    This is the exact shape behind gate G15: pre-fix, the caller debugged
    "cleanup also died" while the real failure was the staging transport.
    """
    parent = _mock_parent()
    f = tmp_path / "payload.bin"
    f.write_bytes(b"x")
    h = _make_container(parent)
    parent.put.side_effect = RuntimeError("staging transport died")

    def exec_side_effect(cmd, *_, **__):
        if "rm -rf" in cmd:
            raise RuntimeError("cleanup also died")
        return _ok()

    parent.exec.side_effect = exec_side_effect

    with pytest.raises(RuntimeError, match="staging transport died"):
        await h.put([f], Path("/srv/in"))


@pytest.mark.asyncio
async def test_get_cleanup_failure_is_warned_not_raised(tmp_path, caplog):
    """get() mirrors put(): a failed staging rm -rf is a warning, not the outcome."""
    parent = _mock_parent()
    h = _make_container(parent)

    def exec_side_effect(cmd, *_, **__):
        if "rm -rf" in cmd:
            raise RuntimeError("parent vanished during cleanup")
        return _ok()

    parent.exec.side_effect = exec_side_effect

    with caplog.at_level("WARNING", logger="otto.host.connections"):
        status, _ = _sm(await h.get([Path("/var/log/syslog")], tmp_path))

    assert status == Status.Success
    assert any("staging-dir removal teardown failed" in r.message for r in caplog.records)
