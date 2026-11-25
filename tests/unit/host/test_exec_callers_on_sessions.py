"""otto's own ``exec()`` callers, with real payloads, on the session ``exec()`` uses.

An unset ``exec()`` on ssh borrows a pooled session without a terminal, whose
shell is a LOGIN shell: its profile has been read. Every host here has a
profile that prints a line and sets a variable, and each of otto's internal
callers (the file verbs, the shell and netcat transfers, the coverage sweep,
a container's delegation, the daemon launcher) is driven through the public
method otto's code calls, over real local shells behind an asyncssh surface
(:func:`~tests.unit.host.conftest.piped`). The printed line must reach no
result and no record.
"""

import asyncio
import contextlib
import itertools
import os
import re
import shutil
import signal
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest
import pytest_asyncio

import otto.host.transfer.nc as nc_mod
from otto.host import cov_sweep
from otto.host.daemon import launch_command
from otto.host.docker_host import DockerContainerHost
from otto.host.process_stop import SESSION_MARK
from otto.host.transfer.shell import _SHELL_CHUNK_BYTES
from otto.host.unix_host import UnixHost
from otto.logger.mode import LogMode
from otto.utils import Status, wait_for_async
from tests.unit.host._exec_log import all_records, close_ssh_parent, ssh_parent_over_shells
from tests.unit.host.test_cov_sweep import _cov, _tree
from tests.unit.host.test_terminal_switch import _EitherConnection, _pid_of, _running, _unix

BANNER = "PROFILE-BANNER-7a"
"""The line every host's login profile prints."""

PROFILE = f"echo {BANNER}\nexport FROM_PROFILE=yes\n"
"""A profile that prints, sets a variable, and is otherwise harmless."""


def _no_banner(*texts: object) -> None:
    for text in texts:
        assert BANNER not in str(text), f"the profile's line reached {text!r:.200}"


@contextlib.asynccontextmanager
async def _profiled(home: Path, **fields: Any):
    """An ssh host over real shells in *home*, whose ``.profile`` is :data:`PROFILE`."""
    (home / ".profile").write_text(PROFILE)
    host = _unix(**fields)
    conn = _EitherConnection(home)
    host._connections.ssh = AsyncMock(return_value=conn)  # ty: ignore[invalid-assignment]
    try:
        yield host, conn
    finally:
        await host.close()
        await conn.reap()


