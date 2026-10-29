"""The private docker staging root on a parent: its name, its creation, and when it is refused.

A parent here is a real :class:`~otto.host.unix_host.UnixHost` whose ``exec``
answers otto's one ownership probe the way the parent's shell would
(:class:`~tests._fixtures.private_root.ParentPaths`): the ``mkdir -m 0700``
makes the root only when nothing is there, and each path then reads back as
``stat -c '%F %u %a'`` prints it. What the probe prints on a real
system, GNU or BusyBox, is pinned at the bottom against the command itself.
"""

import shlex
import shutil
import subprocess
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from otto.host.docker_host import (
    DockerContainerHost,
    _ownership_command,
    _read_ownership,
    ensure_private_root,
    parent_staging_root,
    probe_ownership,
    staging_root,
)
from otto.host.element import Element
from otto.host.errors import HostUnreachableError, StagingRefusedError
from otto.host.login_proxy import Cred
from otto.host.unix_host import UnixHost
from otto.result import CommandNotRunError, CommandResult, NotRunResult, Result
from otto.utils import Status
from tests._fixtures.private_root import SESSION_UID, ParentPaths

ROOT = Path("/tmp/otto-docker-alice")
TMP = Path("/tmp")


def _parent(creds: "list[Cred] | None" = None, **kw) -> UnixHost:
    return UnixHost(
        ip="10.10.200.13",
        element=Element("test3"),
        creds=creds if creds is not None else [Cred(login="alice", password="pw")],
        docker_capable=True,
        **kw,
    )


class _FakeParentShell(ParentPaths):
    """:class:`~tests._fixtures.private_root.ParentPaths`, recording every command it is sent."""

    def __init__(self, paths: "dict[Path, str]", uid: int = SESSION_UID) -> None:
        super().__init__(paths, uid)
        self.commands: list[str] = []

    async def exec(self, command: str, *_a, **_kw) -> CommandResult:
        self.commands.append(command)
        answer = self.answer(command)
        assert answer is not None, f"not the ownership probe: {command}"
        return answer


def _wired(shell: _FakeParentShell, **kw) -> UnixHost:
    parent = _parent(**kw)
    parent.exec = AsyncMock(side_effect=shell.exec)  # type: ignore[method-assign]
    return parent


# ---------------------------------------------------------------------------
# The root's name: pure, from the parent's login
# ---------------------------------------------------------------------------


def test_the_root_is_named_for_the_login():
    assert staging_root("alice") == ROOT
    assert parent_staging_root(_parent()) == ROOT


@pytest.mark.parametrize("login", ["", "a/b", "../x", "a b", "a\nb", "a\x00b"])
def test_a_login_that_is_not_one_path_segment_is_refused(login):
    with pytest.raises(StagingRefusedError, match="cannot name a docker staging directory"):
        staging_root(login)


def test_a_login_proxied_parent_is_refused_naming_both_accounts():
    """Commands would run as `docker` while uploads land as `alice` in a root only one can use."""
    parent = _parent(
        [
            Cred(login="docker", password="pw", proxy="su", via="alice"),
            Cred(login="alice", password="pw"),
        ]
    )
    with pytest.raises(StagingRefusedError) as refused:
        parent_staging_root(parent)
    assert "'docker'" in str(refused.value)
    assert "'alice'" in str(refused.value)
    assert "direct cred" in str(refused.value)


def test_an_ftp_upload_cred_for_another_account_is_refused():
    parent = _parent(
        [
            Cred(login="alice", password="pw", protocols=["ssh"]),
            Cred(login="ftpuser", password="pw", protocols=["ftp"]),
        ],
        transfer="ftp",
        valid_transfers=["ftp"],
    )
    with pytest.raises(StagingRefusedError, match="'ftpuser'"):
        parent_staging_root(parent)


def test_a_parent_with_no_cred_has_no_root():
    with pytest.raises(StagingRefusedError, match="no cred names the account"):
        parent_staging_root(_parent([]))


@pytest.mark.asyncio
async def test_a_refused_parent_is_refused_before_anything_is_run():
    shell = _FakeParentShell({})
    parent = _wired(
        shell,
        creds=[
            Cred(login="docker", password="pw", proxy="su", via="alice"),
            Cred(login="alice", password="pw"),
        ],
    )
    with pytest.raises(StagingRefusedError):
        await ensure_private_root(parent)
    assert shell.commands == []


