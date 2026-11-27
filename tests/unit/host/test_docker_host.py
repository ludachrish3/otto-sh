"""Unit tests for DockerContainerHost.

These tests use mocked parents so they run without docker, ssh, or any
network. They verify command-shape correctness, two-step staging, and
the placeholder ``container_id == ""`` guard.
"""

from __future__ import annotations

import asyncio
import logging
import shlex
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from otto.errors import OttoError
from otto.host.docker_host import DockerContainerHost
from otto.host.element import Element
from otto.host.errors import (
    ContainerNotRunningError,
    HostUnreachableError,
    MountNotFoundError,
    UnsupportedOptionError,
)
from otto.host.inventory_ref import InventoryRef
from otto.host.login_proxy import Cred
from otto.host.mount import Mount
from otto.result import CommandNotRunError, CommandResult, Result
from otto.utils import Status
from tests._fixtures.private_root import CONTAINER_STAGING_ROOT
from tests.conftest import active_context
from tests.unit.host._exec_log import (
    SU_LOGIN,
    all_records,
    close_ssh_parent,
    records_of,
    ssh_parent_over_shells,
)

# The container's parent here is a mock, not a unix host with a login, so its
# private staging root is answered for it; the root itself is pinned in
# test_private_staging_root.py.
pytestmark = pytest.mark.usefixtures("trusted_container_staging")


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

    async def _staged_ok(files, dest, *args, **kwargs):
        """A real parent ``put`` always reports PER FILE; the container's put
        reads those entries to decide which files reach ``docker cp``, so a
        mock that answered with an empty mapping would make every file look
        unstaged."""
        return Result(
            status=Status.Success,
            value={p: Result(status=Status.Success, value=dest / p.name) for p in files},
        )

    parent.put = AsyncMock(side_effect=_staged_ok)
    parent.get = AsyncMock(return_value=Result(status=Status.Success, value={}))
    return parent


def _make_container(
    parent=None, container_id: str = "abc123def456", *, user: str | None = None
) -> DockerContainerHost:
    return DockerContainerHost(
        parent=parent or _mock_parent(),
        container_id=container_id,
        project="project1",
        service="api",
        compose_project="otto-project1-vagrant",
        user=user,
    )


def _build_fake_ssh_remote_host():
    """Construct a real UnixHost whose ``ssh`` term is a registered fake ConnectionManager.

    Real UnixHost is needed so `isinstance(parent, UnixHost)` passes in
    `_make_session`; the fake ConnectionManager keeps the test offline. It is
    registered over the built-in ``ssh``, which the root conftest's registry
    isolation restores after the test.
    """
    from otto.host.connections import ConnectionManager, register_term_backend
    from otto.host.unix_host import UnixHost

    class FakeConnections(ConnectionManager):
        def __init__(self, **kwargs):  # type: ignore[no-untyped-def]
            super().__init__(**kwargs)  # no I/O: every slot starts empty
            self._ssh_conn = MagicMock()  # not awaited in unit tests

        async def ssh(self):
            return self._ssh_conn

    register_term_backend(
        "ssh",
        FakeConnections,
        host_families=frozenset({"unix"}),
        authenticates=True,
        overwrite=True,
    )
    return UnixHost(
        ip="10.0.0.1",
        creds=[Cred(login="root", password="x")],
        element=Element(name="fake_ne"),
        term="ssh",
    )


# ---------------------------------------------------------------------------
# Construction & identity
# ---------------------------------------------------------------------------


def test_id_format():
    h = _make_container(_mock_parent("test3"))
    assert h.id == "test3.project1.api"


def test_id_lowercased():
    h = DockerContainerHost(
        parent=_mock_parent("TEST3"),
        container_id="abc",
        project="Project1",
        service="API",
        compose_project="proj",
    )
    assert h.id == "test3.project1.api"


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


@pytest.mark.asyncio
async def test_exec_sudo_runs_as_root_in_the_container():
    """No pty on the parent's exec channel to answer `sudo -S` on, so `sudo` on a
    container is `docker exec -u root` — what elevation means there."""
    parent = _mock_parent()
    container = _make_container(parent)
    container.parent.exec = AsyncMock(
        return_value=CommandResult(status=Status.Success, value="uid=0", command="x", retcode=0)
    )
    container._ensure_running = AsyncMock()
    await container.exec("id", sudo=True)
    wrapped = container.parent.exec.await_args.args[0]
    assert " -u root " in wrapped
    assert "sudo" not in wrapped


@pytest.mark.asyncio
async def test_exec_user_and_sudo_together_refuse_above_the_dry_run_arm():
    parent = _mock_parent()
    container = _make_container(parent)
    with pytest.raises(NotImplementedError, match="both choose the user"):
        await container.exec("id", user="app", sudo=True)
    with (
        active_context(dry_run=True),
        pytest.raises(NotImplementedError, match="both choose the user"),
    ):
        await container.exec("id", user="app", sudo=True)


@pytest.mark.asyncio
async def test_exec_forwards_expects_to_the_parents_exec():
    """The parent's own pooled shell answers prompts; `docker exec -i` keeps
    stdin open for that hop. `_exec_via_parent` must pass `expects` through
    rather than silently dropping it."""
    parent = _mock_parent()
    h = _make_container(parent)
    parent.exec.return_value = _ok(out="0")

    await h.exec("id", expects=[("assword", "pw\n")])

    assert parent.exec.await_args.kwargs["expects"] == [("assword", "pw\n")]


@pytest.mark.parametrize("terminal", [None, False])
@pytest.mark.asyncio
async def test_exec_without_a_terminal_is_the_exec_a_container_always_had(terminal):
    """``docker exec -i`` gives the command no terminal; the parent is told nothing new."""
    parent = _mock_parent()
    h = _make_container(parent)
    await h.exec("id", terminal=terminal)
    sent = parent.exec.await_args
    assert sent.args[0] == f"docker exec -i {h.container_id} sh -c id"
    assert "terminal" not in sent.kwargs


@pytest.mark.parametrize(
    ("verb", "terminal", "reason"),
    [
        ("exec", True, "terminal=True is not available for a container's exec() yet"),
        ("run", False, "terminal=False is not available for a container's run() and"),
        ("open_session", False, "terminal=False is not available for a container's run() and"),
    ],
)
@pytest.mark.asyncio
async def test_a_container_refuses_the_mode_an_entry_does_not_have_before_any_lookup(
    verb, terminal, reason
):
    parent = _mock_parent()
    h = _make_container(parent, container_id="")
    with pytest.raises(UnsupportedOptionError) as refused:
        await getattr(h, verb)("aux" if verb == "open_session" else "true", terminal=terminal)
    assert reason in str(refused.value)
    parent.exec.assert_not_awaited()