@pytest_asyncio.fixture
async def profiled(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    async with _profiled(home) as pair:
        yield pair


def _live_sessions(host: UnixHost) -> list[Any]:
    return [s for s in host._session_mgr._named_sessions.values() if s.alive]


# ---------------------------------------------------------------------------
# 1. File operations
# ---------------------------------------------------------------------------


def _every_code_point_payload(size: int) -> str:
    """A ``str`` of about *size* UTF-8 bytes holding every class of character ``data`` can carry.

    ``write_file`` takes a ``str`` and ``read_file`` returns one, through
    UTF-8: what the API carries is any string UTF-8 encodes, which is every
    code point but a lone surrogate. So: every C0 control (NUL, CR, ESC,
    DEL included), all of Latin-1, two- and three-byte forms, and astral
    four-byte ones, then the last code point of the BMP and of Unicode.
    """
    block = "".join(chr(c) for c in range(0x800))
    block += "".join(chr(c) for c in range(0xE000, 0xE100))
    block += "\U0001f600\U00010000\U0010ffff" + chr(0xFFFF)
    return block * (size // len(block.encode()) + 1)


_TEXT = ("x" * 9000 + "\r\n") * 10 + "naïve — 日本語 é\U0001f680 end, no newline"


@pytest.mark.asyncio
async def test_write_file_and_read_file_carry_every_character_and_a_text_payload(
    profiled, tmp_path
):
    host, _conn = profiled
    target = tmp_path / "payload"
    payload = _every_code_point_payload(200_000)
    assert len(payload.encode()) >= 200_000
    with all_records() as records:
        for data in (payload, _TEXT):
            assert (await host.write_file(target, data=data)).status is Status.Success
            assert target.read_bytes() == data.encode()
            assert await host.read_file(target) == data
    _no_banner(*(r.getMessage() for r in records))


@pytest.mark.asyncio
async def test_exists_ls_and_glob_answer_only_the_tree_cold_and_warm(profiled, tmp_path):
    host, _conn = profiled
    root = _tree(tmp_path / "tree", "a file.txt", "sub dir/b c.txt", "plain")
    with all_records() as records:
        for _turn in ("cold", "warm"):
            assert await host.exists(root / "a file.txt") is True
            assert await host.exists(root / "absent") is False
            assert await host.ls(root) == ["a file.txt", "plain", "sub dir"]
            assert await host.glob(f"{root}/*") == [
                f"{root}/a file.txt",
                f"{root}/plain",
                f"{root}/sub dir",
            ]
    _no_banner(*(r.getMessage() for r in records))
    assert host._session_mgr._exec_pool_count == 1, "the warm calls reused the cold session"
    assert (await host.exec("echo $FROM_PROFILE")).value == "yes", "the profile was read"


# ---------------------------------------------------------------------------
# 2. The shell transfer's chunk line, and the recursive walk
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_the_shell_transfer_sends_a_full_chunk_unbounded_and_walks_a_spaced_tree(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    async with _profiled(home) as (host, _conn):
        # ssh's menu does not offer the shell transfer; set past it so the
        # backend is built with this ssh host's own line-room callback.
        host.transfer = "shell"
        host._file_transfer = host._build_file_transfer()
        assert host._exec_line_room(user=None, cmd="printf '%s' '' | base64 -d >> /x") is None
        src = tmp_path / "chunk.bin"
        src.write_bytes(bytes(range(256)) * (_SHELL_CHUNK_BYTES // 256))
        assert src.stat().st_size == _SHELL_CHUNK_BYTES
        land = tmp_path / "land"
        land.mkdir()
        put = await host.put(src, land, show_progress=False)
        assert put.status is Status.Success, put.msg
        assert (land / "chunk.bin").read_bytes() == src.read_bytes()

        tree = _tree(tmp_path / "spaced tree", "top file", "a dir/inner file", dirs=("empty dir",))
        (tree / "top file").write_text("top")
        (tree / "a dir" / "inner file").write_text("inner")
        back = tmp_path / "back"
        back.mkdir()
        got = await host.get(tree, back, recursive=True, show_progress=False)
        assert got.status is Status.Success, got.msg
        assert (back / "spaced tree" / "top file").read_text() == "top"
        assert (back / "spaced tree" / "a dir" / "inner file").read_text() == "inner"
        assert (back / "spaced tree" / "empty dir").is_dir()
        _no_banner(*(p.name for p in back.rglob("*")))


# ---------------------------------------------------------------------------
# 3. The coverage sweep
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_the_coverage_probes_parse_their_answers_under_a_printing_profile(profiled, tmp_path):
    host, _conn = profiled
    d = _tree(tmp_path / "cov dir", "a.gcda", "sub/b.gcda", "keep.txt", dirs=("empty",))
    app, gone = _cov("app", d), _cov("gone", tmp_path / "never")
    assert await cov_sweep.probe_presence(host, [app, gone]) == frozenset({"app"})
    left = await cov_sweep.coverage_left(host, [app, gone])
    assert {k: sorted(v) for k, v in left.items()} == {
        "app": sorted([str(d / "a.gcda"), str(d / "sub" / "b.gcda"), str(d / "empty")])
    }


# ---------------------------------------------------------------------------
# 4. The netcat transfer's remote listener
# ---------------------------------------------------------------------------


def _nc_listeners(port: int) -> list[int]:
    """The pids of the running ``nc -l -p <port>`` processes: this test's listener on *port*.

    Scoped by the port the transfer reserved, so a listener another test or
    session runs on this machine is never in the set (nor signalled).
    """
    found = []
    for entry in Path("/proc").iterdir():
        if not entry.name.isdigit():
            continue
        try:
            argv = (entry / "cmdline").read_bytes().split(b"\0")
        except OSError:
            continue
        named = argv and Path(os.fsdecode(argv[0])).name in ("nc", "netcat")
        ours = (b"-p", str(port).encode()) in itertools.pairwise(argv)
        if named and b"-l" in argv and ours and _running(int(entry.name)):
            found.append(int(entry.name))
    return found


def _listener_ports(record: "list[int]") -> Any:
    """A ``_spawn_listener`` that records the port of each listener it starts in *record*."""
    real = nc_mod.NcFileTransfer._spawn_listener

    async def spawn(self: Any, cmd: str) -> Any:
        found = re.search(r"nc -l -p (\d+)", cmd)
        assert found, cmd
        record.append(int(found.group(1)))
        return await real(self, cmd)

    return spawn


@pytest.mark.asyncio
async def test_the_nc_listener_is_held_on_its_own_session_and_the_put_completes(tmp_path):
    assert shutil.which("nc"), "the listener needs this machine's nc"
    home = tmp_path / "home"
    home.mkdir()
    async with _profiled(home, transfer="nc", ip="127.0.0.1") as (host, conn):
        held_while_checking: list[int] = []
        real_wait = nc_mod.NcFileTransfer._wait_for_remote_listener

        async def watched_wait(self, *args: Any, **kwargs: Any) -> Any:
            out = await real_wait(self, *args, **kwargs)
            held_while_checking.append(len(_live_sessions(host)))
            return out

        src = tmp_path / "payload.bin"
        src.write_bytes(os.urandom(300_000))
        land = tmp_path / "land"
        land.mkdir()
        with patch.object(nc_mod.NcFileTransfer, "_wait_for_remote_listener", watched_wait):
            put = await host.put(src, land, show_progress=False)
        assert put.status is Status.Success, put.msg
        assert (land / "payload.bin").read_bytes() == src.read_bytes()
        assert held_while_checking
        assert held_while_checking[0] >= 2, "the readiness check ran on the listener's own session"
        assert len(conn.processes) >= 2


@pytest.mark.asyncio
async def test_a_cancelled_nc_put_ends_its_listener_and_closes_its_session(tmp_path):
    assert shutil.which("nc"), "the listener needs this machine's nc"
    home = tmp_path / "home"
    home.mkdir()
    ports: list[int] = []
    listeners: set[int] = set()
    async with _profiled(home, transfer="nc", ip="127.0.0.1") as (host, _conn):
        parked = asyncio.Event()
        real_connect = nc_mod._connect_with_retry
        calls = 0

        async def park_the_sender(*args: Any, **kwargs: Any) -> Any:
            nonlocal calls
            calls += 1
            if calls == 1:
                parked.set()
                await asyncio.Event().wait()
            return await real_connect(*args, **kwargs)

        src = tmp_path / "payload.bin"
        src.write_bytes(b"x" * 4096)
        land = tmp_path / "land"
        land.mkdir()
        with (
            patch.object(nc_mod, "_connect_with_retry", park_the_sender),
            patch.object(nc_mod.NcFileTransfer, "_spawn_listener", _listener_ports(ports)),
        ):
            task = asyncio.ensure_future(host.put(src, land, show_progress=False))
            try:
                await asyncio.wait_for(parked.wait(), 30)
                (port,) = ports
                listeners = set(_nc_listeners(port))
                assert listeners, "the listener was not running when the sender connected"
                listener_sessions = _live_sessions(host)
            finally:
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await task
            assert task.cancelled()
        try:
            for pid in listeners:
                await wait_for_async(
                    lambda pid=pid: not _running(pid), 10.0, interval=0.01, on_timeout=f"{pid} runs"
                )
            idle = [s for pool in host._session_mgr._exec_pools.values() for s in pool]
            held = [s for s in listener_sessions if s not in idle]
            assert held, "no session held the listener"
            assert not [s for s in held if s.alive], "the listener's session was left open"
        finally:
            for pid in listeners:
                with contextlib.suppress(ProcessLookupError):
                    os.kill(pid, signal.SIGKILL)


# ---------------------------------------------------------------------------
# 5. A container over a session-backed parent
# ---------------------------------------------------------------------------

_DOCKER = """\
[ "$1" = exec ] || exit 99
shift
u=
while :; do
  case "$1" in
    -i|-it) shift;;
    -u) u=$2; shift 2;;
    *) break;;
  esac
done
shift
[ "$1" = sh ] && [ "$2" = -c ] || exit 98
printf 'as:%s\\n' "${u:-default}"
exec sh -c "$3"
"""
"""A ``docker`` stand-in: runs ``exec [-i] [-u U] <id> sh -c CMD`` here, after naming the user."""


class _AuditingParent(UnixHost):
    """A parent whose ``exec`` override is a project's: the container calls its public ``exec``."""

    async def exec(self, cmd, **kwargs):  # ty: ignore[invalid-method-override]
        return await super().exec(cmd, **kwargs)


@pytest.mark.parametrize("parent_cls", [UnixHost, _AuditingParent], ids=["delegated", "public"])
@pytest.mark.asyncio
async def test_a_container_over_a_session_backed_parent_runs_users_prompts_and_logs(
    tmp_path, parent_cls
):
    parent, dial = ssh_parent_over_shells(
        parent_cls, tmp_path, {"docker": _DOCKER}, profile=PROFILE, login=True
    )
    container = DockerContainerHost(
        parent=parent,
        container_id="abc123def456",
        project="project1",
        service="api",
        compose_project="otto-project1-vagrant",
    )
    container.log = LogMode.NORMAL
    try:
        with (
            patch.object(DockerContainerHost, "_ensure_running", AsyncMock(return_value=None)),
            all_records() as records,
        ):
            plain = await container.exec("echo $FROM_PROFILE inside")
            as_user = await container.exec("echo hi", user="app")
            answered = await container.exec(
                "printf 'Name? '; read n; echo \"hello $n\"", expects=[(r"Name\? ", "bob\n")]
            )
        modes = [asked.get("request_pty") for asked in dial.return_value.asked]
    finally:
        await close_ssh_parent(parent, dial)
    # The prompt leg took a terminal session (no request_pty=False), whose
    # login shell found the stand-in only on the PATH its profile sets.
    assert None in modes
    assert False in modes
    assert (plain.status, plain.value) == (Status.Success, "as:default\nyes inside")
    assert as_user.value == "as:app\nhi"
    assert answered.status is Status.Success, answered.value
    assert answered.value.splitlines()[-1].endswith("hello bob")
    for result in (plain, as_user, answered):
        _no_banner(result.value)
    _no_banner(*(r.getMessage() for r in records))
    mine = [r.getMessage() for r in records if getattr(r, "host", None) is container]
    parents = [r.getMessage() for r in records if getattr(r, "host", None) is parent]
    if parent_cls is UnixHost:
        assert "echo hi" in " ".join(mine), "the container logged its own command"
        assert not [m for m in parents if m == "echo hi"]
    else:
        assert any("docker exec" in m for m in parents), "a public exec logs the wrapper"


# ---------------------------------------------------------------------------
# 6. The daemon launcher: the launching session's close sends no hangup
# ---------------------------------------------------------------------------


async def _gone(pid: int) -> None:
    """The negative control: a close ends a child that neither detached nor carries the mark."""
    await wait_for_async(
        lambda: not _running(pid),
        10.0,
        interval=0.01,
        on_timeout=f"the close left {pid}, a child that did not detach",
    )


@pytest.mark.parametrize("closer", ["idle-limit", "host-close"])
@pytest.mark.asyncio
async def test_a_daemon_outlives_its_launching_session_being_closed(tmp_path, closer):
    """A no-terminal session's close reaches no daemon its exec launched.

    The launching exec holds its session until the test lets it go; with a
    second exec's session already idle, the ssh idle limit (one) closes the
    launching session when it returns. Or the host is closed.
    """
    home = tmp_path / "home"
    home.mkdir()
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    for tool in ("bash", "sh", "setsid", "sleep"):
        found = shutil.which(tool)
        assert found, f"the fallback launch needs {tool} on this machine's PATH"
        (bin_dir / tool).symlink_to(found)
    daemon, go, launched = tmp_path / "daemon", tmp_path / "go", tmp_path / "launched"
    sibling = tmp_path / "sibling"
    launch = launch_command(
        "otto-test:v1:keep", ["sh", "-c", f"echo $$ > {daemon}; exec sleep 600"]
    )
    held: "asyncio.Future[Any] | None" = None
    try:
        async with _profiled(home) as (host, _conn):
            held = asyncio.ensure_future(
                host.exec(
                    f"echo $OTTO_SESSION; PATH={bin_dir} {launch}; "
                    f"env -u {SESSION_MARK} sh -c 'echo $$ > {sibling}; exec sleep 600' "
                    f">/dev/null 2>&1 </dev/null & touch {launched}; "
                    f"while [ ! -e {go} ]; do sleep 0.05; done",
                    timeout=60,
                )
            )
            pid = await _pid_of(daemon)
            undetached = await _pid_of(sibling)
            await wait_for_async(launched.exists, 10.0, interval=0.01, on_timeout="never launched")
            environ = Path(f"/proc/{pid}/environ").read_bytes().split(b"\0")
            assert not [e for e in environ if e.startswith(f"{SESSION_MARK}=".encode())]
            mgr = host._session_mgr
            if closer == "idle-limit":
                assert (await host.exec("true")).status is Status.Success
                (launcher,) = [
                    s for s in _live_sessions(host) if s not in mgr._exec_pools[("user", True)]
                ]
                go.touch()
                result = await held
                assert result.status is Status.Success, result.value
                assert not launcher.alive, "the idle limit kept the launching session"
                await _gone(undetached)
            else:
                go.touch()
                assert (await held).status is Status.Success
        await _gone(undetached)
        assert _running(pid), "closing the launching session killed the daemon"
    finally:
        go.touch()
        if held is not None and not held.done():
            held.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await held
        for path in (daemon, sibling):
            with contextlib.suppress(ProcessLookupError, ValueError, FileNotFoundError):
                os.kill(int(path.read_text()), signal.SIGKILL)