# ---------------------------------------------------------------------------
# Creating or verifying the root
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_missing_root_is_created_private_in_one_command():
    shell = _FakeParentShell({})
    parent = _wired(shell)

    assert await ensure_private_root(parent) == ROOT

    (command,) = shell.commands
    assert command.startswith(f"mkdir -m 0700 {ROOT} 2>/dev/null; id -u; ")
    assert shell.paths[ROOT] == f"directory {SESSION_UID} 700"


@pytest.mark.asyncio
async def test_a_root_an_earlier_run_made_is_accepted_and_not_recreated():
    """An owned 0700 root from an earlier run: no refusal, nothing removed or remade."""
    shell = _FakeParentShell({ROOT: f"directory {SESSION_UID} 700"})
    parent = _wired(shell)

    assert await ensure_private_root(parent) == ROOT
    assert await ensure_private_root(parent) == ROOT

    assert len(shell.commands) == 2
    assert not any("rm " in c or "chmod" in c or "chown" in c for c in shell.commands)


@pytest.mark.parametrize(
    ("planted", "why"),
    [
        ("directory 1001 700", "owned by UID 1001"),
        (f"directory {SESSION_UID} 755", "mode is 0755"),
        (f"directory {SESSION_UID} 2700", "mode is 2700"),
        (f"symbolic link {SESSION_UID} 777", "symbolic link"),
        (f"regular file {SESSION_UID} 700", "regular file"),
        ("symbolic link 1001 777", "symbolic link"),
    ],
)
@pytest.mark.asyncio
async def test_a_root_otto_cannot_trust_is_refused_naming_it(planted, why):
    """A pre-created root another account owns, a widened one, or a link: never adopted."""
    shell = _FakeParentShell({ROOT: planted})
    parent = _wired(shell)

    with pytest.raises(StagingRefusedError) as refused:
        await ensure_private_root(parent)

    assert str(ROOT) in str(refused.value)
    assert why in str(refused.value)
    assert "never repairs or adopts" in str(refused.value)
    assert shell.paths[ROOT] == planted, "the planted path was changed"
    assert len(shell.commands) == 1, "nothing may run after the refusal"


@pytest.mark.parametrize(
    ("tmp", "why"),
    [
        ("directory 0 777", "does not have the sticky bit set"),
        ("directory 1001 1777", "owned by UID 1001, not by root"),
        ("symbolic link 0 777", "is a symbolic link"),
        ("unreadable", "cannot be read"),
    ],
)
@pytest.mark.asyncio
async def test_a_tmp_that_is_not_root_owned_and_sticky_is_refused(tmp, why):
    shell = _FakeParentShell({})
    shell.paths[TMP] = tmp
    with pytest.raises(StagingRefusedError) as refused:
        await ensure_private_root(_wired(shell))
    assert why in str(refused.value)


@pytest.mark.asyncio
async def test_a_root_whose_ownership_cannot_be_read_is_refused_not_created():
    """`unreadable` is not `missing`: the root is refused, never taken for absent."""
    shell = _FakeParentShell({ROOT: "unreadable"})
    with pytest.raises(StagingRefusedError, match="cannot be read"):
        await ensure_private_root(_wired(shell))


@pytest.mark.asyncio
async def test_a_root_that_could_not_be_created_is_refused():
    shell = _FakeParentShell({})

    async def _read_only_tmp(command, *_a, **_kw):
        shell.commands.append(command)
        return CommandResult(
            Status.Success, value=f"{SESSION_UID}\ndirectory 0 1777\nmissing\n", command=command
        )

    parent = _parent()
    parent.exec = AsyncMock(side_effect=_read_only_tmp)  # type: ignore[method-assign]
    with pytest.raises(StagingRefusedError, match="could not be created"):
        await ensure_private_root(parent)