@pytest.mark.asyncio
async def test_mkdir_all_creates_every_directory_as_root_in_one_command():
    """``_mkdir_all`` delegates to the base ``PosixFileOps`` implementation with
    ``user="root"`` — one ``parent.exec`` call, the container's ``docker exec
    -u root`` wrapping a single ``mkdir -p`` naming every path, quoted.
    """
    parent = _mock_parent()
    h = _make_container(parent)
    parent.exec.return_value = _ok(out="")

    result = await h._mkdir_all([Path("/a b/c"), Path("/c d")])

    assert result.is_ok
    assert parent.exec.call_count == 1
    sent = parent.exec.call_args.args[0]
    assert "mkdir -p" in sent
    assert shlex.quote("/a b/c") in sent
    assert shlex.quote("/c d") in sent
    assert " -u root " in sent


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
        project="project1",
        service="api",
        compose_project="otto-project1-vagrant",
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
        project="project1",
        service="api",
        compose_project="otto-project1-vagrant",
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
    and ``on_open``), ``_DockerSshSession.open_transport`` (which builds the command
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
        await self.open_transport()  # the real _DockerSshSession.open_transport
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
        patch.object(SshSession, "open_transport", new=_fake_transport_open),
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
async def test_exec_inherits_an_enclosing_as_user_like_run_does(monkeypatch):
    """D11 on the container family: a switch on the default session is what
    ``exec`` runs as too.

    The container's ``as_user`` is real (``PosixPrivilege`` → ``su`` on the
    persistent channel), so without an ``_ambient_user`` of its own
    ``async with container.as_user("app")`` would run ``run`` as ``app`` and
    ``exec`` as the declared default — the silent split D11 removes.
    """
    from otto.host.unix_host import UnixHost

    host = _channel_container(user="postgres")
    sent: list[str] = []

    async def _parent_exec(self, cmd, **kwargs):
        sent.append(cmd)
        return _ok(cmd)

    monkeypatch.setattr(UnixHost, "_exec_delegated", _parent_exec)

    with _stubbed_docker_channel():
        await host.run("id")  # opens the default session
        mgr = host._session_mgr
        assert mgr._baseline_user is not None, (
            "the container's default session records no baseline user, so no "
            "switch on it could ever be reported as ambient"
        )
        assert host._ambient_user() is None, "an unswitched session has no ambient identity"
        await host.exec("id")
        assert " -u postgres " in sent[-1], "outside a switch: the declared default"

        # What `PosixPrivilege.as_user` records once the real `su` has run.
        mgr._set_current_user("app")
        assert host._ambient_user() == "app"
        await host.exec("id")
        assert " -u app " in sent[-1]
        assert " -u postgres " not in sent[-1]


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
                SshSession, "open_transport", new=AsyncMock(side_effect=RuntimeError("no channel"))
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
                SshSession, "open_transport", new=AsyncMock(side_effect=RuntimeError("no channel"))
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
            await self.open_transport()  # fires the on-open callback for real
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
# Placeholder (container_id == "") — fails fast; nothing is ever started
# ---------------------------------------------------------------------------


def _probes(parent: MagicMock) -> list[str]:
    """Every command the mocked *parent* was asked to run, in order."""
    return [call.args[0] for call in parent.exec.call_args_list]


@pytest.mark.asyncio
async def test_a_down_placeholder_fails_fast_and_starts_nothing():
    """Touching a declared container whose stack is down raises; nothing starts.

    The parent is asked one question, the read-only ``docker ps`` probe, and
    nothing else: no ``docker compose``, no ``docker exec``. The error keeps
    ``RuntimeError`` as its stdlib root, as every failure on this path had, so
    an existing ``except RuntimeError`` still catches it.
    """
    parent = _mock_parent()  # docker ps returns empty out -> not running
    h = _make_container(parent, container_id="")

    with pytest.raises(ContainerNotRunningError) as exc:
        await h.exec("echo hi")

    assert isinstance(exc.value, RuntimeError)
    assert isinstance(exc.value, OttoError)
    assert h.container_id == "", "a down placeholder cached an id"
    probes = _probes(parent)
    assert len(probes) == 1, probes
    assert probes[0].startswith("docker ps -q"), probes
    assert "com.docker.compose.project=otto-project1-vagrant" in probes[0], probes


@pytest.mark.asyncio
@pytest.mark.parametrize("project", ["project1", "integration"])
async def test_the_error_names_the_container_and_each_way_to_start_it(project):
    """The message is the remedy: the container, and each explicit way to start it.

    The host does not ask whether ``project`` names a declared use-case or a
    project's own compose files, so the message offers the route for each and
    says which is which.
    """
    parent = _mock_parent()
    h = DockerContainerHost(
        parent=parent,
        container_id="",
        project=project,
        service="api",
        compose_project=f"unix-{project}-vagrant",
    )

    with pytest.raises(ContainerNotRunningError) as exc:
        await h.exec("echo hi")

    msg = str(exc.value)
    assert f"Container 'test3.{project}.api' is not running" in msg, msg
    assert f"compose project 'unix-{project}-vagrant'" in msg, msg
    assert f"`otto docker compose up {project}` (a use-case)" in msg, msg
    assert f"`otto.docker.deploy({project!r})` (a use-case)" in msg, msg
    assert "`otto.docker.compose_up(...)` (a project's own compose files)" in msg, msg
    assert "session-scoped fixture" in msg, msg


@pytest.mark.asyncio
async def test_a_failed_probe_says_why_with_dockers_own_text_and_starts_nothing():
    """A probe that RAN and failed (the daemon down, no docker binary, no
    permission on the socket) is not reported as "nothing is running": the
    error names the probe, its exit and Docker's own text, after the one probe."""
    parent = _mock_parent()
    parent.exec = AsyncMock(
        return_value=_fail(
            out="Cannot connect to the Docker daemon at unix:///var/run/docker.sock\n"
        )
    )
    h = _make_container(parent, container_id="")

    with pytest.raises(ContainerNotRunningError) as exc:
        await h.exec("echo hi")

    msg = str(exc.value)
    probes = _probes(parent)
    assert len(probes) == 1, probes
    assert probes[0].startswith("docker ps -q"), probes
    assert "Cannot connect to the Docker daemon" in msg, msg
    assert probes[0] in msg, msg
    assert "failed on parent 'test3'" in msg, msg
    assert "otto does not start containers on use" in msg, msg
    assert "no running container was found" not in msg, msg
    assert h.container_id == "", "a failed probe cached an id"


_NOT_COMPLETED = [
    pytest.param(
        CommandResult(
            status=Status.Error,
            value="Command timed out after 30.0s",
            command="",
            retcode=-1,
            timed_out=True,
        ),
        id="timed-out",
    ),
    pytest.param(
        CommandResult(
            status=Status.Error,
            value="Session died unexpectedly (connection lost)",
            command="",
            retcode=-1,
        ),
        id="session-died",
    ),
]


@pytest.mark.asyncio
@pytest.mark.parametrize("answer", _NOT_COMPLETED)
async def test_a_probe_the_parent_could_not_complete_is_an_unreachable_parent(answer):
    """A probe that never completed (a timeout, or the parent's session lost
    mid-command) is a transport failure, not a verdict about the container:
    ``HostUnreachableError`` naming the container and its parent, and nothing
    started."""
    parent = _mock_parent()
    parent.exec = AsyncMock(return_value=answer)
    h = _make_container(parent, container_id="")

    with pytest.raises(HostUnreachableError) as exc:
        await h.exec("echo hi")

    assert not isinstance(exc.value, ContainerNotRunningError)
    msg = str(exc.value)
    assert "'test3.project1.api'" in msg, msg
    assert "parent 'test3'" in msg, msg
    probes = _probes(parent)
    assert len(probes) == 1, probes
    assert probes[0].startswith("docker ps -q"), probes
    assert h.container_id == "", "an unreachable parent cached an id"


@pytest.mark.asyncio
async def test_a_parent_whose_transport_raises_propagates_its_own_error():
    """A parent that cannot even open its transport RAISES out of ``exec``;
    that exception reaches the caller unchanged, never re-worded as a
    container that is not running."""
    parent = _mock_parent()
    boom = ConnectionRefusedError("connect to test3 port 22 refused")
    parent.exec = AsyncMock(side_effect=boom)
    h = _make_container(parent, container_id="")

    with pytest.raises(ConnectionRefusedError) as exc:
        await h.exec("echo hi")

    assert exc.value is boom
    assert len(_probes(parent)) == 1
    assert h.container_id == ""


@pytest.mark.asyncio
async def test_a_running_placeholder_resolves_its_id_once_and_runs():
    """The probe's other answer: a stack that IS up (brought up by another
    process) resolves, the id is cached, and the command runs in that container."""
    parent = _mock_parent()

    async def answer(cmd, *_args, **_kwargs):
        return _ok(cmd, "freshcid\n" if cmd.startswith("docker ps -q") else "hi")

    parent.exec = AsyncMock(side_effect=answer)
    h = _make_container(parent, container_id="")

    first = await h.exec("echo hi")
    await h.exec("echo again")

    assert first.status == Status.Success
    assert h.container_id == "freshcid"
    probes = _probes(parent)
    assert [p.startswith("docker ps -q") for p in probes] == [True, False, False], probes
    assert probes[1].startswith("docker exec"), probes
    assert "freshcid" in probes[1], probes


@pytest.mark.asyncio
async def test_a_cached_id_is_trusted_and_a_gone_container_fails_from_docker_exec():
    """Only an UNRESOLVED placeholder is probed. A cached id is used as it is,
    so a container that stopped after its id was cached fails from the
    ``docker exec`` itself, as a failed result; it is not probed and nothing
    is started."""
    parent = _mock_parent()
    parent.exec = AsyncMock(return_value=_fail(out="Error: No such container: abc123def456"))
    h = _make_container(parent)  # the id is already cached

    result = await h.exec("echo hi")

    assert result.status == Status.Failed
    probes = _probes(parent)
    assert len(probes) == 1, probes
    assert probes[0].startswith("docker exec"), probes


async def _use(h: DockerContainerHost, operation: str, tmp_path: Path) -> None:
    """Drive *operation* against *h* the way a caller would."""
    local = tmp_path / "x"
    local.write_text("x")
    if operation == "exec":
        await h.exec("echo hi")
    elif operation == "run":
        await h.run("echo hi")
    elif operation == "put":
        await h.put([local], Path("/tmp"))
    elif operation == "get":
        await h.get([Path("/etc/hostname")], tmp_path)
    elif operation == "send":
        await h.send("echo hi\n")
    else:
        await h.open_session("aux")


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["exec", "run", "put", "get", "send", "open_session"])
async def test_every_operation_on_a_down_placeholder_fails_fast(operation, tmp_path):
    """Each operation resolves the container first, so each raises before any
    transfer, channel or session is opened, and the probe is all the parent saw."""
    parent = _mock_parent()
    h = _make_container(parent, container_id="")

    with pytest.raises(ContainerNotRunningError):
        await _use(h, operation, tmp_path)

    parent.put.assert_not_awaited()
    parent.get.assert_not_awaited()
    probes = _probes(parent)
    assert len(probes) == 1, probes
    assert probes[0].startswith("docker ps -q"), probes


class _WatchedLock(asyncio.Lock):
    """An ``asyncio.Lock`` that says when a caller has to wait for it."""

    def __init__(self) -> None:
        super().__init__()
        self.contended = asyncio.Event()

    async def acquire(self) -> bool:
        if self.locked():
            self.contended.set()
        return await super().acquire()


async def _first_of(
    events: dict[str, asyncio.Event], tasks: list[asyncio.Task], timeout: float = 5.0
) -> str:
    """Name the first of *events* to be set, never waiting past *timeout*.

    A task in *tasks* that ends first surfaces at once: its own exception is
    re-raised, and a clean finish fails the test. *timeout* is a hang guard,
    never a timing assumption: on the passing path an event is always set first.
    """
    waiters = {asyncio.ensure_future(event.wait()): name for name, event in events.items()}
    try:
        done, _pending = await asyncio.wait(
            [*waiters, *tasks], timeout=timeout, return_when=asyncio.FIRST_COMPLETED
        )
    finally:
        for waiter in waiters:
            waiter.cancel()
        await asyncio.gather(*waiters, return_exceptions=True)
    for waiter, name in waiters.items():
        if waiter in done:
            return name
    for task in tasks:
        if task in done:
            raise AssertionError(f"a caller finished early, with {task.result()!r}")
    raise AssertionError(f"none of {sorted(events)} happened within {timeout}s")


@pytest.mark.asyncio
async def test_concurrent_access_raises_for_each_caller_and_starts_nothing():
    """Two concurrent calls against a down placeholder each get the error.

    Real contention, acknowledged rather than hoped for: the first caller's
    probe is held open, and the test goes on only when the second caller is
    seen WAITING on the lock (or, if nothing holds it back, seen probing,
    which fails at once). Then the first probe ends, each caller raises in
    turn, the probes never overlap, nothing starts and no id is cached. Every
    wait is bounded and watches both callers, so a caller that fails early
    surfaces its own error instead of hanging the test.
    """
    parent = _mock_parent()
    first_probe_open = asyncio.Event()
    release_first_probe = asyncio.Event()
    second_probe_started = asyncio.Event()
    in_flight = 0
    most_in_flight = 0

    async def answer(cmd, *_args, **_kwargs):
        nonlocal in_flight, most_in_flight
        in_flight += 1
        most_in_flight = max(most_in_flight, in_flight)
        try:
            if not first_probe_open.is_set():
                first_probe_open.set()
                await release_first_probe.wait()
            else:
                second_probe_started.set()
            return _ok(cmd)
        finally:
            in_flight -= 1

    parent.exec = AsyncMock(side_effect=answer)
    h = _make_container(parent, container_id="")
    lock = _WatchedLock()
    h._ensure_lock = lock

    first = asyncio.create_task(h.exec("echo a"))
    second = None
    try:
        await _first_of({"first probe open": first_probe_open}, [first])
        second = asyncio.create_task(h.exec("echo b"))
        seen = await _first_of(
            {"second waits": lock.contended, "second probed": second_probe_started},
            [first, second],
        )
        assert seen == "second waits", (
            "the second caller probed while the first probe was still open: "
            "nothing serialized the two probes"
        )
        release_first_probe.set()
        for task in (first, second):
            with pytest.raises(ContainerNotRunningError):
                await asyncio.wait_for(task, timeout=5.0)
    finally:
        release_first_probe.set()
        started = [task for task in (first, second) if task is not None]
        for task in started:
            task.cancel()
        await asyncio.gather(*started, return_exceptions=True)

    assert most_in_flight == 1, _probes(parent)
    probes = _probes(parent)
    assert len(probes) == 2, probes
    assert all(p.startswith("docker ps -q") for p in probes), probes
    assert h.container_id == ""


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
async def test_is_running_probes_quietly_and_caches_id():
    """A placeholder probe is one read-only, QUIET `docker ps` — never a compose."""
    from otto.logger.mode import LogMode

    parent = _mock_parent()
    parent.exec = AsyncMock(return_value=_ok(out="abc123\n"))
    h = _make_container(parent, container_id="")

    assert await h.is_running() is True

    assert h.container_id == "abc123"
    assert parent.exec.await_count == 1, parent.exec.call_args_list
    cmd = parent.exec.call_args.args[0]
    assert cmd.startswith("docker ps -q")
    assert parent.exec.call_args.kwargs.get("log") is LogMode.QUIET


@pytest.mark.asyncio
async def test_is_running_false_when_down_asks_only_the_probe():
    """A down placeholder reports False after one read-only probe, and nothing else."""
    parent = _mock_parent()  # docker ps returns empty out -> not running
    h = _make_container(parent, container_id="")

    assert await h.is_running() is False

    assert h.container_id == ""
    probes = _probes(parent)
    assert len(probes) == 1, probes
    assert probes[0].startswith("docker ps -q"), probes


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "answer",
    [
        pytest.param(_fail(out="Cannot connect to the Docker daemon"), id="probe-failed"),
        *_NOT_COMPLETED,
    ],
)
async def test_is_running_folds_a_failed_or_incomplete_probe_to_false(answer):
    """``is_running`` answers rather than raising: a probe that failed or never
    completed reports ``False``, which tunnel discovery relies on."""
    parent = _mock_parent()
    parent.exec = AsyncMock(return_value=answer)
    h = _make_container(parent, container_id="")

    assert await h.is_running() is False

    assert h.container_id == ""
    assert len(_probes(parent)) == 1


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
async def test_put_refuses_an_option_shaped_user_before_any_transfer():
    """A user beginning with ``-`` is refused by ``_validate_user`` up front,
    never part-way through at the chown after the files have landed."""
    parent = _mock_parent()
    h = _make_container(parent)
    with pytest.raises(ValueError, match="must not begin with '-'"):
        await h.put(Path("a.bin"), Path("/data"), user="-R")
    parent.exec.assert_not_called()


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
        patch.object(h, "run_login", new_callable=AsyncMock) as mock,
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
        await h.run_login()


@pytest.mark.asyncio
async def test_login_force_is_refused_on_a_container():
    """--force is a console reset; a container has no console, so it is
    refused before the parent is inspected or a container started."""
    parent = _build_fake_ssh_remote_host()
    h = _make_container(parent=parent)
    with (
        patch("otto.host.interact.run_ssh_login", new_callable=AsyncMock) as mock_login,
        pytest.raises(ValueError, match="--force applies to console hosts only"),
    ):
        await h.login(force=True)
    mock_login.assert_not_awaited()