@pytest.mark.parametrize(
    "answer",
    ["", "not a uid\n", "1000\ndirectory 0 1777\n", "1000\ndirectory 0 1777\nwho knows\n"],
)
@pytest.mark.asyncio
async def test_an_answer_otto_cannot_read_is_refused_never_trusted(answer):
    parent = _parent()
    parent.exec = AsyncMock(  # type: ignore[method-assign]
        return_value=CommandResult(Status.Success, value=answer, command="")
    )
    with pytest.raises(StagingRefusedError, match="could not read the type, owner and mode"):
        await ensure_private_root(parent)


@pytest.mark.asyncio
async def test_a_probe_that_never_completed_is_an_unreachable_parent():
    parent = _parent()
    parent.exec = AsyncMock(  # type: ignore[method-assign]
        return_value=CommandResult(Status.Failed, value="", command="", retcode=-1, timed_out=True)
    )
    with pytest.raises(HostUnreachableError):
        await ensure_private_root(parent)


@pytest.mark.asyncio
async def test_a_dry_run_decline_is_never_read_as_an_answer():
    parent = _parent()
    parent.exec = AsyncMock(  # type: ignore[method-assign]
        return_value=NotRunResult(
            status=Status.NotRun, command="probe", retcode=-1, host_name="test3"
        )
    )
    with pytest.raises(CommandNotRunError):
        await probe_ownership(parent, [ROOT])


@pytest.mark.asyncio
async def test_a_hostile_path_is_one_quoted_operand():
    """A path is data to the probe, never shell text."""
    hostile = Path("/tmp/x'; touch /tmp/pwned; '")
    shell = _FakeParentShell({})
    parent = _wired(shell)

    await probe_ownership(parent, [hostile], create=hostile)

    (command,) = shell.commands
    assert shlex.quote(str(hostile)) in command
    assert "; touch /tmp/pwned;" not in command.replace(shlex.quote(str(hostile)), "")


# ---------------------------------------------------------------------------
# A container's transfers stage under the root, and only once it is trusted
# ---------------------------------------------------------------------------


def _container_on(parent: UnixHost) -> DockerContainerHost:
    return DockerContainerHost(
        parent=parent,
        container_id="abc123def456",
        project="project1",
        service="api",
        compose_project="otto-project1-alice",
    )


@pytest.mark.asyncio
async def test_a_container_put_stages_under_the_private_root(tmp_path):
    shell = _FakeParentShell({})
    parent = _parent()
    staged_to: list[Path] = []

    async def _exec(command, *a, **kw):
        if command.startswith(("mkdir -m 0700", "id -u")):
            return await shell.exec(command)
        return CommandResult(Status.Success, value="", command=command)

    async def _put(files, dest, *a, **kw):
        staged_to.append(dest)
        return Result(Status.Success, value={f: Result(Status.Success) for f in files})

    parent.exec = AsyncMock(side_effect=_exec)  # type: ignore[method-assign]
    parent.put = AsyncMock(side_effect=_put)  # type: ignore[method-assign]
    payload = tmp_path / "payload.bin"
    payload.write_bytes(b"x")

    await _container_on(parent).put([payload], Path("/srv"))

    (dest,) = staged_to
    assert dest.parent == ROOT / ".transfer", dest
    assert dest.name.startswith("abc123def456-"), dest


@pytest.mark.asyncio
async def test_a_container_put_to_an_untrusted_root_sends_nothing(tmp_path):
    shell = _FakeParentShell({ROOT: "directory 1001 777"})
    parent = _wired(shell)
    parent.put = AsyncMock()  # type: ignore[method-assign]
    payload = tmp_path / "payload.bin"
    payload.write_bytes(b"x")

    with pytest.raises(StagingRefusedError, match=str(ROOT)):
        await _container_on(parent).put([payload], Path("/srv"))

    parent.put.assert_not_awaited()
    assert len(shell.commands) == 1, shell.commands


# ---------------------------------------------------------------------------
# What the probe prints on a real system, read back
# ---------------------------------------------------------------------------


def _probe_locally(paths: "list[Path]", create: Path, path_env: "str | None" = None) -> str:
    """Run the probe command under this machine's /bin/sh, as a parent's shell would."""
    env = {"PATH": path_env} if path_env is not None else None
    done = subprocess.run(
        ["/bin/sh", "-c", _ownership_command(paths, create)],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
        env=env,
    )
    return done.stdout