@pytest.mark.asyncio
async def test_logout_is_refused_on_a_container_even_under_a_dry_run():
    h = _make_container()
    with pytest.raises(ValueError, match="logout applies to console hosts only"):
        await h.logout()
    with (
        active_context(dry_run=True),
        pytest.raises(ValueError, match=r"a container has no console"),
    ):
        await h.logout()


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
    # expect never reaches the daemon (not even for the container id probe).
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

    parent.put.side_effect = None
    parent.put.return_value = Result(
        status=Status.Error,
        value={f: Result(status=Status.Error, msg="sftp connection lost")},
        msg="sftp connection lost",
    )

    result = await h.put([f], Path("/dest"))
    assert result.status == Status.Error
    assert "sftp connection lost" in result.msg
    # Failure path must key by the as-passed source paths.
    assert set(result.value.keys()) == {f}
    assert not result.value[f].is_ok


@pytest.mark.asyncio
async def test_put_partial_staging_failure_still_copies_the_staged_ok_file(tmp_path):
    """One file's staging failure is its own entry; its siblings still land.

    The staging leg reports per file, so a file whose bytes DID reach the
    parent has nothing wrong with it — abandoning it because a sibling
    failed would throw away work that succeeded and report a file as
    untransferred that could have been transferred.
    """
    parent = _mock_parent()
    h = _make_container(parent=parent)
    ok_file = tmp_path / "ok.bin"
    bad_file = tmp_path / "bad.bin"
    ok_file.write_bytes(b"data")
    bad_file.write_bytes(b"data")

    parent.put.side_effect = None
    parent.put.return_value = Result(
        status=Status.Error,
        value={
            ok_file: Result(status=Status.Success, value=Path("/stage/ok.bin")),
            bad_file: Result(status=Status.Error, msg="bad.bin: sftp write failed"),
        },
        msg="bad.bin: sftp write failed",
    )

    result = await h.put([ok_file, bad_file], Path("/dest"))
    assert result.status == Status.Error
    assert result.value[ok_file].is_ok, (
        "a staged-ok file must reach docker cp even when a sibling failed staging"
    )
    assert result.value[bad_file].status == Status.Error
    cp_calls = [c.args[0] for c in parent.exec.await_args_list if "docker cp" in c.args[0]]
    assert len(cp_calls) == 1
    assert "ok.bin" in cp_calls[0]
    assert not any(r.status is Status.Skipped for r in result.value.values())


@pytest.mark.asyncio
async def test_put_carries_a_staging_skipped_entry_without_a_docker_cp(tmp_path):
    """``Skipped`` is ok-ish, and that is exactly the trap.

    The one ``Skipped`` a transfer still produces is the sibling of a
    directory handed to a non-recursive ``put``. Those bytes never reached
    the staging dir, so testing the staging entry with ``is_ok`` -- which
    ``Skipped`` satisfies -- would ``docker cp`` a path that does not exist
    and report whatever the container's error happened to be. Only
    ``Success`` earns a copy.
    """
    parent = _mock_parent()
    h = _make_container(parent=parent)
    a_dir = tmp_path / "adir"
    a_dir.mkdir()
    sibling = tmp_path / "sibling.bin"
    sibling.write_bytes(b"data")

    parent.put.side_effect = None
    parent.put.return_value = Result(
        status=Status.Error,
        value={
            a_dir: Result(status=Status.Error, msg=f"{a_dir}: is a directory"),
            sibling: Result(status=Status.Skipped, msg="not attempted (directory source refused)"),
        },
        msg=f"{a_dir}: is a directory",
    )

    result = await h.put([a_dir, sibling], Path("/dest"))

    assert result.value[a_dir].status is Status.Error
    assert result.value[sibling].status is Status.Skipped, (
        "a Skipped staging entry must be carried verbatim, not treated as staged"
    )
    cp_calls = [c.args[0] for c in parent.exec.await_args_list if "docker cp" in c.args[0]]
    assert not cp_calls, f"nothing staged, so nothing may be copied: {cp_calls}"


@pytest.mark.asyncio
async def test_put_forwards_concurrent_to_the_staging_leg(tmp_path):
    """The container's own leg is one ``docker cp`` per file; the fan-out
    the caller asked for belongs to the staging transfer, which is the leg
    that actually moves bytes over a link."""
    parent = _mock_parent()
    h = _make_container(parent=parent)
    f = tmp_path / "payload.bin"
    f.write_bytes(b"data")

    await h.put([f], Path("/dest"), concurrent=False)

    assert parent.put.await_args.kwargs["concurrent"] is False


@pytest.mark.asyncio
async def test_two_overlapping_puts_use_distinct_staging_dirs(tmp_path):
    """Two transfers to ONE container must not delete each other's staging.

    The staging dir is removed unconditionally in ``finally``; shared between
    concurrent calls, the first to finish would carry off the other's files
    mid-copy.
    """
    import asyncio

    parent = _mock_parent()
    h = _make_container(parent=parent)
    a = tmp_path / "a.bin"
    b = tmp_path / "b.bin"
    a.write_bytes(b"a")
    b.write_bytes(b"b")

    await asyncio.gather(h.put([a], Path("/dest")), h.put([b], Path("/dest")))

    mkdirs = [
        shlex.split(c.args[0])[-1]
        for c in parent.exec.await_args_list
        if c.args[0].startswith("mkdir -p ")
    ]
    assert len(mkdirs) == 2
    assert mkdirs[0] != mkdirs[1], f"overlapping puts shared a staging dir: {mkdirs}"
    for d in mkdirs:
        # A direct child of the shared root, never under a per-container
        # parent: a parent shared by two transfers is one that the first to
        # finish could remove between the other's two mkdirs.
        assert Path(d).parent == CONTAINER_STAGING_ROOT / ".transfer", d
        assert Path(d).name.startswith("abc123def456-"), d


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
async def test_get_mid_batch_docker_cp_failure_still_fetches_the_files_that_staged(tmp_path):
    """One file's ``docker cp`` failure is its own entry, not a batch abort.

    The files that DID stage are still fetched in the one ``parent.get``;
    every as-passed source path stays a key, so no lookup raises KeyError.
    """
    parent = _mock_parent()
    h = _make_container(parent=parent)
    first = Path("/remote/first.bin")
    second = Path("/remote/second.bin")

    def exec_side_effect(cmd, *args, **kwargs):
        if "docker cp" in cmd and "second.bin" in cmd:
            return _fail(cmd, out="no such file or directory")
        return _ok()

    parent.exec.side_effect = exec_side_effect

    def get_side_effect(files, dest, *args, **kwargs):
        return Result(
            status=Status.Success,
            value={p: Result(status=Status.Success, value=dest / p.name) for p in files},
        )

    parent.get.side_effect = get_side_effect

    result = await h.get([first, second], tmp_path)
    assert result.status == Status.Error
    assert set(result.value.keys()) == {first, second}
    assert result.value[second].status == Status.Error
    parent.get.assert_awaited_once()
    fetched = parent.get.await_args.args[0]
    assert [p.name for p in fetched] == ["first.bin"], "the file that staged must still be fetched"
    assert result.value[first].is_ok


@pytest.mark.asyncio
async def test_get_with_every_docker_cp_failing_never_calls_the_staging_leg(tmp_path):
    """Nothing staged means there is nothing to fetch.

    ``parent.get([])`` would be a pointless round trip on a real parent --
    and on a backend that validates its inputs, an error of its own laid
    over the per-file errors the caller actually needs to read.
    """
    parent = _mock_parent()
    h = _make_container(parent=parent)
    first = Path("/remote/first.bin")
    second = Path("/remote/second.bin")

    def exec_side_effect(cmd, *args, **kwargs):
        if "docker cp" in cmd:
            return _fail(cmd, out="no such file or directory")
        return _ok()

    parent.exec.side_effect = exec_side_effect

    result = await h.get([first, second], tmp_path)

    assert result.status == Status.Error
    parent.get.assert_not_awaited()
    assert set(result.value.keys()) == {first, second}
    assert all(r.status is Status.Error for r in result.value.values())


@pytest.mark.asyncio
async def test_get_forwards_concurrent_to_the_staging_leg(tmp_path):
    """As on :func:`test_put_forwards_concurrent_to_the_staging_leg`: the
    fan-out belongs to the leg that moves bytes over a link."""
    parent = _mock_parent()
    h = _make_container(parent=parent)

    await h.get([Path("/remote/one.bin")], tmp_path, concurrent=False)

    assert parent.get.await_args.kwargs["concurrent"] is False


# ---------------------------------------------------------------------------
# run_login — non-ssh parent rejection + ssh happy path
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_login_rejects_non_ssh_parent():
    """telnet parent raises NotImplementedError (parent is UnixHost but term != ssh)."""
    from otto.host.connections import ConnectionManager, register_term_backend
    from otto.host.unix_host import UnixHost

    class FakeTelnetConnections(ConnectionManager):
        def __init__(self, **kwargs):  # type: ignore[no-untyped-def]
            super().__init__(**kwargs)  # no I/O: every slot starts empty
            self._term = "telnet"

    register_term_backend(
        "telnet",
        FakeTelnetConnections,
        host_families=frozenset({"unix", "embedded"}),
        authenticates=True,
        overwrite=True,
    )
    telnet_parent = UnixHost(
        ip="10.0.0.1",
        creds=[Cred(login="root", password="x")],
        element=Element(name="fake_ne"),
        term="telnet",
    )
    h = _make_container(parent=telnet_parent)
    with pytest.raises(NotImplementedError):
        await h.run_login()


@pytest.mark.asyncio
async def test_login_ssh_runs_docker_exec():
    """SSH parent: run_login calls run_ssh_login with docker exec -it command."""
    parent = _build_fake_ssh_remote_host()
    h = _make_container(parent=parent, container_id="mycontainer123")

    with patch("otto.host.interact.run_ssh_login", new_callable=AsyncMock) as mock_login:
        await h.run_login()

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
    """``run_login`` threads the effective user into ``docker exec -u`` — enter
    through ``host.run_login()`` itself (not by composing ``_effective_user``
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
        await h.run_login(user=per_call)

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


# ---------------------------------------------------------------------------
# Mount translation
# ---------------------------------------------------------------------------


def test_mounts_defaults_to_empty():
    assert _make_container().mounts == []


def test_parent_path_translates_through_a_bind():
    ctr = DockerContainerHost(
        parent=_mock_parent(),
        container_id="abc123def456",
        project="project1",
        service="api",
        compose_project="otto-project1-vagrant",
        mounts=[Mount(Path("/var/lib/app"), Path("/srv/data"), "bind")],
    )
    assert ctr.parent_path("/var/lib/app/logs/x.txt") == Path("/srv/data/logs/x.txt")
    assert ctr.container_path(parent_path="/srv/data/logs/x.txt") == Path("/var/lib/app/logs/x.txt")
    assert ctr.mount_for(container_path="/var/lib/app/logs/x.txt").kind == "bind"
    assert ctr.mount_for(container_path="/etc/hosts") is None


def test_parent_path_names_the_checked_mounts_when_there_is_no_match():
    ctr = DockerContainerHost(
        parent=_mock_parent(),
        container_id="abc123def456",
        project="project1",
        service="api",
        compose_project="otto-project1-vagrant",
        mounts=[Mount(Path("/var/lib/app"), Path("/srv/data"), "bind")],
    )
    with pytest.raises(MountNotFoundError, match="/var/lib/app"):
        ctr.parent_path("/etc/hosts")


def test_container_path_names_the_checked_parent_mounts_when_there_is_no_match():
    """``container_path`` searches the PARENT-side paths, so its refusal must
    name those -- not the container-side paths ``parent_path`` searches."""
    ctr = DockerContainerHost(
        parent=_mock_parent(),
        container_id="abc123def456",
        project="project1",
        service="api",
        compose_project="otto-project1-vagrant",
        mounts=[Mount(Path("/var/lib/app"), Path("/srv/data"), "bind")],
    )
    with pytest.raises(MountNotFoundError, match="/srv/data"):
        ctr.container_path(parent_path="/opt/elsewhere")


def test_parent_path_on_a_placeholder_says_the_container_is_not_up():
    """And that a bring-up in ANOTHER process leaves this table empty too.

    An empty ``container_id`` is not proof the stack is down -- every new
    ``otto`` invocation re-registers declared container hosts as placeholders,
    so a reader hitting this message may well have run ``otto docker compose up``
    already. Naming only the not-up cause would send them to repeat it.
    """
    ctr = _make_container(container_id="")
    with pytest.raises(MountNotFoundError, match="not up") as excinfo:
        ctr.parent_path("/var/lib/app")
    assert "a different otto invocation" in str(excinfo.value)


def test_parent_path_with_no_mounts_on_a_live_container_points_at_bring_up():
    ctr = _make_container()  # real id, empty mounts
    with pytest.raises(MountNotFoundError, match="no shared directories"):
        ctr.parent_path("/var/lib/app")


def test_no_shared_directories_message_names_the_lazily_resolved_placeholder_cause():
    """Branch 3's two named causes both go false the moment `_ensure_running`
    writes a lazily-resolved id back onto a placeholder -- the message must
    name that third cause too, or a reader is sent to check a compose file
    and hunt for a bring-up warning that was never going to exist."""
    ctr = _make_container()  # real id, empty mounts: indistinguishable from a
    # placeholder whose container_id was resolved lazily in THIS process.
    with pytest.raises(MountNotFoundError, match="resolved lazily"):
        ctr.parent_path("/var/lib/app")


@pytest.mark.asyncio
async def test_put_recursive_mkdirs_as_root_then_stages_per_level(tmp_path: Path):
    tree = tmp_path / "tree"
    (tree / "sub").mkdir(parents=True)
    (tree / "a.txt").write_text("a")
    (tree / "sub" / "b.txt").write_text("b")
    parent = _mock_parent()
    h = _make_container(parent, user="postgres")

    result = await h.put(tree, Path("/data"), recursive=True)

    assert result.status == Status.Success, result.msg
    cmds = [c.args[0] for c in parent.exec.call_args_list]
    mkdirs = [c for c in cmds if "mkdir -p /data/tree" in c]
    assert len(mkdirs) == 1, cmds
    assert " -u root " in mkdirs[0]
    assert "/data/tree/sub" in mkdirs[0]
    assert parent.put.await_count == 2
    assert result.value[tree].value[Path("sub/b.txt")].value == Path("/data/tree/sub/b.txt")


@pytest.mark.asyncio
async def test_get_recursive_lists_inside_the_container_then_stages_per_level(tmp_path: Path):
    parent = _mock_parent()
    listing = "t d\nd /data/sub\nf /data/a\nf /data/sub/b\nn 3\n"
    parent.exec = AsyncMock(
        side_effect=lambda cmd, **kw: _ok(cmd, listing if "printf" in cmd else "")
    )
    # The container's get re-keys the parent's per-file dict by STAGED path
    # (``stage / f.name``); a parent that answers keyed entries is what makes
    # the landed destination visible in the nested result.
    parent.get = AsyncMock(
        side_effect=lambda files, dest, **kw: Result(
            status=Status.Success,
            value={f: Result(status=Status.Success, value=dest / f.name) for f in files},
        )
    )
    h = _make_container(parent)

    result = await h.get(Path("/data"), tmp_path, recursive=True)

    assert result.status == Status.Success, result.msg
    assert (tmp_path / "data" / "sub").is_dir()
    assert parent.get.await_count == 2
    assert result.value[Path("/data")].value[Path("sub/b")].value == tmp_path / "data" / "sub" / "b"


def test_compose_container_probe_quotes_its_values_and_skips_one_offs():
    from otto.host.docker_host import compose_container_probe

    running = compose_container_probe("p x", "web")
    assert running == (
        "docker ps -q --filter label=com.docker.compose.project='p x' "
        "--filter label=com.docker.compose.service=web "
        "--filter label=com.docker.compose.oneoff=False"
    )
    assert compose_container_probe("p x", "web", all=True).startswith("docker ps -aq ")


@pytest.mark.asyncio
async def test_a_put_and_a_get_leave_no_staging_directory_for_the_container(tmp_path):
    """Each transfer removes everything it staged, leaving the shared root empty (#698).

    The parent's filesystem is real here: every staging ``mkdir``/``rm`` the
    container sends runs in a shell, rooted under *tmp_path* instead of
    ``/tmp``. A per-call ``<uuid>`` nested under a per-container directory
    left one empty ``<container_id>`` directory behind per container ever
    transferred to.
    """
    import subprocess

    stage_root = tmp_path / "otto-docker-vagrant" / ".transfer"
    parent = _mock_parent()

    def run_staging(cmd, *_, **__):
        if "docker cp" in cmd:
            return _ok()
        local = cmd.replace(str(CONTAINER_STAGING_ROOT), str(stage_root.parent))
        done = subprocess.run(["sh", "-c", local], capture_output=True, text=True, check=False)
        return _ok() if done.returncode == 0 else _fail(cmd, out=done.stderr)

    parent.exec.side_effect = run_staging
    h = _make_container(parent)
    f = tmp_path / "payload.bin"
    f.write_bytes(b"x")

    await h.put([f], Path("/srv/in"))
    await h.get(Path("/etc/os-release"), tmp_path / "out")

    assert stage_root.is_dir(), "the staging commands never ran against the fake parent"
    assert list(stage_root.iterdir()) == [], sorted(stage_root.rglob("*"))


# ---------------------------------------------------------------------------
# Where a container's exec is logged: on the container, under the stricter of two modes
# ---------------------------------------------------------------------------

_CMD_TOK = "cmdtok-ctr"
_OUT_TOK = "outtok-ctr"


def _wrapped_as(monkeypatch, container: DockerContainerHost, wire: str) -> None:
    """*container*'s ``docker exec`` line is *wire*, whatever it is asked to wrap."""

    async def fixed(cmd: str, *, interactive: bool = False, user: str | None = None) -> str:
        return wire

    monkeypatch.setattr(container, "_docker_exec", fixed)


async def _scripted_parent(replies: dict, *, log, hang: frozenset = frozenset({"c"})):
    """A real unix parent over scripted shells, standing at *log*, and its sessions."""
    from tests.unit.host.conftest import unix_host_over_scripted

    parent, sessions = await unix_host_over_scripted(
        "telnet", elevation="su", creds=SU_LOGIN, replies=replies, hang=hang
    )
    parent.log = log
    return parent, sessions


def _delegations(monkeypatch, parent) -> list:
    """The log context of every delegated call *parent* receives, in order."""
    seen: list = []
    original = parent._exec_delegated

    async def spy(cmd, **kwargs):
        seen.append(kwargs["exec_log"])
        return await original(cmd, **kwargs)

    monkeypatch.setattr(parent, "_exec_delegated", spy)
    return seen


def _container_trace(records: list, container) -> list:
    """*container*'s records as (level, kind, text, mode)."""
    from tests.conformance._log import output_record

    trace = []
    for r in records_of(records, container):
        found = output_record(r, container)
        if found is None:
            trace.append((r.levelno, "cmd", getattr(r, "relayed", None), r.log_mode))
        else:
            trace.append((r.levelno, "out", found.text, found.log_mode))
    return trace


def _wire_on(records: list, host, *tokens: str) -> list:
    """*host*'s records holding any of *tokens*, as (level, text, mode)."""
    return [
        (r.levelno, getattr(r, "relayed", None), getattr(r, "log_mode", None))
        for r in records_of(records, host)
        if any(t in f"{r.getMessage()} {getattr(r, 'relayed', '')}" for t in tokens)
    ]


@contextmanager
def _suppressed(which: str | None, container, parent):
    from otto.host.host import SuppressCommandOutput

    if which is None:
        yield
    elif which == "policy":
        with active_context(log_command_output=False):
            yield
    else:
        host = {"container": container, "parent": parent, "global": None}[which]
        with SuppressCommandOutput(host=host):
            yield


def _mode_rows():
    from otto.logger.mode import LogMode as M

    return [
        pytest.param(M.NORMAL, M.NEVER, M.NORMAL, None, M.NEVER, id="parent-never"),
        pytest.param(M.NEVER, M.NORMAL, M.NORMAL, None, M.NEVER, id="container-never"),
        pytest.param(M.NORMAL, M.QUIET, M.NORMAL, None, M.QUIET, id="parent-quiet"),
        pytest.param(M.QUIET, M.NORMAL, M.NORMAL, None, M.QUIET, id="container-quiet"),
        pytest.param(M.NORMAL, M.NORMAL, M.NEVER, None, M.NEVER, id="call-never"),
        pytest.param(M.NORMAL, M.NORMAL, M.NORMAL, None, M.NORMAL, id="all-normal"),
        pytest.param(M.NORMAL, M.NORMAL, M.NORMAL, "container", M.QUIET, id="suppress-container"),
        pytest.param(M.NORMAL, M.NORMAL, M.NORMAL, "parent", M.QUIET, id="suppress-parent"),
        pytest.param(M.NORMAL, M.NORMAL, M.NORMAL, "global", M.QUIET, id="suppress-global"),
        pytest.param(M.NORMAL, M.NORMAL, M.NORMAL, "policy", M.QUIET, id="policy-off"),
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize(("ctr_log", "parent_log", "call_log", "suppress", "mode"), _mode_rows())
async def test_a_container_exec_logs_on_the_container_under_the_stricter_mode(
    monkeypatch, ctr_log, parent_log, call_log, suppress, mode
):
    """The issued command and output are the container's; the parent keeps the DEBUG wrapper."""
    from otto.logger.mode import LogMode
    from tests.conformance._log import records_containing

    cmd = f"echo {_CMD_TOK}"
    wire = f"docker exec -i abc123def456 sh -c 'echo {_CMD_TOK}'"
    parent, _ = await _scripted_parent({wire: (_OUT_TOK, 0)}, log=parent_log)
    container = _make_container(parent)
    container.log = ctr_log
    _wrapped_as(monkeypatch, container, wire)
    delegated = _delegations(monkeypatch, parent)
    try:
        with (
            _suppressed(suppress, container, parent),
            all_records() as records,
            records_containing(_CMD_TOK) as cmd_seen,
            records_containing(_OUT_TOK) as out_seen,
        ):
            result = await container.exec(cmd, log=call_log)
    finally:
        await parent.close()
    assert (result.retcode, result.value, result.command) == (0, _OUT_TOK, cmd)
    assert [ctx.mode for ctx in delegated] == [mode], "the context carries the stricter mode"
    if mode is LogMode.NEVER:
        assert (cmd_seen, out_seen) == ([], [])
        return
    assert _container_trace(records, container) == [
        (logging.INFO, "cmd", cmd, mode),
        (logging.INFO, "out", _OUT_TOK, mode),
    ]
    assert _wire_on(records, parent, _CMD_TOK, _OUT_TOK) == [(logging.DEBUG, wire, mode)]


@pytest.mark.asyncio
async def test_a_shell_command_s_own_mode_decides_a_container_exec_s_log(monkeypatch):
    """A ``ShellCommand``'s ``log`` beats the call's, either way, on a container as on any host."""
    from otto.host.host import ShellCommand
    from otto.logger.mode import LogMode
    from tests.conformance._log import records_containing

    cmd = f"echo {_CMD_TOK}"
    wire = f"docker exec -i abc123def456 sh -c 'echo {_CMD_TOK}'"
    parent, _ = await _scripted_parent({wire: (_OUT_TOK, 0)}, log=LogMode.NORMAL)
    container = _make_container(parent)
    container.log = LogMode.NORMAL
    _wrapped_as(monkeypatch, container, wire)
    try:
        with records_containing(_CMD_TOK) as cmd_seen, records_containing(_OUT_TOK) as out_seen:
            hidden = await container.exec(
                ShellCommand(cmd=cmd, log=LogMode.NEVER), log=LogMode.NORMAL
            )
        with all_records() as records:
            shown = await container.exec(
                ShellCommand(cmd=cmd, log=LogMode.NORMAL), log=LogMode.NEVER
            )
    finally:
        await parent.close()
    assert (hidden.value, shown.value) == (_OUT_TOK, _OUT_TOK)
    assert (cmd_seen, out_seen) == ([], [])
    assert _container_trace(records, container) == [
        (logging.INFO, "cmd", cmd, LogMode.NORMAL),
        (logging.INFO, "out", _OUT_TOK, LogMode.NORMAL),
    ]


@pytest.mark.asyncio
async def test_a_parent_overriding_run_exec_with_its_signature_forwards_the_context(
    tmp_path,
):
    """An override that hands its ``exec_input`` on to the stock dispatch logs on the container."""
    from otto.host.unix_host import UnixHost
    from otto.logger.mode import LogMode

    calls = 0

    class _ForwardingParent(UnixHost):
        async def _run_exec(self, *, cmd, timeout, log, user, expects, exec_input, plain):
            nonlocal calls
            calls += 1
            return await super()._run_exec(
                cmd=cmd,
                timeout=timeout,
                log=log,
                user=user,
                expects=expects,
                exec_input=exec_input,
                plain=plain,
            )

    parent, dial = ssh_parent_over_shells(
        _ForwardingParent, tmp_path, {"wire-old": f"echo {_OUT_TOK}"}
    )
    container = _make_container(parent)
    container.log = LogMode.NORMAL
    wired = patch.object(DockerContainerHost, "_docker_exec", AsyncMock(return_value="wire-old"))
    try:
        with wired, all_records() as records:
            result = await container.exec("echo old")
    finally:
        await close_ssh_parent(parent, dial)
    assert (result.retcode, result.value, calls) == (0, _OUT_TOK, 1)
    assert _container_trace(records, container) == [
        (logging.INFO, "cmd", "echo old", LogMode.NORMAL),
        (logging.INFO, "out", _OUT_TOK, LogMode.NORMAL),
    ]
    assert _wire_on(records, parent, "wire-old", _OUT_TOK) == [
        (logging.DEBUG, "wire-old", LogMode.NORMAL)
    ]


@pytest.mark.asyncio
async def test_a_structural_parent_keeps_today_s_public_exec():
    """A parent that is no ``BaseHost`` is handed its own ``exec``, as before: no context."""
    from otto.logger.mode import LogMode

    parent = MagicMock(spec=["exec", "id", "name", "term", "log"])
    parent.id = parent.name = "test3"
    parent.term = "ssh"
    parent.exec = AsyncMock(return_value=_ok(out="hi"))
    container = _make_container(parent)
    container.log = LogMode.QUIET
    from tests.conformance._log import records_containing

    wired = patch.object(DockerContainerHost, "_docker_exec", AsyncMock(return_value="wire-s"))
    with wired, records_containing("echo hi") as cmd_seen, records_containing("hi") as out_seen:
        result = await container.exec("echo hi", expects=[("p", "r")], timeout=7.0)
    assert result.value == "hi"
    assert (cmd_seen, out_seen) == ([], []), (
        "the container logs nothing: the lines are the parent's"
    )
    parent.exec.assert_awaited_once_with(
        "wire-s", timeout=7.0, log=container._effective_log(LogMode.NORMAL), expects=[("p", "r")]
    )


@pytest.mark.asyncio
async def test_a_parent_s_exec_override_that_refuses_refuses_its_containers_commands(tmp_path):
    """A project ``exec`` override keeps running for the commands of the containers on it."""
    from otto.host.unix_host import UnixHost

    class _Refusing(UnixHost):
        async def exec(self, cmd, **kwargs):
            raise PermissionError(f"refused: {cmd}")

    parent, dial = ssh_parent_over_shells(_Refusing, tmp_path)
    container = _make_container(parent)
    wired = patch.object(DockerContainerHost, "_docker_exec", AsyncMock(return_value="wire-r"))
    with wired, pytest.raises(PermissionError, match="refused: wire-r"):
        await container.exec("echo refused")
    dial.assert_not_awaited()


@pytest.mark.asyncio
async def test_a_parent_s_exec_override_that_audits_sees_the_wrapper_and_logs_it_as_its_own(
    tmp_path,
):
    """An override that records and calls ``super().exec`` sees the wrapper; the lines are its."""
    from otto.host.unix_host import UnixHost
    from otto.logger.mode import LogMode

    seen: list = []

    class _Auditing(UnixHost):
        async def exec(self, cmd, **kwargs):
            seen.append(cmd)
            return await super().exec(cmd, **kwargs)

    # The override takes the public default, a terminal: a login shell, as a server's pty shell is.
    parent, dial = ssh_parent_over_shells(
        _Auditing, tmp_path, {"wire-au": f"echo {_OUT_TOK}"}, login=True
    )
    container = _make_container(parent)
    container.log = LogMode.NORMAL
    wired = patch.object(DockerContainerHost, "_docker_exec", AsyncMock(return_value="wire-au"))
    try:
        with wired, all_records() as records:
            result = await container.exec("echo audited")
    finally:
        await close_ssh_parent(parent, dial)
    assert (result.value, result.command, seen) == (_OUT_TOK, "echo audited", ["wire-au"])
    assert _container_trace(records, container) == []
    assert _container_trace(records, parent) == [
        (logging.INFO, "cmd", "wire-au", LogMode.NORMAL),
        (logging.INFO, "out", _OUT_TOK, LogMode.NORMAL),
    ]


def test_a_container_whose_parent_is_a_container_is_refused_when_built():
    """A container is a leaf: building one on another container raises, naming both."""
    middle = _make_container(_mock_parent(), container_id="bbb")
    with pytest.raises(TypeError, match=f"{middle.name} is itself a container"):
        _make_container(middle, container_id="aaa")


def test_a_container_cannot_be_reparented_onto_a_container():
    """``parent`` is a plain field: assigning a container to it, itself included, raises."""
    parent = _mock_parent()
    first = _make_container(parent, container_id="aaa")
    second = _make_container(parent, container_id="bbb")
    with pytest.raises(TypeError, match="is itself a container"):
        first.parent = first
    with pytest.raises(TypeError, match=f"{second.name} is itself a container"):
        first.parent = second
    assert first.parent is parent, "a refused assignment leaves the parent as it was"


@pytest.mark.asyncio
async def test_a_cancelled_delegated_exec_keeps_its_lines_on_the_container(tmp_path):
    """Lines read before a cancel are the container's; the parent keeps only the wrapper."""
    from otto.host.unix_host import UnixHost
    from otto.logger.mode import LogMode
    from tests.conformance._log import _watching_records

    # The second line never comes: the command is still running when it is cancelled.
    parent, dial = ssh_parent_over_shells(
        UnixHost, tmp_path, {"wire-cx": "echo outtok-x; exec sleep 3600; echo outtok-y"}
    )
    container = _make_container(parent)
    container.log = LogMode.NORMAL
    first_line = asyncio.Event()

    def line_seen(record: logging.LogRecord) -> None:
        if "outtok-x" in str(getattr(record, "relayed", "")):
            first_line.set()

    wired = patch.object(DockerContainerHost, "_docker_exec", AsyncMock(return_value="wire-cx"))
    with wired, all_records() as records, _watching_records(line_seen, level=logging.DEBUG):
        call = asyncio.create_task(container.exec("echo cancelled"))
        try:
            await asyncio.wait_for(first_line.wait(), timeout=10)
            call.cancel()
            with pytest.raises(asyncio.CancelledError):
                await call
        finally:
            call.cancel()
            await asyncio.gather(call, return_exceptions=True)
            await close_ssh_parent(parent, dial)
    assert _container_trace(records, container) == [
        (logging.INFO, "cmd", "echo cancelled", LogMode.NORMAL),
        (logging.INFO, "out", "outtok-x", LogMode.NORMAL),
    ]
    assert _wire_on(records, parent, "wire-cx", "outtok") == [
        (logging.DEBUG, "wire-cx", LogMode.NORMAL)
    ]
    owned = [*records_of(records, container), *records_of(records, parent)]
    stray = [
        r.getMessage()
        for r in records
        if "outtok" in f"{r.getMessage()} {getattr(r, 'relayed', '')}" and r not in owned
    ]
    assert stray == []


@pytest.mark.asyncio
async def test_a_parent_that_runs_and_logs_a_command_by_its_own_means_keeps_the_lines(tmp_path):
    """A ``_run_exec`` that ignores the call's context logs the command as its own host's.

    A pin of the documented contract for a host class that runs a command
    by its own means, not of something this family does: the container gets
    no record, and the parent logs the wrapper as any command of its own.
    """
    from otto.host.unix_host import UnixHost
    from otto.logger.mode import LogMode

    class _OwnMeans(UnixHost):
        async def _run_exec(self, *, cmd, timeout, log, user, expects, exec_input, plain):
            self._log_command(cmd, log)
            self._log_output(_OUT_TOK, log)
            return CommandResult(
                status=Status.Success,
                value=_OUT_TOK,
                command=cmd,
                retcode=0,
            )

    parent, dial = ssh_parent_over_shells(_OwnMeans, tmp_path)
    container = _make_container(parent)
    container.log = LogMode.NORMAL
    wired = patch.object(DockerContainerHost, "_docker_exec", AsyncMock(return_value="wire-own"))
    with wired, all_records() as records:
        result = await container.exec("echo own")
    assert (result.value, result.command) == (_OUT_TOK, "echo own")
    dial.assert_not_awaited()
    assert _container_trace(records, container) == []
    assert _container_trace(records, parent) == [
        (logging.DEBUG, "cmd", "wire-own", LogMode.NORMAL),
        (logging.INFO, "cmd", "wire-own", LogMode.NORMAL),
        (logging.INFO, "out", _OUT_TOK, LogMode.NORMAL),
    ]


def _session_made(parent, sessions: list, count: int) -> "list[asyncio.Event]":
    """One event per pooled session *parent* will open, set as its scripted shell is made."""
    made = [asyncio.Event() for _ in range(count)]
    mgr = parent._live_session_mgr()
    factory = mgr._session_factory

    def counted(**kw: object):
        session = factory(**kw)
        made[len(sessions) - 1].set()
        return session

    mgr._session_factory = counted
    return made


async def _began(made: "list[asyncio.Event]", sessions: list, index: int) -> None:
    """Return once session *index* is made and its command has begun."""
    await made[index].wait()
    await sessions[index].began.wait()


@pytest.mark.asyncio
async def test_two_containers_on_one_parent_keep_their_own_lines_and_modes(monkeypatch):
    """Concurrent delegated calls cross nothing; the reused pooled session logs as the parent's."""
    from otto.logger.mode import LogMode
    from tests.unit.host._session_feed import _completion

    wire_n = "docker exec -i nnn sh -c 'echo cmdtok-n'"
    wire_q = "docker exec -i qqq sh -c 'echo cmdtok-q'"
    parent, sessions = await _scripted_parent(
        {"echo plain": ("plain-out", 0)}, log=LogMode.NORMAL, hang=frozenset({wire_n, wire_q})
    )
    made = _session_made(parent, sessions, 3)
    shown = _make_container(parent, container_id="nnn")
    shown.log = LogMode.NORMAL
    hidden = _make_container(parent, container_id="qqq")
    hidden.log = LogMode.NEVER
    _wrapped_as(monkeypatch, shown, wire_n)
    _wrapped_as(monkeypatch, hidden, wire_q)
    pooled = [("never-printed", "")]  # a prompt to answer: the pooled shell
    calls: list = []
    try:
        with all_records() as records:
            first = asyncio.ensure_future(shown.exec("echo cmdtok-n", expects=pooled))
            calls.append(first)
            await asyncio.wait_for(_began(made, sessions, 0), timeout=10)
            second = asyncio.ensure_future(hidden.exec("echo cmdtok-q", expects=pooled))
            calls.append(second)
            await asyncio.wait_for(_began(made, sessions, 1), timeout=10)
            for session, out in ((sessions[0], "outtok-n"), (sessions[1], "outtok-q")):
                session.feed(
                    _completion(
                        session._frame, session._markers, session.written_frames[-1], out, 0
                    )
                )
            results = await asyncio.gather(first, second)
            plain = await parent.exec("echo plain", expects=[("never-printed", "")])
    finally:
        # A failed wait above must not leave a command running into the close.
        for call in calls:
            call.cancel()
        await asyncio.gather(*calls, return_exceptions=True)
        await parent.close()
    # Each ran on a pooled session of the parent's own, one each.
    assert [r.value for r in results] == ["outtok-n", "outtok-q"]
    assert len(sessions) == 2, "the later ordinary command reused a pooled session"
    leaked = [
        r.getMessage()
        for r in records
        if any(
            t in f"{r.getMessage()} {getattr(r, 'relayed', '')}" for t in ("cmdtok-q", "outtok-q")
        )
    ]
    assert leaked == []
    assert _container_trace(records, shown) == [
        (logging.INFO, "cmd", "echo cmdtok-n", LogMode.NORMAL),
        (logging.INFO, "out", "outtok-n", LogMode.NORMAL),
    ]
    assert _container_trace(records, hidden) == []
    assert _wire_on(records, parent, "cmdtok-n", "outtok-n") == [
        (logging.DEBUG, wire_n, LogMode.NORMAL)
    ]
    assert plain.value == "plain-out"
    assert _container_trace(records, parent)[-2:] == [
        (logging.INFO, "cmd", "echo plain", LogMode.NORMAL),
        (logging.INFO, "out", "plain-out", LogMode.NORMAL),
    ]


@pytest.mark.asyncio
async def test_direct_parent_calls_carry_no_context_and_own_helpers_do(monkeypatch, tmp_path):
    """Probe, staging, ``docker cp``, unstage and root chown/chmod are the parent's own calls."""
    from otto.logger.mode import LogMode
    from tests.unit.host._ssh_parent import ssh_parent_host

    parent = ssh_parent_host()
    public: list = []
    delegated: list = []

    # Both entries meet in ``_exec_in_turn``; the parent's ``exec`` stays otto's
    # own, so the container's hop takes the delegated entry, which alone
    # carries an ``exec_log``.
    async def exec_in_turn(cmd, **kwargs):
        if kwargs.get("exec_log") is not None:
            delegated.append((cmd, kwargs["exec_log"]))
            return _ok(cmd)
        public.append((cmd, kwargs.get("log")))
        return _ok(cmd, out="abc123\n" if cmd.startswith("docker ps") else "")

    staged = _mock_parent()
    monkeypatch.setattr(parent, "_exec_in_turn", exec_in_turn)
    monkeypatch.setattr(parent, "put", staged.put)
    monkeypatch.setattr(parent, "get", staged.get)
    container = _make_container(parent, container_id="")
    container.log = LogMode.NORMAL

    assert await container.is_running() is True
    assert [(c.split()[:2], log) for c, log in public] == [(["docker", "ps"], LogMode.QUIET)]
    assert delegated == []

    public.clear()
    f = tmp_path / "a.bin"
    f.write_bytes(b"x")
    (s1, _), (s2, _) = (
        _sm(await container.put([f], Path("/data"), mode="644")),
        _sm(await container.get(Path("/etc/os-release"), tmp_path / "out")),
    )
    assert (s1, s2) == (Status.Success, Status.Success)
    publics = [cmd for cmd, _ in public]
    for step in ("mkdir -p", "docker cp", "rm -rf"):
        assert any(step in c for c in publics), (step, publics)
    staged.put.assert_awaited_once()
    staged.get.assert_awaited_once()
    # The no-user chmod goes through the container's own exec: delegated, its log the container's.
    assert [c for c, _ in delegated] == ["docker exec -i abc123 sh -c 'chmod 644 -- /data/a.bin'"]
    assert delegated[0][1].log_command == container._log_command

    public.clear()
    delegated.clear()
    rooted = _make_container(parent, user="postgres")
    _sm(await rooted.put([f], Path("/data"), mode="644"))
    rooted_steps = [c for c, _ in public if "chown" in c or "chmod" in c]
    in_root = f"docker exec -i -u root {rooted.container_id} sh -c"
    assert rooted_steps == [
        f"{in_root} 'chown postgres /data/a.bin'",
        f"{in_root} 'chmod 644 -- /data/a.bin'",
    ], public
    assert delegated == [], "root chown and chmod are the parent's own calls"

    await container.exec("echo own")
    assert [c for c, _ in delegated] == ["docker exec -i abc123 sh -c 'echo own'"]
    assert delegated[-1][1].command == "echo own"


@pytest.mark.asyncio
async def test_exec_dry_run_reaches_no_delegated_entry(monkeypatch):
    from tests.unit.host._ssh_parent import ssh_parent_host

    parent = ssh_parent_host()
    entry = AsyncMock()
    monkeypatch.setattr(parent, "_exec_delegated", entry)
    monkeypatch.setattr(parent, "exec", AsyncMock())
    container = _make_container(parent)
    with active_context(dry_run=True):
        result = await container.exec("echo hi")
    assert result.status == Status.NotRun
    entry.assert_not_awaited()
    parent.exec.assert_not_awaited()