def _applets(tmp_path: Path, names: "list[str]", binary: "str | None" = None) -> str:
    """A PATH holding only *names*: this machine's tools, or one multi-call *binary*."""
    bindir = tmp_path / "bin"
    bindir.mkdir()
    for name in names:
        target = binary or shutil.which(name)
        assert target, f"{name} is not installed here"
        (bindir / name).symlink_to(target)
    return str(bindir)


@pytest.mark.parametrize("fallback", [False, True], ids=["stat", "ls-ldn"])
def test_the_probe_reads_this_systems_answer(tmp_path, fallback):
    """Created 0700 and owned by us; a link reported as a link; a missing path as missing.

    The ``ls-ldn`` row hides ``stat``, so the fallback a stat-less BusyBox
    takes is what answers.
    """
    root = tmp_path / "otto-docker-me"
    link = tmp_path / "link"
    link.symlink_to(tmp_path)
    shared = tmp_path / "shared"
    shared.mkdir(mode=0o777)
    shared.chmod(0o1777)
    paths = [shared, root, link, tmp_path / "absent"]
    path_env = _applets(tmp_path, ["ls", "id", "mkdir"]) if fallback else None

    probe = _read_ownership(_probe_locally(paths, root, path_env), paths)

    assert probe is not None
    assert probe.paths[shared] is not None
    assert probe.paths[shared].mode == 0o1777
    assert probe.problem(root, kind="directory", private=True) is None
    assert probe.problem(link, kind="directory", private=False) == (
        "it is a symbolic link, not a directory"
    )
    assert probe.missing(tmp_path / "absent")


def test_a_path_behind_an_unsearchable_directory_reads_unreadable_not_missing(tmp_path):
    """The real command, against a directory it cannot search: unknown, never absent.

    A path under a directory that is itself absent is still missing.
    """
    locked = tmp_path / "locked"
    (locked / "inner").mkdir(parents=True)
    hidden = locked / "inner"
    absent = tmp_path / "absent" / "deeper"
    paths = [hidden, absent]
    locked.chmod(0)
    try:
        probe = _read_ownership(_probe_locally(paths, tmp_path / "otto-docker-me"), paths)
    finally:
        locked.chmod(0o700)

    assert probe is not None
    assert probe.unreadable == [hidden]
    assert not probe.missing(hidden)
    assert probe.problem(hidden, kind="directory", private=False) == (
        "its type, owner and mode cannot be read"
    )
    assert probe.missing(absent)


@pytest.mark.skipif(shutil.which("busybox") is None, reason="no system busybox")
def test_the_probe_reads_the_system_busyboxs_ls(tmp_path):
    """BusyBox pads ``ls -ldn``'s columns differently; the fallback still reads it."""
    root = tmp_path / "otto-docker-me"
    path_env = _applets(tmp_path, ["ls", "id", "mkdir"], binary=shutil.which("busybox"))

    probe = _read_ownership(_probe_locally([root], root, path_env), [root])

    assert probe is not None
    assert probe.problem(root, kind="directory", private=True) is None


@pytest.mark.parametrize(
    ("line", "kind", "uid", "mode"),
    [
        ("directory 1000 700", "directory", 1000, 0o700),
        ("regular empty file 0 644", "regular file", 0, 0o644),
        ("directory 0 1777", "directory", 0, 0o1777),
        ("drwx------    2 1000     1000          40 Oct 29 10:00 /r", "directory", 1000, 0o700),
        ("drwxrwxrwt 12 0 0 300 Oct 29 10:00 /tmp", "directory", 0, 0o1777),
        ("lrwxrwxrwx 1 1001 1001 5 Oct 29 10:00 /r -> /etc", "symbolic link", 1001, 0o777),
        ("-rw-------. 1 1000 1000 12 Oct 29 10:00 /f", "regular file", 1000, 0o600),
        ("drwsr-S--T+ 2 5 5 4 Oct 29 10:00 /d", "directory", 5, 0o7740),
    ],
)
def test_each_answer_form_reads_as_type_owner_and_mode(line, kind, uid, mode):
    probe = _read_ownership(f"1000\n{line}\n", [ROOT])
    assert probe is not None
    found = probe.paths[ROOT]
    assert found is not None
    assert (found.kind, found.uid, found.mode) == (kind, uid, mode)
